# Plan de despliegue en AWS

Estado: **fase 1 hecha, el resto no iniciado.** Todo está construido y probado
en local (ver README, sección "Probado"), pero nada se ha desplegado todavía.

**Regla que atraviesa todo el documento:** las pruebas siguen siendo locales
hasta que el entorno de AWS esté validado. **No se toca la base de producción
ni la de pruebas de la empresa** (`bitacorapp`, `bitacorapp_staging`). De esas
se lee, no se escribe, y solo cuando llegue el cruce de asistencia.

---

## Resumen de las seis fases

| Fase | Qué es | Quién | Bloquea a | Estado |
|---|---|---|---|---|
| 0 | Trámites y decisiones que no son técnicos | Tú + legal + DBA | Fase 2 | pendiente |
| 1 | Autenticación real en la API | Desarrollo | Fase 6 | **código hecho**; falta configurar Access |
| 2 | Infraestructura AWS (RDS + EC2 + red) | Tú | Fase 3 | espera la región |
| 3 | Bootstrap del servidor | Tú | Fase 4 | — |
| 4 | Migrar los datos | Tú | Fase 5 | — |
| 5 | Validación con criterios de aceptación | Tú | Fase 6 | — |
| 6 | Puesta en marcha | Tú | — | — |

Las fases 0 y 1 eran paralelas: la 1 es trabajo de código y no esperaba a
nadie, así que **ya está hecha** (septiembre de 2026). El bloqueante pasó a ser
la fase 0: la región la decide legal y el usuario de servicio lo da el DBA.

**Decisiones tomadas:** el usuario crea los recursos en la consola de AWS;
autenticación con **Cloudflare Access** (el DNS ya está en Cloudflare); la
región espera a legal.

---

## Fase 0 — Antes de tocar AWS

Nada de esto es código, y por eso se suele dejar para el final y termina
frenando el despliegue una semana.

### 0.1 Rotar las credenciales expuestas

Cuatro credenciales circularon en texto plano durante el desarrollo y hay que
cambiarlas **antes** de que el sistema quede en un servidor.

> Este archivo se versiona, así que aquí van descripciones, **nunca los valores
> ni los nombres de usuario**: identificar una cuenta ya le dice a un atacante a
> quién apuntar. Los valores reales viven en `.env` y en `Migracion/.env`, los
> dos fuera de git.

| Credencial | Problema |
|---|---|
| `SERPI_WEB_PASSWORD` | Es trivial (nueve dígitos consecutivos). Una clave débil usada a diario por un robot desatendido es un riesgo mayor que la misma clave usada a mano unas veces al mes |
| `DB_PASSWORD` local | Deja de importar cuando la BD sea RDS, pero rótala igual |
| Usuario de staging de `bitacorapp` | Tiene `ALL PRIVILEGES` sobre esa base |
| Usuario de producción de `bitacorapp` | **Es la cuenta personal de una empleada**, no una cuenta de servicio |

### 0.2 Pedir al DBA un usuario de servicio

El acceso a `bitacorapp` hoy es la cuenta personal de una persona. Eso no puede
quedar en un archivo de configuración de un servidor: si esa persona se va o le
cambian la clave, el sistema se cae, y lo que el sistema lea queda atribuido a
alguien que no tomó la decisión.

Lo que hay que pedir: **un usuario de solo lectura, dedicado, con nombre de
servicio** (p. ej. `svc_auditoria_turnos`), con `SELECT` limitado a las tablas
del cruce (`users`, `shifts`, `structures`, `clients`).

### 0.3 Decisión de legal: región y encargados del tratamiento

Los datos son PII de 404 trabajadores y AWS implica **transferencia
internacional** (Ley 1581 de 2012). Tres preguntas para legal, no para
desarrollo:

1. ¿Basta con el **DPA de AWS** (el addendum de tratamiento de datos), o la
   región elegida exige autorización adicional?
2. ¿Está la base inscrita en el **RNBD** de la SIC, y hay que actualizar la
   inscripción por el cambio de ubicación?
3. **¿Cloudflare está cubierto?** Este punto se pasó por alto en la primera
   versión de este documento. Cloudflare Tunnel **termina el TLS en el borde de
   Cloudflare**, así que Cloudflare ve el tráfico en claro: es un **segundo
   encargado del tratamiento**, no solo un enrutador, y hay nombres y cédulas en
   ese tráfico. Necesita el mismo análisis que AWS (su DPA y, si aplica, figurar
   en la inscripción del RNBD).
   Si legal lo rechazara, la alternativa técnica es **ALB + Cognito**, que
   mantiene todo dentro de AWS a cambio de ~USD 16/mes y de mover el DNS. El
   código no cambia: solo `CABECERA_IDENTIDAD=x-amzn-oidc-identity`. Ese es
   justo el motivo de que la cabecera sea configurable.

Impacto técnico de la respuesta:

| Región | Latencia desde Cali | Costo | Nota |
|---|---|---|---|
| `us-east-1` (Virginia) | ~70–90 ms | La más barata | La opción por defecto |
| `sa-east-1` (São Paulo) | ~110–130 ms | ~30–50% más cara | Puede simplificar el análisis de transferencia; que lo confirme legal |

Cualquiera de las dos sirve técnicamente: la latencia importa para el dashboard
(un clic), no para el pipeline, y la BD queda junto a la app en la misma VPC.
**No decidas esto por precio antes de tener la respuesta de legal.**

### 0.4 Verificar que hay presupuesto

Ver "Costos" al final. Aproximadamente **USD 32/mes** sin compromisos.

---

## Fase 1 — Autenticación real — **CÓDIGO HECHO**

El código está implementado y probado (14/14 en `test_api_identidad.py`, sin
escribir nada en la base). Lo que falta es **configurar Cloudflare Access** y
cambiar una variable; ver `despliegue/CLOUDFLARE.md`.

### El problema que resuelve

La identidad de la persona era **declarativa**: la API recibía `X-Usuario` como
cabecera y confiaba en lo que le mandaran. `API_TOKEN` autentica al *sistema*
que llama, no a la *persona*.

En red interna cerrada se puede vivir con eso. **Expuesto, no**: cualquiera
podría marcar una anomalía como `JUSTIFICADA` firmando con el nombre de otro, y
`anomalias_historial` —el rastro que puede terminar sustentando una respuesta
ante el Ministerio del Trabajo— pasaría a ser ficción. Es peor que no tener
historial, porque parece confiable.

### Lo que quedó implementado

Dos modos, en `MODO_IDENTIDAD`:

| Modo | Comportamiento | Cuándo |
|---|---|---|
| `DECLARATIVA` | Se cree `X-Usuario`. Avisa por consola al arrancar | Default, por compatibilidad con la operación actual en red interna |
| `PROXY` | La identidad se lee de una cabecera puesta por el autenticador. **`X-Usuario` se ignora**; si falta la cabecera → **403** | Producción |

Detalles que importan y no conviene revertir:

- **En modo `PROXY` no hay caída de vuelta a `X-Usuario`.** Ese fallback sería
  justo el agujero que el modo cierra: bastaría con alcanzar la API sin pasar
  por el proxy para poder firmar como cualquiera. Está protegido por prueba.
- **`CABECERA_IDENTIDAD` es configurable**, así que el código no queda acoplado
  a un proveedor: `Cf-Access-Authenticated-User-Email` para Cloudflare Access,
  `x-amzn-oidc-identity` para ALB+Cognito. La decisión de infraestructura no
  bloqueó el desarrollo.
- **Un `MODO_IDENTIDAD` inválido aborta el arranque.** Un typo en la variable no
  puede dejar la API sirviendo en modo declarativo sin que nadie lo note.
- **`GET /identidad`** existe para que el dashboard sepa en qué modo está: en
  `PROXY` muestra el correo verificado en vez de pedir un nombre. Pedirle el
  nombre a alguien que ya inició sesión es a la vez redundante y engañoso,
  porque sugiere que ese nombre es el que se va a guardar.
- **La confianza en una cabecera plana es aceptable porque la garantía viene de
  la RED**: la API no publica puerto al host y el grupo de seguridad de la EC2
  no admite ingreso, así que el único camino es el proxy. Si algún día se expone
  el puerto, hay que pasar a verificar el JWT firmado
  (`Cf-Access-Jwt-Assertion` / `x-amzn-oidc-data`).

### Se arregló en el mismo cambio

`/anomalias` **ignoraba en silencio los parámetros desconocidos**: un
`?cedula=X` en vez de `?busqueda=X` devolvía la primera página sin filtrar,
con apariencia de haber filtrado — o sea, un resultado que parece ser de un
guarda y es de otro. Ya causó una confusión real durante las pruebas. Ahora
responde **422** nombrando el parámetro no reconocido y los válidos.

### Lo que falta de esta fase

1. Configurar el túnel y la aplicación de Access (`despliegue/CLOUDFLARE.md`).
2. Poner `MODO_IDENTIDAD=PROXY` en el `.env` del servidor.
3. Confirmar que llegar a la API sin pasar por el proxy devuelve **403**.

> **No desplegar con `MODO_IDENTIDAD=DECLARATIVA` accesible desde fuera de la
> red interna.** Si hubiera que salir antes de configurar Access, la salida
> intermedia es poner el proxy delante igual: protege el acceso, aunque la
> atribución en el historial siga siendo débil.

## Fase 2 — Infraestructura AWS

### 2.1 Red

Sirve la VPC por defecto. Lo que importa son los grupos de seguridad:

| Grupo | Ingreso | Egreso |
|---|---|---|
| `sg-turnos-app` (EC2) | **ninguno** | todo (túnel de Cloudflare + SERPI + RDS) |
| `sg-turnos-db` (RDS) | 5432 **solo desde `sg-turnos-app`** | ninguno |

Ingreso vacío en la EC2 no es un descuido: **el acceso administrativo va por
SSM Session Manager**, no por SSH. Eso elimina las llaves SSH, el puerto 22
abierto y la lista de IPs autorizadas — tres cosas que en la práctica se
gestionan mal.

En `sg-turnos-db`, la fuente se referencia **por grupo de seguridad, no por
rango de IP**. Así la regla sigue siendo correcta si la EC2 cambia de IP.

### 2.2 RDS PostgreSQL

| Parámetro | Valor | Por qué |
|---|---|---|
| Motor | PostgreSQL 18 | Es la versión contra la que se probó el bootstrap |
| Clase | `db.t4g.micro` | 24.623 turnos y ~1.000 anomalías. Sobra |
| Almacenamiento | 20 GB gp3 | El dump completo son unos pocos MB |
| **Acceso público** | **No** | Sin esto, la BD queda alcanzable desde internet |
| Cifrado en reposo | Sí (KMS) | Es PII; y activarlo después exige recrear la instancia |
| Respaldos | 7 días + ventana | Aquí vive un rastro de auditoría laboral |
| Multi-AZ | No, al principio | Duplica el costo. Se puede activar sin recrear |
| Zona horaria | Dejar en UTC | La app maneja `DATE`/`TIME` sin zona; cambiarla invita a inconsistencias. La zona se fija en la app y el host |

> **`db.t4g.micro` es una instancia ráfaga.** Con esta carga no habrá problema,
> pero si algún día el histórico crece a años, vigila los créditos de CPU antes
> de culpar a las consultas.

### 2.3 EC2

| Parámetro | Valor | Por qué |
|---|---|---|
| Tipo | `t3.small` (2 vCPU, 2 GB) | La API está ociosa; el pico es Chromium una vez al día |
| Disco | 30 GB gp3 | Imágenes (~800 MB) + un año de mallas archivadas |
| Swap | 2 GB | **Necesario:** Chromium con 2 GB de RAM va justo |
| SO | Amazon Linux 2023 o Ubuntu 22.04+ | Cualquiera; los comandos de abajo asumen Ubuntu |
| Rol IAM | `AmazonSSMManagedInstanceCore` + lectura de Parameter Store | Acceso sin SSH y secretos sin archivos |

Si el pipeline se cae con `Target closed` o el kernel mata el proceso, súbela a
`t3.medium` (4 GB) antes de investigar otra cosa.

### 2.4 Secretos

`.env` **no se copia a mano al servidor.** Va a **SSM Parameter Store** como
`SecureString` (el nivel estándar es gratis; Secrets Manager cuesta
USD 0,40/secreto/mes y aquí no aporta nada):

```bash
aws ssm put-parameter --name /turnos/DB_PASSWORD    --type SecureString --value '...'
aws ssm put-parameter --name /turnos/DB_HOST        --type String       --value 'turnos-db.xxxx.rds.amazonaws.com'
aws ssm put-parameter --name /turnos/SERPI_WEB_USER --type String       --value '<usuario>'
aws ssm put-parameter --name /turnos/SERPI_WEB_PASSWORD --type SecureString --value '...'
aws ssm put-parameter --name /turnos/API_TOKEN      --type SecureString --value '...'
```

El arranque los materializa en un `.env` con permisos `600`, propiedad del
usuario `turnos`. Así la credencial de SERPI no queda en el historial de
comandos, ni en una imagen, ni en git.

---

## Fase 3 — Bootstrap del servidor

```bash
# 1. Zona horaria — HAY QUE HACERLO, y es lo que más se olvida.
#    `OnCalendar` de systemd usa la zona del sistema. Una EC2 nueva viene en
#    UTC, así que el timer de las 04:30 se dispararía a las 23:30 del día
#    anterior. (El TZ del Dockerfile es un ajuste distinto y también hace
#    falta: decide qué día cree que es el contenedor.)
sudo timedatectl set-timezone America/Bogota

# 2. Swap: Chromium con 2 GB va justo
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 3. Docker
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git

# 4. Cuenta de servicio (NO la de un empleado — ver el .service)
sudo useradd --system --home /opt/turnos --shell /usr/sbin/nologin turnos
sudo usermod -aG docker turnos

# 5. Código
sudo git clone <repo> /opt/turnos
sudo chown -R turnos:turnos /opt/turnos
sudo mkdir -p /opt/turnos/logs "/opt/turnos/Reportes mensuales"
sudo chown turnos:turnos /opt/turnos/logs "/opt/turnos/Reportes mensuales"

# 6. .env desde Parameter Store (no a mano)
#    Escribe /opt/turnos/.env con permisos 600, propiedad de `turnos`.

# 7. Imágenes — OJO CON EL PROFILE
cd /opt/turnos
sudo -u turnos docker compose --profile manual build
```

> **`docker compose build` a secas solo construye `api`.** El servicio
> `pipeline` vive en el profile `manual`, así que sin `--profile manual` la
> imagen del pipeline no se actualiza y el timer seguiría corriendo la versión
> anterior. Es un fallo silencioso: no da error, simplemente ejecuta código
> viejo. Aplica también a cada actualización posterior.

```bash
# 8. Esquema — el mismo orden probado contra un PostgreSQL 18 vacío
export PGPASSWORD=...
for f in schema.sql migracion_002_dashboard.sql migracion_003_estados.sql \
         seed_reglas.sql vistas_reporte.sql; do
  psql -h <endpoint-rds> -U turnos_app -d turnos -f "$f"
done

# 9. API + timer
sudo -u turnos docker compose up -d api
sudo cp despliegue/turnos-pipeline.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now turnos-pipeline.timer
systemctl list-timers turnos-pipeline.timer
```

Resultado esperado del paso 8: **9 tablas, 7 vistas, 7 reglas** y la columna
`slot` presente en `turnos`. Ya se verificó que esa secuencia se aplica limpia
sobre una base vacía.

---

## Fase 4 — Migrar los datos

Dos caminos. **El primero es el correcto.**

### Opción A (recomendada): volcar la base local

```bash
# En local
pg_dump -U turnos_app -d turnos --no-owner --no-privileges -Fc -f turnos.dump

# Contra el RDS (desde la EC2, que es quien tiene alcance a la BD)
pg_restore -h <endpoint-rds> -U turnos_app -d turnos --no-owner --clean --if-exists turnos.dump
```

Preserva las **huellas** de las anomalías y `anomalias_historial`. Ahora mismo
el historial está vacío y las 990 anomalías están todas `ABIERTA`, así que da
igual — pero si alguien empieza a gestionar anomalías en local antes del corte,
esto es lo único que no pierde ese trabajo.

> El dump lleva PII de 404 trabajadores. Que no pase por ningún servicio
> intermedio, y bórralo del disco al terminar.

### Opción B: recargar desde los Excel

Correr el ETL y el motor contra las mallas de `Reportes mensuales/`. Válido, y
reproduce los mismos números (está verificado). Pero **descarta el historial de
gestión**, así que solo sirve mientras siga vacío.

### Sobre julio

Julio ya se facturó y pagó con la regla vieja de 44h, y la malla ya pasó. Se
migra por valor histórico, no operativo. Si se decide dejarlo fuera, **los
números de agosto cambian**: el mes aislado da 303 hallazgos y con los meses
vecinos cargados da 339. No es un error — es que las ventanas móviles a caballo
entre meses recién se ven completas cuando el mes vecino existe (CLAUDE.md §7).
Decide **antes** de la fase 5, porque afecta los criterios de aceptación.

---

## Fase 5 — Validación

No se declara desplegado hasta que estos seis pasen. Son criterios verificables,
no "parece funcionar".

| # | Criterio | Cómo se comprueba |
|---|---|---|
| 1 | El esquema quedó completo | 9 tablas, 7 vistas, 7 reglas, columna `slot` |
| 2 | Los datos migraron íntegros | `turnos` = 24.623 (Jul 8.821 / Ago 8.391 / Sep 7.411); `anomalias` = 990; 413 guardas |
| 3 | El motor da lo mismo que en local | Correrlo sobre septiembre → **244 hallazgos, 62 guardas**. Si difiere, algo se migró mal |
| 4 | El pipeline completo corre en el servidor | `sudo systemctl start turnos-pipeline.service`, luego `journalctl -u turnos-pipeline`. Descarga real contra SERPI incluida |
| 5 | La red de seguridad sigue viva | Correr con `--umbral-borrado -1` → sale con **4**, no ejecuta el motor, la BD queda intacta |
| 6 | El timer dispara solo | Esperar una noche y confirmar con `journalctl` que corrió a las 04:30 |

El criterio 3 es el importante: es la prueba de que la migración no perdió ni
inventó nada. El 5 es el que nadie prueba y el que evita un borrado masivo
silencioso el día que SERPI devuelva un archivo truncado.

**El criterio 4 es la primera vez que el sistema hace una descarga real desde el
servidor.** Reserva tiempo: el reporte de SERPI tarda varios minutos.

> **Confirmado con el proveedor (septiembre 2026): SERPI NO restringe por IP.**
> Era el riesgo grande de este criterio — con lista blanca, la EC2 no habría
> podido entrar y el despliegue se habría trabado esperando al proveedor. Queda
> descartado, así que lo único que puede fallar aquí es la salida a internet de
> la EC2, que se arregla sin depender de nadie.
>
> Sigue pendiente, pero es higiene y no bloqueante: **pedir un usuario aparte en
> SERPI para el robot**, en vez de reutilizar una cuenta de persona.

---

## Fase 6 — Puesta en marcha

1. Configurar Cloudflare Access con el directorio de la empresa y la lista de
   personas que entran.
2. Cambiar `MODO_IDENTIDAD` a `PROXY` (fase 1) y reiniciar la API.
3. Confirmar que sin pasar por el proxy la API responde **403**, no 200.
4. Entregar la URL a las tres audiencias (programador / nómina / gerencia).
5. Dejar avisos de fallo: `OnFailure=` en el `.service` (está comentado) o una
   alarma de CloudWatch sobre el log. **Sin esto, una corrida fallida queda solo
   en el journal y nadie se entera** — que en una herramienta de cumplimiento
   es el peor modo de fallo.

---

## Costos aproximados

Verifícalos en la calculadora de AWS; los precios cambian y estos son de
memoria, `us-east-1`, sin compromisos:

| Recurso | USD/mes |
|---|---|
| EC2 `t3.small` | ~15 |
| EBS 30 GB gp3 | ~2,5 |
| RDS `db.t4g.micro` | ~12 |
| RDS 20 GB gp3 + respaldos | ~2,5 |
| SSM Parameter Store (estándar) | 0 |
| Cloudflare Tunnel + Access (<50 usuarios) | 0 |
| **Total** | **~32** |

Con un Savings Plan de 1 año baja a ~22. Multi-AZ en RDS lo subiría ~12 más;
no hace falta al principio.

---

## Lo que este despliegue NO hace

Para que no se asuma de más:

- **No migra nada a MySQL.** Decidido en septiembre de 2026 (CLAUDE.md §8): se
  queda en PostgreSQL. Meter el esquema en `bitacorapp_staging` costaba ~68
  construcciones a reescribir, 75 ms de latencia por consulta, y dejaba
  `anomalias_historial` dentro de un esquema Laravel vivo.
- **No escribe en las bases de la empresa.** De `bitacorapp` se leerá, cuando
  llegue el cruce de asistencia por cédula.
- **No activa la regla de 42h.** Sigue comentada al final de `seed_reglas.sql`.
  Lo acordado: 42–60h aparece en el reporte de horas suplementarias pero **no**
  como anomalía; por encima de 60h sí, y eso ya lo cubre `SEMANA_SUPERA_60H`.
  El reporte de la banda 42–60h para la bandeja de Nómina **está sin construir**.
- **No manda los tres correos por audiencia.** Los hacía el flujo de n8n, que
  quedó descartado. El payload sigue en `/informe/mensual`, pero no hay quien
  lo envíe.
- **No usa n8n.** `n8n_auditoria_mensual.json` está eliminado a propósito.
