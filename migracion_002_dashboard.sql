-- Migracion 002 — capa de reportes y workflow del dashboard
--
-- La migracion 001 (columna `slot` en turnos/horas_declaradas_mes) ya esta
-- aplicada y quedo incorporada directamente en schema.sql.
--
-- Esta migracion habilita tres cosas que el dashboard necesita y que hoy
-- no existen:
--
--   1. IDENTIDAD ESTABLE de cada anomalia (`huella`). Sin esto, cada corrida
--      del motor duplica las anomalias que ya tienen ticket: `borrar_abiertas`
--      solo borra las ABIERTA, pero el INSERT posterior no sabe que la
--      violacion ya existia en estado TICKET_CREADO y la vuelve a crear.
--      Verificado: un guarda paso de 9 a 10 anomalias al re-correr el motor.
--      En el flujo n8n eso significaria abrir un ticket nuevo en SERPI cada
--      mes por la misma anomalia sin resolver.
--
--      Se usa una huella (hash de llaves de negocio) en vez de una UNIQUE
--      sobre columnas: `puesto_id` es NULL para casi todas las reglas y en
--      Postgres NULL <> NULL, asi que una UNIQUE nunca dispararia — es
--      exactamente la trampa que ya documenta CLAUDE.md para `turnos`.
--
--   2. TAXONOMIA POR RESPONSABLE como dato, no como codigo. Cada regla dice
--      si su incumplimiento es PUNTUAL (lo corrige el programador caso a
--      caso), ESTRUCTURAL (exige que gerencia rediseñe el patron de turnos)
--      o ADMINISTRATIVA (lo concilia nomina). Es el eje sobre el que se
--      arma el dashboard: cada audiencia ve su bandeja.
--
--   3. TRAZABILIDAD del workflow (`anomalias_historial`). El dashboard tiene
--      escritura, asi que hay que poder responder "quien cerro esta anomalia,
--      cuando y con que justificacion" — lo que exige la Circular 0040 de
--      2026 y el principio de trazabilidad de CLAUDE.md §9.
--
-- Ejecutar con: psql -U turnos_app -d turnos -f migracion_002_dashboard.sql
-- Es idempotente: se puede correr varias veces.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Taxonomia de reglas por responsable
-- ---------------------------------------------------------------------------

ALTER TABLE reglas_anomalia
    ADD COLUMN IF NOT EXISTS naturaleza  TEXT,
    ADD COLUMN IF NOT EXISTS responsable TEXT;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reglas_anomalia_naturaleza_check') THEN
        ALTER TABLE reglas_anomalia ADD CONSTRAINT reglas_anomalia_naturaleza_check
            CHECK (naturaleza IN ('PUNTUAL', 'ESTRUCTURAL', 'ADMINISTRATIVA'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'reglas_anomalia_responsable_check') THEN
        ALTER TABLE reglas_anomalia ADD CONSTRAINT reglas_anomalia_responsable_check
            CHECK (responsable IN ('PROGRAMADOR', 'GERENCIA', 'NOMINA'));
    END IF;
END $$;

-- Puntual = error de asignacion concreto, se corrige moviendo un turno.
-- Estructural = consecuencia del patron 2x2x2, que de base ya opera ~56h/semana;
--               no se arregla caso a caso, exige rediseñar la plantilla.
-- Administrativa = el turno esta bien, lo que no cuadra es el registro de horas.
UPDATE reglas_anomalia SET naturaleza = 'PUNTUAL', responsable = 'PROGRAMADOR'
 WHERE codigo IN ('CRUCE_DE_HORARIO',
                  'DESCANSO_ENTRE_JORNADAS_INSUFICIENTE',
                  'JORNADA_DIARIA_SUPERA_12H');

UPDATE reglas_anomalia SET naturaleza = 'ESTRUCTURAL', responsable = 'GERENCIA'
 WHERE codigo IN ('SEMANA_SUPERA_60H',
                  'SIN_DESCANSO_SEMANAL',
                  'RACHA_SIN_DESCANSO');

UPDATE reglas_anomalia SET naturaleza = 'ADMINISTRATIVA', responsable = 'NOMINA'
 WHERE codigo = 'DESCUADRE_HORAS_DECLARADAS';

-- ---------------------------------------------------------------------------
-- 2. Identidad estable + contexto + workflow en `anomalias`
-- ---------------------------------------------------------------------------

ALTER TABLE anomalias
    -- Hash de llaves de negocio (regla + cedula + clave natural del detector).
    -- Es lo que permite re-correr el motor sin duplicar lo ya gestionado.
    ADD COLUMN IF NOT EXISTS huella          TEXT,
    -- Puesto al que se atribuye la anomalia cuando aplica (DESCUADRE es por
    -- guarda-puesto-mes). NULL en las reglas que son transversales al guarda.
    ADD COLUMN IF NOT EXISTS puesto_id       INTEGER REFERENCES puestos(id),
    -- Justificacion que escribe quien la gestiona en el dashboard.
    ADD COLUMN IF NOT EXISTS nota            TEXT,
    ADD COLUMN IF NOT EXISTS actualizado_en  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS actualizado_por TEXT;

-- Las anomalias que ya existen no tienen huella. Se les calcula una
-- provisional para no dejarlas fuera de la UNIQUE; la proxima corrida del
-- motor las regenera con la huella definitiva.
UPDATE anomalias a
   SET huella = md5(r.codigo || '|' || a.guarda_cedula || '|' ||
                    COALESCE(a.fecha_referencia::text, '') || '|' ||
                    COALESCE(a.puesto_id::text, '') || '|' || a.id::text)
  FROM reglas_anomalia r
 WHERE r.id = a.regla_id AND a.huella IS NULL;

ALTER TABLE anomalias ALTER COLUMN huella SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_anomalias_huella ON anomalias (huella);
CREATE INDEX IF NOT EXISTS idx_anomalias_periodo
    ON anomalias (fecha_referencia);

-- ---------------------------------------------------------------------------
-- 3. Historial de gestion (trazabilidad ante el MinTrabajo)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS anomalias_historial (
    id               BIGSERIAL PRIMARY KEY,
    anomalia_id      BIGINT NOT NULL REFERENCES anomalias(id) ON DELETE CASCADE,
    estado_anterior  TEXT,
    estado_nuevo     TEXT NOT NULL,
    nota             TEXT,
    usuario          TEXT NOT NULL,
    ocurrido_en      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_historial_anomalia ON anomalias_historial (anomalia_id);

COMMIT;
