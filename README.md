# Detección de anomalías en mallas de turnos

Normaliza el export de SERPI (`RepProgramacion.xlsx`) e inserta la
información en PostgreSQL siguiendo el esquema de `schema.sql`.

## Archivos

- `schema.sql` — crea las tablas (clientes, puestos, guardas, tipos_turno,
  turnos, horas_declaradas_mes, reglas_anomalia, anomalias).
- `seed_reglas.sql` — precarga las 7 reglas con su taxonomía por responsable
  (naturaleza y área que debe actuar). Idempotente.
- `etl_normalizacion.py` — lee el Excel y carga `clientes`, `puestos`,
  `guardas`, `tipos_turno`, `turnos` y `horas_declaradas_mes`.
- `motor_reglas.py` — evalúa las 7 reglas de anomalías sobre los turnos ya
  cargados y escribe en `anomalias`.
- `vistas_reporte.sql` — capa semántica: vistas por audiencia para el
  dashboard y los correos.
- `api.py` — API interna (FastAPI). Es el **puerto único**: el dashboard y
  cualquier integración consumen solo esto, nadie más abre conexiones a
  PostgreSQL.
- `dashboard.html` — dashboard con tres bandejas. Lo sirve la propia API en `/`.
- `descargar_malla_serpi.py` — descarga la malla de SERPI por RPA (Playwright),
  mientras el módulo de turnos no tenga API.
- `pipeline_diario.py` — la corrida diaria completa: descarga + ETL + motor,
  con reintentos, bitácora y códigos de salida distinguibles.
- `Dockerfile`, `docker-compose.yml` — empaquetado. Tres perfiles: `manual`
  (pipeline), `local` (PostgreSQL de pruebas) y `produccion` (túnel de
  Cloudflare).
- `despliegue/` — unidades de systemd para la ejecución programada,
  `DESPLIEGUE.md` (paso a paso del despliegue en AWS) y `CLOUDFLARE.md`
  (configuración del autenticador).
- `migracion_002_dashboard.sql`, `migracion_003_estados.sql` — solo para bases
  creadas con versiones anteriores del esquema. En instalación limpia no hacen
  falta: `schema.sql` ya las incorpora.
- `generar_informe.py` — genera un informe autónomo en un solo HTML.
- `test_reglas.py` — pruebas de los detectores; no necesitan base de datos.
- `test_api_identidad.py` — pruebas de los dos modos de identidad y del
  rechazo de parámetros desconocidos; **sí** necesitan base de datos.
- `requirements.txt`, `.env.example`.

## Primera vez

Si es la primera vez que se corre este proyecto en esta máquina, PostgreSQL
necesita el rol y la base de datos — `.env.example` asume que ya existen, no
los crea:

```bash
psql -U postgres
```

```sql
CREATE ROLE turnos_app WITH LOGIN PASSWORD 'elige-una-clave-fuerte';
CREATE DATABASE turnos OWNER turnos_app;
\q
```

Con eso ya listo:

```bash
python3 -m venv venv
source venv/bin/activate          # en Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edita .env: DB_PASSWORD debe ser la clave que elegiste arriba

psql -U turnos_app -d turnos -f schema.sql
psql -U turnos_app -d turnos -f seed_reglas.sql
psql -U turnos_app -d turnos -f vistas_reporte.sql
```

> `.env` nunca viaja con el repo (está en `.gitignore`): al clonar en una
> máquina nueva hay que repetir este paso, con una clave propia de esa
> máquina — no reutilices la del PC donde se hizo el desarrollo original.

Los tres son idempotentes: se pueden volver a correr sin duplicar nada.

### Y falta la malla

**El Excel tampoco viene en el repositorio.** Lleva nombres y cédulas de 404
trabajadores reales, así que `*.xlsx` está en `.gitignore`. Al clonar en una
máquina nueva hay que traerlo, o el ETL se queda sin nada que leer:

```bash
python -m playwright install chromium        # una sola vez
python descargar_malla_serpi.py --desde 2026-09-01 --hasta 2026-09-30
```

Necesita `SERPI_WEB_USER` y `SERPI_WEB_PASSWORD` en el `.env` de esa máquina.
La alternativa es copiar el archivo desde otro equipo **por medio interno**
(USB, red local): nunca por correo ni por un servicio externo.

Si lo olvidas, el ETL lo dice con un mensaje que explica qué hacer. Antes no:
`openpyxl` propagaba un `FileNotFoundError` desde dentro de `zipfile`, con seis
marcos de pila de por medio, y el error parecía de un Excel corrupto.

## Correr el ETL

```bash
python3 etl_normalizacion.py --archivo RepProgramacion.xlsx
```

Es **idempotente**: puedes correrlo de nuevo cada mes con el nuevo export y
no duplica filas — actualiza (`UPSERT`) sobre la llave natural
`(guarda_cedula, puesto_id, fecha, slot)` en `turnos`. El `slot` es el
número de fila/asignación dentro del puesto (columna C del Excel): un mismo
guarda puede tener más de un bloque en el mismo puesto el mismo día (p.ej.
turno regular + turno adicional `ADI`), y sin el `slot` en la llave esas
asignaciones concurrentes se perdían.

## Correr el motor de reglas

```bash
python3 motor_reglas.py                    # evalua todos los guardas, escribe en anomalias
python3 motor_reglas.py --cedula 1234567    # solo un guarda (para depurar)
python3 motor_reglas.py --dry-run           # imprime hallazgos sin escribir en la BD
```

Consolida los turnos de cada guarda **a través de todos sus puestos** (nunca
puesto por puesto — los cruces de horario reales aparecen justamente entre
puestos distintos) y evalúa las 7 reglas de `reglas_anomalia`, resolviendo el
umbral vigente para la fecha de cada hallazgo.

Cada corrida borra y regenera las anomalías en estado `ABIERTA` para las
reglas y guardas evaluados; las que ya están en `EN_REVISION` o
`JUSTIFICADA` no se tocan, para no resucitar ni duplicar algo que un humano
ya revisó. Cada anomalía se identifica por una `huella` derivada de llaves
de negocio, así que sobrevive a una recarga completa del ETL.

Las funciones `detectar_*` en `motor_reglas.py` son puras (reciben una lista
de turnos, devuelven violaciones) para poder reutilizarse en modo predictivo
más adelante (validar una asignación nueva antes de guardarla), no solo en
esta auditoría retrospectiva.

Acota a un mes con `--desde` / `--hasta`:

```bash
python3 motor_reglas.py --desde 2026-08-01 --hasta 2026-08-31
```

## Levantar la API y el dashboard

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

El dashboard queda en `http://<servidor>:8000/` y la documentación
interactiva de la API en `/docs`.

Define `API_TOKEN` en el `.env` para exigir la cabecera `X-API-Token` en cada
petición. Si no la defines, la API arranca **sin autenticación** y lo avisa por
consola — sólo aceptable en red interna cerrada.

### Quién es la persona: `MODO_IDENTIDAD`

`API_TOKEN` autentica al *sistema* que llama, no a la *persona*. Para la
persona hay dos modos, y la diferencia decide si `anomalias_historial` es un
rastro de auditoría confiable o solo lo parece:

| Modo | Cómo se identifica | Cuándo usarlo |
|---|---|---|
| `DECLARATIVA` **(default)** | El dashboard pide un nombre y lo manda en `X-Usuario`. **Nadie verifica que sea quien dice** | Solo red interna cerrada |
| `PROXY` | Se lee de `CABECERA_IDENTIDAD`, que pone un proxy que **ya autenticó** a la persona. `X-Usuario` se ignora; si falta la cabecera → `403` | Producción |

```
MODO_IDENTIDAD=PROXY
CABECERA_IDENTIDAD=Cf-Access-Authenticated-User-Email   # Cloudflare Access
# CABECERA_IDENTIDAD=x-amzn-oidc-identity               # ALB + Cognito
```

**El default es el modo inseguro a propósito**, para no romper la operación
actual en red interna. La contrapartida es que olvidarlo **no da ningún error**:
la API arranca y funciona igual, solo lo avisa por consola. Por eso
`docker-compose.yml` no publica el puerto a la red — la barrera real contra
desplegar en modo declarativo es que no haya por dónde llegar.

En modo `PROXY` el dashboard consulta `GET /identidad`, muestra el correo
verificado y deja de pedir un nombre. Configuración paso a paso en
[despliegue/CLOUDFLARE.md](despliegue/CLOUDFLARE.md).

```bash
python test_api_identidad.py                   # 14 comprobaciones de los dos modos
python test_api_identidad.py --con-escritura   # + 7 del camino de escritura
```

> A diferencia de `test_reglas.py`, esta prueba **sí necesita la base de
> datos** (la API abre su pool al arrancar). Por defecto **no escribe nada**:
> hace `PATCH` sobre un id inexistente, así que un `404` es su resultado de
> éxito. Con `--con-escritura` modifica una anomalía real y la restaura,
> comprobando que el correo verificado es el que queda en
> `anomalias.actualizado_por` y en `anomalias_historial.usuario` — el hueco que
> las pruebas de código de estado no cubren.

## El ciclo de trabajo

El sistema **no corrige nada y no abre tickets en SERPI** (ese módulo es para
escalar con el proveedor). El ciclo es:

```
el dashboard muestra qué está mal y dónde
   → el usuario entra a SERPI y corrige la malla
   → el siguiente cargue confirma que quedó corregido
```

Por eso hay cuatro estados y **`RESUELTA` no se puede marcar a mano**: la pone
el motor cuando comprueba que la anomalía ya no aparece en el cargue más
reciente. El usuario corrige en SERPI y el sistema le confirma que su
corrección llegó — no tiene que acordarse de volver a cerrar nada. Si una
anomalía dada por resuelta reaparece, el motor la reabre como `ABIERTA`.

## La corrida diaria

```bash
python pipeline_diario.py                 # mes actual + el siguiente
python pipeline_diario.py --meses 1       # solo el mes actual
python pipeline_diario.py --archivo "Reportes mensuales/RepProgramacion_....xlsx"
```

Hace los tres pasos seguidos —descarga de SERPI, ETL y motor— y es lo que
convierte el sistema en algo vivo: **el usuario corrige en SERPI y a la mañana
siguiente el dashboard lo confirma solo.** Sin esta corrida el dashboard queda
congelado en la foto del último cargue manual.

**Por defecto abarca dos meses, y no es arbitrario.** La malla del mes entrante
se carga en SERPI entre el 25 y el 27. Con un solo mes, esa malla nueva sería
invisible hasta el día 1 — justo cuando ya está en vigencia y corregirla
implica reprogramar gente. Con dos, aparece el mismo día que la cargan y el
programador tiene 3–4 días para arreglarla antes de que arranque. Ese es el
salto de detección retrospectiva a prevención.

### Qué se reintenta y qué no

Solo la descarga (3 intentos, 5 min de espera). Es la única parte que depende de
una red y de un servidor ajeno, así que un fallo suele ser transitorio. Si
fallan el ETL o el motor es por datos o por código, y reintentar no arregla
nada: solo esconde el error.

### Códigos de salida

Son el contrato con quien lo programe (systemd, cron, Task Scheduler):

| Código | Significado |
|---|---|
| `0` | Todo bien |
| `1` | Falta configuración; no se intentó nada |
| `2` | La descarga falló tras todos los reintentos |
| `3` | El ETL falló |
| `4` | El ETL **se abortó por seguridad**: requiere revisión humana |
| `5` | El motor de reglas falló |
| `6` | Ya había otra corrida en curso (no es un error) |

**El `4` merece atención aparte.** No es un fallo técnico: significa que la
malla descargada difiere tanto de la base que la reconciliación se negó a
borrar (umbral por defecto: 20%). Es la red de seguridad contra un archivo que
llegó vacío o truncado por un fallo de SERPI, donde borrar sería catastrófico y
silencioso. **La base queda intacta.** Revisa el archivo que quedó en
`Reportes mensuales/` antes de forzar nada con `--umbral-borrado`.

### Bitácora y evidencia

- `logs/pipeline_diario.log` — rotativa, 5 MB × 10 archivos.
- `Reportes mensuales/RepProgramacion_<desde>_a_<hasta>_<AAAAMMDD>.xlsx` — la
  malla exacta de cada corrida. **No se sobreescribe un archivo único a
  propósito:** si en seis meses alguien pregunta por qué el sistema reportó una
  violación, hay que poder mostrar los datos que se evaluaron ese día, no los
  de hoy. Es el respaldo de la cadena
  `anomalía → turnos_involucrados → archivo de origen`.
- `logs/pipeline_diario.lock` — candado contra corridas solapadas; se considera
  huérfano a las 6 horas.

## Probado

Corrido de punta a punta contra `RepProgramacion.xlsx` (julio 2026): 67
clientes, 164 puestos, 404 guardas, 99 tipos de turno, 8,821 turnos-día
procesados = **8,821 filas distintas** en `turnos` (cero colisiones con la
llave `guarda_cedula, puesto_id, fecha, slot`). Confirmado idempotente (dos
corridas seguidas no duplican).

`motor_reglas.py` corrido contra esa misma carga reproduce **exactamente**
los 386 hallazgos / 166 guardas / 7 reglas del análisis manual de referencia
de julio 2026 (`Deteccion de anomalias Julio.xlsx`) — cero discrepancias sin
explicar.

**El empaquetado también está probado**, no solo escrito (septiembre de 2026):

- Las dos imágenes construyen y la de API responde desde el contenedor:
  `/reglas` 200, `/kpi` con los denominadores por periodo correctos, el
  dashboard servido en `/`, y el `HEALTHCHECK` en estado `healthy`.
- Chromium arranca dentro del contenedor **como usuario sin privilegios**
  (uid 10001) — es el punto que se rompe si se toca
  `PLAYWRIGHT_BROWSERS_PATH`, con un error que no apunta a la causa.
- Se verificó que en la imagen **no hay** `.env`, `Migracion/`, `.git` ni
  ningún `*.xlsx`.
- Los cinco `.sql` se aplican limpios sobre un PostgreSQL 18 vacío, en orden:
  9 tablas, 7 vistas, las 7 reglas con su taxonomía y la columna `slot`
  presente. Esa es la misma secuencia del despliegue en servidor.
- La cadena completa corrió contra esa base recién creada: agosto → 303
  hallazgos, y septiembre encima → **244, idéntico al resultado en local**.
  (Agosto da 303 aislado y 339 con julio y septiembre cargados: es el
  comportamiento documentado en `CLAUDE.md` §7 — las ventanas móviles a
  caballo entre meses recién se ven completas cuando el mes vecino existe.)
- La red de seguridad se probó de verdad: forzando el aborto de la
  reconciliación, `pipeline_diario.py` devuelve `4`, **no** ejecuta el motor y
  la base queda intacta (24.623 turnos y 990 anomalías, sin cambios).

Si migras una base de datos creada con una versión anterior de `schema.sql`
(antes de que `turnos` tuviera `slot`), no alcanza con re-correr el ETL:
necesitas la migración (`ALTER TABLE ... ADD COLUMN slot ...` + cambiar la
`UNIQUE`) y luego **vaciar y recargar** `turnos`/`horas_declaradas_mes`
completos — un `slot` con valor por defecto sobre filas ya colapsadas deja
datos huérfanos que no corresponden a ningún slot real.

## Informe autónomo para reuniones

```bash
python3 generar_informe.py --periodo 2026-07
```

Produce `informe_2026-07.html`: **un solo archivo** con los datos del periodo
incrustados, que se abre con doble clic. No necesita servidor, base de datos ni
internet, así que se le puede enviar a gerencia para que lo proyecte por su
cuenta. Es de solo lectura (sin botones de gestión) y lleva un aviso con la
fecha del corte.

> **Contiene nombres y cédulas reales.** Se distribuye internamente igual que
> el Excel de la malla: nunca en un repositorio ni en un servicio público.
> `.gitignore` ya excluye `informe_*.html`.

## Descarga automática de la malla (mientras SERPI no tenga API de turnos)

```bash
pip install -r requirements.txt
python -m playwright install chromium   # una sola vez, descarga el navegador

python descargar_malla_serpi.py                                  # mes actual completo
python descargar_malla_serpi.py --desde 2026-09-01 --hasta 2026-09-30
```

La API general de SERPI (`CONTEXTO_API_SERPI.md`, si lo tienes) es solo el
núcleo ERP contable/comercial — no expone ningún endpoint de turnos ni
programación. Mientras eso no exista, este script automatiza lo mismo que
hace un usuario a mano: inicia sesión en la interfaz web de SERPI
(`https://shatter.serpi.com.co/`), abre el reporte de programación y lo
descarga con Proyecto/Puesto/Empleado vacíos (equivale a "todos los
clientes", igual que el export manual).

Variables necesarias en `.env` (ver `.env.example`):

```
SERPI_WEB_USER=...
SERPI_WEB_PASSWORD=...
```

> Son las credenciales de **login web**, distintas del `SERPI_TOKEN` /
> `SERPI_SECRET_KEY` de la API general. Cambia la clave de este usuario en
> SERPI por una fuerte antes de dejar el script corriendo desatendido — una
> clave débil usada a diario por un robot es un riesgo mayor que la misma
> clave usada manualmente unas pocas veces al mes.

**Por qué tarda varios minutos:** el reporte para todos los clientes es
lento de generar en el propio servidor de SERPI (se midió igual de lento
para un solo día que para un mes completo), no es un problema del script.
El timeout por defecto es de 10 minutos (`--timeout-min`).

**SERPI entrega un `.xls` binario antiguo (Excel 97-2003), no un `.xlsx`
moderno.** `etl_normalizacion.py` usa `openpyxl`, que solo lee `.xlsx`
(OOXML) — por eso el script convierte el archivo automáticamente
(`xlrd` + `openpyxl`, celda por celda) antes de guardarlo. Verificado que la
conversión preserva la misma estructura de hojas y encabezados que el
export manual.

**No lo programes directamente.** Este script solo descarga; para la corrida
diaria usa `pipeline_diario.py`, que lo invoca y además hace el ETL y el motor,
reintenta, deja bitácora y devuelve códigos de salida distinguibles. Ver
"La corrida diaria" y "Ejecución programada".

## Despliegue con Docker

```bash
docker compose --profile manual build       # construye las DOS imágenes
docker compose up -d api                    # API + dashboard
docker compose run --rm pipeline            # corrida diaria, a mano
docker compose --profile local up -d        # + PostgreSQL local, para pruebas
```

> Ojo con el primer comando: `docker compose build` **a secas solo construye
> `api`**, porque `pipeline` vive en el profile `manual`. Sin `--profile manual`
> la imagen del pipeline no se actualiza y el timer seguiría corriendo la
> versión anterior — un fallo silencioso.

**Dos imágenes y no una**, desde la misma base:

| Target | Tamaño | Lleva Chromium | Rol |
|---|---|---|---|
| `api` | ~250 MB | no | Proceso de larga duración: uvicorn + dashboard |
| `pipeline` | ~540 MB | sí | Arranca, trabaja y termina. RPA de descarga |

Se separan porque la API es el proceso que queda expuesto en red, y meterle un
navegador completo solo aumentaría su superficie de ataque sin que lo use nunca.

Detalles que ya están resueltos en los archivos y conviene no revertir:

- **`TZ=America/Bogota` en la imagen.** No es cosmético: el rango por defecto se
  calcula con `date.today()`, y un contenedor en UTC corriendo a las 19:00 de
  Bogotá ya está en el día siguiente — el 31 pediría la malla del mes entrante y
  el rango se correría un mes entero. **Hay que fijarla también en el host**
  (`sudo timedatectl set-timezone America/Bogota`), porque `OnCalendar` de
  systemd usa la zona del sistema, no la del contenedor.
- **La API se publica en `127.0.0.1`, no en `0.0.0.0`.** No es porque falte la
  autenticación —ya está (`MODO_IDENTIDAD=PROXY`)— sino porque **su default es
  el modo declarativo**, y en ese modo cualquiera podría firmar una
  justificación con el nombre de otro. Olvidar la variable no produce ningún
  error, así que el binding a loopback es lo único que impide que un despliegue
  distraído quede expuesto en modo declarativo. Delante va un proxy que
  autentica de verdad. Para red interna cerrada, `API_BIND=0.0.0.0` en `.env`,
  a sabiendas.
- **`shm_size: 1gb` en el pipeline.** Chromium necesita más que los 64 MB de
  `/dev/shm` que da Docker por defecto; sin esto se cae a mitad de la descarga
  con errores que no apuntan a la causa (`Target closed`).
- **El servicio `pipeline` está detrás de un `profile`.** Así un
  `docker compose up` no dispara por sorpresa una descarga de 20 minutos contra
  SERPI ni una reconciliación que borra turnos.
- **Nada de PII ni credenciales entra en la imagen.** `.dockerignore` excluye
  `.env`, `Migracion/` y todo `*.xlsx`/`*.csv`/`*.har`. Las variables entran en
  tiempo de ejecución (`env_file`), nunca horneadas en una capa: una imagen se
  sube a un registry y guarda cada capa para siempre.
- **El volumen de PostgreSQL va en `/var/lib/postgresql`**, no en
  `/var/lib/postgresql/data`. Desde la imagen 18 los datos viven en un
  subdirectorio por versión mayor; con la ruta vieja el contenedor entra en
  bucle de reinicio.

## Ejecución programada

En `despliegue/` hay un `.service` y un `.timer` de systemd:

```bash
sudo timedatectl set-timezone America/Bogota
sudo useradd --system --home /opt/turnos --shell /usr/sbin/nologin turnos
sudo usermod -aG docker turnos
sudo cp despliegue/turnos-pipeline.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now turnos-pipeline.timer

systemctl list-timers turnos-pipeline.timer    # próxima ejecución
journalctl -u turnos-pipeline -n 50            # última corrida
sudo systemctl start turnos-pipeline.service   # forzar una corrida ahora
```

**Por qué systemd y no un cron dentro del contenedor:** los códigos de salida
quedan visibles (`systemctl status`, `OnFailure=`), mientras que un cron en
contenedor los tira a la basura y una corrida fallida se vuelve invisible —
que en una herramienta de cumplimiento es el peor modo de fallo. Además
`Persistent=true` recupera la corrida si la máquina estaba apagada: un día sin
corrida es un día en que una violación nueva no se detectó y nadie se enteró.

**Corre a las 04:30** de Bogotá: antes de que llegue el personal, así el
dashboard ya tiene la foto de hoy cuando alguien lo abre, y de madrugada el
servidor de reportes de SERPI está descargado.

> **La cuenta que ejecuta es de servicio, no de un empleado.** Si alguien se va
> de la empresa y se le desactiva la cuenta, la auditoría diaria no puede caerse
> con ella; y lo que el proceso escribe debe quedar atribuido a un sistema, no a
> una persona que no tomó la decisión.
>
> Ojo con un detalle de seguridad: pertenecer al grupo `docker` equivale a root
> en esa máquina. Es inevitable con Docker clásico; si eso no es aceptable para
> la política interna, la alternativa es Podman rootless.

En Windows, el equivalente es una tarea de Task Scheduler que ejecute
`python pipeline_diario.py` con el directorio de trabajo en la raíz del
proyecto — con la salvedad de que **solo funciona si ese PC está encendido a la
hora programada**. No es un servicio; para operación real hace falta un
servidor.
