# Funciones compartidas por desplegar.ps1 y servidor.ps1. No se ejecuta solo.
#
# Todo el acceso al servidor va por SSM Session Manager (Run Command): la EC2
# no tiene SSH ni llaves. Basta con credenciales de AWS que tengan los permisos
# de despliegue/politica-operador.json.
#
# Sin tildes a proposito: Windows PowerShell 5.1 lee los .ps1 sin BOM como
# ANSI y destrozaria los acentos de los mensajes.

$ErrorActionPreference = 'Stop'

$Script:Region    = 'us-east-1'
$Script:Bucket    = 'shatter-turnos-deploy-306005333749'
$Script:NombreEc2 = 'turnos-app'
$Script:AppDir    = '/opt/turnos'

# La salida del servidor trae caracteres fuera de cp1252 (p. ej. el '●' de
# systemctl). Sin esto el AWS CLI revienta con "'charmap' codec can't encode".
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

function Invoke-Aws {
    # Llama al AWS CLI y falla en serio si sale con error (PowerShell 5.1 no
    # lo hace solo con ejecutables nativos).
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $Argumentos)
    # Con 'Stop', PowerShell 5.1 convierte en excepcion cualquier linea que el
    # CLI escriba en stderr (aunque termine bien) y se pierde el mensaje real.
    # Se decide por el codigo de salida, no por stderr.
    $ErrorActionPreference = 'Continue'
    $salida = & aws @Argumentos --region $Script:Region 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) {
        throw "aws $($Argumentos[0..1] -join ' ') fallo: $($salida | Out-String)"
    }
    return ($salida | Out-String).Trim()
}

function Assert-AwsListo {
    # Recien instalado, una terminal ya abierta no ve el PATH nuevo.
    $porDefecto = 'C:\Program Files\Amazon\AWSCLIV2'
    if (-not (Get-Command aws -ErrorAction SilentlyContinue) -and (Test-Path "$porDefecto\aws.exe")) {
        $env:Path += ";$porDefecto"
    }
    if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
        throw "No esta instalado el AWS CLI. Instalar con: winget install --id Amazon.AWSCLI -e --source winget"
    }
    try {
        $id = Invoke-Aws sts get-caller-identity --output json | ConvertFrom-Json
    } catch {
        throw "El AWS CLI no tiene credenciales validas. Ver DESPLIEGUE.md, 'Preparar un equipo'. Detalle: $_"
    }
    if ($id.Account -ne '306005333749') {
        throw "Las credenciales son de la cuenta $($id.Account), no de la del sistema (306005333749)."
    }
    Write-Host "AWS: $($id.Arn)" -ForegroundColor DarkGray
}

function Get-InstanciaTurnos {
    $id = Invoke-Aws ec2 describe-instances `
        --filters "Name=tag:Name,Values=$Script:NombreEc2" "Name=instance-state-name,Values=running" `
        --query 'Reservations[0].Instances[0].InstanceId' --output text
    if (-not $id -or $id -eq 'None') {
        throw "No hay una EC2 '$Script:NombreEc2' encendida en $Script:Region."
    }
    return $id
}

function Invoke-EnServidor {
    # Corre un script bash en la EC2 como root, espera a que termine y devuelve
    # su salida. Lanza excepcion si el script sale con error.
    param(
        [Parameter(Mandatory)] [string] $Bash,
        [string] $Comentario = 'turnos',
        [int] $TimeoutMin = 30
    )
    $instancia = Get-InstanciaTurnos

    # Los parametros van por archivo JSON: pasar comandos con comillas en la
    # linea de comandos de Windows es una fuente segura de errores.
    $archivo = Join-Path $env:TEMP "ssm-$([guid]::NewGuid()).json"
    $lineas = @('set -eu', "cd $Script:AppDir 2>/dev/null || true") + ($Bash -split "`r?`n")
    @{ commands = $lineas; executionTimeout = @("$($TimeoutMin * 60)") } |
        ConvertTo-Json -Depth 3 | Out-File -Encoding ascii $archivo
    try {
        $cmdId = Invoke-Aws ssm send-command --instance-ids $instancia `
            --document-name AWS-RunShellScript --comment $Comentario `
            --parameters "file://$archivo" --query 'Command.CommandId' --output text
    } finally {
        Remove-Item $archivo -ErrorAction SilentlyContinue
    }

    Write-Host "Ejecutando en $instancia (comando $cmdId)..." -ForegroundColor DarkGray
    $limite = (Get-Date).AddMinutes($TimeoutMin + 2)
    do {
        Start-Sleep -Seconds 5
        try {
            $r = Invoke-Aws ssm get-command-invocation --command-id $cmdId `
                --instance-id $instancia --output json | ConvertFrom-Json
        } catch {
            # Justo despues de enviarlo, la invocacion puede no existir todavia.
            # Cualquier otro error se reporta: tratarlo como "sigue corriendo"
            # dejaba el script esperando media hora un comando ya terminado.
            if ("$_" -notmatch 'InvocationDoesNotExist') { throw }
            $r = @{ Status = 'Pending' }
        }
    } while ($r.Status -in 'Pending', 'InProgress', 'Delayed' -and (Get-Date) -lt $limite)

    if ($r.StandardOutputContent) { Write-Host $r.StandardOutputContent }
    if ($r.StandardErrorContent)  { Write-Host $r.StandardErrorContent -ForegroundColor Yellow }
    if ($r.Status -ne 'Success') {
        throw "El comando termino en estado '$($r.Status)' (codigo $($r.ResponseCode))."
    }
}

function Invoke-ComposeEnServidor {
    # Atajo para correr algo dentro del contenedor de la API (misma imagen, mismo
    # .env), como lo hace bootstrap.sh para crear el primer ADMIN.
    param([Parameter(Mandatory)] [string] $Comando, [int] $TimeoutMin = 10)
    Invoke-EnServidor -TimeoutMin $TimeoutMin -Bash `
        "sudo -u turnos docker compose run --rm --no-deps --entrypoint python api $Comando"
}
