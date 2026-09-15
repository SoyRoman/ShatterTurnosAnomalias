"""
Pruebas del login propio, la proteccion de URL y el rastro de auditoria.

    python test_api_identidad.py                   # no toca datos de negocio
    python test_api_identidad.py --con-escritura   # + el camino de escritura

Necesita la base de datos (a diferencia de test_reglas.py): la API abre su pool
al arrancar, y aqui ademas se crean y borran usuarios de prueba.

Que protege, y por que hace falta
---------------------------------
Tres propiedades que pueden romperse sin que nada falle a la vista:

1. **Ninguna ruta entrega datos sin sesion.** Agregar un endpoint y olvidar la
   proteccion lo deja abierto en silencio. Por eso el muro es un middleware
   global y esta prueba recorre TODAS las rutas declaradas, no una lista fija:
   un endpoint nuevo queda cubierto automaticamente.
2. **La cabecera X-Usuario no puede suplantar a la sesion.** Un `or` de mas en
   `resolver_identidad` y cualquiera podria firmar una justificacion con el
   nombre de otro. El dashboard seguiria funcionando igual.
3. **Desactivar una cuenta corta el acceso YA.** Es lo que justifica guardar las
   sesiones en tabla en vez de usar un JWT; si alguien lo cambia por un token
   autocontenido "para simplificar", esta prueba lo detecta.

Los usuarios de prueba se crean con prefijo `_prueba_` y se borran al final,
incluso si algo falla.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, RAIZ)
import autenticacion as auth   # noqa: E402

PUERTO = 8041
PREFIJO = '_prueba_'
ADMIN = PREFIJO + 'admin'
LLANO = PREFIJO + 'llano'
CLAVE_ADMIN = 'clave de prueba administrador'
CLAVE_LLANO = 'clave de prueba sin privilegios'

fallos = []


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def conectar():
    load_dotenv(os.path.join(RAIZ, '.env'))
    return psycopg2.connect(
        host=os.environ['DB_HOST'], port=os.environ.get('DB_PORT', '5432'),
        dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'])


def comprobar(etiqueta, obtenido, esperado):
    ok = obtenido == esperado
    print(f"  [{'OK ' if ok else 'MAL'}] {etiqueta}: {obtenido} (esperado {esperado})")
    if not ok:
        fallos.append(etiqueta)


class _SinSeguirRedirecciones(urllib.request.HTTPRedirectHandler):
    """urllib sigue los 3xx por su cuenta y aqui eso esconde lo que se prueba.

    El muro responde 303 hacia /login a un navegador sin sesion; si el cliente
    lo sigue, la prueba ve un 200 de la pagina de login y no distingue "me
    redirigio" de "me dejo entrar". Hay que ver el 303 tal cual.
    """
    def redirect_request(self, *a, **k):
        return None


_ABRIDOR = urllib.request.build_opener(_SinSeguirRedirecciones)


def _json_o_nada(bruto):
    """El cuerpo no siempre es JSON: /login y / devuelven HTML."""
    try:
        return json.loads(bruto or b'null')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def peticion(ruta, metodo='GET', cuerpo=None, cookie=None, cabeceras=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    pet = urllib.request.Request(f"http://127.0.0.1:{PUERTO}{ruta}", data=datos, method=metodo)
    if datos:
        pet.add_header('Content-Type', 'application/json')
    if cookie:
        pet.add_header('Cookie', f"{auth.NOMBRE_COOKIE}={cookie}")
    for k, v in (cabeceras or {}).items():
        pet.add_header(k, v)
    try:
        with _ABRIDOR.open(pet, timeout=10) as r:
            galleta = None
            for clave, valor in r.getheaders():
                if clave.lower() == 'set-cookie' and auth.NOMBRE_COOKIE in valor:
                    galleta = valor.split(';')[0].split('=', 1)[1]
            return r.status, _json_o_nada(r.read()), galleta
    except urllib.error.HTTPError as e:
        # Un 3xx tambien llega aqui, porque el manejador de arriba se niega a
        # seguirlo. Es justo lo que se quiere observar.
        return e.code, _json_o_nada(e.read()), None


def arrancar():
    proceso = subprocess.Popen(
        [sys.executable, '-m', 'uvicorn', 'api:app', '--host', '127.0.0.1',
         '--port', str(PUERTO), '--log-level', 'warning'],
        cwd=RAIZ, env={**os.environ, 'MODO_IDENTIDAD': 'SESION', 'COOKIE_SEGURA': '0'},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if proceso.poll() is not None:
            raise SystemExit("la API murio al arrancar; revisa el .env y PostgreSQL")
        try:
            peticion('/salud')
            return proceso
        except Exception:
            time.sleep(0.5)
    proceso.kill()
    raise SystemExit("la API no respondio en 30 s")


def sembrar(cur):
    limpiar(cur)
    auth.crear_usuario(cur, ADMIN, 'Admin De Prueba', 'ADMIN', CLAVE_ADMIN,
                       creado_por='test', debe_cambiar=False)
    auth.crear_usuario(cur, LLANO, 'Usuario De Prueba', 'PROGRAMADOR', CLAVE_LLANO,
                       creado_por='test', debe_cambiar=False)


def limpiar(cur):
    cur.execute("DELETE FROM usuarios WHERE usuario LIKE %s", (PREFIJO + '%',))
    cur.execute("DELETE FROM accesos WHERE usuario LIKE %s", (PREFIJO + '%',))


# ---------------------------------------------------------------------------
# Pruebas
# ---------------------------------------------------------------------------

def probar_muro(cookie_admin):
    """Recorre TODAS las rutas declaradas por la propia API."""
    print("=== El muro: ninguna ruta entrega datos sin sesion ===")

    esquema = peticion('/openapi.json', cookie=cookie_admin)[1]
    rutas = [r for r in esquema['paths']
             if '{' not in r and r not in auth_rutas_libres()]
    print(f"    descubiertas {len(rutas)} rutas GET/POST sin parametros de ruta")

    abiertas = []
    for ruta in rutas:
        cod, _, _ = peticion(ruta)
        if cod not in (401, 403, 404, 405, 422):
            abiertas.append(f"{ruta} -> {cod}")
    comprobar("ninguna ruta responde sin sesion", abiertas, [])

    # El dashboard y la documentacion tampoco, que es lo que un middleware
    # cubre y un `Depends` por ruta no.
    for ruta in ('/', '/docs', '/openapi.json'):
        cod, _, _ = peticion(ruta)
        comprobar(f"{ruta} sin sesion", cod, 401)

    # A un navegador se le redirige al login en vez de un 401 crudo.
    cod, _, _ = peticion('/', cabeceras={'Accept': 'text/html'})
    comprobar("un navegador es redirigido a /login", cod, 303)

    for ruta in ('/salud', '/login'):
        cod, _, _ = peticion(ruta)
        comprobar(f"{ruta} es publica", cod, 200)


def auth_rutas_libres():
    import api
    return api.RUTAS_LIBRES


def probar_clasificacion():
    """Ninguna ruta de la API puede quedar fuera de la tabla de permisos.

    Es la prueba que hace que la tabla no se quede atras: si alguien agrega un
    endpoint y no lo clasifica, esto falla aqui en vez de dejarlo inaccesible
    (o peor, accesible) en produccion sin que nadie lo note.
    """
    print("=== Toda ruta esta clasificada ===")
    import api
    rutas = [r for r in api.app.openapi()['paths']]
    sin_clasificar = []
    for ruta in rutas:
        # Las rutas con parametro se resuelven por prefijo; se prueba con un
        # valor concreto, que es lo que vera el middleware.
        concreta = ruta.replace('{anomalia_id}', '1').replace('{usuario_id}', '1')
        if concreta in api.RUTAS_LIBRES:
            continue
        if api.roles_de_ruta(concreta) is None:
            sin_clasificar.append(ruta)
    comprobar(f"las {len(rutas)} rutas tienen roles asignados", sin_clasificar, [])

    # Y las paginas que no son endpoints declarados.
    for ruta in ('/', '/docs', '/openapi.json'):
        comprobar(f"{ruta} clasificada", api.roles_de_ruta(ruta) is not None, True)


def probar_autorizacion(cur):
    """Cada rol alcanza su bandeja y ninguna otra."""
    print()
    print("=== Autorizacion por URL segun el rol ===")
    import api

    # Un usuario por rol, para probar la matriz completa.
    claves = {}
    for rol in ('GERENCIA', 'NOMINA'):
        nombre = PREFIJO + rol.lower()
        clave = f"clave de prueba {rol.lower()}"
        cur.execute("DELETE FROM usuarios WHERE usuario = %s", (nombre,))
        auth.crear_usuario(cur, nombre, f"{rol} De Prueba", rol, clave,
                           creado_por='test', debe_cambiar=False)
        claves[rol] = (nombre, clave)
    claves['PROGRAMADOR'] = (LLANO, CLAVE_LLANO)
    claves['ADMIN'] = (ADMIN, CLAVE_ADMIN)

    esperado = {
        '/kpi':               {'ADMIN', 'GERENCIA'},
        '/estructural':       {'ADMIN', 'GERENCIA'},
        '/clientes':          {'ADMIN', 'GERENCIA'},
        '/informe/mensual?periodo=2026-07': {'ADMIN', 'GERENCIA'},
        '/anomalias':         {'ADMIN', 'GERENCIA', 'PROGRAMADOR'},
        '/nomina':            {'ADMIN', 'GERENCIA', 'NOMINA'},
        '/usuarios':          {'ADMIN'},
        '/accesos':           {'ADMIN'},
        '/docs':              {'ADMIN'},
        '/periodos':          {'ADMIN', 'GERENCIA', 'PROGRAMADOR', 'NOMINA'},
        '/reglas':            {'ADMIN', 'GERENCIA', 'PROGRAMADOR', 'NOMINA'},
    }

    cookies = {}
    for rol, (nombre, clave) in claves.items():
        _, _, cookies[rol] = peticion('/login', 'POST', {'usuario': nombre, 'clave': clave})

    errores = []
    for ruta, permitidos in esperado.items():
        for rol, cookie in cookies.items():
            cod, _, _ = peticion(ruta, cookie=cookie)
            deberia_entrar = rol in permitidos
            entro = cod == 200
            if entro != deberia_entrar:
                errores.append(f"{rol} en {ruta}: HTTP {cod} "
                               f"(esperaba {'200' if deberia_entrar else '403'})")
    comprobar(f"matriz de {len(esperado)} rutas x {len(cookies)} roles", errores, [])

    # El endpoint peligroso: recarga la base entera y recibe la ruta del archivo
    # en la peticion. Un PROGRAMADOR no puede dispararlo.
    for rol in ('PROGRAMADOR', 'NOMINA', 'GERENCIA'):
        cod, _, _ = peticion('/pipeline/ejecutar', 'POST', {'archivo': 'x.xlsx'},
                             cookie=cookies[rol])
        comprobar(f"{rol} NO puede ejecutar el pipeline", cod, 403)

    # Una ruta inventada no revela nada.
    cod, _, _ = peticion('/inventada', cookie=cookies['ADMIN'])
    comprobar("ruta sin clasificar se niega", cod, 404)

    # Los paneles que ve cada rol salen de la misma tabla.
    _, ident, _ = peticion('/identidad', cookie=cookies['PROGRAMADOR'])
    comprobar("un PROGRAMADOR solo ve su bandeja", ident['paneles'], ['programador'])
    _, ident, _ = peticion('/identidad', cookie=cookies['NOMINA'])
    comprobar("un NOMINA solo ve la suya", ident['paneles'], ['nomina'])
    _, ident, _ = peticion('/identidad', cookie=cookies['ADMIN'])
    comprobar("un ADMIN las ve todas", len(ident['paneles']), 4)

    return cookies


def probar_login():
    print("\n=== Login ===")
    cod, _, _ = peticion('/login', 'POST', {'usuario': ADMIN, 'clave': 'incorrecta'})
    comprobar("clave incorrecta", cod, 401)

    # El mensaje debe ser el MISMO: distinguirlos confirmaria que cuentas
    # existen, que es el primer paso para dirigir la fuerza bruta.
    _, c1, _ = peticion('/login', 'POST', {'usuario': ADMIN, 'clave': 'incorrecta'})
    _, c2, _ = peticion('/login', 'POST', {'usuario': PREFIJO + 'noexiste', 'clave': 'x'})
    comprobar("el error no revela si la cuenta existe", c1['detail'], c2['detail'])

    cod, cuerpo, cookie = peticion('/login', 'POST', {'usuario': ADMIN, 'clave': CLAVE_ADMIN})
    comprobar("clave correcta", cod, 200)
    comprobar("entrega cookie de sesion", bool(cookie), True)
    comprobar("reporta el rol", cuerpo['rol'], 'ADMIN')
    return cookie


def probar_roles(cookie_admin):
    print("\n=== Roles: solo ADMIN gestiona cuentas ===")
    _, _, cookie_llano = peticion('/login', 'POST', {'usuario': LLANO, 'clave': CLAVE_LLANO})

    for ruta, metodo, cuerpo in (('/usuarios', 'GET', None),
                                 ('/accesos', 'GET', None),
                                 ('/usuarios', 'POST', {'usuario': PREFIJO + 'colado',
                                                        'nombre': 'Colado', 'rol': 'ADMIN'})):
        cod, _, _ = peticion(ruta, metodo, cuerpo, cookie=cookie_llano)
        comprobar(f"{metodo} {ruta} sin ser ADMIN", cod, 403)

    cod, _, _ = peticion('/anomalias', cookie=cookie_llano)
    comprobar("un PROGRAMADOR si alcanza su bandeja", cod, 200)
    cod, _, _ = peticion('/kpi', cookie=cookie_llano)
    comprobar("pero NO la de gerencia", cod, 403)

    cod, _, _ = peticion('/usuarios', cookie=cookie_admin)
    comprobar("/usuarios siendo ADMIN", cod, 200)
    return cookie_llano


def probar_corte_de_acceso(cur, cookie_admin, cookie_llano):
    """Lo que justifica sesiones en tabla en vez de un JWT."""
    print("\n=== Desactivar una cuenta corta el acceso al instante ===")
    cod, _, _ = peticion('/anomalias', cookie=cookie_llano)
    comprobar("antes de desactivar, la sesion sirve", cod, 200)

    cur.execute("SELECT id FROM usuarios WHERE usuario = %s", (LLANO,))
    id_llano = cur.fetchone()['id']
    cod, cuerpo, _ = peticion(f'/usuarios/{id_llano}', 'PATCH', {'activo': False},
                              cookie=cookie_admin)
    comprobar("el ADMIN desactiva la cuenta", cod, 200)
    comprobar("y le cierra las sesiones", cuerpo['sesiones_cerradas'] >= 1, True)

    cod, _, _ = peticion('/anomalias', cookie=cookie_llano)
    comprobar("la MISMA cookie ya no sirve", cod, 401)
    cod, _, _ = peticion('/login', 'POST', {'usuario': LLANO, 'clave': CLAVE_LLANO})
    comprobar("y tampoco puede volver a entrar", cod, 401)


def probar_ultimo_admin(cur, cookie_admin):
    print("\n=== No se puede dejar el sistema sin ADMIN ===")
    cur.execute("SELECT id FROM usuarios WHERE usuario = %s", (ADMIN,))
    mi_id = cur.fetchone()['id']
    cod, _, _ = peticion(f'/usuarios/{mi_id}', 'PATCH', {'activo': False}, cookie=cookie_admin)
    comprobar("un ADMIN no puede desactivarse a si mismo", cod, 409)
    cod, _, _ = peticion(f'/usuarios/{mi_id}', 'PATCH', {'rol': 'NOMINA'}, cookie=cookie_admin)
    comprobar("ni degradarse a si mismo", cod, 409)


def probar_suplantacion(cookie_admin):
    print("\n=== X-Usuario no puede suplantar a la sesion ===")
    # Id inexistente: `resolver_identidad` corre antes del handler, asi que un
    # 404 significa "identidad aceptada" sin escribir nada.
    cod, _, _ = peticion('/anomalias/999999999', 'PATCH',
                         {'estado': 'EN_REVISION', 'nota': 'x'},
                         cookie=cookie_admin, cabeceras={'X-Usuario': 'el_jefe'})
    comprobar("con sesion, la cabecera no estorba (404 = llego al handler)", cod, 404)

    cod, _, _ = peticion('/anomalias/999999999', 'PATCH',
                         {'estado': 'EN_REVISION', 'nota': 'x'},
                         cabeceras={'X-Usuario': 'el_jefe'})
    comprobar("SIN sesion, la cabecera sola no sirve", cod, 401)

    cod, _, _ = peticion('/anomalias?cedula=123456', cookie=cookie_admin)
    comprobar("/anomalias?cedula= (parametro inventado) -> 422", cod, 422)


def probar_escritura(cur, cookie_admin):
    """Verifica QUE queda escrito. Modifica una anomalia real y la restaura."""
    print("\n=== La identidad de la sesion es la que se guarda ===")
    cur.execute("SELECT (SELECT count(*) FROM anomalias) a,"
                "       (SELECT count(*) FROM anomalias_historial) h")
    base = dict(cur.fetchone())
    cur.execute("""SELECT id, estado, nota, actualizado_por, actualizado_en
                     FROM anomalias WHERE estado = 'ABIERTA' ORDER BY id LIMIT 1""")
    antes = cur.fetchone()
    if not antes:
        print("  (no hay anomalias ABIERTA; se omite)")
        return
    print(f"    anomalia de prueba: id={antes['id']}")
    try:
        cod, _, _ = peticion(f"/anomalias/{antes['id']}", 'PATCH',
                             {'estado': 'EN_REVISION', 'nota': 'prueba automatica'},
                             cookie=cookie_admin, cabeceras={'X-Usuario': 'el_jefe'})
        comprobar("PATCH con sesion -> 200", cod, 200)

        cur.execute("SELECT actualizado_por FROM anomalias WHERE id = %s", (antes['id'],))
        comprobar("anomalias.actualizado_por = la SESION, no la cabecera",
                  cur.fetchone()['actualizado_por'], ADMIN)
        cur.execute("SELECT usuario FROM anomalias_historial WHERE anomalia_id = %s",
                    (antes['id'],))
        hist = [r['usuario'] for r in cur.fetchall()]
        comprobar("anomalias_historial.usuario = la SESION", hist, [ADMIN])
    finally:
        cur.execute("""UPDATE anomalias SET estado=%s, nota=%s, actualizado_por=%s,
                              actualizado_en=%s WHERE id=%s""",
                    (antes['estado'], antes['nota'], antes['actualizado_por'],
                     antes['actualizado_en'], antes['id']))
        cur.execute("DELETE FROM anomalias_historial WHERE anomalia_id = %s", (antes['id'],))
        cur.execute("SELECT (SELECT count(*) FROM anomalias) a,"
                    "       (SELECT count(*) FROM anomalias_historial) h")
        comprobar("la base volvio a como estaba", dict(cur.fetchone()), base)


def main():
    cn = conectar()
    cn.autocommit = True
    cur = cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    proceso = None
    try:
        sembrar(cur)
        proceso = arrancar()

        cookie_admin = probar_login()
        probar_clasificacion()
        probar_muro(cookie_admin)
        probar_autorizacion(cur)
        cookie_llano = probar_roles(cookie_admin)
        probar_corte_de_acceso(cur, cookie_admin, cookie_llano)
        probar_ultimo_admin(cur, cookie_admin)
        probar_suplantacion(cookie_admin)
        if '--con-escritura' in sys.argv:
            probar_escritura(cur, cookie_admin)
        else:
            print("\n(omitida la prueba de escritura; para incluirla: "
                  "python test_api_identidad.py --con-escritura)")
    finally:
        if proceso:
            proceso.kill()
        limpiar(cur)
        cn.close()

    print()
    if fallos:
        print(f"FALLARON {len(fallos)}:")
        for f in fallos:
            print(f"  - {f}")
        sys.exit(1)
    print("Todas las comprobaciones pasaron. Usuarios de prueba eliminados.")


if __name__ == '__main__':
    main()
