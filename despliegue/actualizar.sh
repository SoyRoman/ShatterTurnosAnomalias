#!/bin/bash
# Actualiza el servidor con una version nueva del codigo. Corre en la EC2 via
# SSM, lanzado por desplegar.ps1 desde el equipo principal; no se usa a mano.
#
# A diferencia de bootstrap.sh (instalacion desde cero, una sola vez), esto NO
# toca la base de datos, ni el .env, ni las cuentas de usuario: solo cambia el
# codigo, reconstruye las imagenes y reinicia la API. Las migraciones de esquema
# se aplican aparte y a proposito (ver DESPLIEGUE.md, "Migraciones"): un
# despliegue de codigo no debe poder alterar la base por accidente.
#
# Uso (lo arma desplegar.ps1):
#   bash actualizar.sh s3://<bucket>/releases/<sha>.tar.gz
set -euo pipefail

ARTEFACTO="${1:?falta la ruta s3:// del artefacto}"
APP_DIR="/opt/turnos"
export AWS_DEFAULT_REGION="us-east-1"

echo "== descargando $ARTEFACTO =="
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
aws s3 cp "$ARTEFACTO" "$TMP/app.tar.gz" --only-show-errors

echo "== version anterior: $(cat "$APP_DIR/VERSION" 2>/dev/null || echo desconocida) =="

# Se extrae ENCIMA del directorio actual. El artefacto sale de `git archive`,
# asi que no trae .env, logs/ ni "Reportes mensuales/" (estan en .gitignore):
# esos se conservan tal cual. Un archivo borrado del repo queda en disco, lo
# cual es inofensivo; borrarlo aqui arriesgaria llevarse algo que no es codigo.
tar -xzf "$TMP/app.tar.gz" -C "$APP_DIR"
chown -R turnos:turnos "$APP_DIR"
# Los volumenes del pipeline los escribe el usuario DEL CONTENEDOR (uid 10001,
# ver Dockerfile), no el `turnos` del host (uid de sistema, ~997). Con el dueno
# del host, el pipeline moria en el primer segundo con PermissionError sobre
# logs/pipeline_diario.log — fallo todas las noches del 27-09 al 01-10-2026.
# Va DESPUES del chown -R, que si no lo deshace.
UID_CONTENEDOR=10001
mkdir -p "$APP_DIR/logs" "$APP_DIR/Reportes mensuales"
chown -R "$UID_CONTENEDOR:$UID_CONTENEDOR" "$APP_DIR/logs" "$APP_DIR/Reportes mensuales"
echo "== version nueva: $(cat "$APP_DIR/VERSION") =="

cd "$APP_DIR"

# --profile manual SIEMPRE: sin el solo se reconstruye `api`, y el timer
# seguiria corriendo el pipeline viejo sin dar ningun error.
echo "== construyendo imagenes =="
sudo -u turnos docker compose --profile manual build --quiet
sudo -u turnos docker compose up -d api

echo "== unidades systemd =="
cp despliegue/turnos-pipeline.service despliegue/turnos-pipeline.timer /etc/systemd/system/
if [ -f /usr/local/bin/caddy ]; then
  cp despliegue/caddy.service /etc/systemd/system/
fi
systemctl daemon-reload
systemctl enable --now turnos-pipeline.timer >/dev/null
if systemctl is-active --quiet caddy; then
  systemctl reload caddy
fi

echo "== comprobando la API =="
# /login responde sin sesion; cualquier otra ruta da 401 a proposito.
for i in $(seq 1 30); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/login; then
    echo "API arriba."
    docker image prune -f >/dev/null || true
    systemctl list-timers turnos-pipeline.timer --no-pager | head -3
    echo "== actualizacion ok: $(cat VERSION) =="
    exit 0
  fi
  sleep 2
done

echo "ERROR: la API no responde tras la actualizacion. Ultimas lineas:" >&2
sudo -u turnos docker compose logs --tail 40 api >&2
exit 1
