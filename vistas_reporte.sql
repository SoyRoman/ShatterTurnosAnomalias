-- Capa semantica (read model) para el dashboard y los informes de n8n.
--
-- PATRON: separacion lectura/escritura (CQRS). El modelo de escritura
-- (turnos, anomalias) esta normalizado para que el ETL y el motor sean
-- rapidos e idempotentes; este modelo de LECTURA esta desnormalizado y
-- rotulado en lenguaje de negocio para que el dashboard y los correos no
-- tengan que rehacer joins ni reglas de presentacion.
--
-- Toda la logica de presentacion vive aqui, en SQL, y no repartida entre el
-- dashboard, la API y n8n. Si mañana cambia como se rotula una severidad,
-- se cambia en un solo lugar.
--
-- Son VIEW y no MATERIALIZED VIEW a proposito: el universo es ~400 guardas
-- y ~400 anomalias por mes, donde el join cuesta milisegundos. Materializar
-- agregaria un paso de REFRESH al flujo de n8n y la posibilidad de servir
-- datos viejos, a cambio de nada. Si el historico crece a varios años y
-- alguna consulta se siente lenta, se materializan estas mismas vistas sin
-- tocar quien las consume.
--
-- Ejecutar con: psql -U turnos_app -d turnos -f vistas_reporte.sql

-- ---------------------------------------------------------------------------
-- Vista base: una fila por anomalia, ya resuelta contra sus dimensiones.
-- Es la unica que hace el trabajo pesado; las demas se apoyan en ella.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_anomalias AS
SELECT
    a.id,
    a.huella,
    to_char(a.fecha_referencia, 'YYYY-MM')          AS periodo,
    a.fecha_referencia,
    r.codigo                                        AS regla,
    r.descripcion                                   AS regla_descripcion,
    r.fundamento_legal,
    r.naturaleza,
    r.responsable,
    a.severidad,
    -- Orden de urgencia para listas: lo critico primero, sin depender de que
    -- cada consumidor recuerde el orden correcto de un texto.
    CASE a.severidad WHEN 'CRITICA' THEN 1 WHEN 'ALTA' THEN 2 ELSE 3 END AS severidad_orden,
    a.detalle,
    a.estado,
    -- Necesita atencion humana: lo que no esta ni justificado ni confirmado
    -- como corregido. Es el filtro que usan las bandejas de trabajo.
    (a.estado IN ('ABIERTA', 'EN_REVISION'))        AS pendiente,
    a.nota,
    a.actualizado_en,
    a.actualizado_por,
    g.cedula                                        AS guarda_cedula,
    g.nombre                                        AS guarda_nombre,
    ctx.clientes,
    ctx.puestos,
    ctx.n_clientes,
    -- Puesto por el que se agrupa la bandeja de trabajo. En SERPI se navega
    -- cliente -> puesto -> guarda, asi que agrupar por aqui permite entrar una
    -- sola vez a cada puesto y corregir todo lo suyo de una pasada, en vez de
    -- ir saltando de puesto en puesto siguiendo una lista por severidad.
    ctx.puesto_principal,
    ctx.cliente_principal,
    a.turnos_involucrados,
    a.fecha_deteccion
FROM anomalias a
JOIN reglas_anomalia r ON r.id = a.regla_id
JOIN guardas g         ON g.cedula = a.guarda_cedula
-- El contexto cliente/puesto no esta en `anomalias`: se deriva de los turnos
-- que la originaron. Un mismo hallazgo puede tocar VARIOS clientes (un guarda
-- que rota entre puestos), y de hecho los cruces de horario nacen justamente
-- de eso. DESCUADRE no trae turnos pero si `puesto_id`, por eso el UNION.
LEFT JOIN LATERAL (
    SELECT
        string_agg(DISTINCT c.nombre, '; ' ORDER BY c.nombre) AS clientes,
        string_agg(DISTINCT p.nombre, '; ' ORDER BY p.nombre) AS puestos,
        count(DISTINCT c.id)                                  AS n_clientes,
        min(p.nombre)                                         AS puesto_principal,
        min(c.nombre)                                         AS cliente_principal
    FROM (
        SELECT t.puesto_id
        FROM unnest(COALESCE(a.turnos_involucrados, '{}'::bigint[])) AS u(turno_id)
        JOIN turnos t ON t.id = u.turno_id
        UNION
        SELECT a.puesto_id WHERE a.puesto_id IS NOT NULL
    ) src
    JOIN puestos  p ON p.id = src.puesto_id
    JOIN clientes c ON c.id = p.cliente_id
) ctx ON TRUE;

COMMENT ON VIEW vw_anomalias IS
    'Modelo de lectura base: una anomalia por fila con guarda, clientes/puestos afectados, regla y estado de gestion.';


-- ---------------------------------------------------------------------------
-- Puente anomalia -> cliente. Necesario para FILTRAR por cliente sin que se
-- pierdan los hallazgos que tocan varios (un string agregado no sirve para
-- filtrar, solo para mostrar).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_anomalia_cliente AS
SELECT DISTINCT
    a.id          AS anomalia_id,
    c.id          AS cliente_id,
    c.nombre      AS cliente_nombre
FROM anomalias a
LEFT JOIN LATERAL (
    SELECT t.puesto_id
    FROM unnest(COALESCE(a.turnos_involucrados, '{}'::bigint[])) AS u(turno_id)
    JOIN turnos t ON t.id = u.turno_id
    UNION
    SELECT a.puesto_id WHERE a.puesto_id IS NOT NULL
) src ON TRUE
JOIN puestos  p ON p.id = src.puesto_id
JOIN clientes c ON c.id = p.cliente_id;


-- ---------------------------------------------------------------------------
-- GERENCIA — indicadores de cabecera por periodo.
-- Una sola fila por mes: es lo que va al correo mensual y a las tarjetas
-- superiores del dashboard.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_kpi_periodo AS
SELECT
    v.periodo,
    count(*)                                                        AS total_hallazgos,
    count(DISTINCT v.guarda_cedula)                                 AS guardas_afectados,
    (SELECT count(*) FROM guardas)                                  AS guardas_totales,
    round(100.0 * count(DISTINCT v.guarda_cedula)
          / NULLIF((SELECT count(*) FROM guardas), 0), 1)           AS pct_guardas_afectados,
    count(*) FILTER (WHERE v.severidad = 'CRITICA')                 AS criticas,
    count(*) FILTER (WHERE v.severidad = 'ALTA')                    AS altas,
    count(*) FILTER (WHERE v.severidad = 'BAJA')                    AS bajas,
    count(*) FILTER (WHERE v.naturaleza = 'PUNTUAL')                AS puntuales,
    count(*) FILTER (WHERE v.naturaleza = 'ESTRUCTURAL')            AS estructurales,
    count(*) FILTER (WHERE v.naturaleza = 'ADMINISTRATIVA')         AS administrativas,
    count(*) FILTER (WHERE v.estado = 'ABIERTA')                    AS abiertas,
    count(*) FILTER (WHERE v.estado = 'EN_REVISION')                AS en_revision,
    count(*) FILTER (WHERE v.estado = 'JUSTIFICADA')                AS justificadas,
    count(*) FILTER (WHERE v.estado = 'RESUELTA')                   AS resueltas,
    count(*) FILTER (WHERE v.pendiente)                             AS pendientes,
    -- Avance real: cuanto se saco de la bandeja, sea porque se corrigio en
    -- SERPI (RESUELTA) o porque se reviso y se acepta (JUSTIFICADA).
    round(100.0 * count(*) FILTER (WHERE NOT v.pendiente)
          / NULLIF(count(*), 0), 1)                                 AS pct_gestionado
FROM vw_anomalias v
GROUP BY v.periodo;


-- ---------------------------------------------------------------------------
-- GERENCIA — ranking por cliente. Responde "¿donde duele mas?" y sirve para
-- priorizar la conversacion comercial con cada cliente.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_resumen_cliente AS
SELECT
    ac.cliente_id,
    ac.cliente_nombre,
    v.periodo,
    count(*)                                                AS total_hallazgos,
    count(*) FILTER (WHERE v.severidad = 'CRITICA')         AS criticas,
    count(*) FILTER (WHERE v.severidad = 'ALTA')            AS altas,
    count(*) FILTER (WHERE v.severidad = 'BAJA')            AS bajas,
    count(*) FILTER (WHERE v.naturaleza = 'ESTRUCTURAL')    AS estructurales,
    count(DISTINCT v.guarda_cedula)                         AS guardas_afectados,
    count(*) FILTER (WHERE v.pendiente)                     AS pendientes
FROM vw_anomalias v
JOIN vw_anomalia_cliente ac ON ac.anomalia_id = v.id
GROUP BY ac.cliente_id, ac.cliente_nombre, v.periodo;


-- ---------------------------------------------------------------------------
-- PROGRAMADOR DE TURNOS — su bandeja de trabajo.
-- Solo lo PUNTUAL (lo que se corrige moviendo un turno) y aun sin cerrar,
-- ordenado por urgencia. Lo estructural no entra aqui a proposito: no es su
-- decision rediseñar la plantilla, y mezclarlo le ahoga la lista.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_bandeja_programador AS
SELECT
    v.id, v.periodo, v.fecha_referencia, v.regla, v.severidad, v.severidad_orden,
    v.guarda_cedula, v.guarda_nombre, v.clientes, v.puestos,
    v.cliente_principal, v.puesto_principal,
    v.detalle, v.estado, v.pendiente, v.nota, v.fundamento_legal,
    v.turnos_involucrados
FROM vw_anomalias v
WHERE v.naturaleza = 'PUNTUAL'
  AND v.pendiente;


-- ---------------------------------------------------------------------------
-- NOMINA — conciliacion de horas declaradas contra horas programadas.
-- No se limita a las anomalias: trae TODAS las filas guarda-puesto-mes con
-- su diferencia, porque nomina necesita el panorama completo para liquidar,
-- no solo lo que incumple.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_nomina_horas AS
SELECT
    h.guarda_cedula,
    g.nombre                                     AS guarda_nombre,
    c.nombre                                     AS cliente,
    p.nombre                                     AS puesto,
    h.slot,
    to_char(make_date(h.anio, h.mes, 1), 'YYYY-MM') AS periodo,
    h.horas_diurnas_ordinarias,
    h.horas_diurnas_festivas,
    h.horas_nocturnas_ordinarias,
    h.horas_nocturnas_festivas,
    h.total_horas                                AS total_declarado_serpi,
    COALESCE(h.horas_diurnas_ordinarias, 0) + COALESCE(h.horas_diurnas_festivas, 0)
      + COALESCE(h.horas_nocturnas_ordinarias, 0) + COALESCE(h.horas_nocturnas_festivas, 0)
                                                 AS suma_categorias,
    h.total_horas - (COALESCE(h.horas_diurnas_ordinarias, 0) + COALESCE(h.horas_diurnas_festivas, 0)
      + COALESCE(h.horas_nocturnas_ordinarias, 0) + COALESCE(h.horas_nocturnas_festivas, 0))
                                                 AS descuadre_categorias,
    prog.horas_programadas,
    h.total_horas - prog.horas_programadas       AS diferencia_vs_programado
FROM horas_declaradas_mes h
JOIN guardas  g ON g.cedula = h.guarda_cedula
JOIN puestos  p ON p.id = h.puesto_id
JOIN clientes c ON c.id = p.cliente_id
LEFT JOIN LATERAL (
    SELECT round(SUM(t.horas_calculadas), 2) AS horas_programadas
    FROM turnos t
    JOIN tipos_turno tt ON tt.codigo = t.tipo_turno_codigo
    WHERE t.guarda_cedula = h.guarda_cedula
      AND t.puesto_id     = h.puesto_id
      AND t.slot          = h.slot
      AND tt.categoria    = 'TRABAJADO'
      AND date_part('year',  t.fecha) = h.anio
      AND date_part('month', t.fecha) = h.mes
) prog ON TRUE;


-- ---------------------------------------------------------------------------
-- GERENCIA — guardas con carga estructural. Es el insumo para decidir si hay
-- que rediseñar el patron 2x2x2, no para regañar a nadie: estos hallazgos
-- son consecuencia de que la plantilla base ya opera al limite legal.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW vw_carga_estructural AS
SELECT
    v.periodo,
    v.guarda_cedula,
    v.guarda_nombre,
    v.clientes,
    count(*)                                                    AS hallazgos_estructurales,
    bool_or(v.regla = 'SEMANA_SUPERA_60H')                      AS supera_60h,
    bool_or(v.regla = 'SIN_DESCANSO_SEMANAL')                   AS sin_descanso_semanal,
    bool_or(v.regla = 'RACHA_SIN_DESCANSO')                     AS racha_larga,
    hp.horas_mes
FROM vw_anomalias v
LEFT JOIN LATERAL (
    SELECT round(SUM(t.horas_calculadas), 1) AS horas_mes
    FROM turnos t
    JOIN tipos_turno tt ON tt.codigo = t.tipo_turno_codigo
    WHERE t.guarda_cedula = v.guarda_cedula
      AND tt.categoria = 'TRABAJADO'
      AND to_char(t.fecha, 'YYYY-MM') = v.periodo
) hp ON TRUE
WHERE v.naturaleza = 'ESTRUCTURAL'
GROUP BY v.periodo, v.guarda_cedula, v.guarda_nombre, v.clientes, hp.horas_mes;
