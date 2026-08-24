-- Semillas del catálogo de reglas que evalúa motor_reglas.py.
-- Ejecutar con: psql -U turnos_app -d turnos -f seed_reglas.sql
--
-- Idempotente: la llave natural es (codigo, vigente_desde), así que correrlo
-- dos veces no duplica el catálogo. Importa: reglas duplicadas harían que cada
-- hallazgo se contara dos veces.

INSERT INTO reglas_anomalia (codigo, descripcion, severidad_default, parametros, fundamento_legal, vigente_desde, vigente_hasta, naturaleza, responsable) VALUES
('CRUCE_DE_HORARIO', 'Mismo guarda con turnos que se solapan en el tiempo', 'CRITICA', '{}', 'Imposibilidad física; Circular 0040 de 2026 MinTrabajo sobre registro trazable de horas', '2026-01-01', NULL, 'PUNTUAL', 'PROGRAMADOR'),
('DESCANSO_ENTRE_JORNADAS_INSUFICIENTE', 'Menos de 12h continuas entre el fin de una jornada y el inicio de la siguiente', 'ALTA', '{"umbral_horas": 12}', 'Ley 2466 de 2025 (lineamiento de descanso mínimo); Art. 167 CST', '2026-01-01', NULL, 'PUNTUAL', 'PROGRAMADOR'),
('JORNADA_DIARIA_SUPERA_12H', 'Suma de horas trabajadas en un mismo día calendario superior a 12h', 'ALTA', '{"umbral_horas": 12}', 'Art. 7 Ley 1920 de 2018', '2026-01-01', NULL, 'PUNTUAL', 'PROGRAMADOR'),
('SEMANA_SUPERA_60H', 'Ventana móvil de 7 días con más de 60h trabajadas', 'ALTA', '{"umbral_horas": 60}', 'Art. 7 Ley 1920 de 2018 (tope semanal absoluto)', '2026-01-01', NULL, 'ESTRUCTURAL', 'GERENCIA'),
('SIN_DESCANSO_SEMANAL', 'Ventana móvil de 7 días sin ningún día de descanso', 'ALTA', '{}', 'Art. 172 y 175 CST', '2026-01-01', NULL, 'ESTRUCTURAL', 'GERENCIA'),
('RACHA_SIN_DESCANSO', 'Más de 6 días consecutivos trabajados sin un día de descanso', 'ALTA', '{"umbral_dias": 6}', 'Art. 172 y 175 CST', '2026-01-01', NULL, 'ESTRUCTURAL', 'GERENCIA'),
('DESCUADRE_HORAS_DECLARADAS', 'El Total Horas declarado no coincide con la suma de sus 4 categorías', 'BAJA', '{}', 'Circular 0040 de 2026 MinTrabajo (registro riguroso de horas)', '2026-01-01', NULL, 'ADMINISTRATIVA', 'NOMINA')
ON CONFLICT (codigo, vigente_desde) DO UPDATE SET
    descripcion       = EXCLUDED.descripcion,
    severidad_default = EXCLUDED.severidad_default,
    parametros        = EXCLUDED.parametros,
    fundamento_legal  = EXCLUDED.fundamento_legal,
    vigente_hasta     = EXCLUDED.vigente_hasta,
    naturaleza        = EXCLUDED.naturaleza,
    responsable       = EXCLUDED.responsable;


-- ---------------------------------------------------------------------------
-- Jornada ORDINARIA de referencia (Ley 2101 de 2021): 44h hasta el 14/07/2026,
-- 42h desde el 15/07/2026. NO está activada todavía, y es deliberado:
--
--   * Julio de 2026 ya se facturó y se pagó nómina con la regla vieja de 44h.
--     Activarla retroactivamente reabriría un mes ya cerrado contablemente.
--   * Superar la jornada de referencia NO es ilegal en vigilancia: la Ley 1920
--     permite hasta 60h con suplementarias, y ese tope absoluto sí lo cubre
--     SEMANA_SUPERA_60H. Lo que exige la ley es que esas horas se paguen con
--     recargo y queden registradas (Circular 0040). Es un asunto de
--     LIQUIDACIÓN, no un incumplimiento — por eso va como ADMINISTRATIVA/NOMINA
--     y no como una violación ALTA, que inflaría los hallazgos y le quitaría
--     credibilidad al informe.
--
-- Para activarla desde agosto de 2026, descomenta y corre el motor acotado:
--     python motor_reglas.py --desde 2026-08-01
--
-- INSERT INTO reglas_anomalia (codigo, descripcion, severidad_default, parametros, fundamento_legal, vigente_desde, vigente_hasta, naturaleza, responsable) VALUES
-- ('JORNADA_REFERENCIA_SUPERADA', 'Semana que supera la jornada ordinaria de referencia; las horas por encima son suplementarias y deben liquidarse con recargo', 'BAJA', '{"umbral_horas": 42}', 'Art. 161 CST modificado por Ley 2101 de 2021; Circular 0040 de 2026 MinTrabajo', '2026-08-01', NULL, 'ADMINISTRATIVA', 'NOMINA')
-- ON CONFLICT (codigo, vigente_desde) DO NOTHING;
