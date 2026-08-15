param([Parameter(Mandatory=$true)][string]$ManagerKey)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if (-not $Root.EndsWith("\")) { $RootSlash = $Root + "\" } else { $RootSlash = $Root }
$EnvPath = Join-Path $Root ".env.TPilot"
$Py = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { throw "Python venv not found: $Py" }
$Reg = Join-Path $Root "manager_registry.py"
$ManagerKey = ($ManagerKey -replace '[^a-zA-Z0-9_-]', '').ToLower()
if (-not $ManagerKey) { throw "Manager key is empty" }

# --- TPILOT M2.12A STAGE1: honest start verification via start_status.json ---
# Polls runtime/managers/{key}/start_status.json instead of a fixed 3-second sleep.
# Prints STARTED only when phase becomes connected or running.
# TPILOT RESTART ISOLATION 20260809: exits 3 (manager-scoped, isolatable by the
# caller) or 1 (global/shared, must abort the whole startup) instead of throwing --
# see Resolve-StartFailureExit below for the exact rule.

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$StatusDir = Join-Path $Root "runtime\managers\$ManagerKey"
New-Item -ItemType Directory -Force -Path $StatusDir | Out-Null
$StatusFile = Join-Path $StatusDir "start_status.json"
$LaunchLog = Join-Path $LogDir ("manager_" + $ManagerKey + ".launcher.log")
$LaunchErr = Join-Path $LogDir ("manager_" + $ManagerKey + ".launcher.err.log")
$LaunchTs = (Get-Date -Format "yyyy-MM-dd HH:mm:ss UTC")

# Write launch timestamp header to launcher logs so each launch is clearly delimited
$Header = "=== LAUNCH $ManagerKey at $LaunchTs ==="
try { Add-Content -Path $LaunchLog -Value $Header -Encoding UTF8 } catch {}
try { Add-Content -Path $LaunchErr -Value $Header -Encoding UTF8 } catch {}

# Delete stale start_status.json from a previous run so we start clean
try { if (Test-Path $StatusFile) { Remove-Item $StatusFile -Force } } catch {}

# Stop any existing matching process
$pattern = "--manager\s+" + [regex]::Escape($ManagerKey) + "(\s|$)"
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($RootSlash) -and $_.CommandLine -match $pattern } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 1

# TPILOT F1 SAFETY 20260810 (C-1a): real UTC anchor for the freshness check in
# Resolve-StartFailureExit below. Deliberately NOT $LaunchTs (line ~27): that one
# is (Get-Date -Format "... UTC") -- LOCAL time with the literal text "UTC" glued
# on, fine for a log header but off by the local offset (Kyiv +3) if compared
# against start_status.json's genuinely-UTC updated_at.
$launchUtc = [DateTime]::UtcNow

# Start the manager process
$p = Start-Process -FilePath $Py -ArgumentList @((Join-Path $Root "main.py"), "--env", $EnvPath, "--manager", $ManagerKey) -WorkingDirectory $Root -WindowStyle Hidden -PassThru -RedirectStandardOutput $LaunchLog -RedirectStandardError $LaunchErr

# TPILOT RESTART ISOLATION 20260809: decide whether a start failure is safe to
# isolate (exit 3 -- caller skips this manager and continues starting the rest) or
# must be treated as a shared/global failure (exit 1 -- caller aborts the whole
# startup). The decision is based SOLELY on whether start_status.json exists, not on
# whether its reason_class is recognized: the file is only ever written from inside
# main() (main.py:41296, "starting" phase) BEFORE asyncio.run(main()) -- i.e. its mere
# presence already proves the shared runtime (imports, .env, venv) loaded successfully
# for this process. A failure recorded after that point can only be scoped to this one
# manager, known reason_class or not. classify-start-failure (manager_registry.py) is
# used only to label the log line (manager_scoped vs unknown) for operator visibility
# -- it never overrides the exit code.
function Resolve-StartFailureExit {
    param([string]$Detail)
    $result = [ordered]@{ ExitCode = 1; Detail = $Detail; Classification = "global" }

    # (1) file must exist at all
    if (-not (Test-Path $StatusFile)) {
        $result.Detail = "$Detail -- no start_status.json (shared/global failure)"
        return $result
    }

    # (2) it must actually parse. A truncated/garbage/empty file proves NOTHING:
    # a process that died mid-write is exactly the shape a shared regression
    # takes, so this must fail closed instead of isolating one manager.
    $ss = $null
    try { $ss = Get-Content $StatusFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $ss = $null }

    $evidenceOk = $true
    $why = ""
    if ($null -eq $ss) {
        $evidenceOk = $false; $why = "status file unreadable or invalid JSON"
    }
    # (3) phase must be one main.py actually writes -- this is the proof that the
    # shared startup boundary was crossed (main.py writes 'starting' from __main__
    # AFTER the shared code, .env and venv all loaded successfully).
    elseif (@("starting","connected","running","exited") -notcontains [string]$ss.phase) {
        $evidenceOk = $false; $why = "status file has no valid phase ('$([string]$ss.phase)')"
    }
    # (4) it must belong to THIS manager (both sides are normalized lowercase:
    # main.py writes str(key).strip().lower(), line 10 above does the same).
    elseif (([string]$ss.manager_key) -ne $ManagerKey) {
        $evidenceOk = $false; $why = "status file belongs to '$([string]$ss.manager_key)', not '$ManagerKey'"
    }
    else {
        # (5)+(6) it must belong to THIS launch attempt. The stale-file delete at
        # the top of this script is best-effort (try{}catch{}), so a leftover file
        # from a previous run can survive; without this check it would let an
        # old, unrelated failure isolate a manager that never even started now.
        # 5s slack covers updated_at's second-level truncation; a genuinely stale
        # file is minutes/hours old, orders of magnitude beyond that.
        # ConvertFrom-Json already coerces an ISO-8601 value into a [DateTime]
        # (Kind=Local, offset applied). Round-tripping that back through [string]
        # would render it in the CURRENT CULTURE ("08/10/2026 09:41:34") and
        # re-parsing would read it as 8 October -- a stale file could then look
        # two months in the FUTURE and sail through this check. So: use the
        # DateTime as-is when we already have one, and parse with the invariant
        # culture (offset-aware) only when the value is still a raw string.
        $rawUpd = $ss.updated_at
        $upd = $null
        try {
            if ($rawUpd -is [DateTime]) {
                $upd = ([DateTime]$rawUpd).ToUniversalTime()
            } else {
                $upd = ([DateTimeOffset]::Parse([string]$rawUpd, [System.Globalization.CultureInfo]::InvariantCulture)).UtcDateTime
            }
        } catch { $upd = $null }
        if ($null -eq $upd) {
            $evidenceOk = $false; $why = "status file has unparsable updated_at ('$([string]$ss.updated_at)')"
        } elseif ($upd -lt $launchUtc.AddSeconds(-5)) {
            $evidenceOk = $false; $why = "status file is stale (updated_at=$([string]$ss.updated_at) predates this launch)"
        }
    }

    if (-not $evidenceOk) {
        $result.Detail = "$Detail -- $why (treated as shared/global, not isolatable)"
        return $result
    }

    # Only here is manager-scoped isolation legitimate. reason_class still does
    # NOT decide the exit code -- it only labels the log line.
    $rc = [string]$ss.reason_class
    $err = [string]$ss.error
    $result.Detail = if ($rc) { "reason_class=$rc error=$err" } else { "phase=$([string]$ss.phase)" }
    $cls = "unknown"
    try { $cls = (& $Py $Reg classify-start-failure $rc 2>$null | Select-Object -First 1) } catch { $cls = "unknown" }
    if (-not $cls) { $cls = "unknown" }
    $result.Classification = $cls.Trim()
    $result.ExitCode = 3
    return $result
}

# TPILOT F1 SAFETY 20260810 (C-1a): one place that turns a Resolve-StartFailureExit
# result into a log line, so the wording can never contradict the exit code again
# (exit 1 used to be printed as "SKIPPED MANAGER" even though nothing was skipped
# -- the whole startup was about to abort).
function Write-StartFailureLine {
    param($Result)
    if ($Result.ExitCode -eq 3) {
        Write-Host "SKIPPED MANAGER $ManagerKey ($($Result.Classification)): $($Result.Detail)"
    } else {
        Write-Host "MANAGER $ManagerKey FAILED GLOBALLY (aborting startup): $($Result.Detail)"
    }
}

# Poll start_status.json for up to 60 seconds waiting for connected/running or exited
$PollMaxSec = 60
$PollIntervalMs = 1000
$elapsed = 0

while ($elapsed -lt $PollMaxSec) {
    Start-Sleep -Milliseconds $PollIntervalMs
    $elapsed += 1

    # Check if process is still alive
    $still_running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($RootSlash) -and $_.CommandLine -match $pattern }
    if (-not $still_running) {
        # Process exited — classify and exit accordingly (see Resolve-StartFailureExit above).
        $r = Resolve-StartFailureExit -Detail "process exited during startup"
        Write-StartFailureLine -Result $r
        exit $r.ExitCode
    }

    # Try to read start_status.json
    if (Test-Path $StatusFile) {
        try {
            $ss = Get-Content $StatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $phase = $ss.phase
            if ($phase -eq "connected" -or $phase -eq "running") {
                Write-Host "STARTED MANAGER $ManagerKey PID $($p.Id) phase=$phase elapsed=${elapsed}s"
                exit 0
            }
            if ($phase -eq "exited") {
                # The file parsed here, but Resolve-StartFailureExit re-validates
                # ownership and freshness too -- it can still legitimately return 1.
                $r = Resolve-StartFailureExit -Detail "exited during startup"
                Write-StartFailureLine -Result $r
                exit $r.ExitCode
            }
            # phase=starting: keep polling
        } catch {
            # JSON parse error -> keep polling (file may be mid-write)
        }
    }
}

# Poll window expired — never reached connected/running within PollMaxSec.
# Success (exit 0) is allowed ONLY when phase connected/running was confirmed above.
$still_alive = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains($RootSlash) -and $_.CommandLine -match $pattern })
if ($still_alive.Count -gt 0) {
    # TPILOT F1 SAFETY 20260810 (H9): stop EVERY process matching $pattern, not
    # just $p.Id. Start-Process returns the venv launcher PID, while the actual
    # manager runtime is its AppData child with the SAME command line (CLAUDE.md
    # §10, "duplicate Python process pairs are normal"). Killing only $p.Id left
    # a live Telethon session behind while the caller printed SKIPPED MANAGER --
    # directly contradicting the F1 contract. Matches production semantics.
    $still_alive | ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
}
$r = Resolve-StartFailureExit -Detail "did not reach connected/running within ${PollMaxSec}s"
Write-StartFailureLine -Result $r
exit $r.ExitCode
