-- Historial de actualizaciones: una fila por corrida del pipeline diario.
--
-- Para que cualquier rol pueda ver en el dashboard (pestana Historial) que trajo
-- cada actualizacion: que turnos cambiaron en SERPI, que anomalias se
-- corrigieron y cuales aparecieron, y si la corrida fallo. Sin esto, la unica
-- forma de saber que paso una noche era leer el journal del servidor.
--
-- Idempotente: se puede correr varias veces.

CREATE TABLE IF NOT EXISTS corridas (
    id             BIGSERIAL PRIMARY KEY,
    inicio         TIMESTAMPTZ NOT NULL DEFAULT now(),
    fin            TIMESTAMPTZ,
    desde          DATE,
    hasta          DATE,
    estado         TEXT NOT NULL DEFAULT 'EN_CURSO'
                   CHECK (estado IN ('EN_CURSO', 'OK', 'FALLO', 'REVISAR')),
    codigo_salida  INTEGER,
    mensaje        TEXT,
    -- Version del codigo que corrio (VERSION del despliegue), para poder
    -- atribuir un cambio de cifras a un cambio de reglas.
    version        TEXT,
    -- Por mes 'YYYY-MM': {turnos, nuevos, modificados, borrados, primera_carga,
    -- detalle: [{tipo, cedula, guarda, puesto, fecha, antes, despues}]}
    meses          JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- {vigentes_antes, vigentes, nuevas, corregidas, por_regla,
    --  lista_nuevas, lista_corregidas}
    anomalias      JSONB
);

CREATE INDEX IF NOT EXISTS idx_corridas_inicio ON corridas (inicio DESC);
