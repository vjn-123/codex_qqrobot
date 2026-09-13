param(
    [string]$OutputPath = ""
)

$ErrorActionPreference = "Stop"

$clientDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $clientDir
$sourcePath = Join-Path $clientDir "tray\CodexRemoteBridgeTray.cs"
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $rootDir "CodexRemoteBridgeTray.exe"
}

if (-not (Test-Path -LiteralPath $sourcePath)) {
    throw "找不到托盘程序源码：$sourcePath"
}

$outputDir = Split-Path -Parent $OutputPath
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

if (Test-Path -LiteralPath $OutputPath) {
    $trashDir = Join-Path $rootDir "trash"
    New-Item -ItemType Directory -Force -Path $trashDir | Out-Null
    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupPath = Join-Path $trashDir "CodexRemoteBridgeTray-$timestamp.exe"
    Move-Item -LiteralPath $OutputPath -Destination $backupPath -Force
    Write-Host "已把旧 EXE 移动到：$backupPath" -ForegroundColor DarkYellow
}

Write-Host "正在构建托盘 EXE..." -ForegroundColor Cyan
Add-Type `
    -LiteralPath $sourcePath `
    -ReferencedAssemblies @(
        "System.dll",
        "System.Core.dll",
        "System.Drawing.dll",
        "System.Management.dll",
        "System.Windows.Forms.dll"
    ) `
    -OutputAssembly $OutputPath `
    -OutputType WindowsApplication

Write-Host "构建完成：$OutputPath" -ForegroundColor Green
Write-Host "运行后会出现在 Windows 右下角托盘区域。" -ForegroundColor Green
