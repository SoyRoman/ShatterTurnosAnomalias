"""
Genera un informe AUTONOMO en un solo archivo HTML.

    python generar_informe.py --periodo 2026-07

Toma el dashboard, le incrusta los datos del periodo y produce un archivo que
se abre con doble clic: sin servidor, sin base de datos y sin internet. Es lo
que se le envia a gerencia para que lo proyecte por su cuenta.

  OJO: el archivo generado lleva nombres y cedulas reales incrustados. Se
  distribuye internamente igual que el Excel de la malla — nunca se sube a un
  repositorio ni a un servicio publico. `.gitignore` ya excluye informe_*.html.

Lee de la base de datos directamente, asi que no necesita que la API este
levantada.
"""

import argparse
import json
import os
from datetime import date, datetime
from decimal import Decimal

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.abspath(__file__))


def serializar(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (bytes, memoryview)):
        return str(o)
    raise TypeError(f"No se puede serializar {type(o)}")


def consultar(cur, sql, params=()):
    cur.execute(sql, params)
    return [dict(f) for f in cur.fetchall()]


def recolectar(cur, periodo):
    """Las mismas consultas que sirve la API, para que el informe muestre
    exactamente lo que muestra el dashboard en vivo."""
    return {
        'periodo': periodo,
        'generado': datetime.now().isoformat(timespec='minutes'),
        'kpi': (consultar(cur, "SELECT * FROM vw_kpi_periodo WHERE periodo = %s", (periodo,)) or [{}])[0],
        'clientes': consultar(cur, """SELECT * FROM vw_resumen_cliente WHERE periodo = %s
                                      ORDER BY criticas DESC, total_hallazgos DESC LIMIT 12""", (periodo,)),
        'estructural': consultar(cur, """SELECT * FROM vw_carga_estructural WHERE periodo = %s
                                         ORDER BY horas_mes DESC NULLS LAST""", (periodo,)),
        'puntuales': consultar(cur, """SELECT id, periodo, fecha_referencia, regla, severidad,
                                              severidad_orden, guarda_cedula, guarda_nombre,
                                              cliente_principal, puesto_principal, detalle, estado,
                                              TRUE AS pendiente
                                       FROM vw_bandeja_programador WHERE periodo = %s
                                       ORDER BY severidad_orden, fecha_referencia""", (periodo,)),
        'nomina': consultar(cur, """SELECT * FROM vw_nomina_horas WHERE periodo = %s
                                    ORDER BY abs(descuadre_categorias) DESC NULLS LAST,
                                             guarda_nombre""", (periodo,)),
    }


def construir(datos):
    html = open(os.path.join(RAIZ, 'dashboard.html'), encoding='utf-8').read()
    carga = json.dumps(datos, default=serializar, ensure_ascii=False)

    # `pedir()` es el unico punto por el que el dashboard habla con la API.
    # Se sustituye por una version que resuelve contra los datos incrustados,
    # asi el resto del dashboard (filtros, agrupado, CSV) sigue igual sin
    # tocar una sola linea mas.
    puente = """
const DATOS = __CARGA__;
const ESTATICO = true;

async function pedir(ruta, opciones){
  if (opciones && opciones.method) throw new Error('Informe de solo lectura');
  const [camino, consulta] = ruta.split('?');
  const q = new URLSearchParams(consulta || '');
  if (camino === '/periodos')    return [{periodo: DATOS.periodo}];
  if (camino === '/kpi')         return DATOS.kpi;
  if (camino === '/clientes')    return DATOS.clientes;
  if (camino === '/estructural') return DATOS.estructural;
  if (camino === '/nomina'){
    return q.get('solo_descuadres') === 'true'
      ? DATOS.nomina.filter(f => Math.abs(Number(f.descuadre_categorias || 0)) > 0.01)
      : DATOS.nomina;
  }
  if (camino === '/anomalias'){
    let filas = DATOS.puntuales;
    if (q.get('severidad')) filas = filas.filter(a => a.severidad === q.get('severidad'));
    if (q.get('estado'))    filas = filas.filter(a => a.estado === q.get('estado'));
    const b = (q.get('busqueda') || '').toLowerCase();
    if (b) filas = filas.filter(a => (a.guarda_nombre || '').toLowerCase().includes(b) ||
                                     String(a.guarda_cedula).includes(b));
    return {total: filas.length, pagina: 1, por_pagina: filas.length, datos: filas};
  }
  throw new Error('Sin datos para ' + camino);
}
"""
    original = html[html.index("async function pedir(ruta, opciones){"):html.index("function pedirToken(){")]
    html = html.replace(original, puente.replace('__CARGA__', carga) + "\n")

    # En un informe estatico no hay nada que gestionar ni a quien identificarse.
    html = html.replace(
        '<button onclick="identificarse()" id="btnUsuario">Identificarse</button>', '')
    html = html.replace(
        '<td><button onclick="abrirGestion(${a.id})">Gestionar</button></td>', '<td></td>')
    html = html.replace('<th>Estado</th><th></th></tr>', '<th>Estado</th></tr>')

    aviso = f"""<div style="background:#fef3c7;color:#b45309;padding:11px 16px;
      font-size:13px;border-bottom:1px solid #f0d9a0">
      <b>Informe del periodo {datos['periodo']}</b> &mdash; foto tomada el
      {datos['generado'].replace('T', ' a las ')}. Documento de solo lectura con
      datos reales: distribuci&oacute;n interna &uacute;nicamente.
    </div>"""
    html = html.replace('<main>', aviso + '\n<main>')
    html = html.replace(
        '<title>Anomalías en mallas de turnos — Shatter</title>',
        f"<title>Anomalías en mallas de turnos {datos['periodo']} — Shatter</title>")
    return html


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--periodo', required=True, help='Periodo a exportar, formato YYYY-MM')
    ap.add_argument('--salida', help='Ruta del archivo (por defecto informe_<periodo>.html)')
    args = ap.parse_args()

    load_dotenv(os.path.join(RAIZ, '.env'))
    conn = psycopg2.connect(
        host=os.environ['DB_HOST'], port=os.environ.get('DB_PORT', '5432'),
        dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'], sslmode=os.environ.get('DB_SSLMODE', 'prefer'),
    )
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    datos = recolectar(cur, args.periodo)
    conn.close()

    if not datos['kpi']:
        raise SystemExit(f"No hay datos para el periodo {args.periodo}")

    salida = args.salida or os.path.join(RAIZ, f"informe_{args.periodo}.html")
    with open(salida, 'w', encoding='utf-8') as f:
        f.write(construir(datos))

    kb = os.path.getsize(salida) / 1024
    print(f"Informe generado: {salida}  ({kb:.0f} KB)")
    print(f"  {datos['kpi']['total_hallazgos']} hallazgos - "
          f"{datos['kpi']['guardas_afectados']} guardas - "
          f"{len(datos['puntuales'])} puntuales - {len(datos['nomina'])} filas de nomina")
    print("  Se abre con doble clic. No necesita servidor, base de datos ni internet.")
    print("  CONTIENE DATOS REALES: distribucion interna unicamente.")


if __name__ == '__main__':
    main()
