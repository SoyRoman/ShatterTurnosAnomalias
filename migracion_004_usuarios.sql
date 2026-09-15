-- ---------------------------------------------------------------------------
-- Autenticacion propia: usuarios, sesiones y bitacora de acceso.
--
--   psql -U turnos_app -d turnos -f migracion_004_usuarios.sql
--
-- Idempotente, como el resto de los .sql del proyecto.
--
-- Por que autenticacion propia y no un proveedor externo: decision explicita
-- del usuario (septiembre 2026). Todo el control de acceso vive aqui, sin
-- depender de Cloudflare, Entra ni ningun tercero. El administrador crea las
-- cuentas y se las entrega a quien deba entrar.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- usuarios
-- ---------------------------------------------------------------------------
-- El `rol` reutiliza la taxonomia que ya organiza el dashboard (CLAUDE.md §5):
-- cada audiencia tiene su bandeja. ADMIN es el unico que ademas gestiona
-- cuentas. No se inventa un sistema de permisos nuevo: el rol ES la bandeja.
CREATE TABLE IF NOT EXISTS usuarios (
    id                  BIGSERIAL PRIMARY KEY,

    -- Con lo que se inicia sesion. Se guarda en minusculas (lo normaliza la
    -- aplicacion) para que "Ana" y "ana" no sean dos cuentas distintas:
    -- dos cuentas para una persona romperian la atribucion del historial.
    usuario             TEXT NOT NULL UNIQUE,

    -- Nombre para mostrar. Es lo que vera quien lea el historial dentro de seis
    -- meses, asi que vale la pena que sea el nombre real y no un alias.
    nombre              TEXT NOT NULL,
    correo              TEXT,

    -- Hash bcrypt. NUNCA la clave en claro: si alguien obtiene un volcado de
    -- esta base, no puede entrar ni reutilizar la clave en otros sistemas.
    hash_clave          TEXT NOT NULL,

    rol                 TEXT NOT NULL
                        CHECK (rol IN ('ADMIN', 'PROGRAMADOR', 'NOMINA', 'GERENCIA')),

    -- Desactivar en vez de borrar. Si se borrara la fila, el historial de
    -- auditoria quedaria apuntando a un usuario inexistente y ya no se podria
    -- responder "quien justifico esta anomalia" — que es justo lo que este
    -- sistema tiene que poder responder ante el Ministerio del Trabajo.
    activo              BOOLEAN NOT NULL DEFAULT TRUE,

    -- El administrador crea la cuenta con una clave temporal y se la entrega a
    -- la persona; el sistema la obliga a cambiarla en el primer ingreso. Asi
    -- nadie opera con una clave que un tercero conoce.
    debe_cambiar_clave  BOOLEAN NOT NULL DEFAULT TRUE,

    -- Freno a la fuerza bruta. Se cuenta por cuenta, no por IP: una lista de
    -- IPs no sirve cuando todos salen por la misma NAT de la oficina.
    intentos_fallidos   INTEGER NOT NULL DEFAULT 0,
    bloqueado_hasta     TIMESTAMPTZ,

    ultimo_ingreso      TIMESTAMPTZ,
    creado_en           TIMESTAMPTZ NOT NULL DEFAULT now(),
    creado_por          TEXT,
    actualizado_en      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_usuarios_activo ON usuarios (activo);

-- ---------------------------------------------------------------------------
-- sesiones
-- ---------------------------------------------------------------------------
-- Sesiones EN SERVIDOR, no un JWT autocontenido. La diferencia importa aqui:
-- un JWT firmado es valido hasta que expira y no hay forma de anularlo, asi que
-- si alguien deja de trabajar en la empresa a las 10 de la manana su token
-- sigue abriendo el dashboard hasta que caduque. Con sesiones en tabla, el
-- administrador desactiva la cuenta y el acceso se corta en la siguiente
-- peticion. Para una herramienta con PII de 404 trabajadores, poder cortar el
-- acceso *ya* vale mas que ahorrarse una consulta.
CREATE TABLE IF NOT EXISTS sesiones (
    -- SHA-256 del token, no el token. Mismo criterio que con las claves: un
    -- volcado de esta tabla no debe entregar sesiones vivas y listas para usar.
    -- El token en claro solo existe en la cookie del navegador.
    token_hash   TEXT PRIMARY KEY,

    usuario_id   BIGINT NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    creada_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expira_en    TIMESTAMPTZ NOT NULL,
    ultima_vez   TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Contexto para investigar un acceso raro. No es identificacion.
    ip           TEXT,
    agente       TEXT
);

CREATE INDEX IF NOT EXISTS idx_sesiones_usuario ON sesiones (usuario_id);
CREATE INDEX IF NOT EXISTS idx_sesiones_expira ON sesiones (expira_en);

-- ---------------------------------------------------------------------------
-- accesos
-- ---------------------------------------------------------------------------
-- Quien entro, quien lo intento y falló. Separado de `anomalias_historial`
-- porque responden preguntas distintas: aquel dice quien cambio un dato, este
-- dice quien tuvo acceso al sistema. Ante una fuga de PII, la pregunta que hay
-- que poder responder es la segunda.
--
-- Se guarda el texto del usuario intentado y no una FK: un intento contra una
-- cuenta que no existe tambien es informacion (alguien probando nombres).
CREATE TABLE IF NOT EXISTS accesos (
    id           BIGSERIAL PRIMARY KEY,
    usuario      TEXT,
    evento       TEXT NOT NULL
                 CHECK (evento IN ('INGRESO', 'FALLO', 'SALIDA', 'BLOQUEO',
                                   'CLAVE_CAMBIADA', 'SESION_EXPIRADA')),
    detalle      TEXT,
    ip           TEXT,
    agente       TEXT,
    ocurrido_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_accesos_usuario ON accesos (usuario, ocurrido_en DESC);
CREATE INDEX IF NOT EXISTS idx_accesos_evento ON accesos (evento, ocurrido_en DESC);

-- ---------------------------------------------------------------------------
-- Vista de apoyo para la pantalla de administracion.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_usuarios AS
SELECT u.id,
       u.usuario,
       u.nombre,
       u.correo,
       u.rol,
       u.activo,
       u.debe_cambiar_clave,
       u.ultimo_ingreso,
       u.creado_en,
       u.creado_por,
       (u.bloqueado_hasta IS NOT NULL AND u.bloqueado_hasta > now()) AS bloqueado,
       u.bloqueado_hasta,
       (SELECT count(*) FROM sesiones s
         WHERE s.usuario_id = u.id AND s.expira_en > now())          AS sesiones_activas
  FROM usuarios u
 ORDER BY u.activo DESC, u.nombre;
