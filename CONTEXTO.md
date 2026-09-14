# Sistema de detección de anomalías en mallas de turnos

Contexto para retomar el proyecto en otra conversación. Estado a 31 de agosto
de 2026.

## 1. El problema de negocio

**Empresa:** Seguridad Shatter de Colombia LTDA BIC — vigilancia y seguridad
privada, Santiago de Cali. Opera ~67 clientes, ~164 puestos de vigilancia y
~404 guardas.

La malla mensual de turnos se carga **a mano** en SERPI (el ERP de un proveedor
externo) entre el 25 y el 27 de cada mes, para el mes siguiente. SERPI exporta
a Excel pero **no importa**, y aunque avisa de duplicidades y posibles
incumplimientos, **permite continuar bajo responsabilidad de la
administración** — o sea, las alertas se ignoran con frecuencia. Resultado: la
malla publicada contiene incumplimientos legales que nadie audita de forma
sistemática.

Un análisis manual de julio de 2026 (8.821 turnos-día) encontró **386
hallazgos** que afectan a **166 de 404 guardas (41%)**, incluidos 10 cruces de
horario físicamente imposibles de cumplir. Este proyecto automatiza esa
auditoría.

**Por qué existe este repo y no una integración directa con SERPI:** el módulo
de turnos de SERPI no tiene API. El gerente de SERPI indicó que, cuando no
existe API para un módulo, primero hay que definir el conjunto de datos
requerido y **validar el modelo con datos reales**; solo entonces se solicita el
desarrollo. Este proyecto ES esa validación, y su esquema es el insumo técnico
para pedir formalmente la API de turnos.

## 2. Marco legal (no son reglas arbitrarias)

| Norma | Qué aplica |
|---|---|
| Ley 1920/2018 Art. 7 ("ley del vigilante") | Jornada 8h/día, hasta 4h suplementarias, **máx. 12h diarias**, **máx. 60h semanales**. Tope absoluto. |
| Ley 2101/2021 | Jornada *de referencia*: 44h hasta 14/07/2026, **42h desde 15/07/2026**. |
| Art. 172 y 175 CST | Descanso semanal mínimo 24h continuas; no diferible más de 6 días. |
| Art. 167 CST | La jornada puede tener un intermedio que **no computa**. |
| Ley 2466/2025 Art. 13 | Vigilancia exceptuada del régimen general de extras, conserva el especial de la 1920. Base del descanso mínimo de 12h entre jornadas. |
| Circular 0040/2026 MinTrabajo | Registro riguroso y trazable de horas suplementarias. |

### Las 7 reglas implementadas

| Código | Severidad | Qué detecta |
|---|---|---|
| `CRUCE_DE_HORARIO` | CRÍTICA | Turnos del mismo guarda que se solapan. Imposible de cumplir. |
| `DESCANSO_ENTRE_JORNADAS_INSUFICIENTE` | ALTA | Menos de 12h entre el fin de una jornada y el inicio de la siguiente. |
| `JORNADA_DIARIA_SUPERA_12H` | ALTA | Más de 12h en un día calendario. |
| `SEMANA_SUPERA_60H` | ALTA | Ventana móvil de 7 días con más de 60h. |
| `SIN_DESCANSO_SEMANAL` | ALTA | Ventana móvil de 7 días sin ningún descanso. |
| `RACHA_SIN_DESCANSO` | ALTA | Más de 6 días consecutivos trabajados. |
| `DESCUADRE_HORAS_DECLARADAS` | BAJA | El `Total Horas` de SERPI ≠ suma de sus 4 categorías. |

**Ventanas móviles, no semanas naturales:** se evalúa *cualquier* secuencia de 7
días consecutivos. Una violación a caballo entre dos semanas calendario es
igual de ilegal.

## 3. El hallazgo de negocio más importante

El patrón de programación dominante es **2×2×2**: 2 días diurnos (E), 2
nocturnos (F), 2 de descanso, en ciclos de 6 días. Produce ~56h/semana y, cuando
el ciclo se desalinea con el calendario, llega a 60–72h en una ventana de 7 días.

Por eso la taxonomía por responsable es el eje de todo:

| Naturaleza | Reglas | Responsable | Julio 2026 |
|---|---|---|---|
| **PUNTUAL** — error de asignación, se corrige moviendo un turno | Cruce, Jornada >12h, Descanso <12h | Programador | 176 |
| **ESTRUCTURAL** — consecuencia del patrón 2×2×2 | Semana >60h, Sin descanso, Racha | Gerencia | 189 |
| **ADMINISTRATIVA** — el turno está bien, el registro no cuadra | Descuadre de horas | Nómina | 21 |

**Que lo estructural (189) supere lo puntual (176) es la conclusión clave: la
mayoría no son errores de digitación.** Son consecuencia de que la plantilla base
ya opera al límite legal. Corregirlos caso a caso no los elimina; vuelven al mes
siguiente.

Dos patrones más que salieron de los datos:
- **Los 21 descuadres de nómina son sistemáticos, no aleatorios:** 20 de 21 son
  exactamente **+1,0 hora**, y en 20 de 21 el total declarado sí coincide con lo
  programado. O sea, el Total está bien y una de las cuatro categorías pierde una
  hora al desglosar. Apunta a un redondeo del propio SERPI, no a digitación.
- **Un solo puesto concentra 35 de los 176 hallazgos puntuales**, siempre el
  mismo par: jornadas de 13h con descansos de 11h. No son 35 errores distintos;
  es un horario mal diseñado para ese puesto.

## 4. El ciclo de trabajo real

El sistema **no corrige nada y no abre tickets**. Su trabajo es que identificar
el problema y ubicarlo en SERPI sea rápido:

```
el dashboard muestra qué está mal y dónde
   → el usuario entra a SERPI y corrige la malla
   → el siguiente cargue confirma que quedó corregido
```

> Corrección de un malentendido anterior: el módulo Soporte>Tickets de SERPI
> sirve para escalar incidencias con los **desarrolladores del proveedor**, no
> para gestionar turnos. No hay integración de tickets.

### Los cuatro estados

| Estado | Significado | Quién lo pone |
|---|---|---|
| `ABIERTA` | Detectada, nadie la ha mirado | El motor |
| `EN_REVISION` | Alguien la trabaja / va a corregirla en SERPI | Usuario |
| `JUSTIFICADA` | Revisada y se acepta así. Nota obligatoria | Usuario |
| `RESUELTA` | Ya no aparece en el cargue más reciente | **Solo el motor** |

`RESUELTA` **no se puede poner a mano**, y es deliberado: significa «verificado
contra los datos», no «alguien dijo que sí». Cuando el usuario corrige en SERPI
y se recarga la malla, la violación desaparece, el motor no la regenera y la
marca resuelta sola — le confirma al usuario que su corrección llegó. Si una
anomalía dada por resuelta reaparece, el motor la reabre como `ABIERTA`; sin eso,
una violación activa quedaría escondida bajo un estado que dice que ya se arregló.

## 5. Arquitectura

Todo **self-hosted en servidor propio**. Es una decisión consciente: hay PII real
de 404 trabajadores y no puede salir de la empresa (Ley 1581 de 2012, habeas
data). Por eso se descartó Looker Studio (es de Google y no alcanza un servidor
interno) y por eso no se despliega en Vercel.

```
Excel de SERPI (hoy) → API de SERPI (cuando exista)
      ↓
etl_normalizacion.py          idempotente, upsert por llave natural
      ↓
Modelo de ESCRITURA           clientes · puestos · guardas · turnos
      ↓                       reglas_anomalia (reglas como datos)
motor_reglas.py  ─────────→   anomalias + anomalias_historial
      ↓
Modelo de LECTURA (CQRS)      vistas_reporte.sql — 7 vistas por audiencia
      ↓
api.py  ◄══ PUERTO ÚNICO ══►  nadie más abre conexiones a la BD
      ↓                ↓
dashboard.html      n8n (cron + 3 correos)
      ↓
el usuario corrige en SERPI
```

**Stack:** Python 3.14 + PostgreSQL 18 + FastAPI + n8n. Sin framework de front:
el dashboard es un HTML con JS plano que sirve la propia API.

**Patrones aplicados y por qué:**
- **CQRS.** Escritura normalizada para que ETL y motor sean rápidos e
  idempotentes; lectura desnormalizada y rotulada en lenguaje de negocio para
  que dashboard y correos no rehagan joins ni presentación.
- **Vistas normales, no materializadas.** ~400 anomalías/mes: el join cuesta
  milisegundos. Materializar solo añadiría un `REFRESH` al flujo y riesgo de
  servir datos viejos.
- **Puerto único.** Dashboard y n8n consumen la misma API.
- **Reglas y taxonomía como datos, no `if`s.** Umbrales en JSONB con vigencia
  por fecha (`vigente_desde` / `vigente_hasta`).

## 6. Estado actual — todo verificado contra datos reales

| Componente | Estado |
|---|---|
| `etl_normalizacion.py` | Excel → PostgreSQL. Idempotente (verificado en 3 corridas) |
| `motor_reglas.py` | 7 reglas. **Reproduce exactamente 386 hallazgos / 166 guardas** |
| `vistas_reporte.sql` | 7 vistas por audiencia |
| `api.py` | FastAPI, 17 rutas. Probada en ejecución |
| `dashboard.html` | 3 bandejas. Verificado con capturas reales |
| `n8n_auditoria_mensual.json` | 15 nodos + 3 correos, validados contra el payload real |
| `generar_informe.py` | Informe autónomo en un solo HTML para reuniones |
| `test_reglas.py` | 18 pruebas de los detectores, sin BD |

**Cifras de la carga de julio 2026 (test de regresión):**
`67 clientes · 164 puestos · 404 guardas · 99 tipos de turno · 8.821 turnos ·
738 filas de horas declaradas` → **386 hallazgos / 166 guardas**.

**Estado de la base ahora mismo:** 386 anomalías, todas `ABIERTA`, historial
vacío. Nadie ha gestionado nada todavía.

**Repositorio:** https://github.com/SoyRoman/ShatterTurnosAnomalias — solo
código. Los dos Excel con PII, el `.env` y los informes generados están
excluidos por `.gitignore`, verificado con coincidencia exacta y con 0
ocurrencias de la contraseña en todo el historial.

## 7. Trampas ya descubiertas (no repetirlas)

- **`NULL` rompe `UNIQUE` en Postgres.** La primera llave natural de `turnos`
  incluía `hora_inicio`, que es `NULL` en las ausencias (VAC/INC/LIC), así que
  el `ON CONFLICT` nunca disparaba y esas filas se duplicaban cada mes.
- **Un guarda puede tener varios *slots* en el mismo puesto el mismo día**, y no
  es un duplicado: es su turno regular más un adicional que se le solapa. La
  llave correcta es `(guarda_cedula, puesto_id, fecha, slot)`. Antes de este fix
  el ETL colapsaba esas filas y **ocultaba cruces de horario críticos**.
- **Turnos que cruzan medianoche:** un turno F va de 18:00 a 06:00. Toda
  comparación temporal se hace sobre timestamps absolutos (`fecha + hora`),
  nunca sobre `TIME` sueltos.
- **Pausa intradía ≠ descanso entre jornadas.** El hueco entre dos bloques del
  mismo día es un intermedio (Art. 167 CST), no una violación. Confundirlos
  infló los falsos positivos de 95 reales a 216 en una versión temprana.
- **Consolidar por cédula a través de TODOS los puestos**, nunca puesto por
  puesto: los cruces reales aparecen justamente entre puestos distintos.
- **Un puesto no es una hoja del Excel.** Una hoja puede tener ~20 puestos y un
  cliente puede ocupar 5 hojas.
- **El motor duplicaba las anomalías ya gestionadas.** Se resolvió dando a cada
  una una `huella` derivada de **llaves de negocio** (regla + cédula + clave
  natural del detector). Nunca meter `detalle` ni ids de `turnos` en la huella:
  el primero cambia al reescribir un mensaje y los segundos al recargar el ETL.
- **Los barridos de estado deben acotarse igual que el escaneo.** Si no, correr
  el motor con `--cedula` o `--desde/--hasta` marcaría como resuelto todo lo
  demás, que ni se evaluó.

## 8. Identidad del usuario y su trampa

`API_TOKEN` autentica al *sistema* que llama (el dashboard), no a la *persona*.
Para la persona hay dos modos en `MODO_IDENTIDAD`:

- **`DECLARATIVA` (default):** el dashboard pide un nombre y lo manda en
  `X-Usuario`; nadie verifica que sea quien dice. Solo aceptable en red interna
  cerrada, y la API lo avisa por consola al arrancar.
- **`PROXY`:** la identidad se lee de `CABECERA_IDENTIDAD`, puesta por un proxy
  que ya autenticó a la persona (Cloudflare Access, ALB+Cognito). `X-Usuario`
  se **ignora** y su ausencia es `403`.

Importa porque el historial de auditoría puede terminar sustentando una
respuesta ante el Ministerio del Trabajo: con identidad declarativa expuesta,
cualquiera podría marcar una anomalía como `JUSTIFICADA` firmando con el nombre
de otro, y un rastro que parece confiable y no lo es es peor que no tener
rastro.

**La trampa es que el default es el modo inseguro** (para no romper la
operación actual en red interna), así que olvidarlo no produce ningún error. Por
eso el `docker-compose.yml` no publica el puerto a la red: es la única barrera
real. Nunca añadir una caída de vuelta a `X-Usuario` en modo `PROXY` — hay una
prueba (`test_api_identidad.py`) que lo vigila.

## 9. Qué falta

1. **Jornada de referencia 42h — solo desde agosto de 2026.** Julio ya se
   facturó y pagó con la regla vieja de 44h, así que **julio no se toca**. La
   regla está escrita y comentada al final de `seed_reglas.sql`; se activa
   descomentándola y corriendo `python motor_reglas.py --desde 2026-08-01`.
   Va como **ADMINISTRATIVA/NOMINA**, no como violación: superar la jornada de
   referencia **no es ilegal** en vigilancia (la Ley 1920 permite hasta 60h con
   suplementarias, y ese tope sí lo cubre `SEMANA_SUPERA_60H`); lo que exige es
   pagarlas con recargo y registrarlas. Es asunto de liquidación.
2. **Autenticación real** antes de exponer el dashboard fuera de la red interna.
3. **Migración a la API de SERPI** cuando exista: cambiar la fuente del ETL y
   marcar `origen = 'API_SERPI'`. Nada más debería cambiar — por eso
   `turnos.origen` existe desde el día uno.
4. **Variables nuevas** que el usuario mencionó pero aún no ha definido. La
   arquitectura está lista para recibirlas sin rediseño.

**Objetivo a largo plazo (fuera del alcance actual):** este modelo alimenta un
sistema mayor de gestión de turnos con confirmación de asistencia por WhatsApp y
sugerencia automática de reemplazos desde el pool de "Ocasionales". Las mismas
reglas que aquí *detectan* incumplimientos, allá *previenen* asignaciones
inválidas — por eso los detectores son funciones puras, reutilizables en modo
predictivo.

## 10. Cómo correrlo

```bash
psql -U turnos_app -d turnos -f schema.sql
psql -U turnos_app -d turnos -f seed_reglas.sql
psql -U turnos_app -d turnos -f vistas_reporte.sql
python etl_normalizacion.py --archivo RepProgramacion.xlsx
python motor_reglas.py                       # o --desde/--hasta para acotar
uvicorn api:app --host 0.0.0.0 --port 8000   # dashboard en /
python test_reglas.py                        # pruebas, no necesitan BD
python generar_informe.py --periodo 2026-07  # informe autónomo para reuniones
```

Los tres `.sql` y los dos scripts son idempotentes.

**Convenciones:** código, variables y comentarios **en español** (`guarda`,
`puesto`, `turno`, `malla`). Reglas como datos, no como código. Trazabilidad
ante todo: toda anomalía debe poder rastrearse hasta los turnos concretos que la
originaron y la regla concreta que se aplicó — esto puede terminar sustentando
una respuesta ante el Ministerio del Trabajo.

**El flujo de n8n audita el mes SIGUIENTE**, no el que pasó: la malla se carga
del 25 al 27 para el mes entrante, así que auditarla el día 28 le da al
programador 3–4 días para corregir antes de que entre en vigencia. Es el salto
de detección retrospectiva a prevención.
