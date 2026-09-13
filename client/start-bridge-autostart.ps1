$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$rootDir = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $rootDir ".venv\Scripts\python.exe"

$logDir = Join-Path $PSScriptRoot "data"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$logFile = Join-Path $logDir "qq-gateway-autostart.log"
$oldLogFile = Join-Path $logDir "qq-gateway-autostart.log.1"

if ((Test-Path -LiteralPath $logFile) -and ((Get-Item -LiteralPath $logFile).Length -gt 5242880)) {
    Move-Item -LiteralPath $logFile -Destination $oldLogFile -Force
}

$env:PYTHONIOENCODING = "utf-8"
$startedAt = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$startedAt] starting QQ Gateway bridge" | Add-Content -LiteralPath $logFile -Encoding UTF8

$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
$cmd = "`"$python`" .\qq_gateway_client.py >> `"$logFile`" 2>&1"
cmd.exe /d /c $cmd
