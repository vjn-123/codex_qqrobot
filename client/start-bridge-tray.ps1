param(
    [string]$PythonExe = "",
    [string[]]$PythonArgs = @()
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$baseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$rootDir = Split-Path -Parent $baseDir
$venvPython = Join-Path $rootDir ".venv\Scripts\python.exe"
Set-Location -LiteralPath $baseDir

$dataDir = Join-Path $baseDir "data"
$logFile = Join-Path $dataDir "qq-gateway-autostart.log"
$configFile = Join-Path $baseDir ".env"
$backgroundScript = Join-Path $baseDir "qq_gateway_background.py"
$script:statusTimer = $null
$script:notifyIcon = $null

function Ensure-DataDir {
    New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
}

function Write-TrayLog {
    param([string]$Message)
    Ensure-DataDir
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$timestamp] tray: $Message" | Add-Content -LiteralPath $logFile -Encoding UTF8
}

function Get-BridgeProcesses {
    $escapedBaseDir = [regex]::Escape((Resolve-Path -LiteralPath $baseDir).Path)
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match '^(python|pythonw|py)\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine -match $escapedBaseDir -and
            ($_.CommandLine -match 'qq_gateway_client\.py' -or $_.CommandLine -match 'qq_gateway_background\.py')
        } |
        Select-Object ProcessId, Name, CommandLine
}

function Get-BridgeStatus {
    $processes = @(Get-BridgeProcesses)
    $supervisors = @($processes | Where-Object { $_.CommandLine -match 'qq_gateway_background\.py' })
    $clients = @($processes | Where-Object { $_.CommandLine -match 'qq_gateway_client\.py' })
    return [pscustomobject]@{
        Processes = $processes
        Supervisors = $supervisors
        Clients = $clients
        IsRunning = $processes.Count -gt 0
    }
}

function Stop-Bridge {
    $processes = @(Get-BridgeProcesses)
    if ($processes.Count -eq 0) {
        Write-TrayLog "停止请求：未检测到运行中的进程"
        return
    }

    $supervisors = @($processes | Where-Object { $_.CommandLine -match 'qq_gateway_background\.py' })
    $clients = @($processes | Where-Object { $_.CommandLine -match 'qq_gateway_client\.py' })
    foreach ($process in $supervisors + $clients) {
        try {
            & taskkill.exe /PID $process.ProcessId /T /F | Out-Null
            Write-TrayLog "已终止进程树 PID $($process.ProcessId)"
        } catch {
            Write-TrayLog "终止 PID $($process.ProcessId) 失败：$($_.Exception.Message)"
        }
    }
}

function Start-Bridge {
    Ensure-DataDir
    $status = Get-BridgeStatus
    if ($status.IsRunning) {
        Write-TrayLog "启动请求：已在运行"
        return
    }

    $env:PYTHONIOENCODING = "utf-8"
    Write-TrayLog "启动后台守护进程"
    $resolvedPythonExe = $PythonExe
    $resolvedPythonArgs = @($PythonArgs)
    if ([string]::IsNullOrWhiteSpace($resolvedPythonExe) -and (Test-Path -LiteralPath $venvPython)) {
        $resolvedPythonExe = $venvPython
    }
    if ([string]::IsNullOrWhiteSpace($resolvedPythonExe)) {
        $pythonCommand = Get-Command "python" -ErrorAction SilentlyContinue
        if ($pythonCommand) {
            $resolvedPythonExe = $pythonCommand.Source
        } else {
            $pyCommand = Get-Command "py" -ErrorAction SilentlyContinue
            if ($pyCommand) {
                $resolvedPythonExe = $pyCommand.Source
                $resolvedPythonArgs = @("-3")
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($resolvedPythonExe)) {
        $condaCommand = Get-Command "conda" -ErrorAction SilentlyContinue
        if ($condaCommand) {
            try {
                $condaBase = (& $condaCommand.Source info --base 2>$null).Trim()
                if ($LASTEXITCODE -eq 0 -and $condaBase) {
                    $condaPython = Join-Path $condaBase "python.exe"
                    if (Test-Path -LiteralPath $condaPython) {
                        $resolvedPythonExe = $condaPython
                        $resolvedPythonArgs = @()
                    }
                }
            } catch {
                Write-TrayLog "conda Python 检测失败：$($_.Exception.Message)"
            }
        }
    }
    if ([string]::IsNullOrWhiteSpace($resolvedPythonExe)) {
        Write-TrayLog "启动失败：未找到 python 或 py"
        [System.Windows.Forms.MessageBox]::Show("未找到 Python，无法启动后台进程。", "Codex Remote Bridge") | Out-Null
        return
    }
    $arguments = @($resolvedPythonArgs) + @("-B", $backgroundScript)
    Start-Process `
        -FilePath $resolvedPythonExe `
        -ArgumentList $arguments `
        -WorkingDirectory $baseDir `
        -WindowStyle Hidden | Out-Null
}

function Restart-Bridge {
    Write-TrayLog "重启请求"
    Stop-Bridge
    Start-Sleep -Seconds 2
    Start-Bridge
}

function Open-PathIfExists {
    param(
        [string]$Path,
        [string]$MissingMessage
    )
    if (Test-Path -LiteralPath $Path) {
        Start-Process -FilePath $Path | Out-Null
    } else {
        [System.Windows.Forms.MessageBox]::Show($MissingMessage, "Codex Remote Bridge") | Out-Null
    }
}

function New-StatusIcon {
    param([bool]$Running)
    $bitmap = New-Object System.Drawing.Bitmap 16, 16
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.Clear([System.Drawing.Color]::Transparent)
    $brushColor = if ($Running) { [System.Drawing.Color]::FromArgb(37, 170, 84) } else { [System.Drawing.Color]::FromArgb(190, 60, 50) }
    $brush = New-Object System.Drawing.SolidBrush $brushColor
    $graphics.FillEllipse($brush, 2, 2, 12, 12)
    $pen = New-Object System.Drawing.Pen ([System.Drawing.Color]::White), 1
    $graphics.DrawEllipse($pen, 2, 2, 12, 12)
    $icon = [System.Drawing.Icon]::FromHandle($bitmap.GetHicon())
    $graphics.Dispose()
    $brush.Dispose()
    $pen.Dispose()
    return $icon
}

function Update-TrayStatus {
    $status = Get-BridgeStatus
    $oldIcon = $script:notifyIcon.Icon
    $script:notifyIcon.Icon = New-StatusIcon -Running $status.IsRunning
    if ($oldIcon) {
        $oldIcon.Dispose()
    }
    if ($status.IsRunning) {
        $script:notifyIcon.Text = "Codex Remote Bridge：运行中"
    } else {
        $script:notifyIcon.Text = "Codex Remote Bridge：未运行"
    }
}

Ensure-DataDir
Write-TrayLog "托盘程序启动"
Start-Bridge

$contextMenu = New-Object System.Windows.Forms.ContextMenuStrip
$statusItem = $contextMenu.Items.Add("状态：检测中")
$statusItem.Enabled = $false
$contextMenu.Items.Add("-") | Out-Null
$startItem = $contextMenu.Items.Add("启动桥接")
$stopItem = $contextMenu.Items.Add("停止桥接")
$restartItem = $contextMenu.Items.Add("重启桥接")
$contextMenu.Items.Add("-") | Out-Null
$logItem = $contextMenu.Items.Add("打开日志")
$configItem = $contextMenu.Items.Add("打开配置")
$refreshItem = $contextMenu.Items.Add("刷新状态")
$contextMenu.Items.Add("-") | Out-Null
$exitItem = $contextMenu.Items.Add("停止桥接并退出")

$script:notifyIcon = New-Object System.Windows.Forms.NotifyIcon
$script:notifyIcon.Text = "Codex Remote Bridge：检测中"
$script:notifyIcon.ContextMenuStrip = $contextMenu
$script:notifyIcon.Visible = $true

$startItem.Add_Click({
    Start-Bridge
    Update-TrayStatus
})
$stopItem.Add_Click({
    Stop-Bridge
    Start-Sleep -Seconds 1
    Update-TrayStatus
})
$restartItem.Add_Click({
    Restart-Bridge
    Start-Sleep -Seconds 1
    Update-TrayStatus
})
$logItem.Add_Click({
    Open-PathIfExists -Path $logFile -MissingMessage "日志文件尚未创建。"
})
$configItem.Add_Click({
    Open-PathIfExists -Path $configFile -MissingMessage "配置文件不存在：client\.env"
})
$refreshItem.Add_Click({
    Update-TrayStatus
})
$exitItem.Add_Click({
    Stop-Bridge
    Write-TrayLog "退出托盘程序"
    if ($script:statusTimer) {
        $script:statusTimer.Stop()
        $script:statusTimer.Dispose()
    }
    $script:notifyIcon.Visible = $false
    $script:notifyIcon.Dispose()
    [System.Windows.Forms.Application]::Exit()
})

$script:statusTimer = New-Object System.Windows.Forms.Timer
$script:statusTimer.Interval = 5000
$script:statusTimer.Add_Tick({
    $status = Get-BridgeStatus
    if ($status.IsRunning) {
        $statusItem.Text = "状态：运行中"
    } else {
        $statusItem.Text = "状态：未运行"
    }
    Update-TrayStatus
})
$script:statusTimer.Start()
Update-TrayStatus

[System.Windows.Forms.Application]::Run()
