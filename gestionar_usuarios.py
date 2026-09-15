"""
Administracion de cuentas desde la linea de comandos.

    python gestionar_usuarios.py crear --usuario jperez --nombre "Nombre Apellido" --rol ADMIN
    python gestionar_usuarios.py listar
    python gestionar_usuarios.py clave --usuario jperez          # clave temporal nueva
    python gestionar_usuarios.py rol --usuario ana --rol NOMINA
    python gestionar_usuarios.py desactivar --usuario ana
    python gestionar_usuarios.py activar --usuario ana
    python gestionar_usuarios.py sesiones                        # quien esta conectado
    python gestionar_usuarios.py accesos --limite 30             # bitacora de ingresos

Existe por una razon concreta: **la primera cuenta no se puede crear desde el
dashboard**, porque para entrar al dashboard hace falta una cuenta. Este script
rompe ese huevo-gallina. Despues del primer ADMIN, la gestion normal se hace
desde la pantalla de administracion; esto queda como red de seguridad para
cuando alguien se bloquee a si mismo o pierda la clave del unico ADMIN.

Las claves NUNCA se pasan por argumento: el historial del shell las guarda, y
quedan visibles en la lista de procesos mientras el comando corre. O se pide por
consola sin eco, o el script genera una temporal y la imprime una sola vez.
"""
import argparse
import getpass
import os
import sys

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

import autenticacion as auth

RAIZ = os.path.dirname(os.path.abspath(__file__))


def conectar():
    load_dotenv(os.path.join(RAIZ, '.env'))
    faltan = [v for v in ('DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD')
              if not os.environ.get(v)]
    if faltan:
        sys.exit(f"Faltan variables en .env: {', '.join(faltan)}")
    return psycopg2.connect(
        host=os.environ['DB_HOST'], port=os.environ.get('DB_PORT', '5432'),
        dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        sslmode=os.environ.get('DB_SSLMODE', 'prefer'))


def quien_ejecuta():
    """Para `creado_por`. Es trazabilidad, no autenticacion: quien corre este
    script ya tiene acceso a la base."""
    return f"cli:{getpass.getuser()}"


def pedir_clave_interactiva():
    """Pide la clave dos veces, sin eco. Devuelve None si el usuario prefiere
    que el script genere una temporal."""
    print("Deja ambas vacias para que el sistema genere una clave temporal.")
    a = getpass.getpass("  Clave: ")
    if not a:
        return None
    b = getpass.getpass("  Repetir: ")
    if a != b:
        sys.exit("Las claves no coinciden.")
    motivo = auth.validar_clave(a)
    if motivo:
        sys.exit(motivo)
    return a


def cmd_crear(cur, args):
    motivo = auth.validar_usuario(auth.normalizar_usuario(args.usuario))
    if motivo:
        sys.exit(motivo)
    if auth.buscar_usuario(cur, args.usuario):
        sys.exit(f"Ya existe el usuario '{auth.normalizar_usuario(args.usuario)}'. "
                 "Para reactivarlo:  python gestionar_usuarios.py activar --usuario "
                 f"{auth.normalizar_usuario(args.usuario)}")

    clave = None if args.sin_preguntar else pedir_clave_interactiva()
    temporal = clave is None
    if temporal:
        clave = auth.clave_temporal()

    fila = auth.crear_usuario(
        cur, args.usuario, args.nombre, args.rol, clave,
        correo=args.correo, creado_por=quien_ejecuta(),
        # Si el administrador eligio la clave, la persona igual debe cambiarla:
        # una clave que un tercero conoce no sirve para atribuir nada.
        debe_cambiar=True)
    auth.registrar_acceso(cur, fila['usuario'], 'CLAVE_CAMBIADA',
                          f"cuenta creada por {quien_ejecuta()} con rol {args.rol}")

    print(f"\nCreado: {fila['usuario']}  ({args.nombre}, rol {fila['rol']})")
    print(f"  Clave {'temporal' if temporal else 'asignada'}: {clave}")
    print("\n  Entregasela a la persona por un medio seguro. El sistema le va a")
    print("  exigir cambiarla en el primer ingreso, asi que esta clave deja de")
    print("  servir en cuanto la use. No la reenvies por correo ni chat.")


def cmd_listar(cur, args):
    cur.execute("SELECT * FROM vw_usuarios")
    filas = cur.fetchall()
    if not filas:
        print("No hay usuarios. Crea el primer ADMIN:")
        print("  python gestionar_usuarios.py crear --usuario <tu> --nombre \"Tu Nombre\" --rol ADMIN")
        return
    print(f"{'usuario':<20}{'nombre':<28}{'rol':<13}{'estado':<24}{'ultimo ingreso':<20}sesiones")
    print("-" * 115)
    for f in filas:
        estado = 'activo' if f['activo'] else 'DESACTIVADO'
        if f['bloqueado']:
            estado = f"BLOQUEADO hasta {f['bloqueado_hasta']:%H:%M}"
        elif f['activo'] and f['debe_cambiar_clave']:
            estado = 'activo (clave temporal)'
        ultimo = f"{f['ultimo_ingreso']:%Y-%m-%d %H:%M}" if f['ultimo_ingreso'] else 'nunca'
        print(f"{f['usuario']:<20}{f['nombre'][:27]:<28}{f['rol']:<13}"
              f"{estado:<24}{ultimo:<20}{f['sesiones_activas']}")


def _exigir(cur, usuario):
    fila = auth.buscar_usuario(cur, usuario)
    if not fila:
        sys.exit(f"No existe el usuario '{auth.normalizar_usuario(usuario)}'.")
    return fila


def cmd_clave(cur, args):
    fila = _exigir(cur, args.usuario)
    clave = None if args.sin_preguntar else pedir_clave_interactiva()
    temporal = clave is None
    if temporal:
        clave = auth.clave_temporal()

    auth.cambiar_clave(cur, fila['id'], clave)
    cur.execute("UPDATE usuarios SET debe_cambiar_clave = TRUE WHERE id = %s", (fila['id'],))
    # Cortar sus sesiones: si la clave se restablece porque se filtro, dejar
    # sesiones vivas haria inutil el cambio.
    n = auth.cerrar_sesiones_de(cur, fila['id'])
    auth.registrar_acceso(cur, fila['usuario'], 'CLAVE_CAMBIADA',
                          f"restablecida por {quien_ejecuta()}")
    print(f"Clave restablecida para {fila['usuario']}: {clave}")
    print(f"  {n} sesion(es) cerrada(s). Debera cambiarla al entrar.")


def cmd_rol(cur, args):
    fila = _exigir(cur, args.usuario)
    if fila['rol'] == args.rol:
        print(f"{fila['usuario']} ya tiene rol {args.rol}.")
        return
    cur.execute("UPDATE usuarios SET rol = %s, actualizado_en = now() WHERE id = %s",
                (args.rol, fila['id']))
    # El rol decide que ve la persona, y eso se resuelve al abrir la sesion.
    # Sin cortar las sesiones, seguiria viendo lo de su rol anterior.
    n = auth.cerrar_sesiones_de(cur, fila['id'])
    print(f"{fila['usuario']}: {fila['rol']} -> {args.rol}. {n} sesion(es) cerrada(s).")


def cmd_desactivar(cur, args):
    fila = _exigir(cur, args.usuario)
    cur.execute("SELECT count(*) n FROM usuarios WHERE rol='ADMIN' AND activo AND id <> %s",
                (fila['id'],))
    if fila['rol'] == 'ADMIN' and cur.fetchone()['n'] == 0:
        sys.exit("Es el unico ADMIN activo. Desactivarlo dejaria el sistema sin\n"
                 "nadie que pueda gestionar cuentas. Crea otro ADMIN primero.")
    cur.execute("UPDATE usuarios SET activo = FALSE, actualizado_en = now() WHERE id = %s",
                (fila['id'],))
    n = auth.cerrar_sesiones_de(cur, fila['id'])
    auth.registrar_acceso(cur, fila['usuario'], 'SALIDA',
                          f"cuenta desactivada por {quien_ejecuta()}")
    print(f"{fila['usuario']} desactivado. {n} sesion(es) cerrada(s).")
    print("  No se borra: el historial de auditoria seguiria apuntando a esta")
    print("  cuenta, y hay que poder responder quien hizo cada cambio.")


def cmd_activar(cur, args):
    fila = _exigir(cur, args.usuario)
    cur.execute("""UPDATE usuarios SET activo = TRUE, intentos_fallidos = 0,
                          bloqueado_hasta = NULL, actualizado_en = now()
                    WHERE id = %s""", (fila['id'],))
    print(f"{fila['usuario']} activado y desbloqueado.")


def cmd_sesiones(cur, args):
    cur.execute("""SELECT u.usuario, u.nombre, s.creada_en, s.ultima_vez, s.expira_en, s.ip
                     FROM sesiones s JOIN usuarios u ON u.id = s.usuario_id
                    WHERE s.expira_en > now()
                    ORDER BY s.ultima_vez DESC""")
    filas = cur.fetchall()
    if not filas:
        print("No hay sesiones activas.")
        return
    print(f"{'usuario':<20}{'desde':<18}{'ultima actividad':<18}{'expira':<18}ip")
    print("-" * 90)
    for f in filas:
        print(f"{f['usuario']:<20}{f['creada_en']:%Y-%m-%d %H:%M}  "
              f"{f['ultima_vez']:%Y-%m-%d %H:%M}  {f['expira_en']:%Y-%m-%d %H:%M}  "
              f"{f['ip'] or ''}")


def cmd_accesos(cur, args):
    cur.execute("""SELECT usuario, evento, detalle, ip, ocurrido_en
                     FROM accesos ORDER BY ocurrido_en DESC LIMIT %s""", (args.limite,))
    filas = cur.fetchall()
    if not filas:
        print("Sin registros de acceso todavia.")
        return
    print(f"{'cuando':<18}{'usuario':<20}{'evento':<18}detalle")
    print("-" * 95)
    for f in filas:
        print(f"{f['ocurrido_en']:%Y-%m-%d %H:%M}  {(f['usuario'] or '-'):<20}"
              f"{f['evento']:<18}{f['detalle'] or ''}")


def cmd_purgar(cur, args):
    n = auth.purgar_sesiones_vencidas(cur)
    print(f"{n} sesion(es) vencida(s) eliminada(s).")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='comando', required=True)

    def con_usuario(p):
        p.add_argument('--usuario', required=True)
        return p

    p = sub.add_parser('crear', help='Crear una cuenta')
    p.add_argument('--usuario', required=True, help='minusculas, sin espacios')
    p.add_argument('--nombre', required=True, help='Nombre real, sale en el historial')
    p.add_argument('--rol', required=True, choices=auth.ROLES)
    p.add_argument('--correo')
    p.add_argument('--sin-preguntar', action='store_true',
                   help='No pedir clave: genera una temporal y la imprime')
    p.set_defaults(fn=cmd_crear)

    sub.add_parser('listar', help='Ver todas las cuentas').set_defaults(fn=cmd_listar)

    p = con_usuario(sub.add_parser('clave', help='Restablecer la clave'))
    p.add_argument('--sin-preguntar', action='store_true')
    p.set_defaults(fn=cmd_clave)

    p = con_usuario(sub.add_parser('rol', help='Cambiar el rol'))
    p.add_argument('--rol', required=True, choices=auth.ROLES)
    p.set_defaults(fn=cmd_rol)

    con_usuario(sub.add_parser('desactivar', help='Quitar el acceso')).set_defaults(fn=cmd_desactivar)
    con_usuario(sub.add_parser('activar', help='Devolver el acceso')).set_defaults(fn=cmd_activar)
    sub.add_parser('sesiones', help='Quien esta conectado').set_defaults(fn=cmd_sesiones)

    p = sub.add_parser('accesos', help='Bitacora de ingresos y fallos')
    p.add_argument('--limite', type=int, default=20)
    p.set_defaults(fn=cmd_accesos)

    sub.add_parser('purgar', help='Borrar sesiones vencidas').set_defaults(fn=cmd_purgar)

    args = ap.parse_args()

    cn = conectar()
    try:
        with cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            args.fn(cur, args)
        cn.commit()
    except Exception:
        cn.rollback()
        raise
    finally:
        cn.close()


if __name__ == '__main__':
    main()
