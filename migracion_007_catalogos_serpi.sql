-- Catalogos de la API de turnos de SERPI (apis.serpi.com.co, seccion Turnos).
--
-- La API todavia NO trae la programacion dia a dia (eso sigue llegando por la
-- descarga del reporte), pero si los catalogos de alrededor: proyectos con el
-- NIT del cliente, centro de operacion y zona; puestos con su identificador
-- fijo; y que colaborador esta asignado a que puesto, con su cargo y contrato.
-- catalogos_serpi.py los copia aqui en cada corrida diaria.
--
-- Son una COPIA de SERPI, no datos propios: se reemplazan enteros en cada
-- sincronizacion. Idempotente.

CREATE TABLE IF NOT EXISTS serpi_proyectos (
    gdtproyectoid      INTEGER PRIMARY KEY,
    descripcion        TEXT,
    abreviatura        TEXT,
    cliente            TEXT,
    nit                TEXT,
    centro_operacion   TEXT,
    zona               TEXT,          -- "grupo" en SERPI, p.ej. ZONA NORTE CALI
    activo             BOOLEAN,
    sincronizado_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS serpi_puestos (
    gdtproyectoactid   INTEGER PRIMARY KEY,
    gdtproyectoid      INTEGER,
    nombre             TEXT,
    sincronizado_en    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Asignacion colaborador -> puesto. SERPI no expone la cedula aqui (solo el
-- nombre y el contrato), por eso se guarda el nombre normalizado para cruzar.
CREATE TABLE IF NOT EXISTS serpi_asignaciones (
    grhcontratoid      INTEGER NOT NULL,
    gdtproyectoactid   INTEGER NOT NULL,
    fechaini           DATE NOT NULL,
    fechafin           DATE,
    colaborador        TEXT,
    colaborador_norm   TEXT,
    cargo              TEXT,
    activo             BOOLEAN,
    sincronizado_en    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grhcontratoid, gdtproyectoactid, fechaini)
);
CREATE INDEX IF NOT EXISTS idx_serpi_asig_nombre ON serpi_asignaciones (colaborador_norm);

-- A que puesto de SERPI corresponde cada puesto de la malla. La malla nombra
-- los puestos con un sufijo (p.ej. "N.U PORTERIA - CELULAR" es "N.U PORTERIA"
-- en la API) y el mismo nombre se repite entre clientes, asi que el cruce se
-- decide por nombre Y por los guardas que trabajan alli (ver
-- catalogos_serpi.vincular_puestos). `metodo` deja constancia de como se
-- decidio, para poder auditar un vinculo dudoso.
CREATE TABLE IF NOT EXISTS serpi_puesto_vinculo (
    puesto_id          INTEGER PRIMARY KEY REFERENCES puestos(id),
    gdtproyectoactid   INTEGER NOT NULL,
    gdtproyectoid      INTEGER,
    metodo             TEXT NOT NULL,
    guardas_en_comun   INTEGER,
    vinculado_en       TIMESTAMPTZ NOT NULL DEFAULT now()
);
