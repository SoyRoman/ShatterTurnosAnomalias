-- Migracion 003 — estados que describen el proceso real
--
-- El modelo anterior (ABIERTA / EN_REVISION / TICKET_CREADO / CERRADA) venia de
-- suponer que las anomalias se gestionaban como tickets en SERPI. No es asi:
-- el modulo Soporte>Tickets de SERPI sirve para escalar con los DESARROLLADORES
-- del proveedor, no para corregir turnos. El ciclo real es:
--
--     dashboard muestra el hallazgo -> el usuario entra a SERPI y corrige la
--     malla -> el siguiente cargue confirma que quedo corregido
--
-- De ahi el modelo nuevo:
--
--   ABIERTA      detectada, nadie la ha mirado            (la pone el motor)
--   EN_REVISION  alguien la trabaja / va a corregirla     (la pone el usuario)
--   JUSTIFICADA  revisada y se acepta como esta           (la pone el usuario,
--                                                          con nota obligatoria)
--   RESUELTA     ya no aparece en el cargue mas reciente  (la pone el motor)
--
-- RESUELTA es la que cierra el ciclo: cuando el usuario corrige en SERPI y se
-- recarga la malla, la violacion deja de existir y el motor no la regenera.
-- En vez de dejar esa fila huerfana, el motor la marca RESUELTA — o sea, le
-- confirma al usuario que su correccion si llego. Nadie tiene que acordarse de
-- volver al dashboard a cerrar nada a mano.
--
-- `ticket_serpi_id` se deja en la tabla pero sin uso: no cuesta nada y evita
-- perder el dato si alguna vez se decide referenciar un ticket del proveedor.
--
-- Ejecutar con: psql -U turnos_app -d turnos -f migracion_003_estados.sql
-- Es idempotente.

BEGIN;

-- El CHECK viejo hay que soltarlo ANTES de migrar los valores, si no rechaza
-- los estados nuevos.
ALTER TABLE anomalias DROP CONSTRAINT IF EXISTS anomalias_estado_check;

-- Traduccion de los estados viejos por si la tabla ya tenia gestion hecha.
-- TICKET_CREADO significaba "alguien la esta trabajando" -> EN_REVISION.
-- CERRADA significaba "revisada y se acepta / se resolvio" -> JUSTIFICADA,
-- que es el estado terminal puesto por una persona (RESUELTA solo la pone el
-- motor, y solo cuando comprueba que la violacion desaparecio de los datos).
UPDATE anomalias SET estado = 'EN_REVISION' WHERE estado = 'TICKET_CREADO';
UPDATE anomalias SET estado = 'JUSTIFICADA' WHERE estado = 'CERRADA';

ALTER TABLE anomalias ADD CONSTRAINT anomalias_estado_check
    CHECK (estado IN ('ABIERTA', 'EN_REVISION', 'JUSTIFICADA', 'RESUELTA'));

-- Mismo mapeo en el historial, para que la trazabilidad quede coherente.
UPDATE anomalias_historial SET estado_anterior = 'EN_REVISION' WHERE estado_anterior = 'TICKET_CREADO';
UPDATE anomalias_historial SET estado_nuevo    = 'EN_REVISION' WHERE estado_nuevo    = 'TICKET_CREADO';
UPDATE anomalias_historial SET estado_anterior = 'JUSTIFICADA' WHERE estado_anterior = 'CERRADA';
UPDATE anomalias_historial SET estado_nuevo    = 'JUSTIFICADA' WHERE estado_nuevo    = 'CERRADA';

-- El barrido de RESUELTA filtra por estado y periodo en cada corrida.
CREATE INDEX IF NOT EXISTS idx_anomalias_estado_fecha
    ON anomalias (estado, fecha_referencia);

COMMIT;
