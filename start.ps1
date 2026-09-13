param(
    [switch]$Reconfigure,
    [switch]$Background,
    [switch]$CheckOnly,
    [string]$WorkDir = ""
)

$ErrorActionPreference = "Stop"
$script:InstallProxyPreferenceChecked = $false
$script:InstallProxy = ""

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok {
    param([string]$Message)
    Write-Host "[OK] $Message" -ForegroundColor Green
}

function Write-WarnLine {
    param([string]$Message)
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "[FAIL] $Message" -ForegroundColor Red
}

function Confirm-Action {
    param(
        [string]$Question,
        [bool]$DefaultYes = $false
    )
    $suffix = "[y/N，默认否]"
    if ($DefaultYes) {
        $suffix = "[Y/n，默认是]"
    }
    $answer = Read-Host "$Question $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) {
        return $DefaultYes
    }
    return $answer.Trim().ToLowerInvariant() -in @("y", "yes", "是", "好", "确认")
}

function Read-YesNoNever {
    param([string]$Question)
    while ($true) {
        $answer = Read-Host "$Question [y/n/never]"
        $normalized = $answer.Trim().ToLowerInvariant()
        if ($normalized -in @("y", "yes", "是", "好", "确认")) {
            return "yes"
        }
        if ($normalized -in @("n", "no", "否", "不")) {
            return "no"
        }
        if ($normalized -eq "never") {
            return "never"
        }
        Write-WarnLine "请输入 y、n 或 never。"
    }
}

function Invoke-Logged {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory = $PSScriptRoot,
        [string]$Activity = "正在执行命令"
    )
    Write-Host "> $FilePath $($ArgumentList -join ' ')" -ForegroundColor DarkGray
    Write-Progress -Activity $Activity -Status "准备开始..." -PercentComplete 0
    try {
        $process = Start-Process `
            -FilePath $FilePath `
            -ArgumentList $ArgumentList `
            -WorkingDirectory $WorkingDirectory `
            -NoNewWindow `
            -PassThru

        $started = Get-Date
        while (-not $process.HasExited) {
            $elapsed = [int]((Get-Date) - $started).TotalSeconds
            $percent = [Math]::Min(95, 5 + $elapsed)
            Write-Progress `
                -Activity $Activity `
                -Status "正在执行，已用 $elapsed 秒。安装器可能正在下载，请不要关闭窗口。" `
                -PercentComplete $percent
            Start-Sleep -Seconds 1
            $process.Refresh()
        }
        $exitCode = $process.ExitCode
    } finally {
        Write-Progress -Activity $Activity -Completed
    }
    if ($exitCode -ne 0) {
        throw "命令执行失败，退出码 ${exitCode}：$FilePath"
    }
}

function Test-Command {
    param([string]$Name)
    try {
        return $null -ne (Get-Command $Name -ErrorAction Stop)
    } catch {
        return $false
    }
}

function Add-ProxyCandidate {
    param(
        [System.Collections.Generic.List[string]]$Candidates,
        [string]$Value
    )
    $value = ([string]$Value).Trim()
    if (-not $value -or $value -match "^(?:none|null|direct)$") {
        return
    }
    if ($value -notmatch "^[a-zA-Z][a-zA-Z0-9+.-]*://") {
        $value = "http://$value"
    }
    if (-not $Candidates.Contains($value)) {
        $Candidates.Add($value) | Out-Null
    }
}

function Format-ProxyForDisplay {
    param([string]$Proxy)
    return ($Proxy -replace "(://[^:/@]+):([^@]+)@", '$1:***@')
}

function Get-ProxyCandidates {
    $candidates = [System.Collections.Generic.List[string]]::new()

    foreach ($name in @("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")) {
        Add-ProxyCandidate -Candidates $candidates -Value ([Environment]::GetEnvironmentVariable($name))
    }

    try {
        $internetSettings = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction Stop
        if ([int]$internetSettings.ProxyEnable -eq 1) {
            $proxyServer = [string]$internetSettings.ProxyServer
            if ($proxyServer) {
                foreach ($part in $proxyServer -split ";") {
                    $candidate = $part
                    if ($candidate -match "=") {
                        $candidate = ($candidate -split "=", 2)[1]
                    }
                    Add-ProxyCandidate -Candidates $candidates -Value $candidate
                }
            }
        }
    } catch {
    }

    if (Test-Command "git") {
        foreach ($key in @("http.proxy", "https.proxy")) {
            try {
                $value = (& git config --get $key 2>$null)
                if ($LASTEXITCODE -eq 0) {
                    Add-ProxyCandidate -Candidates $candidates -Value $value
                }
            } catch {
            }
        }
    }

    if (Test-Command "npm") {
        foreach ($key in @("https-proxy", "proxy")) {
            try {
                $value = (& npm config get $key 2>$null)
                if ($LASTEXITCODE -eq 0) {
                    Add-ProxyCandidate -Candidates $candidates -Value $value
                }
            } catch {
            }
        }
    }

    try {
        $winHttp = (& netsh winhttp show proxy 2>$null) -join "`n"
        foreach ($match in [regex]::Matches($winHttp, "(https?://[^\s;]+|(?:127\.0\.0\.1|localhost|\d{1,3}(?:\.\d{1,3}){3}):\d+)")) {
            Add-ProxyCandidate -Candidates $candidates -Value $match.Value
        }
    } catch {
    }

    return @($candidates)
}

function Use-InstallProxy {
    param([string]$Proxy)
    $script:InstallProxy = $Proxy
    foreach ($name in @("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")) {
        [Environment]::SetEnvironmentVariable($name, $Proxy, "Process")
    }
    Write-Ok "本次下载安装将使用代理：$(Format-ProxyForDisplay $Proxy)"
}

function Ensure-InstallProxyPreference {
    if ($script:InstallProxyPreferenceChecked) {
        return
    }
    $script:InstallProxyPreferenceChecked = $true

    Write-Step "检查下载代理"
    $candidates = @(Get-ProxyCandidates)
    if ($candidates.Count -eq 0) {
        Write-WarnLine "未检测到本地代理配置。"
        $manual = Read-Host "如需使用代理下载安装，请输入代理地址；直接回车表示不使用代理"
        $manual = $manual.Trim()
        if ($manual) {
            if ($manual -notmatch "^[a-zA-Z][a-zA-Z0-9+.-]*://") {
                $manual = "http://$manual"
            }
            Use-InstallProxy -Proxy $manual
        } else {
            Write-Host "本次下载安装不使用代理。"
        }
        return
    }

    Write-Host "检测到以下本地代理配置："
    for ($i = 0; $i -lt $candidates.Count; $i++) {
        Write-Host "  $($i + 1). $(Format-ProxyForDisplay $candidates[$i])"
    }
    while ($true) {
        $answer = Read-Host "是否使用代理下载安装？回车或 y 使用第 1 个；输入序号选择；输入 n 不使用；也可以直接粘贴代理地址"
        $normalized = $answer.Trim()
        if ([string]::IsNullOrWhiteSpace($normalized) -or $normalized.ToLowerInvariant() -in @("y", "yes", "是", "好", "确认")) {
            Use-InstallProxy -Proxy $candidates[0]
            return
        }
        if ($normalized.ToLowerInvariant() -in @("n", "no", "否", "不")) {
            Write-Host "本次下载安装不使用脚本检测到的代理。"
            return
        }
        $index = 0
        if ([int]::TryParse($normalized, [ref]$index) -and $index -ge 1 -and $index -le $candidates.Count) {
            Use-InstallProxy -Proxy $candidates[$index - 1]
            return
        }
        if ($normalized -match "^[a-zA-Z][a-zA-Z0-9+.-]*://|^(?:127\.0\.0\.1|localhost|\d{1,3}(?:\.\d{1,3}){3}):\d+") {
            if ($normalized -notmatch "^[a-zA-Z][a-zA-Z0-9+.-]*://") {
                $normalized = "http://$normalized"
            }
            Use-InstallProxy -Proxy $normalized
            return
        }
        Write-WarnLine "输入无法识别，请输入 y、n、序号，或代理地址。"
    }
}

function Get-PipProxyArgs {
    if ([string]::IsNullOrWhiteSpace($script:InstallProxy)) {
        return @()
    }
    return @("--proxy", $script:InstallProxy)
}

function Get-NpmProxyArgs {
    if ([string]::IsNullOrWhiteSpace($script:InstallProxy)) {
        return @()
    }
    return @("--proxy", $script:InstallProxy, "--https-proxy", $script:InstallProxy)
}

function Get-TailArgs {
    param([string[]]$Command)
    if ($Command.Count -le 1) {
        return @()
    }
    return $Command[1..($Command.Count - 1)]
}

function Resolve-PythonCommand {
    if (Test-Command "python") {
        try {
            $version = & python --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$version" -match "Python\s+3\.") {
                return @("python")
            }
        } catch {
        }
    }
    if (Test-Command "py") {
        try {
            $version = & py -3 --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$version" -match "Python\s+3\.") {
                return @("py", "-3")
            }
        } catch {
        }
    }
    if (Test-Command "conda") {
        try {
            $condaBase = (& conda info --base 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and $condaBase) {
                $condaPython = Join-Path $condaBase "python.exe"
                if (Test-Path -LiteralPath $condaPython) {
                    $version = & $condaPython --version 2>&1
                    if ($LASTEXITCODE -eq 0 -and "$version" -match "Python\s+3\.") {
                        return @($condaPython)
                    }
                }
            }
        } catch {
        }
    }
    return @()
}

function Invoke-Python {
    param(
        [string[]]$PythonCommand,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $PSScriptRoot
    )
    $exe = $PythonCommand[0]
    $baseArgs = Get-TailArgs -Command $PythonCommand
    Push-Location -LiteralPath $WorkingDirectory
    try {
        & $exe @baseArgs @Arguments
        return $LASTEXITCODE
    } finally {
        Pop-Location
    }
}

function Invoke-PythonLogged {
    param(
        [string[]]$PythonCommand,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $PSScriptRoot,
        [string]$Activity = "正在执行 Python 命令"
    )
    $exe = $PythonCommand[0]
    $baseArgs = Get-TailArgs -Command $PythonCommand
    Invoke-Logged `
        -FilePath $exe `
        -ArgumentList ($baseArgs + $Arguments) `
        -WorkingDirectory $WorkingDirectory `
        -Activity $Activity
}

function Invoke-PythonCapture {
    param(
        [string[]]$PythonCommand,
        [string[]]$Arguments,
        [string]$WorkingDirectory = $PSScriptRoot
    )
    $exe = $PythonCommand[0]
    $baseArgs = Get-TailArgs -Command $PythonCommand
    Push-Location -LiteralPath $WorkingDirectory
    try {
        $output = & $exe @baseArgs @Arguments 2>&1
        return [pscustomobject]@{
            ExitCode = $LASTEXITCODE
            Output = ($output -join "`n")
        }
    } catch {
        return [pscustomobject]@{
            ExitCode = 1
            Output = $_.Exception.Message
        }
    } finally {
        Pop-Location
    }
}

function Ensure-WingetPackage {
    param(
        [string]$PackageId,
        [string]$DisplayName
    )
    if (-not (Test-Command "winget")) {
        throw "未检测到 winget，无法自动安装 $DisplayName。请手动安装后重新运行本脚本。"
    }
    Ensure-InstallProxyPreference
    Invoke-Logged `
        -FilePath "winget" `
        -ArgumentList @("install", "--id", $PackageId, "-e", "--source", "winget", "--accept-package-agreements", "--accept-source-agreements") `
        -Activity "正在安装 $DisplayName"
}

function Ensure-Python {
    Write-Step "检查 Python"
    $python = @(Resolve-PythonCommand)
    if ($python.Count -gt 0) {
        $exe = $python[0]
        try {
            $version = & $exe @(Get-TailArgs -Command $python) --version 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "$version"
                return $python
            }
        } catch {
            Write-WarnLine "检测 Python 版本失败，将按未安装处理：$($_.Exception.Message)"
        }
    }

    Write-WarnLine "未检测到可用的 Python 3。"
    if (Confirm-Action "是否现在通过 winget 自动安装 Python 3？") {
        Ensure-WingetPackage -PackageId "Python.Python.3.13" -DisplayName "Python 3"
        $python = @(Resolve-PythonCommand)
        if ($python.Count -gt 0) {
            Write-Ok "Python 已安装"
            return $python
        }
        throw "Python 已安装，但当前 PowerShell 会话暂时找不到它。请重启 PowerShell 后重新运行本脚本。"
    }
    throw "需要 Python 3 才能继续。"
}

function Ensure-VirtualEnvironment {
    param(
        [string[]]$SystemPythonCommand,
        [string]$VenvPath
    )
    $venvPython = Join-Path $VenvPath "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Write-Step "创建项目虚拟环境"
        Invoke-PythonLogged `
            -PythonCommand $SystemPythonCommand `
            -Arguments (@("-m", "venv", $VenvPath)) `
            -WorkingDirectory $root `
            -Activity "正在创建项目虚拟环境" | Out-Null
        if (-not (Test-Path -LiteralPath $venvPython)) {
            throw "项目虚拟环境创建失败：$VenvPath"
        }
        Write-Ok "虚拟环境已创建：$VenvPath"
    } else {
        Write-Ok "使用项目虚拟环境：$VenvPath"
    }
    return @($venvPython)
}

function Ensure-WebsocketClient {
    param(
        [string[]]$PythonCommand,
        [string]$RequirementsPath
    )
    Write-Step "检查项目 Python 依赖"
    $code = "import websocket; print(getattr(websocket, '__version__', 'installed'))"
    $exe = $PythonCommand[0]
    try {
        $output = & $exe @(Get-TailArgs -Command $PythonCommand) -c $code 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "websocket-client $output"
            return
        }
    } catch {
        $output = $_.Exception.Message
    }

    Write-WarnLine "缺少项目 Python 依赖 websocket-client。检测输出：$output"
    if (Confirm-Action "是否现在安装项目虚拟环境依赖？") {
        Ensure-InstallProxyPreference
        $pipArgs = @("-m", "pip", "install", "--progress-bar", "on") + (Get-PipProxyArgs) + @("-r", $RequirementsPath)
        Invoke-PythonLogged -PythonCommand $PythonCommand -Arguments $pipArgs -Activity "正在安装项目 Python 依赖" | Out-Null
        try {
            $output = & $exe @(Get-TailArgs -Command $PythonCommand) -c $code 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Ok "websocket-client $output"
                return
            }
        } catch {
            $output = $_.Exception.Message
        }
        throw "websocket-client 安装后仍未通过导入检查：$output"
    }
    throw "需要 websocket-client 才能继续。"
}

function Ensure-NodeAndNpm {
    Write-Step "检查 Node.js 和 npm"
    if (Test-Command "node" -and Test-Command "npm") {
        try {
            $nodeVersion = & node --version 2>&1
            $nodeOk = $LASTEXITCODE -eq 0
            $npmVersion = & npm --version 2>&1
            $npmOk = $LASTEXITCODE -eq 0
            if ($nodeOk -and $npmOk) {
                Write-Ok "node $nodeVersion；npm $npmVersion"
                return
            }
            Write-WarnLine "Node.js 或 npm 命令存在但不可用：node=$nodeVersion；npm=$npmVersion"
        } catch {
            Write-WarnLine "检测 Node.js/npm 失败，将按未安装处理：$($_.Exception.Message)"
        }
    }

    Write-WarnLine "未检测到可用的 Node.js/npm。安装 npm 版 Codex CLI 时需要它。"
    if (Confirm-Action "是否现在通过 winget 自动安装 Node.js LTS？") {
        Ensure-WingetPackage -PackageId "OpenJS.NodeJS.LTS" -DisplayName "Node.js LTS"
        if (Test-Command "node" -and Test-Command "npm") {
            Write-Ok "Node.js/npm 已安装"
            return
        }
        throw "Node.js 已安装，但当前 PowerShell 会话暂时找不到 npm。请重启 PowerShell 后重新运行本脚本。"
    }
    throw "未找到可运行的 Codex CLI 时，需要 npm 才能继续自动安装。"
}

function Test-ExecutableVersion {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    try {
        $result = & $Path --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$result".Trim()) {
            Write-Ok "Codex CLI: $Path ($result)"
            return $true
        }
    } catch {
    }
    return $false
}

function Resolve-CodexCommand {
    $appDataCodex = Join-Path $env:APPDATA "npm\codex.cmd"
    if (Test-Path -LiteralPath $appDataCodex) {
        if (Test-ExecutableVersion -Path $appDataCodex) {
            return $appDataCodex
        }
    }

    $codexCmd = Get-Command "codex.cmd" -ErrorAction SilentlyContinue
    if ($codexCmd -and (Test-ExecutableVersion -Path $codexCmd.Source)) {
        return $codexCmd.Source
    }

    if (Test-Command "npm") {
        try {
            $npmPrefix = (& npm prefix -g 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and $npmPrefix) {
                $npmCodex = Join-Path $npmPrefix "codex.cmd"
                if (Test-Path -LiteralPath $npmCodex) {
                    if (Test-ExecutableVersion -Path $npmCodex) {
                        return $npmCodex
                    }
                }
            }
        } catch {
        }
    }

    $codex = Get-Command "codex" -ErrorAction SilentlyContinue
    if ($codex -and (Test-ExecutableVersion -Path $codex.Source)) {
        return $codex.Source
    }

    return ""
}

function Ensure-CodexCli {
    Write-Step "检查 Codex CLI"
    $codexCommand = Resolve-CodexCommand
    if ($codexCommand) {
        return $codexCommand
    }

    Write-WarnLine "未找到可运行的 Codex CLI。"
    Write-WarnLine "如果 WindowsApps 的 codex.exe 返回 Access is denied，本脚本可以改装 npm 版 Codex CLI。"
    Ensure-NodeAndNpm
    if (Confirm-Action "是否现在通过 npm 全局安装 @openai/codex？") {
        Ensure-InstallProxyPreference
        $npmArgs = @("install", "-g") + (Get-NpmProxyArgs) + @("@openai/codex")
        Invoke-Logged -FilePath "npm" -ArgumentList $npmArgs -Activity "正在安装 npm 版 Codex CLI"
        $codexCommand = Resolve-CodexCommand
        if ($codexCommand) {
            return $codexCommand
        }
        throw "@openai/codex 已安装，但仍未找到可运行的 codex 命令。请检查 npm 全局目录和 PATH。"
    }
    throw "需要可运行的 Codex CLI 才能继续。"
}

function Read-EnvFile {
    param([string]$Path)
    $map = [ordered]@{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $map
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $key, $value = $trimmed.Split("=", 2)
        $map[$key.Trim()] = $value.Trim().Trim('"').Trim("'")
    }
    return $map
}

function Get-EnvValue {
    param(
        [hashtable]$Map,
        [string]$Key,
        [string]$DefaultValue
    )
    if ($Map.Contains($Key) -and -not [string]::IsNullOrWhiteSpace([string]$Map[$Key])) {
        return [string]$Map[$Key]
    }
    return $DefaultValue
}

function Test-ConfiguredValue {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $false
    }
    return -not ($Value.Trim().ToLowerInvariant().StartsWith("replace-"))
}

function Read-SecretPlainText {
    param([string]$Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Write-ConfigHelp {
    Write-Host ""
    Write-Host "QQ Bot AppID / AppSecret 获取方式：" -ForegroundColor Yellow
    Write-Host "申请入口：https://q.qq.com/#/"
    Write-Host "1. 打开 QQ 官方机器人/开放平台后台：https://q.qq.com/#/"
    Write-Host "2. 创建或选择你的 Bot 应用。"
    Write-Host "3. 在开发设置、基础信息或凭据页面复制 AppID 和 AppSecret。"
    Write-Host "4. 确认机器人启用了 Gateway 事件，并至少允许 C2C_MESSAGE_CREATE 或 GROUP_AT_MESSAGE_CREATE。"
    Write-Host "5. 如需按钮卡片，再启用 INTERACTION_CREATE，并在 .env 中加入对应事件。"
    Write-Host "注意：这里不需要、也不应该输入个人 QQ 账号密码。"
}

function Write-EnvFile {
    param(
        [string]$Path,
        [hashtable]$Existing,
        [string]$CodexCommand,
        [string]$CodexWorkdir,
        [string]$AppId,
        [string]$AppSecret
    )

    $lines = @(
        "CODEX_COMMAND=$CodexCommand",
        "CODEX_WORKDIR=$CodexWorkdir",
        "CODEX_MODEL=$(Get-EnvValue -Map $Existing -Key 'CODEX_MODEL' -DefaultValue 'gpt-5.5')",
        "CODEX_REASONING_EFFORT=$(Get-EnvValue -Map $Existing -Key 'CODEX_REASONING_EFFORT' -DefaultValue 'xhigh')",
        "CODEX_PERMISSION=$(Get-EnvValue -Map $Existing -Key 'CODEX_PERMISSION' -DefaultValue 'read-only')",
        "CODEX_CONTEXT_MODE=$(Get-EnvValue -Map $Existing -Key 'CODEX_CONTEXT_MODE' -DefaultValue 'native')",
        "CODEX_ALLOWED_MODELS=$(Get-EnvValue -Map $Existing -Key 'CODEX_ALLOWED_MODELS' -DefaultValue 'gpt-5.5,gpt-5.4')",
        "CODEX_TIMEOUT_SECONDS=$(Get-EnvValue -Map $Existing -Key 'CODEX_TIMEOUT_SECONDS' -DefaultValue '1800')",
        "RECENT_DEFAULT_COUNT=$(Get-EnvValue -Map $Existing -Key 'RECENT_DEFAULT_COUNT' -DefaultValue '5')",
        "MAX_HISTORY_CHARS=$(Get-EnvValue -Map $Existing -Key 'MAX_HISTORY_CHARS' -DefaultValue '12000')",
        "",
        "# QQ official Gateway mode.",
        "# C2C and group @ messages usually use 1<<25.",
        "# Add INTERACTION_CREATE when Markdown/Keyboard buttons are enabled.",
        "QQ_APP_ID=$AppId",
        "QQ_APP_SECRET=$AppSecret",
        "QQ_API_BASE=$(Get-EnvValue -Map $Existing -Key 'QQ_API_BASE' -DefaultValue 'https://api.sgroup.qq.com')",
        "QQ_AUTH_URL=$(Get-EnvValue -Map $Existing -Key 'QQ_AUTH_URL' -DefaultValue 'https://bots.qq.com/app/getAppAccessToken')",
        "QQ_GATEWAY_PATH=$(Get-EnvValue -Map $Existing -Key 'QQ_GATEWAY_PATH' -DefaultValue '/gateway')",
        "QQ_GATEWAY_INTENTS=$(Get-EnvValue -Map $Existing -Key 'QQ_GATEWAY_INTENTS' -DefaultValue '33554432')",
        "QQ_ALLOWED_EVENTS=$(Get-EnvValue -Map $Existing -Key 'QQ_ALLOWED_EVENTS' -DefaultValue 'C2C_MESSAGE_CREATE,GROUP_AT_MESSAGE_CREATE')",
        "QQ_REPLY_MAX_CHARS=$(Get-EnvValue -Map $Existing -Key 'QQ_REPLY_MAX_CHARS' -DefaultValue '1500')",
        "QQ_MAX_REPLY_CHUNKS=$(Get-EnvValue -Map $Existing -Key 'QQ_MAX_REPLY_CHUNKS' -DefaultValue '5')",
        "QQ_JOB_QUEUE_SIZE=$(Get-EnvValue -Map $Existing -Key 'QQ_JOB_QUEUE_SIZE' -DefaultValue '20')",
        "QQ_CODEX_MAX_PARALLEL=$(Get-EnvValue -Map $Existing -Key 'QQ_CODEX_MAX_PARALLEL' -DefaultValue '5')",
        "QQ_TASK_STATUS_INTERVAL_SECONDS=$(Get-EnvValue -Map $Existing -Key 'QQ_TASK_STATUS_INTERVAL_SECONDS' -DefaultValue '300')",
        "QQ_TASK_PARTIAL_INTERVAL_SECONDS=$(Get-EnvValue -Map $Existing -Key 'QQ_TASK_PARTIAL_INTERVAL_SECONDS' -DefaultValue '60')",
        "QQ_TASK_PARTIAL_MAX_CHARS=$(Get-EnvValue -Map $Existing -Key 'QQ_TASK_PARTIAL_MAX_CHARS' -DefaultValue '1200')",
        "QQ_SEND_PARTIAL_OUTPUTS=$(Get-EnvValue -Map $Existing -Key 'QQ_SEND_PARTIAL_OUTPUTS' -DefaultValue '0')",
        "QQ_SHOW_TASK_CONTEXT_ON_FINAL=$(Get-EnvValue -Map $Existing -Key 'QQ_SHOW_TASK_CONTEXT_ON_FINAL' -DefaultValue '1')",
        "QQ_TRUNCATE_LONG_REPLIES=$(Get-EnvValue -Map $Existing -Key 'QQ_TRUNCATE_LONG_REPLIES' -DefaultValue '1')",
        "QQ_SEND_PROCESSING_MESSAGE=$(Get-EnvValue -Map $Existing -Key 'QQ_SEND_PROCESSING_MESSAGE' -DefaultValue '0')",
        "QQ_PROCESSING_TEXT=$(Get-EnvValue -Map $Existing -Key 'QQ_PROCESSING_TEXT' -DefaultValue '')",
        "QQ_SEND_STARTUP_TO_ALLOWED_USERS=$(Get-EnvValue -Map $Existing -Key 'QQ_SEND_STARTUP_TO_ALLOWED_USERS' -DefaultValue '1')",
        "QQ_ATTACHMENT_DOWNLOAD=$(Get-EnvValue -Map $Existing -Key 'QQ_ATTACHMENT_DOWNLOAD' -DefaultValue '1')",
        "QQ_ATTACHMENT_MAX_COUNT=$(Get-EnvValue -Map $Existing -Key 'QQ_ATTACHMENT_MAX_COUNT' -DefaultValue '4')",
        "QQ_ATTACHMENT_MAX_BYTES=$(Get-EnvValue -Map $Existing -Key 'QQ_ATTACHMENT_MAX_BYTES' -DefaultValue '26214400')",
        "QQ_SEND_LOCAL_IMAGES=$(Get-EnvValue -Map $Existing -Key 'QQ_SEND_LOCAL_IMAGES' -DefaultValue '1')",
        "QQ_SEND_IMAGE_MAX_COUNT=$(Get-EnvValue -Map $Existing -Key 'QQ_SEND_IMAGE_MAX_COUNT' -DefaultValue '4')",
        "QQ_SEND_IMAGE_MAX_BYTES=$(Get-EnvValue -Map $Existing -Key 'QQ_SEND_IMAGE_MAX_BYTES' -DefaultValue '10485760')",
        "QQ_RESTART_DELAY_SECONDS=$(Get-EnvValue -Map $Existing -Key 'QQ_RESTART_DELAY_SECONDS' -DefaultValue '2')",
        "",
        "# Optional labels and allowlists.",
        "# Send /whoami to the bot first, then put user_openid into QQ_ALLOWED_USER_OPENIDS.",
        "# Leave allowlists empty only when every reachable user/group may use the bot.",
        "QQ_OWNER_QQ=$(Get-EnvValue -Map $Existing -Key 'QQ_OWNER_QQ' -DefaultValue '')",
        "QQ_ALLOWED_USER_OPENIDS=$(Get-EnvValue -Map $Existing -Key 'QQ_ALLOWED_USER_OPENIDS' -DefaultValue '')",
        "QQ_ALLOWED_GROUP_OPENIDS=$(Get-EnvValue -Map $Existing -Key 'QQ_ALLOWED_GROUP_OPENIDS' -DefaultValue '')"
    )

    Set-Content -LiteralPath $Path -Value $lines -Encoding UTF8
}

function Ensure-EnvConfig {
    param(
        [string]$EnvPath,
        [string]$CodexCommand,
        [string]$DefaultWorkdir
    )

    Write-Step "检查桥接器配置"
    $existing = Read-EnvFile -Path $EnvPath
    $appId = Get-EnvValue -Map $existing -Key "QQ_APP_ID" -DefaultValue "replace-with-qq-app-id"
    $appSecret = Get-EnvValue -Map $existing -Key "QQ_APP_SECRET" -DefaultValue "replace-with-qq-app-secret"
    $codexWorkdir = Get-EnvValue -Map $existing -Key "CODEX_WORKDIR" -DefaultValue $DefaultWorkdir

    $needsCredentials = $Reconfigure -or -not (Test-ConfiguredValue $appId) -or -not (Test-ConfiguredValue $appSecret)
    if ($needsCredentials) {
        Write-ConfigHelp
        $inputAppId = Read-Host "请输入 QQ_APP_ID"
        if (-not [string]::IsNullOrWhiteSpace($inputAppId)) {
            $appId = $inputAppId.Trim()
        }
        $inputSecret = Read-SecretPlainText -Prompt "请输入 QQ_APP_SECRET（输入时不会显示）"
        if (-not [string]::IsNullOrWhiteSpace($inputSecret)) {
            $appSecret = $inputSecret.Trim()
        }
    }

    if ($Reconfigure -or -not (Test-ConfiguredValue $codexWorkdir)) {
        $answer = Read-Host "Codex 工作目录 [$codexWorkdir]"
        if (-not [string]::IsNullOrWhiteSpace($answer)) {
            $codexWorkdir = $answer.Trim()
        }
    }

    if (-not (Test-ConfiguredValue $appId) -or -not (Test-ConfiguredValue $appSecret)) {
        throw "必须先配置 QQ_APP_ID 和 QQ_APP_SECRET，才能连接 QQ Gateway。"
    }

    Write-EnvFile -Path $EnvPath -Existing $existing -CodexCommand $CodexCommand -CodexWorkdir $codexWorkdir -AppId $appId -AppSecret $appSecret
    Write-Ok "配置已保存到 $EnvPath"
    Write-Host "当前配置摘要："
    Write-Host "  CODEX_COMMAND=$CodexCommand"
    Write-Host "  CODEX_WORKDIR=$codexWorkdir"
    Write-Host "  QQ_APP_ID=$appId"
    Write-Host "  QQ_APP_SECRET=(已隐藏)"
}

function Read-DeployPreferences {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return @{}
    }
    try {
        $json = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        $map = @{}
        foreach ($property in $json.PSObject.Properties) {
            $map[$property.Name] = $property.Value
        }
        return $map
    } catch {
        Write-WarnLine "无法读取部署偏好，将忽略：$Path"
        return @{}
    }
}

function Write-DeployPreferences {
    param(
        [string]$Path,
        [hashtable]$Preferences
    )
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $Preferences | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Test-AutostartTaskConfigured {
    param(
        [string]$TaskName,
        [string]$AutostartScript
    )
    if (-not (Test-Path -LiteralPath $AutostartScript)) {
        return $false
    }

    try {
        $expectedScript = (Resolve-Path -LiteralPath $AutostartScript).Path
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            return $false
        }
        if ([string]$task.State -eq "Disabled") {
            return $false
        }

        $hasExpectedAction = $false
        foreach ($action in @($task.Actions)) {
            $arguments = [string]$action.Arguments
            if ($arguments.IndexOf($expectedScript, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $hasExpectedAction = $true
                break
            }
        }
        if (-not $hasExpectedAction) {
            return $false
        }

        $enabledTriggers = @($task.Triggers | Where-Object { $_.Enabled -ne $false })
        return $enabledTriggers.Count -gt 0
    } catch {
        Write-WarnLine "无法验证自启动任务，将继续询问：$($_.Exception.Message)"
        return $false
    }
}

function Ensure-AutostartTask {
    param(
        [string]$TaskName,
        [string]$AutostartScript
    )
    if (-not (Test-Path -LiteralPath $AutostartScript)) {
        throw "找不到自启动脚本：$AutostartScript"
    }

    Write-Step "配置 Windows 登录自启动"
    $powershellPath = Join-Path $PSHOME "powershell.exe"
    if (-not (Test-Path -LiteralPath $powershellPath)) {
        $powershellPath = "powershell.exe"
    }

    $action = New-ScheduledTaskAction `
        -Execute $powershellPath `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$AutostartScript`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Days 0)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "用户登录时启动 Codex Remote Bridge。" `
        -Force | Out-Null

    Write-Ok "自启动任务已配置：$TaskName"
    Write-Host "  脚本：$AutostartScript"
}

function Maybe-ConfigureAutostart {
    param(
        [string]$PreferencesPath,
        [string]$AutostartScript
    )
    $taskName = "CodexRemoteBridge"
    if (Test-AutostartTaskConfigured -TaskName $taskName -AutostartScript $AutostartScript) {
        Write-Ok "已检测到本项目的自启动任务处于启用状态，跳过自启动询问。"
        return
    }

    $preferences = Read-DeployPreferences -Path $PreferencesPath
    if ([string]$preferences["autostart_prompt"] -eq "never") {
        Write-Ok "你之前选择过 never，本次跳过自启动询问。"
        return
    }

    $choice = Read-YesNoNever -Question "是否要配置 Windows 登录后自启动 Codex Remote Bridge？"
    if ($choice -eq "never") {
        $preferences["autostart_prompt"] = "never"
        Write-DeployPreferences -Path $PreferencesPath -Preferences $preferences
        Write-Ok "已保存偏好：以后不再询问自启动。"
        return
    }
    if ($choice -eq "no") {
        Write-Host "本次不配置自启动。下次运行脚本仍会再次询问。"
        return
    }

    Ensure-AutostartTask -TaskName $taskName -AutostartScript $AutostartScript
}

function Show-QuickStartHelp {
    param(
        [string]$LogFile,
        [bool]$IsBackground
    )
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host "  启动后会尝试向已认证用户发送 /start，也可手动发送 /start" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "桥接器启动后的快速入门：" -ForegroundColor Cyan
    Write-Host "1. 给机器人发送 /start，查看当前 model、reasoning 和快捷按钮。"
    Write-Host "2. 给机器人发送 /status，确认 Gateway、模型、思考强度和权限模式。"
    Write-Host "3. 给机器人发送 /whoami，获取你的 user_openid。"
    Write-Host "4. 如果要限制访问，把 user_openid 写入 client\.env 的 QQ_ALLOWED_USER_OPENIDS，然后重启。"
    Write-Host "5. 给机器人发送 /help，可以随时查看完整命令。"
    Write-Host "6. 直接发送普通消息，即可通过这个桥接器调用 Codex。"
    Write-Host "7. 如果 client\.env 已配置 QQ_ALLOWED_USER_OPENIDS，启动时会主动给这些用户发送 /start。"
    Write-Host "8. 每次桥接器启动后，每个 QQ 联系来源首次发消息时，机器人也会自动返回一次 /start。"
    Write-Host ""
    Write-Host "常用命令："
    Write-Host "  /start                        显示入口面板和快捷按钮"
    Write-Host "  /help                         显示所有命令"
    Write-Host "  /status                       显示桥接器状态"
    Write-Host "  /whoami                       显示当前 QQ Gateway openid"
    Write-Host "  /model                        显示当前模型和思考强度"
    Write-Host "  /model gpt-5.5 high           设置模型和思考强度"
    Write-Host "  /setup                        显示设置面板"
    Write-Host "  /permission                   显示权限模式"
    Write-Host "  /timeout 45                   设置单次 Codex 调用超时为 45 分钟"
    Write-Host "  /heartbeat 5                  设置任务运行提醒频率为 5 分钟"
    Write-Host "  /truncate on/off              开关长内容截断"
    Write-Host "  /recent-default 10            设置最近对话默认展示 10 条"
    Write-Host "  /permission read only         使用只读模式"
    Write-Host "  /permission ask               请求批准"
    Write-Host "  /permission auto              替我审批"
    Write-Host "  /permission full              完全访问权限"
    Write-Host "  /pending                      显示待批准操作"
    Write-Host "  /allow <id>                   批准待执行操作"
    Write-Host "  /cancel                       取消当前 Codex 任务"
    Write-Host "  /cancel <task_id>             取消指定 Codex 任务"
    Write-Host "  /tasks                        显示运行中和排队中的 Codex 任务"
    Write-Host "  /restart                      重启 QQ Gateway 客户端"
    Write-Host "  /resume                       列出 Codex 原生会话"
    Write-Host "  /new [标题]                   开启新的 Codex 会话"
    if ($IsBackground) {
        Write-Host ""
        Write-Host "后台日志："
        Write-Host "  $LogFile"
    } else {
        Write-Host ""
        Write-Host "前台模式：保持这个 PowerShell 窗口打开。按 Ctrl+C 停止。"
    }
}

function Get-BridgeProcesses {
    param([string]$ClientDir)
    $resolvedClientDir = (Resolve-Path -LiteralPath $ClientDir).Path
    $escapedClientDir = [regex]::Escape($resolvedClientDir)
    $currentPid = $PID

    Get-CimInstance Win32_Process |
        Where-Object {
            $_.ProcessId -ne $currentPid -and
            $_.Name -match '^(python|pythonw|py)\.exe$' -and
            $_.CommandLine -and
            $_.CommandLine -match $escapedClientDir -and
            ($_.CommandLine -match 'qq_gateway_client\.py' -or $_.CommandLine -match 'qq_gateway_background\.py')
        } |
        Select-Object ProcessId, Name, CommandLine
}

function Stop-BridgeProcessTree {
    param(
        [int]$ProcessId,
        [string]$Label
    )
    $output = & taskkill.exe /PID $ProcessId /T /F 2>&1
    if ($LASTEXITCODE -ne 0) {
        $stillRunning = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
        if ($stillRunning) {
            throw "无法终止 $Label PID ${ProcessId}: $output"
        }
        Write-Ok "$Label PID $ProcessId 已退出"
        return
    }
    Write-Ok "已终止 $Label 进程树 PID $ProcessId"
}

function Resolve-ForegroundProcessConflict {
    param([string]$ClientDir)
    $processes = @(Get-BridgeProcesses -ClientDir $ClientDir)
    if ($processes.Count -eq 0) {
        return
    }

    Write-Host ""
    Write-Fail "检测到已有后台 Codex Remote Bridge 进程正在运行。"
    foreach ($process in $processes) {
        Write-Host "  PID $($process.ProcessId): $($process.CommandLine)"
    }

    while ($true) {
        $answer = Read-Host "选择操作：输入 y 终止这些后台进程并继续前台启动；输入 n 停止本次前台运行 [y/n]"
        $normalized = $answer.Trim().ToLowerInvariant()
        if ($normalized -in @("y", "yes", "是", "好", "确认")) {
            $supervisors = @($processes | Where-Object { $_.CommandLine -match 'qq_gateway_background\.py' })
            $clients = @($processes | Where-Object { $_.CommandLine -match 'qq_gateway_client\.py' })

            foreach ($process in $supervisors) {
                Stop-BridgeProcessTree -ProcessId $process.ProcessId -Label "supervisor"
            }

            Start-Sleep -Seconds 1
            $remaining = @(Get-BridgeProcesses -ClientDir $ClientDir)
            $remainingClients = @($remaining | Where-Object { $_.CommandLine -match 'qq_gateway_client\.py' })
            foreach ($process in $remainingClients) {
                Stop-BridgeProcessTree -ProcessId $process.ProcessId -Label "client"
            }

            $remaining = @(Get-BridgeProcesses -ClientDir $ClientDir)
            if ($remaining.Count -gt 0) {
                foreach ($process in $remaining) {
                    Write-WarnLine "仍检测到进程 PID $($process.ProcessId): $($process.CommandLine)"
                }
                throw "仍有 Codex Remote Bridge 进程未退出，请手动检查后再前台启动。"
            }
            Start-Sleep -Seconds 1
            return
        }
        if ($normalized -in @("n", "no", "否", "不", "")) {
            throw "已停止前台启动；后台 bridge 仍在运行。"
        }
        Write-WarnLine "请输入 y 或 n。"
    }
}

function Start-BridgeForeground {
    param(
        [string[]]$PythonCommand,
        [string]$ClientDir
    )
    Resolve-ForegroundProcessConflict -ClientDir $ClientDir
    Write-Step "以前台模式启动 QQ Gateway"
    Write-Host "保持此窗口打开；按 Ctrl+C 停止。"
    Invoke-Python -PythonCommand $PythonCommand -Arguments @(".\qq_gateway_client.py") -WorkingDirectory $ClientDir | Out-Null
}

function Start-BridgeBackground {
    param(
        [string[]]$PythonCommand,
        [string]$ClientDir,
        [string]$LogFile
    )
    Write-Step "以后台模式启动 QQ Gateway"
    $exe = $PythonCommand[0]
    $args = @(Get-TailArgs -Command $PythonCommand)
    $args += @("-B", (Join-Path $ClientDir "qq_gateway_background.py"))
    Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $ClientDir -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 3
    Write-Ok "后台守护进程已启动"
    if (Test-Path -LiteralPath $LogFile) {
        Write-Host ""
        Write-Host "最近日志：$LogFile" -ForegroundColor Cyan
        Get-Content -LiteralPath $LogFile -Tail 80 -Encoding UTF8
    } else {
        Write-WarnLine "日志文件尚未创建：$LogFile"
    }
}

if (-not $IsWindows -and $PSVersionTable.PSEdition -eq "Core") {
    throw "此脚本仅适用于 Windows。"
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$clientDir = Join-Path $root "client"
$envPath = Join-Path $clientDir ".env"
$venvPath = Join-Path $root ".venv"
$requirementsPath = Join-Path $root "requirements.txt"
$dataDir = Join-Path $clientDir "data"
$logFile = Join-Path $dataDir "qq-gateway-autostart.log"
$preferencesPath = Join-Path $dataDir "deploy-preferences.json"
$autostartScript = Join-Path $clientDir "start-bridge-autostart.ps1"

if (-not (Test-Path -LiteralPath $clientDir)) {
    throw "找不到 client 目录：$clientDir"
}

if ([string]::IsNullOrWhiteSpace($WorkDir)) {
    $WorkDir = $root
}

New-Item -ItemType Directory -Force -Path $dataDir | Out-Null

Write-Host "Codex Remote Bridge Windows 一键部署脚本" -ForegroundColor Cyan
Write-Host "项目目录：$root"

$systemPython = @(Ensure-Python)
$python = @(Ensure-VirtualEnvironment -SystemPythonCommand $systemPython -VenvPath $venvPath)
Ensure-WebsocketClient -PythonCommand $python -RequirementsPath $requirementsPath
$codexCommand = Ensure-CodexCli
Ensure-EnvConfig -EnvPath $envPath -CodexCommand $codexCommand -DefaultWorkdir $WorkDir

Write-Step "验证 Python 文件和本地命令"
$compileCheck = Invoke-PythonCapture -PythonCommand $python -Arguments @("-m", "py_compile", "codex_bridge_client.py", "qq_gateway_client.py", "qq_gateway_background.py") -WorkingDirectory $clientDir
if ($compileCheck.ExitCode -ne 0) {
    if ($compileCheck.Output) {
        Write-Host $compileCheck.Output -ForegroundColor Red
    }
    throw "Python 文件编译检查失败，请查看上方错误。"
}
$commandCheckCode = "from codex_bridge_client import handle_bridge_command; result = handle_bridge_command('/help'); assert isinstance(result, str) and '/help' in result"
$commandCheck = Invoke-PythonCapture -PythonCommand $python -Arguments @("-c", $commandCheckCode) -WorkingDirectory $clientDir
if ($commandCheck.ExitCode -ne 0) {
    if ($commandCheck.Output) {
        Write-Host $commandCheck.Output -ForegroundColor Red
    }
    throw "本地命令处理检查失败，请查看上方错误。"
}
Write-Ok "本地验证通过"

if ($CheckOnly) {
    Write-Step "仅检查模式"
    Write-Host "配置和本地验证已完成。"
    Write-Host "前台运行："
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    Write-Host "后台运行："
    Write-Host "  powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Background"
    exit 0
}

Maybe-ConfigureAutostart -PreferencesPath $preferencesPath -AutostartScript $autostartScript

if ($Background) {
    Show-QuickStartHelp -LogFile $logFile -IsBackground $true
    Start-BridgeBackground -PythonCommand $python -ClientDir $clientDir -LogFile $logFile
} else {
    Show-QuickStartHelp -LogFile $logFile -IsBackground $false
    Start-BridgeForeground -PythonCommand $python -ClientDir $clientDir
}
