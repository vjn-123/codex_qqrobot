param(
    [switch]$UpgradePip
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir = Join-Path $root ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$requirements = Join-Path $root "requirements.txt"
$exampleEnv = Join-Path $root "client\.env.example"
$localEnv = Join-Path $root "client\.env"

function Find-SystemPython {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $version = & $python.Source --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$version" -match "Python\s+3\.") {
            return @($python.Source)
        }
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $version = & $py.Source -3 --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$version" -match "Python\s+3\.") {
            return @($py.Source, "-3")
        }
    }
    return @()
}

$systemPython = @(Find-SystemPython)
if ($systemPython.Count -eq 0) {
    throw "未找到 Python 3。请先安装 Python 3.10 或更高版本，并确认 python 或 py 已加入 PATH。"
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "创建项目虚拟环境：$venvDir" -ForegroundColor Cyan
    $systemArgs = @()
    if ($systemPython.Count -gt 1) {
        $systemArgs = @($systemPython[1..($systemPython.Count - 1)])
    }
    & $systemPython[0] @systemArgs -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "创建 .venv 失败。"
    }
}

if ($UpgradePip) {
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "升级 pip 失败。"
    }
}

Write-Host "安装项目依赖：$requirements" -ForegroundColor Cyan
& $venvPython -m pip install -r $requirements
if ($LASTEXITCODE -ne 0) {
    throw "安装 Python 依赖失败。"
}

if (-not (Test-Path -LiteralPath $localEnv)) {
    Copy-Item -LiteralPath $exampleEnv -Destination $localEnv
    Write-Host "已创建 client\.env，请填写 QQ_APP_ID、QQ_APP_SECRET 和 CODEX_COMMAND。" -ForegroundColor Yellow
} else {
    Write-Host "已保留现有 client\.env。" -ForegroundColor Green
}

Write-Host "环境准备完成。" -ForegroundColor Green
Write-Host "虚拟环境 Python：$venvPython"
Write-Host "下一步：编辑 client\.env，然后运行 .\start-qqrobot.cmd"
