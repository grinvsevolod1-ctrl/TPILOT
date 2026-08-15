$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (-not $Root.EndsWith("\")) { $RootSlash = $Root + "\" } else { $RootSlash = $Root }
$EnvPath = Join-Path $Root ".env.TPilot"
$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Python venv not found: $Py" }
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Write-Host "START EVERYTHING ROOT=$RootSlash"
& (Join-Path $Root "stop_everything.ps1")
Start-Sleep -Seconds 2
$Reg = Join-Path $Root "manager_registry.py"
$keysOut = & $Py $Reg --env $EnvPath list-active-keys
if ($LASTEXITCODE -ne 0) { throw "manager_registry list-active-keys failed" }
$keys = @($keysOut | Where-Object { $_ -and $_.Trim() })
if ($keys.Count -eq 0) {
    # TPILOT RESTART ISOLATION 20260809: zero active managers is not a startup
    # failure -- core (controller, PanelBot, watchdog, etc.) must still come up so
    # an admin can add/re-enable a manager through it. Was: throw (fatal).
    Write-Warning "No active managers returned by manager_registry.py -- continuing without managers"
}
$p = Start-Process -FilePath $Py -ArgumentList @((Join-Path $Root "main.py"), "--env", $EnvPath) -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $LogDir "tpilot.log") -RedirectStandardError (Join-Path $LogDir "tpilot.err.log")
Start-Sleep -Seconds 1
if ($p.HasExited) { throw "TPilot controller exited immediately" }
Write-Host "STARTED TPILOT PID $($p.Id)"

# TPILOT RESTART ISOLATION 20260809: one manager's start_status.json-proven,
# manager-scoped failure (exit 3 from start_manager.ps1) must not abort the whole
# startup -- soft watchdog / health server / PanelBot / PartnerBot still need to
# come up, and every other manager still needs a chance to start. Exit 1 (no
# start_status.json -- shared/global failure, e.g. broken shared code, missing
# venv) still aborts immediately, same as before. See start_manager.ps1's
# Resolve-StartFailureExit for the exact rule, and the plan's PLAN FREEZE section
# F3 for why "no status file" is the correct global/manager-scoped discriminator.
$skipped = @()
foreach ($k in $keys) {
    & (Join-Path $Root "start_manager.ps1") -ManagerKey $k
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        continue
    } elseif ($code -eq 3) {
        $skipped += $k
        continue
    } else {
        throw "Manager $k failed with a global/shared error (exit $code) -- aborting startup. See logs\manager_$k.launcher.err.log"
    }
}
# TPILOT RESTART ISOLATION 20260809: if EVERY manager was isolated as
# manager-scoped, that is not N independent per-manager problems -- it is much more
# likely a shared regression that start_manager.ps1 could not distinguish from a
# manager-scoped one (e.g. a bad shared config change that fails auth for everyone).
# Treat it as a global failure rather than silently reporting SUCCESS with zero
# managers actually running.
if ($keys.Count -gt 0 -and $skipped.Count -eq $keys.Count) {
    throw "All $($keys.Count) manager(s) failed to start (manager-scoped exit code from each) -- treating as a shared regression, aborting startup. Skipped: $($skipped -join ', ')"
}

& (Join-Path $Root "start_manager_bot.ps1")
& (Join-Path $Root "start_soft_watchdog.ps1")
& (Join-Path $Root "start_health_server.ps1")
& (Join-Path $Root "start_panel_bot.ps1")
if ($skipped.Count -gt 0) {
    Write-Host "START EVERYTHING OK (skipped: $($skipped -join ', '))"
} else {
    Write-Host "START EVERYTHING OK"
}
