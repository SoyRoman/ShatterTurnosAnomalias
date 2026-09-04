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
- `api.py` — API interna (FastAPI). Es el **puerto único**: dashboard y n8n
  consumen solo esto, nadie más abre conexiones a PostgreSQL.
- `dashboard.html` — dashboard con tres bandejas. Lo sirve la propia API en `/`.
- `n8n_auditoria_mensual.json` — flujo mensual importable en n8n, con los tres
  correos ya maquetados.
- `migracion_002_dashboard.sql`, `migracion_003_estados.sql` — solo para bases
  creadas con versiones anteriores del esquema. En instalación limpia no hacen
  falta: `schema.sql` ya las incorpora.
- `generar_informe.py` — genera un informe autónomo en un solo HTML.
- `test_reglas.py` — pruebas de los detectores; no necesitan base de datos.
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

> **La identidad del usuario es declarativa, no autenticación.** El dashboard
> pide un nombre y lo manda en `X-Usuario` para atribuir cada cambio en
> `anomalias_historial`, pero nadie verifica que sea quien dice. `API_TOKEN`
> autentica al *sistema* que llama, no a la *persona*. Antes de exponer esto
> fuera de la red interna hay que enganchar el login real de la empresa.

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

## Orquestación con n8n

Importa `n8n_auditoria_mensual.json`. Define estas variables en n8n:

| Variable | Ejemplo |
|---|---|
| `API_TURNOS` | `http://localhost:8000` |
| `URL_DASHBOARD` | `http://servidor-interno:8000/` |
| `CORREO_REMITENTE` | `auditoria@shatter.com.co` |
| `CORREO_GERENCIA` / `CORREO_PROGRAMADOR` / `CORREO_NOMINA` / `CORREO_ADMIN` | destinatarios |

Ajusta también la ruta de la carpeta que vigila el nodo *Llega malla nueva*.

**El flujo audita el mes siguiente, no el que pasó.** La malla se carga en
SERPI entre el 25 y el 27 para el mes entrante, así que auditarla el 28 le da
al programador 3–4 días para corregir **antes de que entre en vigencia**.

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
