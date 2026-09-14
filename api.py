"""
API de turnos y anomalias — puerto unico del sistema.

Todo lo que consume estos datos (el dashboard y futuras integraciones) entra
por aqui. Nadie mas abre conexiones a PostgreSQL: asi la logica de negocio no
se reparte entre varios clientes distintos y hay un solo lugar donde auditar
quien leyo y quien escribio.

Uso:
    uvicorn api:app --host 127.0.0.1 --port 8000

Variables de entorno adicionales a las de .env:
    API_TOKEN            Token compartido. Si esta definido se exige en todas
                         las peticiones via cabecera  X-API-Token. Autentica al
                         SISTEMA que llama, no a la persona.
    MODO_IDENTIDAD       DECLARATIVA (default) o PROXY. Quien es la PERSONA.
                         Ver el bloque de comentarios mas abajo: es la
                         diferencia entre un rastro de auditoria confiable y
                         uno que solo lo parece.
    CABECERA_IDENTIDAD   En modo PROXY, de que cabecera se lee la identidad ya
                         verificada. Default: Cf-Access-Authenticated-User-Email
                         (Cloudflare Access). Para ALB+Cognito:
                         x-amzn-oidc-identity.
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
from pydantic import BaseModel, Field

load_dotenv()

RAIZ = os.path.dirname(os.path.abspath(__file__))
API_TOKEN = os.environ.get('API_TOKEN')

# ---------------------------------------------------------------------------
# Identidad de la persona
# ---------------------------------------------------------------------------
# Dos modos, y la diferencia no es cosmetica:
#
#   DECLARATIVA  El cliente dice quien es en `X-Usuario` y se le cree. Sirve
#                para operar en red interna cerrada y es el comportamiento
#                historico. NO se debe usar expuesto.
#   PROXY        La identidad la pone un proxy que ya autentico a la persona
#                (Cloudflare Access, ALB+Cognito, el login de la empresa) en
#                una cabecera que el cliente NO controla. `X-Usuario` se
#                IGNORA por completo en este modo.
#
# Por que importa: `anomalias_historial` puede terminar sustentando una
# respuesta ante el Ministerio del Trabajo. Con identidad declarativa expuesta,
# cualquiera podria marcar una anomalia como JUSTIFICADA firmando con el nombre
# de otro — y un historial que parece confiable y no lo es es peor que no tener
# historial.
#
# El nombre de la cabecera es configurable a proposito: asi el codigo no queda
# acoplado a un proveedor concreto y la decision de infraestructura
# (Cloudflare vs ALB) no bloquea el desarrollo.
MODOS_IDENTIDAD = ('DECLARATIVA', 'PROXY')
MODO_IDENTIDAD = os.environ.get('MODO_IDENTIDAD', 'DECLARATIVA').upper()
CABECERA_IDENTIDAD = os.environ.get(
    'CABECERA_IDENTIDAD', 'Cf-Access-Authenticated-User-Email')

if MODO_IDENTIDAD not in MODOS_IDENTIDAD:
    raise SystemExit(
        f"MODO_IDENTIDAD='{MODO_IDENTIDAD}' no es valido. "
        f"Opciones: {', '.join(MODOS_IDENTIDAD)}.")

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
    if not API_TOKEN:
        print("ADVERTENCIA: API_TOKEN no esta definido. La API queda SIN "
              "autenticacion. No la expongas fuera de la red interna.",
              file=sys.stderr)
    if MODO_IDENTIDAD == 'DECLARATIVA':
        print("ADVERTENCIA: MODO_IDENTIDAD=DECLARATIVA. Se cree la cabecera "
              "X-Usuario sin verificarla, asi que el historial de auditoria NO "
              "impide suplantacion. Solo aceptable en red interna cerrada; "
              "para exponer esto usa MODO_IDENTIDAD=PROXY detras de un proxy "
              "que autentique de verdad.", file=sys.stderr)
    else:
        print(f"Identidad en modo PROXY: se lee de la cabecera "
              f"'{CABECERA_IDENTIDAD}' y X-Usuario se ignora.", file=sys.stderr)
    yield
    if _pool:
        _pool.closeall()


app = FastAPI(
    title="Deteccion de anomalias en mallas de turnos",
    description="Seguridad Shatter de Colombia LTDA BIC — API interna",
    version="1.0.0",
    lifespan=ciclo_de_vida,
)


def verificar_token(x_api_token: Optional[str] = Header(None)):
    """Token compartido, al estilo Secret Key de SERPI.

    OJO: esto autentica al SISTEMA que llama (el dashboard), no a la PERSONA.
    La identidad de quien gestiona una anomalia viaja aparte y la resuelve
    `resolver_identidad` segun `MODO_IDENTIDAD`.
    """
    if API_TOKEN and x_api_token != API_TOKEN:
        raise HTTPException(status_code=401, detail="Token invalido o ausente")


def resolver_identidad(peticion: Request) -> str:
    """Quien esta haciendo el cambio. Ver el bloque de MODO_IDENTIDAD arriba.

    En modo PROXY, la ausencia de la cabecera es un 403 y NO se cae de vuelta a
    `X-Usuario`. Ese fallback seria justo el agujero que este modo cierra:
    bastaria con alcanzar la API sin pasar por el proxy para poder firmar como
    cualquiera. Si esto devuelve 403 en produccion, el diagnostico es de red
    (alguien llego directo al puerto), no de configuracion del cliente.

    La confianza en una cabecera simple es aceptable porque la garantia viene
    de la RED: la API escucha en 127.0.0.1 y el grupo de seguridad de la EC2 no
    admite ningun ingreso, asi que el unico camino es el proxy. Si algun dia se
    expone el puerto, hay que pasar a verificar el JWT firmado que emiten tanto
    Cloudflare Access (`Cf-Access-Jwt-Assertion`) como ALB+Cognito
    (`x-amzn-oidc-data`) — la cabecera plana dejaria de ser suficiente.
    """
    if MODO_IDENTIDAD == 'PROXY':
        quien = (peticion.headers.get(CABECERA_IDENTIDAD) or '').strip()
        if not quien:
            raise HTTPException(
                status_code=403,
                detail=f"Sin identidad verificada en '{CABECERA_IDENTIDAD}'. "
                       "Esta peticion no paso por el proxy de autenticacion.")
        return quien

    quien = (peticion.headers.get('X-Usuario') or '').strip()
    if not quien:
        raise HTTPException(
            status_code=422,
            detail="Falta la cabecera X-Usuario: toda gestion debe quedar atribuida")
    return quien


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


@app.get("/identidad", dependencies=[Depends(verificar_token)])
def identidad(peticion: Request):
    """Quien soy, segun el modo de identidad configurado.

    Existe para que el dashboard no tenga que adivinar: en modo PROXY toma el
    nombre de aqui y esconde el boton de "identificarse" (pedirle el nombre a
    alguien que ya inicio sesion es a la vez redundante y engañoso, porque
    sugiere que ese nombre es el que se va a guardar cuando en realidad se
    ignora). En modo DECLARATIVA devuelve `usuario: null` y el dashboard sigue
    preguntando como siempre.

    No usa `Depends(resolver_identidad)` a proposito: aqui la falta de
    identidad es una respuesta valida que el dashboard necesita poder leer,
    no un 403.
    """
    if MODO_IDENTIDAD == 'PROXY':
        quien = (peticion.headers.get(CABECERA_IDENTIDAD) or '').strip() or None
    else:
        quien = None
    return {"modo": MODO_IDENTIDAD, "usuario": quien}


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
    `resolver_identidad`, que en modo PROXY exige una cabecera puesta por el
    autenticador e ignora `X-Usuario`.
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
