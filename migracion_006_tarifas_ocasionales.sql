-- Valorizacion de servicios ocasionales (pestana "Servicios ocasionales").
--
-- Reproduce el cotizador de Gerencia (Entradas/Cotizador_Vigilancia_2026.xlsx,
-- hojas Parametros y Ocasional), que aplica la Circular Externa
-- 20251300000115CS de la Supervigilancia. Los valores son DATOS con vigencia,
-- igual que reglas_anomalia: el 1 de enero cambia el SMLMV y entra otra fila,
-- sin tocar codigo, y un turno se valora con la tarifa vigente en su fecha.
--
-- Idempotente: se puede correr varias veces.

CREATE TABLE IF NOT EXISTS tarifas_vigilancia (
    id                    SERIAL PRIMARY KEY,
    vigente_desde         DATE NOT NULL,
    vigente_hasta         DATE,
    smlmv                 NUMERIC(14,2) NOT NULL,
    factor_smlmv          NUMERIC(8,4)  NOT NULL,   -- 10,21 empresarial
    seguro_vida_guarda    NUMERIC(12,2) NOT NULL,   -- mensual, por guarda
    guardas_por_puesto    NUMERIC(6,2)  NOT NULL,   -- puesto 24 h / 30 dias
    as_sin_arma           NUMERIC(6,4)  NOT NULL,   -- administracion y supervision
    as_con_arma           NUMERIC(6,4)  NOT NULL,
    prop_diurna           NUMERIC(6,4)  NOT NULL,   -- variable de proporcionalidad
    prop_nocturna         NUMERIC(6,4)  NOT NULL,
    jornada_diurna        NUMERIC(5,2)  NOT NULL,   -- horas (6:00 a 19:00)
    jornada_nocturna      NUMERIC(5,2)  NOT NULL,   -- horas (19:00 a 6:00)
    recargo_festivo       NUMERIC(6,4)  NOT NULL,   -- domingos y festivos
    sobrecosto_ocasional  NUMERIC(6,4)  NOT NULL,   -- politica comercial
    fuente                TEXT,
    UNIQUE (vigente_desde)
);

INSERT INTO tarifas_vigilancia
    (vigente_desde, vigente_hasta, smlmv, factor_smlmv, seguro_vida_guarda, guardas_por_puesto,
     as_sin_arma, as_con_arma, prop_diurna, prop_nocturna, jornada_diurna, jornada_nocturna,
     recargo_festivo, sobrecosto_ocasional, fuente)
VALUES
    ('2026-07-15', '2026-12-31', 1750905, 10.21, 4360, 3,
     0.08, 0.10, 0.4759, 0.5241, 13, 11,
     1.00, 0.20,
     'Circular Externa 20251300000115CS (Supervigilancia), secc. 1.3 y 1.4. Seguro de vida, '
     'recargo dominical/festivo (100%) y sobrecosto ocasional: politica de Gerencia. El '
     'sobrecosto es 20% (el cotizador traia 15% en Parametros y 20% en la hoja Ocasional; '
     'Gerencia confirmo 20%, oct-2026).')
ON CONFLICT (vigente_desde) DO NOTHING;

-- Modalidad de cada servicio ocasional. SERPI no la trae en la malla y cambia
-- la tarifa (8% vs 10% de A&S), asi que facturacion la confirma una vez por
-- puesto. Sin fila = SIN_ARMA por defecto, marcado como "sin confirmar".
CREATE TABLE IF NOT EXISTS ocasionales_puesto (
    puesto_id        INTEGER PRIMARY KEY REFERENCES puestos(id),
    modalidad        TEXT NOT NULL CHECK (modalidad IN ('SIN_ARMA', 'CON_ARMA')),
    nota             TEXT,
    actualizado_por  TEXT NOT NULL,
    actualizado_en   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Quien cambio la modalidad de que servicio, cuando y de que a que. Cambia el
-- valor que se factura: tiene que quedar rastro.
CREATE TABLE IF NOT EXISTS ocasionales_puesto_historial (
    id              BIGSERIAL PRIMARY KEY,
    puesto_id       INTEGER NOT NULL REFERENCES puestos(id),
    modalidad_antes TEXT,
    modalidad_nueva TEXT NOT NULL,
    nota            TEXT,
    usuario         TEXT NOT NULL,
    fecha           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ocasionales_hist_puesto ON ocasionales_puesto_historial (puesto_id, fecha DESC);
