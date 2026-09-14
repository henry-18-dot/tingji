[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$dataRoot = if ($env:TINGJI_DATA_DIR) { [IO.Path]::GetFullPath($env:TINGJI_DATA_DIR) } else { Join-Path $projectRoot 'data' }
[void][IO.Directory]::CreateDirectory($dataRoot)
$launcherLog = Join-Path $dataRoot 'launcher.log'
$serverOutLog = Join-Path $dataRoot 'server.stdout.log'
$serverErrorLog = Join-Path $dataRoot 'server.stderr.log'
$baseUrl = "http://127.0.0.1:$Port"
$mutex = $null
$hasMutex = $false

function Write-LauncherLog([string]$Message) {
    $line = '{0} {1}{2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message, [Environment]::NewLine
    [IO.File]::AppendAllText($launcherLog, $line, [Text.UTF8Encoding]::new($false))
}

function Get-TingjiHealth {
    try {
        $response = Invoke-RestMethod -Uri "$baseUrl/api/health" -Method Get -TimeoutSec 1 -Proxy $null
        return ($response.ok -eq $true -and $response.app -eq 'tingji')
    } catch {
        return $false
    }
}

function Test-LocalPort {
    $client = [Net.Sockets.TcpClient]::new()
    try {
        $connection = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $connection.Wait(600)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Show-LaunchError([string]$Message) {
    if ($NoBrowser) {
        [Console]::Error.WriteLine($Message)
        return
    }
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($Message, '听记 · 启动失败', 'OK', 'Error')
    } catch {
        [Console]::Error.WriteLine($Message)
    }
}

try {
    $mutex = [Threading.Mutex]::new($false, "Local\TingjiLauncher-$Port")
    try { $hasMutex = $mutex.WaitOne(12000) } catch [Threading.AbandonedMutexException] { $hasMutex = $true }
    if (-not $hasMutex) { throw '听记正在启动。请稍等片刻后重新打开。' }

    if (Get-TingjiHealth) {
        Write-LauncherLog "Reusing healthy Tingji service on port $Port."
    } else {
        if (Test-LocalPort) {
            throw "本机端口 $Port 正被其他程序占用。请关闭占用程序后重试，听记不会停止其他程序。"
        }
        $serverPath = Join-Path $projectRoot 'server.py'
        if (-not [IO.File]::Exists($serverPath)) { throw "找不到听记服务文件：$serverPath。请保持启动文件和 server.py 位于同一文件夹。" }

        $pythonCommand = Get-Command pythonw.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $pythonCommand) {
            $pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        }
        if (-not $pythonCommand -or $pythonCommand.Source -match '\\WindowsApps\\') {
            throw '未找到可用的 Python。请安装 Python 3.11 或更高版本，并在安装时勾选 Add Python to PATH，然后重新打开听记。'
        }

        $serverArguments = '"{0}" --port {1}' -f $serverPath, $Port
        Write-LauncherLog "Starting Tingji on port $Port."
        $serverProcess = Start-Process -FilePath $pythonCommand.Source -ArgumentList $serverArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -RedirectStandardOutput $serverOutLog -RedirectStandardError $serverErrorLog -PassThru
        [IO.File]::WriteAllText((Join-Path $dataRoot 'server.pid'), [string]$serverProcess.Id, [Text.UTF8Encoding]::new($false))

        $timer = [Diagnostics.Stopwatch]::StartNew()
        $healthy = $false
        do {
            if (Get-TingjiHealth) { $healthy = $true; break }
            $serverProcess.Refresh()
            if ($serverProcess.HasExited) { break }
            Start-Sleep -Milliseconds 200
        } while ($timer.Elapsed.TotalSeconds -lt 10)
        if (-not $healthy) {
            throw "听记服务未能在 10 秒内启动。请查看日志：$serverErrorLog"
        }
        Write-LauncherLog "Tingji is ready; process ID $($serverProcess.Id)."
    }

    if (-not $NoBrowser) { Start-Process -FilePath $baseUrl }
    Write-Output "听记已就绪：$baseUrl"
} catch {
    $errorMessage = $_.Exception.Message
    try { Write-LauncherLog "ERROR: $errorMessage" } catch {}
    Show-LaunchError "$errorMessage`r`n`r`n启动日志：$launcherLog"
    exit 1
} finally {
    if ($hasMutex -and $mutex) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
