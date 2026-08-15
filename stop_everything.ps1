$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (-not $Root.EndsWith("\")) { $RootSlash = $Root + "\" } else { $RootSlash = $Root }
Write-Host "STOP EVERYTHING ROOT=$RootSlash"
# TPILOT RESTART ISOLATION 20260809: manager_bot.py added -- it previously had no
# canonical stop owner (start_manager_bot.ps1/stop_manager_bot.ps1 were never called
# by this chain), so a plain "stop_everything" left it running. Its own launcher
# (start_manager_bot.ps1) already kills any pre-existing manager_bot.py process
# before starting a new one, so this addition cannot create a double-spawn; it only
# makes stop_everything's name match what it actually stops.
$targets = @("main.py", "panel_bot.py", "partner_stat_bot.py", "soft_watchdog_pinger.py", "manager_bot.py")
$procs = Get-CimInstance Win32_Process | Where-Object {
    $cmd = $_.CommandLine
    if (-not $cmd) { $false }
    elseif (-not $cmd.Contains($RootSlash)) { $false }
    else {
        $hit = $false
        foreach ($t in $targets) { if ($cmd -like "*$t*") { $hit = $true } }
        $hit
    }
}
foreach ($p in $procs) { Write-Host "KILL PID $($p.ProcessId) $($p.CommandLine)"; try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 1
Write-Host "STOP EVERYTHING OK"
