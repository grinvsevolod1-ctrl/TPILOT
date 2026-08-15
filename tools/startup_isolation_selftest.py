# -*- coding: utf-8 -*-
"""tools/startup_isolation_selftest.py -- offline selftest for TPILOT RESTART
ISOLATION (Ф1) + the C-1a status-file safety contract.

Covers the five Ф1 runtime files:
    manager_registry.py, start_manager.ps1, start_everything.ps1,
    stop_everything.ps1, soft_watchdog_pinger.py
plus the taxonomy inside main.py (_manager_recovery_classify).

No network, no Telegram, no provider, no spend, no DB writes outside a temp
directory, no Start-Process, no touching runtime\\ db\\ logs\\ sessions\\.

Three layers, deliberately NOT a grep suite:
  A. Real Python execution -- manager_registry's functions and its real CLI
     (subprocess) against a temp SQLite DB + temp .env; soft_watchdog_pinger's
     _check_soft AST-extracted and executed against stubbed process/registry
     readers (it would otherwise shell out to WMI).
  B. Real PowerShell execution -- Resolve-StartFailureExit is extracted from the
     LIVE start_manager.ps1 source and actually executed in pwsh/powershell with
     a temp $StatusFile, the REAL python + REAL manager_registry.py behind
     classify-start-failure, and a controllable $launchUtc. The manager loop is
     extracted from the LIVE start_everything.ps1 and executed against a stub
     start_manager.ps1 that only exits with a chosen code (never Start-Process).
  C. Structural anchors -- only for what cannot be executed safely
     (Start-Process / Stop-Process / Get-CimInstance ownership invariants).

    python tools\\startup_isolation_selftest.py
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import manager_registry  # noqa: E402

FAILURES: list[str] = []
BLOCKED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> bool:
    if condition:
        print(f"[OK]   {label}")
        return True
    print(f"[FAIL] {label}  {detail}")
    FAILURES.append(label)
    return False


def blocked(label: str, detail: str = "") -> None:
    print(f"[BLOCKED] {label}  {detail}")
    BLOCKED.append(label)


# --------------------------------------------------------------------------
# temp sandbox
# --------------------------------------------------------------------------
TMP = Path(tempfile.mkdtemp(prefix="tpilot_startup_iso_"))


def _guard_temp(p: Path) -> None:
    rp = os.path.realpath(str(p))
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"path must live under tempdir, got {rp}"
    assert "ALM_TPilot" not in rp or rp.startswith(tmp), rp


_guard_temp(TMP)


def _read_source(path: Path) -> str:
    # utf-8-sig: start_manager_bot.ps1 / stop_manager_bot.ps1 carry a BOM.
    return path.read_text(encoding="utf-8-sig")


SRC_START_MANAGER = _read_source(BASE_DIR / "start_manager.ps1")
SRC_START_EVERYTHING = _read_source(BASE_DIR / "start_everything.ps1")
SRC_STOP_EVERYTHING = _read_source(BASE_DIR / "stop_everything.ps1")
SRC_START_MANAGER_BOT = _read_source(BASE_DIR / "start_manager_bot.ps1")
SRC_WATCHDOG = _read_source(BASE_DIR / "soft_watchdog_pinger.py")
# utf-8-sig: main.py carries a UTF-8 BOM, which ast.parse rejects outright.
SRC_MAIN = _read_source(BASE_DIR / "main.py")

PYEXE = sys.executable
REG_PY = str(BASE_DIR / "manager_registry.py")


# --------------------------------------------------------------------------
# PowerShell host
# --------------------------------------------------------------------------
def _find_ps_host() -> str:
    for exe in ("pwsh", "powershell"):
        found = shutil.which(exe)
        if found:
            return found
    return ""


PS_HOST = _find_ps_host()


def _run_ps(script_text: str, name: str) -> tuple[int, str, str]:
    p = TMP / name
    p.write_text(script_text, encoding="utf-8")
    proc = subprocess.run(
        [PS_HOST, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(p)],
        capture_output=True, text=True, timeout=180,
    )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _ps_lit(s: str) -> str:
    """PowerShell single-quoted literal."""
    return "'" + str(s).replace("'", "''") + "'"


# --------------------------------------------------------------------------
# PowerShell source extraction (real source, not re-implemented)
# --------------------------------------------------------------------------
def extract_ps_function(src: str, name: str) -> str:
    """Extract `function <name> { ... }` by brace matching."""
    m = re.search(r"^function\s+" + re.escape(name) + r"\s*\{", src, flags=re.M)
    if not m:
        raise RuntimeError(f"function {name} not found")
    start = m.start()
    i = src.index("{", m.start())
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise RuntimeError(f"unbalanced braces in {name}")


def strip_shell_comments(src: str, suffix: str) -> str:
    """Drop comment text so 'is this file an invocation site?' checks cannot be
    fooled by prose that merely names another script."""
    out = []
    for line in src.splitlines():
        if suffix == ".bat":
            if re.match(r"^\s*(rem\b|::)", line, flags=re.I):
                continue
            out.append(line)
            continue
        # PowerShell: cut at the first '#' that is not inside a quoted string.
        in_s = in_d = False
        cut = len(line)
        for i, ch in enumerate(line):
            if ch == "'" and not in_d:
                in_s = not in_s
            elif ch == '"' and not in_s:
                in_d = not in_d
            elif ch == "#" and not in_s and not in_d:
                cut = i
                break
        out.append(line[:cut])
    return "\n".join(out)


def extract_lines(src: str, start_pat: str, end_pat: str | None) -> str:
    """Extract from the line matching start_pat up to (excluding) end_pat."""
    lines = src.splitlines()
    si = next((i for i, l in enumerate(lines) if re.search(start_pat, l)), None)
    if si is None:
        raise RuntimeError(f"start anchor not found: {start_pat}")
    if end_pat is None:
        return "\n".join(lines[si:])
    ei = next((i for i, l in enumerate(lines) if i > si and re.search(end_pat, l)), None)
    if ei is None:
        raise RuntimeError(f"end anchor not found: {end_pat}")
    return "\n".join(lines[si:ei])


RESOLVE_FN = extract_ps_function(SRC_START_MANAGER, "Resolve-StartFailureExit")

# The manager-start loop and the final summary block of start_everything.ps1,
# taken verbatim from the live source. The four launcher invocations between
# them (start_manager_bot / soft_watchdog / health_server / panel_bot) are
# deliberately NOT included -- this selftest never starts a real process.
LOOP_REGION = extract_lines(SRC_START_EVERYTHING, r"^\$skipped\s*=\s*@\(\)", r"start_manager_bot\.ps1")
SUMMARY_REGION = extract_lines(SRC_START_EVERYTHING, r"^if \(\$skipped\.Count -gt 0\)", None)

# TPILOT F1 SAFETY 20260810 (H9): the timeout-cleanup hunk at the tail of
# start_manager.ps1 -- runs when the poll window expires without the manager
# ever reaching connected/running. Extracted verbatim (not re-implemented) so
# the test exercises the real cleanup logic, not a description of it.
TIMEOUT_CLEANUP_REGION = extract_lines(
    SRC_START_MANAGER,
    r"^\$still_alive = @\(Get-CimInstance",
    r'^\$r = Resolve-StartFailureExit -Detail "did not reach',
)

# --- M3a reference: the pre-C-1a defective implementation, frozen verbatim ---
# `$result.ExitCode = 3` sits inside `if (Test-Path $StatusFile)` but OUTSIDE
# the try/catch, so an unreadable/invalid/stale/foreign status file still
# isolated the manager instead of aborting the startup.
PREFIX_ORIGINAL_RESOLVE = '''function Resolve-StartFailureExit {
    param([string]$Detail)
    $result = [ordered]@{ ExitCode = 1; Detail = $Detail; Classification = "global" }
    if (Test-Path $StatusFile) {
        $rc = ""
        try {
            $ss = Get-Content $StatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $rc = [string]$ss.reason_class
            $err = [string]$ss.error
            $result.Detail = if ($rc) { "reason_class=$rc error=$err" } else { "phase=$($ss.phase)" }
        } catch {
            $result.Detail = "(status file unreadable)"
        }
        $cls = "unknown"
        try { $cls = (& $Py $Reg classify-start-failure $rc 2>$null | Select-Object -First 1) } catch { $cls = "unknown" }
        if (-not $cls) { $cls = "unknown" }
        $result.Classification = $cls.Trim()
        $result.ExitCode = 3
    }
    return $result
}'''


# --------------------------------------------------------------------------
# Layer B harness 1: Resolve-StartFailureExit
# --------------------------------------------------------------------------
LAUNCH_UTC = datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def run_resolve(status_payload, *, manager_key: str = "alpha",
                launch_utc: datetime | None = None, fn_text: str | None = None,
                raw_text: str | None = None, tag: str = "case") -> dict:
    """Execute the REAL Resolve-StartFailureExit against a temp status file.

    status_payload: dict -> written as JSON; str -> written verbatim (invalid
    JSON cases); None -> file is not created at all.
    """
    case_dir = TMP / f"resolve_{tag}"
    case_dir.mkdir(parents=True, exist_ok=True)
    status_file = case_dir / "start_status.json"
    if status_file.exists():
        status_file.unlink()
    if raw_text is not None:
        status_file.write_text(raw_text, encoding="utf-8")
    elif status_payload is not None:
        status_file.write_text(json.dumps(status_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lu = launch_utc or LAUNCH_UTC
    script = "\n".join([
        '$ErrorActionPreference = "Stop"',
        f"$StatusFile = {_ps_lit(str(status_file))}",
        f"$Py = {_ps_lit(PYEXE)}",
        f"$Reg = {_ps_lit(REG_PY)}",
        f"$ManagerKey = {_ps_lit(manager_key)}",
        f"$launchUtc = ([DateTime]::Parse({_ps_lit(_iso(lu))})).ToUniversalTime()",
        "",
        fn_text if fn_text is not None else RESOLVE_FN,
        "",
        '$r = Resolve-StartFailureExit -Detail "harness"',
        'Write-Output ("EXITCODE=" + $r.ExitCode)',
        'Write-Output ("CLASSIFICATION=" + $r.Classification)',
        'Write-Output ("DETAIL=" + $r.Detail)',
    ])
    rc, out, err = _run_ps(script, f"resolve_{tag}.ps1")
    res = {"rc": rc, "stdout": out, "stderr": err, "exitcode": None,
           "classification": "", "detail": ""}
    for line in out.splitlines():
        if line.startswith("EXITCODE="):
            try:
                res["exitcode"] = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("CLASSIFICATION="):
            res["classification"] = line.split("=", 1)[1].strip()
        elif line.startswith("DETAIL="):
            res["detail"] = line.split("=", 1)[1].strip()
    return res


def fresh_status(**over) -> dict:
    d = {
        "manager_key": "alpha",
        "phase": "exited",
        "updated_at": _iso(LAUNCH_UTC + timedelta(seconds=3)),
        "pid": 4242,
        "reason_class": "session_unauthorized",
        "error": "session is not authorized",
        "last_exit_reason_class": "session_unauthorized",
        "respawn_seq": 1,
    }
    d.update(over)
    return d


# --------------------------------------------------------------------------
# Layer B harness 2: the start_everything manager loop
# --------------------------------------------------------------------------
def run_loop(exitmap: dict, keys: list[str], *, loop_text: str | None = None,
             summary_text: str | None = None, tag: str = "loop") -> dict:
    """Execute the REAL start_everything.ps1 manager loop against a stub
    start_manager.ps1 that only records its key and exits with a chosen code
    (or throws). Never spawns a real process."""
    d = TMP / f"loop_{tag}"
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "exitmap.txt").write_text(
        "\n".join(f"{k}={v}" for k, v in exitmap.items()), encoding="utf-8")

    stub = "\n".join([
        'param([Parameter(Mandatory=$true)][string]$ManagerKey)',
        'Add-Content -Path (Join-Path $PSScriptRoot "started.txt") -Value $ManagerKey',
        '$code = 0',
        'foreach ($line in (Get-Content (Join-Path $PSScriptRoot "exitmap.txt"))) {',
        '    $parts = $line.Split("=")',
        '    if ($parts[0] -eq $ManagerKey) { $code = $parts[1] }',
        '}',
        'if ($code -eq "throw") { throw "stub: shared/global failure for $ManagerKey" }',
        'exit [int]$code',
    ])
    (d / "start_manager.ps1").write_text(stub, encoding="utf-8")

    keys_ps = ", ".join(_ps_lit(k) for k in keys)
    script = "\n".join([
        '$ErrorActionPreference = "Stop"',
        f"$Root = {_ps_lit(str(d))}",
        f"$keys = @({keys_ps})",
        "try {",
        (loop_text if loop_text is not None else LOOP_REGION),
        (summary_text if summary_text is not None else SUMMARY_REGION),
        '    Write-Output "RESULT=SUCCESS"',
        "} catch {",
        '    Write-Output ("RESULT=ABORT: " + $_.Exception.Message)',
        "}",
        'Write-Output ("SKIPPED=" + ($skipped -join ","))',
    ])
    rc, out, err = _run_ps(script, f"loop_{tag}.ps1")
    started_file = d / "started.txt"
    started = started_file.read_text(encoding="utf-8").split() if started_file.exists() else []
    result = ""
    skipped = ""
    for line in out.splitlines():
        if line.startswith("RESULT="):
            result = line.split("=", 1)[1].strip()
        elif line.startswith("SKIPPED="):
            skipped = line.split("=", 1)[1].strip()
    return {"rc": rc, "stdout": out, "stderr": err, "result": result,
            "skipped": [s for s in skipped.split(",") if s], "started": started,
            "printed_ok": "START EVERYTHING OK" in out}


# --------------------------------------------------------------------------
# Layer B harness 3: the start_manager.ps1 timeout-cleanup hunk (H9)
# --------------------------------------------------------------------------
def run_timeout_cleanup(matching_pids: list[int], *, region_text: str | None = None,
                        tag: str = "cleanup") -> dict:
    """Execute the REAL timeout-cleanup hunk from start_manager.ps1 (the block
    that runs after the poll window expires without connected/running) against
    a stubbed Get-CimInstance / Stop-Process -- never touches a real OS
    process. Get-CimInstance is shadowed (function definitions in the same
    script scope take precedence over cmdlets) to return one fake
    Win32_Process-shaped object per PID in `matching_pids`, each carrying a
    CommandLine that satisfies the hunk's own $RootSlash/$pattern filter, so
    the real filter logic still runs. Stop-Process is shadowed to append the
    PID it was called with to a log file. $p.Id is fixed to the first PID
    (mirrors Start-Process returning the venv launcher's PID). Returns the
    list of PIDs the hunk actually asked to stop."""
    case_dir = TMP / f"cleanup_{tag}"
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    stop_log = case_dir / "stopped.txt"

    fake_procs = ",\n".join(
        "        [PSCustomObject]@{ ProcessId = " + str(pid) + "; CommandLine = "
        + _ps_lit(f"C:\\FakeRoot\\venv\\Scripts\\python.exe C:\\FakeRoot\\main.py --env x --manager alpha")
        + " }"
        for pid in matching_pids
    )
    script = "\n".join([
        '$ErrorActionPreference = "Stop"',
        "$RootSlash = 'C:\\FakeRoot\\'",
        "$pattern = '--manager\\s+alpha(\\s|$)'",
        f"$StopLog = {_ps_lit(str(stop_log))}",
        "function Get-CimInstance {",
        "    param($ClassName)",
        "    @(",
        fake_procs,
        "    )",
        "}",
        "function Stop-Process {",
        "    param($Id, [switch]$Force, $ErrorAction)",
        "    Add-Content -Path $StopLog -Value $Id",
        "}",
        "$p = [PSCustomObject]@{ Id = " + str(matching_pids[0] if matching_pids else 0) + " }",
        "$PollMaxSec = 60",
        "",
        region_text if region_text is not None else TIMEOUT_CLEANUP_REGION,
        "",
        'Write-Output "DONE"',
    ])
    rc, out, err = _run_ps(script, f"cleanup_{tag}.ps1")
    stopped = [int(x) for x in stop_log.read_text(encoding="utf-8").split()] if stop_log.exists() else []
    return {"rc": rc, "stdout": out, "stderr": err, "stopped": stopped}


# --------------------------------------------------------------------------
# Layer A: temp registry DB + real CLI
# --------------------------------------------------------------------------
def build_registry_db() -> tuple[str, str]:
    d = TMP / "registry"
    d.mkdir(parents=True, exist_ok=True)
    db = d / "data.db"
    if db.exists():
        db.unlink()
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE managers (id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT, "
        "status TEXT, is_enabled INTEGER, manual_stopped INTEGER)"
    )
    rows = [
        ("alpha", "active", 1, 0),      # active
        ("beta", "active", 1, 0),       # active
        ("gamma", "disabled", 0, 0),    # disabled + is_enabled=0
        ("delta", "active", 0, 0),      # is_enabled=0 only
        ("epsilon", "active", 1, 1),    # manual_stopped
        ("zeta", "archived", 1, 0),     # archived
    ]
    con.executemany(
        "INSERT INTO managers(manager_key,status,is_enabled,manual_stopped) VALUES(?,?,?,?)", rows)
    con.commit()
    con.close()
    env = d / ".env.temp"
    env.write_text("DB_PATH=data.db\n", encoding="utf-8")
    return str(env), str(db)


# --------------------------------------------------------------------------
# Layer A: AST extraction of _check_soft
# --------------------------------------------------------------------------
def build_check_soft(source: str, base_dir: Path, active_keys: list[str],
                     manager_procs: dict[str, int]):
    """AST-extract _check_soft (+ the two pure helpers it uses) from the live
    watchdog source and execute it with stubbed process/registry readers."""
    tree = ast.parse(source)
    wanted = {"_check_soft", "_norm_path", "_collapse_venv_children"}
    picked = [n for n in tree.body
              if isinstance(n, (ast.FunctionDef,)) and n.name in wanted]
    missing = wanted - {n.name for n in picked}
    if missing:
        raise RuntimeError(f"missing functions in watchdog source: {missing}")
    mod = ast.Module(body=picked, type_ignores=[])
    code = ast.unparse(mod)

    # Fake processes: one controller, one PanelBot, plus one per running manager.
    procs = [{"pid": "1", "cmd": f"{base_dir}\\venv\\Scripts\\python.exe {base_dir}\\main.py --env x"},
             {"pid": "2", "cmd": f"{base_dir}\\venv\\Scripts\\python.exe {base_dir}\\panel_bot.py"}]
    pid = 10
    for k, n in manager_procs.items():
        for _ in range(n):
            procs.append({"pid": str(pid), "cmd": f"{base_dir}\\venv\\Scripts\\python.exe {base_dir}\\main.py --env x --manager {k}"})
            pid += 1

    ns = {
        "re": re, "json": json, "Path": Path, "sys": sys, "os": os,
        "Dict": dict, "List": list, "Tuple": tuple,
        "BASE_DIR": base_dir,
        "REQUIRE_TPILOT": True, "REQUIRE_MANAGERS": True, "REQUIRE_PANEL": True,
        # R1B/F-37 (2026-08-12, large reliability batch): _check_soft() now also
        # gates on these two -- this fixture's fake process list has no
        # manager_bot.py/partner_stat_bot.py entries, and this test's scope is
        # startup isolation (unrelated to F-37), so both are left un-required
        # here, matching this fixture's pre-existing intent/assertions.
        "REQUIRE_MANAGERBOT": False, "REQUIRE_PARTNERBOT": False,
        "WATCHDOG_NAME": "selftest",
        "manager_registry": manager_registry,
        "_get_python_processes": lambda: list(procs),
        "_list_active_manager_keys": lambda: list(active_keys),
        "_log": lambda *_a, **_k: None,
        "_now_iso": lambda: "1970-01-01T00:00:00+00:00",
    }
    exec(compile(code, "<watchdog_check_soft>", "exec"), ns)
    return ns["_check_soft"]


def write_status(base_dir: Path, key: str, payload: dict) -> None:
    d = base_dir / "runtime" / "managers" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "start_status.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------
# Layer A: taxonomy parity via AST on main.py
# --------------------------------------------------------------------------
def main_classifier_rc_values(source: str) -> set[str]:
    tree = ast.parse(source)
    fns = [n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef) and n.name == "_manager_recovery_classify"]
    if not fns:
        raise RuntimeError("_manager_recovery_classify not found in main.py")
    fn = fns[-1]  # last def wins -- project-wide override convention
    values: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
            left = node.left
            if isinstance(left, ast.Name) and left.id == "rc":
                comp = node.comparators[0]
                if isinstance(comp, (ast.Tuple, ast.List, ast.Set)):
                    for elt in comp.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            values.add(elt.value)
    return values


# ==========================================================================
# SCENARIOS
# ==========================================================================
print("=" * 78)
print("TPILOT STARTUP ISOLATION SELFTEST (F1 + C-1a status-file safety contract)")
print("=" * 78)
print(f"sandbox      : {TMP}")
print(f"powershell   : {PS_HOST or '<NOT FOUND>'}")
print(f"python       : {PYEXE}")
print()

if not PS_HOST:
    blocked("powershell host not available",
            "layer B (real Resolve-StartFailureExit / loop execution) cannot run")

# --------------------------------------------------------------------------
print("--- R1: known manager-scoped failure -> exit 3, next manager continues ---")
if PS_HOST:
    r1 = run_resolve(fresh_status(), tag="r1")
    check("R1a. valid+fresh status with reason_class=session_unauthorized -> ExitCode 3",
          r1["exitcode"] == 3, f"got {r1['exitcode']} detail={r1['detail']!r} err={r1['stderr'][:200]}")
    check("R1b. classification is manager_scoped (log label only)",
          r1["classification"] == "manager_scoped", f"got {r1['classification']!r}")

    l1 = run_loop({"alpha": 3, "beta": 0, "gamma": 0}, ["alpha", "beta", "gamma"], tag="r1")
    check("R1c. loop: exit 3 is isolated, startup does not abort",
          l1["result"] == "SUCCESS", f"got {l1['result']!r} err={l1['stderr'][:200]}")
    check("R1d. loop: every remaining manager was still attempted",
          l1["started"] == ["alpha", "beta", "gamma"], f"started={l1['started']}")
    check("R1e. loop: the failed manager is reported as skipped",
          l1["skipped"] == ["alpha"], f"skipped={l1['skipped']}")

# --------------------------------------------------------------------------
print("--- R2: global/shared failure -> exit 1, startup aborts ---")
if PS_HOST:
    r2 = run_resolve(None, tag="r2")
    check("R2a. no status file -> ExitCode 1 (global)",
          r2["exitcode"] == 1, f"got {r2['exitcode']} detail={r2['detail']!r}")

    l2 = run_loop({"alpha": 1, "beta": 0}, ["alpha", "beta"], tag="r2")
    check("R2b. loop: a non-3 exit code aborts the whole startup",
          l2["result"].startswith("ABORT"), f"got {l2['result']!r}")
    check("R2c. loop: START EVERYTHING OK is not printed on abort",
          not l2["printed_ok"], l2["stdout"][:200])

# --------------------------------------------------------------------------
print("--- R3: POSITIVE case -- valid + fresh + allowed phase -> exit 3 ---")
if PS_HOST:
    for phase in ("starting", "connected", "running", "exited"):
        r3 = run_resolve(fresh_status(phase=phase, reason_class="", error=""), tag=f"r3_{phase}")
        check(f"R3. valid, fresh, manager_key matches, phase='{phase}' -> ExitCode 3",
              r3["exitcode"] == 3, f"got {r3['exitcode']} detail={r3['detail']!r}")

# --------------------------------------------------------------------------
print("--- R4a: status file missing -> exit 1 ---")
if PS_HOST:
    r4a = run_resolve(None, tag="r4a")
    check("R4a. missing start_status.json -> ExitCode 1",
          r4a["exitcode"] == 1, f"got {r4a['exitcode']} detail={r4a['detail']!r}")

# --------------------------------------------------------------------------
print("--- R4b: status file unreadable / invalid JSON -> exit 1 (FAIL CLOSED) ---")
if PS_HOST:
    for tag, raw in (
        ("truncated", '{"manager_key": "alpha", "phase": "exi'),
        ("garbage", "\x00\x01 not json at all \xff"),
        ("empty", ""),
        ("jsonnull", "null"),
    ):
        r4b = run_resolve(None, raw_text=raw, tag=f"r4b_{tag}")
        check(f"R4b. invalid status file ({tag}) -> ExitCode 1, never isolated",
              r4b["exitcode"] == 1, f"got {r4b['exitcode']} detail={r4b['detail']!r}")

# --------------------------------------------------------------------------
print("--- R4c: status file valid but STALE -> exit 1 ---")
if PS_HOST:
    stale = fresh_status(updated_at=_iso(LAUNCH_UTC - timedelta(minutes=10)))
    r4c = run_resolve(stale, tag="r4c")
    check("R4c. status file from a previous launch (updated_at = launch-10min) -> ExitCode 1",
          r4c["exitcode"] == 1, f"got {r4c['exitcode']} detail={r4c['detail']!r}")

    stale2 = fresh_status(updated_at=_iso(LAUNCH_UTC - timedelta(days=3)))
    r4c2 = run_resolve(stale2, tag="r4c2")
    check("R4c. status file 3 days old -> ExitCode 1",
          r4c2["exitcode"] == 1, f"got {r4c2['exitcode']} detail={r4c2['detail']!r}")

# --------------------------------------------------------------------------
print("--- R4d: fresh but insufficient manager-local evidence -> exit 1 ---")
if PS_HOST:
    cases = [
        ("empty_phase", fresh_status(phase="")),
        ("bogus_phase", fresh_status(phase="bogus")),
        ("foreign_key", fresh_status(manager_key="someone_else")),
        ("bad_updated_at", fresh_status(updated_at="not-a-timestamp")),
        ("missing_updated_at", {k: v for k, v in fresh_status().items() if k != "updated_at"}),
        ("missing_phase", {k: v for k, v in fresh_status().items() if k != "phase"}),
    ]
    for tag, payload in cases:
        r4d = run_resolve(payload, tag=f"r4d_{tag}")
        check(f"R4d. insufficient evidence ({tag}) -> ExitCode 1",
              r4d["exitcode"] == 1, f"got {r4d['exitcode']} detail={r4d['detail']!r}")

# --------------------------------------------------------------------------
print("--- R5: disabled / is_enabled=0 / manual_stopped are not active keys ---")
env_path, db_path = build_registry_db()
rows = manager_registry.list_manager_rows_from_db_sync(db_path, only_enabled=True, only_active=True)
keys_fn = sorted(manager_registry.normalize_manager_key(r["manager_key"]) for r in rows)
check("R5a. list_manager_rows_from_db_sync(active) returns only alpha+beta",
      keys_fn == ["alpha", "beta"], f"got {keys_fn}")

proc = subprocess.run([PYEXE, REG_PY, "--env", env_path, "list-active-keys"],
                      capture_output=True, text=True, timeout=60)
cli_keys = sorted(l.strip() for l in (proc.stdout or "").splitlines() if l.strip())
check("R5b. REAL CLI list-active-keys returns only alpha+beta",
      proc.returncode == 0 and cli_keys == ["alpha", "beta"],
      f"rc={proc.returncode} out={cli_keys} err={(proc.stderr or '')[:200]}")
check("R5c. disabled/manual_stopped/archived keys never reach the launcher",
      all(k not in cli_keys for k in ("gamma", "delta", "epsilon", "zeta")), f"got {cli_keys}")

# --------------------------------------------------------------------------
print("--- R6: quarantined manager does not make the whole watchdog unhealthy ---")
wd_base = TMP / "wd"
wd_base.mkdir(parents=True, exist_ok=True)
write_status(wd_base, "alpha", {"manager_key": "alpha", "phase": "exited",
                                "last_exit_reason_class": "session_unauthorized",
                                "last_exit_error": "not authorized"})
write_status(wd_base, "beta", {"manager_key": "beta", "phase": "exited",
                               "last_exit_reason_class": "totally_unknown_class",
                               "last_exit_error": "???"})
check_soft = build_check_soft(SRC_WATCHDOG, wd_base, ["alpha", "beta", "gamma"], {"gamma": 1})
ok, reasons, meta = check_soft()
check("R6a. manager-scoped down manager lands in quarantined_managers",
      meta.get("quarantined_managers") == ["alpha"], f"got {meta.get('quarantined_managers')}")
check("R6b. manager-scoped down manager is NOT in reasons",
      not any("alpha" in r for r in reasons), f"reasons={reasons}")
check("R6c. unknown-class down manager IS in reasons (no fail-open)",
      any("beta" in r for r in reasons), f"reasons={reasons}")
check("R6d. down reason is still visible to operators",
      "alpha" in (meta.get("manager_down_reasons") or {}), f"got {meta.get('manager_down_reasons')}")

wd_base2 = TMP / "wd2"
wd_base2.mkdir(parents=True, exist_ok=True)
write_status(wd_base2, "alpha", {"manager_key": "alpha", "phase": "exited",
                                 "last_exit_reason_class": "session_unauthorized"})
check_soft2 = build_check_soft(SRC_WATCHDOG, wd_base2, ["alpha", "gamma"], {"gamma": 1})
ok2, reasons2, meta2 = check_soft2()
check("R6e. a lone quarantined manager keeps the external signal GREEN (ok=True)",
      ok2 is True and reasons2 == [], f"ok={ok2} reasons={reasons2}")

# --------------------------------------------------------------------------
print("--- R7: leaving quarantine, and no DB mutation anywhere in F1 ---")
wd_base3 = TMP / "wd3"
wd_base3.mkdir(parents=True, exist_ok=True)
write_status(wd_base3, "alpha", {"manager_key": "alpha", "phase": "starting",
                                 "last_exit_reason_class": "session_unauthorized"})
check_soft3 = build_check_soft(SRC_WATCHDOG, wd_base3, ["alpha"], {"alpha": 1})
ok3, reasons3, meta3 = check_soft3()
check("R7a. after a successful relogin (process back + phase='starting') the key "
      "is neither quarantined nor a reason",
      ok3 is True and meta3.get("quarantined_managers") == [] and reasons3 == [],
      f"ok={ok3} q={meta3.get('quarantined_managers')} reasons={reasons3}")

f1_files = ["manager_registry.py", "soft_watchdog_pinger.py", "start_manager.ps1",
            "start_everything.ps1", "stop_everything.ps1"]
write_hits: list[str] = []
for fname in f1_files:
    txt = _read_source(BASE_DIR / fname)
    for i, line in enumerate(txt.splitlines(), 1):
        low = line.lower()
        if "managers" in low and re.search(r"\b(update|insert\s+into|delete\s+from)\b", low):
            write_hits.append(f"{fname}:{i}: {line.strip()[:90]}")
check("R7b. no F1 file writes to the managers table (quarantine is derived state only)",
      not write_hits, "; ".join(write_hits))

# --------------------------------------------------------------------------
print("--- R8: taxonomy parity manager_registry <-> main.py ---")
main_rcs = main_classifier_rc_values(SRC_MAIN)
expected = set(main_rcs) - {"unknown"}
actual = set(manager_registry.MANAGER_SCOPED_START_FAILURES)
check("R8a. MANAGER_SCOPED_START_FAILURES == main.py classifier rc values minus 'unknown'",
      actual == expected, f"registry={sorted(actual)} main={sorted(expected)}")
check("R8b-parity. 'unknown' is deliberately excluded from the registry set",
      "unknown" not in actual and "unknown" in main_rcs,
      f"registry={sorted(actual)} main={sorted(main_rcs)}")

# --------------------------------------------------------------------------
print("--- R8b: UNKNOWN never fails open ---")
for rc in ("", "   ", "totally_unknown", "SESSION_UNAUTHORIZED_X", "global", "manager_scoped"):
    check(f"R8b. classify_start_failure({rc!r}) is not manager_scoped",
          manager_registry.classify_start_failure(rc) == "unknown",
          f"got {manager_registry.classify_start_failure(rc)!r}")
for rc in sorted(manager_registry.MANAGER_SCOPED_START_FAILURES):
    check(f"R8b. classify_start_failure({rc!r}) == manager_scoped",
          manager_registry.classify_start_failure(rc) == "manager_scoped")

proc = subprocess.run([PYEXE, REG_PY, "classify-start-failure", "totally_unknown"],
                      capture_output=True, text=True, timeout=60)
check("R8b. REAL CLI classify-start-failure on an unknown class prints 'unknown'",
      proc.returncode == 0 and (proc.stdout or "").strip() == "unknown",
      f"rc={proc.returncode} out={(proc.stdout or '').strip()!r}")

if PS_HOST:
    r8b = run_resolve(None, raw_text='{"manager_key":"alpha","phase":"exited","updated_at":"'
                                     + _iso(LAUNCH_UTC + timedelta(seconds=1))
                                     + '","reason_class":"totally_unknown"}', tag="r8b")
    check("R8b. unknown reason_class WITH valid fresh evidence is still isolatable (exit 3) "
          "-- the exit code is decided by evidence, not by reason_class",
          r8b["exitcode"] == 3 and r8b["classification"] == "unknown",
          f"exit={r8b['exitcode']} cls={r8b['classification']!r}")

# --------------------------------------------------------------------------
print("--- R9: ManagerBot has exactly one canonical start/stop owner ---")
start_mb_calls = len(re.findall(r"start_manager_bot\.ps1", SRC_START_EVERYTHING))
check("R9a. start_everything.ps1 invokes start_manager_bot.ps1 exactly once",
      start_mb_calls == 1, f"found {start_mb_calls}")

targets_m = re.search(r"^\$targets\s*=\s*@\((.*?)\)", SRC_STOP_EVERYTHING, flags=re.M | re.S)
targets = re.findall(r'"([^"]+)"', targets_m.group(1)) if targets_m else []
check("R9b. stop_everything.ps1 $targets contains manager_bot.py exactly once",
      targets.count("manager_bot.py") == 1, f"targets={targets}")

kill_idx = SRC_START_MANAGER_BOT.find("Stop-Process")
spawn_idx = SRC_START_MANAGER_BOT.find("Start-Process")
check("R9c. start_manager_bot.ps1 kills pre-existing manager_bot.py BEFORE starting a new one",
      kill_idx != -1 and spawn_idx != -1 and kill_idx < spawn_idx,
      f"kill@{kill_idx} spawn@{spawn_idx}")

other_launchers = []
for p in sorted(list(BASE_DIR.glob("*.ps1")) + list(BASE_DIR.glob("*.bat"))):
    if p.name in ("start_manager_bot.ps1", "start_manager_bot.bat", "stop_manager_bot.ps1"):
        continue
    code = strip_shell_comments(_read_source(p), p.suffix.lower())
    # An INVOCATION, not a mention: a comment that merely names the launcher
    # (stop_everything.ps1 documents why manager_bot.py joined $targets) is not
    # a second start owner, which is why comments are stripped first.
    for i, line in enumerate(code.splitlines(), 1):
        # start_everything.ps1 is the intended single owner (R9a already pins it
        # to exactly one invocation); every OTHER launcher must stay silent.
        if p.name != "start_everything.ps1" and re.search(
                r"(&|\.\\|call\s|-File\s+)[^\n]*start_manager_bot", line):
            other_launchers.append(f"{p.name}:{i} (start_manager_bot invocation)")
        if re.search(r"(Start-Process|&|\.\\|call\s|python[^\n]*)\s[^\n]*manager_bot\.py", line) \
                and p.name != "stop_everything.ps1":
            other_launchers.append(f"{p.name}:{i} (manager_bot.py spawn)")
check("R9d. no other .ps1/.bat in the project root spawns manager_bot.py",
      not other_launchers, f"found {other_launchers}")

py_spawners = []
for p in sorted(BASE_DIR.glob("*.py")):
    if p.name == "manager_bot.py":
        continue
    txt = p.read_text(encoding="utf-8", errors="ignore")
    for i, line in enumerate(txt.splitlines(), 1):
        if "manager_bot.py" in line and re.search(r"Popen|Start-Process|subprocess\.run|create_subprocess", line):
            py_spawners.append(f"{p.name}:{i}")
check("R9e. no Python file spawns manager_bot.py as a process",
      not py_spawners, f"found {py_spawners}")

# --------------------------------------------------------------------------
print("--- R10: every key isolated -> shared regression, overall FAIL ---")
if PS_HOST:
    l10 = run_loop({"alpha": 3, "beta": 3, "gamma": 3}, ["alpha", "beta", "gamma"], tag="r10")
    check("R10a. all keys exit 3 -> startup aborts (not a false SUCCESS)",
          l10["result"].startswith("ABORT"), f"got {l10['result']!r}")
    check("R10b. START EVERYTHING OK is not printed when every manager was skipped",
          not l10["printed_ok"], l10["stdout"][:200])

    l10b = run_loop({"alpha": 3, "beta": 0}, ["alpha", "beta"], tag="r10b")
    check("R10c. a partial skip is still SUCCESS",
          l10b["result"] == "SUCCESS" and l10b["printed_ok"], f"got {l10b['result']!r}")

# --------------------------------------------------------------------------
print("--- R11: timeout cleanup stops EVERY matching process, not just $p.Id (H9) ---")
if PS_HOST:
    # Start-Process returns the venv launcher PID (501); its AppData child
    # (777) matches the same $pattern and must be stopped too.
    r11a = run_timeout_cleanup([501, 777], tag="r11a")
    check("R11a. two matching processes (launcher + AppData child) -> both stopped",
          sorted(r11a["stopped"]) == [501, 777],
          f"stopped={r11a['stopped']} err={r11a['stderr'][:200]}")

    r11b = run_timeout_cleanup([501], tag="r11b")
    check("R11b. a single matching process (no child) -> still stopped (no regression)",
          r11b["stopped"] == [501], f"stopped={r11b['stopped']}")

    r11c = run_timeout_cleanup([], tag="r11c")
    check("R11c. zero matching processes -> cleanup block is skipped, nothing stopped",
          r11c["stopped"] == [], f"stopped={r11c['stopped']}")

# ==========================================================================
# MUTATION PROOFS
# ==========================================================================
print()
print("--- MUTATION PROOFS (each mutation must turn its scenario RED) ---")


def mutate(text: str, old: str, new: str, label: str) -> str | None:
    if old not in text:
        check(f"{label} (mutation anchor present)", False, f"anchor not found: {old!r}")
        return None
    return text.replace(old, new, 1)


if PS_HOST:
    # M1 -- manager-scoped path throws instead of exiting 3
    m1 = run_loop({"alpha": "throw", "beta": 0}, ["alpha", "beta"], tag="m1")
    check("M1. throw instead of exit 3 -> loop aborts (R1 would be RED)",
          m1["result"].startswith("ABORT"), f"got {m1['result']!r}")

    # M2 -- remove the `continue` branch for exit 3
    mut = mutate(LOOP_REGION, "} elseif ($code -eq 3) {", "} elseif ($false) {", "M2")
    if mut:
        m2 = run_loop({"alpha": 3, "beta": 0}, ["alpha", "beta"], loop_text=mut, tag="m2")
        check("M2. no isolation branch -> exit 3 aborts the startup (R1 RED)",
              m2["result"].startswith("ABORT"), f"got {m2['result']!r}")

    # M3 -- fail-open default: a missing status file becomes manager-scoped
    mut = mutate(RESOLVE_FN, "ExitCode = 1;", "ExitCode = 3;", "M3")
    if mut:
        m3 = run_resolve(None, fn_text=mut, tag="m3")
        check("M3. fail-open default -> missing status file becomes exit 3 (R4a/R8b RED)",
              m3["exitcode"] == 3, f"got {m3['exitcode']}")

    # M3a -- the pre-C-1a implementation itself
    m3a = run_resolve(None, raw_text='{"manager_key": "alpha", "phase": "exi',
                      fn_text=PREFIX_ORIGINAL_RESOLVE, tag="m3a")
    check("M3a. pre-fix implementation isolates on an UNREADABLE status file (R4b RED)",
          m3a["exitcode"] == 3, f"got {m3a['exitcode']} detail={m3a['detail']!r}")
    m3a2 = run_resolve(fresh_status(updated_at=_iso(LAUNCH_UTC - timedelta(days=3))),
                       fn_text=PREFIX_ORIGINAL_RESOLVE, tag="m3a2")
    check("M3a. pre-fix implementation isolates on a STALE status file (R4c RED)",
          m3a2["exitcode"] == 3, f"got {m3a2['exitcode']}")
    m3a3 = run_resolve(fresh_status(manager_key="someone_else"),
                       fn_text=PREFIX_ORIGINAL_RESOLVE, tag="m3a3")
    check("M3a. pre-fix implementation isolates on a FOREIGN status file (R4d RED)",
          m3a3["exitcode"] == 3, f"got {m3a3['exitcode']}")

    # M3b -- drop the freshness comparison (regex anchor: formatting-independent)
    mut, _n = re.subn(r"elseif\s*\(\s*\$upd\s+-lt\s+\$launchUtc\.AddSeconds\([^)]*\)\s*\)",
                      "elseif ($false)", RESOLVE_FN, count=1)
    if _n != 1:
        check("M3b (mutation anchor present)", False,
              "freshness comparison ($upd -lt $launchUtc ...) not found in Resolve-StartFailureExit")
        mut = None
    if mut is not None:
        m3b = run_resolve(fresh_status(updated_at=_iso(LAUNCH_UTC - timedelta(days=3))),
                          fn_text=mut, tag="m3b")
        check("M3b. no freshness check -> a stale status file is accepted as evidence (R4c RED)",
              m3b["exitcode"] == 3, f"got {m3b['exitcode']}")

    # M3c -- drop the phase whitelist and the manager_key ownership check
    mut = RESOLVE_FN
    anchors_ok = True
    for pattern in (
        r"elseif\s*\(\s*@\([^)]*\)\s+-notcontains\s+\[string\]\$ss\.phase\s*\)",
        r"elseif\s*\(\s*\(\[string\]\$ss\.manager_key\)\s+-ne\s+\$ManagerKey\s*\)",
    ):
        mut, _n = re.subn(pattern, "elseif ($false)", mut, count=1)
        if _n != 1:
            anchors_ok = False
            break
    if not anchors_ok:
        check("M3c (mutation anchors present)", False,
              "phase whitelist / manager_key ownership checks not found in Resolve-StartFailureExit")
    else:
        m3c1 = run_resolve(fresh_status(phase="bogus"), fn_text=mut, tag="m3c1")
        check("M3c. no phase whitelist -> a bogus phase is accepted (R4d RED)",
              m3c1["exitcode"] == 3, f"got {m3c1['exitcode']}")
        m3c2 = run_resolve(fresh_status(manager_key="someone_else"), fn_text=mut, tag="m3c2")
        check("M3c. no ownership check -> a foreign status file is accepted (R4d RED)",
              m3c2["exitcode"] == 3, f"got {m3c2['exitcode']}")

    # M3d -- naive locale round-trip of updated_at. ConvertFrom-Json hands back a
    # [DateTime] (Kind=Local); re-rendering it via [string] uses the CURRENT
    # CULTURE, so "2026-08-10T06:41:34+00:00" comes back as "08/10/2026 09:41:34"
    # and re-parses as 8 OCTOBER -- a stale file would look months in the future
    # and pass the freshness check. This mutation restores exactly that bug.
    mut = RESOLVE_FN
    naive = "([DateTime]::Parse([string]$rawUpd)).ToUniversalTime()"
    mut, n1 = re.subn(r"\(\[DateTime\]\$rawUpd\)\.ToUniversalTime\(\)", naive.replace("\\", "\\\\"), mut, count=1)
    mut, n2 = re.subn(r"\(\[DateTimeOffset\]::Parse\([^\n]*\)\)\.UtcDateTime", naive.replace("\\", "\\\\"), mut, count=1)
    if n1 != 1 or n2 != 1:
        check("M3d (mutation anchors present)", False,
              f"updated_at conversion branches not found (n1={n1}, n2={n2})")
    else:
        m3d = run_resolve(fresh_status(updated_at=_iso(LAUNCH_UTC - timedelta(minutes=10))),
                          fn_text=mut, tag="m3d")
        check("M3d. locale round-trip of updated_at -> a 10-minute-stale file is "
              "accepted as fresh (R4c RED)",
              m3d["exitcode"] == 3, f"got {m3d['exitcode']} detail={m3d['detail']!r}")

    # M4 -- drop the all-skipped abort
    mut = mutate(SUMMARY_REGION + "", "if ($keys.Count -gt 0 -and $skipped.Count -eq $keys.Count) {",
                 "if ($false) {", "M4") if "if ($keys.Count -gt 0 -and $skipped.Count -eq $keys.Count) {" in SUMMARY_REGION else None
    loop_mut = None
    if mut is None:
        if "if ($keys.Count -gt 0 -and $skipped.Count -eq $keys.Count) {" in LOOP_REGION:
            loop_mut = LOOP_REGION.replace(
                "if ($keys.Count -gt 0 -and $skipped.Count -eq $keys.Count) {", "if ($false) {", 1)
        else:
            check("M4 (mutation anchor present)", False, "all-skipped abort not found")
    if loop_mut is not None:
        m4 = run_loop({"alpha": 3, "beta": 3}, ["alpha", "beta"], loop_text=loop_mut, tag="m4")
        check("M4. no all-skipped rule -> zero running managers reported as SUCCESS (R10 RED)",
              m4["result"] == "SUCCESS", f"got {m4['result']!r}")
    elif mut is not None:
        m4 = run_loop({"alpha": 3, "beta": 3}, ["alpha", "beta"], summary_text=mut, tag="m4")
        check("M4. no all-skipped rule -> zero running managers reported as SUCCESS (R10 RED)",
              m4["result"] == "SUCCESS", f"got {m4['result']!r}")

    # M7 -- revert H9: stop only $p.Id instead of every matching process
    mut = mutate(
        TIMEOUT_CLEANUP_REGION,
        "    $still_alive | ForEach-Object {\n"
        "        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}\n"
        "    }",
        "    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}",
        "M7",
    )
    if mut:
        m7 = run_timeout_cleanup([501, 777], region_text=mut, tag="m7")
        check("M7. pre-H9-fix logic (stop only $p.Id) leaves the AppData child "
              "process alive (R11a RED)",
              sorted(m7["stopped"]) != [501, 777], f"stopped={m7['stopped']}")

# M5 -- ManagerBot duplicate start / missing pre-kill
m5_src = SRC_START_EVERYTHING + '\n& (Join-Path $Root "start_manager_bot.ps1")\n'
check("M5a. a second ManagerBot start makes the single-owner anchor RED",
      len(re.findall(r"start_manager_bot\.ps1", m5_src)) != 1,
      "duplicate start was not detected")
m5b_src = re.sub(r"Get-CimInstance[\s\S]*?\}\s*\n", "", SRC_START_MANAGER_BOT, count=1)
check("M5b. removing the pre-kill block makes the ordering anchor RED",
      not (m5b_src.find("Stop-Process") != -1
           and m5b_src.find("Stop-Process") < m5b_src.find("Start-Process")),
      "pre-kill removal was not detected")

# M6 -- quarantined key routed back into reasons
wd_src_mut = SRC_WATCHDOG.replace(
    "if manager_down_classification.get(k) == 'manager_scoped':", "if False:", 1)
if wd_src_mut == SRC_WATCHDOG:
    check("M6 (mutation anchor present)", False, "quarantine branch not found in watchdog")
else:
    cs_mut = build_check_soft(wd_src_mut, wd_base2, ["alpha", "gamma"], {"gamma": 1})
    okm, reasonsm, metam = cs_mut()
    check("M6. quarantined manager back in reasons -> whole watchdog unhealthy (R6 RED)",
          okm is False and any("alpha" in r for r in reasonsm),
          f"ok={okm} reasons={reasonsm}")

# ==========================================================================
print()
print("=" * 78)
if BLOCKED:
    print(f"BLOCKED: {len(BLOCKED)}")
    for b in BLOCKED:
        print(f"  - {b}")
if FAILURES:
    print(f"FAILURES: {len(FAILURES)}")
    for f in FAILURES:
        print(f"  - {f}")
else:
    print("ALL CHECKS PASSED")
print("=" * 78)

try:
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass

sys.exit(1 if (FAILURES or BLOCKED) else 0)
