"""
Servicios ocasionales: lo que hay que facturar en cada corte.

Un servicio ocasional es uno no permanente (un fin de semana, un apoyo de un
mes). Se identifican por CONVENCION DE NOMBRE: el puesto contiene la palabra
"Ocasional" (p.ej. proyecto Bomba Food, puesto "Ocasional Martin Virreria
Salomia"). Es el nombre del PUESTO, no del proyecto. Se descarto pedirle a SERPI
un estado "ocasional" del guarda: ese estado cambia cuando se le asigna un
puesto y se perderia la trazabilidad.

Corte de facturacion: del 21 del mes anterior al 20 del mes del corte, ambos
inclusive (los clientes reciben facturas hasta el 25). Cada dia cae en un solo
corte.

Este modulo no toca la base: recibe turnos y devuelve horas separadas por tipo.
Las funciones puras se prueban en test_ocasionales.py.
"""
import datetime as dt
import re
import unicodedata

PATRON_OCASIONAL = "ocasional"

# Inicio y fin de la jornada nocturna. Desde el 25-dic-2025 el trabajo nocturno
# va de 19:00 a 06:00 (Ley 2466 de 2025, art. 10, que modifica el Art. 160 CST).
# Es parametro, no constante: si el Excel de tarifas usa otro corte, se cambia
# aqui sin tocar el calculo.
INICIO_NOCTURNO = dt.time(19, 0)
FIN_NOCTURNO = dt.time(6, 0)

TIPOS = ('diurna', 'nocturna', 'diurna_festiva', 'nocturna_festiva')


def es_ocasional(nombre_puesto):
    """True si el nombre del puesto contiene 'ocasional' (sin importar tildes
    ni mayusculas)."""
    s = unicodedata.normalize('NFKD', nombre_puesto or '').encode('ascii', 'ignore').decode()
    return PATRON_OCASIONAL in s.lower()


def rango_corte(corte):
    """'2026-10' -> (2026-09-21, 2026-10-20)."""
    anio, mes = map(int, corte.split('-'))
    fin = dt.date(anio, mes, 20)
    primero = dt.date(anio, mes, 1)
    anterior = (primero - dt.timedelta(days=1)).replace(day=21)
    return anterior, fin


def corte_de(fecha):
    """A que corte pertenece una fecha: hasta el 20 es el del mes; desde el 21,
    el del mes siguiente."""
    if fecha.day <= 20:
        return f"{fecha.year:04d}-{fecha.month:02d}"
    siguiente = (fecha.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    return f"{siguiente.year:04d}-{siguiente.month:02d}"


# ---------------------------------------------------------------------------
# Festivos de Colombia (Ley 51 de 1983, "Ley Emiliani")
# ---------------------------------------------------------------------------

def _pascua(anio):
    """Domingo de Pascua (algoritmo anonimo gregoriano)."""
    a, b, c = anio % 19, anio // 100, anio % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = (h + l - 7 * m + 114) % 31 + 1
    return dt.date(anio, mes, dia)


def _al_lunes(d):
    """Traslada al lunes siguiente (o deja la fecha si ya es lunes)."""
    return d + dt.timedelta(days=(7 - d.weekday()) % 7)


_FESTIVOS = {}


def festivos(anio):
    if anio in _FESTIVOS:
        return _FESTIVOS[anio]
    fijos = [(1, 1), (5, 1), (7, 20), (8, 7), (12, 8), (12, 25)]
    trasladables = [(1, 6), (3, 19), (6, 29), (8, 15), (10, 12), (11, 1), (11, 11)]
    p = _pascua(anio)
    s = {dt.date(anio, m, d) for m, d in fijos}
    s |= {_al_lunes(dt.date(anio, m, d)) for m, d in trasladables}
    s |= {p - dt.timedelta(days=3), p - dt.timedelta(days=2)}          # jueves y viernes santo
    s |= {_al_lunes(p + dt.timedelta(days=n)) for n in (39, 60, 68)}   # Ascension, Corpus, Sagrado Corazon
    _FESTIVOS[anio] = s
    return s


def es_festivo(fecha):
    """Domingo o festivo: ambos se liquidan con recargo dominical/festivo."""
    return fecha.weekday() == 6 or fecha in festivos(fecha.year)


# ---------------------------------------------------------------------------
# Separacion de horas
# ---------------------------------------------------------------------------

def _es_nocturno(t):
    return t >= INICIO_NOCTURNO or t < FIN_NOCTURNO


def separar_horas(fecha, inicio, fin):
    """Horas de un turno separadas en diurna / nocturna / diurna_festiva /
    nocturna_festiva.

    Se recorre en tramos de un minuto sobre timestamps absolutos, asi que un
    turno que cruza medianoche (F 18:00-06:00) reparte bien sus horas, y la
    parte que cae en domingo o festivo se marca como festiva aunque el turno
    haya empezado el dia anterior.
    """
    horas = dict.fromkeys(TIPOS, 0.0)
    if inicio is None or fin is None:
        return horas
    t0 = dt.datetime.combine(fecha, inicio)
    t1 = dt.datetime.combine(fecha, fin)
    if t1 <= t0:
        t1 += dt.timedelta(days=1)
    # Por tramos hasta el siguiente cambio de categoria (06:00, 19:00 o
    # medianoche), no minuto a minuto: mismo resultado, mucho menos trabajo.
    t = t0
    while t < t1:
        cortes = [dt.datetime.combine(t.date(), h) for h in (FIN_NOCTURNO, INICIO_NOCTURNO)]
        cortes.append(dt.datetime.combine(t.date() + dt.timedelta(days=1), dt.time(0, 0)))
        siguiente = min([c for c in cortes if c > t] + [t1])
        clave = ('nocturna' if _es_nocturno(t.time()) else 'diurna')
        if es_festivo(t.date()):
            clave += '_festiva'
        horas[clave] += (siguiente - t).total_seconds() / 3600
        t = siguiente
    return {k: round(v, 2) for k, v in horas.items()}
