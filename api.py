"""
API de turnos y anomalias — puerto unico del sistema.

Todo lo que consume estos datos (el dashboard y futuras integraciones) entra
por aqui. Nadie mas abre conexiones a PostgreSQL: asi la logica de negocio no
se reparte entre varios clientes distintos y hay un solo lugar donde auditar
quien leyo y quien escribio.

Uso:
    uvicorn api:app --host 127.0.0.1 --port 8000

Variables de entorno adicionales a las de .env:
    MODO_IDENTIDAD       SESION (default) o DECLARATIVA. Quien es la PERSONA.
                         Ver el bloque de comentarios mas abajo.
    HORAS_SESION         Duracion de la sesion (default 12).
    COOKIE_SEGURA        1 (default) exige HTTPS para la cookie. Solo se pone
                         en 0 para desarrollo local por HTTP.
    API_TOKEN            Token compartido opcional para integraciones que no
                         son un navegador. Autentica al SISTEMA, no a la
                         persona: no sustituye el login.
"""

import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal, Optional

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from psycopg2.pool import SimpleConnectionPool
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

import autenticacion as auth

load_dotenv()

RAIZ = os.path.dirname(os.path.abspath(__file__))
API_TOKEN = os.environ.get('API_TOKEN')

# ---------------------------------------------------------------------------
# Identidad de la persona
# ---------------------------------------------------------------------------
# Autenticacion PROPIA: usuarios, claves y sesiones viven en esta base y no
# dependen de ningun proveedor externo (decision explicita, septiembre 2026).
# Toda la logica esta en `autenticacion.py`; aqui solo se conecta a HTTP.
#
# Dos modos:
#
#   SESION       El usuario inicia sesion en /login y recibe una cookie de
#                sesion. TODA ruta exige esa sesion. Es el modo normal.
#   DECLARATIVA  Sin login: se cree la cabecera `X-Usuario`. Existe solo para
#                desarrollo local y scripts; la API lo avisa al arrancar.
#
# Por que importa: `anomalias_historial` puede terminar sustentando una
# respuesta ante el Ministerio del Trabajo. Con identidad declarativa expuesta,
# cualquiera podria marcar una anomalia como JUSTIFICADA firmando con el nombre
# de otro — y un historial que parece confiable y no lo es es peor que no tener
# historial.
MODOS_IDENTIDAD = ('SESION', 'DECLARATIVA')
MODO_IDENTIDAD = os.environ.get('MODO_IDENTIDAD', 'SESION').upper()

if MODO_IDENTIDAD not in MODOS_IDENTIDAD:
    raise SystemExit(
        f"MODO_IDENTIDAD='{MODO_IDENTIDAD}' no es valido. "
        f"Opciones: {', '.join(MODOS_IDENTIDAD)}.")

# Rutas que NO exigen sesion. Deliberadamente cortas:
#   /salud   lo consulta el healthcheck del contenedor, desde dentro, y no
#            devuelve ningun dato personal (solo un conteo).
#   /login   es la puerta; exigir sesion para entrar seria circular.
RUTAS_LIBRES = frozenset({'/salud', '/login', '/favicon.ico'})

# `Secure` en la cookie exige HTTPS. En local se sirve por HTTP, asi que se
# apaga con COOKIE_SEGURA=0; en el servidor NUNCA debe apagarse: sin esto, la
# cookie de sesion viaja en claro y cualquiera en la red la puede copiar.
COOKIE_SEGURA = os.environ.get('COOKIE_SEGURA', '1') not in ('0', 'false', 'False')

_pool: Optional[SimpleConnectionPool] = None


@asynccontextmanager
async def ciclo_de_vida(app: FastAPI):
    global _pool
    _pool = SimpleConnectionPool(
        1, 10,
        host=os.environ['DB_HOST'], port=os.environ.get('DB_PORT', '5432'),
        dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'], sslmode=os.environ.get('DB_SSLMODE', 'prefer'),
    )
    if not API_TOKEN and MODO_IDENTIDAD != 'SESION':
        print("ADVERTENCIA: sin API_TOKEN y sin login. La API queda ABIERTA a "
              "quien alcance el puerto. Solo para desarrollo local.",
              file=sys.stderr)
    if MODO_IDENTIDAD == 'DECLARATIVA':
        print("ADVERTENCIA: MODO_IDENTIDAD=DECLARATIVA. No hay login: se cree la "
              "cabecera X-Usuario sin verificarla, asi que el historial de "
              "auditoria NO impide suplantacion y CUALQUIERA que alcance el "
              "puerto lee los datos. Solo para desarrollo local.", file=sys.stderr)
    else:
        with _conexion() as cn:
            with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                hay_admin = auth.hay_algun_admin(cur)
                n = auth.purgar_sesiones_vencidas(cur)
            cn.commit()
        print(f"Identidad en modo SESION. {n} sesion(es) vencida(s) purgada(s).",
              file=sys.stderr)
        if COOKIE_SEGURA:
            print("  COOKIE_SEGURA=1: la cookie de sesion exige HTTPS. Si abres la "
                  "app por http:// con una IP, el navegador la descartara y el "
                  "login no dejara sesion. Para red interna sin TLS: "
                  "COOKIE_SEGURA=0 en el .env.", file=sys.stderr)
        else:
            print("  ADVERTENCIA: COOKIE_SEGURA=0, la cookie de sesion viaja en "
                  "claro. Solo para desarrollo o red interna cerrada.",
                  file=sys.stderr)
        if not hay_admin:
            print("ADVERTENCIA: no hay ningun ADMIN activo, asi que nadie puede "
                  "gestionar cuentas. Crea el primero con: "
                  "python gestionar_usuarios.py crear --usuario <tu> "
                  "--nombre 'Tu Nombre' --rol ADMIN", file=sys.stderr)
    yield
    if _pool:
        _pool.closeall()


app = FastAPI(
    title="Deteccion de anomalias en mallas de turnos",
    description="Seguridad Shatter de Colombia LTDA BIC — API interna",
    version="1.0.0",
    lifespan=ciclo_de_vida,
)


def _conexion():
    """Conexion cruda del pool, para usar con `with`.

    `consultar`/`ejecutar` cubren el 95% de los casos, pero la autenticacion
    necesita varias sentencias en UNA transaccion: validar la clave, abrir la
    sesion y registrar el acceso tienen que confirmarse juntos o no confirmarse.
    """
    class _Ctx:
        def __enter__(self):
            self.cn = _pool.getconn()
            return self.cn

        def __exit__(self, *exc):
            if exc[0]:
                self.cn.rollback()
            _pool.putconn(self.cn)
            return False
    return _Ctx()


def _contexto(peticion: Request):
    """IP y navegador, para la bitacora de accesos.

    Se lee X-Forwarded-For porque en el servidor hay un proxy delante y, sin
    esto, TODA la bitacora diria la IP del proxy. No es identificacion —una
    cabecera la puede poner cualquiera—: es contexto para investigar un acceso
    raro.
    """
    reenviada = (peticion.headers.get('x-forwarded-for') or '').split(',')[0].strip()
    ip = reenviada or (peticion.client.host if peticion.client else None)
    return ip, peticion.headers.get('user-agent')


def conexion_segura(peticion: Request) -> bool:
    """Si el NAVEGADOR considera segura esta conexion.

    Importa porque una cookie marcada `Secure` que llega por HTTP se descarta
    en silencio: el login responde 200, no queda sesion, y la peticion
    siguiente dice "sesion no iniciada". Sin este chequeo el sintoma no apunta
    a la causa y se pierden horas.

    Tres casos, en orden:

    1. `X-Forwarded-Proto`, que pone el proxy que termina TLS. Hay que mirarlo
       primero: detras de nginx o un ALB, la peticion llega al proceso como
       HTTP aunque el usuario este usando HTTPS, y sin esto la API se negaria a
       funcionar justo en produccion.
    2. El esquema de la propia peticion.
    3. localhost. Los navegadores lo tratan como contexto seguro y SI aceptan
       cookies `Secure` por HTTP ahi — que es exactamente por lo que este
       problema no aparece en la maquina de desarrollo y si al abrir la app por
       IP desde otro equipo.
    """
    reenviado = (peticion.headers.get('x-forwarded-proto') or '').split(',')[0].strip().lower()
    if reenviado:
        return reenviado == 'https'
    if peticion.url.scheme == 'https':
        return True
    return (peticion.url.hostname or '').lower() in ('localhost', '127.0.0.1', '::1')


def verificar_token(x_api_token: Optional[str] = Header(None)):
    """Token compartido opcional, para integraciones que no son un navegador.

    Autentica al SISTEMA que llama, no a la PERSONA, asi que NO sustituye al
    login: en modo SESION las rutas siguen exigiendo sesion aunque el token sea
    correcto.
    """
    if API_TOKEN and x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido o ausente")


def sesion_actual(peticion: Request):
    """Devuelve el usuario de la sesion, o None. No lanza."""
    if MODO_IDENTIDAD != 'SESION':
        return None
    token = peticion.cookies.get(auth.NOMBRE_COOKIE)
    if not token:
        return None
    with _conexion() as cn:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            fila = auth.sesion_valida(cur, token)
        cn.commit()
    return fila


def exigir_sesion(peticion: Request):
    """Dependencia GLOBAL: ninguna ruta se sirve sin sesion valida.

    Es global y no ruta por ruta a proposito. Con `dependencies=[...]` en cada
    decorador, agregar un endpoint nuevo y olvidar la dependencia lo deja
    abierto — y nada falla, asi que nadie se entera. Aqui el olvido es
    imposible: lo que no este en RUTAS_LIBRES exige sesion.
    """
    if MODO_IDENTIDAD != 'SESION':
        return None
    fila = sesion_actual(peticion)
    if not fila:
        raise HTTPException(status_code=401, detail="Sesion no iniciada o vencida")
    return fila


def exigir_admin(usuario=Depends(exigir_sesion)):
    """Solo ADMIN gestiona cuentas."""
    if MODO_IDENTIDAD != 'SESION':
        return None
    if usuario['rol'] != 'ADMIN':
        raise HTTPException(
            status_code=403,
            detail="Solo un usuario con rol ADMIN puede gestionar cuentas")
    return usuario


def resolver_identidad(peticion: Request) -> str:
    """Quien esta haciendo el cambio; es lo que queda escrito en el historial.

    En modo SESION sale de la sesion verificada y **no hay forma de
    sobreescribirlo desde el cliente**: `X-Usuario` se ignora por completo.
    Aceptar esa cabecera como alternativa seria el agujero que todo esto cierra
    — bastaria con enviarla para firmar como cualquiera.

    Se guarda el `usuario` y no el nombre para mostrar: el nombre puede
    cambiar (correcciones, matrimonio) y el historial debe seguir apuntando a
    la misma cuenta.
    """
    if MODO_IDENTIDAD == 'SESION':
        fila = sesion_actual(peticion)
        if not fila:
            raise HTTPException(status_code=401, detail="Sesion no iniciada o vencida")
        return fila['usuario']

    quien = (peticion.headers.get('X-Usuario') or '').strip()
    if not quien:
        raise HTTPException(
            status_code=422,
            detail="Falta la cabecera X-Usuario: toda gestion debe quedar atribuida")
    return quien


# ---------------------------------------------------------------------------
# Autorizacion por URL
# ---------------------------------------------------------------------------
# Que ruta puede tocar cada rol. Es una TABLA y no `if`s repartidos por los
# endpoints, por la misma razon por la que el muro es un middleware: lo que se
# declara en un solo sitio se puede auditar de un vistazo, y lo que se reparte
# se olvida.
#
# Criterio: cada audiencia alcanza SU bandeja y nada mas. GERENCIA ve todo lo
# operativo porque su panel es el panorama completo; ADMIN suma la gestion de
# cuentas y la ejecucion del pipeline.
#
# FALLA CERRADO: una ruta que no este aqui se niega a todo el mundo (ver
# `roles_de_ruta`). Es deliberado — agregar un endpoint y olvidar clasificarlo
# no puede dejarlo accesible en silencio.

_TODOS = frozenset(auth.ROLES)
_MANDO = frozenset({'ADMIN', 'GERENCIA'})

PERMISOS = {
    # Comunes: la cascara del dashboard y lo que necesita cualquiera para
    # operar. Ninguna devuelve datos de un guarda.
    '/':                 _TODOS,
    '/identidad':        _TODOS,
    '/logout':           _TODOS,
    '/cambiar-clave':    _TODOS,
    '/periodos':         _TODOS,   # el selector de mes lo usan las tres bandejas
    '/reglas':           _TODOS,   # catalogo normativo, sin PII

    # Bandeja del programador: hallazgos puntuales.
    '/anomalias':        _MANDO | {'PROGRAMADOR'},

    # Bandeja de gerencia: panorama, clientes y hallazgos estructurales.
    '/kpi':              _MANDO,
    '/clientes':         _MANDO,
    '/estructural':      _MANDO,
    '/informe/mensual':  _MANDO,

    # Bandeja de nomina: descuadres de horas.
    '/nomina':           _MANDO | {'NOMINA'},

    # Solo ADMIN. `/pipeline/ejecutar` recarga la base entera y recibe la ruta
    # del archivo en la peticion: no es una consulta, es una operacion de
    # mantenimiento.
    '/pipeline/ejecutar': frozenset({'ADMIN'}),
    '/usuarios':          frozenset({'ADMIN'}),
    '/accesos':           frozenset({'ADMIN'}),

    # La documentacion interactiva describe TODA la superficie de la API,
    # incluidas las rutas de administracion. No hay razon para que la vea quien
    # no puede usarlas.
    '/docs':             frozenset({'ADMIN'}),
    '/redoc':            frozenset({'ADMIN'}),
    '/openapi.json':     frozenset({'ADMIN'}),
}

# Rutas con parametro. El middleware ve el camino concreto (`/anomalias/4247`),
# asi que hay que resolverlas por prefijo. El orden importa: el primero que
# coincida gana, y los mas especificos van primero.
PERMISOS_PREFIJO = (
    ('/usuarios/',  frozenset({'ADMIN'})),
    # El detalle de una anomalia y su gestion los alcanzan las tres audiencias:
    # cada una llega desde su propia bandeja, y quien puede ver un hallazgo
    # tiene que poder justificarlo.
    ('/anomalias/', _TODOS),
)


# Que pestana del dashboard corresponde a cada ruta representativa. Se deriva de
# PERMISOS en vez de repetir la lista de roles en el JavaScript: con dos tablas,
# tarde o temprano uno cambia y el otro no, y el dashboard ofreceria una pestana
# que el servidor va a rechazar.
PANELES = {
    'gerencia':    '/kpi',
    'programador': '/anomalias',
    'nomina':      '/nomina',
    'admin':       '/usuarios',
}


def paneles_de_rol(rol: str):
    return [panel for panel, ruta in PANELES.items() if rol in PERMISOS[ruta]]


def roles_de_ruta(ruta: str):
    """Roles admitidos para una ruta, o None si no esta clasificada.

    `None` significa denegar. Una ruta nueva sin entrada en la tabla no queda
    accesible por descuido: deja de funcionar, que es el fallo correcto.
    """
    if ruta in PERMISOS:
        return PERMISOS[ruta]
    for prefijo, roles in PERMISOS_PREFIJO:
        if ruta.startswith(prefijo):
            return roles
    return None


@app.middleware("http")
async def muro_de_sesion(peticion: Request, siguiente):
    """Cierra TODA la aplicacion, no solo las rutas declaradas.

    Un `Depends` global protege las rutas de la API, pero no cubre la pagina
    del dashboard ni la documentacion automatica. Sin este muro, `/docs` y
    `/openapi.json` seguirian publicando la superficie completa de la API a
    cualquiera que alcance el puerto.

    A un navegador se le responde con una redireccion a /login en vez de un 401
    crudo; a una peticion de datos (fetch/XHR), con 401, para que el JS lo
    pueda manejar.
    """
    ruta = peticion.url.path
    if MODO_IDENTIDAD != 'SESION' or ruta in RUTAS_LIBRES:
        return await siguiente(peticion)

    usuario = sesion_actual(peticion)
    if not usuario:
        acepta = peticion.headers.get('accept', '')
        es_navegacion = 'text/html' in acepta and peticion.method == 'GET'
        if es_navegacion:
            return RedirectResponse('/login', status_code=303)
        return JSONResponse({"detail": "Sesion no iniciada o vencida"}, status_code=401)

    # Hay sesion; ahora, si ESTA ruta le corresponde a ESE rol.
    permitidos = roles_de_ruta(ruta)
    if permitidos is None:
        # Ruta sin clasificar. Puede ser un endpoint nuevo que nadie asigno, o
        # sencillamente una URL que no existe. En ambos casos se niega: no
        # distinguirlos ademas evita confirmar que rutas existen.
        return JSONResponse(
            {"detail": "Ruta no disponible"}, status_code=404)

    if usuario['rol'] not in permitidos:
        return JSONResponse(
            {"detail": f"Tu rol ({usuario['rol']}) no tiene acceso a esta seccion"},
            status_code=403)

    return await siguiente(peticion)


# ---------------------------------------------------------------------------
# Login y sesion
# ---------------------------------------------------------------------------

class Credenciales(BaseModel):
    usuario: str = Field(..., max_length=40)
    clave: str = Field(..., max_length=200)


class CambioClave(BaseModel):
    clave_actual: str = Field(..., max_length=200)
    clave_nueva: str = Field(..., max_length=200)


@app.get("/login", include_in_schema=False)
def pagina_login():
    return FileResponse(os.path.join(RAIZ, "login.html"))


@app.post("/login")
def iniciar_sesion(credenciales: Credenciales, peticion: Request):
    """Valida la clave y abre sesion.

    El mensaje de error es el MISMO para usuario inexistente y clave incorrecta.
    Distinguirlos le confirmaria a un atacante que cuentas existen, que es el
    primer paso para dirigir la fuerza bruta contra las que si.
    """
    if MODO_IDENTIDAD != 'SESION':
        raise HTTPException(status_code=404, detail="El login no esta activo en este modo")

    # Fallar AQUI y no despues. Con COOKIE_SEGURA=1 sobre HTTP, el navegador
    # descarta la cookie sin avisar: el login diria 200 y la pantalla siguiente
    # "sesion no iniciada o vencida", sin ninguna pista de la causa.
    if COOKIE_SEGURA and not conexion_segura(peticion):
        raise HTTPException(
            status_code=500,
            detail="COOKIE_SEGURA=1 exige HTTPS, y esta peticion llego por HTTP. "
                   "El navegador descartaria la cookie de sesion y el login no "
                   "serviria de nada. Dos salidas: pon un proxy con HTTPS delante "
                   "(lo correcto en el servidor), o COOKIE_SEGURA=0 en el .env si "
                   "esto es una prueba en red interna — a sabiendas de que la "
                   "cookie viajaria en claro y cualquiera en esa red podria "
                   "copiarla.")

    ip, agente = _contexto(peticion)
    generico = "Usuario o clave incorrectos"
    nombre = auth.normalizar_usuario(credenciales.usuario)

    with _conexion() as cn:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            fila = auth.buscar_usuario(cur, nombre)

            if not fila or not fila['activo']:
                # Se registra igual: un intento contra una cuenta inexistente o
                # desactivada es informacion util (alguien probando nombres).
                auth.registrar_acceso(cur, nombre, 'FALLO',
                                      'cuenta inexistente o desactivada', ip, agente)
                cn.commit()
                raise HTTPException(status_code=401, detail=generico)

            if auth.esta_bloqueado(fila):
                auth.registrar_acceso(cur, nombre, 'FALLO',
                                      'intento sobre cuenta bloqueada', ip, agente)
                cn.commit()
                raise HTTPException(
                    status_code=429,
                    detail="Cuenta bloqueada temporalmente por intentos fallidos. "
                           "Espera unos minutos o pide a un administrador que la desbloquee.")

            if not auth.verificar_clave(credenciales.clave, fila['hash_clave']):
                bloqueada = auth.anotar_fallo(cur, fila, ip, agente)
                cn.commit()
                if bloqueada:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Demasiados intentos. Cuenta bloqueada "
                               f"{auth.MINUTOS_BLOQUEO} minutos.")
                raise HTTPException(status_code=401, detail=generico)

            token = auth.abrir_sesion(cur, fila['id'], ip, agente)
            auth.registrar_acceso(cur, fila['usuario'], 'INGRESO', None, ip, agente)
            cuerpo = {
                "usuario": fila['usuario'],
                "nombre": fila['nombre'],
                "rol": fila['rol'],
                "debe_cambiar_clave": fila['debe_cambiar_clave'],
            }
        cn.commit()

    respuesta = JSONResponse(cuerpo)
    respuesta.set_cookie(
        auth.NOMBRE_COOKIE, token,
        max_age=auth.HORAS_SESION * 3600,
        # httponly: el JS de la pagina no puede leerla, asi que un XSS no se
        # lleva la sesion.
        httponly=True,
        # secure: solo viaja por HTTPS. En el servidor es obligatorio.
        secure=COOKIE_SEGURA,
        # samesite=lax: el navegador no la envia en peticiones que origina otro
        # sitio, que es lo que hace un CSRF.
        samesite="lax",
        path="/",
    )
    return respuesta


@app.post("/logout")
def cerrar_sesion_actual(peticion: Request):
    ip, agente = _contexto(peticion)
    token = peticion.cookies.get(auth.NOMBRE_COOKIE)
    with _conexion() as cn:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            fila = auth.sesion_valida(cur, token) if token else None
            auth.cerrar_sesion(cur, token)
            if fila:
                auth.registrar_acceso(cur, fila['usuario'], 'SALIDA', None, ip, agente)
        cn.commit()
    respuesta = JSONResponse({"ok": True})
    respuesta.delete_cookie(auth.NOMBRE_COOKIE, path="/")
    return respuesta


@app.post("/cambiar-clave")
def cambiar_mi_clave(datos: CambioClave, peticion: Request,
                     usuario=Depends(exigir_sesion)):
    """Cambio de clave por el propio usuario.

    Exige la clave actual aunque ya haya sesion: si alguien deja el equipo
    desbloqueado, no debe poder cambiarle la clave y quedarse con la cuenta.
    """
    if MODO_IDENTIDAD != 'SESION':
        raise HTTPException(status_code=404, detail="No aplica en este modo")

    motivo = auth.validar_clave(datos.clave_nueva)
    if motivo:
        raise HTTPException(status_code=422, detail=motivo)
    if datos.clave_nueva == datos.clave_actual:
        raise HTTPException(status_code=422, detail="La clave nueva debe ser distinta")

    ip, agente = _contexto(peticion)
    token_actual = peticion.cookies.get(auth.NOMBRE_COOKIE)

    with _conexion() as cn:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            fila = auth.buscar_usuario(cur, usuario['usuario'])
            if not auth.verificar_clave(datos.clave_actual, fila['hash_clave']):
                auth.registrar_acceso(cur, fila['usuario'], 'FALLO',
                                      'clave actual incorrecta al cambiarla', ip, agente)
                cn.commit()
                raise HTTPException(status_code=401, detail="La clave actual no coincide")

            auth.cambiar_clave(cur, fila['id'], datos.clave_nueva)
            # Cerrar las demas sesiones: si la clave se cambia porque se filtro,
            # dejar vivas las otras sesiones haria inutil el cambio. La actual
            # se conserva para no expulsar a quien acaba de cambiarla.
            cur.execute("""DELETE FROM sesiones
                            WHERE usuario_id = %s AND token_hash <> %s""",
                        (fila['id'], auth.hash_token(token_actual)))
            otras = cur.rowcount
            auth.registrar_acceso(cur, fila['usuario'], 'CLAVE_CAMBIADA',
                                  'cambiada por el propio usuario', ip, agente)
        cn.commit()
    return {"ok": True, "otras_sesiones_cerradas": otras}


def rechazar_parametros_desconocidos(*permitidos: str):
    """Devuelve una dependencia que responde 422 ante un parametro no previsto.

    FastAPI ignora en silencio lo que no declara, y eso ya causo una confusion
    real: un `?cedula=X` en vez de `?busqueda=X` devolvia la primera pagina sin
    filtrar, con apariencia de haber filtrado — o sea, un resultado que parece
    ser de un guarda y es de otro. En una herramienta de cumplimiento, un
    filtro que falla en silencio es peor que un error.
    """
    permitidos_set = frozenset(permitidos)

    def verificar(peticion: Request):
        sobrantes = sorted(set(peticion.query_params) - permitidos_set)
        if sobrantes:
            raise HTTPException(
                status_code=422,
                detail={
                    "mensaje": "Parametros no reconocidos; se habrian ignorado "
                               "en silencio devolviendo resultados sin filtrar",
                    "no_reconocidos": sobrantes,
                    "validos": sorted(permitidos_set),
                })

    return verificar


def consultar(sql: str, params=(), una=False):
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone() if una else cur.fetchall()
    finally:
        _pool.putconn(conn)


def ejecutar(sql: str, params=(), devolver=False):
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            fila = cur.fetchone() if devolver else None
        conn.commit()
        return fila
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# Dashboard (se sirve desde la misma app: un solo proceso que desplegar)
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(RAIZ, "dashboard.html"))


@app.get("/salud")
def salud():
    """Healthcheck: confirma que la API y la BD responden."""
    fila = consultar("SELECT count(*) AS n FROM anomalias", una=True)
    return {"estado": "ok", "anomalias": fila["n"]}


@app.get("/identidad")
def identidad(peticion: Request):
    """Quien soy. Lo consulta el dashboard al cargar.

    En modo SESION devuelve la cuenta de la sesion, su rol y si todavia debe
    cambiar la clave temporal. El dashboard lo usa para saludar por nombre,
    mostrar el boton de salir, decidir si ensena la pestana de administracion y
    forzar el cambio de clave cuando toca.

    No usa `Depends(exigir_sesion)` a proposito: aqui "no hay sesion" es una
    respuesta valida que el cliente necesita poder leer. Igual esta protegida,
    porque el middleware ya filtro la peticion antes de llegar aqui.
    """
    fila = sesion_actual(peticion)
    if fila:
        return {
            "modo": MODO_IDENTIDAD,
            "usuario": fila['usuario'],
            "nombre": fila['nombre'],
            "rol": fila['rol'],
            "debe_cambiar_clave": fila['debe_cambiar_clave'],
            "es_admin": fila['rol'] == 'ADMIN',
            # Que pestanas mostrar. Sale de la MISMA tabla que autoriza las
            # rutas, asi que el dashboard nunca ofrece algo que el servidor
            # vaya a rechazar.
            "paneles": paneles_de_rol(fila['rol']),
        }
    return {"modo": MODO_IDENTIDAD, "usuario": None, "nombre": None,
            "rol": None, "debe_cambiar_clave": False, "es_admin": False,
            "paneles": list(PANELES)}


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------

@app.get("/periodos", dependencies=[Depends(verificar_token)])
def periodos():
    """Meses con datos, para el selector del dashboard."""
    return consultar("SELECT DISTINCT periodo FROM vw_anomalias ORDER BY periodo DESC")


@app.get("/kpi", dependencies=[Depends(verificar_token)])
def kpi(periodo: Optional[str] = None):
    if periodo:
        return consultar("SELECT * FROM vw_kpi_periodo WHERE periodo = %s", (periodo,), una=True) or {}
    return consultar("SELECT * FROM vw_kpi_periodo ORDER BY periodo DESC")


@app.get("/clientes", dependencies=[Depends(verificar_token)])
def clientes(periodo: Optional[str] = None, limite: int = Query(100, le=500)):
    sql = "SELECT * FROM vw_resumen_cliente"
    params = []
    if periodo:
        sql += " WHERE periodo = %s"
        params.append(periodo)
    sql += " ORDER BY criticas DESC, total_hallazgos DESC LIMIT %s"
    params.append(limite)
    return consultar(sql, params)


@app.get("/anomalias", dependencies=[
    Depends(verificar_token),
    Depends(rechazar_parametros_desconocidos(
        'periodo', 'severidad', 'naturaleza', 'responsable', 'estado', 'regla',
        'cliente_id', 'busqueda', 'pagina', 'por_pagina')),
])
def anomalias(
    periodo: Optional[str] = None,
    severidad: Optional[str] = None,
    naturaleza: Optional[str] = None,
    responsable: Optional[str] = None,
    estado: Optional[str] = None,
    regla: Optional[str] = None,
    cliente_id: Optional[int] = None,
    busqueda: Optional[str] = Query(None, description="Nombre o cedula del guarda"),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=500),
):
    filtros, params = [], []
    for campo, valor in (
        ("periodo", periodo), ("severidad", severidad), ("naturaleza", naturaleza),
        ("responsable", responsable), ("estado", estado), ("regla", regla),
    ):
        if valor:
            filtros.append(f"v.{campo} = %s")
            params.append(valor)
    if cliente_id:
        filtros.append("EXISTS (SELECT 1 FROM vw_anomalia_cliente ac "
                       "WHERE ac.anomalia_id = v.id AND ac.cliente_id = %s)")
        params.append(cliente_id)
    if busqueda:
        filtros.append("(v.guarda_nombre ILIKE %s OR v.guarda_cedula ILIKE %s)")
        params += [f"%{busqueda}%", f"%{busqueda}%"]

    donde = ("WHERE " + " AND ".join(filtros)) if filtros else ""

    total = consultar(f"SELECT count(*) AS n FROM vw_anomalias v {donde}", params, una=True)["n"]
    filas = consultar(
        f"""SELECT * FROM vw_anomalias v {donde}
            ORDER BY v.severidad_orden, v.fecha_referencia, v.guarda_nombre
            LIMIT %s OFFSET %s""",
        params + [por_pagina, (pagina - 1) * por_pagina],
    )
    return {"total": total, "pagina": pagina, "por_pagina": por_pagina, "datos": filas}


@app.get("/anomalias/{anomalia_id}", dependencies=[Depends(verificar_token)])
def anomalia(anomalia_id: int):
    """Detalle completo: la anomalia, los turnos exactos que la originaron y
    el historial de gestion. Es la vista que sustenta la trazabilidad ante
    el MinTrabajo: de la anomalia se puede bajar hasta el turno concreto."""
    cab = consultar("SELECT * FROM vw_anomalias WHERE id = %s", (anomalia_id,), una=True)
    if not cab:
        raise HTTPException(status_code=404, detail="Anomalia no encontrada")
    turnos = consultar(
        """SELECT t.id, t.fecha, t.slot, t.tipo_turno_codigo, t.hora_inicio, t.hora_fin,
                  t.horas_calculadas, p.nombre AS puesto, c.nombre AS cliente
           FROM turnos t
           JOIN puestos p  ON p.id = t.puesto_id
           JOIN clientes c ON c.id = p.cliente_id
           WHERE t.id = ANY(%s) ORDER BY t.fecha, t.hora_inicio""",
        (cab.get("turnos_involucrados") or [],),
    )
    historial = consultar(
        """SELECT estado_anterior, estado_nuevo, nota, usuario, ocurrido_en
           FROM anomalias_historial WHERE anomalia_id = %s ORDER BY ocurrido_en""",
        (anomalia_id,),
    )
    return {**cab, "turnos": turnos, "historial": historial}


@app.get("/nomina", dependencies=[Depends(verificar_token)])
def nomina(periodo: Optional[str] = None, solo_descuadres: bool = False):
    sql = "SELECT * FROM vw_nomina_horas WHERE TRUE"
    params = []
    if periodo:
        sql += " AND periodo = %s"
        params.append(periodo)
    if solo_descuadres:
        sql += " AND abs(descuadre_categorias) > 0.01"
    sql += " ORDER BY abs(descuadre_categorias) DESC, guarda_nombre"
    return consultar(sql, params)


@app.get("/estructural", dependencies=[Depends(verificar_token)])
def estructural(periodo: Optional[str] = None):
    sql = "SELECT * FROM vw_carga_estructural"
    params = []
    if periodo:
        sql += " WHERE periodo = %s"
        params.append(periodo)
    sql += " ORDER BY horas_mes DESC NULLS LAST"
    return consultar(sql, params)


@app.get("/reglas", dependencies=[Depends(verificar_token)])
def reglas():
    """Catalogo vigente: sirve para auditar QUE se aplico y con que umbral."""
    return consultar(
        """SELECT id, codigo, descripcion, severidad_default, naturaleza, responsable,
                  parametros, fundamento_legal, vigente_desde, vigente_hasta
           FROM reglas_anomalia ORDER BY naturaleza, codigo"""
    )


# ---------------------------------------------------------------------------
# Escritura — gestion de anomalias (con auditoria)
# ---------------------------------------------------------------------------

class CambioAnomalia(BaseModel):
    # RESUELTA no esta aqui a proposito: solo la pone el motor, y solo cuando
    # comprueba que la violacion desaparecio del cargue. Si una persona
    # pudiera marcarla a mano, el estado dejaria de significar "verificado
    # contra los datos" y pasaria a significar "alguien dijo que si".
    estado: Literal['ABIERTA', 'EN_REVISION', 'JUSTIFICADA']
    nota: Optional[str] = Field(None, max_length=2000)


@app.patch("/anomalias/{anomalia_id}", dependencies=[Depends(verificar_token)])
def gestionar_anomalia(
    anomalia_id: int,
    cambio: CambioAnomalia,
    quien: str = Depends(resolver_identidad),
):
    """Cambia el estado de una anomalia y deja rastro de quien y por que.

    El historial se escribe SIEMPRE, en la misma transaccion que el cambio:
    si falla el registro de auditoria, no se aplica el cambio. Es la
    propiedad que exige la Circular 0040 — no puede haber una anomalia que
    cambio de estado sin que se sepa quien lo hizo.

    `quien` NO llega como cabecera declarada a proposito: lo resuelve
    `resolver_identidad`, que en modo SESION lo saca de la sesion verificada e
    ignora `X-Usuario`. Asi el cliente no puede decidir quien firma el cambio.
    """
    actual = consultar("SELECT estado FROM anomalias WHERE id = %s", (anomalia_id,), una=True)
    if not actual:
        raise HTTPException(status_code=404, detail="Anomalia no encontrada")

    if cambio.estado == 'JUSTIFICADA' and not (cambio.nota or '').strip():
        raise HTTPException(
            status_code=422,
            detail="Justificar una anomalia exige una nota que explique por que se acepta",
        )

    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """UPDATE anomalias
                      SET estado = %s,
                          nota = COALESCE(%s, nota),
                          actualizado_en = now(),
                          actualizado_por = %s
                    WHERE id = %s
                RETURNING id, estado, nota, actualizado_en, actualizado_por""",
                (cambio.estado, cambio.nota, quien, anomalia_id),
            )
            fila = cur.fetchone()
            cur.execute(
                """INSERT INTO anomalias_historial
                       (anomalia_id, estado_anterior, estado_nuevo, nota, usuario)
                   VALUES (%s, %s, %s, %s, %s)""",
                (anomalia_id, actual['estado'], cambio.estado, cambio.nota, quien),
            )
        conn.commit()
        return fila
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# ---------------------------------------------------------------------------
# Orquestacion — lo que dispara n8n
# ---------------------------------------------------------------------------

class EjecucionPipeline(BaseModel):
    archivo: str = "RepProgramacion.xlsx"
    desde: Optional[date] = None
    hasta: Optional[date] = None


def _correr(comando: list[str]) -> dict:
    proc = subprocess.run(comando, cwd=RAIZ, capture_output=True, text=True, timeout=1800)
    return {
        "comando": " ".join(comando),
        "codigo_salida": proc.returncode,
        "salida": proc.stdout[-4000:],
        "error": proc.stderr[-2000:] if proc.returncode != 0 else None,
    }


@app.post("/pipeline/ejecutar", dependencies=[Depends(verificar_token)])
def ejecutar_pipeline(cfg: EjecucionPipeline):
    """Corre ETL + motor de reglas de punta a punta.

    Ambos pasos son idempotentes, asi que n8n puede reintentar sin miedo a
    duplicar: el ETL hace upsert sobre la llave natural y el motor reconoce
    cada anomalia por su huella y respeta las que ya se gestionaron.
    """
    resultados = [_correr([sys.executable, "etl_normalizacion.py", "--archivo", cfg.archivo])]
    if resultados[0]["codigo_salida"] != 0:
        raise HTTPException(status_code=500, detail={"paso": "etl", **resultados[0]})

    cmd = [sys.executable, "motor_reglas.py"]
    if cfg.desde:
        cmd += ["--desde", cfg.desde.isoformat()]
    if cfg.hasta:
        cmd += ["--hasta", cfg.hasta.isoformat()]
    resultados.append(_correr(cmd))
    if resultados[1]["codigo_salida"] != 0:
        raise HTTPException(status_code=500, detail={"paso": "motor", **resultados[1]})

    return {"estado": "ok", "pasos": resultados}


@app.get("/informe/mensual", dependencies=[Depends(verificar_token)])
def informe_mensual(periodo: str):
    """Payload unico y ya masticado para los correos que arma n8n.

    Trae las tres secciones (gerencia / programador / nomina) en una sola
    llamada, para que el flujo de n8n no tenga que encadenar seis nodos
    HTTP ni recomponer la logica de negocio en expresiones.
    """
    resumen = consultar("SELECT * FROM vw_kpi_periodo WHERE periodo = %s", (periodo,), una=True)
    if not resumen:
        raise HTTPException(status_code=404, detail=f"Sin datos para el periodo {periodo}")

    return {
        "periodo": periodo,
        "resumen": resumen,
        "clientes_criticos": consultar(
            """SELECT cliente_nombre, total_hallazgos, criticas, altas, guardas_afectados
               FROM vw_resumen_cliente WHERE periodo = %s
               ORDER BY criticas DESC, total_hallazgos DESC LIMIT 10""",
            (periodo,),
        ),
        # Ordenado por puesto, no por severidad: el correo es una lista de
        # trabajo para ir a SERPI, y alli se navega cliente -> puesto.
        "programador": consultar(
            """SELECT id, regla, severidad, guarda_nombre, guarda_cedula,
                      fecha_referencia, cliente_principal, puesto_principal,
                      clientes, detalle, estado
               FROM vw_bandeja_programador WHERE periodo = %s
               ORDER BY cliente_principal, puesto_principal, severidad_orden, fecha_referencia""",
            (periodo,),
        ),
        "nomina": consultar(
            """SELECT guarda_nombre, guarda_cedula, cliente, puesto,
                      total_declarado_serpi, suma_categorias, descuadre_categorias
               FROM vw_nomina_horas
               WHERE periodo = %s AND abs(descuadre_categorias) > 0.01
               ORDER BY abs(descuadre_categorias) DESC""",
            (periodo,),
        ),
        "estructural": consultar(
            """SELECT guarda_nombre, guarda_cedula, clientes, horas_mes, hallazgos_estructurales
               FROM vw_carga_estructural WHERE periodo = %s
               ORDER BY horas_mes DESC NULLS LAST LIMIT 25""",
            (periodo,),
        ),
    }


# ---------------------------------------------------------------------------
# Administracion de cuentas (solo ADMIN)
# ---------------------------------------------------------------------------
# Quien tiene rol ADMIN crea las cuentas y se las entrega a las personas que
# deben entrar. Las mismas operaciones estan en `gestionar_usuarios.py`, que es
# lo que rompe el huevo-gallina de la primera cuenta.

class UsuarioNuevo(BaseModel):
    usuario: str = Field(..., max_length=40)
    nombre: str = Field(..., min_length=2, max_length=120)
    rol: Literal['ADMIN', 'PROGRAMADOR', 'NOMINA', 'GERENCIA']
    correo: Optional[str] = Field(None, max_length=200)


class CambioUsuario(BaseModel):
    """Todos opcionales: se aplica solo lo que venga."""
    nombre: Optional[str] = Field(None, min_length=2, max_length=120)
    correo: Optional[str] = Field(None, max_length=200)
    rol: Optional[Literal['ADMIN', 'PROGRAMADOR', 'NOMINA', 'GERENCIA']] = None
    activo: Optional[bool] = None


@app.get("/usuarios")
def listar_usuarios(admin=Depends(exigir_admin)):
    return consultar("SELECT * FROM vw_usuarios")


@app.post("/usuarios", status_code=201)
def crear_usuario_nuevo(datos: UsuarioNuevo, peticion: Request,
                        admin=Depends(exigir_admin)):
    """Crea la cuenta y devuelve una clave temporal, UNA sola vez.

    La clave no se guarda en claro ni se puede volver a consultar: si se
    pierde, se restablece. Es la misma razon por la que no hay un endpoint que
    liste claves — no existe nada que listar.
    """
    nombre_usuario = auth.normalizar_usuario(datos.usuario)
    motivo = auth.validar_usuario(nombre_usuario)
    if motivo:
        raise HTTPException(status_code=422, detail=motivo)

    clave = auth.clave_temporal()
    ip, agente = _contexto(peticion)

    with _conexion() as cn:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if auth.buscar_usuario(cur, nombre_usuario):
                raise HTTPException(
                    status_code=409,
                    detail=f"Ya existe una cuenta '{nombre_usuario}'. Si esta "
                           "desactivada, reactivala en vez de crear otra: dos "
                           "cuentas para una persona parten su historial.")
            fila = auth.crear_usuario(
                cur, nombre_usuario, datos.nombre, datos.rol, clave,
                correo=datos.correo, creado_por=admin['usuario'])
            auth.registrar_acceso(
                cur, nombre_usuario, 'CLAVE_CAMBIADA',
                f"cuenta creada por {admin['usuario']} con rol {datos.rol}", ip, agente)
        cn.commit()

    return {
        "usuario": fila['usuario'],
        "nombre": datos.nombre,
        "rol": fila['rol'],
        "clave_temporal": clave,
        "aviso": ("Entregasela a la persona por un medio seguro. El sistema le "
                  "exigira cambiarla al entrar, asi que deja de servir en cuanto "
                  "la use. Esta clave no se puede volver a consultar."),
    }


@app.patch("/usuarios/{usuario_id}")
def modificar_usuario(usuario_id: int, cambio: CambioUsuario, peticion: Request,
                      admin=Depends(exigir_admin)):
    """Cambia nombre, correo, rol o estado.

    Dos salvaguardas que no son opcionales:

    - **No se puede quitar el ultimo ADMIN activo.** Sin ADMIN nadie puede
      gestionar cuentas, y recuperarlo exige entrar a la base por fuera de la
      aplicacion. Aplica tanto a desactivarlo como a bajarle el rol.
    - **Un ADMIN no puede desactivarse ni degradarse a si mismo** por accidente
      en dos clics. Que lo haga otro ADMIN, que ademas deja rastro cruzado.
    """
    campos, valores = [], []
    for col in ('nombre', 'correo', 'rol', 'activo'):
        valor = getattr(cambio, col)
        if valor is not None:
            campos.append(f"{col} = %s")
            valores.append(valor)
    if not campos:
        raise HTTPException(status_code=422, detail="No hay nada que cambiar")

    ip, agente = _contexto(peticion)

    with _conexion() as cn:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, usuario, rol, activo FROM usuarios WHERE id = %s",
                        (usuario_id,))
            actual = cur.fetchone()
            if not actual:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            pierde_admin = (actual['rol'] == 'ADMIN' and
                            (cambio.activo is False or
                             (cambio.rol is not None and cambio.rol != 'ADMIN')))
            if pierde_admin:
                if actual['id'] == admin['id']:
                    raise HTTPException(
                        status_code=409,
                        detail="No puedes quitarte a ti mismo el rol ADMIN ni "
                               "desactivarte. Pideselo a otro administrador.")
                cur.execute("""SELECT count(*) n FROM usuarios
                                WHERE rol = 'ADMIN' AND activo AND id <> %s""",
                            (usuario_id,))
                if cur.fetchone()['n'] == 0:
                    raise HTTPException(
                        status_code=409,
                        detail="Es el unico ADMIN activo. El sistema quedaria sin "
                               "nadie que pueda gestionar cuentas. Crea otro ADMIN "
                               "primero.")

            valores.append(usuario_id)
            cur.execute(f"""UPDATE usuarios SET {', '.join(campos)}, actualizado_en = now()
                             WHERE id = %s
                         RETURNING id, usuario, nombre, correo, rol, activo""",
                        valores)
            fila = cur.fetchone()

            # Desactivar o cambiar de rol tiene que surtir efecto YA. Con la
            # sesion viva, la persona seguiria entrando o viendo la bandeja de
            # su rol anterior hasta que caducara.
            cerradas = 0
            if cambio.activo is False or cambio.rol is not None:
                cerradas = auth.cerrar_sesiones_de(cur, usuario_id)
                auth.registrar_acceso(
                    cur, fila['usuario'], 'SALIDA',
                    f"sesiones cerradas por cambio hecho por {admin['usuario']}",
                    ip, agente)
        cn.commit()

    return {**fila, "sesiones_cerradas": cerradas}


@app.post("/usuarios/{usuario_id}/clave")
def restablecer_clave(usuario_id: int, peticion: Request, admin=Depends(exigir_admin)):
    """Genera una clave temporal nueva y corta todas las sesiones de esa cuenta.

    Cortar las sesiones no es un extra: si la clave se restablece porque se
    filtro, dejar sesiones vivas haria el restablecimiento inutil.
    """
    clave = auth.clave_temporal()
    ip, agente = _contexto(peticion)

    with _conexion() as cn:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, usuario FROM usuarios WHERE id = %s", (usuario_id,))
            fila = cur.fetchone()
            if not fila:
                raise HTTPException(status_code=404, detail="Usuario no encontrado")

            auth.cambiar_clave(cur, usuario_id, clave)
            cur.execute("UPDATE usuarios SET debe_cambiar_clave = TRUE WHERE id = %s",
                        (usuario_id,))
            cerradas = auth.cerrar_sesiones_de(cur, usuario_id)
            auth.registrar_acceso(cur, fila['usuario'], 'CLAVE_CAMBIADA',
                                  f"restablecida por {admin['usuario']}", ip, agente)
        cn.commit()

    return {"usuario": fila['usuario'], "clave_temporal": clave,
            "sesiones_cerradas": cerradas,
            "aviso": "Entregasela por un medio seguro. No se puede volver a consultar."}


@app.get("/accesos")
def bitacora_accesos(limite: int = Query(50, ge=1, le=500),
                     usuario: Optional[str] = None,
                     admin=Depends(exigir_admin)):
    """Quien entro, quien lo intento y fallo.

    Responde una pregunta distinta a `anomalias_historial`: aquel dice quien
    cambio un dato, este dice quien tuvo acceso al sistema. Ante una fuga de
    PII, la que hay que poder responder es la segunda.
    """
    if usuario:
        return consultar(
            """SELECT usuario, evento, detalle, ip, ocurrido_en FROM accesos
                WHERE usuario = %s ORDER BY ocurrido_en DESC LIMIT %s""",
            (auth.normalizar_usuario(usuario), limite))
    return consultar(
        """SELECT usuario, evento, detalle, ip, ocurrido_en FROM accesos
            ORDER BY ocurrido_en DESC LIMIT %s""", (limite,))
