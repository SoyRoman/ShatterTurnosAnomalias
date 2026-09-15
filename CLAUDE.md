# Sistema de Detección de Anomalías en Mallas de Turnos

Contexto del proyecto para Claude Code. Léelo completo antes de proponer
cambios: hay decisiones de diseño no obvias documentadas aquí (sobre todo en
"Reglas del dominio" y "Errores conocidos / trampas").

---

## 1. Contexto de negocio

**Empresa:** Seguridad Shatter de Colombia LTDA BIC — empresa de vigilancia y
seguridad privada con sede en Santiago de Cali, Colombia. Opera ~67 clientes,
~164 puestos de vigilancia y ~404 guardas activos.

**Los dos sistemas de la empresa:**

| Sistema | Qué es | Acceso |
|---|---|---|
| **SERPI** | ERP de un proveedor externo colombiano. Maneja programación y malla de turnos, informes, nómina y recursos humanos. | APIs generales disponibles (usuario admin, Secret Key, token). **El módulo de turnos NO tiene API propia todavía.** |
| **Sistema propio** | Desarrollo interno de la empresa, acceso total al código. | Total |

**El problema que resuelve este proyecto:**

La malla mensual de turnos se carga manualmente en SERPI entre el 25 y el 27 de
cada mes. SERPI **exporta a Excel pero no importa**, y aunque tiene un monitor
que advierte sobre duplicidades y posibles incumplimientos normativos,
**permite continuar bajo responsabilidad de la administración** — es decir, las
alertas se pueden ignorar y frecuentemente se ignoran. El resultado es que la
malla publicada contiene incumplimientos legales que nadie audita de forma
sistemática.

Un análisis manual de la malla de julio de 2026 (8.821 turnos-día) encontró
**386 hallazgos** afectando a **166 de 404 guardas (~41%)**, incluyendo 10
cruces de horario físicamente imposibles de cumplir (fuente: `Deteccion de
anomalias Julio.xlsx`, hoja "Hallazgos Detallados" — no la subas a
repositorios remotos, contiene PII). Este proyecto automatiza esa auditoría,
y `motor_reglas.py` ya reproduce ese resultado **exacto** — las 7 reglas,
386/386, 166/166 guardas — contra la malla real de julio 2026 (ver §8).

**Por qué existe este repo y no una integración directa:** el gerente de SERPI
indicó explícitamente que, cuando no existe API para un módulo, primero debe
definirse el conjunto de datos requerido y **validarse el modelo con datos
reales**; solo entonces se solicita el desarrollo de la API. Este proyecto ES
esa validación. El esquema de datos que aquí se consolide es el insumo técnico
para pedir formalmente la API de turnos.

---

## 2. Objetivos

**Objetivo inmediato (alcance actual del repo):**

1. Normalizar el export Excel de SERPI (`RepProgramacion.xlsx`) hacia un modelo
   relacional en PostgreSQL. **[HECHO]**
2. Implementar un motor de reglas que detecte anomalías normativas sobre esos
   datos. **[HECHO — pendiente de correr contra la BD real y validar contra
   el análisis manual de 386 hallazgos]**
3. Exponer una API propia de consulta de turnos y anomalías. **[HECHO —
   `api.py`, puerto único: el dashboard y cualquier integración consumen solo
   esto]**
4. Dashboard propio con las tres bandejas (programador / nómina / gerencia).
   **[HECHO — `dashboard.html`]**
5. Corrida diaria automática que cierre el ciclo (corregir en SERPI → el
   sistema lo confirma solo). **[HECHO — `pipeline_diario.py` + timer de
   systemd en `despliegue/`, empaquetado en `Dockerfile` /
   `docker-compose.yml`]**

> **Corrección importante (agosto 2026).** Una versión anterior de este archivo
> planteaba «automatizar el registro de anomalías críticas como tickets en
> SERPI (Soporte > Tickets)». **Eso era un malentendido:** ese módulo sirve para
> escalar incidencias con los *desarrolladores de SERPI* (el proveedor), no para
> gestionar turnos. El ciclo real es:
>
> ```
> el dashboard muestra qué está mal y dónde
>        → el usuario entra a SERPI y corrige la malla
>        → el siguiente cargue confirma que quedó corregido
> ```
>
> El sistema **no corrige nada y no abre tickets**. Su trabajo es que
> identificar el problema y ubicarlo en SERPI sea rápido. Por eso el criterio
> de diseño del dashboard no es «mostrar los datos» sino **cuánto tarda alguien
> en pasar de ver un hallazgo a saber exactamente qué tocar en SERPI**.

**Objetivo a mediano plazo:** que el ETL cambie su fuente de `EXCEL_EXPORT` a
`API_SERPI` sin tocar el resto del sistema, cuando SERPI desarrolle la API de
turnos. Por eso `turnos.origen` existe desde el día uno.

**Objetivo a largo plazo (fuera del alcance de este repo, pero orienta el
diseño):** este modelo alimenta un sistema mayor de gestión de turnos que
incluye confirmación automática de asistencia por WhatsApp y sugerencia
automática de reemplazos desde el pool de guardas "Ocasionales" — validando
descanso mínimo, tope semanal y ausencia de doble turno. Las mismas reglas que
aquí *detectan* incumplimientos, allá *previenen* asignaciones inválidas. Diseña
el motor de reglas pensando en que será reutilizado en modo predictivo, no solo
retrospectivo.

---

## 3. Reglas del dominio (marco legal colombiano)

Estas no son reglas arbitrarias. Son ley colombiana vigente para el sector de
vigilancia y seguridad privada. **No las modifiques sin verificar la norma.**

| Norma | Contenido aplicado |
|---|---|
| **Ley 1920 de 2018, Art. 7** ("ley del vigilante") | Jornada ordinaria 8h/día; hasta 4h suplementarias; **máximo 12h diarias**; **máximo 60h semanales** incluyendo suplementarias. Este es el tope absoluto. |
| **Ley 2101 de 2021** (modifica Art. 161 CST) | Reducción progresiva de la jornada *de referencia*: 44h hasta el 14/07/2026, **42h desde el 15/07/2026**. |
| **Art. 172 y 175 CST** | Descanso semanal remunerado: mínimo **24h continuas** por semana. No diferible más allá de 6 días consecutivos de trabajo. |
| **Art. 167 CST** | La jornada puede tener un intermedio de descanso que **no se computa** como parte de la jornada. |
| **Ley 2466 de 2025, Art. 13** (reforma laboral) | El sector vigilancia queda exceptuado del régimen general de horas extra pero conserva el régimen especial de la Ley 1920/2018. Fuente del lineamiento de **12h continuas de descanso mínimo entre jornadas**. |
| **Circular 0040 de 2026** (MinTrabajo) | Exige registro riguroso y trazable de horas suplementarias por trabajador. |

**Punto crítico de diseño:** julio de 2026 tiene **dos topes legales distintos
dentro del mismo mes** (44h hasta el 14, 42h desde el 15). Por eso
`reglas_anomalia` tiene columnas `vigente_desde` / `vigente_hasta` y los
umbrales viven en la columna JSONB `parametros`. **Nunca hardcodees un umbral
en el código Python** — el motor debe consultar qué regla está vigente para
cada fecha evaluada.

### Las 7 reglas implementadas

| Código | Severidad | Qué detecta |
|---|---|---|
| `CRUCE_DE_HORARIO` | CRÍTICA | Mismo guarda con turnos que se solapan en el tiempo en dos puestos distintos. Físicamente imposible. |
| `DESCANSO_ENTRE_JORNADAS_INSUFICIENTE` | ALTA | Menos de 12h continuas entre el fin de una jornada y el inicio de la siguiente. |
| `JORNADA_DIARIA_SUPERA_12H` | ALTA | Suma de horas trabajadas en un día calendario > 12h. |
| `SEMANA_SUPERA_60H` | ALTA | Ventana móvil de 7 días con > 60h trabajadas. |
| `SIN_DESCANSO_SEMANAL` | ALTA | Ventana móvil de 7 días sin ningún día de descanso. |
| `RACHA_SIN_DESCANSO` | ALTA | Más de 6 días consecutivos trabajados. |
| `DESCUADRE_HORAS_DECLARADAS` | BAJA | El `Total Horas` declarado por SERPI ≠ suma de sus 4 categorías. |

**Ventanas móviles, no semanas naturales:** las reglas semanales se evalúan
sobre **cualquier** secuencia de 7 días consecutivos, no sobre semanas
calendario lunes-domingo. Una violación que cae a caballo entre dos semanas
naturales es igual de ilegal.

### Hallazgo estructural importante

El patrón de programación dominante es **2x2x2**: 2 días de turno diurno (E),
2 de nocturno (F), 2 de descanso, en ciclos de 6 días. Este patrón produce
~56h/semana en promedio, y cuando el ciclo se desalinea con el calendario llega
a 60-72h en una ventana de 7 días.

**Implicación:** buena parte de los hallazgos no son errores de digitación sino
consecuencia directa de que la plantilla base ya opera al límite legal. Al
reportar resultados, distingue entre anomalías puntuales (corregibles caso a
caso) y anomalías estructurales (requieren rediseñar el patrón de turnos).

---

## 4. Estructura del export de SERPI

Entender esto es indispensable para tocar el parser. El Excel **no es tabular** —
es un reporte visual con estructura jerárquica anidada.

```
Cada hoja (Sheet1..Sheet83) = un cliente
├─ Fila 1:  nombre de la empresa
├─ Fila 7:  "Año:" ... 2026 ... "Mes:" ... Julio   ← celdas separadas, no contiguas
├─ "CLIENTE: <nombre>"
├─ "PROYECTO: <nombre>"
└─ "PUESTO: <nombre>"                      ← una hoja puede tener VARIOS puestos
   ├─ fila de encabezado: col 3 = "T", luego "1\nMi", "2\nJ", ... "31\nV"
   │                       ↑ marca la fila           ↑ día \n inicial-del-día-semana
   │   Las columnas de días NO son contiguas (hay columnas vacías intercaladas).
   │   Hay que mapear día→columna leyendo el encabezado, no asumir offsets.
   │
   ├─ fila de guarda:  col 3 = número de slot (int)
   │                   col 4 = "NOMBRE APELLIDO - CEDULA"
   │                   + celdas sueltas con "Horas Diurnas Ordinarias: 116",
   │                     "Total Horas: 252", etc.
   └─ fila de turnos (la fila inmediatamente siguiente):
       cada celda = "CODIGO\nHH:MM\nHH:MM"  (ej. "E\n06:00\n18:00")
```

**Formatos de celda de turno que hay que manejar:**

| Contenido | Interpretación |
|---|---|
| `E\n06:00\n18:00` | Código + hora inicio + hora fin (caso normal) |
| `VAC` (solo texto) | Ausencia sin horario |
| `06:00\n18:00` (solo dos horas) | Turno **sin letra de código** → el ETL genera un código sintético `T_0600_1800` para no perder el registro |
| celda vacía | Día no programado (descanso) |

**Códigos de turno observados** (el catálogo `tipos_turno` se llena solo al
correr el ETL, no está hardcodeado — se detectaron 99 códigos distintos):

- **E** = diurno 12h (06:00-18:00) · **F** = nocturno 12h (18:00-06:00) — los
  dos dominantes
- **A / B / C** = turnos de 8h del esquema rotativo de tres turnos
- **D** no se usa como turno (aparentemente reservado para "Descanso")
- **ESC** (escolta), **OCA** (ocasional/flotante), **ADI** (adicional)
- Sufijos `+N` / `-N` (ej. `F+2`) = turno desplazado N horas respecto al
  estándar. **Estos son los que producen jornadas > 12h.**
- **Ausencias sin horario:** `VAC`, `INC`, `LIC`, `LICNR`

---

## 5. Arquitectura

```
   timer de systemd (04:30 diario)   ← despliegue/ [HECHO]
            │
            ▼
   pipeline_diario.py            ← encadena los tres pasos [HECHO]
            │
            ▼
   Datos de turnos
   (Excel vía RPA hoy → API SERPI mañana)
            │                     ← descargar_malla_serpi.py [HECHO]
            ▼
   ETL y normalización          ← etl_normalizacion.py [HECHO]
   (parsea códigos y horarios;
    upsert + reconciliación)
            │
            ▼
      PostgreSQL                 ← schema.sql [HECHO]
            │
            ▼
   Motor de reglas               ← motor_reglas.py [HECHO]
   (7 reglas, umbrales en BD)
            │
            ▼
   anomalias + anomalias_historial
            │
            ▼
   Capa semántica (read model)   ← vistas_reporte.sql [HECHO]
   vistas por audiencia
            │
            ▼
   api.py  ◄══ PUERTO ÚNICO ══►  nadie más abre conexiones a la BD
            │
            ▼
      dashboard.html
            │
            ▼
 el usuario corrige en SERPI ──┐
                               │
   (y al día siguiente el timer vuelve a arriba y lo confirma:
    la anomalía ya no aparece y el motor la marca RESUELTA sola)
```

**Stack:** Python + PostgreSQL + FastAPI, empaquetado en Docker y programado
con un timer de systemd.

> **n8n quedó descartado** (decisión del usuario, septiembre 2026). Una versión
> anterior de este archivo lo daba como orquestador y `n8n_auditoria_mensual.json`
> está eliminado a propósito — **no lo recrees.** `pipeline_diario.py` + timer de
> systemd hacen lo mismo con una pieza menos que mantener y con los códigos de
> salida visibles para el supervisor. Lo único que se perdió y no tiene
> reemplazo son los tres correos por audiencia (§8).

**Los datos tienen PII real de 404 trabajadores** (Ley 1581 de 2012, habeas
data), y eso condiciona el despliegue: nada de servicios que no se puedan
cubrir con un DPA, y la BD nunca expuesta a internet. Se descartó Vercel por
esto y porque además no puede correr el ETL, el motor ni Chromium. El destino
decidido es AWS (RDS PostgreSQL + la app al lado), y **hasta que esté validado
las pruebas siguen siendo locales** — no se tocan las bases de producción ni de
pruebas de la empresa.

> **Looker Studio quedó descartado.** Una versión anterior de este archivo lo
> daba como destino de los reportes de gerencia. No es viable: es un servicio de
> Google y no puede alcanzar un PostgreSQL en la red interna. Lo reemplaza el
> dashboard propio, que además hacía falta porque Looker es de solo lectura y
> aquí se necesita gestionar estados.

**Patrón: CQRS (lectura y escritura separadas).** El modelo de escritura está
normalizado para que ETL y motor sean rápidos e idempotentes; el de lectura
(`vistas_reporte.sql`) está desnormalizado y rotulado en lenguaje de negocio
para que dashboard y correos no rehagan joins ni reglas de presentación. Toda
la lógica de presentación vive en SQL, en un solo sitio.

Son `VIEW` y no `MATERIALIZED VIEW` a propósito: con ~400 guardas y ~400
anomalías/mes el join cuesta milisegundos, y materializar sólo agregaría un
`REFRESH` a la corrida diaria y la posibilidad de servir datos viejos. Si el
histórico crece a años, se materializan sin tocar a quien las consume.

### Modelo de datos

```
clientes ──< puestos ──< turnos >── guardas
                            │            │
                     tipos_turno         │
                                         ▼
              reglas_anomalia ──< anomalias
```

| Tabla | Rol |
|---|---|
| `clientes` | Cliente contratante. |
| `puestos` | Puesto de vigilancia. `hoja_origen` guarda la hoja del Excel (trazabilidad). |
| `guardas` | **PK = cédula**, nunca el nombre (los nombres se repiten y varían). |
| `tipos_turno` | Catálogo autopoblado. `categoria` ∈ {`TRABAJADO`, `AUSENCIA`}. |
| `turnos` | El hecho central. `origen` ∈ {`EXCEL_EXPORT`, `API_SERPI`}. Llave natural `(guarda_cedula, puesto_id, fecha, slot)` — `slot` distingue asignaciones concurrentes del mismo guarda en el mismo puesto el mismo día. |
| `horas_declaradas_mes` | Totales que SERPI declara por guarda-puesto-mes (para el chequeo de consistencia). |
| `reglas_anomalia` | **Reglas como datos**, con vigencia por fecha y umbrales en JSONB. Llave natural `(codigo, vigente_desde)` — una regla puede tener varias versiones vigentes en tramos distintos. También trae `naturaleza`/`responsable` (ver abajo). |
| `anomalias` | Resultado. Identidad estable por `huella`; `estado` ∈ {`ABIERTA`,`EN_REVISION`,`JUSTIFICADA`,`RESUELTA`}. |
| `anomalias_historial` | Quién cambió el estado de qué, cuándo y con qué nota. Se escribe en la misma transacción que el cambio. |

### Taxonomía: quién puede actuar sobre cada regla

`naturaleza` y `responsable` viven en `reglas_anomalia` (son datos, no `if`s) y
son **el eje sobre el que se arma el dashboard**: cada audiencia ve su bandeja.

| Naturaleza | Reglas | Responsable | Julio 2026 |
|---|---|---|---|
| `PUNTUAL` — error de asignación, se corrige moviendo un turno | Cruce, Jornada >12h, Descanso <12h | `PROGRAMADOR` | 176 |
| `ESTRUCTURAL` — consecuencia del patrón 2x2x2, exige rediseñar la plantilla | Semana >60h, Sin descanso, Racha | `GERENCIA` | 189 |
| `ADMINISTRATIVA` — el turno está bien, el registro no cuadra | Descuadre de horas | `NOMINA` | 21 |

Que lo estructural (189) supere lo puntual (176) es el hallazgo de negocio más
importante del proyecto: **la mayoría no son errores de digitación**. El informe
tiene que decirlo, no esconderlo.

### Los cuatro estados

| Estado | Significado | Quién lo pone |
|---|---|---|
| `ABIERTA` | Detectada, nadie la ha mirado | El motor |
| `EN_REVISION` | Alguien la trabaja / va a corregirla en SERPI | Usuario |
| `JUSTIFICADA` | Revisada y se acepta así. **Nota obligatoria** | Usuario |
| `RESUELTA` | Ya no aparece en el cargue más reciente | **Solo el motor** |

**`RESUELTA` no se puede poner a mano, y es deliberado:** significa «verificado
contra los datos», no «alguien dijo que sí». Cuando el usuario corrige en SERPI
y se recarga la malla, la violación deja de existir, el motor no la regenera y
la marca `RESUELTA` sola — o sea, **le confirma al usuario que su corrección
llegó**. Nadie tiene que acordarse de volver a cerrar nada.

El caso inverso también está cubierto: si una anomalía dada por resuelta vuelve
a aparecer, `reabrir_reincidentes` la devuelve a `ABIERTA`. Sin eso, una
violación activa quedaría escondida bajo un estado que dice que ya se arregló —
lo peor que puede pasar en una herramienta de cumplimiento.

---

## 6. Estructura del proyecto

```
turnos_etl/
├── CLAUDE.md               ← este archivo
├── README.md               instrucciones de instalación y uso
├── schema.sql              DDL de las 8 tablas
├── seed_reglas.sql         precarga de las 7 reglas (opcional)
├── migracion_002_dashboard.sql  huella + taxonomía + historial
├── migracion_003_estados.sql    estados del proceso real
├── vistas_reporte.sql      capa semántica (read model) por audiencia
├── etl_normalizacion.py    ETL Excel → PostgreSQL (upsert + reconciliación)
├── motor_reglas.py         motor de reglas: turnos → anomalias
├── api.py                  FastAPI — puerto único (dashboard + integraciones)
├── dashboard.html          3 bandejas; lo sirve la propia API en /
├── descargar_malla_serpi.py     RPA de descarga (Playwright)
├── pipeline_diario.py      corrida diaria: descarga + ETL + motor
├── Dockerfile              dos targets: api (sin navegador) y pipeline
├── docker-compose.yml      api · pipeline (profile manual) · postgres (local)
├── despliegue/             systemd (.service + .timer), DESPLIEGUE.md,
│                          CLOUDFLARE.md
├── test_reglas.py          pruebas de los detectores (no necesitan BD)
├── test_api_identidad.py   pruebas de MODO_IDENTIDAD (SI necesitan BD)
├── requirements.txt        openpyxl, psycopg2-binary, python-dotenv, fastapi, uvicorn
├── .env.example            plantilla de conexión
└── .env                    credenciales reales — NO versionar
```

### Cómo correrlo

```bash
psql -U turnos_app -d turnos -f schema.sql
psql -U turnos_app -d turnos -f seed_reglas.sql
psql -U turnos_app -d turnos -f vistas_reporte.sql
python etl_normalizacion.py --archivo RepProgramacion.xlsx
python motor_reglas.py                    # o --desde/--hasta para acotar un mes
uvicorn api:app --host 0.0.0.0 --port 8000  # dashboard en http://servidor:8000/
```

Los tres `.sql` y los dos scripts son **idempotentes**: correrlos de nuevo no
duplica nada.

En operación esos tres pasos no se corren a mano: los encadena
`pipeline_diario.py` (descarga + ETL + motor), que es lo que dispara el timer de
systemd a diario. Sus **códigos de salida son un contrato** con el supervisor y
no se deben colapsar a 0/1: `2` descarga, `3` ETL roto, `4` ETL abortado por el
umbral de borrado (**requiere revisión humana**, la BD quedó intacta a
propósito), `5` motor, `6` ya había otra corrida. El `3` y el `4` significan
cosas opuestas para quien recibe la alerta y por eso están separados.

**Salida esperada con la malla de julio 2026** (úsala como test de regresión —
si tocas el parser y estos números cambian, algo se rompió):

```
clientes: 67 · puestos: 164 · guardas: 404
tipos_turno: 99 · turnos: 8821 · horas_declaradas: 738
```

---

## 7. Errores conocidos / trampas

Cosas que ya costaron trabajo descubrir. No las repitas.

- **`NULL` rompe las restricciones `UNIQUE` en PostgreSQL.** La primera versión
  de la llave natural de `turnos` incluía `hora_inicio`. Como los registros de
  ausencia (`VAC`, `INC`, `LIC`, `LICNR`) tienen `hora_inicio = NULL`, y en SQL
  `NULL <> NULL`, el `ON CONFLICT` nunca disparaba y esas filas **se duplicaban
  en cada corrida mensual**. La llave correcta es
  `(guarda_cedula, puesto_id, fecha)` — sin horas. Ya corregido; no lo
  revientes.

- **Turnos que cruzan medianoche.** Un turno `F` va de 18:00 a 06:00. Al
  calcular duración, si `hora_fin <= hora_inicio` hay que sumar 24h. Toda
  comparación temporal debe hacerse sobre **timestamps absolutos**
  (`fecha + hora`), nunca sobre `TIME` sueltos.

- **Distinguir pausa intradía de descanso entre jornadas.** Si un guarda tiene
  dos bloques el mismo día, el hueco entre ellos es un intermedio de jornada
  (Art. 167 CST), **no** una violación de descanso entre jornadas. El descanso
  de 12h se mide entre el **fin de la última actividad de un día** y el
  **inicio de la primera del siguiente día activo**. Confundirlos infla los
  falsos positivos dramáticamente (en una versión temprana del análisis
  generó 216 hallazgos falsos donde el número real era 95).

- **Un guarda puede estar en varios clientes y puestos en el mismo mes.** 34 de
  los 92 guardas de Harinera del Valle rotaron entre puestos. **Todo análisis
  de anomalías debe consolidarse por cédula a través de todos los puestos**,
  nunca puesto por puesto — los cruces de horario reales aparecen justamente
  entre puestos distintos.

- **Un `puesto` no es una hoja.** Una hoja puede contener varios puestos
  (Sheet57 tiene ~20). Y un cliente puede estar repartido en varias hojas
  (Harinera del Valle ocupa 5). Nunca uses el nombre de la hoja como
  identificador de nada.

- **Un guarda puede tener más de una fila (slot) en el mismo puesto el mismo
  día — y eso NO es un duplicado a colapsar.** El Excel puede asignar al
  mismo guarda dos bloques distintos dentro del mismo puesto (p.ej. su turno
  regular en el slot 1 más un turno `ADI` en el slot 2 que se le solapa).
  La primera versión de este ETL usaba `(guarda_cedula, puesto_id, fecha)`
  como llave natural de `turnos`, así que el `ON CONFLICT` colapsaba esas
  asignaciones concurrentes reales — y con ellas, cruces de horario
  **CRÍTICOS** dentro del mismo puesto (verificado en la malla de julio 2026:
  un guarda de Harinera del Valle tenía, el día 9, dos turnos `A 06:00-14:00`
  solapados en el mismo puesto —"8.PORTERIA DAGUA 8 HORAS"— invisibles para el
  motor hasta este fix. La cédula se omite aquí a propósito: este archivo se
  versiona y no puede llevar PII). La llave correcta, ya aplicada, es
  `(guarda_cedula, puesto_id, fecha, slot)`, donde `slot` es el número de
  fila/slot que trae la columna C del Excel para cada bloque guarda-puesto.
  Por eso `detectar_cruce_horario` en `motor_reglas.py` ya NO excluye los
  cruces dentro del mismo puesto — antes tenía que hacerlo porque esas filas
  ni siquiera coexistían en `turnos`.

- **Los datos son reales y contienen PII** (nombres y cédulas de trabajadores
  reales). No los subas a servicios externos, no los incluyas en issues, y
  mantén `.env` y cualquier export fuera del control de versiones. Es también
  la razón de que todo el stack sea self-hosted.

- **El motor duplicaba las anomalías ya gestionadas.** `borrar_abiertas` solo
  borraba las `ABIERTA`, pero el `INSERT` posterior no sabía que la violación ya
  existía en otro estado y la volvía a crear (verificado: un guarda pasó de 9 a
  10 filas al re-correr el motor). Resuelto con `anomalias.huella`, derivada de
  **llaves de negocio**: `regla + cédula + clave natural del detector`.
  - **Nunca metas `detalle` ni ids de `turnos` en la huella.** El primero cambia
    si se reescribe un mensaje; los segundos cambian si se recarga el ETL
    (son `BIGSERIAL`). Cualquiera de los dos reintroduce el duplicado. Está
    verificado que la huella sobrevive a borrar y recargar los turnos.
  - Se usó huella y no un `UNIQUE` de columnas porque `puesto_id` es `NULL` en
    casi todas las reglas, y en Postgres `NULL <> NULL` haría que el `UNIQUE`
    nunca dispare — la misma trampa que ya cayó en `turnos`.
  - Cada detector aporta su `clave`: la fecha para las diarias, el par de
    bloques que chocan para los cruces, el periodo `YYYY-MM` para las que son
    «una fila por guarda y mes».

- **Los barridos de estado tienen que acotarse igual que el escaneo.**
  `marcar_resueltas` y `reabrir_reincidentes` reciben el mismo filtro de guarda
  y rango de fechas que `borrar_abiertas` (helper `_acotar`). Si no, correr el
  motor con `--cedula` o `--desde/--hasta` marcaría como resuelto todo lo demás,
  que ni siquiera se evaluó.

- **La identidad del historial sale de la SESION, nunca del cliente.** La
  autenticacion es propia (decision explicita del usuario, septiembre 2026: sin
  proveedores externos). `MODO_IDENTIDAD` tiene dos valores:
  - `SESION` (default): login propio. **Ninguna ruta responde sin sesion**, ni
    `/`, ni `/docs`, ni `/openapi.json`. Lo garantiza el middleware
    `muro_de_sesion`, no una lista de rutas protegidas una por una — con la
    lista, agregar un endpoint y olvidar la dependencia lo dejaria abierto y
    nada fallaria.
  - `DECLARATIVA`: sin login, se cree `X-Usuario`. **Solo desarrollo local**; la
    API avisa por consola al arrancar.
  - **Nunca aceptes `X-Usuario` como alternativa en modo `SESION`.** Seria el
    agujero que todo esto cierra: bastaria enviar esa cabecera para firmar una
    justificacion con el nombre de otro, y `anomalias_historial` puede terminar
    sustentando una respuesta ante el Ministerio del Trabajo. Un historial que
    parece confiable y no lo es es peor que no tener historial. Protegido por
    `test_api_identidad.py`.
  - **Las sesiones viven en tabla, no en un JWT**, y eso no es accidental: un
    JWT firmado vale hasta que expira y no se puede anular, asi que desactivar
    una cuenta no cortaria el acceso. Con la tabla se corta en la siguiente
    peticion. Si alguien propone cambiarlo "para simplificar", la prueba lo
    detecta.
  - De la sesion solo se guarda el **SHA-256** del token, y de la clave solo su
    hash **bcrypt**. Un volcado de la base no entrega ni claves ni sesiones
    vivas.

- **La autorizacion por URL vive en una TABLA y falla cerrado.** `PERMISOS` en
  `api.py` dice que roles alcanzan cada ruta, y el mismo middleware la aplica.
  - Una ruta que no este en la tabla **se niega a todo el mundo**. Agregar un
    endpoint y olvidar clasificarlo lo deja inaccesible, nunca expuesto. Hay una
    prueba que recorre las rutas declaradas por la API y falla si alguna quedo
    sin asignar: la tabla no se puede quedar atras.
  - `/pipeline/ejecutar` es **solo ADMIN**. Recarga la base entera y recibe la
    ruta del archivo en la peticion: no es una consulta, es mantenimiento. Antes
    lo podia disparar cualquiera con sesion.
  - **Las pestanas del dashboard se derivan de esa misma tabla** (`/identidad`
    devuelve `paneles`). No repitas la lista de roles en el JavaScript: con dos
    tablas, una cambia y la otra no, y el dashboard ofreceria una pestana que el
    servidor va a rechazar. Esconder una pestana no es la proteccion — el 403
    del servidor lo es.

- **`motor_reglas.py` escanea con el umbral de la versión de regla más
  reciente**, no una por cada fecha. Cada violación puntual sí se guarda con
  la versión de regla vigente en su `fecha_referencia` (así que una regla
  retirada no genera anomalías fuera de su vigencia), pero si en el futuro
  agregas una segunda versión de una regla con umbral distinto para un tramo
  de fechas ya cerrado (p.ej. si el tope semanal de referencia 44h/42h se
  modela como regla en vez de solo informativo), el escaneo de ventanas
  móviles/rachas no lo va a partir solo — corre el motor por separado para
  cada tramo con `--desde`/`--hasta` (ya implementados). No es un problema hoy
  porque `seed_reglas.sql` solo tiene una versión vigente por regla.

- **`SIN_DESCANSO_SEMANAL` y `RACHA_SIN_DESCANSO` parten de la misma condición
  subyacente** (rachas de días consecutivos trabajados) pero se reportan con
  convenciones distintas — verificado contra el análisis manual de
  referencia, no es una decisión libre:
  - `RACHA_SIN_DESCANSO`: **una fila por racha** de más de `umbral_dias` días
    consecutivos trabajados.
  - `SIN_DESCANSO_SEMANAL`: **una sola fila por guarda para todo el mes**,
    que cuenta cuántas ventanas móviles de 7 días sin descanso hubo en total
    (una racha de N≥7 días contiene N-6 ventanas) y referencia la primera.
  - `SEMANA_SUPERA_60H` sigue la misma convención de "una fila por guarda
    para todo el mes": cuenta cuántas ventanas de 7 días superaron el
    umbral y referencia la de más horas.
  Si tocas alguno de estos tres detectores, no cambies la convención de
  agregación sin volver a correr `motor_reglas.py` contra
  `Deteccion de anomalias Julio.xlsx` para confirmar que sigue coincidiendo.

- **«Una fila por guarda **y mes**» significa por mes, no por rango
  evaluado.** Es la misma frase del punto anterior, pero la primera
  implementación la leyó mal y agregaba sobre **todo** lo que hubiera en la
  base: `detectar_semana_excesiva` y `detectar_sin_descanso_semanal`
  devolvían **una sola fila por guarda**, con la clave del mes donde caía la
  semana pico. Con un solo mes cargado las dos lecturas son idénticas —por
  eso la validación de julio pasaba y el bug quedó latente—; pero al cargar
  agosto y septiembre la semana pico se corría de mes, la `clave` cambiaba y
  **la huella del mes anterior desaparecía**. Y como `marcar_resueltas` da
  por `RESUELTA` toda huella que ya no aparece, el sistema le decía al
  usuario *«tu corrección llegó»* sobre una violación que nadie tocó — el
  peor fallo posible aquí, y justo el que `reabrir_reincidentes` existe para
  evitar. Medido: se perdieron 6 hallazgos de julio al cargar agosto.
  - Ya corregido: **cada ventana móvil pertenece al mes de su día de
    INICIO**, y los tres detectores de ventana/racha respetan esa misma
    convención. Protegido por pruebas en `test_reglas.py` (agosto se
    construye deliberadamente peor que julio para que la fila migre si
    alguien revierte esto).
  - **El contrato de los 386 solo aplica con julio como único mes en la
    base.** `cargar_turnos_guarda` NO filtra por fecha: `--desde/--hasta`
    acota qué hallazgos se escriben, no qué datos se evalúan. Con agosto
    cargado, una ventana que arranca el 28 de julio por fin se ve completa
    (antes se truncaba el 31) y aparecen violaciones reales que no eran
    visibles: julio pasa de 386 a 407. Eso es **más** correcto, no menos. Si
    vas a re-validar los 386, hazlo alimentando los detectores solo con
    julio, no corriendo el motor con `--desde/--hasta` sobre una base que ya
    tiene más meses.

- **En el contenedor, `/app` NO es escribible, y eso es a propósito.** El
  código pertenece a root y el proceso corre como `turnos` (uid 10001), así
  que los únicos directorios donde se puede escribir son `logs/` y
  `Reportes mensuales/`, que además son volúmenes persistentes. Dos versiones
  de `pipeline_diario.py` cayeron aquí: el candado estaba en la raíz del
  proyecto y la malla se descargaba a un `RepProgramacion.xlsx` fijo, también
  en la raíz. Ambos daban `PermissionError` solo dentro del contenedor, no en
  local. **Cualquier archivo que un script genere en tiempo de ejecución tiene
  que ir a uno de esos dos directorios.**
  - De paso, la malla ya no se descarga a un nombre fijo que se sobreescribe,
    sino a `Reportes mensuales/RepProgramacion_<desde>_a_<hasta>_<AAAAMMDD>.xlsx`.
    Un archivo único reescrito a diario destruye la evidencia de qué datos se
    evaluaron cada día, que es justo lo que sustenta la trazabilidad.

- **La zona horaria hay que fijarla en DOS sitios.** `TZ` en el `Dockerfile`
  decide qué día cree que es el contenedor al calcular el rango de fechas;
  `timedatectl` en el host decide a qué hora dispara `OnCalendar` de systemd
  (que usa la zona del sistema, no UTC). Una EC2 recién creada viene en UTC:
  sin el primer ajuste, una corrida de las 19:00 de Bogotá del día 31 pide la
  malla del mes entrante y el rango se corre un mes completo; sin el segundo,
  el timer de las 04:30 se dispara a las 23:30 del día anterior.

- **El volumen de PostgreSQL va en `/var/lib/postgresql`, no en
  `/var/lib/postgresql/data`.** Desde la imagen 18 los datos viven en un
  subdirectorio por versión mayor (para que `pg_upgrade --link` no cruce el
  límite del montaje). Con la ruta antigua el contenedor arranca, detecta datos
  en un *"unused mount/volume"* y entra en bucle de reinicio — y el mensaje no
  dice cuál es el arreglo.

- **En systemd, las rutas con espacios van entre comillas.** `ReadWritePaths`
  separa por espacios, así que `/opt/turnos/Reportes mensuales` sin comillas se
  lee como dos rutas, el arranque falla con *"Failed to parse"* y **el timer
  queda muerto en silencio** — que en una auditoría diaria es el peor modo de
  fallo posible.

---

## 8. Próximos pasos

1. ~~**Motor de reglas**~~ — **hecho y validado** (`motor_reglas.py`). Corrido
   contra la malla real de julio 2026: reproduce **exactamente** los 386
   hallazgos / 166 guardas / 7 reglas del análisis manual de referencia
   (`Deteccion de anomalias Julio.xlsx`), tras corregir la llave natural de
   `turnos` para incluir `slot` (ver "Errores conocidos"). Cero
   discrepancias sin explicar.
2. ~~**API propia**~~ — **hecha** (`api.py`). Puerto único: `/anomalias`
   (filtrable y paginado), `/anomalias/{id}` (detalle con los turnos que la
   originaron + historial), `PATCH /anomalias/{id}` (gestión con auditoría),
   `/kpi`, `/clientes`, `/nomina`, `/estructural`, `/reglas`,
   `/pipeline/ejecutar` y `/informe/mensual` (payload único de los correos
   por audiencia, todavía sin remitente — ver §8).
3. ~~**Dashboard**~~ — **hecho** (`dashboard.html`, lo sirve la propia API).
   Tres bandejas por audiencia. La del programador se agrupa **por puesto**, no
   por severidad: en SERPI se navega cliente → puesto, así que agrupar así
   permite entrar una vez a cada puesto y corregir todo lo suyo de una pasada.
   En julio, 176 hallazgos puntuales se agrupan en 50 visitas, y las 5 primeras
   cubren 77 (44%).
4. ~~**Orquestación**~~ — **hecha, y n8n quedó descartado** (decisión del
   usuario, septiembre 2026). El flujo `n8n_auditoria_mensual.json` está
   eliminado a propósito: **no lo recrees.** Lo reemplaza `pipeline_diario.py`
   disparado por un timer de systemd (`despliegue/`), que hace lo mismo con una
   pieza menos que mantener y con los códigos de salida visibles. Los tres
   correos por audiencia que hacía n8n **todavía no tienen reemplazo** — el
   payload que los alimentaba sigue existiendo en `/informe/mensual`.
   **Cubre el mes SIGUIENTE**: el rango por defecto son dos meses (actual +
   entrante) porque la malla se carga del 25 al 27 para el mes que viene; así
   aparece el mismo día que la cargan y el programador tiene 3–4 días para
   corregir **antes de que entre en vigencia** — el salto de detección
   retrospectiva a prevención.
5. ~~**Descarga automática diaria de la malla**~~ — **hecha** (`descargar_malla_serpi.py`).
   La API general de SERPI (usuario admin + Secret Key + token, ver §1) es solo
   el núcleo ERP contable/comercial: no expone ningún endpoint de turnos,
   programación ni asistencia. Mientras el proveedor no la construya, este
   script automatiza lo mismo que hace un usuario a mano (login web + reporte
   de programación con todos los filtros vacíos = todos los clientes), vía
   Playwright. SERPI entrega el export como `.xls` binario antiguo (no
   `.xlsx`), así que el script lo convierte antes de entregárselo al ETL. El
   reporte es lento en el servidor de SERPI (varios minutos incluso para un
   solo día), el timeout por defecto es generoso a propósito.
   **No lo programes directamente:** lo invoca `pipeline_diario.py`, que además
   hace el ETL y el motor.
6. ~~**Corrida diaria y empaquetado**~~ — **hechos y probados en contenedor**
   (`pipeline_diario.py`, `Dockerfile`, `docker-compose.yml`, `despliegue/`).
   Esto es lo que cierra el ciclo central del §2: el usuario corrige en SERPI y
   a la mañana siguiente el motor marca `RESUELTA` sola. Verificado de punta a
   punta contra un PostgreSQL 18 vacío (los cinco `.sql` en orden → 9 tablas,
   7 vistas, 7 reglas) y con la cadena completa: agosto 303 hallazgos y
   septiembre encima 244, idéntico al resultado en local. La red de seguridad
   del ETL también está probada de verdad: forzando el aborto, el pipeline sale
   con `4`, **no** corre el motor y la BD queda intacta.
7. **Jornada de referencia 42h — pendiente, solo desde agosto 2026.** Julio ya
   se facturó y pagó con la regla vieja de 44h, así que **julio no se toca**.
   La regla está escrita y comentada al final de `seed_reglas.sql`; para
   activarla, descoméntala y corre `python motor_reglas.py --desde 2026-08-01`.
   Va como `ADMINISTRATIVA`/`NOMINA` y no como violación: superar la jornada de
   referencia **no es ilegal** en vigilancia (la Ley 1920 permite hasta 60h con
   suplementarias, y ese tope sí lo cubre `SEMANA_SUPERA_60H`); lo que exige es
   pagarlas con recargo y registrarlas. Es asunto de liquidación.
8. ~~**Autenticación real**~~ — **hecha, y es propia** (septiembre 2026).
   Decision explicita del usuario: nada de proveedores externos, todo dentro del
   sistema. Login con claves bcrypt, sesiones en servidor, roles que reutilizan
   la taxonomia de las bandejas (PROGRAMADOR / NOMINA / GERENCIA + ADMIN),
   bloqueo por intentos fallidos y bitacora de accesos.
   Piezas: `autenticacion.py`, `migracion_004_usuarios.sql`,
   `gestionar_usuarios.py`, `login.html`, y la pestana «Cuentas» del dashboard.
   El ADMIN crea las cuentas y entrega una clave temporal que el sistema obliga
   a cambiar al primer ingreso.
   Probado con `test_api_identidad.py` (44 comprobaciones): ninguna ruta
   responde sin sesion, los roles se respetan, desactivar corta el acceso al
   instante y la cabecera no puede suplantar a la sesion.
   **Cloudflare Access quedo descartado** y su guia eliminada — no la recrees.
9. **Despliegue en AWS — pendiente.** Decidido (septiembre 2026): **se queda en
   PostgreSQL**, sobre RDS, en la misma cuenta de AWS que el resto. Se evaluó
   meter el esquema en el MySQL de la empresa (`bitacorapp_staging`, el único
   donde el usuario disponible puede crear tablas) y se descartó con números:
   ~68 construcciones de PostgreSQL a reescribir —18 de ellas estructurales
   (`BIGINT[]` + `unnest`, `RETURNING`, `xmax = 0`)—, 75 ms de latencia por
   consulta contra ese host, y el rastro de auditoría (`anomalias_historial`)
   quedaría dentro de un esquema Laravel vivo, escribible por cualquier
   desarrollador de la bitácora. De `bitacorapp` se lee, no se escribe: sirve
   para el cruce de asistencia por cédula.
   El paso a paso completo —seis fases, con criterios de aceptación
   verificables y costos— está en **`despliegue/DESPLIEGUE.md`**. Léelo antes
   de proponer cambios de infraestructura.
   Falta: crear el RDS, subir las imágenes, y **las pruebas siguen siendo
   locales hasta que esto esté validado** (instrucción explícita del usuario:
   no tocar las bases de producción ni de pruebas de la empresa).
10. **Batch de los INSERT del ETL.** Hoy inserta fila por fila. En local no
    importa (segundos), pero contra una BD remota son ~8.400 idas y vueltas por
    mes. Si el RDS queda en la misma VPC deja de ser urgente; si alguna vez la
    BD queda lejos, es lo primero que hay que arreglar.
12. **Los tres correos por audiencia** que hacía el flujo de n8n eliminado. El
    payload sigue en `/informe/mensual`.
13. **Migración a la API de SERPI** cuando exista: cambiar la fuente del ETL,
    marcar `origen = 'API_SERPI'` y retirar `descargar_malla_serpi.py` (el RPA
    deja de ser necesario). Nada más debería cambiar.

---

## 9. Convenciones

- **Código, nombres de variables y comentarios en español**, consistente con el
  dominio (`guarda`, `puesto`, `turno`, `malla`). Los términos del negocio no se
  traducen.
- **El ETL debe ser idempotente.** Correrlo dos veces con el mismo archivo no
  puede duplicar filas. Verifícalo tras cualquier cambio. Lo mismo aplica a los
  tres `.sql` y al motor.
- **Tras tocar los detectores, corre `python test_reglas.py`** (rápido, sin BD)
  y luego `python motor_reglas.py` contra la malla de julio: debe seguir dando
  **386 hallazgos / 166 guardas**. Ese número es el contrato con el análisis
  manual de referencia.
- **Tras tocar `api.py` o el `dashboard.html`, corre
  `python test_api_identidad.py`.** Esa prueba SI necesita BD, y protege lo
  unico del sistema que puede fallar sin que se note: que la identidad escrita
  en el historial sea la verificada. Con `--con-escritura` cubre tambien el
  camino de escritura (modifica una anomalia real y la restaura).
- **Y tras tocar `dashboard.html`, regenera el informe autónomo**
  (`python generar_informe.py --periodo 2026-07`). `generar_informe.py:69-107`
  lo reutiliza sustituyendo texto **por coincidencia exacta** (`html.index` de
  `async function pedir(...)` y de `function pedirToken(){`, más el HTML del
  botón de identificarse). Renombrar o mover cualquiera de esos anclajes rompe
  el informe, y el error no aparece hasta que alguien lo abre.
- **Reglas como datos, no como código.** Cualquier umbral nuevo va a
  `reglas_anomalia`, no a un `if`.
- **Trazabilidad ante todo.** Este sistema puede terminar sustentando una
  respuesta ante el Ministerio del Trabajo: toda anomalía debe poder rastrearse
  hasta los turnos concretos que la originaron (`turnos_involucrados`) y la
  regla concreta que se aplicó (`regla_id`).
