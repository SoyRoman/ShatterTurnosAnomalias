"""
Autenticacion propia del sistema: usuarios, claves y sesiones.

Toda la logica de seguridad vive aqui y no repartida por `api.py`, para que
haya UN solo sitio donde auditar como se valida una clave, como se crea una
sesion y cuando se corta el acceso.

Decision de diseno (septiembre 2026, explicita del usuario): la autenticacion
es propia y no delega en ningun proveedor externo. El administrador crea las
cuentas y se las entrega a las personas que deben entrar.

Las tres piezas
---------------
1. **Claves con bcrypt.** Nunca se guarda la clave, solo su hash. bcrypt es
   lento a proposito: hace inviable probar millones de claves contra un volcado
   de la base.
2. **Sesiones en servidor** (tabla `sesiones`), no un JWT autocontenido. Un JWT
   firmado vale hasta que expira y no se puede anular; con sesiones en tabla,
   desactivar una cuenta corta el acceso en la siguiente peticion. Aqui hay PII
   de 404 trabajadores: poder cortar el acceso *ya* vale mas que ahorrarse una
   consulta.
3. **Del token solo se guarda el SHA-256.** Mismo criterio que con las claves:
   un volcado de `sesiones` no debe entregar sesiones vivas listas para usar.
   El token en claro solo existe en la cookie del navegador.
"""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt

# Duracion de la sesion. 12 horas cubre una jornada completa sin obligar a
# reingresar a media tarde, y no deja una sesion viva toda la noche en un
# equipo compartido.
HORAS_SESION = int(os.environ.get('HORAS_SESION', '12'))

# Freno a la fuerza bruta: tras N fallos, la cuenta queda bloqueada M minutos.
# Se cuenta POR CUENTA y no por IP porque en la oficina todos salen por la
# misma NAT: bloquear por IP dejaria fuera a todo el mundo por culpa de uno.
MAX_INTENTOS = int(os.environ.get('MAX_INTENTOS_LOGIN', '5'))
MINUTOS_BLOQUEO = int(os.environ.get('MINUTOS_BLOQUEO_LOGIN', '15'))

LONGITUD_MINIMA_CLAVE = 10

ROLES = ('ADMIN', 'PROGRAMADOR', 'NOMINA', 'GERENCIA')

NOMBRE_COOKIE = 'sesion_turnos'

RE_USUARIO = re.compile(r'^[a-z0-9._-]{3,40}$')


# ---------------------------------------------------------------------------
# Claves
# ---------------------------------------------------------------------------

def hashear_clave(clave: str) -> str:
    """bcrypt con sal aleatoria por clave.

    La sal va dentro del hash resultante, asi que no hay que guardarla aparte.
    Que sea distinta por usuario impide precomputar: romper una clave no ayuda
    en nada con la siguiente.
    """
    return bcrypt.hashpw(clave.encode('utf-8'), bcrypt.gensalt()).decode('ascii')


def verificar_clave(clave: str, hash_guardado: str) -> bool:
    """Comparacion en tiempo constante (la hace bcrypt).

    Nunca compares hashes con `==`: el tiempo que tarda un `==` en fallar
    depende de cuantos caracteres coincidieron, y eso filtra informacion.
    """
    try:
        return bcrypt.checkpw(clave.encode('utf-8'), hash_guardado.encode('ascii'))
    except (ValueError, TypeError):
        # Hash corrupto o de otro formato: se trata como clave incorrecta, no
        # como error del servidor. Que no explote no significa que deje pasar.
        return False


def validar_clave(clave: str) -> str | None:
    """Devuelve el motivo del rechazo, o None si la clave sirve.

    El criterio es LONGITUD antes que composicion. Exigir mayuscula + numero +
    simbolo produce `Shatter2026!` en todas las cuentas; exigir 10 caracteres
    deja pasar frases largas, que son mas faciles de recordar y mas dificiles
    de adivinar.
    """
    if len(clave) < LONGITUD_MINIMA_CLAVE:
        return f"La clave debe tener al menos {LONGITUD_MINIMA_CLAVE} caracteres"
    if clave.strip() != clave:
        return "La clave no puede empezar ni terminar en espacio"
    comunes = {'123456789', '1234567890', 'contrasena', 'password', 'qwertyuiop',
               'shatter123', 'administrador'}
    if clave.lower() in comunes:
        return "Esa clave es de las primeras que se prueban; elige otra"
    return None


def validar_usuario(usuario: str) -> str | None:
    if not RE_USUARIO.match(usuario or ''):
        return ("El usuario debe tener entre 3 y 40 caracteres y solo puede "
                "llevar minusculas, numeros, punto, guion y guion bajo")
    return None


def normalizar_usuario(usuario: str) -> str:
    """Minusculas y sin espacios alrededor.

    Sin esto, "Ana" y "ana" serian dos cuentas distintas para la misma
    persona, y el historial de auditoria quedaria repartido entre ambas.
    """
    return (usuario or '').strip().lower()


def clave_temporal() -> str:
    """Clave inicial que el administrador le entrega a la persona.

    Legible al dictarla (sin caracteres que se confundan) pero con suficiente
    entropia para que no se pueda adivinar mientras la persona la cambia.
    """
    alfabeto = 'abcdefghijkmnopqrstuvwxyz23456789'   # sin l, 0, 1, o
    return '-'.join(''.join(secrets.choice(alfabeto) for _ in range(4))
                    for _ in range(3))


# ---------------------------------------------------------------------------
# Sesiones
# ---------------------------------------------------------------------------

def nuevo_token() -> str:
    """Token de sesion. 32 bytes de `secrets` = 256 bits de entropia real.

    `secrets` y no `random`: el segundo es predecible si se conoce su estado, y
    aqui un token adivinable es una sesion ajena.
    """
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 y no bcrypt, a proposito.

    bcrypt es lento por diseno, y esto se verifica en CADA peticion. Es seguro
    usar un hash rapido aqui porque el token ya tiene 256 bits de entropia: no
    hay nada que adivinar por fuerza bruta, a diferencia de una clave humana.
    """
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def ahora():
    return datetime.now(timezone.utc)


def vencimiento_sesion():
    return ahora() + timedelta(hours=HORAS_SESION)


# ---------------------------------------------------------------------------
# Operaciones contra la base
# ---------------------------------------------------------------------------
# Reciben un cursor ya abierto: quien llama decide la transaccion. Asi el
# registro en `accesos` se confirma junto con el cambio que lo origino.

def buscar_usuario(cur, usuario: str):
    cur.execute("""SELECT id, usuario, nombre, correo, hash_clave, rol, activo,
                          debe_cambiar_clave, intentos_fallidos, bloqueado_hasta
                     FROM usuarios WHERE usuario = %s""", (normalizar_usuario(usuario),))
    return cur.fetchone()


def registrar_acceso(cur, usuario, evento, detalle=None, ip=None, agente=None):
    cur.execute("""INSERT INTO accesos (usuario, evento, detalle, ip, agente)
                   VALUES (%s, %s, %s, %s, %s)""",
                (usuario, evento, detalle, ip, (agente or '')[:300] or None))


def esta_bloqueado(fila) -> bool:
    return bool(fila['bloqueado_hasta']) and fila['bloqueado_hasta'] > ahora()


def anotar_fallo(cur, fila, ip=None, agente=None):
    """Suma un intento fallido y bloquea la cuenta si se pasa del limite."""
    intentos = (fila['intentos_fallidos'] or 0) + 1
    if intentos >= MAX_INTENTOS:
        cur.execute("""UPDATE usuarios
                          SET intentos_fallidos = 0,
                              bloqueado_hasta = now() + make_interval(mins => %s)
                        WHERE id = %s""", (MINUTOS_BLOQUEO, fila['id']))
        registrar_acceso(cur, fila['usuario'], 'BLOQUEO',
                         f"{MAX_INTENTOS} intentos fallidos; bloqueada {MINUTOS_BLOQUEO} min",
                         ip, agente)
        return True
    cur.execute("UPDATE usuarios SET intentos_fallidos = %s WHERE id = %s",
                (intentos, fila['id']))
    registrar_acceso(cur, fila['usuario'], 'FALLO',
                     f"intento {intentos} de {MAX_INTENTOS}", ip, agente)
    return False


def abrir_sesion(cur, usuario_id, ip=None, agente=None):
    """Crea la sesion y devuelve el token EN CLARO (la base solo guarda su hash)."""
    token = nuevo_token()
    cur.execute("""INSERT INTO sesiones (token_hash, usuario_id, expira_en, ip, agente)
                   VALUES (%s, %s, %s, %s, %s)""",
                (hash_token(token), usuario_id, vencimiento_sesion(), ip,
                 (agente or '')[:300] or None))
    cur.execute("""UPDATE usuarios
                      SET ultimo_ingreso = now(), intentos_fallidos = 0,
                          bloqueado_hasta = NULL
                    WHERE id = %s""", (usuario_id,))
    return token


def sesion_valida(cur, token):
    """Devuelve el usuario de la sesion, o None.

    Comprueba de una vez que la sesion no expiro Y que la cuenta sigue activa.
    Lo segundo es lo que hace que desactivar un usuario corte el acceso de
    inmediato en vez de esperar a que caduque su sesion.
    """
    if not token:
        return None
    cur.execute("""SELECT u.id, u.usuario, u.nombre, u.correo, u.rol,
                          u.debe_cambiar_clave, s.expira_en
                     FROM sesiones s
                     JOIN usuarios u ON u.id = s.usuario_id
                    WHERE s.token_hash = %s
                      AND s.expira_en > now()
                      AND u.activo""", (hash_token(token),))
    fila = cur.fetchone()
    if fila:
        # Marca de actividad. Sirve para saber si una sesion sigue en uso; no
        # extiende el vencimiento, que es absoluto a proposito: una sesion
        # renovada indefinidamente nunca obliga a reautenticarse.
        cur.execute("UPDATE sesiones SET ultima_vez = now() WHERE token_hash = %s",
                    (hash_token(token),))
    return fila


def cerrar_sesion(cur, token):
    if token:
        cur.execute("DELETE FROM sesiones WHERE token_hash = %s", (hash_token(token),))
        return cur.rowcount
    return 0


def cerrar_sesiones_de(cur, usuario_id):
    """Corta TODAS las sesiones de un usuario.

    Se usa al desactivar una cuenta, al cambiar su clave y al cambiarle el rol:
    en los tres casos dejar sesiones vivas contradiria la accion que se acaba de
    tomar.
    """
    cur.execute("DELETE FROM sesiones WHERE usuario_id = %s", (usuario_id,))
    return cur.rowcount


def purgar_sesiones_vencidas(cur):
    cur.execute("DELETE FROM sesiones WHERE expira_en <= now()")
    return cur.rowcount


def cambiar_clave(cur, usuario_id, clave_nueva):
    cur.execute("""UPDATE usuarios
                      SET hash_clave = %s, debe_cambiar_clave = FALSE,
                          actualizado_en = now(), intentos_fallidos = 0,
                          bloqueado_hasta = NULL
                    WHERE id = %s
                RETURNING usuario""",
                (hashear_clave(clave_nueva), usuario_id))
    return cur.fetchone()


def crear_usuario(cur, usuario, nombre, rol, clave, correo=None, creado_por=None,
                  debe_cambiar=True):
    cur.execute("""INSERT INTO usuarios
                       (usuario, nombre, correo, hash_clave, rol, creado_por,
                        debe_cambiar_clave)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, usuario, nombre, rol, activo, debe_cambiar_clave""",
                (normalizar_usuario(usuario), nombre.strip(), correo,
                 hashear_clave(clave), rol, creado_por, debe_cambiar))
    return cur.fetchone()


def hay_algun_admin(cur) -> bool:
    cur.execute("SELECT 1 FROM usuarios WHERE rol = 'ADMIN' AND activo LIMIT 1")
    return cur.fetchone() is not None
