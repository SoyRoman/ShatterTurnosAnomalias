"""
ETL de normalizacion de la malla de turnos (export SERPI) hacia PostgreSQL.

Uso:
    python etl_normalizacion.py --archivo RepProgramacion.xlsx

Requiere un archivo .env (ver .env.example) con los datos de conexion a
PostgreSQL. Es idempotente: se puede correr varias veces con el mismo
archivo sin duplicar turnos (usa upsert sobre una llave natural).
"""

import argparse
import os
import re
import sys
from datetime import datetime

import openpyxl
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

DAY_RE = re.compile(r'^(\d{1,2})\n')
TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')
NAME_RE = re.compile(r'^(.*?)\s*-\s*(\d{5,12})\s*(?:-\s*(.*))?$')
HOURS_RE = re.compile(r'(Horas [A-Za-zÁÉÍÓÚñ ]+|Total Horas):\s*(\d+)')

MESES = {
    'Enero': 1, 'Febrero': 2, 'Marzo': 3, 'Abril': 4, 'Mayo': 5, 'Junio': 6,
    'Julio': 7, 'Agosto': 8, 'Septiembre': 9, 'Octubre': 10, 'Noviembre': 11,
    'Diciembre': 12,
}

LEAVE_CODES = {'VAC', 'INC', 'LIC', 'LICNR'}


# --------------------------------------------------------------------------
# Parsing del Excel (misma logica validada sobre la malla de julio 2026)
# --------------------------------------------------------------------------

def build_day_map(ws, header_row, max_col):
    dmap = {}
    for c in range(1, max_col + 1):
        v = ws.cell(row=header_row, column=c).value
        if isinstance(v, str):
            m = DAY_RE.match(v)
            if m:
                dmap[int(m.group(1))] = c
    return dmap


def parse_shift_cell(v):
    """Devuelve (codigo, inicio, fin) o None si la celda esta vacia."""
    if not isinstance(v, str) or not v.strip():
        return None
    parts = [p.strip() for p in v.split('\n') if p.strip() != '']
    if len(parts) == 1:
        return (parts[0], None, None)
    elif len(parts) == 2:
        if TIME_RE.match(parts[0]) and TIME_RE.match(parts[1]):
            return (None, parts[0], parts[1])  # celda sin letra de codigo
        return (parts[0], parts[1], None)
    else:
        return (parts[0], parts[1], parts[2])


def duration_hours(start, end):
    if not start or not end:
        return None
    sh, sm = map(int, start.split(':'))
    eh, em = map(int, end.split(':'))
    s, e = sh * 60 + sm, eh * 60 + em
    if e <= s:
        e += 24 * 60
    return round((e - s) / 60.0, 2)


def parse_name(name_raw):
    m = NAME_RE.match(name_raw)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return name_raw.strip(), None


def find_year_month(ws, max_col):
    for r in range(1, 15):
        row_vals = [ws.cell(row=r, column=c).value for c in range(1, max_col + 1)]
        for c, v in enumerate(row_vals):
            if isinstance(v, str) and v.strip() == 'Año:':
                anio = row_vals[c + 5] if c + 5 < len(row_vals) else None
                # el mes esta unas celdas mas a la derecha en la misma fila
                for cc in range(c, len(row_vals)):
                    vv = row_vals[cc]
                    if isinstance(vv, str) and vv.strip() == 'Mes:':
                        for ccc in range(cc, len(row_vals)):
                            mes_val = row_vals[ccc]
                            if isinstance(mes_val, str) and mes_val.strip() in MESES:
                                # Un mes sin malla cargada en SERPI trae el
                                # encabezado con otra disposicion y aqui cae
                                # el rotulo «Mes:» en vez del año (visto con
                                # noviembre, el 2026-10-01). Sin año valido la
                                # hoja no se puede fechar: se descarta.
                                try:
                                    return int(anio), MESES[mes_val.strip()]
                                except (TypeError, ValueError):
                                    return None, None
    return None, None


# Firmas de archivo, en hex para no depender de escapes: OOXML (el .xlsx
# moderno) es un ZIP y empieza con "PK"; OLE2 (el .xls 97-2003 que entrega
# SERPI) tiene su propia firma.
FIRMA_ZIP = bytes.fromhex("504b0304")
FIRMA_OLE2 = bytes.fromhex("d0cf11e0a1b11ae1")

AYUDA_SIN_ARCHIVO = """La malla NO viene en el repositorio a proposito: tiene nombres y cedulas de
trabajadores reales y esta excluida por .gitignore. En una maquina nueva hay
que traerla. Dos formas:

  1. Descargarla de SERPI. Necesita SERPI_WEB_USER y SERPI_WEB_PASSWORD en un
     .env propio de esa maquina (.env tampoco se versiona) y, una sola vez,
     `python -m playwright install chromium`:

         python descargar_malla_serpi.py --desde 2026-09-01 --hasta 2026-09-30

     O el pipeline completo, que ademas corre este ETL y el motor:

         python pipeline_diario.py

  2. Copiarla desde otra maquina que ya la tenga, por medio interno (USB, red
     local). NO la subas a ningun servicio externo ni la mandes por correo:
     son datos personales de 404 trabajadores (Ley 1581 de 2012)."""


def verificar_archivo(path):
    """Falla con un mensaje util ANTES de que openpyxl entierre la causa.

    Los dos tropiezos de siempre producen trazas que apuntan al sitio
    equivocado:

    - **El archivo no esta.** openpyxl propaga un FileNotFoundError desde
      dentro de `zipfile`, con seis marcos de pila de por medio, y parece un
      problema de Excel corrupto. Le pasa a cualquiera que clone el repo en una
      maquina nueva, porque la malla no se versiona (ver CLAUDE.md §7).
    - **El archivo es el .xls crudo de SERPI.** openpyxl solo lee OOXML, asi
      que da "File is not a zip file", que en ningun momento insinua que el
      arreglo sea convertir el formato.
    """
    if not os.path.exists(path):
        sys.exit(f"No existe el archivo: {path}\n\n{AYUDA_SIN_ARCHIVO}")

    with open(path, "rb") as f:
        firma = f.read(8)

    if firma.startswith(FIRMA_OLE2):
        sys.exit(
            f"{path} es un .xls binario antiguo (Excel 97-2003), no un .xlsx.\n"
            "Es el formato que entrega SERPI, y openpyxl solo lee .xlsx.\n\n"
            "`descargar_malla_serpi.py` ya hace esa conversion automaticamente, "
            "asi que lo mas simple es volver a descargarla con ese script en vez "
            "de convertirla a mano.")

    if not firma.startswith(FIRMA_ZIP):
        sys.exit(
            f"{path} no parece un Excel: empieza con {firma!r}.\n"
            "Si lo descargaste a mano, revisa que SERPI no haya devuelto una "
            "pagina de error o de login en vez del reporte.")


def parse_workbook(path):
    """Devuelve una lista de dicts, uno por asignacion guarda-puesto, con sus
    turnos dia a dia y las horas declaradas en el encabezado."""
    wb = openpyxl.load_workbook(path, data_only=True)
    records = []

    for sname in wb.sheetnames:
        ws = wb[sname]
        max_row, max_col = ws.max_row, ws.max_column

        anio, mes = find_year_month(ws, max_col)

        cliente = proyecto = puesto = None
        r = 1
        day_map = {}
        while r <= max_row:
            row_vals = [ws.cell(row=r, column=c).value for c in range(1, max_col + 1)]
            marker_text = None
            for v in row_vals:
                if isinstance(v, str) and v.strip().startswith(('CLIENTE:', 'PROYECTO:', 'PUESTO:')):
                    marker_text = v.strip()
                    break
            if marker_text is not None:
                if marker_text.startswith('CLIENTE:'):
                    cliente = marker_text[len('CLIENTE:'):].strip()
                elif marker_text.startswith('PROYECTO:'):
                    proyecto = marker_text[len('PROYECTO:'):].strip()
                elif marker_text.startswith('PUESTO:'):
                    puesto = marker_text[len('PUESTO:'):].strip()
                    header_row = None
                    for rr in range(r + 1, min(r + 4, max_row + 1)):
                        if ws.cell(row=rr, column=3).value == 'T':
                            header_row = rr
                            break
                    if header_row:
                        day_map = build_day_map(ws, header_row, max_col)
                        r = header_row + 1
                        continue
                r += 1
                continue

            c3 = ws.cell(row=r, column=3).value
            c4 = ws.cell(row=r, column=4).value
            if isinstance(c3, int) and isinstance(c4, str) and c4.strip() and day_map:
                name, cedula = parse_name(c4.strip())
                hours_decl = {}
                for c in range(1, max_col + 1):
                    v = ws.cell(row=r, column=c).value
                    if isinstance(v, str):
                        m = HOURS_RE.match(v.strip())
                        if m:
                            hours_decl[m.group(1)] = int(m.group(2))

                shift_row = r + 1
                turnos = []
                if shift_row <= max_row and cedula:
                    for day, col in day_map.items():
                        parsed = parse_shift_cell(ws.cell(row=shift_row, column=col).value)
                        if not parsed:
                            continue
                        code, start, end = parsed
                        dur = duration_hours(start, end)
                        if code is None:
                            # celda sin letra: codigo sintetico a partir del horario
                            code = f"T_{start.replace(':','')}_{end.replace(':','')}"
                        categoria = 'AUSENCIA' if code in LEAVE_CODES else 'TRABAJADO'
                        turnos.append({
                            'dia': day, 'codigo': code, 'inicio': start, 'fin': end,
                            'horas': dur, 'categoria': categoria,
                        })

                if cedula:
                    records.append({
                        'sheet': sname, 'cliente': cliente, 'puesto': puesto,
                        'guarda_cedula': cedula, 'guarda_nombre': name, 'slot': c3,
                        'anio': anio, 'mes': mes,
                        'hours_decl': hours_decl, 'turnos': turnos,
                    })
                r = shift_row + 1
                continue
            r += 1
    return records


# --------------------------------------------------------------------------
# Insercion en PostgreSQL
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


def reconciliar(cur, claves_turnos, claves_horas, puestos, fecha_min, fecha_max,
                periodos, umbral_pct):
    """Borra lo que ya NO viene en el archivo, dentro del alcance que el archivo
    realmente cubre.

    Sin esto el ETL era solo upsert y nunca borraba, con lo cual el ciclo central
    del sistema quedaba a medias: si el programador CORRIGE un turno en SERPI el
    upsert lo actualiza y la anomalia desaparece, pero si lo BORRA -- que es
    justo como se arregla un doble turno, quitando la asignacion duplicada -- la
    fila se quedaba en la BD para siempre y la violacion no se limpiaba nunca.
    Lo mismo al MOVER un turno de puesto: quedaba la fila vieja mas la nueva, y
    eso puede inventar un cruce que ya no existe.

    Alcance del borrado (deliberadamente acotado): solo el rango de fechas que
    trae el archivo Y solo los puestos que aparecen en el. Asi, un export parcial
    (p.ej. filtrado a un cliente) no puede borrar lo que no venia a reemplazar.
    Si un puesto entero desaparece de la malla sus turnos sobreviven: preferimos
    conservar de mas a borrar por una exportacion incompleta.

    `umbral_pct` es la red de seguridad para la corrida diaria desatendida: si el
    archivo llegara vacio o truncado por un fallo de SERPI, borrar seria
    catastrofico y silencioso. Por encima de ese porcentaje se aborta sin tocar
    nada."""
    if not claves_turnos:
        return {'turnos_borrados': 0, 'horas_borradas': 0}

    lista_puestos = sorted(puestos)

    cur.execute("""CREATE TEMP TABLE _archivo_turnos (
                       guarda_cedula TEXT, puesto_id INTEGER, fecha DATE, slot INTEGER
                   ) ON COMMIT DROP""")
    psycopg2.extras.execute_values(
        cur, "INSERT INTO _archivo_turnos (guarda_cedula, puesto_id, fecha, slot) VALUES %s",
        sorted(claves_turnos))
    cur.execute("CREATE INDEX ON _archivo_turnos (guarda_cedula, puesto_id, fecha, slot)")

    alcance = ("t.fecha BETWEEN %s AND %s AND t.puesto_id = ANY(%s)")
    params = (fecha_min, fecha_max, lista_puestos)

    cur.execute(f"SELECT count(*) FROM turnos t WHERE {alcance}", params)
    en_alcance = cur.fetchone()[0]

    sobrantes = f"""FROM turnos t
                    WHERE {alcance}
                      AND NOT EXISTS (SELECT 1 FROM _archivo_turnos a
                                       WHERE a.guarda_cedula = t.guarda_cedula
                                         AND a.puesto_id = t.puesto_id
                                         AND a.fecha = t.fecha
                                         AND a.slot = t.slot)"""
    cur.execute(f"SELECT count(*) {sobrantes}", params)
    a_borrar = cur.fetchone()[0]

    if en_alcance and 100.0 * a_borrar / en_alcance > umbral_pct:
        raise SystemExit(
            f"ABORTADO: la reconciliacion borraria {a_borrar} de {en_alcance} turnos "
            f"({100.0 * a_borrar / en_alcance:.1f}%), por encima del umbral de "
            f"{umbral_pct}%.\n"
            "  Eso normalmente significa que el archivo llego vacio o truncado, no que "
            "la malla cambiara tanto.\n"
            "  Revisa el archivo. Si el cambio es real, repite con "
            "--umbral-borrado <pct> o --sin-reconciliar.")

    cur.execute(f"DELETE {sobrantes}", params)
    turnos_borrados = cur.rowcount

    # Mismo criterio para las horas declaradas: si la asignacion guarda-puesto
    # desaparecio del archivo, su fila de horas tambien sobra.
    horas_borradas = 0
    if claves_horas and periodos:
        cur.execute("""CREATE TEMP TABLE _archivo_horas (
                           guarda_cedula TEXT, puesto_id INTEGER, slot INTEGER,
                           anio INTEGER, mes INTEGER
                       ) ON COMMIT DROP""")
        psycopg2.extras.execute_values(
            cur, """INSERT INTO _archivo_horas
                        (guarda_cedula, puesto_id, slot, anio, mes) VALUES %s""",
            sorted(claves_horas))
        cur.execute("""DELETE FROM horas_declaradas_mes h
                        WHERE (h.anio, h.mes) IN %s
                          AND h.puesto_id = ANY(%s)
                          AND NOT EXISTS (SELECT 1 FROM _archivo_horas a
                                           WHERE a.guarda_cedula = h.guarda_cedula
                                             AND a.puesto_id = h.puesto_id
                                             AND a.slot = h.slot
                                             AND a.anio = h.anio
                                             AND a.mes = h.mes)""",
                    (tuple(sorted(periodos)), lista_puestos))
        horas_borradas = cur.rowcount

    return {'turnos_borrados': turnos_borrados, 'horas_borradas': horas_borradas}


def load(records, conn, reconciliacion=True, umbral_pct=20.0):
    cur = conn.cursor()

    cliente_id = {}
    puesto_id = {}
    guardas_vistos = set()
    tipos_vistos = {}

    n_turnos = 0
    n_horas_decl = 0

    # Lo que el archivo trae, para poder reconciliar despues (ver reconciliar()).
    claves_turnos = set()
    claves_horas = set()
    puestos_archivo = set()
    periodos_archivo = set()
    fecha_min = fecha_max = None

    for rec in records:
        cliente = rec['cliente'] or '(sin cliente)'
        if cliente not in cliente_id:
            cur.execute(
                """INSERT INTO clientes (nombre) VALUES (%s)
                   ON CONFLICT (nombre) DO UPDATE SET nombre = EXCLUDED.nombre
                   RETURNING id""",
                (cliente,),
            )
            cliente_id[cliente] = cur.fetchone()[0]

        puesto = rec['puesto'] or '(sin puesto)'
        pkey = (cliente, puesto)
        if pkey not in puesto_id:
            cur.execute(
                """INSERT INTO puestos (cliente_id, nombre, hoja_origen) VALUES (%s, %s, %s)
                   ON CONFLICT (cliente_id, nombre)
                   DO UPDATE SET hoja_origen = EXCLUDED.hoja_origen
                   RETURNING id""",
                (cliente_id[cliente], puesto, rec['sheet']),
            )
            puesto_id[pkey] = cur.fetchone()[0]

        cedula = rec['guarda_cedula']
        if cedula not in guardas_vistos:
            cur.execute(
                """INSERT INTO guardas (cedula, nombre) VALUES (%s, %s)
                   ON CONFLICT (cedula) DO UPDATE SET nombre = EXCLUDED.nombre""",
                (cedula, rec['guarda_nombre']),
            )
            guardas_vistos.add(cedula)

        this_puesto_id = puesto_id[pkey]

        for t in rec['turnos']:
            tkey = t['codigo']
            if tkey not in tipos_vistos:
                cur.execute(
                    """INSERT INTO tipos_turno (codigo, hora_inicio, hora_fin, duracion_horas, categoria)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (codigo) DO NOTHING""",
                    (t['codigo'], t['inicio'], t['fin'], t['horas'], t['categoria']),
                )
                tipos_vistos[tkey] = True

            if not (rec['anio'] and rec['mes']):
                continue
            fecha = f"{rec['anio']:04d}-{rec['mes']:02d}-{t['dia']:02d}"

            cur.execute(
                """INSERT INTO turnos
                       (guarda_cedula, puesto_id, fecha, slot, tipo_turno_codigo,
                        hora_inicio, hora_fin, horas_calculadas, origen)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'EXCEL_EXPORT')
                   ON CONFLICT (guarda_cedula, puesto_id, fecha, slot)
                   DO UPDATE SET tipo_turno_codigo = EXCLUDED.tipo_turno_codigo,
                                 hora_inicio = EXCLUDED.hora_inicio,
                                 hora_fin = EXCLUDED.hora_fin,
                                 horas_calculadas = EXCLUDED.horas_calculadas,
                                 fecha_carga = now()""",
                (cedula, this_puesto_id, fecha, rec['slot'], t['codigo'],
                 t['inicio'], t['fin'], t['horas']),
            )
            n_turnos += 1

            claves_turnos.add((cedula, this_puesto_id, fecha, rec['slot']))
            puestos_archivo.add(this_puesto_id)
            periodos_archivo.add((rec['anio'], rec['mes']))
            if fecha_min is None or fecha < fecha_min:
                fecha_min = fecha
            if fecha_max is None or fecha > fecha_max:
                fecha_max = fecha

        hd = rec['hours_decl']
        if hd and rec['anio'] and rec['mes']:
            cur.execute(
                """INSERT INTO horas_declaradas_mes
                       (guarda_cedula, puesto_id, slot, anio, mes,
                        horas_diurnas_ordinarias, horas_diurnas_festivas,
                        horas_nocturnas_ordinarias, horas_nocturnas_festivas, total_horas)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (guarda_cedula, puesto_id, slot, anio, mes)
                   DO UPDATE SET
                       horas_diurnas_ordinarias = EXCLUDED.horas_diurnas_ordinarias,
                       horas_diurnas_festivas = EXCLUDED.horas_diurnas_festivas,
                       horas_nocturnas_ordinarias = EXCLUDED.horas_nocturnas_ordinarias,
                       horas_nocturnas_festivas = EXCLUDED.horas_nocturnas_festivas,
                       total_horas = EXCLUDED.total_horas""",
                (cedula, this_puesto_id, rec['slot'], rec['anio'], rec['mes'],
                 hd.get('Horas Diurnas Ordinarias'), hd.get('Horas Diurnas Festivas'),
                 hd.get('Horas Nocturnas Ordinarias'), hd.get('Horas Nocturnas Festivas'),
                 hd.get('Total Horas')),
            )
            n_horas_decl += 1
            claves_horas.add((cedula, this_puesto_id, rec['slot'], rec['anio'], rec['mes']))

    stats = {
        'clientes': len(cliente_id), 'puestos': len(puesto_id),
        'guardas': len(guardas_vistos), 'tipos_turno': len(tipos_vistos),
        'turnos': n_turnos, 'horas_declaradas': n_horas_decl,
    }

    if reconciliacion:
        stats.update(reconciliar(cur, claves_turnos, claves_horas, puestos_archivo,
                                 fecha_min, fecha_max, periodos_archivo, umbral_pct))
    else:
        stats.update({'turnos_borrados': 0, 'horas_borradas': 0})

    conn.commit()
    cur.close()
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--archivo', required=True, help='Ruta al Excel exportado de SERPI')
    ap.add_argument('--sin-reconciliar', action='store_true',
                    help='No borrar lo que ya no viene en el archivo (solo upsert, '
                         'comportamiento anterior)')
    ap.add_argument('--umbral-borrado', type=float, default=20.0, metavar='PCT',
                    help='Aborta si la reconciliacion borraria mas de este %% de los '
                         'turnos en alcance (default 20). Red de seguridad para la '
                         'corrida diaria: un archivo truncado no debe vaciar la base.')
    args = ap.parse_args()

    verificar_archivo(args.archivo)

    print(f"Leyendo {args.archivo} ...")
    records = parse_workbook(args.archivo)
    print(f"  {len(records)} asignaciones guarda-puesto encontradas")

    # Un mes que SERPI todavia no tiene programado llega como un reporte vacio.
    # No es un error (la corrida diaria siempre pide el mes siguiente, que se
    # carga entre el 25 y el 27), y sobre todo NO se reconcilia: reconciliar
    # contra un archivo vacio es pedir que se borre todo lo que haya.
    if not any(r['turnos'] for r in records):
        print("  El archivo no trae turnos (mes aun sin programar en SERPI). "
              "No se toca la base.")
        return 0

    sin_fecha = sum(1 for r in records if not (r['anio'] and r['mes']))
    if sin_fecha:
        print(f"  ADVERTENCIA: {sin_fecha} hojas sin Año/Mes detectado, sus turnos no se insertaron")

    print("Conectando a PostgreSQL ...")
    conn = get_connection()

    print("Insertando (upsert, se puede correr varias veces sin duplicar) ...")
    if not args.sin_reconciliar:
        print("  con reconciliacion: lo que ya no venga en el archivo se borra")
    stats = load(records, conn, reconciliacion=not args.sin_reconciliar,
                 umbral_pct=args.umbral_borrado)
    conn.close()

    print("Listo:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


if __name__ == '__main__':
    sys.exit(main())
