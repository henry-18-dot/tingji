[CmdletBinding()]
param([switch]$NoApp)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

try {
    & (Join-Path $projectRoot 'launcher.ps1') -NoBrowser
    if (-not $?) { throw '听记后台启动失败，请查看 data/launcher.log。' }
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 5 -Proxy $null
    if ($health.ok -ne $true -or $health.app -ne 'tingji') {
        throw '听记后台尚未就绪，请查看 data/launcher.log。'
    }
    if ($NoApp) { return }
    $appConfig = Join-Path $projectRoot 'config/desktop-app.json'
    if (Test-Path -LiteralPath $appConfig) {
        $app = Get-Content -LiteralPath $appConfig -Raw -Encoding UTF8 | ConvertFrom-Json
        Start-Process -FilePath $app.Target -ArgumentList $app.Arguments -WorkingDirectory $app.WorkingDirectory
    } else {
        Start-Process -FilePath 'http://127.0.0.1:8765/'
    }
} catch {
    if ($NoApp) { throw }
    Add-Type -AssemblyName System.Windows.Forms
    [void][System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '听记 · 启动失败', 'OK', 'Error')
    exit 1
}
