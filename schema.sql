-- Esquema: malla de turnos + motor de anomalías
-- Seguridad Shatter de Colombia LTDA BIC
--
-- Ejecutar con: psql -U turnos_app -d turnos -f schema.sql

CREATE TABLE IF NOT EXISTS clientes (
    id      SERIAL PRIMARY KEY,
    nombre  TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS puestos (
    id           SERIAL PRIMARY KEY,
    cliente_id   INTEGER NOT NULL REFERENCES clientes(id),
    nombre       TEXT NOT NULL,
    hoja_origen  TEXT,                     -- nombre de la hoja del Excel de donde vino (trazabilidad)
    UNIQUE (cliente_id, nombre)
);

CREATE TABLE IF NOT EXISTS guardas (
    cedula   TEXT PRIMARY KEY,
    nombre   TEXT NOT NULL
);

-- Catálogo de tipos de turno. Se completa solo mientras corre el ETL:
-- códigos con letra (E, F, A, B, C, ESC, OCA, ADI, VAC, INC, LIC, LICNR...)
-- y códigos sintéticos para celdas que solo traen horario sin letra
-- (T_HHMM_HHMM), para no perder esos turnos.
CREATE TABLE IF NOT EXISTS tipos_turno (
    codigo           TEXT PRIMARY KEY,
    hora_inicio      TIME,
    hora_fin         TIME,
    duracion_horas   NUMERIC(4,2),
    categoria        TEXT NOT NULL CHECK (categoria IN ('TRABAJADO', 'AUSENCIA'))
);

CREATE TABLE IF NOT EXISTS turnos (
    id                 BIGSERIAL PRIMARY KEY,
    guarda_cedula      TEXT NOT NULL REFERENCES guardas(cedula),
    puesto_id          INTEGER NOT NULL REFERENCES puestos(id),
    fecha              DATE NOT NULL,
    slot               INTEGER NOT NULL DEFAULT 1,  -- numero de slot/fila dentro del puesto (col C del Excel)
    tipo_turno_codigo  TEXT NOT NULL REFERENCES tipos_turno(codigo),
    hora_inicio        TIME,
    hora_fin           TIME,
    horas_calculadas   NUMERIC(5,2),
    origen             TEXT NOT NULL DEFAULT 'EXCEL_EXPORT',   -- EXCEL_EXPORT | API_SERPI
    fecha_carga        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- La llave natural es guarda-puesto-dia-slot, NO solo guarda-puesto-dia:
    -- un mismo guarda puede tener mas de una fila (slot) asignada dentro del
    -- MISMO puesto el mismo dia (p.ej. su turno regular en el slot 1 + un
    -- turno ADI/adicional en el slot 2). Sin el slot en la llave, el
    -- ON CONFLICT colapsaba esas asignaciones concurrentes reales y perdia
    -- turnos -- incluyendo cruces de horario CRITICOS. OJO: no incluir
    -- hora_inicio aqui; en Postgres NULL <> NULL en una UNIQUE, y
    -- hora_inicio es NULL para las ausencias (VAC/INC/LIC/LICNR), lo que
    -- duplicaria esas filas en cada corrida en vez de actualizarlas.
    UNIQUE (guarda_cedula, puesto_id, fecha, slot)
);
CREATE INDEX IF NOT EXISTS idx_turnos_guarda_fecha ON turnos (guarda_cedula, fecha);
CREATE INDEX IF NOT EXISTS idx_turnos_puesto_fecha ON turnos (puesto_id, fecha);

-- Totales declarados en el encabezado de cada bloque de SERPI (Horas Diurnas
-- Ordinarias/Festivas, Nocturnas Ordinarias/Festivas, Total Horas), por
-- guarda-puesto-mes. Sirve para el chequeo de consistencia contra la malla.
CREATE TABLE IF NOT EXISTS horas_declaradas_mes (
    id                            SERIAL PRIMARY KEY,
    guarda_cedula                 TEXT NOT NULL REFERENCES guardas(cedula),
    puesto_id                     INTEGER NOT NULL REFERENCES puestos(id),
    slot                          INTEGER NOT NULL DEFAULT 1,  -- ver nota en turnos.slot: un guarda puede tener mas de un bloque de horas declaradas por puesto (uno por slot)
    anio                          INTEGER NOT NULL,
    mes                           INTEGER NOT NULL,
    horas_diurnas_ordinarias      NUMERIC(6,2),
    horas_diurnas_festivas        NUMERIC(6,2),
    horas_nocturnas_ordinarias    NUMERIC(6,2),
    horas_nocturnas_festivas      NUMERIC(6,2),
    total_horas                   NUMERIC(6,2),
    UNIQUE (guarda_cedula, puesto_id, slot, anio, mes)
);

-- Motor de reglas: cada regla es una fila, con vigencia por fecha. Así el
-- cambio de tope semanal (44h -> 42h el 15 de julio de 2026) se resuelve con
-- datos, no con ifs en el código.
CREATE TABLE IF NOT EXISTS reglas_anomalia (
    id                  SERIAL PRIMARY KEY,
    codigo              TEXT NOT NULL,
    descripcion         TEXT,
    severidad_default   TEXT NOT NULL CHECK (severidad_default IN ('CRITICA','ALTA','BAJA')),
    parametros          JSONB,             -- ej. {"umbral_horas": 60}
    fundamento_legal    TEXT,
    vigente_desde       DATE,
    vigente_hasta       DATE,              -- NULL = sigue vigente
    -- Taxonomia por responsable: es el eje sobre el que se arma el dashboard,
    -- porque define QUIEN puede hacer algo con cada hallazgo.
    --   PUNTUAL        error de asignacion concreto; se corrige moviendo un turno.
    --   ESTRUCTURAL    consecuencia del patron 2x2x2, que de base ya opera cerca
    --                  del tope legal; no se arregla caso a caso, exige rediseñar
    --                  la plantilla.
    --   ADMINISTRATIVA el turno esta bien; lo que no cuadra es el registro de horas.
    naturaleza          TEXT CHECK (naturaleza IN ('PUNTUAL','ESTRUCTURAL','ADMINISTRATIVA')),
    responsable         TEXT CHECK (responsable IN ('PROGRAMADOR','GERENCIA','NOMINA')),
    -- Una regla puede tener VARIAS versiones vigentes en tramos distintos (es
    -- justo el caso del tope de referencia 44h/42h), asi que la llave natural
    -- es el codigo mas su fecha de entrada en vigencia. Ademas hace que
    -- seed_reglas.sql se pueda correr dos veces sin duplicar el catalogo — y
    -- catalogo duplicado significa hallazgos contados doble.
    UNIQUE (codigo, vigente_desde)
);

CREATE TABLE IF NOT EXISTS anomalias (
    id                     BIGSERIAL PRIMARY KEY,
    -- Identidad estable derivada de llaves de NEGOCIO (regla + cedula + clave
    -- natural del detector). Es lo que permite re-correr el motor sin duplicar
    -- lo que alguien ya gestiono. Se usa una huella y no un UNIQUE de columnas
    -- porque puesto_id es NULL en casi todas las reglas, y en Postgres
    -- NULL <> NULL haria que el UNIQUE nunca dispare — la misma trampa que ya
    -- documenta este archivo para `turnos`.
    huella                 TEXT NOT NULL UNIQUE,
    regla_id               INTEGER REFERENCES reglas_anomalia(id),
    guarda_cedula          TEXT REFERENCES guardas(cedula),
    -- Puesto al que se atribuye la anomalia cuando aplica (DESCUADRE es por
    -- guarda-puesto-mes). NULL en las reglas transversales al guarda.
    puesto_id              INTEGER REFERENCES puestos(id),
    fecha_referencia       DATE,
    severidad              TEXT NOT NULL CHECK (severidad IN ('CRITICA','ALTA','BAJA')),
    detalle                TEXT,
    turnos_involucrados    BIGINT[],
    -- Estados del proceso real. El usuario NO corrige aqui: corrige en SERPI.
    --   ABIERTA      detectada, nadie la ha mirado          (la pone el motor)
    --   EN_REVISION  alguien la trabaja / va a corregirla   (la pone el usuario)
    --   JUSTIFICADA  revisada y se acepta como esta         (usuario + nota obligatoria)
    --   RESUELTA     ya no aparece en el cargue mas reciente (SOLO la pone el motor)
    -- RESUELTA no puede ponerse a mano a proposito: significa "verificado contra
    -- los datos", no "alguien dijo que si".
    estado                 TEXT NOT NULL DEFAULT 'ABIERTA'
                           CHECK (estado IN ('ABIERTA','EN_REVISION','JUSTIFICADA','RESUELTA')),
    nota                   TEXT,          -- justificacion de quien la gestiona
    actualizado_en         TIMESTAMPTZ,
    actualizado_por        TEXT,
    ticket_serpi_id        TEXT,          -- sin uso: Soporte>Tickets de SERPI es
                                          -- para escalar con el proveedor, no
                                          -- para corregir turnos. Se deja por si
                                          -- algun dia se referencia desde aqui.
    fecha_deteccion        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_anomalias_guarda ON anomalias (guarda_cedula);
CREATE INDEX IF NOT EXISTS idx_anomalias_estado ON anomalias (estado);
CREATE INDEX IF NOT EXISTS idx_anomalias_periodo ON anomalias (fecha_referencia);
CREATE INDEX IF NOT EXISTS idx_anomalias_estado_fecha ON anomalias (estado, fecha_referencia);

-- Trazabilidad del workflow. El dashboard tiene escritura, asi que hay que poder
-- responder "quien cerro esta anomalia, cuando y con que justificacion" — lo que
-- exige la Circular 0040 de 2026. Se escribe SIEMPRE en la misma transaccion que
-- el cambio de estado: si falla la auditoria, no se aplica el cambio.
CREATE TABLE IF NOT EXISTS anomalias_historial (
    id               BIGSERIAL PRIMARY KEY,
    anomalia_id      BIGINT NOT NULL REFERENCES anomalias(id) ON DELETE CASCADE,
    estado_anterior  TEXT,
    estado_nuevo     TEXT NOT NULL,
    nota             TEXT,
    usuario          TEXT NOT NULL,   -- persona, o 'motor_reglas' cuando lo hace el sistema
    ocurrido_en      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_historial_anomalia ON anomalias_historial (anomalia_id);
