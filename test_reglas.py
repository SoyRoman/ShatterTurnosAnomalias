"""
Pruebas de las funciones puras de motor_reglas.py.

    python test_reglas.py

No necesita PostgreSQL ni el Excel: construye turnos sintéticos y comprueba
los detectores directamente. Cubre sobre todo las trampas del dominio que
documenta CLAUDE.md §7, que son las que ya costaron trabajo descubrir una vez:

  * el intermedio intradía (Art. 167 CST) NO es descanso entre jornadas
  * los turnos que cruzan medianoche duran 12h, no -12h
  * el patrón 2x2x2 alineado no debe generar falsos positivos
  * dos slots del mismo puesto sí pueden cruzarse entre sí

La regresión contra datos reales (386 hallazgos / 166 guardas de julio 2026)
es complementaria a esto y se corre con `python motor_reglas.py`.
"""

import datetime as dt
import sys
import types

# Solo se prueban funciones puras, que no tocan la BD. Se stubea psycopg2 para
# que las pruebas corran en cualquier parte, sin conexión ni credenciales.
_pg = types.ModuleType('psycopg2')
_pg.extras = types.ModuleType('psycopg2.extras')
sys.modules.setdefault('psycopg2', _pg)
sys.modules.setdefault('psycopg2.extras', _pg.extras)

import motor_reglas as m  # noqa: E402

fallos = []


def check(nombre, condicion):
    print(f"[{'OK ' if condicion else 'FALLA'}] {nombre}")
    if not condicion:
        fallos.append(nombre)


def T(h, mi=0):
    return dt.time(h, mi)


def turno(id, puesto_id, fecha, hi, hf, categoria='TRABAJADO', horas=None):
    inicio_dt = fin_dt = None
    if hi and hf:
        inicio_dt = dt.datetime.combine(fecha, hi)
        fin_dt = dt.datetime.combine(fecha, hf)
        if fin_dt <= inicio_dt:
            fin_dt += dt.timedelta(days=1)   # cruza medianoche
        if horas is None:
            horas = round((fin_dt - inicio_dt).total_seconds() / 3600.0, 2)
    return {
        'id': id, 'puesto_id': puesto_id, 'puesto_nombre': f'P{puesto_id}',
        'fecha': fecha, 'codigo': 'X', 'categoria': categoria,
        'horas': horas, 'inicio_dt': inicio_dt, 'fin_dt': fin_dt,
    }


d = dt.date(2026, 7, 1)

# --- CRUCE_DE_HORARIO ---
check("cruce entre puestos distintos se detecta",
      len(m.detectar_cruce_horario([
          turno(1, 10, d, T(6), T(18)),
          turno(2, 20, d, T(14), T(22)),
      ])) == 1)

check("un solo turno no genera cruce",
      len(m.detectar_cruce_horario([turno(1, 10, d, T(6), T(18))])) == 0)

# Antes del fix de `slot` estas dos filas ni siquiera coexistían en `turnos`,
# así que el detector tenía que excluir el mismo puesto. Ya no.
check("dos slots del MISMO puesto que se solapan sí son cruce",
      len(m.detectar_cruce_horario([
          turno(1, 10, d, T(6), T(14)),
          turno(2, 10, d, T(13), T(21)),
      ])) == 1)

check("cada cruce trae clave propia (para la huella)",
      'clave' in m.detectar_cruce_horario([
          turno(1, 10, d, T(6), T(18)),
          turno(2, 20, d, T(14), T(22)),
      ])[0])

# --- Turno nocturno que cruza medianoche ---
tf = turno(1, 10, d, T(18), T(6))
check("turno F 18:00-06:00 dura 12h y termina después de empezar",
      tf['horas'] == 12.0 and tf['fin_dt'] > tf['inicio_dt'])

# --- DESCANSO_ENTRE_JORNADAS (la trampa del Art. 167) ---
check("intermedio intradía NO es descanso insuficiente entre jornadas",
      len(m.detectar_descanso_insuficiente([
          turno(1, 10, d, T(6), T(10)),
          turno(2, 10, d, T(11), T(18)),
      ], umbral_horas=12)) == 0)

check("4h entre la jornada de un día y la del siguiente sí se detecta",
      len(m.detectar_descanso_insuficiente([
          turno(1, 10, dt.date(2026, 7, 7), T(18), T(6)),   # termina el 8 a las 06:00
          turno(2, 10, dt.date(2026, 7, 8), T(10), T(18)),  # arranca el 8 a las 10:00
      ], umbral_horas=12)) == 1)

check("un día de descanso de por medio no genera violación",
      len(m.detectar_descanso_insuficiente([
          turno(1, 10, dt.date(2026, 7, 10), T(6), T(18)),
          turno(2, 10, dt.date(2026, 7, 11), T(6), T(18)),
          # 12 de descanso
          turno(3, 10, dt.date(2026, 7, 13), T(6), T(18)),
      ], umbral_horas=12)) == 0)

# --- JORNADA_DIARIA_SUPERA_12H ---
largo = turno(1, 10, d, T(18), None, horas=14.0)
largo['inicio_dt'] = dt.datetime.combine(d, T(18))
largo['fin_dt'] = largo['inicio_dt'] + dt.timedelta(hours=14)
check("14h en un día dispara", len(m.detectar_jornada_diaria_excesiva([largo], 12)) == 1)
check("exactamente 12h NO dispara (el umbral es estrictamente mayor)",
      len(m.detectar_jornada_diaria_excesiva([turno(1, 10, d, T(6), T(18))], 12)) == 0)

# --- Patrón 2x2x2: E,E,F,F,descanso,descanso ---
patron, tid = [], 1
for i in range(30):
    codigo = ['E', 'E', 'F', 'F', None, None][i % 6]
    fecha = d + dt.timedelta(days=i)
    if codigo == 'E':
        patron.append(turno(tid, 10, fecha, T(6), T(18)))
    elif codigo == 'F':
        patron.append(turno(tid, 10, fecha, T(18), T(6)))
    else:
        continue
    tid += 1
check("patrón 2x2x2 alineado no genera falsos positivos de racha ni descanso",
      not m.detectar_racha_sin_descanso(patron, 6) and not m.detectar_sin_descanso_semanal(patron))

# --- Rachas ---
racha8 = [turno(i + 1, 10, d + dt.timedelta(days=i), T(6), T(18)) for i in range(8)]
r = m.detectar_racha_sin_descanso(racha8, 6)
check("8 días seguidos dispara RACHA_SIN_DESCANSO (una fila por racha)",
      len(r) == 1 and r[0]['detalle'].startswith('8 dias'))

s = m.detectar_sin_descanso_semanal(racha8)
check("8 días seguidos dispara SIN_DESCANSO_SEMANAL con 2 ventanas (N-6)",
      len(s) == 1 and s[0]['detalle'].startswith('2 semana'))

check("SIN_DESCANSO_SEMANAL se identifica por periodo, no por día",
      s[0]['clave'] == '2026-07')

# --- DESCUADRE_HORAS_DECLARADAS ---
cuadrada = {'horas_diurnas_ordinarias': 100, 'horas_diurnas_festivas': 10,
            'horas_nocturnas_ordinarias': 50, 'horas_nocturnas_festivas': 0,
            'total_horas': 160}
check("fila cuadrada no dispara", m.detectar_descuadre_horas(cuadrada) is None)
check("fila descuadrada dispara", m.detectar_descuadre_horas(dict(cuadrada, total_horas=252)) is not None)

# --- Huella: estable ante cambios de texto, distinta por violación ---
h1 = m.calcular_huella('CRUCE_DE_HORARIO', '123', '2026-07-09|10@06:00')
h2 = m.calcular_huella('CRUCE_DE_HORARIO', '123', '2026-07-09|10@06:00')
h3 = m.calcular_huella('CRUCE_DE_HORARIO', '123', '2026-07-10|10@06:00')
check("misma violación => misma huella (no se duplica entre corridas)", h1 == h2)
check("violación distinta => huella distinta", h1 != h3)

print()
if fallos:
    print(f"FALLARON {len(fallos)}: " + ", ".join(fallos))
    sys.exit(1)
print("RESULTADO: todas las pruebas pasaron")
