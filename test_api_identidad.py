"""
Pruebas de la identidad de la persona y del rechazo de parametros desconocidos.

    python test_api_identidad.py                   # solo lectura
    python test_api_identidad.py --con-escritura   # + el camino de escritura

A DIFERENCIA de test_reglas.py, esto SI necesita la base de datos: la API abre
su pool de conexiones al arrancar. Necesita el .env configurado.

Por defecto NO escribe nada (ver abajo). Con `--con-escritura` se agrega una
prueba que modifica UNA anomalia real y la restaura: comprueba que el correo
verificado es el que termina en `anomalias.actualizado_por` y en
`anomalias_historial.usuario`. Va detras de una bandera porque correrla por
accidente contra produccion dejaria una fila de auditoria falsa.

Por que existe
--------------
Lo que se prueba aqui es el limite entre "rastro de auditoria confiable" y
"rastro que solo lo parece". El fallo que hay que impedir es de una linea: un
`or` de mas en `resolver_identidad` y `X-Usuario` volveria a servir de
suplantacion en modo PROXY. Eso no rompe nada visible —el dashboard sigue
funcionando igual— asi que sin una prueba nadie se enteraria hasta que alguien
tuviera que explicarle al Ministerio del Trabajo por que el historial dice que
una anomalia la justifico una persona que nunca la vio.

NO ESCRIBE EN LA BASE, y no por suerte
--------------------------------------
Todos los PATCH van contra `anomalias/999999999`, que no existe.
`resolver_identidad` es una dependencia de FastAPI, asi que corre ANTES del
handler:

    403 / 422  -> la identidad fue rechazada; no se llego a consultar la BD
    404        -> la identidad fue ACEPTADA y el handler no encontro la anomalia

O sea que aqui **un 404 es el resultado de exito**. Cero UPDATE, cero filas
nuevas en anomalias_historial. Si algun dia se cambia el id por uno real, esta
prueba empezaria a modificar datos de produccion: no lo hagas.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

RAIZ = os.path.dirname(os.path.abspath(__file__))
ID_INEXISTENTE = 999999999
CABECERA_CF = "Cf-Access-Authenticated-User-Email"
CUERPO = {"estado": "EN_REVISION", "nota": "prueba de identidad; no debe escribir"}

# Puertos altos para no chocar con la API que alguien tenga levantada en 8000.
PUERTO_DECLARATIVA = 8021
PUERTO_PROXY = 8022

fallos = []


def peticion(url, metodo="GET", cabeceras=None, cuerpo=None):
    datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
    pet = urllib.request.Request(url, data=datos, method=metodo)
    if datos:
        pet.add_header("Content-Type", "application/json")
    for k, v in (cabeceras or {}).items():
        pet.add_header(k, v)
    try:
        with urllib.request.urlopen(pet, timeout=10) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def comprobar(etiqueta, obtenido, esperado):
    ok = obtenido == esperado
    print(f"  [{'OK ' if ok else 'MAL'}] {etiqueta}: {obtenido} (esperado {esperado})")
    if not ok:
        fallos.append(etiqueta)


def arrancar(puerto, entorno_extra):
    """Levanta la API en un proceso aparte con el entorno pedido.

    Se usa un proceso y no TestClient a proposito: MODO_IDENTIDAD se lee al
    IMPORTAR api.py, asi que probar los dos modos en el mismo interprete
    exigiria recargar el modulo y es mas fragil que arrancar dos veces.
    """
    proceso = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--host", "127.0.0.1",
         "--port", str(puerto), "--log-level", "warning"],
        cwd=RAIZ, env={**os.environ, **entorno_extra},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{puerto}"
    for _ in range(60):
        if proceso.poll() is not None:
            raise SystemExit(
                f"la API murio al arrancar en el puerto {puerto}. "
                "Revisa el .env y que PostgreSQL este arriba.")
        try:
            peticion(base + "/salud")
            return proceso, base
        except Exception:
            time.sleep(0.5)
    proceso.kill()
    raise SystemExit(f"la API no respondio en el puerto {puerto} tras 30 s")


def probar_declarativa():
    print("=== MODO DECLARATIVA (red interna; el cliente dice quien es) ===")
    proceso, base = arrancar(PUERTO_DECLARATIVA, {"MODO_IDENTIDAD": "DECLARATIVA"})
    try:
        _, ident = peticion(base + "/identidad")
        comprobar("/identidad reporta el modo", ident["modo"], "DECLARATIVA")
        comprobar("/identidad no inventa un usuario", ident["usuario"], None)

        cod, _ = peticion(f"{base}/anomalias/{ID_INEXISTENTE}", "PATCH", cuerpo=CUERPO)
        comprobar("PATCH sin X-Usuario se rechaza", cod, 422)

        # Una cabecera en blanco no puede colarse como atribucion: dejaria una
        # fila de historial que no dice quien hizo el cambio.
        cod, _ = peticion(f"{base}/anomalias/{ID_INEXISTENTE}", "PATCH",
                          {"X-Usuario": "   "}, CUERPO)
        comprobar("PATCH con X-Usuario en blanco se rechaza", cod, 422)

        cod, _ = peticion(f"{base}/anomalias/{ID_INEXISTENTE}", "PATCH",
                          {"X-Usuario": "camilo"}, CUERPO)
        comprobar("PATCH con X-Usuario pasa la identidad (404 = llego al handler)",
                  cod, 404)
    finally:
        proceso.kill()


def probar_proxy():
    print("\n=== MODO PROXY (Cloudflare Access delante) ===")
    proceso, base = arrancar(PUERTO_PROXY, {"MODO_IDENTIDAD": "PROXY",
                                            "CABECERA_IDENTIDAD": CABECERA_CF})
    try:
        _, ident = peticion(base + "/identidad")
        comprobar("/identidad reporta el modo", ident["modo"], "PROXY")
        comprobar("/identidad sin cabecera no inventa usuario", ident["usuario"], None)

        _, ident = peticion(base + "/identidad", cabeceras={CABECERA_CF: "ana@empresa.com"})
        comprobar("/identidad refleja la cabecera verificada",
                  ident["usuario"], "ana@empresa.com")

        cod, _ = peticion(f"{base}/anomalias/{ID_INEXISTENTE}", "PATCH", cuerpo=CUERPO)
        comprobar("PATCH sin identidad verificada -> 403", cod, 403)

        # ESTE ES EL CASO QUE JUSTIFICA TODO EL ARCHIVO.
        # Si algun dia esto devuelve 404, significa que X-Usuario volvio a
        # servir de identidad en modo PROXY: cualquiera podria firmar una
        # justificacion con el nombre de otro.
        cod, _ = peticion(f"{base}/anomalias/{ID_INEXISTENTE}", "PATCH",
                          {"X-Usuario": "el_jefe"}, CUERPO)
        comprobar("PATCH con SOLO X-Usuario -> 403 (no hay suplantacion)", cod, 403)

        cod, _ = peticion(f"{base}/anomalias/{ID_INEXISTENTE}", "PATCH",
                          {CABECERA_CF: "ana@empresa.com"}, CUERPO)
        comprobar("PATCH con cabecera verificada pasa (404 = llego al handler)",
                  cod, 404)

        # Parametros desconocidos: el defecto que devolvia la primera pagina sin
        # filtrar con apariencia de haber filtrado, o sea datos de otro guarda.
        cod, _ = peticion(base + "/anomalias?cedula=123456")
        comprobar("/anomalias?cedula= (parametro inventado) -> 422", cod, 422)

        cod, _ = peticion(base + "/anomalias?busqueda=x&pagina=1&por_pagina=5")
        comprobar("/anomalias con parametros validos sigue funcionando", cod, 200)
    finally:
        proceso.kill()


def probar_modo_invalido():
    print("\n=== Un MODO_IDENTIDAD invalido debe abortar el arranque ===")
    # Fallar al arrancar y no caer a un default: un typo en la variable no puede
    # dejar la API sirviendo en modo declarativo sin que nadie lo note.
    r = subprocess.run([sys.executable, "-c", "import api"], cwd=RAIZ,
                       env={**os.environ, "MODO_IDENTIDAD": "CLOUDFLARE"},
                       capture_output=True, text=True)
    comprobar("importar api con modo invalido falla", r.returncode != 0, True)
    salida = (r.stderr or r.stdout).strip().splitlines()
    comprobar("el mensaje nombra la variable",
              "MODO_IDENTIDAD" in (salida[-1] if salida else ""), True)


def probar_escritura():
    """Comprueba que la identidad VERIFICADA es la que queda ESCRITA.

    Las pruebas de arriba usan un id inexistente, asi que nunca ejercitan el
    camino de escritura. Eso deja un hueco real: `resolver_identidad` alimenta
    el UPDATE de `anomalias` y el INSERT de `anomalias_historial`, y si se
    perdiera uno de los dos —o se cruzaran— ninguna prueba de codigo de estado
    lo notaria. El historial diria que un cambio lo hizo alguien que no fue.

    ESTA PRUEBA SI ESCRIBE, sobre UNA anomalia real, y restaura en el `finally`
    verificando que la base volvio a como estaba. Va detras de una bandera
    porque correrla por accidente contra produccion dejaria un cambio de estado
    y una fila de auditoria falsa — exactamente el tipo de dato que este
    sistema existe para que sea confiable.
    """
    print("\n=== ESCRITURA: la identidad verificada es la que se guarda ===")
    print("    (modifica UNA anomalia real y la restaura)")

    import psycopg2
    import psycopg2.extras
    from dotenv import load_dotenv

    load_dotenv(os.path.join(RAIZ, ".env"))
    cn = psycopg2.connect(
        host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"])
    cn.autocommit = True
    cur = cn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    correo = "prueba.identidad@invalido.local"

    def contar():
        cur.execute("SELECT (SELECT count(*) FROM anomalias) a,"
                    "       (SELECT count(*) FROM anomalias_historial) h")
        return dict(cur.fetchone())

    inicial = contar()
    cur.execute("""SELECT id, estado, nota, actualizado_por, actualizado_en
                     FROM anomalias WHERE estado = 'ABIERTA' ORDER BY id LIMIT 1""")
    antes = cur.fetchone()
    if not antes:
        print("  (no hay anomalias ABIERTA; se omite)")
        cn.close()
        return
    print(f"    anomalia de prueba: id={antes['id']}")

    proceso, base = arrancar(PUERTO_PROXY + 1, {"MODO_IDENTIDAD": "PROXY",
                                                "CABECERA_IDENTIDAD": CABECERA_CF})
    try:
        cod, _ = peticion(f"{base}/anomalias/{antes['id']}", "PATCH",
                          {CABECERA_CF: correo},
                          {"estado": "EN_REVISION", "nota": "prueba automatica"})
        comprobar("PATCH con identidad verificada -> 200", cod, 200)

        cur.execute("SELECT estado, actualizado_por FROM anomalias WHERE id = %s",
                    (antes["id"],))
        fila = cur.fetchone()
        comprobar("anomalias.actualizado_por = correo verificado",
                  fila["actualizado_por"], correo)

        cur.execute("""SELECT usuario, estado_anterior FROM anomalias_historial
                        WHERE anomalia_id = %s""", (antes["id"],))
        hist = cur.fetchall()
        comprobar("se escribio 1 fila de historial", len(hist), 1)
        if hist:
            comprobar("anomalias_historial.usuario = correo verificado",
                      hist[0]["usuario"], correo)
            comprobar("el historial guarda el estado anterior",
                      hist[0]["estado_anterior"], antes["estado"])

        # Suplantacion sobre una anomalia REAL: debe rebotar y no escribir.
        cod, _ = peticion(f"{base}/anomalias/{antes['id']}", "PATCH",
                          {"X-Usuario": "otra.persona@invalido.local"},
                          {"estado": "JUSTIFICADA", "nota": "suplantacion"})
        comprobar("PATCH con solo X-Usuario sobre una real -> 403", cod, 403)
        cur.execute("SELECT count(*) n FROM anomalias_historial WHERE anomalia_id = %s",
                    (antes["id"],))
        comprobar("el intento de suplantacion no escribio", cur.fetchone()["n"], 1)
    finally:
        proceso.kill()
        cur.execute("""UPDATE anomalias
                          SET estado = %s, nota = %s, actualizado_por = %s,
                              actualizado_en = %s
                        WHERE id = %s""",
                    (antes["estado"], antes["nota"], antes["actualizado_por"],
                     antes["actualizado_en"], antes["id"]))
        cur.execute("DELETE FROM anomalias_historial WHERE anomalia_id = %s",
                    (antes["id"],))
        comprobar("la base volvio a como estaba", contar(), inicial)
        cn.close()


if __name__ == "__main__":
    probar_declarativa()
    probar_proxy()
    probar_modo_invalido()
    if "--con-escritura" in sys.argv:
        probar_escritura()
    else:
        print("\n(omitida la prueba de escritura; para incluirla: "
              "python test_api_identidad.py --con-escritura)")
    print()
    if fallos:
        print(f"FALLARON {len(fallos)}:")
        for f in fallos:
            print(f"  - {f}")
        sys.exit(1)
    if "--con-escritura" in sys.argv:
        print("Todas las comprobaciones pasaron. La anomalia de prueba quedo restaurada.")
    else:
        print("Todas las comprobaciones pasaron. Cero escrituras en la base.")
