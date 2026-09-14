# Cloudflare Tunnel + Access — configuración

Es el paso que falta de la fase 1: el código ya soporta identidad verificada
(`MODO_IDENTIDAD=PROXY`), pero alguien tiene que poner el autenticador delante.

**Por qué esta opción** (decidida en septiembre de 2026, el DNS de la empresa ya
está en Cloudflare):

| Ventaja | Detalle |
|---|---|
| Gratis | Hasta 50 usuarios; aquí serán ~5 |
| **Cero puertos abiertos** | El túnel sale de la EC2 *hacia* Cloudflare. El grupo de seguridad no necesita **ningún** ingreso |
| Identidad ya verificada | Access valida contra el Entra ID de la empresa e inyecta `Cf-Access-Authenticated-User-Email` |
| Sin ALB | Ahorra ~USD 16/mes y el manejo de certificados |

El correo de la empresa es **Microsoft 365**, así que el proveedor de identidad
es **Microsoft Entra ID** (el nombre nuevo de Azure AD). Eso trae dos ventajas
gratis: la política de MFA y de acceso condicional que ya tenga la empresa se
aplica también aquí, y los grupos de seguridad quedan disponibles para
autorizar por bandeja más adelante.

---

## 1. Crear el túnel

En el panel de Cloudflare: **Zero Trust → Networks → Tunnels → Create a
tunnel**, tipo *Cloudflared*.

| Campo | Valor |
|---|---|
| Nombre del túnel | `turnos-anomalias` |
| Public hostname | `turnos.seguridadshatter.com` |
| Service (tipo) | `HTTP` |
| Service (URL) | `api:8000` |

**`api:8000` y no `localhost:8000`.** El `cloudflared` va a correr como un
servicio más del `docker-compose.yml`, así que alcanza a la API por el nombre de
servicio en la red de compose. Ver el paso 4.

Cloudflare entrega un **token del túnel**. Ese token es una credencial: va a
`.env` (que está en `.gitignore`), nunca a un comando ni al repo.

---

## 2. Conectar Microsoft Entra ID

Hay que registrar Cloudflare como aplicación en Entra y luego darle a Cloudflare
los tres datos que salen de ese registro.

### 2.1 En Entra (portal de Azure / Microsoft Entra admin center)

**Identity → Applications → App registrations → New registration**

| Campo | Valor |
|---|---|
| Name | `Cloudflare Access — Auditoría de turnos` |
| Supported account types | *Accounts in this organizational directory only* (single tenant) |
| Redirect URI | **Web** → `https://<equipo>.cloudflareaccess.com/cdn-cgi/access/callback` |

`<equipo>` es el nombre del *team domain* de Cloudflare Zero Trust (sale en
**Settings → Custom Pages**, o en la URL del panel). Si el URI de redirección no
coincide **exactamente**, el login falla con un error de Microsoft que no
menciona a Cloudflare.

Después, dentro de la misma app:

1. **Certificates & secrets → New client secret.** Copia el **Value** en ese
   momento: al recargar la página ya no se puede ver, solo el Secret ID.
2. **API permissions → Add a permission → Microsoft Graph → Delegated**, y
   agrega: `openid`, `profile`, `email`, `offline_access`, `User.Read`.
3. Si se quiere usar grupos de seguridad más adelante (ver 2.3), agrega también
   `Directory.Read.All`.
4. **Grant admin consent** para el tenant. Sin esto, cada usuario vería una
   pantalla de consentimiento — o directamente un bloqueo, si la política del
   tenant no permite que los usuarios consientan.

Anota los tres valores: **Application (client) ID**, **Directory (tenant) ID** y
el **client secret**.

### 2.2 En Cloudflare

**Zero Trust → Settings → Authentication → Login methods → Add new → Azure AD**

| Campo | De dónde sale |
|---|---|
| App ID | Application (client) ID |
| Client secret | El *Value* del secreto |
| Azure AD directory ID | Directory (tenant) ID |
| Support groups | Actívalo si hiciste el paso 2.1.3 |

Usa **Test** antes de seguir. Si falla ahí, no tiene sentido montar la
aplicación de Access encima.

### 2.3 Dos advertencias que importan en este proyecto

**El client secret CADUCA, y cuando caduca nadie puede entrar.** Entra propone
6 meses por defecto y permite 24 como máximo; no hay opción de "nunca". El día
que expire, la auditoría de cumplimiento queda inaccesible para todo el mundo, y
el error de Microsoft no dice que sea eso. **Pon un recordatorio de calendario
un mes antes de la fecha de expiración** y anótala aquí cuando la sepas:

```
Client secret creado el:  ____________
Expira el:                ____________   ← recordatorio 1 mes antes
```

**El correo que llega es el que queda escrito en la auditoría.** Access pone en
`Cf-Access-Authenticated-User-Email` lo que Entra reporte como correo, y la API
lo guarda tal cual en `anomalias.actualizado_por` y en
`anomalias_historial.usuario` (las dos son `TEXT`, así que no hay riesgo de
truncar un UPN largo). Dos consecuencias:

- Si alguna cuenta **no tiene el atributo `mail` poblado** en Entra, Microsoft
  puede reportar el UPN en su lugar, que a veces no coincide con el correo real
  (`camilo.roman@shatter.onmicrosoft.com` en vez de
  `ingeniero.ia@seguridadshatter.com`). Conviene revisar que las ~5 personas que
  van a usar esto tengan `mail` poblado **antes** de empezar a gestionar
  anomalías: cambiarlo después no reescribe el historial ya escrito.
- Si a alguien le cambian el correo, las filas viejas conservan el anterior. Eso
  es **correcto** para un rastro de auditoría —registra lo que era cierto
  entonces— pero hay que saberlo al leer el historial.

## 3. Crear la aplicación de Access

**Zero Trust → Access → Applications → Add an application**, tipo
*Self-hosted*.

| Campo | Valor |
|---|---|
| Application name | `Auditoría de mallas de turnos` |
| Session duration | `24 hours` |
| Application domain | `turnos.seguridadshatter.com` |
| Identity providers | `Entra ID` (el del paso 2). **Desmarcar el resto**, incluido *One-time PIN*: si queda activo, cualquiera con un correo del dominio entra con un código enviado por correo, sin pasar por el MFA de la empresa |

Y una política:

| Campo | Valor |
|---|---|
| Policy name | `Personal autorizado` |
| Action | **Allow** |
| Include | `Emails ending in` → `@seguridadshatter.com` |

> Si se prefiere lista explícita en vez de dominio completo, usar
> `Include → Emails` con los correos de las tres audiencias (programador,
> nómina, gerencia). Es más restrictivo y más trabajo de mantener; con ~5
> personas es perfectamente viable y probablemente lo correcto para datos de
> 404 trabajadores.

### La comprobación que NO se puede omitir

**Que no quede ninguna política con Action = `Bypass`.**

Una `Bypass` deja pasar sin autenticar. Como la API confía en la cabecera que
pone Access, una `Bypass` equivale a **volver al modo declarativo sin ningún
aviso**: las peticiones llegarían sin `Cf-Access-Authenticated-User-Email`, la
API devolvería `403` en las escrituras… pero todas las **lecturas** quedarían
abiertas a internet, con nombres y cédulas de 404 trabajadores.

Cloudflare a veces sugiere una `Bypass` para rutas de salud o webhooks. Aquí no
hace falta ninguna: el healthcheck corre *dentro* del contenedor.

---

## 4. `cloudflared` como servicio de compose

**Ya está añadido** al `docker-compose.yml` bajo el profile `produccion`, así
que no hay que escribirlo. Dos detalles del cómo, por si alguien lo toca:

- **El token va por entorno (`TUNNEL_TOKEN`), no como `--token`.** Un argumento
  de la línea de comandos queda visible en `docker inspect`, en la lista de
  procesos del host y en cualquier log que imprima el comando. `cloudflared` lee
  `TUNNEL_TOKEN` de forma nativa.
- **La variable se interpola con `:-` y no con `:?`.** Compose evalúa
  `${VAR:?...}` **aunque el servicio no esté en el perfil activo**, así que
  exigirla rompía `docker compose config` y el arranque en local, donde no hay
  túnel.

En el `.env` del servidor:

```
MODO_IDENTIDAD=PROXY
CABECERA_IDENTIDAD=Cf-Access-Authenticated-User-Email
CLOUDFLARE_TUNNEL_TOKEN=...
```

Levantar con:

```bash
docker compose --profile produccion up -d api cloudflared
```

### Quitar el puerto del host

Con el túnel dentro de la red de compose, el servicio `api` **ya no necesita
publicar ningún puerto**. Hoy publica en `127.0.0.1:8000`, que ya es seguro,
pero sin puerto la superficie es todavía menor: la API deja de ser alcanzable
incluso desde la propia EC2.

En el servidor, comentar el bloque `ports:` de `api`. Se deja en el repo porque
en desarrollo local hace falta llegar al dashboard desde el navegador.

> Consecuencia práctica: para depurar en el servidor ya no vale `curl
> localhost:8000`. Se usa `docker compose exec api curl -s localhost:8000/salud`
> o se mira el hostname público.

---

## 5. Verificación

Cuatro comprobaciones, en este orden. Las dos últimas son las que importan.

```bash
# 1. El túnel está conectado
docker compose --profile produccion logs cloudflared | grep -i "registered\|connection"

# 2. La API responde por dentro
docker compose exec api curl -s localhost:8000/salud
```

**3. A través del hostname, `/identidad` debe reportar modo `PROXY` y tu correo
real.** Desde el navegador, ya autenticado:

```
https://turnos.seguridadshatter.com/identidad
→ {"modo":"PROXY","usuario":"tu.correo@seguridadshatter.com"}
```

Si `usuario` viene `null`, Access no está inyectando la cabecera: revisar que la
aplicación cubra ese hostname y que la política sea `Allow` y no `Bypass`.

**4. Sin autenticar no se debe ver nada.** En una ventana privada, sin sesión:

```
https://turnos.seguridadshatter.com/kpi
→ debe redirigir al login, NUNCA devolver JSON
```

Si devuelve JSON, hay una `Bypass` activa o la aplicación no cubre el
hostname. **Eso sería una fuga de PII, no una molestia de configuración**:
párese el túnel (`docker compose stop cloudflared`) hasta arreglarlo.

### Y en el dashboard

Abrirlo por el hostname. El botón de usuario debe mostrar **el correo
verificado**, no "Identificarse". Si se hace clic, debe avisar que la identidad
viene del inicio de sesión y no se puede cambiar. Al gestionar una anomalía, la
fila de `anomalias_historial` debe quedar con ese correo.

---

## 6. Lo que esto NO resuelve

- **No verifica el JWT firmado.** La API confía en la cabecera plana, y la
  garantía de que nadie más la puede poner viene de la **red** (sin puerto en el
  host, sin ingreso en el grupo de seguridad). Si algún día la API queda
  alcanzable directamente, hay que pasar a validar `Cf-Access-Jwt-Assertion`.
  Está anotado en `CLAUDE.md` §7.
- **No autoriza por rol.** Access decide *quién entra*, no *qué bandeja ve*.
  Hoy cualquiera que entre ve las tres. Segmentar por audiencia (programador /
  nómina / gerencia) sería un trabajo aparte, sobre el correo que ya llega
  verificado.
  Si activaste *Support groups* en el paso 2.2, los grupos de seguridad de Entra
  quedan disponibles en las políticas de Access, así que ese trabajo futuro se
  apoyaría en los grupos que la empresa ya administra en lugar de una lista de
  correos aparte. **Activarlo ahora cuesta un permiso de Graph; retrofitearlo
  después obliga a volver a pedir consentimiento de administrador**, que es el
  paso que depende de otra persona.
- **No cubre el pipeline diario.** Ese no pasa por la API: corre en la misma
  máquina y habla directo con PostgreSQL.
