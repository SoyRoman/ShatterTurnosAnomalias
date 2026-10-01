"""
Descarga automatica de la malla de turnos desde SERPI (RPA con Playwright).

    python descargar_malla_serpi.py                       # mes actual completo
    python descargar_malla_serpi.py --desde 2026-09-01 --hasta 2026-09-30

Mientras el modulo de turnos de SERPI no tenga API propia (ver CLAUDE.md), esta
es la unica forma de automatizar la descarga: inicia sesion en la interfaz web
de SERPI igual que un usuario, navega al reporte de programacion y descarga el
Excel. Pensado para correr a diario via Task Scheduler / cron, dejando el
archivo listo para que el flujo de n8n (Local File Trigger) dispare el ETL y
el motor de reglas automaticamente.

Credenciales SOLO por variable de entorno (.env, fuera de git):
    SERPI_WEB_USER=...
    SERPI_WEB_PASSWORD=...

OJO: el archivo que entrega SERPI es un .xls binario antiguo (formato OLE2,
Excel 97-2003), no un .xlsx moderno. openpyxl (el que usa etl_normalizacion.py)
solo lee .xlsx, asi que este script SIEMPRE convierte el resultado con
xlrd + openpyxl antes de guardarlo. No lo cambies a una copia directa del
archivo descargado o el ETL fallara al abrirlo.

El reporte es lento en el servidor de SERPI (~2-3 min incluso para un solo
dia, mas para un mes completo con los 67 clientes) - el timeout por defecto
es generoso a proposito, no lo bajes sin necesidad.
"""
import argparse
import os
import sys
from datetime import date, timedelta

import openpyxl
import xlrd
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

RAIZ = os.path.dirname(os.path.abspath(__file__))
URL_LOGIN = "https://shatter.serpi.com.co/"
URL_FORMULARIO = "https://shatter.serpi.com.co/Turnos/ReporteProgramacion/Programacion"
URL_BASE_DESCARGA = "https://shatter.serpi.com.co/Turnos/ReporteProgramacion/RepProgramacion"

FIRMA_OLE2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FIRMA_ZIP = b"PK\x03\x04"


def primer_y_ultimo_dia_mes_actual():
    hoy = date.today()
    primero = hoy.replace(day=1)
    if hoy.month == 12:
        ultimo = date(hoy.year, 12, 31)
    else:
        ultimo = date(hoy.year, hoy.month + 1, 1) - timedelta(days=1)
    return primero, ultimo


def descargar_xls_crudo(usuario, clave, fecha_desde, fecha_hasta, ruta_salida, timeout_ms, headless):
    url_descarga = (
        f"{URL_BASE_DESCARGA}?gdtproyectoid=&gdtproyectoactid=&grhcontratoid="
        f"&fi1={fecha_desde}&ff1={fecha_hasta}"
        "&id=Excel&tipotur=ambos&hiddenlogo=0&paginaxpuesto=1"
    )

    with sync_playwright() as p:
        # NAVEGADOR_CANAL=chrome|msedge usa el navegador ya instalado en el
        # equipo, para cuando `playwright install chromium` no puede descargar
        # (proxy/firewall). Vacio = el Chromium propio de Playwright.
        canal = os.environ.get("NAVEGADOR_CANAL") or None
        navegador = p.chromium.launch(headless=headless, channel=canal)
        contexto = navegador.new_context(accept_downloads=True)
        pagina = contexto.new_page()

        print(f"[1/4] Iniciando sesion en SERPI como {usuario}...")
        pagina.goto(URL_LOGIN, wait_until="networkidle", timeout=30000)
        pagina.fill("#UserName", usuario)
        pagina.fill("#Password", clave)
        pagina.click("button[type=submit]")
        pagina.wait_for_load_state("networkidle", timeout=30000)
        if "Account/Login" in pagina.url:
            navegador.close()
            raise SystemExit("Login rechazado por SERPI: revisa SERPI_WEB_USER/SERPI_WEB_PASSWORD en .env")

        print("[2/4] Abriendo el formulario de Programaciones (fija el contexto de sesion)...")
        pagina.goto(URL_FORMULARIO, wait_until="networkidle", timeout=30000)

        print(f"[3/4] Solicitando el reporte {fecha_desde} -> {fecha_hasta} "
              f"(puede tardar varios minutos, el servidor de SERPI es lento aqui)...")
        with pagina.expect_download(timeout=timeout_ms) as info_descarga:
            try:
                pagina.goto(url_descarga, wait_until="commit", timeout=timeout_ms)
            except Exception:
                pass  # la navegacion se aborta a favor de la descarga; es lo esperado
        descarga = info_descarga.value
        descarga.save_as(ruta_salida)

        navegador.close()
    print(f"[4/4] Descarga cruda guardada en {ruta_salida}")


def convertir_a_xlsx(ruta_cruda, ruta_xlsx):
    """SERPI entrega .xls (OLE2 binario). openpyxl solo lee .xlsx (OOXML), asi
    que convertimos hoja por hoja y celda por celda antes de entregarselo al
    ETL. Si SERPI alguna vez empieza a entregar OOXML real, esto detecta la
    firma y copia el archivo tal cual, sin reconvertir de mas."""
    with open(ruta_cruda, "rb") as f:
        firma = f.read(8)

    if firma.startswith(FIRMA_ZIP):
        print("El archivo ya es .xlsx (OOXML) real, se copia sin conversion.")
        os.replace(ruta_cruda, ruta_xlsx)
        return

    if not firma.startswith(FIRMA_OLE2):
        raise SystemExit(f"Formato de archivo no reconocido (firma {firma!r}); "
                          "revisa manualmente lo que descargo SERPI.")

    print("Convirtiendo de .xls (OLE2) a .xlsx (OOXML) para que openpyxl pueda leerlo...")
    libro_viejo = xlrd.open_workbook(ruta_cruda)
    libro_nuevo = openpyxl.Workbook()
    libro_nuevo.remove(libro_nuevo.active)

    for nombre_hoja in libro_viejo.sheet_names():
        hoja_vieja = libro_viejo.sheet_by_name(nombre_hoja)
        hoja_nueva = libro_nuevo.create_sheet(title=nombre_hoja[:31])
        for r in range(hoja_vieja.nrows):
            for c in range(hoja_vieja.ncols):
                valor = hoja_vieja.cell_value(r, c)
                if valor == "":
                    continue
                hoja_nueva.cell(row=r + 1, column=c + 1, value=valor)

    libro_nuevo.save(ruta_xlsx)
    os.remove(ruta_cruda)
    print(f"Convertido: {len(libro_viejo.sheet_names())} hojas -> {ruta_xlsx}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--desde", help="YYYY-MM-DD (por defecto: primer dia del mes actual)")
    ap.add_argument("--hasta", help="YYYY-MM-DD (por defecto: ultimo dia del mes actual)")
    ap.add_argument("--salida", default=os.path.join(RAIZ, "RepProgramacion.xlsx"),
                     help="Ruta final del .xlsx (por defecto la raiz del proyecto, "
                          "la misma que vigila n8n)")
    ap.add_argument("--timeout-min", type=float, default=10.0,
                     help="Minutos maximos de espera por la descarga (default 10)")
    ap.add_argument("--visible", action="store_true",
                     help="Corre el navegador visible (para depurar); por defecto headless")
    args = ap.parse_args()

    load_dotenv(os.path.join(RAIZ, ".env"))
    usuario = os.environ.get("SERPI_WEB_USER")
    clave = os.environ.get("SERPI_WEB_PASSWORD")
    if not usuario or not clave:
        sys.exit("Faltan SERPI_WEB_USER / SERPI_WEB_PASSWORD en .env")

    if args.desde and args.hasta:
        fecha_desde, fecha_hasta = args.desde, args.hasta
    else:
        primero, ultimo = primer_y_ultimo_dia_mes_actual()
        fecha_desde, fecha_hasta = primero.isoformat(), ultimo.isoformat()

    ruta_cruda = args.salida + ".descarga_cruda"
    descargar_xls_crudo(usuario, clave, fecha_desde, fecha_hasta, ruta_cruda,
                         timeout_ms=int(args.timeout_min * 60 * 1000), headless=not args.visible)
    convertir_a_xlsx(ruta_cruda, args.salida)

    kb = os.path.getsize(args.salida) / 1024
    print(f"\nListo: {args.salida} ({kb:.0f} KB), periodo {fecha_desde} a {fecha_hasta}.")
    print("Si hay un Local File Trigger de n8n vigilando esta carpeta, el pipeline "
          "(ETL + motor de reglas) deberia dispararse solo.")


if __name__ == "__main__":
    main()
