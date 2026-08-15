$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root "venv\Scripts\python.exe"
$script = Join-Path $root "manager_bot.py"
$logDir = Join-Path $root "logs"

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

$outLog = Join-Path $logDir "manager_bot.out.log"
$errLog = Join-Path $logDir "manager_bot.err.log"

Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like "*$root*manager_bot.py*"
} | ForEach-Object {
    try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    } catch {}
}

$p = Start-Process `
    -FilePath $py `
    -ArgumentList @($script) `
    -WorkingDirectory $root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru

Write-Host "START MANAGER BOT OK PID $($p.Id)"
