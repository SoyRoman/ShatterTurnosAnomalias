<#
.SYNOPSIS
    Publica una version del codigo en el servidor de AWS.

.DESCRIPTION
    Empaqueta un commit de git, lo sube a S3 y le pide a la EC2 (via SSM) que
    lo instale con despliegue/actualizar.sh: reconstruye las imagenes, reinicia
    la API y reinstala el timer del pipeline.

    NO toca la base de datos, el .env del servidor ni las cuentas de usuario.

    Lo que se publica es un COMMIT, no la carpeta: los cambios sin commit no
    viajan. Por eso se exige el arbol limpio, para que "lo que hay en el
    servidor" siempre tenga un commit con nombre al que volver.

.EXAMPLE
    .\despliegue\desplegar.ps1                 # publica HEAD
.EXAMPLE
    .\despliegue\desplegar.ps1 -Version a1b2c3d  # vuelve a una version anterior
.EXAMPLE
    .\despliegue\desplegar.ps1 -SoloSubir      # sube el paquete sin instalarlo
#>
param(
    # Commit, rama o etiqueta a publicar.
    [string] $Version = 'HEAD',
    # Publicar aunque haya cambios sin commit (los cambios NO se incluyen).
    [switch] $IgnorarCambiosLocales,
    # Solo subir el paquete a S3, sin instalarlo en el servidor.
    [switch] $SoloSubir
)

. (Join-Path $PSScriptRoot '_aws.ps1')
$raiz = Split-Path $PSScriptRoot -Parent
Set-Location $raiz

Assert-AwsListo

# ---- 1. Que se publica ------------------------------------------------------
$sha = (git rev-parse --short=10 "$Version^{commit}" 2>$null)
if ($LASTEXITCODE -ne 0 -or -not $sha) { throw "No existe la version '$Version' en git." }
$resumen = (git log -1 --format='%h %s (%an, %ad)' --date=short $sha)

$sucio = git status --porcelain --untracked-files=no
if ($sucio -and $Version -eq 'HEAD' -and -not $IgnorarCambiosLocales) {
    Write-Host $sucio
    throw "Hay cambios sin commit. Haz commit primero, o usa -IgnorarCambiosLocales (no se incluiran)."
}

git fetch --quiet origin 2>$null
$enOrigen = git branch -r --contains $sha 2>$null
if (-not $enOrigen) {
    Write-Warning "El commit $sha no esta en GitHub. Haz 'git push' para que el resto del equipo lo tenga."
}

Write-Host "Version a publicar: $resumen" -ForegroundColor Cyan

# ---- 2. Paquete ---------------------------------------------------------------
# git archive solo incluye lo versionado: nada de .env, mallas con PII,
# Reportes mensuales/ ni venv/. VERSION permite saber que corre en el servidor.
$fecha = Get-Date -Format 'yyyy-MM-dd HH:mm'
$paquete = Join-Path $env:TEMP "turnos-$sha.tar.gz"
git archive --format=tar.gz --add-virtual-file="VERSION:$sha publicado $fecha por $env:USERNAME" -o $paquete $sha
if ($LASTEXITCODE -ne 0) { throw "git archive fallo." }

$destino = "s3://$Script:Bucket/releases/$sha.tar.gz"
try {
    Invoke-Aws s3 cp $paquete $destino --only-show-errors | Out-Null
    # app.tar.gz es la ruta fija que usa bootstrap.sh para instalar desde cero.
    Invoke-Aws s3 cp $destino "s3://$Script:Bucket/app.tar.gz" --only-show-errors | Out-Null
} finally {
    Remove-Item $paquete -ErrorAction SilentlyContinue
}
Write-Host "Paquete subido: $destino"

if ($SoloSubir) { Write-Host 'No se instalo (-SoloSubir).'; return }

# ---- 3. Instalar en el servidor ----------------------------------------------
# Se extrae solo actualizar.sh del paquete nuevo y se corre ESA copia, para que
# un cambio en el propio script de actualizacion se aplique en este despliegue.
Invoke-EnServidor -Comentario "desplegar $sha" -TimeoutMin 30 -Bash @"
T=`$(mktemp -d)
aws s3 cp $destino `$T/app.tar.gz --only-show-errors --region $Script:Region
tar -xzf `$T/app.tar.gz -C `$T despliegue/actualizar.sh
bash `$T/despliegue/actualizar.sh $destino
rm -rf `$T
"@

Write-Host "Publicado $sha en el servidor." -ForegroundColor Green
