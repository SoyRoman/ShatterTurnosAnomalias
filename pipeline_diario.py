"""
Corrida diaria desatendida: descarga la malla de SERPI, la normaliza y vuelve a
evaluar las reglas.

    python pipeline_diario.py                      # mes actual + el siguiente
    python pipeline_diario.py --meses 1            # solo el mes actual
    python pipeline_diario.py --desde 2026-09-01 --hasta 2026-10-31
    python pipeline_diario.py --archivo malla.xlsx # omite la descarga (pruebas)

Este es el script que cierra el ciclo central del sistema (ver CLAUDE.md §2):
el usuario corrige la malla en SERPI, a la mañana siguiente esta corrida trae la
malla nueva, el ETL reconcilia (borra lo que ya no está) y el motor marca
`RESUELTA` la anomalía que desapareció. Sin esta corrida el dashboard queda
congelado en la foto del último cargue manual.

Por qué son subprocesos y no imports
------------------------------------
Los tres pasos se invocan por su propia CLI, exactamente igual que a mano. Es a
propósito: así no hay una segunda copia de la lógica de orquestación que se
pueda desincronizar de `descargar_malla_serpi.py`, `etl_normalizacion.py` y
`motor_reglas.py`. Este archivo solo aporta lo que ninguno de los tres tiene
por sí solo: reintentos, bitácora, candado contra corridas solapadas y códigos
de salida distinguibles para que el supervisor (systemd/cron/n8n) sepa QUÉ
falló, no solo que algo falló.

Qué se reintenta y qué no
-------------------------
Solo la descarga. Es la única parte que depende de una red y de un servidor
ajeno y lento, así que un fallo suele ser transitorio. El ETL y el motor corren
contra datos ya en disco y contra la BD: si fallan es por un problema de datos o
de código, y reintentar no arregla nada — solo esconde el error y, en el caso
del ETL, podría repetir escrituras. Fallan una vez y se reporta.

Códigos de salida (contrato con el supervisor)
----------------------------------------------
    0  todo bien
    1  falta configuración / entorno (no se intentó nada)
    2  la descarga falló tras todos los reintentos
    3  el ETL falló
    4  el ETL se ABORTÓ por el umbral de borrado -> REQUIERE REVISIÓN HUMANA,
       no es un fallo técnico: la BD quedó intacta a propósito
    5  el motor de reglas falló
    6  ya había otra corrida en curso (no es un error)

El 4 se separa del 3 porque significan cosas opuestas para quien recibe la
alerta: el 3 es "esto se rompió", el 4 es "esto se negó a borrar 8.000 turnos
por un archivo truncado y necesita que alguien mire el archivo".
"""
import argparse
import calendar
import json
import datetime as dt
import logging
import logging.handlers
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.abspath(__file__))
DIR_LOGS = os.path.join(RAIZ, "logs")
DIR_REPORTES = os.path.join(RAIZ, "Reportes mensuales")

# El candado va en logs/ y no en la raiz del proyecto por dos razones:
#   1. en el contenedor, /app es de root a proposito (el codigo no debe ser
#      escribible por el proceso que lo ejecuta); los unicos directorios que el
#      usuario `turnos` puede escribir son logs/ y Reportes mensuales/
#   2. logs/ es un volumen persistente, asi que el candado sobrevive al
#      contenedor — que es justo la semantica que se quiere: impedir dos
#      corridas EN LA MISMA MAQUINA, no dentro del mismo contenedor
CANDADO = os.path.join(DIR_LOGS, "pipeline_diario.lock")

# `etl_normalizacion.reconciliar` levanta SystemExit con un mensaje que empieza
# así cuando se niega a borrar. Es el único marcador que permite distinguir
# "abortó por seguridad" de "se rompió": ambos salen con returncode 1.
MARCA_ABORTO_UMBRAL = "ABORTADO:"

# El ETL y el motor terminan con una línea «RESUMEN_JSON {...}» que este
# script guarda en el historial de corridas (ver Historial). No va a la
# bitácora: es larga y no es para leerla.
MARCA_RESUMEN = "RESUMEN_JSON "

# Un candado más viejo que esto se considera huérfano (la máquina se reinició a
# mitad de corrida). Holgado a propósito: una descarga de dos meses contra SERPI
# puede tardar 20-30 min y no queremos que una corrida lenta se pise a sí misma.
HORAS_CANDADO_RANCIO = 6

log = logging.getLogger("pipeline")


def configurar_bitacora(verboso):
    os.makedirs(DIR_LOGS, exist_ok=True)
    log.setLevel(logging.DEBUG if verboso else logging.INFO)
    formato = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")

    # A archivo con rotación: esto corre a diario y desatendido durante meses.
    archivo = logging.handlers.RotatingFileHandler(
        os.path.join(DIR_LOGS, "pipeline_diario.log"),
        maxBytes=5 * 1024 * 1024, backupCount=10, encoding="utf-8")
    archivo.setFormatter(formato)
    log.addHandler(archivo)

    # Y a consola, para que systemd/cron lo capture en su propio journal.
    # La consola de Windows arranca en cp1252 y revienta con UnicodeEncodeError
    # en cuanto un mensaje trae un «→» o un «—». Desatendido eso no se ve, así
    # que se fuerza UTF-8 con reemplazo: preferimos un carácter feo a perder una
    # línea de bitácora (o a que el handler se caiga a mitad de corrida).
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    consola = logging.StreamHandler(sys.stdout)
    consola.setFormatter(formato)
    log.addHandler(consola)


def tomar_candado():
    """Evita que dos corridas se solapen. Importante aquí: el ETL borra y el
    motor borra/reinserta anomalías; dos procesos haciendo eso a la vez sobre el
    mismo rango pueden dejar estados incoherentes."""
    if os.path.exists(CANDADO):
        edad_h = (time.time() - os.path.getmtime(CANDADO)) / 3600
        if edad_h < HORAS_CANDADO_RANCIO:
            with open(CANDADO, encoding="utf-8") as f:
                quien = f.read().strip()
            log.warning("Ya hay una corrida en curso (%s, hace %.1f h). Me retiro.",
                        quien, edad_h)
            return False
        log.warning("Candado rancio de hace %.1f h (probablemente un reinicio a "
                    "mitad de corrida); lo reemplazo.", edad_h)

    with open(CANDADO, "w", encoding="utf-8") as f:
        f.write(f"pid={os.getpid()} inicio={dt.datetime.now().isoformat(timespec='seconds')}")
    return True


def soltar_candado():
    try:
        os.remove(CANDADO)
    except OSError:
        pass


def rango_por_defecto(meses):
    """Del primer día del mes actual al último día del mes (actual + meses - 1).

    Por defecto son DOS meses, y no es arbitrario: la malla del mes entrante se
    carga en SERPI entre el 25 y el 27 (CLAUDE.md §1). Con un solo mes, la malla
    nueva sería invisible hasta el día 1 — justo cuando ya está en vigencia y
    corregirla cuesta reprogramar gente. Con dos, aparece el mismo día que la
    cargan y el programador tiene 3-4 días para arreglarla antes de que arranque.
    """
    hoy = dt.date.today()
    primero = hoy.replace(day=1)
    anio, mes = hoy.year, hoy.month + meses - 1
    anio += (mes - 1) // 12
    mes = (mes - 1) % 12 + 1
    ultimo = dt.date(anio, mes, calendar.monthrange(anio, mes)[1])
    return primero.isoformat(), ultimo.isoformat()


def meses_del_rango(desde, hasta):
    """Parte [desde, hasta] en tramos de un mes calendario: [(desde, hasta), ...].

    SERPI NO sabe entregar más de un mes por reporte. Pedido 2026-08-01 →
    2026-09-30 devuelve UNA sola grilla de 31 días con la disposición de agosto
    (el día 1 cae en sábado) pero rotulada «Mes: Septiembre» — el ETL la fecha
    como septiembre y revienta en «2026-09-31» (visto el 2026-10-01). Peor que
    el error sería que no reventara: con dos meses de 30 días cargaría turnos
    en el mes equivocado sin avisar. Por eso se descarga mes por mes.
    """
    d = dt.date.fromisoformat(desde)
    fin = dt.date.fromisoformat(hasta)
    tramos = []
    while d <= fin:
        ultimo_mes = dt.date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        tramos.append((d.isoformat(), min(ultimo_mes, fin).isoformat()))
        d = ultimo_mes + dt.timedelta(days=1)
    return tramos


def ejecutar(etiqueta, argumentos, timeout_s):
    """Corre un paso y vuelca su salida a la bitácora línea por línea.

    Devuelve (returncode, salida_completa). La salida se conserva porque el ETL
    comunica el aborto por umbral en el texto, no en el código de salida.
    """
    log.info("--- %s ---", etiqueta)
    log.debug("  $ %s", " ".join(argumentos))
    t0 = time.monotonic()
    lineas = []
    try:
        proceso = subprocess.Popen(
            argumentos, cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        for linea in proceso.stdout:
            linea = linea.rstrip()
            lineas.append(linea)
            if linea and not linea.startswith(MARCA_RESUMEN):
                log.info("  | %s", linea)
        proceso.wait(timeout=timeout_s)
        rc = proceso.returncode
    except subprocess.TimeoutExpired:
        proceso.kill()
        log.error("%s excedió el timeout de %.0f min y fue terminado.", etiqueta, timeout_s / 60)
        return 124, "\n".join(lineas)
    except OSError as e:
        log.error("No se pudo lanzar %s: %s", etiqueta, e)
        return 127, "\n".join(lineas)

    log.info("--- %s terminó con código %d en %.1f min ---",
             etiqueta, rc, (time.monotonic() - t0) / 60)
    return rc, "\n".join(lineas)


def ruta_de_hoy(desde, hasta):
    """Dónde se guarda la malla de esta corrida.

    Va directo a «Reportes mensuales» con la fecha en el nombre, y ese archivo
    ES el respaldo — no se descarga a un `RepProgramacion.xlsx` fijo para luego
    copiarlo. Dos razones:

    - En el contenedor, `/app` pertenece a root a propósito (el código no debe
      ser escribible por quien lo ejecuta), así que un nombre fijo en la raíz
      falla con `PermissionError`. Los únicos directorios escribibles son
      `logs/` y `Reportes mensuales/`, ambos volúmenes persistentes.
    - Trazabilidad: si en seis meses alguien pregunta por qué el sistema
      reportó una violación, hay que poder mostrar la malla EXACTA que se
      evaluó ese día, no la de hoy, que ya cambió. Un `RepProgramacion.xlsx`
      que se sobreescribe a diario destruye justo esa evidencia.

    Dos corridas el mismo día reescriben el mismo archivo, que es lo correcto:
    un archivo por día, no uno por intento.
    """
    sello = dt.datetime.now().strftime("%Y%m%d")
    return os.path.join(DIR_REPORTES, f"RepProgramacion_{desde}_a_{hasta}_{sello}.xlsx")


def paso_descarga(desde, hasta, ruta_malla, timeout_min, reintentos, espera_s):
    for intento in range(1, reintentos + 1):
        etiqueta = f"1/3 DESCARGA {desde} → {hasta} (intento {intento}/{reintentos})"
        rc, _ = ejecutar(etiqueta, [
            sys.executable, "descargar_malla_serpi.py",
            "--desde", desde, "--hasta", hasta,
            "--salida", ruta_malla,
            "--timeout-min", str(timeout_min),
        ], timeout_s=timeout_min * 60 + 180)

        if rc == 0 and os.path.exists(ruta_malla):
            return True

        # Un .descarga_cruda a medias envenenaría el siguiente intento.
        for basura in (ruta_malla + ".descarga_cruda",):
            if os.path.exists(basura):
                log.warning("Limpiando descarga parcial: %s", basura)
                os.remove(basura)

        if intento < reintentos:
            log.warning("Descarga fallida. Reintento en %d s...", espera_s)
            time.sleep(espera_s)

    log.error("La descarga falló en los %d intentos. SERPI puede estar caído, lento "
              "o haber cambiado el formulario; revisa la bitácora de arriba.", reintentos)
    return False


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--desde", metavar="YYYY-MM-DD",
                    help="Inicio del rango a descargar y evaluar")
    ap.add_argument("--hasta", metavar="YYYY-MM-DD", help="Fin del rango")
    ap.add_argument("--meses", type=int, default=2, metavar="N",
                    help="Si no se dan --desde/--hasta: mes actual y los N-1 "
                         "siguientes (default 2; ver rango_por_defecto)")
    ap.add_argument("--archivo", metavar="RUTA",
                    help="Usa este .xlsx en vez de descargarlo. Para pruebas y "
                         "para re-procesar una malla ya archivada.")
    ap.add_argument("--solo-descarga", action="store_true",
                    help="Descarga y archiva, sin tocar la BD")
    ap.add_argument("--reintentos", type=int, default=3,
                    help="Intentos de descarga antes de rendirse (default 3)")
    ap.add_argument("--espera-reintento", type=int, default=300, metavar="SEG",
                    help="Segundos entre reintentos de descarga (default 300)")
    ap.add_argument("--timeout-min", type=float, default=25.0,
                    help="Minutos máximos por descarga. Más alto que el default de "
                         "descargar_malla_serpi.py porque aquí el rango son dos "
                         "meses completos con los 67 clientes (default 25)")
    ap.add_argument("--umbral-borrado", type=float, default=20.0, metavar="PCT",
                    help="Se pasa tal cual al ETL: máximo %% de turnos que la "
                         "reconciliación puede borrar antes de abortar (default 20)")
    ap.add_argument("--verboso", action="store_true", help="Registra también los comandos")
    args = ap.parse_args()

    configurar_bitacora(args.verboso)
    log.info("=" * 78)
    log.info("CORRIDA DIARIA — %s", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    load_dotenv(os.path.join(RAIZ, ".env"))

    # Fallar rápido y barato: comprobar el entorno ANTES de gastar 20 minutos
    # descargando para morir después en la conexión a la BD.
    faltantes = [v for v in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
                 if not os.environ.get(v)]
    if not args.archivo and not os.environ.get("SERPI_WEB_USER"):
        faltantes.append("SERPI_WEB_USER")
    if not args.archivo and not os.environ.get("SERPI_WEB_PASSWORD"):
        faltantes.append("SERPI_WEB_PASSWORD")
    if faltantes:
        log.error("Faltan variables en .env: %s. No se intentó nada.", ", ".join(faltantes))
        return 1

    if bool(args.desde) != bool(args.hasta):
        log.error("--desde y --hasta van juntos o no van.")
        return 1
    desde, hasta = (args.desde, args.hasta) if args.desde else rango_por_defecto(args.meses)
    log.info("Rango a evaluar: %s → %s", desde, hasta)

    if not tomar_candado():
        return 6

    historial = None if args.solo_descarga else Historial(desde, hasta)
    rc = 1
    try:
        rc = correr_pasos(args, desde, hasta, historial)
        return rc
    finally:
        if historial:
            historial.cerrar(rc)
        soltar_candado()


# Lo que ve quien abre la pestana Historial del dashboard, por codigo de salida.
MENSAJES = {
    0: "Actualización completa",
    1: "Falta configuración o un archivo",
    2: "No se pudo descargar la malla de SERPI",
    3: "Falló la carga de la malla (ETL)",
    4: "Carga detenida por seguridad: la malla nueva borraría demasiados turnos. Requiere revisión",
    5: "Los turnos se actualizaron, pero falló el cálculo de anomalías",
}


class Historial:
    """Registra la corrida en la tabla `corridas` (pestaña Historial).

    Nunca hace fallar la corrida: si la BD no acepta el registro (p.ej. falta
    migracion_005_historial.sql), se avisa en la bitácora y se sigue. Perder el
    historial de un día es mucho menos grave que perder la auditoría del día.
    """

    def __init__(self, desde, hasta):
        self.id = None
        self.meses = {}
        self.anomalias = None
        try:
            version = open(os.path.join(RAIZ, "VERSION"), encoding="utf-8").read().strip()
        except OSError:
            version = None
        try:
            with self._conectar() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO corridas (desde, hasta, version) VALUES (%s, %s, %s) "
                            "RETURNING id", (desde, hasta, version))
                self.id = cur.fetchone()[0]
        except Exception as e:
            log.warning("No se pudo abrir el registro del historial: %s", e)

    @staticmethod
    def _conectar():
        import psycopg2
        return psycopg2.connect(
            host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"], sslmode=os.environ.get("DB_SSLMODE", "prefer"))

    def absorber(self, salida):
        """Toma las líneas RESUMEN_JSON que imprimen el ETL y el motor."""
        for linea in salida.splitlines():
            if not linea.startswith(MARCA_RESUMEN):
                continue
            try:
                datos = json.loads(linea[len(MARCA_RESUMEN):])
            except ValueError:
                continue
            if datos.pop("etapa", None) == "etl":
                self.meses.update(datos.get("cambios", {}))
            else:
                self.anomalias = datos

    def cerrar(self, rc):
        if self.id is None:
            return
        estado = "OK" if rc == 0 else "REVISAR" if rc == 4 else "FALLO"
        try:
            with self._conectar() as conn, conn.cursor() as cur:
                cur.execute(
                    """UPDATE corridas SET fin = now(), estado = %s, codigo_salida = %s,
                              mensaje = %s, meses = %s::jsonb, anomalias = %s::jsonb
                       WHERE id = %s""",
                    (estado, rc, MENSAJES.get(rc, f"Terminó con código {rc}"),
                     json.dumps(self.meses, ensure_ascii=False),
                     json.dumps(self.anomalias, ensure_ascii=False) if self.anomalias else None,
                     self.id))
            log.info("Historial: corrida #%d registrada como %s.", self.id, estado)
        except Exception as e:
            log.warning("No se pudo cerrar el registro del historial: %s", e)


def correr_pasos(args, desde, hasta, historial):
    # ---- 1/3 descarga y 2/3 ETL, un mes a la vez ----------------------
    # (ver meses_del_rango: SERPI no entrega más de un mes por reporte).
    # Un --archivo se carga tal cual: ya es un reporte de SERPI, de un mes.
    if args.archivo:
        ruta = os.path.abspath(args.archivo)
        if not os.path.exists(ruta):
            log.error("No existe el archivo indicado: %s", ruta)
            return 1
        log.info("1/3 DESCARGA omitida: se usa %s", ruta)
        tramos = [(desde, hasta, ruta)]
    else:
        os.makedirs(DIR_REPORTES, exist_ok=True)
        tramos = [(d, h, ruta_de_hoy(d, h)) for d, h in meses_del_rango(desde, hasta)]

    for tramo_desde, tramo_hasta, ruta_malla in tramos:
        if not args.archivo:
            if not paso_descarga(tramo_desde, tramo_hasta, ruta_malla, args.timeout_min,
                                 args.reintentos, args.espera_reintento):
                return 2
            log.info("Malla del día guardada en %s (%.0f KB)",
                     ruta_malla, os.path.getsize(ruta_malla) / 1024)

        if args.solo_descarga:
            continue

        # Lo que baja este pipeline es SIEMPRE el reporte completo (todos los
        # clientes, el mes entero): la reconciliación cubre todos los
        # puestos, para que un puesto renombrado en SERPI no deje turnos
        # huérfanos (ver etl_normalizacion.reconciliar). Un --archivo puede
        # ser un export filtrado, así que ahí se mantiene el alcance por puesto.
        completa = [] if args.archivo else ["--malla-completa"]
        rc, salida = ejecutar(f"2/3 ETL {tramo_desde} → {tramo_hasta} "
                              "(normalización + reconciliación)", [
            sys.executable, "etl_normalizacion.py",
            "--archivo", ruta_malla,
            "--umbral-borrado", str(args.umbral_borrado),
            *completa,
        ], timeout_s=60 * 60)
        if historial:
            historial.absorber(salida)
        if rc != 0:
            if MARCA_ABORTO_UMBRAL in salida:
                log.error("EL ETL SE ABORTÓ POR SEGURIDAD, la BD quedó intacta. "
                          "Esto NO es un fallo del programa: la malla descargada "
                          "difiere tanto de la BD que borrar sería sospechoso. "
                          "Revisa el archivo archivado antes de forzar nada.")
                return 4
            log.error("El ETL falló. La BD puede haber quedado sin los cambios de hoy.")
            return 3

    if args.solo_descarga:
        log.info("--solo-descarga: la BD no se tocó. Fin.")
        return 0

    # ---- 3/3 motor ----------------------------------------------------
    # Se acota al mismo rango que se cargó: `_acotar` aplica ese filtro a
    # borrar_abiertas/marcar_resueltas/reabrir_reincidentes, así que sin él
    # la corrida marcaría estados sobre meses que hoy no se evaluaron.
    rc, salida = ejecutar("3/3 MOTOR DE REGLAS", [
        sys.executable, "motor_reglas.py", "--desde", desde, "--hasta", hasta,
    ], timeout_s=60 * 60)
    if historial:
        historial.absorber(salida)
    if rc != 0:
        log.error("El motor falló. Los turnos SÍ se actualizaron, pero las "
                  "anomalías quedaron con la foto de ayer. Se puede reintentar "
                  "solo el motor: python motor_reglas.py --desde %s --hasta %s",
                  desde, hasta)
        return 5

    log.info("CORRIDA COMPLETA sin errores.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
