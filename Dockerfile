# Imagen del sistema de deteccion de anomalias en mallas de turnos.
#
# Dos targets a proposito, desde una base comun:
#
#   api       -> uvicorn + dashboard. Ligera (~250 MB): NO lleva navegador.
#   pipeline  -> corrida diaria. Lleva Chromium porque descargar_malla_serpi.py
#                es RPA sobre la web de SERPI (~900 MB).
#
# Se separan porque la API es el proceso que queda expuesto en red, y meterle un
# navegador completo solo aumenta su superficie de ataque sin que lo use nunca.
#
# Construir:
#   docker build --target api      -t turnos-api .
#   docker build --target pipeline -t turnos-pipeline .
# (o directamente `docker compose build`, que hace los dos)

# ---------------------------------------------------------------------------
# base
# ---------------------------------------------------------------------------
# Python 3.13 y no 3.14 (la version del entorno local) porque psycopg2-binary y
# xlrd publican ruedas para 3.13 con seguridad; en 3.14 pip tendria que
# compilar psycopg2 desde fuente y haria falta libpq-dev + gcc en la imagen.
# El codigo no usa nada especifico de 3.14.
FROM python:3.13-slim AS base

# TZ NO es cosmetico. `pipeline_diario.rango_por_defecto` y
# `descargar_malla_serpi` usan `date.today()`, y un contenedor en UTC corriendo
# a las 19:00 de Bogota ya esta en el dia siguiente: el 31 a las 19:00 pediria
# la malla del mes entrante y el rango se correria un mes entero.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Bogota

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Las dependencias antes del codigo: asi editar un .py no invalida la capa de
# pip, que es la que tarda (y en el target pipeline, la de Chromium).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Usuario sin privilegios. Los datos son PII de 404 trabajadores: si algun dia
# hay una vulnerabilidad en uvicorn o en el Chromium del RPA, que no sea root.
RUN useradd --create-home --uid 10001 turnos \
 && mkdir -p /app/logs "/app/Reportes mensuales" \
 && chown -R turnos:turnos /app/logs "/app/Reportes mensuales"

# ---------------------------------------------------------------------------
# api — el proceso de larga duracion
# ---------------------------------------------------------------------------
FROM base AS api
USER turnos
EXPOSE 8000

# El healthcheck usa /reglas y no /kpi: /reglas toca la BD (asi que un fallo de
# conexion se detecta) pero lee 7 filas de catalogo, sin los joins del read
# model. Un healthcheck cada 30 s no deberia costar un agregado sobre turnos.
# Con login (MODO_IDENTIDAD=SESION, o API_TOKEN definido) /reglas responde 401
# a quien no trae sesion — que igual prueba que el proceso esta vivo y
# sirviendo, que es lo que el healthcheck mide. Se usa http.client y no urllib
# porque urllib LANZA excepcion ante un 401: con urllib el contenedor quedaba
# "unhealthy" para siempre estando sano (visto en produccion, 2026-10-01).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import http.client,sys; \
        c=http.client.HTTPConnection('127.0.0.1', 8000, timeout=4); \
        c.request('GET', '/reglas'); \
        sys.exit(0 if c.getresponse().status in (200, 401, 403) else 1)" || exit 1

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]

# ---------------------------------------------------------------------------
# pipeline — se ejecuta y termina (no es un servicio)
# ---------------------------------------------------------------------------
FROM base AS pipeline

# Fuera de /root para que el usuario `turnos` pueda leer el navegador. Con el
# default (~/.cache/ms-playwright de root) el RPA falla con "Executable doesn't
# exist" en cuanto se deja de correr como root, y el error no es obvio.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

RUN python -m playwright install --with-deps chromium \
 && chmod -R a+rX /opt/playwright \
 && rm -rf /var/lib/apt/lists/*

USER turnos

# Sin argumentos: mes actual + el siguiente (ver pipeline_diario.rango_por_defecto).
# Se sobreescribe pasando argumentos al `docker compose run`.
ENTRYPOINT ["python", "pipeline_diario.py"]
CMD []
