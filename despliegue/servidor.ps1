<#
.SYNOPSIS
    Operacion diaria del servidor de AWS desde el equipo principal, sin SSH.

.DESCRIPTION
    Acciones:
      estado            Version publicada, API, timer y ultima corrida del pipeline
      logs              Bitacora de las ultimas corridas del pipeline
      logs-api          Ultimas lineas de la API
      pipeline          Lanza una corrida del pipeline ahora (descarga SERPI + ETL + motor).
                        Sin fechas: mes actual + siguiente. Con -Desde/-Hasta: ese rango
      migrar            Aplica una migracion de esquema (migracion_NNN_*.sql) ya publicada
      usuarios          Lista las cuentas del dashboard
      crear-usuario     Crea una cuenta con clave temporal (se muestra una vez)
      restablecer-clave Da una clave temporal nueva a una cuenta
      desactivar        Desactiva una cuenta (no se borra: queda en el historial)

.EXAMPLE
    .\despliegue\servidor.ps1 estado
.EXAMPLE
    .\despliegue\servidor.ps1 crear-usuario -Usuario mperez -Nombre "Maria Perez" -Rol PROGRAMADOR
.EXAMPLE
    .\despliegue\servidor.ps1 pipeline
.EXAMPLE
    .\despliegue\servidor.ps1 pipeline -Desde 2026-08-01 -Hasta 2026-10-31
.EXAMPLE
    .\despliegue\servidor.ps1 migrar -Archivo migracion_005_historial.sql
#>
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('estado', 'logs', 'logs-api', 'pipeline', 'migrar', 'usuarios',
                 'crear-usuario', 'restablecer-clave', 'desactivar')]
    [string] $Accion,
    [string] $Usuario,
    [string] $Nombre,
    [ValidateSet('ADMIN', 'PROGRAMADOR', 'NOMINA', 'GERENCIA')] [string] $Rol,
    [string] $Correo,
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')] [string] $Desde,
    [ValidatePattern('^\d{4}-\d{2}-\d{2}$')] [string] $Hasta,
    [ValidatePattern('^migracion_\d{3}_[a-z0-9_]+\.sql$')] [string] $Archivo,
    [int] $Lineas = 80
)

. (Join-Path $PSScriptRoot '_aws.ps1')
Assert-AwsListo

function Assert-Param($valor, $nombre) {
    if (-not $valor) { throw "La accion '$Accion' necesita -$nombre." }
}

# Nombres de usuario y roles van a un comando de shell en el servidor: se
# validan aqui para que nada raro llegue a esa linea.
if ($Usuario -and $Usuario -notmatch '^[a-z0-9._-]{3,40}$') {
    throw "Usuario invalido: solo minusculas, numeros, punto, guion y guion bajo."
}
if ($Nombre -and $Nombre -notmatch "^[\p{L} .'-]{2,80}$") {
    throw "Nombre invalido: solo letras, espacios, punto, apostrofe y guion."
}
if ($Correo -and $Correo -notmatch '^[^\s@''"]+@[^\s@''"]+$') { throw "Correo invalido." }

switch ($Accion) {
    'estado' {
        Invoke-EnServidor -Comentario 'estado' -Bash @'
echo "Version:  $(cat VERSION 2>/dev/null || echo 'desconocida (instalada antes de desplegar.ps1)')"
echo "Hora:     $(date '+%Y-%m-%d %H:%M %Z')"
echo; echo "== Contenedores =="; sudo -u turnos docker compose ps
echo; echo "== API =="; curl -fsS -o /dev/null -w "login HTTP %{http_code}\n" http://127.0.0.1:8000/login || echo "API NO RESPONDE"
echo; echo "== Timer del pipeline =="; systemctl list-timers turnos-pipeline.timer --no-pager | head -3
echo; echo "== Ultima corrida =="; systemctl status turnos-pipeline.service --no-pager -n 0 | sed -n '1,6p' || true
echo; echo "== Disco =="; df -h / | tail -1
'@
    }
    'logs' {
        # La bitacora propia del pipeline trae TODAS las corridas, tambien las
        # manuales con fechas (que no pasan por turnos-pipeline.service). El
        # journal se agrega porque ahi quedan los fallos de arranque, cuando el
        # pipeline murio antes de poder escribir su bitacora.
        Invoke-EnServidor -Comentario 'logs pipeline' -Bash @"
echo '== Bitacora del pipeline =='; tail -n $Lineas logs/pipeline_diario.log 2>/dev/null || echo '(todavia no hay bitacora)'
echo; echo '== Journal (fallos de arranque) =='; journalctl -u 'turnos-pipeline*' --no-pager -n 15 -o short-iso | grep -vE '^\s*$' || true
"@
    }
    'logs-api' {
        Invoke-EnServidor -Comentario 'logs api' -Bash "sudo -u turnos docker compose logs --no-color --tail $Lineas api"
    }
    'pipeline' {
        # Corre en segundo plano en el servidor: tarda 10-30 min (SERPI es lento)
        # y no hace falta tener esta ventana abierta. Seguirlo con 'logs'.
        if ([bool]$Desde -ne [bool]$Hasta) { throw '-Desde y -Hasta van juntos.' }
        if (-not $Desde) {
            Invoke-EnServidor -Comentario 'pipeline manual' -Bash 'systemctl start --no-block turnos-pipeline.service'
        } else {
            # Mismo usuario, carpeta e imagen que el .service, como unidad
            # transitoria: sobrevive a que se cierre esta ventana, y el candado
            # del pipeline impide que se pise con la corrida de las 04:30.
            Invoke-EnServidor -Comentario "pipeline $Desde a $Hasta" -Bash @"
systemd-run --unit=turnos-pipeline-manual-`$(date +%Y%m%d%H%M%S) --uid=turnos --gid=turnos --working-directory=/opt/turnos --property=TimeoutStartSec=2h /usr/bin/docker compose --profile manual run --rm pipeline --desde $Desde --hasta $Hasta
"@
        }
        Write-Host 'Corrida lanzada. Sigue el avance con:  .\despliegue\servidor.ps1 logs'
    }
    'migrar' {
        Assert-Param $Archivo 'Archivo'
        # Se ejecuta DENTRO del contenedor de la API: usa su .env y la copia del
        # .sql que viene en la imagen, o sea la de la version publicada. Por eso
        # primero se publica (desplegar.ps1) y despues se migra. Las migraciones
        # tienen que ser idempotentes (IF NOT EXISTS): repetir una no rompe nada.
        Invoke-ComposeEnServidor -TimeoutMin 10 @"
-c "import os,psycopg2; c=psycopg2.connect(host=os.environ['DB_HOST'],port=os.environ.get('DB_PORT','5432'),dbname=os.environ['DB_NAME'],user=os.environ['DB_USER'],password=os.environ['DB_PASSWORD'],sslmode=os.environ.get('DB_SSLMODE','require')); cur=c.cursor(); cur.execute(open('$Archivo',encoding='utf-8').read()); c.commit(); print('Migracion aplicada: $Archivo')"
"@
    }
    'usuarios' {
        Invoke-ComposeEnServidor 'gestionar_usuarios.py listar'
    }
    'crear-usuario' {
        Assert-Param $Usuario 'Usuario'; Assert-Param $Nombre 'Nombre'; Assert-Param $Rol 'Rol'
        $extra = if ($Correo) { " --correo '$Correo'" } else { '' }
        Invoke-ComposeEnServidor "gestionar_usuarios.py crear --usuario $Usuario --nombre '$Nombre' --rol $Rol$extra --sin-preguntar"
        Write-Warning 'La clave temporal de arriba solo se muestra esta vez. Entregala por un medio seguro; se cambia en el primer ingreso.'
    }
    'restablecer-clave' {
        Assert-Param $Usuario 'Usuario'
        Invoke-ComposeEnServidor "gestionar_usuarios.py clave --usuario $Usuario --sin-preguntar"
    }
    'desactivar' {
        Assert-Param $Usuario 'Usuario'
        Invoke-ComposeEnServidor "gestionar_usuarios.py desactivar --usuario $Usuario"
    }
}
