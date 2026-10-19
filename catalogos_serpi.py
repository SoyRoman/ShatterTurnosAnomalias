"""
Sincroniza los catalogos de la API de turnos de SERPI con la base.

    python catalogos_serpi.py

Lo llama pipeline_diario.py antes de descargar la malla. Trae proyectos (NIT,
centro de operacion, zona), puestos (identificador fijo) y asignaciones
colaborador -> puesto (cargo, contrato), y vincula cada puesto de la malla con
su puesto de SERPI.

NO es parte de la auditoria: si la API falla, la corrida diaria sigue sin los
catalogos (quedan los de la ultima sincronizacion). Por eso sale con 0 aunque
no tenga credenciales, y con 1 solo si algo fallo de verdad.

Credenciales en .env (en el servidor, desde Parameter Store), nunca en codigo:
    SERPI_TOKEN=<JWT de la API>
    SERPI_SECRET_KEY=<SecretKey de la empresa>
"""
import json
import os
import re
import ssl
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.abspath(__file__))
BASE = "https://apis.serpi.com.co/api/v1/Turnos"
POR_PAGINA = 1000

# Se sigue verificando la cadena de certificados; solo se relaja la regla
# "estricta" de Python 3.13+, que rechaza la raiz con la que Norton (y otros
# antivirus) reemiten el HTTPS en los equipos de la oficina.
_SSL = ssl.create_default_context()
_SSL.verify_flags &= ~ssl.VERIFY_X509_STRICT


def normalizar(texto):
    """Mayusculas, sin tildes ni espacios dobles: para cruzar nombres que la
    malla y la API escriben con pequenas diferencias."""
    s = unicodedata.normalize('NFKD', texto or '').encode('ascii', 'ignore').decode()
    return re.sub(r'\s+', ' ', s.upper()).strip()


def pedir_todo(ruta, token, secretkey):
    """Todas las paginas de un endpoint de la API."""
    filas, pagina = [], 1
    while True:
        url = f"{BASE}/{ruta}?" + urllib.parse.urlencode({'limite': POR_PAGINA, 'pagina': pagina})
        req = urllib.request.Request(url, headers={
            'Authorization': f'Bearer {token}', 'secretkey': secretkey, 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=120, context=_SSL) as r:
            if r.status == 204:
                break
            lote = json.loads(r.read().decode('utf-8')).get('result') or []
        filas += lote
        if len(lote) < POR_PAGINA:
            break
        pagina += 1
        if pagina > 100:
            raise RuntimeError(f"{ruta}: mas de 100 paginas, algo anda mal con la paginacion")
    return filas


def fecha(s):
    return s[:10] if s else None


def guardar(cur, proyectos, asignaciones):
    cur.execute("DELETE FROM serpi_proyectos")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO serpi_proyectos (gdtproyectoid, descripcion, abreviatura, cliente, nit,
                                     centro_operacion, zona, activo) VALUES %s""",
        [(p['gdtproyectoid'], p.get('descripcion'), p.get('abreviatura'), p.get('cliente'),
          p.get('nit'), p.get('centroOperacion'), p.get('grupo'), p.get('activo')) for p in proyectos])

    puestos = {a['gdtproyectoactid']: (a['gdtproyectoid'], a.get('puesto')) for a in asignaciones}
    cur.execute("DELETE FROM serpi_puestos")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO serpi_puestos (gdtproyectoactid, gdtproyectoid, nombre) VALUES %s""",
        [(k, v[0], (v[1] or '').strip()) for k, v in puestos.items()])

    # Una misma asignacion puede venir repetida; la llave la deduplica.
    unicas = {}
    for a in asignaciones:
        clave = (a['grhcontratoid'], a['gdtproyectoactid'], fecha(a.get('fechaini')) or '1900-01-01')
        unicas[clave] = a
    cur.execute("DELETE FROM serpi_asignaciones")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO serpi_asignaciones (grhcontratoid, gdtproyectoactid, fechaini, fechafin,
                                        colaborador, colaborador_norm, cargo, activo) VALUES %s""",
        [(k[0], k[1], k[2], fecha(a.get('fechafin')), a.get('colaborador'),
          normalizar(a.get('colaborador')), a.get('cargo'), a.get('activo')) for k, a in unicas.items()])
    return len(puestos), len(unicas)


def vincular_puestos(cur):
    """Decide a que puesto de SERPI corresponde cada puesto de la malla.

    Candidatos: puestos de SERPI cuyo nombre es igual al de la malla, o es el
    comienzo del de la malla (la malla agrega sufijos: "N.U PORTERIA" ->
    "N.U PORTERIA - CELULAR"). Entre los candidatos gana el que comparte mas
    guardas con la malla de los ultimos 120 dias, porque el mismo nombre de
    puesto se repite entre clientes distintos. Sin guardas en comun solo se
    vincula si hay un unico candidato.
    """
    cur.execute("SELECT gdtproyectoactid, gdtproyectoid, nombre FROM serpi_puestos")
    api = [(pid, proy, normalizar(n)) for pid, proy, n in cur.fetchall()]
    cur.execute("SELECT gdtproyectoactid, colaborador_norm FROM serpi_asignaciones")
    guardas_api = defaultdict(set)
    for pid, nombre in cur.fetchall():
        guardas_api[pid].add(nombre)

    cur.execute("""SELECT p.id, p.nombre, array_agg(DISTINCT g.nombre)
                   FROM puestos p
                   JOIN turnos t  ON t.puesto_id = p.id AND t.fecha >= current_date - 120
                   JOIN guardas g ON g.cedula = t.guarda_cedula
                   GROUP BY p.id, p.nombre""")
    vinculos, sin_vinculo = [], 0
    for puesto_id, nombre, guardas in cur.fetchall():
        n = normalizar(nombre)
        mios = {normalizar(g) for g in guardas}
        candidatos = [(pid, proy, an) for pid, proy, an in api
                      if an and (n == an or n.startswith(an + ' -') or n.startswith(an + ' '))]
        if not candidatos:
            sin_vinculo += 1
            continue
        puntaje = sorted(((len(mios & guardas_api[pid]), n == an, len(an), pid, proy)
                          for pid, proy, an in candidatos), reverse=True)
        comun, exacto, _, pid, proy = puntaje[0]
        if comun == 0 and len(candidatos) > 1:
            sin_vinculo += 1
            continue
        metodo = ('nombre exacto' if exacto else 'nombre con sufijo') + (
            f' + {comun} guardas en comun' if comun else ' (candidato unico)')
        vinculos.append((puesto_id, pid, proy, metodo, comun))

    cur.execute("DELETE FROM serpi_puesto_vinculo")
    psycopg2.extras.execute_values(cur, """
        INSERT INTO serpi_puesto_vinculo (puesto_id, gdtproyectoactid, gdtproyectoid, metodo,
                                          guardas_en_comun) VALUES %s""", vinculos)
    return len(vinculos), sin_vinculo


def main():
    load_dotenv(os.path.join(RAIZ, '.env'))
    token = (os.environ.get('SERPI_TOKEN') or '').strip()
    secretkey = (os.environ.get('SERPI_SECRET_KEY') or '').strip()
    if not token or not secretkey:
        print("Sin SERPI_TOKEN / SERPI_SECRET_KEY: no se sincronizan los catalogos (se usan los ultimos).")
        return 0

    try:
        proyectos = pedir_todo('proyectos', token, secretkey)
        asignaciones = pedir_todo('puestos', token, secretkey)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
        detalle = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else type(e).__name__
        print(f"No se pudo consultar la API de SERPI ({detalle}). Se conservan los catalogos anteriores.")
        return 1
    if not proyectos or not asignaciones:
        print("La API devolvio catalogos vacios. Se conservan los anteriores.")
        return 1

    conn = psycopg2.connect(
        host=os.environ['DB_HOST'], port=os.environ.get('DB_PORT', '5432'),
        dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'], sslmode=os.environ.get('DB_SSLMODE', 'prefer'))
    try:
        with conn, conn.cursor() as cur:
            n_puestos, n_asig = guardar(cur, proyectos, asignaciones)
            vinculados, sin = vincular_puestos(cur)
    finally:
        conn.close()
    print(f"Catalogos SERPI: {len(proyectos)} proyectos, {n_puestos} puestos, {n_asig} asignaciones.")
    print(f"Puestos de la malla vinculados a SERPI: {vinculados} (sin vinculo: {sin}).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
