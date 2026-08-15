$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (-not $Root.EndsWith("\")) { $RootSlash = $Root + "\" } else { $RootSlash = $Root }
$EnvPath = Join-Path $Root ".env.TPilot"
$Py = Join-Path $Root "venv\Scripts\python.exe"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($RootSlash) -and $_.CommandLine -like "*panel_bot.py*" } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 1
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$p = Start-Process -FilePath $Py -ArgumentList @((Join-Path $Root "panel_bot.py"), "--env", $EnvPath) -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir "panel_bot.log") -RedirectStandardError (Join-Path $LogDir "panel_bot.err.log")
Start-Sleep -Seconds 1
if ($p.HasExited) { throw "PanelBot exited immediately" }
Write-Host "STARTED PANEL BOT PID $($p.Id)"
