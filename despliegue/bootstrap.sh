#!/bin/bash
# Bootstrap del servidor de turnos. Corre en la EC2 via SSM.
# No imprime secretos. No toca bitacorapp.
set -euo pipefail

REGION="us-east-1"
APP_DIR="/opt/turnos"
BUCKET="shatter-turnos-deploy-306005333749"

export DEBIAN_FRONTEND=noninteractive
export AWS_DEFAULT_REGION="$REGION"

echo "== timezone =="
timedatectl set-timezone America/Bogota

echo "== swap =="
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== docker =="
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y docker.io docker-compose-v2 git postgresql-client unzip jq
  systemctl enable --now docker
fi

echo "== user turnos =="
if ! id turnos >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin turnos
fi
usermod -aG docker turnos

echo "== code from S3 =="
mkdir -p "$APP_DIR"
aws s3 cp "s3://$BUCKET/app.tar.gz" /tmp/app.tar.gz
tar -xzf /tmp/app.tar.gz -C "$APP_DIR"
rm -f /tmp/app.tar.gz
mkdir -p "$APP_DIR/logs" "$APP_DIR/Reportes mensuales"
chown -R turnos:turnos "$APP_DIR"
# Los volumenes los escribe el uid del CONTENEDOR (10001, ver Dockerfile), no el
# `turnos` del host. Ver actualizar.sh.
chown -R 10001:10001 "$APP_DIR/logs" "$APP_DIR/Reportes mensuales"

echo "== .env from Parameter Store =="
python3 - <<'PY'
import json, subprocess, os, pwd, grp
os.umask(0o077)
raw = subprocess.check_output([
    "aws", "ssm", "get-parameters-by-path",
    "--path", "/turnos",
    "--with-decryption",
    "--recursive",
    "--region", "us-east-1",
    "--output", "json",
], text=True)
params = {p["Name"].split("/")[-1]: p["Value"] for p in json.loads(raw)["Parameters"]}
needed = ["DB_HOST","DB_PORT","DB_NAME","DB_USER","DB_PASSWORD","DB_SSLMODE",
          "MODO_IDENTIDAD","COOKIE_SEGURA","API_BIND","HORAS_SESION",
          "SERPI_WEB_USER","SERPI_WEB_PASSWORD"]
missing = [k for k in needed if k not in params]
if missing:
    raise SystemExit(f"faltan parametros: {missing}")
path = "/opt/turnos/.env"
with open(path, "w") as f:
    for k in needed:
        f.write(f"{k}={params[k]}\n")
uid = pwd.getpwnam("turnos").pw_uid
gid = grp.getgrnam("turnos").gr_gid
os.chown(path, uid, gid)
os.chmod(path, 0o600)
print("env escrito (valores no impresos)")
PY

echo "== app role on RDS =="
python3 - <<'PY'
import json, os, subprocess, tempfile
raw = subprocess.check_output([
    "aws", "ssm", "get-parameter",
    "--name", "/turnos/MASTER_SECRET_ARN",
    "--query", "Parameter.Value", "--output", "text", "--region", "us-east-1",
], text=True).strip()
master = json.loads(subprocess.check_output([
    "aws", "secretsmanager", "get-secret-value",
    "--secret-id", raw, "--region", "us-east-1",
    "--query", "SecretString", "--output", "text",
], text=True))
app = {x["Name"].split("/")[-1]: x["Value"] for x in json.loads(subprocess.check_output([
    "aws", "ssm", "get-parameters-by-path",
    "--path", "/turnos", "--with-decryption", "--recursive",
    "--region", "us-east-1", "--output", "json",
], text=True))["Parameters"]}
os.environ["PGPASSWORD"] = master["password"]
sql = (
    f"SELECT 1 FROM pg_roles WHERE rolname = '{app['DB_USER']}';\n"
)
exists = subprocess.run(
    ["psql", "-h", app["DB_HOST"], "-U", master["username"], "-d", app["DB_NAME"],
     "-tAc", f"SELECT 1 FROM pg_roles WHERE rolname = '{app['DB_USER']}'"],
    capture_output=True, text=True, check=True,
).stdout.strip()
fd, path = tempfile.mkstemp(suffix=".sql")
os.close(fd)
os.chmod(path, 0o600)
with open(path, "w") as f:
    if exists == "1":
        f.write(f"ALTER ROLE {app['DB_USER']} WITH LOGIN PASSWORD '{app['DB_PASSWORD']}';\n")
    else:
        f.write(f"CREATE ROLE {app['DB_USER']} LOGIN PASSWORD '{app['DB_PASSWORD']}';\n")
    f.write(f"GRANT ALL PRIVILEGES ON DATABASE {app['DB_NAME']} TO {app['DB_USER']};\n")
    f.write(f"GRANT ALL ON SCHEMA public TO {app['DB_USER']};\n")
    f.write(f"ALTER SCHEMA public OWNER TO {app['DB_USER']};\n")
try:
    subprocess.check_call([
        "psql", "-h", app["DB_HOST"], "-U", master["username"], "-d", app["DB_NAME"],
        "-v", "ON_ERROR_STOP=1", "-f", path,
    ])
finally:
    os.remove(path)
print("rol de aplicacion listo")
PY

echo "== schema =="
export PGPASSWORD="$(aws ssm get-parameter --name /turnos/DB_PASSWORD --with-decryption --query Parameter.Value --output text --region us-east-1)"
export PGHOST="$(aws ssm get-parameter --name /turnos/DB_HOST --query Parameter.Value --output text --region us-east-1)"
export PGSSLMODE=require
export PGSSLCERT=
export PGSSLKEY=
cd "$APP_DIR"
for f in schema.sql migracion_004_usuarios.sql migracion_005_historial.sql seed_reglas.sql vistas_reporte.sql; do
  psql -h "$PGHOST" -U turnos_app -d turnos -v ON_ERROR_STOP=1 -f "$f"
done
unset PGPASSWORD

echo "== docker images =="
cd "$APP_DIR"
sudo -u turnos docker compose --profile manual build
sudo -u turnos docker compose up -d api

echo "== first admin if none =="
# Clave temporal impresa una sola vez en el journal de SSM. Cambiarla al entrar.
sudo -u turnos docker compose run --rm --entrypoint python api gestionar_usuarios.py crear \
  --usuario admin --nombre "Administrador" --rol ADMIN --sin-preguntar || true

echo "== systemd timer =="
cp "$APP_DIR/despliegue/turnos-pipeline.service" /etc/systemd/system/
cp "$APP_DIR/despliegue/turnos-pipeline.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now turnos-pipeline.timer
systemctl list-timers turnos-pipeline.timer --no-pager

echo "== bootstrap ok =="
