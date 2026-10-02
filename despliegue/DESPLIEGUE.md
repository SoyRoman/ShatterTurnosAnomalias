# Despliegue y operación en AWS

Manual para quien publica versiones y opera el servidor. Para instalar el
sistema en un equipo local, ver el README.

> Este archivo se versiona: aquí van descripciones, **nunca claves, tokens ni
> usuarios reales**. Los valores viven en SSM Parameter Store (servidor) y en
> `.env` (cada equipo), ambos fuera de git.

---

## 1. Cómo está montado hoy

**El sistema está en producción** en la cuenta de AWS `306005333749`, región
`us-east-1`. Lo creó la plantilla `infra.yaml` (CloudFormation) y lo instaló
`bootstrap.sh`.

```
 Navegador ──HTTPS──▶ Caddy (EC2, 443) ──▶ API + dashboard (Docker, 127.0.0.1:8000)
                                                │
                       timer 04:30 ──▶ pipeline (Docker) ──▶ SERPI (descarga)
                                                │
                                                ▼
                                     RDS PostgreSQL 18 (privada)
```

| Pieza | Qué es | Dónde |
|---|---|---|
| EC2 `turnos-app` | Ubuntu 24.04, `t3.small`, IP elástica | Corre la API, Caddy y el pipeline |
| RDS `turnos-db` | PostgreSQL 18, cifrada, sin acceso público | Solo la alcanza la EC2 |
| Caddy | Proxy HTTPS con certificado automático (Let's Encrypt) | `despliegue/Caddyfile` |
| Timer `turnos-pipeline` | systemd, todos los días 04:30 hora de Bogotá | `turnos-pipeline.timer` / `.service` |
| S3 `shatter-turnos-deploy-306005333749` | Paquetes de cada versión publicada (30 días) | `releases/<commit>.tar.gz` |
| Parameter Store `/turnos/*` | Configuración y secretos del servidor | De ahí sale el `.env` de la EC2 |

**URL del dashboard:** https://100.62.231.162.sslip.io — es provisional
(`sslip.io` convierte la IP en un nombre para poder sacar certificado). Cuando
haya dominio de la empresa, se cambia en `Caddyfile` y se publica (ver §5).

**Acceso:** login propio (`MODO_IDENTIDAD=SESION`). Nadie entra sin una cuenta
creada por un ADMIN, ni siquiera a `/docs`. **No hay SSH**: el servidor se
administra por SSM desde el equipo principal con los scripts de esta carpeta.

---

## 2. El equipo principal

Las actualizaciones y la operación se hacen desde **un equipo principal**
(hoy, el equipo de Automatización), que tiene:

- el repositorio clonado y al día con GitHub,
- el AWS CLI con credenciales de operador,
- los scripts `desplegar.ps1` y `servidor.ps1`.

Cualquier otra persona puede preparar su propio equipo igual (§3). El único
requisito real es tener credenciales de AWS con la política de operador.

| Script | Para qué |
|---|---|
| `desplegar.ps1` | Publicar una versión del código en el servidor |
| `servidor.ps1` | Operación diaria: estado, logs, correr el pipeline, cuentas |
| `actualizar.sh` | Lo ejecuta el servidor durante un despliegue. No se usa a mano |
| `bootstrap.sh` | Instalación desde cero de una EC2 nueva. Una sola vez |
| `infra.yaml` | La infraestructura (CloudFormation) |
| `politica-operador.json` | Permisos IAM mínimos para operar |

---

## 3. Preparar un equipo (una sola vez)

1. **Git y el repositorio**
   ```powershell
   git clone https://github.com/SoyRoman/ShatterTurnosAnomalias.git
   ```

2. **AWS CLI**
   ```powershell
   winget install --id Amazon.AWSCLI -e --source winget
   ```
   Cerrar y volver a abrir la terminal después de instalarlo.

3. **Credenciales de AWS.** Cada persona con las suyas — nunca una cuenta
   compartida, porque todo lo que se hace en el servidor queda registrado a
   nombre de quien lo hizo (CloudTrail).
   - Quien administra la cuenta de AWS crea un usuario IAM (o un acceso por
     IAM Identity Center) y le adjunta la política de
     `despliegue/politica-operador.json`.
   - En el equipo:
     ```powershell
     aws configure        # access key, secret, región us-east-1, salida json
     aws sts get-caller-identity   # debe mostrar la cuenta 306005333749
     ```

4. **Permitir scripts de PowerShell.** Windows los trae bloqueados
   ("la ejecución de scripts está deshabilitada en este sistema"):
   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
   ```

5. **Comprobar:**
   ```powershell
   .\despliegue\servidor.ps1 estado
   ```

> **Ojo con lo que da la política de operador.** Ejecutar comandos por SSM es
> ejecutar como `root` en el servidor: quien la tenga puede leer el `.env` del
> servidor (clave de la BD y de SERPI). Dásela solo a quien vaya a administrar
> el sistema. Para **usar** el dashboard no hace falta nada de esto: basta una
> cuenta del dashboard (§6).

---

## 4. Publicar una versión

El flujo normal: cambiar código → probar en local → commit → push → publicar.

```powershell
# 1. Probar en local (sin base de datos)
.\venv\Scripts\python.exe test_reglas.py

# 2. Commit y push
git add <archivos>
git commit -m "Descripción del cambio"
git push

# 3. Publicar
.\despliegue\desplegar.ps1
```

Qué hace `desplegar.ps1`:

1. Empaqueta **el commit** (no la carpeta) con `git archive`. Los cambios sin
   commit no viajan, y el script se niega a publicar si los hay.
2. Lo sube a S3 como `releases/<commit>.tar.gz`.
3. Por SSM, el servidor descarga el paquete y corre `actualizar.sh`: extrae el
   código, reconstruye **las dos** imágenes (API y pipeline), reinicia la API,
   reinstala el timer y comprueba que la API responde.
4. Si la API no responde, el despliegue falla y muestra los logs.

Tarda unos 3–8 minutos (la imagen del pipeline trae Chromium).

**No toca** la base de datos, el `.env` del servidor ni las cuentas de usuario.
Lo que corre en el servidor queda registrado en `/opt/turnos/VERSION`
(`servidor.ps1 estado` lo muestra).

### Volver a una versión anterior

```powershell
git log --oneline                         # buscar el commit bueno
.\despliegue\desplegar.ps1 -Version a1b2c3d4e5
```

Funciona mientras la versión siga en GitHub. El paquete en S3 se borra a los
30 días, pero `desplegar.ps1` lo vuelve a generar desde git.

### Migraciones de esquema

**Un despliegue de código nunca cambia la base.** Si una versión trae un
`migracion_NNN_*.sql` nuevo, se aplica aparte y a propósito:

1. Probarla primero sobre la base local, **dos veces**: tiene que ser
   idempotente (`IF NOT EXISTS`), para que repetirla no rompa nada.
2. Publicar la versión (así el archivo entra en la imagen del servidor).
3. Aplicarla:
   ```powershell
   .\despliegue\servidor.ps1 migrar -Archivo migracion_005_historial.sql
   ```

Corre dentro del contenedor de la API, con su `.env`, y usa la copia del
`.sql` de la versión publicada. Por eso va **después** de `desplegar.ps1`.
Agregar la migración nueva también a la lista de `bootstrap.sh`, para que una
instalación desde cero la incluya.

| Migración | Qué agrega | En producción |
|---|---|---|
| `migracion_004_usuarios.sql` | Cuentas, sesiones, bitácora de accesos | Aplicada por `bootstrap.sh` |
| `migracion_005_historial.sql` | Tabla `corridas` (pestaña Historial) | **Aplicar al publicar la versión del 2026-10-02** |

### Novedades del sistema

La sección "Mejoras y correcciones" de la pestaña Historial sale de
`novedades.json`, en la raíz del repo. **Al publicar un cambio que note quien
usa el dashboard, agregar una entrada arriba de todo** (fecha, `MEJORA` o
`CORRECCION`, título y detalle en lenguaje de usuario, no técnico) en el mismo
commit. Es lo que le permite a cualquier rol saber qué cambió y por qué.

---

## 5. Operar el servidor

```powershell
.\despliegue\servidor.ps1 estado      # versión, API, próxima corrida, disco
.\despliegue\servidor.ps1 logs        # bitácora de las últimas corridas del pipeline
.\despliegue\servidor.ps1 logs -Lineas 300
.\despliegue\servidor.ps1 logs-api    # errores de la API / dashboard
.\despliegue\servidor.ps1 pipeline    # correr el pipeline ahora (10–30 min, en segundo plano)
```

### Qué significa el código de salida del pipeline

Aparece en `servidor.ps1 logs` y `estado` (`status=N`):

| Código | Significa | Qué hacer |
|---|---|---|
| 0 | Todo bien | Nada |
| 1 | Falta configuración | Revisar `/turnos/*` en Parameter Store |
| 2 | La descarga de SERPI falló 3 veces | SERPI caído o cambió el formulario. Reintentar más tarde con `servidor.ps1 pipeline` |
| 3 | El ETL falló | Bug o cambio de formato del Excel. Revisar el log |
| 4 | **El ETL se negó a borrar** | No es un fallo: la malla nueva borraría >20% de los turnos. Revisar el archivo antes de forzar nada |
| 5 | El motor de reglas falló | Los turnos sí se cargaron; revisar el log |
| 6 | Ya había otra corrida | Nada |

> **Nadie recibe aviso si una corrida falla.** Desde el 2026-10-02 cada corrida
> queda en la pestaña **Historial** del dashboard, con su resultado — es lo
> primero que hay que mirar. Pero no llega ninguna alerta: si nadie abre el
> Historial, un fallo pasa desapercibido hasta que exista la alarma (§8).

### La actualización diaria y los duplicados

Programación cambia la malla en SERPI todos los días, así que el sistema la
vuelve a descargar **todos los días a las 04:30** (mes actual y siguiente) y
la compara con la anterior. Lo que garantiza que no queden datos duplicados:

1. **Cada mes se reemplaza entero.** Lo que ya no viene en el reporte de SERPI
   se borra, aunque sea de un puesto que SERPI renombró o eliminó
   (`--malla-completa`, lo pasa el pipeline siempre que descarga).
2. **Verificación antes de confirmar.** Si después de cargar queda en el mes
   algún turno que no vino en el archivo, la carga se cancela entera y la base
   queda como estaba (`ERROR DE INTEGRIDAD`, código 3).
3. **Freno contra archivos truncados.** Si la malla nueva borraría más del 20%
   de los turnos, no se toca nada (código 4).
4. **Un mismo turno no puede existir dos veces**: la base lo impide por
   guarda + puesto + fecha + turno.

Cada actualización queda en el Historial con los turnos nuevos, modificados
(antes → después) y borrados, y las anomalías que se corrigieron o
aparecieron por esos cambios.

### Cambiar el dominio

1. Apuntar el DNS del dominio a la IP elástica.
2. Editar `despliegue/Caddyfile` (cambiar el hostname), commit, push.
3. `.\despliegue\desplegar.ps1` — Caddy se recarga y saca el certificado solo.

---

## 6. Dar acceso a otras personas

Para **gestionar anomalías** en el dashboard, una persona solo necesita una
cuenta del dashboard. No necesita AWS ni este repositorio.

Se crean desde el propio dashboard (pestaña de usuarios, con una cuenta ADMIN)
o desde el equipo principal:

```powershell
.\despliegue\servidor.ps1 crear-usuario -Usuario mperez -Nombre "Maria Perez" -Rol PROGRAMADOR -Correo mperez@seguridadshatter.com
.\despliegue\servidor.ps1 usuarios
.\despliegue\servidor.ps1 restablecer-clave -Usuario mperez
.\despliegue\servidor.ps1 desactivar -Usuario mperez
```

| Rol | Para quién |
|---|---|
| `PROGRAMADOR` | Quien arma la malla y corrige en SERPI |
| `NOMINA` | Horas y novedades |
| `GERENCIA` | Lectura y reportes |
| `ADMIN` | Gestiona cuentas. Darlo con cuidado |

- La clave temporal se muestra **una sola vez** y el sistema obliga a cambiarla
  en el primer ingreso. Entregarla por un medio seguro, no por correo.
- **Una cuenta por persona.** El historial de cada anomalía guarda quién la
  cambió; una cuenta compartida lo vuelve inútil como evidencia.
- Las cuentas se **desactivan**, no se borran: siguen apareciendo en el
  historial de lo que hicieron.

---

## 7. Problemas conocidos y cómo se resolvieron

**El pipeline no corrió nunca en el servidor** (27-09 al 01-10-2026, cinco
noches seguidas con `status=1`). El contenedor escribe con su propio usuario
(uid 10001, ver `Dockerfile`) y las carpetas `logs/` y `Reportes mensuales/`
del servidor eran del `turnos` del host (uid 997): moría en el primer segundo
con `PermissionError: /app/logs/pipeline_diario.log`, antes de descargar nada.
Corregido en `actualizar.sh` y `bootstrap.sh` (`chown 10001` de esas dos
carpetas). Los datos de agosto y septiembre que hay en producción vienen de una
carga manual, no del pipeline.

**SERPI no entrega más de un mes por reporte** (descubierto el 2026-10-01).
Pedido un rango de dos meses, devuelve una sola grilla con la disposición de
un mes y el rótulo del otro. El ETL fallaba con «fecha fuera de rango:
2026-09-31»; con dos meses de 30 días habría cargado turnos en el mes
equivocado sin avisar. Estaba escondido detrás del fallo de permisos: era lo
siguiente con lo que el servidor se iba a encontrar. Corregido:
`pipeline_diario.py` descarga y carga mes por mes, y corre el motor una vez
sobre todo el rango.

**La API figuraba `unhealthy` estando sana.** El healthcheck del `Dockerfile`
pedía `/reglas` con `urllib`, que lanza excepción ante el 401 que da cualquier
ruta sin sesión. Corregido con `http.client`, que acepta 401/403 como "vivo".

**`playwright install chromium` falla en redes con proxy.** Solo afecta a
equipos locales (el servidor descarga sin problema). Poner
`NAVEGADOR_CANAL=msedge` en el `.env` local para usar el Edge instalado.

**`docker compose build` a secas solo construye `api`.** El pipeline vive en el
profile `manual`. `actualizar.sh` ya usa `--profile manual`; si se construye a
mano, no olvidarlo o el timer seguirá corriendo código viejo sin dar error.

**Zona horaria.** El timer usa la hora del sistema; la EC2 está en
`America/Bogota` (lo fija `bootstrap.sh`). Si se recrea la máquina, verificarlo
o las 04:30 se volverán las 23:30 del día anterior.

**Memoria.** Chromium con 2 GB va justo; hay 2 GB de swap. Si el pipeline cae
con `Target closed` o el kernel mata el proceso, subir la EC2 a `t3.medium`.

---

## 8. Pendientes

| Pendiente | Por qué importa |
|---|---|
| **Aviso cuando falla el pipeline** (`OnFailure=` en el `.service` o alarma de CloudWatch) | Sin esto, una corrida fallida solo queda en el journal y nadie se entera — el peor modo de fallo en una herramienta de cumplimiento |
| **Rotar la clave de SERPI** y pedir un usuario aparte para el robot | La actual es trivial y es la cuenta de una persona |
| **Rotar las claves que circularon en texto** durante el desarrollo | Ver historial del proyecto |
| **Dominio propio** en lugar de `sslip.io` | La URL provisional depende de un servicio externo y de que la IP no cambie |
| **Decisión de legal** sobre transferencia internacional (Ley 1581): DPA de AWS, inscripción en el RNBD | Los datos son PII de ~400 trabajadores alojados fuera de Colombia |
| **Usuario de solo lectura en `bitacorapp`** para el cruce de asistencia | No se toca la base de la empresa hasta tenerlo |

### Lo que este despliegue NO hace

- No escribe en las bases de la empresa (`bitacorapp`, `bitacorapp_staging`).
- No migra nada a MySQL: se queda en PostgreSQL (CLAUDE.md §8).
- No activa la regla de 42h (comentada al final de `seed_reglas.sql`).
- No manda los correos por audiencia: el payload sigue en `/informe/mensual`,
  pero no hay quien lo envíe.

---

## Costos aproximados

`us-east-1`, sin compromisos; verificar en la calculadora de AWS.

| Recurso | USD/mes |
|---|---|
| EC2 `t3.small` + IP elástica | ~15 |
| EBS 30 GB gp3 | ~2,5 |
| RDS `db.t4g.micro` + 20 GB + respaldos | ~14,5 |
| S3, Parameter Store, SSM | ~0 |
| **Total** | **~32** |
