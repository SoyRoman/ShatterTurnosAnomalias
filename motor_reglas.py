"""
Motor de reglas de anomalias sobre la malla de turnos.

Uso:
    python motor_reglas.py                    # evalua todos los guardas
    python motor_reglas.py --cedula 1234567    # evalua solo un guarda
    python motor_reglas.py --dry-run           # imprime hallazgos, no escribe en anomalias

Lee turnos de PostgreSQL (consolidados por cedula a traves de TODOS los
puestos, nunca puesto por puesto), evalua las 7 reglas de reglas_anomalia
consultando el umbral vigente para cada fecha, y escribe en anomalias.

Idempotente en el sentido que importa aqui: cada corrida borra y regenera
las anomalias en estado ABIERTA para las reglas y guardas evaluados. Las que
ya estan en EN_REVISION, TICKET_CREADO o CERRADA no se tocan -- alguien ya
las reviso, no se resucitan ni se duplican en la siguiente corrida mensual.

Las funciones detectar_* son puras (reciben turnos, devuelven violaciones)
para poder reutilizarse en modo predictivo -- validar una asignacion nueva
antes de guardarla -- y no solo en esta auditoria retrospectiva.
"""

import argparse
import hashlib
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv


def calcular_huella(codigo_regla, cedula, clave):
    """Identidad estable de una anomalia, derivada de llaves de NEGOCIO.

    `clave` la construye cada detector con lo que hace unica a esa violacion
    (la fecha, el par de puestos que se cruzan, el periodo...). Nunca se
    incluye el texto de `detalle` ni los ids de `turnos`: el primero cambia
    si se reescribe un mensaje y los segundos cambian si se recarga el ETL,
    y en ambos casos la anomalia se duplicaria contra la que ya tiene ticket.
    """
    base = f"{codigo_regla}|{cedula}|{clave}"
    return hashlib.md5(base.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------
# Conexion y reglas vigentes
# --------------------------------------------------------------------------

def get_connection():
    load_dotenv()
    return psycopg2.connect(
        host=os.environ['DB_HOST'],
        port=os.environ.get('DB_PORT', '5432'),
        dbname=os.environ['DB_NAME'],
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        sslmode=os.environ.get('DB_SSLMODE', 'prefer'),
    )


def cargar_reglas_vigentes(cur):
    """{codigo: [version, ...]} ordenadas por vigente_desde. Puede haber mas
    de una version por codigo (p.ej. si mas adelante se parte un umbral en
    dos tramos de fecha, como el 44h/42h de referencia de la Ley 2101)."""
    cur.execute(
        """SELECT codigo, id, severidad_default, parametros, vigente_desde, vigente_hasta
           FROM reglas_anomalia ORDER BY codigo, vigente_desde"""
    )
    reglas = defaultdict(list)
    for codigo, rid, severidad, parametros, desde, hasta in cur.fetchall():
        reglas[codigo].append({
            'id': rid, 'severidad': severidad, 'parametros': parametros or {},
            'vigente_desde': desde, 'vigente_hasta': hasta,
        })
    return reglas


def regla_vigente_en(reglas, codigo, fecha):
    """La version de `codigo` vigente para `fecha`, o None si ninguna aplica
    (regla retirada o fecha anterior a que existiera)."""
    if fecha is None:
        return None
    for r in reglas.get(codigo, []):
        if r['vigente_desde'] and fecha < r['vigente_desde']:
            continue
        if r['vigente_hasta'] and fecha > r['vigente_hasta']:
            continue
        return r
    return None


# --------------------------------------------------------------------------
# Carga de turnos consolidados por guarda (todos los puestos)
# --------------------------------------------------------------------------

def cargar_guardas(cur, cedula_filtro=None):
    if cedula_filtro:
        return [cedula_filtro]
    cur.execute("SELECT cedula FROM guardas ORDER BY cedula")
    return [r[0] for r in cur.fetchall()]


def cargar_turnos_guarda(cur, cedula):
    """Turnos del guarda a traves de TODOS sus puestos, con timestamps
    absolutos (fecha+hora) ya resueltos y el cruce de medianoche aplicado.
    Es indispensable consolidar asi: los cruces de horario reales aparecen
    justamente entre puestos distintos, no dentro del mismo puesto."""
    cur.execute(
        """SELECT t.id, t.puesto_id, p.nombre, t.fecha, t.tipo_turno_codigo,
                  t.hora_inicio, t.hora_fin, t.horas_calculadas, tt.categoria
           FROM turnos t
           JOIN tipos_turno tt ON tt.codigo = t.tipo_turno_codigo
           JOIN puestos p ON p.id = t.puesto_id
           WHERE t.guarda_cedula = %s
           ORDER BY t.fecha""",
        (cedula,),
    )
    turnos = []
    for tid, puesto_id, puesto_nombre, fecha, codigo, hi, hf, horas, categoria in cur.fetchall():
        inicio_dt = fin_dt = None
        if hi and hf:
            inicio_dt = datetime.combine(fecha, hi)
            fin_dt = datetime.combine(fecha, hf)
            if fin_dt <= inicio_dt:
                fin_dt += timedelta(days=1)  # turno cruza medianoche (p.ej. F 18:00-06:00)
        turnos.append({
            'id': tid, 'puesto_id': puesto_id, 'puesto_nombre': puesto_nombre,
            'fecha': fecha, 'codigo': codigo, 'categoria': categoria,
            'horas': float(horas) if horas is not None else None,
            'inicio_dt': inicio_dt, 'fin_dt': fin_dt,
        })
    return turnos


# --------------------------------------------------------------------------
# Reglas (funciones puras: turnos -> violaciones)
# --------------------------------------------------------------------------

def detectar_cruce_horario(turnos):
    """Turnos TRABAJADOS con horario conocido que se solapan en el tiempo:
    fisicamente imposible de cumplir, sea en puestos distintos o en el
    MISMO puesto (dos slots concurrentes del mismo guarda -- p.ej. su turno
    regular mas un turno ADI que se le empalma -- son un cruce igual de
    real, ahora que `turnos.slot` evita que el ETL los colapse en una sola
    fila). Sweep ordenado por inicio; para cada turno nuevo, cualquier
    turno "activo" que no haya terminado todavia es un cruce."""
    bloques = sorted(
        (t for t in turnos if t['categoria'] == 'TRABAJADO' and t['inicio_dt'] and t['fin_dt']),
        key=lambda t: t['inicio_dt'],
    )
    violaciones = []
    activos = []
    for t in bloques:
        activos = [a for a in activos if a['fin_dt'] > t['inicio_dt']]
        for a in activos:
            # Un guarda puede tener mas de un cruce el mismo dia, asi que la
            # fecha no basta: la clave incluye el par de bloques que chocan,
            # ordenado para que no dependa de cual se leyo primero.
            par = sorted([
                (a['puesto_id'], a['inicio_dt'].isoformat()),
                (t['puesto_id'], t['inicio_dt'].isoformat()),
            ])
            violaciones.append({
                'clave': f"{t['fecha']}|{par[0][0]}@{par[0][1]}|{par[1][0]}@{par[1][1]}",
                'puesto_id': t['puesto_id'],
                'fecha_referencia': t['fecha'],
                'turnos': [a['id'], t['id']],
                'detalle': (
                    f"Cruce de horario: puesto '{a['puesto_nombre']}' "
                    f"({a['inicio_dt']:%Y-%m-%d %H:%M}-{a['fin_dt']:%Y-%m-%d %H:%M}) "
                    f"vs puesto '{t['puesto_nombre']}' "
                    f"({t['inicio_dt']:%Y-%m-%d %H:%M}-{t['fin_dt']:%Y-%m-%d %H:%M})"
                ),
            })
        activos.append(t)
    return violaciones


def detectar_descanso_insuficiente(turnos, umbral_horas):
    """Menos de `umbral_horas` continuas entre el fin de la ultima actividad
    de un dia activo y el inicio de la primera del SIGUIENTE dia activo (no
    necesariamente el dia calendario inmediato: puede haber dias de descanso
    entre medio, y esos no cuentan). El hueco intradia entre dos bloques del
    mismo dia (Art. 167 CST) nunca se mide aqui porque se agrupa por dia."""
    por_dia = defaultdict(list)
    for t in turnos:
        if t['categoria'] == 'TRABAJADO' and t['inicio_dt'] and t['fin_dt']:
            por_dia[t['fecha']].append(t)
    dias = sorted(por_dia)
    violaciones = []
    for d_prev, d_next in zip(dias, dias[1:]):
        fin_prev = max(t['fin_dt'] for t in por_dia[d_prev])
        inicio_next = min(t['inicio_dt'] for t in por_dia[d_next])
        gap_horas = (inicio_next - fin_prev).total_seconds() / 3600.0
        if gap_horas < umbral_horas:
            turnos_prev = [t['id'] for t in por_dia[d_prev] if t['fin_dt'] == fin_prev]
            turnos_next = [t['id'] for t in por_dia[d_next] if t['inicio_dt'] == inicio_next]
            violaciones.append({
                'clave': str(d_next),  # un solo descanso insuficiente por dia de reinicio
                'fecha_referencia': d_next,
                'turnos': turnos_prev + turnos_next,
                'detalle': (
                    f"Descanso de {gap_horas:.1f}h entre la jornada del {d_prev} y la del "
                    f"{d_next} (minimo {umbral_horas}h)"
                ),
            })
    return violaciones


def detectar_jornada_diaria_excesiva(turnos, umbral_horas):
    """Suma de horas trabajadas en un mismo dia CALENDARIO (la fecha propia
    de cada fila, no el timestamp absoluto de fin) mayor al tope."""
    por_dia = defaultdict(list)
    for t in turnos:
        if t['categoria'] == 'TRABAJADO' and t['horas']:
            por_dia[t['fecha']].append(t)
    violaciones = []
    for d, ts in por_dia.items():
        total = sum(t['horas'] for t in ts)
        if total > umbral_horas:
            violaciones.append({
                'clave': str(d),  # una sola jornada excesiva por dia calendario
                'fecha_referencia': d,
                'turnos': [t['id'] for t in ts],
                'detalle': f"{total:.1f}h trabajadas el {d} (maximo {umbral_horas}h)",
            })
    return violaciones


def detectar_semana_excesiva(turnos, umbral_horas):
    """Ventana movil de cualquier 7 dias consecutivos con mas de
    `umbral_horas`. Se reporta UN hallazgo por guarda Y POR MES -- cuenta
    cuantas ventanas violatorias empezaron en ese mes y referencia la de mas
    horas -- en vez de una fila por ventana o por racha. Asi es como modela
    esta convencion el analisis manual de referencia (386 hallazgos de julio
    2026): nunca hay mas de 1 fila por guarda y mes para esta regla.

    Cada ventana pertenece al mes de su dia de INICIO (misma convencion en
    detectar_sin_descanso_semanal y detectar_racha_sin_descanso).

    OJO -- agrupar por mes NO es cosmetico, es obligatorio. La version
    anterior agregaba sobre TODO el rango evaluado y devolvia una sola fila
    por guarda. Con un solo mes cargado las dos cosas son identicas, por eso
    la validacion de julio pasaba; pero al cargar varios meses la semana pico
    se corria a otro mes, la `clave` cambiaba y la huella del mes anterior
    DESAPARECIA. Como `marcar_resueltas` da por RESUELTA toda huella que ya
    no aparece, el sistema le decia al usuario "tu correccion llego" sobre una
    violacion que nadie corrigio. Verificado con julio+agosto+septiembre
    cargados: se perdian 6 hallazgos de julio. No vuelvas a agregar sobre el
    rango completo."""
    horas_por_dia = defaultdict(float)
    turnos_por_dia = defaultdict(list)
    for t in turnos:
        if t['categoria'] == 'TRABAJADO' and t['horas']:
            horas_por_dia[t['fecha']] += t['horas']
            turnos_por_dia[t['fecha']].append(t['id'])
    if not horas_por_dia:
        return []

    d_min, d_max = min(horas_por_dia), max(horas_por_dia)
    por_mes = {}
    d = d_min
    while d <= d_max:
        ventana = [d + timedelta(days=i) for i in range(7)]
        total = sum(horas_por_dia.get(dd, 0.0) for dd in ventana)
        if total > umbral_horas:
            mes = por_mes.setdefault(f"{d:%Y-%m}", {
                'conteo': 0, 'max_horas': None, 'dia_max': None, 'ids': set()})
            mes['conteo'] += 1
            for dd in ventana:
                mes['ids'].update(turnos_por_dia.get(dd, []))
            if mes['max_horas'] is None or total > mes['max_horas']:
                mes['max_horas'], mes['dia_max'] = total, d
        d += timedelta(days=1)

    return [{
        # La clave es el periodo: si al recargar la malla la semana pico se
        # corre de dia dentro del mismo mes, sigue siendo la misma anomalia y
        # no se duplica contra la que ya este en gestion.
        'clave': clave,
        'fecha_referencia': m['dia_max'],
        'turnos': sorted(m['ids']),
        'detalle': (
            f"{m['conteo']} semana(s) movil(es) del mes superan el tope legal de "
            f"{umbral_horas}h; la mas alta: {m['max_horas']:.1f}h en semana que "
            f"inicia el dia {m['dia_max'].day}"
        ),
    } for clave, m in sorted(por_mes.items())]


def _rachas_dias_trabajados(turnos):
    """Rachas de dias calendario consecutivos con al menos un turno
    TRABAJADO: [{'inicio': fecha, 'fin': fecha, 'turnos': [ids]}, ...]."""
    dias_trabajados = defaultdict(list)
    for t in turnos:
        if t['categoria'] == 'TRABAJADO':
            dias_trabajados[t['fecha']].append(t['id'])
    if not dias_trabajados:
        return []

    dias = sorted(dias_trabajados)
    rachas = []
    inicio = prev = dias[0]
    ids = list(dias_trabajados[dias[0]])
    for d in dias[1:]:
        if d == prev + timedelta(days=1):
            ids.extend(dias_trabajados[d])
            prev = d
        else:
            rachas.append({'inicio': inicio, 'fin': prev, 'turnos': ids})
            inicio = prev = d
            ids = list(dias_trabajados[d])
    rachas.append({'inicio': inicio, 'fin': prev, 'turnos': ids})
    return rachas


def detectar_racha_sin_descanso(turnos, umbral_dias):
    violaciones = []
    for racha in _rachas_dias_trabajados(turnos):
        n_dias = (racha['fin'] - racha['inicio']).days + 1
        if n_dias > umbral_dias:
            violaciones.append({
                'clave': str(racha['inicio']),  # una fila por racha, identificada por su inicio
                'fecha_referencia': racha['inicio'],
                'turnos': racha['turnos'],
                'detalle': (
                    f"{n_dias} dias consecutivos trabajados sin descanso, del "
                    f"{racha['inicio']} al {racha['fin']} (maximo {umbral_dias})"
                ),
            })
    return violaciones


def detectar_sin_descanso_semanal(turnos):
    """Cuenta cuantas ventanas de 7 dias calendario sin ningun dia de
    descanso hubo (una racha de N>=7 dias consecutivos trabajados contiene
    N-6 de esas ventanas) y reporta UN hallazgo por guarda Y POR MES, igual
    que detectar_semana_excesiva. Aunque la condicion de base es la misma
    racha que usa RACHA_SIN_DESCANSO (Art. 172/175 CST: el descanso semanal
    es diferible hasta por 6 dias), aqui se cuenta por ventana y se colapsa a
    1 fila por mes porque asi la modela el analisis manual de referencia --
    RACHA_SIN_DESCANSO en cambio reporta una fila POR racha (ver esa funcion).

    Cada ventana pertenece al mes de su dia de INICIO. Ver
    detectar_semana_excesiva para por que agrupar por mes es obligatorio y no
    cosmetico (una racha a caballo entre dos meses hacia desaparecer la huella
    del mes anterior, y eso la marcaba RESUELTA sin que nadie corrigiera nada)."""
    turnos_por_dia = defaultdict(list)
    for t in turnos:
        if t['categoria'] == 'TRABAJADO':
            turnos_por_dia[t['fecha']].append(t['id'])

    por_mes = {}
    for racha in _rachas_dias_trabajados(turnos):
        n_dias = (racha['fin'] - racha['inicio']).days + 1
        for i in range(n_dias - 6):  # 0 iteraciones si la racha no llega a 7 dias
            inicio_ventana = racha['inicio'] + timedelta(days=i)
            mes = por_mes.setdefault(f"{inicio_ventana:%Y-%m}", {
                'ventanas': 0, 'primera': None, 'ids': set()})
            mes['ventanas'] += 1
            for j in range(7):
                mes['ids'].update(turnos_por_dia.get(inicio_ventana + timedelta(days=j), []))
            if mes['primera'] is None or inicio_ventana < mes['primera']:
                mes['primera'] = inicio_ventana

    return [{
        'clave': clave,  # un hallazgo por guarda y mes
        'fecha_referencia': m['primera'],
        'turnos': sorted(m['ids']),
        'detalle': (
            f"{m['ventanas']} semana(s) movil(es) del mes sin ningun dia de descanso "
            f"(Art. 172/175 CST); ej. semana que inicia el dia {m['primera'].day}"
        ),
    } for clave, m in sorted(por_mes.items())]


def detectar_descuadre_horas(fila):
    """fila: dict con las 4 categorias declaradas + total_horas de una fila
    de horas_declaradas_mes. Devuelve un dict de violacion o None."""
    categorias = [
        'horas_diurnas_ordinarias', 'horas_diurnas_festivas',
        'horas_nocturnas_ordinarias', 'horas_nocturnas_festivas',
    ]
    if fila['total_horas'] is None:
        return None
    suma = sum(float(fila[c]) for c in categorias if fila[c] is not None)
    total = float(fila['total_horas'])
    if abs(total - suma) > 0.01:
        return {
            'detalle': (
                f"Total Horas declarado = {total:.1f}, suma de las 4 categorias = "
                f"{suma:.1f} (diferencia {total - suma:+.1f}h)"
            ),
        }
    return None


# --------------------------------------------------------------------------
# Orquestacion: aplica las reglas vigentes a un guarda
# --------------------------------------------------------------------------

def evaluar_guarda(turnos, reglas):
    """Corre los detectores de turnos contra un guarda ya consolidado y
    devuelve [(codigo_regla, violacion), ...]. El umbral usado para escanear
    es el de la version vigente MAS RECIENTE de cada regla; la version final
    que se guarda en cada anomalia se resuelve aparte, por la
    fecha_referencia de esa violacion puntual (ver regla_vigente_en en el
    caller). Si el futuro trae una segunda version de una regla con umbral
    distinto para un tramo de fechas ya cerrado, evalua ese tramo aparte con
    --desde/--hasta en vez de fiarte de este escaneo unico."""
    hallazgos = []  # cada codigo puede aportar 0, 1 o varias violaciones (ver cada detector)

    if 'CRUCE_DE_HORARIO' in reglas:
        for v in detectar_cruce_horario(turnos):
            hallazgos.append(('CRUCE_DE_HORARIO', v))

    if 'DESCANSO_ENTRE_JORNADAS_INSUFICIENTE' in reglas:
        umbral = reglas['DESCANSO_ENTRE_JORNADAS_INSUFICIENTE'][-1]['parametros'].get('umbral_horas', 12)
        for v in detectar_descanso_insuficiente(turnos, umbral):
            hallazgos.append(('DESCANSO_ENTRE_JORNADAS_INSUFICIENTE', v))

    if 'JORNADA_DIARIA_SUPERA_12H' in reglas:
        umbral = reglas['JORNADA_DIARIA_SUPERA_12H'][-1]['parametros'].get('umbral_horas', 12)
        for v in detectar_jornada_diaria_excesiva(turnos, umbral):
            hallazgos.append(('JORNADA_DIARIA_SUPERA_12H', v))

    if 'SEMANA_SUPERA_60H' in reglas:
        umbral = reglas['SEMANA_SUPERA_60H'][-1]['parametros'].get('umbral_horas', 60)
        for v in detectar_semana_excesiva(turnos, umbral):
            hallazgos.append(('SEMANA_SUPERA_60H', v))

    if 'SIN_DESCANSO_SEMANAL' in reglas:
        for v in detectar_sin_descanso_semanal(turnos):
            hallazgos.append(('SIN_DESCANSO_SEMANAL', v))

    if 'RACHA_SIN_DESCANSO' in reglas:
        umbral = reglas['RACHA_SIN_DESCANSO'][-1]['parametros'].get('umbral_dias', 6)
        for v in detectar_racha_sin_descanso(turnos, umbral):
            hallazgos.append(('RACHA_SIN_DESCANSO', v))

    return hallazgos


# --------------------------------------------------------------------------
# Escritura en PostgreSQL
# --------------------------------------------------------------------------

def borrar_abiertas(cur, regla_ids, cedula=None, desde=None, hasta=None):
    """Limpia las anomalias ABIERTA que el escaneo va a regenerar.

    Nunca toca las que alguien ya gestiono: las que salieron de ABIERTA
    (EN_REVISION / TICKET_CREADO / CERRADA) y tampoco las que, aun estando
    ABIERTA, tienen historial — alguien las miro y dejo una nota.
    """
    if not regla_ids:
        return
    sql = [
        "DELETE FROM anomalias a",
        "WHERE a.estado = 'ABIERTA'",
        "  AND a.regla_id = ANY(%s)",
        "  AND NOT EXISTS (SELECT 1 FROM anomalias_historial h WHERE h.anomalia_id = a.id)",
    ]
    params = [regla_ids]
    if cedula:
        sql.append("  AND a.guarda_cedula = %s")
        params.append(cedula)
    if desde:
        sql.append("  AND a.fecha_referencia >= %s")
        params.append(desde)
    if hasta:
        sql.append("  AND a.fecha_referencia <= %s")
        params.append(hasta)
    cur.execute("\n".join(sql), params)


def insertar_anomalia(cur, regla, codigo_regla, cedula, violacion):
    """Inserta la anomalia si su huella no existe todavia.

    Si ya existe se refresca solo el texto descriptivo y el contexto, pero
    NUNCA el `estado` ni la `nota`: eso es trabajo humano y una nueva corrida
    del motor no puede pisarlo. Es lo que evita que una anomalia ya revisada
    o justificada vuelva a aparecer como nueva en la bandeja cada mes.
    """
    huella = calcular_huella(codigo_regla, cedula, violacion['clave'])
    cur.execute(
        """INSERT INTO anomalias
               (huella, regla_id, guarda_cedula, puesto_id, fecha_referencia,
                severidad, detalle, turnos_involucrados)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (huella) DO UPDATE
               SET detalle = EXCLUDED.detalle,
                   severidad = EXCLUDED.severidad,
                   turnos_involucrados = EXCLUDED.turnos_involucrados,
                   fecha_referencia = EXCLUDED.fecha_referencia
           RETURNING (xmax = 0) AS es_nueva""",
        (
            huella, regla['id'], cedula, violacion.get('puesto_id'),
            violacion.get('fecha_referencia'), regla['severidad'],
            violacion['detalle'], violacion.get('turnos') or None,
        ),
    )
    return huella, cur.fetchone()[0]


def _acotar(filtros, params, cedula, desde, hasta):
    """Aplica el mismo alcance que uso el escaneo.

    Es critico que los barridos de estado se acoten IGUAL que `borrar_abiertas`:
    si no, correr el motor para un solo guarda o un solo mes tocaria anomalias
    de periodos que ni siquiera se evaluaron.
    """
    if cedula:
        filtros.append("guarda_cedula = %s")
        params.append(cedula)
    if desde:
        filtros.append("fecha_referencia >= %s")
        params.append(desde)
    if hasta:
        filtros.append("fecha_referencia <= %s")
        params.append(hasta)


def reabrir_reincidentes(cur, regla_ids, huellas_vigentes, cedula=None, desde=None, hasta=None):
    """Una anomalia dada por resuelta que vuelve a aparecer, se reabre.

    Pasa si alguien corrige la malla en SERPI, el motor confirma RESUELTA, y
    despues se vuelve a romper el mismo turno dentro del mismo mes. Como la
    huella es la misma, el upsert refrescaria el texto pero dejaria el estado
    en RESUELTA: una violacion activa quedaria escondida bajo un estado que
    dice que ya se arreglo. En una herramienta de cumplimiento eso es lo peor
    que puede pasar, asi que se reabre como ABIERTA y queda en el historial.
    """
    if not regla_ids or not huellas_vigentes:
        return []
    filtros = ["estado = 'RESUELTA'", "regla_id = ANY(%s)", "huella = ANY(%s::text[])"]
    params = [regla_ids, list(huellas_vigentes)]
    _acotar(filtros, params, cedula, desde, hasta)

    cur.execute(f"SELECT id FROM anomalias WHERE {' AND '.join(filtros)}", params)
    ids = [fila[0] for fila in cur.fetchall()]
    if not ids:
        return []

    cur.execute(
        """UPDATE anomalias SET estado = 'ABIERTA',
               actualizado_en = now(), actualizado_por = 'motor_reglas'
           WHERE id = ANY(%s)""",
        (ids,),
    )
    psycopg2.extras.execute_batch(
        cur,
        """INSERT INTO anomalias_historial
               (anomalia_id, estado_anterior, estado_nuevo, nota, usuario)
           VALUES (%s, 'RESUELTA', 'ABIERTA',
                   'Reincidencia: la violacion volvio a aparecer en el cargue.',
                   'motor_reglas')""",
        [(i,) for i in ids],
    )
    return ids


def marcar_resueltas(cur, regla_ids, huellas_vigentes, cedula=None, desde=None, hasta=None):
    """Cierra el ciclo: lo que se gestiono y ya no aparece, quedo corregido.

    Cuando alguien corrige la malla en SERPI y se recarga el Excel, la
    violacion simplemente deja de existir y este escaneo no la regenera. Sin
    esto, esa anomalia se quedaria para siempre en EN_REVISION, obligando a
    la persona a volver al dashboard a cerrarla a mano — y nadie lo haria.

    El barrido se acota EXACTAMENTE igual que `borrar_abiertas` (mismas
    reglas, mismo guarda, mismo rango de fechas). Si no, correr el motor
    para un solo guarda o un solo mes marcaria como resuelto todo lo demas,
    que ni siquiera se evaluo.

    Las ABIERTA no entran: esas se borran y se regeneran, no hay nada que
    confirmar. Solo aplica a lo que una persona ya toco.
    """
    if not regla_ids:
        return []

    # Se captura el estado ANTERIOR antes de actualizar: un UPDATE ...
    # RETURNING devolveria ya el valor nuevo, y el historial quedaria sin
    # saber de donde venia cada anomalia.
    filtros = [
        "estado IN ('EN_REVISION', 'JUSTIFICADA')",
        "regla_id = ANY(%s)",
        "huella <> ALL(%s::text[])",
    ]
    params = [regla_ids, list(huellas_vigentes)]
    _acotar(filtros, params, cedula, desde, hasta)

    cur.execute(f"SELECT id, estado FROM anomalias WHERE {' AND '.join(filtros)}", params)
    previas = cur.fetchall()
    if not previas:
        return []

    ids = [fila[0] for fila in previas]
    cur.execute(
        """UPDATE anomalias SET estado = 'RESUELTA',
               actualizado_en = now(), actualizado_por = 'motor_reglas'
           WHERE id = ANY(%s)""",
        (ids,),
    )

    # El historial se escribe en la misma transaccion: ninguna anomalia cambia
    # de estado sin dejar registro, ni siquiera cuando la cambia el motor.
    psycopg2.extras.execute_batch(
        cur,
        """INSERT INTO anomalias_historial
               (anomalia_id, estado_anterior, estado_nuevo, nota, usuario)
           VALUES (%s, %s, 'RESUELTA',
                   'La violacion ya no aparece en el cargue mas reciente.',
                   'motor_reglas')""",
        previas,
    )
    return previas


# La lista de anomalias nuevas/corregidas que se guarda en el historial se corta
# aqui (los contadores son exactos). Igual que en el ETL.
LIMITE_DETALLE = 300


def foto_vigentes(cur, regla_ids, cedula=None, desde=None, hasta=None):
    """Anomalias vigentes (todo menos RESUELTA) en el alcance de esta corrida,
    por huella. Es el "antes" del historial."""
    if not regla_ids:
        return {}
    filtros = ["a.estado <> 'RESUELTA'", "a.regla_id = ANY(%s)"]
    params = [regla_ids]
    _acotar(filtros, params, cedula, desde, hasta)
    filtros = [f if f.startswith('a.') else 'a.' + f for f in filtros]
    cur.execute(f"""SELECT a.huella, r.codigo, a.guarda_cedula, a.fecha_referencia,
                           a.severidad, a.detalle
                    FROM anomalias a JOIN reglas_anomalia r ON r.id = a.regla_id
                    WHERE {' AND '.join(filtros)}""", params)
    return {h: {'regla': c, 'cedula': ced, 'fecha': str(f), 'severidad': s, 'detalle': d}
            for h, c, ced, f, s, d in cur.fetchall()}


def resumen_historial(cur, antes, despues):
    """Que cambio en las anomalias frente a la corrida anterior.

    - corregidas: estaban y ya no aparecen -> la malla se arreglo en SERPI
    - nuevas: aparecen y no estaban -> la malla se rompio (o se cargo un mes nuevo)
    """
    corregidas = [antes[h] for h in antes.keys() - despues.keys()]
    nuevas = [despues[h] for h in despues.keys() - antes.keys()]
    cedulas = sorted({x['cedula'] for x in corregidas + nuevas if x['cedula']})
    nombres = {}
    if cedulas:
        cur.execute("SELECT cedula, nombre FROM guardas WHERE cedula = ANY(%s)", (cedulas,))
        nombres = dict(cur.fetchall())

    def lista(xs):
        orden = {'CRITICA': 0, 'ALTA': 1, 'BAJA': 2}
        xs = sorted(xs, key=lambda x: (orden.get(x['severidad'], 9), x['fecha'], x['regla']))
        return [{**x, 'guarda': nombres.get(x['cedula'])} for x in xs[:LIMITE_DETALLE]]

    por_regla = defaultdict(lambda: {'nuevas': 0, 'corregidas': 0})
    for x in nuevas:
        por_regla[x['regla']]['nuevas'] += 1
    for x in corregidas:
        por_regla[x['regla']]['corregidas'] += 1
    return {
        'vigentes_antes': len(antes), 'vigentes': len(despues),
        'nuevas': len(nuevas), 'corregidas': len(corregidas),
        'por_regla': dict(sorted(por_regla.items())),
        'lista_nuevas': lista(nuevas), 'lista_corregidas': lista(corregidas),
    }


def _fecha(texto):
    return datetime.strptime(texto, '%Y-%m-%d').date()


def en_rango(fecha, desde, hasta):
    if fecha is None:
        return False
    if desde and fecha < desde:
        return False
    if hasta and fecha > hasta:
        return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--cedula', help='Evaluar solo este guarda (por defecto, todos)')
    ap.add_argument('--desde', type=_fecha, metavar='YYYY-MM-DD',
                    help='Solo registrar hallazgos con fecha_referencia >= esta fecha')
    ap.add_argument('--hasta', type=_fecha, metavar='YYYY-MM-DD',
                    help='Solo registrar hallazgos con fecha_referencia <= esta fecha')
    ap.add_argument('--dry-run', action='store_true',
                    help='No escribe en anomalias, solo imprime lo que encontraria')
    args = ap.parse_args()

    conn = get_connection()
    cur = conn.cursor()

    reglas = cargar_reglas_vigentes(cur)
    todos_los_regla_ids = [r['id'] for versiones in reglas.values() for r in versiones]

    cedulas = cargar_guardas(cur, args.cedula)
    rango = ''
    if args.desde or args.hasta:
        rango = f" (rango {args.desde or '...'} a {args.hasta or '...'})"
    print(f"Evaluando {len(cedulas)} guarda(s){rango} ...")

    # Foto de lo vigente ANTES de recalcular, para el historial: lo que estaba y
    # ya no aparece se corrigio en SERPI; lo que aparece y no estaba es nuevo.
    # Hay que tomarla antes de borrar_abiertas, que regenera las ABIERTA.
    antes = foto_vigentes(cur, todos_los_regla_ids, args.cedula, args.desde, args.hasta)

    if not args.dry_run:
        borrar_abiertas(cur, todos_los_regla_ids, args.cedula, args.desde, args.hasta)

    conteo = defaultdict(int)
    guardas_afectados = set()
    huellas_vigentes = set()
    info_vigentes = {}
    nuevas = preservadas = 0

    def registrar(codigo, cedula, violacion):
        """Aplica vigencia + rango y persiste. Devuelve True si conto."""
        nonlocal nuevas, preservadas
        fecha_ref = violacion.get('fecha_referencia')
        if not en_rango(fecha_ref, args.desde, args.hasta):
            return False
        regla = regla_vigente_en(reglas, codigo, fecha_ref)
        if regla is None:
            return False
        conteo[codigo] += 1
        guardas_afectados.add(cedula)
        if args.dry_run:
            print(f"  [{codigo}] {cedula} {fecha_ref}: {violacion['detalle']}")
        else:
            huella, es_nueva = insertar_anomalia(cur, regla, codigo, cedula, violacion)
            huellas_vigentes.add(huella)
            info_vigentes[huella] = {'regla': codigo, 'cedula': cedula,
                                     'fecha': str(fecha_ref), 'severidad': regla['severidad'],
                                     'detalle': violacion['detalle']}
            if es_nueva:
                nuevas += 1
            else:
                preservadas += 1
        return True

    for cedula in cedulas:
        turnos = cargar_turnos_guarda(cur, cedula)
        for codigo, v in evaluar_guarda(turnos, reglas):
            registrar(codigo, cedula, v)

    sql = """SELECT guarda_cedula, puesto_id, anio, mes,
                    horas_diurnas_ordinarias, horas_diurnas_festivas,
                    horas_nocturnas_ordinarias, horas_nocturnas_festivas, total_horas
             FROM horas_declaradas_mes"""
    params = ()
    if args.cedula:
        sql += " WHERE guarda_cedula = %s"
        params = (args.cedula,)
    cur.execute(sql, params)
    for cedula, puesto_id, anio, mes, hdo, hdf, hno, hnf, total in cur.fetchall():
        fila = {
            'horas_diurnas_ordinarias': hdo, 'horas_diurnas_festivas': hdf,
            'horas_nocturnas_ordinarias': hno, 'horas_nocturnas_festivas': hnf,
            'total_horas': total,
        }
        d = detectar_descuadre_horas(fila)
        if d:
            # El descuadre es por guarda-puesto-mes: el puesto entra en la
            # clave porque un mismo guarda puede descuadrar en varios puestos
            # el mismo mes (y de hecho pasa: hasta 3 en la malla de julio).
            registrar('DESCUADRE_HORAS_DECLARADAS', cedula, {
                'clave': f"{anio:04d}-{mes:02d}|{puesto_id}",
                'puesto_id': puesto_id,
                'fecha_referencia': date(anio, mes, 1),
                'detalle': d['detalle'],
            })

    resueltas, reabiertas = [], []
    if not args.dry_run:
        # Ambos barridos van DESPUES del escaneo completo: hasta no terminar no
        # se sabe que huellas siguen vigentes y cuales desaparecieron.
        resueltas = marcar_resueltas(
            cur, todos_los_regla_ids, huellas_vigentes,
            args.cedula, args.desde, args.hasta,
        )
        reabiertas = reabrir_reincidentes(
            cur, todos_los_regla_ids, huellas_vigentes,
            args.cedula, args.desde, args.hasta,
        )
        resumen = resumen_historial(cur, antes, info_vigentes)
        conn.commit()
    conn.close()

    total = sum(conteo.values())
    print("Hallazgos por regla:")
    for codigo, n in sorted(conteo.items()):
        print(f"  {codigo}: {n}")
    print(f"TOTAL: {total} hallazgos, {len(guardas_afectados)} de {len(cedulas)} guardas afectados")
    if not args.dry_run:
        # `preservadas` son las que alguien ya reviso o justifico: el motor las
        # reconoce por su huella y respeta su estado. Las ABIERTA sin gestionar
        # se borran y se regeneran, por eso cuentan como nuevas aunque ya
        # estuvieran en la tabla.
        print(f"  regeneradas: {nuevas} | ya gestionadas, estado respetado: {preservadas}")
        if resueltas:
            print(f"  RESUELTAS: {len(resueltas)} anomalias gestionadas ya no aparecen "
                  f"en el cargue -> la correccion en SERPI si llego")
        if reabiertas:
            print(f"  REABIERTAS: {len(reabiertas)} anomalias dadas por resueltas "
                  f"volvieron a aparecer -> revisar reincidencia")
        print(f"  frente a la corrida anterior: {resumen['nuevas']} nuevas, "
              f"{resumen['corregidas']} corregidas, {resumen['vigentes']} vigentes")
        # Para pipeline_diario.py (historial de corridas). Una sola linea.
        print("RESUMEN_JSON " + json.dumps({'etapa': 'motor', **resumen},
                                           ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
