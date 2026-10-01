<#
.SYNOPSIS
    Operacion diaria del servidor de AWS desde el equipo principal, sin SSH.

.DESCRIPTION
    Acciones:
      estado            Version publicada, API, timer y ultima corrida del pipeline
      logs              Bitacora de las ultimas corridas del pipeline
      logs-api          Ultimas lineas de la API
      pipeline          Lanza una corrida del pipeline ahora (descarga SERPI + ETL + motor)
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
#>
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('estado', 'logs', 'logs-api', 'pipeline', 'usuarios',
                 'crear-usuario', 'restablecer-clave', 'desactivar')]
    [string] $Accion,
    [string] $Usuario,
    [string] $Nombre,
    [ValidateSet('ADMIN', 'PROGRAMADOR', 'NOMINA', 'GERENCIA')] [string] $Rol,
    [string] $Correo,
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
        Invoke-EnServidor -Comentario 'logs pipeline' -Bash "journalctl -u turnos-pipeline.service --no-pager -n $Lineas -o cat"
    }
    'logs-api' {
        Invoke-EnServidor -Comentario 'logs api' -Bash "sudo -u turnos docker compose logs --no-color --tail $Lineas api"
    }
    'pipeline' {
        # Corre en segundo plano en el servidor: tarda 10-30 min (SERPI es lento)
        # y no hace falta tener esta ventana abierta. Seguirlo con 'logs'.
        Invoke-EnServidor -Comentario 'pipeline manual' -Bash @'
systemctl start --no-block turnos-pipeline.service
echo "Corrida lanzada. Sigue el avance con:  .\despliegue\servidor.ps1 logs"
'@
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
