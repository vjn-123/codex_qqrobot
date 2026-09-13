using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Management;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class Program
{
    [STAThread]
    private static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        bool createdNew;
        using (var mutex = new Mutex(true, @"Local\CodexRemoteBridgeTray", out createdNew))
        {
            if (!createdNew)
            {
                MessageBox.Show("Codex Remote Bridge 托盘程序已经在运行。", "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }

            Application.Run(new TrayContext());
            GC.KeepAlive(mutex);
        }
    }
}

internal sealed class TrayContext : ApplicationContext
{
    private const string TaskName = "CodexRemoteBridgeTray";
    private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";

    private readonly string exePath;
    private readonly string clientDir;
    private readonly string rootDir;
    private readonly string dataDir;
    private readonly string logFile;
    private readonly string configFile;
    private readonly string startScript;
    private readonly string backgroundScript;
    private readonly NotifyIcon notifyIcon;
    private readonly System.Windows.Forms.Timer timer;
    private readonly System.Windows.Forms.Timer autoStartTimer;
    private readonly ToolStripMenuItem statusItem;
    private readonly ToolStripMenuItem autostartItem;
    private readonly Icon runningIcon;
    private readonly Icon stoppedIcon;
    private int autoStartAttempts;
    private bool environmentPromptShown;

    public TrayContext()
    {
        exePath = Application.ExecutablePath;
        clientDir = ResolveClientDir(AppDomain.CurrentDomain.BaseDirectory);
        rootDir = Directory.GetParent(clientDir).FullName;
        dataDir = Path.Combine(clientDir, "data");
        logFile = Path.Combine(dataDir, "qq-gateway-autostart.log");
        configFile = Path.Combine(clientDir, ".env");
        startScript = Path.Combine(rootDir, "start.ps1");
        backgroundScript = Path.Combine(clientDir, "qq_gateway_background.py");

        Directory.CreateDirectory(dataDir);
        WriteLog("托盘程序启动");
        runningIcon = CreateStatusIcon(true);
        stoppedIcon = CreateStatusIcon(false);

        var menu = new ContextMenuStrip();
        statusItem = new ToolStripMenuItem("状态：检测中") { Enabled = false };
        autostartItem = new ToolStripMenuItem("开机自启动：检测中") { Enabled = false };
        menu.Items.Add(statusItem);
        menu.Items.Add(autostartItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("启动桥接", null, delegate { StartBridgeBackground(true); RefreshStatus(); });
        menu.Items.Add("停止桥接", null, delegate { StopBridge(); RefreshStatus(); });
        menu.Items.Add("重启桥接", null, delegate { RestartBridge(); RefreshStatus(); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("安装/检查/配置...", null, delegate { RunStartScript("-CheckOnly", false); });
        menu.Items.Add("前台启动窗口...", null, delegate { RunStartScript("", false); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("开启开机自启动", null, delegate { ConfigureAutostart(); RefreshStatus(); });
        menu.Items.Add("关闭开机自启动", null, delegate { RemoveAutostart(); RefreshStatus(); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("打开日志", null, delegate { OpenPath(logFile, "日志文件尚未创建。"); });
        menu.Items.Add("打开配置", null, delegate { OpenPath(configFile, "配置文件不存在，请先运行安装/检查/配置。"); });
        menu.Items.Add("打开项目目录", null, delegate { OpenPath(rootDir, "项目目录不存在。"); });
        menu.Items.Add("刷新状态", null, delegate { RefreshStatus(); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("停止桥接并退出", null, delegate { StopBridge(); ExitTray(); });

        notifyIcon = new NotifyIcon
        {
            Text = "Codex Remote Bridge：检测中",
            ContextMenuStrip = menu,
            Visible = true
        };
        notifyIcon.DoubleClick += delegate { ShowStatusBalloon(); };

        timer = new System.Windows.Forms.Timer { Interval = 5000 };
        timer.Tick += delegate { RefreshStatus(); };
        timer.Start();

        autoStartTimer = new System.Windows.Forms.Timer { Interval = 5000 };
        autoStartTimer.Tick += delegate { AutoStartBridgeTick(); };

        RefreshStatus();
        AutoStartBridgeTick();
    }

    private static string ResolveClientDir(string exeDir)
    {
        string normalizedExeDir = Path.GetFullPath(exeDir.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        if (File.Exists(Path.Combine(normalizedExeDir, "qq_gateway_background.py")))
        {
            return normalizedExeDir;
        }

        string childClientDir = Path.Combine(normalizedExeDir, "client");
        if (File.Exists(Path.Combine(childClientDir, "qq_gateway_background.py")))
        {
            return childClientDir;
        }

        return normalizedExeDir;
    }

    private void RefreshStatus()
    {
        bool running = GetBridgeProcesses().Any();
        statusItem.Text = running ? "状态：运行中" : "状态：未运行";
        autostartItem.Text = IsAutostartConfigured() ? "开机自启动：已开启" : "开机自启动：未开启";
        notifyIcon.Text = running ? "Codex Remote Bridge：运行中" : "Codex Remote Bridge：未运行";

        notifyIcon.Icon = running ? runningIcon : stoppedIcon;
    }

    private void ShowStatusBalloon()
    {
        bool running = GetBridgeProcesses().Any();
        notifyIcon.BalloonTipTitle = "Codex Remote Bridge";
        notifyIcon.BalloonTipText = running ? "后台正在运行。右键图标可停止、重启或打开日志。" : "后台未运行。右键图标可启动或打开配置检查。";
        notifyIcon.ShowBalloonTip(2500);
    }

    private IEnumerable<BridgeProcess> GetBridgeProcesses()
    {
        string normalizedClientDir = NormalizePathForCompare(clientDir);
        var processes = new List<BridgeProcess>();

        try
        {
            using (var searcher = new ManagementObjectSearcher("SELECT ProcessId, Name, CommandLine FROM Win32_Process"))
            {
                foreach (ManagementObject process in searcher.Get())
                {
                    string name = Convert.ToString(process["Name"] ?? "");
                    if (!IsPythonProcess(name))
                    {
                        continue;
                    }

                    string commandLine = Convert.ToString(process["CommandLine"] ?? "");
                    string normalizedCommandLine = NormalizeTextForCompare(commandLine);
                    if (!normalizedCommandLine.Contains(normalizedClientDir))
                    {
                        continue;
                    }

                    bool isBridgeProcess =
                        normalizedCommandLine.Contains("qq_gateway_background.py") ||
                        normalizedCommandLine.Contains("qq_gateway_client.py");
                    if (!isBridgeProcess)
                    {
                        continue;
                    }

                    processes.Add(new BridgeProcess
                    {
                        ProcessId = Convert.ToInt32(process["ProcessId"]),
                        CommandLine = commandLine,
                        IsSupervisor = normalizedCommandLine.Contains("qq_gateway_background.py")
                    });
                }
            }
        }
        catch (Exception ex)
        {
            WriteLog("进程检测失败：" + ex.Message);
        }

        return processes;
    }

    private static bool IsPythonProcess(string name)
    {
        return string.Equals(name, "python.exe", StringComparison.OrdinalIgnoreCase) ||
               string.Equals(name, "pythonw.exe", StringComparison.OrdinalIgnoreCase) ||
               string.Equals(name, "py.exe", StringComparison.OrdinalIgnoreCase);
    }

    private static string NormalizePathForCompare(string value)
    {
        return Path.GetFullPath(value.Replace('/', '\\')).TrimEnd('\\').ToLowerInvariant();
    }

    private static string NormalizeTextForCompare(string value)
    {
        return (value ?? "").Replace('/', '\\').ToLowerInvariant();
    }

    private void StartBridgeBackground(bool manual)
    {
        if (GetBridgeProcesses().Any())
        {
            WriteLog("启动请求：后台已在运行");
            return;
        }

        if (!File.Exists(backgroundScript))
        {
            MessageBox.Show("找不到后台脚本：" + backgroundScript, "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        string resultFile = Path.Combine(dataDir, "tray-start-result.txt");
        try
        {
            if (File.Exists(resultFile))
            {
                File.Delete(resultFile);
            }
        }
        catch
        {
        }

        var psi = new ProcessStartInfo
        {
            FileName = ResolvePowerShell(),
            Arguments = "-ExecutionPolicy Bypass -Command " + Quote(BuildBackgroundStartCommand(resultFile)),
            WorkingDirectory = clientDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = false,
            RedirectStandardError = false
        };

        try
        {
            using (Process process = Process.Start(psi))
            {
                if (!process.WaitForExit(30000))
                {
                    try
                    {
                        process.Kill();
                    }
                    catch
                    {
                    }
                    WriteLog("启动命令 30 秒内未返回，将继续按非环境错误重试");
                    return;
                }

                string resultText = ReadTextIfExists(resultFile).Trim();
                if (!string.IsNullOrWhiteSpace(resultText))
                {
                    WriteLog("启动结果：" + resultText);
                }

                if (process.ExitCode == 20)
                {
                    HandleEnvironmentMissing(resultText, manual);
                    return;
                }
                if (process.ExitCode != 0)
                {
                    WriteLog("启动命令失败，退出码 " + process.ExitCode);
                    if (manual)
                    {
                        MessageBox.Show("桥接启动失败，请查看日志。", "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    }
                    return;
                }

                WriteLog("已请求启动后台守护进程");
            }
        }
        catch (Exception ex)
        {
            WriteLog("后台启动失败：" + ex.Message);
            if (manual)
            {
                MessageBox.Show("后台启动失败：" + ex.Message, "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }

    private void AutoStartBridgeTick()
    {
        RefreshStatus();
        if (GetBridgeProcesses().Any())
        {
            autoStartTimer.Stop();
            WriteLog("自动启动检查：桥接已运行");
            return;
        }

        autoStartAttempts++;
        if (autoStartAttempts > 24)
        {
            autoStartTimer.Stop();
            WriteLog("自动启动检查：120 秒后仍未检测到桥接进程");
            MessageBox.Show(
                "桥接 120 秒内未能自动启动，请查看日志或右键托盘图标手动启动。",
                "Codex Remote Bridge",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
            return;
        }

        WriteLog("自动启动桥接，第 " + autoStartAttempts + " 次");
        StartBridgeBackground(false);

        RefreshStatus();
        if (GetBridgeProcesses().Any())
        {
            autoStartTimer.Stop();
            WriteLog("自动启动成功");
            return;
        }
        if (!autoStartTimer.Enabled)
        {
            autoStartTimer.Start();
        }
    }

    private void HandleEnvironmentMissing(string detail, bool manual)
    {
        autoStartTimer.Stop();
        WriteLog("检测到环境缺失：" + detail);
        if (environmentPromptShown && !manual)
        {
            return;
        }

        environmentPromptShown = true;
        DialogResult result = MessageBox.Show(
            "环境缺失，是否打开安装/检查/配置？",
            "Codex Remote Bridge",
            MessageBoxButtons.YesNo,
            MessageBoxIcon.Warning);
        if (result == DialogResult.Yes)
        {
            RunStartScript("-CheckOnly", false);
        }
    }

    private void StopBridge()
    {
        var processes = GetBridgeProcesses()
            .OrderByDescending(p => p.IsSupervisor)
            .ThenBy(p => p.ProcessId)
            .ToList();

        if (processes.Count == 0)
        {
            WriteLog("停止请求：未检测到运行中的后台");
            return;
        }

        foreach (BridgeProcess process in processes)
        {
            RunTaskkill(process.ProcessId);
            WriteLog("已请求终止进程树 PID " + process.ProcessId);
        }
    }

    private void RestartBridge()
    {
        WriteLog("重启请求");
        StopBridge();
        System.Threading.Thread.Sleep(1500);
        StartBridgeBackground(true);
    }

    private void RunStartScript(string extraArguments, bool hidden)
    {
        if (!File.Exists(startScript))
        {
            MessageBox.Show("找不到启动脚本：" + startScript, "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }

        var args = new StringBuilder();
        args.Append("-ExecutionPolicy Bypass -File ");
        args.Append(Quote(startScript));
        if (!string.IsNullOrWhiteSpace(extraArguments))
        {
            args.Append(' ');
            args.Append(extraArguments);
        }

        var psi = new ProcessStartInfo
        {
            FileName = ResolvePowerShell(),
            Arguments = args.ToString(),
            WorkingDirectory = rootDir,
            UseShellExecute = true,
            WindowStyle = hidden ? ProcessWindowStyle.Hidden : ProcessWindowStyle.Normal
        };

        try
        {
            Process.Start(psi);
            WriteLog("已打开 start.ps1 " + extraArguments);
        }
        catch (Exception ex)
        {
            WriteLog("打开 start.ps1 失败：" + ex.Message);
            MessageBox.Show("打开启动脚本失败：" + ex.Message, "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private bool IsAutostartConfigured()
    {
        try
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKeyPath, false))
            {
                string value = Convert.ToString(key == null ? null : key.GetValue(TaskName, ""));
                if (string.IsNullOrWhiteSpace(value))
                {
                    return false;
                }

                return NormalizeTextForCompare(value).Contains(NormalizePathForCompare(exePath));
            }
        }
        catch (Exception ex)
        {
            WriteLog("读取开机自启动状态失败：" + ex.Message);
            return false;
        }
    }

    private void ConfigureAutostart()
    {
        try
        {
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RunKeyPath))
            {
                key.SetValue(TaskName, Quote(exePath), RegistryValueKind.String);
            }

            WriteLog("已配置开机自启动");
            System.Threading.Thread.Sleep(300);
            MessageBox.Show("已配置 Windows 登录后启动托盘程序。", "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        catch (Exception ex)
        {
            WriteLog("配置开机自启动失败：" + ex.Message);
            MessageBox.Show("配置开机自启动失败：" + ex.Message, "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private void RemoveAutostart()
    {
        try
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKeyPath, true))
            {
                if (key != null)
                {
                    key.DeleteValue(TaskName, false);
                }
            }

            WriteLog("已取消开机自启动");
            System.Threading.Thread.Sleep(300);
            MessageBox.Show("已取消开机自启动。", "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        catch (Exception ex)
        {
            WriteLog("取消开机自启动失败：" + ex.Message);
            MessageBox.Show("取消开机自启动失败：" + ex.Message, "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private void RunTaskkill(int processId)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "taskkill.exe",
            Arguments = "/PID " + processId + " /T /F",
            WorkingDirectory = clientDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.Default,
            StandardErrorEncoding = Encoding.Default
        };

        try
        {
            using (Process process = Process.Start(psi))
            {
                string error = process.StandardError.ReadToEnd();
                process.StandardOutput.ReadToEnd();
                process.WaitForExit(10000);
                if (!string.IsNullOrWhiteSpace(error))
                {
                    WriteLog("taskkill：" + error.Trim());
                }
            }
        }
        catch (Exception ex)
        {
            WriteLog("taskkill 执行失败：" + ex.Message);
        }
    }

    private string ResolvePowerShell()
    {
        string powershell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe");
        if (File.Exists(powershell))
        {
            return powershell;
        }
        return "powershell.exe";
    }

    private string BuildBackgroundStartCommand(string resultFile)
    {
        var script = new StringBuilder();
        script.Append("$ErrorActionPreference='Stop'; ");
        script.Append("$env:PYTHONIOENCODING='utf-8'; ");
        script.Append("$resultPath=").Append(SingleQuote(resultFile)).Append("; ");
        script.Append("function WriteResult([string]$Message) { Set-Content -LiteralPath $resultPath -Value $Message -Encoding UTF8 } ");
        script.Append("Set-Location -LiteralPath ").Append(SingleQuote(clientDir)).Append("; ");
        script.Append("function FailEnv([string]$Message) { WriteResult ('ENV_MISSING: ' + $Message); exit 20 } ");
        script.Append("$python=$null; ");
        script.Append("$venvPython=Join-Path (Split-Path -Parent $clientDir) '.venv\\Scripts\\python.exe'; ");
        script.Append("if (Test-Path -LiteralPath $venvPython) { $python=@($venvPython) } ");
        script.Append("if (-not $python) { try { $cmd=Get-Command python -ErrorAction SilentlyContinue; if ($cmd) { $ver=& $cmd.Source --version 2>&1; if ($LASTEXITCODE -eq 0 -and ($ver -join ' ') -match 'Python\\s+3\\.') { $python=@($cmd.Source) } } } catch {} } ");
        script.Append("if (-not $python) { try { $cmd=Get-Command py -ErrorAction SilentlyContinue; if ($cmd) { $ver=& $cmd.Source -3 --version 2>&1; if ($LASTEXITCODE -eq 0 -and ($ver -join ' ') -match 'Python\\s+3\\.') { $python=@($cmd.Source,'-3') } } } catch {} } ");
        script.Append("if (-not $python) { try { $cmd=Get-Command conda -ErrorAction SilentlyContinue; if ($cmd) { $base=(& $cmd.Source info --base 2>$null).Trim(); if ($LASTEXITCODE -eq 0 -and $base) { $condaPython=Join-Path $base 'python.exe'; if (Test-Path -LiteralPath $condaPython) { $ver=& $condaPython --version 2>&1; if ($LASTEXITCODE -eq 0 -and ($ver -join ' ') -match 'Python\\s+3\\.') { $python=@($condaPython) } } } } } catch {} } ");
        script.Append("if (-not $python) { FailEnv '未找到 Python 3' } ");
        script.Append("$exe=$python[0]; $baseArgs=@(); if ($python.Count -gt 1) { $baseArgs=$python[1..($python.Count-1)] }; ");
        script.Append("$ws = & $exe @baseArgs -c 'import websocket' 2>&1; if ($LASTEXITCODE -ne 0) { FailEnv ('缺少 Python 依赖 websocket-client: ' + ($ws -join ' ')) } ");
        script.Append("$envPath=").Append(SingleQuote(configFile)).Append("; if (-not (Test-Path -LiteralPath $envPath)) { FailEnv '缺少 client\\.env 配置文件' } ");
        script.Append("$envText=Get-Content -LiteralPath $envPath -Raw -Encoding UTF8; ");
        script.Append("if ($envText -notmatch '(?m)^QQ_APP_ID\\s*=\\s*[^\\s#].+' -or $envText -match '(?m)^QQ_APP_ID\\s*=\\s*replace-') { FailEnv '缺少 QQ_APP_ID' } ");
        script.Append("if ($envText -notmatch '(?m)^QQ_APP_SECRET\\s*=\\s*[^\\s#].+' -or $envText -match '(?m)^QQ_APP_SECRET\\s*=\\s*replace-') { FailEnv '缺少 QQ_APP_SECRET' } ");
        script.Append("$codexCommand='codex'; $match=[regex]::Match($envText,'(?m)^CODEX_COMMAND\\s*=\\s*(.+)$'); if ($match.Success -and -not [string]::IsNullOrWhiteSpace($match.Groups[1].Value)) { $codexCommand=$match.Groups[1].Value.Trim().Trim('\"').Trim(\"'\") } ");
        script.Append("$codexOk=$false; if (Test-Path -LiteralPath $codexCommand) { $codexOk=$true } else { $cmd=Get-Command $codexCommand -ErrorAction SilentlyContinue; if ($cmd) { $codexOk=$true } } ");
        script.Append("if (-not $codexOk -and $codexCommand -eq 'codex') { if (Get-Command 'codex.cmd' -ErrorAction SilentlyContinue) { $codexOk=$true } elseif (Get-Command 'codex.exe' -ErrorAction SilentlyContinue) { $codexOk=$true } } ");
        script.Append("if (-not $codexOk) { FailEnv ('未找到 Codex CLI: ' + $codexCommand) } ");
        script.Append("Start-Process -FilePath $exe -ArgumentList ($baseArgs + @('-B',").Append(SingleQuote(backgroundScript)).Append(")) -WorkingDirectory ").Append(SingleQuote(clientDir)).Append(" -WindowStyle Hidden; ");
        script.Append("WriteResult 'STARTED'; ");
        script.Append("exit 0");

        return script.ToString();
    }

    private string ReadTextIfExists(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                return File.ReadAllText(path, Encoding.UTF8);
            }
        }
        catch (Exception ex)
        {
            WriteLog("读取启动结果失败：" + ex.Message);
        }
        return "";
    }

    private static void OpenPath(string path, string missingMessage)
    {
        if (!File.Exists(path) && !Directory.Exists(path))
        {
            MessageBox.Show(missingMessage, "Codex Remote Bridge", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return;
        }

        Process.Start(new ProcessStartInfo
        {
            FileName = path,
            UseShellExecute = true
        });
    }

    private void ExitTray()
    {
        WriteLog("托盘程序退出");
        autoStartTimer.Stop();
        timer.Stop();
        notifyIcon.Visible = false;
        notifyIcon.Dispose();
        runningIcon.Dispose();
        stoppedIcon.Dispose();
        Application.Exit();
    }

    private void WriteLog(string message)
    {
        try
        {
            Directory.CreateDirectory(dataDir);
            File.AppendAllText(logFile, "[" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "] tray-exe: " + message + Environment.NewLine, Encoding.UTF8);
        }
        catch
        {
        }
    }

    private static Icon CreateStatusIcon(bool running)
    {
        using (var bitmap = new Bitmap(16, 16))
        using (Graphics graphics = Graphics.FromImage(bitmap))
        using (var brush = new SolidBrush(running ? Color.FromArgb(37, 170, 84) : Color.FromArgb(190, 60, 50)))
        using (var pen = new Pen(Color.White, 1))
        {
            graphics.Clear(Color.Transparent);
            graphics.FillEllipse(brush, 2, 2, 12, 12);
            graphics.DrawEllipse(pen, 2, 2, 12, 12);
            IntPtr handle = bitmap.GetHicon();
            return Icon.FromHandle(handle);
        }
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static string SingleQuote(string value)
    {
        return "'" + value.Replace("'", "''") + "'";
    }

    private sealed class BridgeProcess
    {
        public int ProcessId;
        public string CommandLine;
        public bool IsSupervisor;
    }

}
