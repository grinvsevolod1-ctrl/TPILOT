$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (-not $Root.EndsWith("\")) { $RootSlash = $Root + "\" } else { $RootSlash = $Root }
$Py = Join-Path $Root "venv\Scripts\python.exe"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($RootSlash) -and $_.CommandLine -like "*soft_watchdog_pinger.py*" } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 1
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$p = Start-Process -FilePath $Py -ArgumentList @((Join-Path $Root "soft_watchdog_pinger.py")) -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir "soft_watchdog.log") -RedirectStandardError (Join-Path $LogDir "soft_watchdog.err.log")
Start-Sleep -Seconds 1
if ($p.HasExited) { throw "Soft Watchdog exited immediately" }
Write-Host "STARTED SOFT WATCHDOG PID $($p.Id)"
