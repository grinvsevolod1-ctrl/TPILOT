"""process_control.py -- single cross-platform source of truth for TPilot
process discovery and lifecycle management.

WHY THIS MODULE EXISTS
======================
Before this module, process handling was duplicated in four places with four
different (and mutually inconsistent) implementations:

  * main.py           -- `pgrep -f "main.py --manager <key>"` / `pkill -f ...`
  * panel_bot.py      -- PowerShell CIM on Windows, `ps aux` on POSIX
  * preflight_check.py-- PowerShell CIM only, hard `return []` on non-Windows
  * soft_watchdog_pinger.py -- PowerShell CIM, `ps aux`, Windows-only child collapse

Every one of those POSIX paths was broken, which made the whole project
undeployable on Ubuntu:

  BUG 1 (main.py, critical): the pgrep/pkill pattern was the literal string
      "main.py --manager <key>", but `_spawn_manager_process` actually launches
      `main.py --env <env_file> --manager <key>`. The `--env` argument sits
      between the two halves of the pattern, so `pgrep -f` NEVER matched.
      Consequences: every manager always reported "stopped"; `MANAGER STOP`
      returned `(True, "OK")` while killing nothing; and
      `_onboarding_stop_running_for_key` -- whose entire job is to prevent two
      Telethon clients from opening the same `.session` -- silently passed,
      allowing exactly the session collision it was written to prevent.

  BUG 2 (main.py): `_stop_manager_process` returned `(True, "OK")` whenever
      `subprocess.run` did not raise, regardless of whether anything was
      actually killed. A "successful" stop was never verified.

  BUG 3 (preflight_check.py): `_scan_python_processes` began with
      `if not sys.platform.startswith("win"): return []`, so on Ubuntu the
      preflight report declared every single service and manager dead and
      told the operator to run `restart_everything.bat`.

  BUG 4 (soft_watchdog_pinger.py): same Windows-only gate, so the watchdog was
      blind on Linux and could never restart anything.

  BUG 5 (panel_bot.py): the POSIX branch used `ps aux`, whose `args` column is
      truncated to the terminal/buffer width. With a realistic absolute venv
      path the `--manager <key>` tail fell off the end of the line, so the
      regex never matched and managers rendered as dead.

  BUG 6 (soft_watchdog_pinger.py): `_collapse_venv_children` dropped any
      process whose ppid belonged to another project-relevant process. That
      rule is safe on Windows (where the venv launcher and its real Python
      child share an identical command line) but catastrophic on Linux, where
      the controller legitimately spawns manager children -- the rule would
      delete every manager from the scan. This module replaces it with a
      parent/child *identical command line* rule, which is what the Windows
      case actually needed and which is correct on every platform.

DESIGN CONTRACT
===============
* No import-time side effects. Safe to import from any module, including ones
  that cannot themselves be imported standalone (main.py, panel_bot.py).
* stdlib only, with `psutil` used opportunistically when installed. There is a
  pure-stdlib `/proc` reader and a `ps -ww` reader behind it, so the module
  never hard-fails because psutil is missing.
* TRI-STATE SCAN CONTRACT (inherited verbatim from panel_bot.py N5.4.6/RF1 --
  downstream code depends on it):
      - a LIST (possibly empty) means the scan genuinely completed and its
        content is trustworthy; an empty list is a REAL, CONFIRMED "no match";
      - None means the scan itself could not be trusted (all backends failed)
        and callers MUST treat it as UNKNOWN, never as a confirmed negative.
  Reporting a false "process down" is what triggers spurious restarts, so the
  distinction is a safety property, not a nicety.
* Nothing in this module spends money, touches Telegram, or writes to the
  database. It reads process tables and sends signals.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "IS_WINDOWS",
    "IS_LINUX",
    "ProcRow",
    "norm_path",
    "venv_python",
    "list_python_processes",
    "manager_key_from_cmdline",
    "filter_project_procs",
    "collapse_launcher_children",
    "count_managers_by_key",
    "manager_process_running",
    "find_manager_procs",
    "find_manager_pids",
    "script_process_running",
    "systemd_available",
    "manager_unit_name",
    "systemd_unit_exists",
    "systemd_is_active",
    "systemd_start",
    "systemd_stop",
    "systemd_restart",
    "stop_manager",
    "start_manager",
    "running_manager_keys",
    "start_script",
    "stop_script",
]

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

# A scanned process row. Values are always `str` so that every consumer can
# keep using the plain-dict access pattern it already had.
ProcRow = Dict[str, str]

# Matches both `--manager <key>` and `--manager=<key>`. Manager keys are
# normalised to [a-z0-9_-] everywhere in the project (manager_registry.py:
# normalize_manager_key), so the character class is exhaustive.
_MANAGER_ARG_RE = re.compile(r"--manager[=\s]+([A-Za-z0-9_-]+)")

_DEFAULT_SCAN_TIMEOUT = 10.0
# Must exceed the units' TimeoutStopSec=45, otherwise we would declare a stop
# failed while systemd is still legitimately waiting for Telethon to close its
# SQLite session -- and the caller would then refuse to restart the manager.
_DEFAULT_STOP_TIMEOUT = 55.0

# systemd unit naming. The Ubuntu deployment runs one templated unit per
# manager plus one plain unit per service, so the panel can address any of them
# without knowing about PIDs at all.
SYSTEMD_MANAGER_UNIT_TEMPLATE = "tpilot-manager@{key}.service"
SYSTEMD_SERVICE_UNITS = {
    "controller": "tpilot-controller.service",
    "panel": "tpilot-panel.service",
    "manager_bot": "tpilot-manager-bot.service",
    "partner_bot": "tpilot-partner-bot.service",
    "watchdog": "tpilot-watchdog.service",
    "health": "tpilot-health.service",
}


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def norm_path(value: Any) -> str:
    """Normalise a path or command line for case-insensitive substring
    comparison. Backslashes become forward slashes so that a Windows-recorded
    command line and a POSIX one compare identically. This is byte-identical in
    behaviour to the four private `_norm_path` copies it replaces."""
    return str(value or "").replace("\\", "/").lower()


def venv_python(base_dir: Any) -> Path:
    """Interpreter inside the project venv, or the current interpreter when no
    venv is present. Replaces the hardcoded `venv/Scripts/python.exe` lookup in
    soft_watchdog_pinger.py, which resolved to a non-existent path on Ubuntu and
    silently fell back to `sys.executable`."""
    base = Path(str(base_dir or "."))
    candidates = (
        base / "venv" / "bin" / "python3",
        base / "venv" / "bin" / "python",
        base / "venv" / "Scripts" / "python.exe",
        base / ".venv" / "bin" / "python3",
        base / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
    return Path(sys.executable)


def _looks_like_python(comm: str, cmd: str) -> bool:
    """Whether a process row belongs to a Python interpreter. Mirrors the
    `Name -like 'python*'` filter of the original PowerShell queries, but also
    accepts the interpreter's basename from argv[0] so that renamed or
    version-suffixed binaries (`python3.12`, venv symlinks) still match."""
    if comm.startswith("python"):
        return True
    head = (cmd.strip().split(" ", 1)[0] or "").rsplit("/", 1)[-1]
    return head.startswith("python")


# ---------------------------------------------------------------------------
# Process enumeration backends
# ---------------------------------------------------------------------------

def _scan_via_psutil() -> Optional[List[ProcRow]]:
    """Preferred backend: psutil reads the process table through the OS API, so
    there is no output truncation and no shell parsing at all."""
    try:
        import psutil  # noqa: PLC0415 -- optional dependency, probed at call time
    except Exception:
        return None

    rows: List[ProcRow] = []
    try:
        iterator = psutil.process_iter(["pid", "ppid", "name", "cmdline"])
    except Exception:
        return None

    for proc in iterator:
        try:
            info = proc.info
            cmdline = info.get("cmdline") or []
            cmd = " ".join(str(part) for part in cmdline)
            comm = str(info.get("name") or "").lower()
            if not cmd or not _looks_like_python(comm, cmd):
                continue
            rows.append({
                "pid": str(info.get("pid") or ""),
                "ppid": str(info.get("ppid") or ""),
                "name": comm,
                "cmd": cmd,
            })
        except Exception:
            # A process that exited mid-iteration (NoSuchProcess) or one we may
            # not inspect (AccessDenied) is skipped -- the scan as a whole is
            # still trustworthy, so this must NOT downgrade the result to None.
            continue
    return rows


def _scan_via_proc() -> Optional[List[ProcRow]]:
    """Pure-stdlib Linux backend: read `/proc/<pid>/cmdline` directly. `cmdline`
    is NUL-separated and world-readable, and `stat` gives us comm + ppid, so
    this needs no external process and cannot be truncated."""
    proc_root = Path("/proc")
    try:
        entries = [name for name in os.listdir(proc_root) if name.isdigit()]
    except Exception:
        return None

    rows: List[ProcRow] = []
    for pid in entries:
        try:
            raw = (proc_root / pid / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if not raw:
            continue  # kernel thread
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()

        comm = ""
        ppid = ""
        try:
            stat_raw = (proc_root / pid / "stat").read_text(errors="replace")
            # The comm field is parenthesised and may itself contain spaces or
            # ')', so split on the LAST ')' rather than naive whitespace.
            open_paren = stat_raw.find("(")
            close_paren = stat_raw.rfind(")")
            if 0 <= open_paren < close_paren:
                comm = stat_raw[open_paren + 1:close_paren].lower()
                tail = stat_raw[close_paren + 1:].split()
                if len(tail) >= 2:
                    ppid = tail[1]
        except (OSError, PermissionError, IndexError, ValueError):
            pass

        if not _looks_like_python(comm, cmd):
            continue
        rows.append({"pid": pid, "ppid": ppid, "name": comm, "cmd": cmd})
    return rows


def _scan_via_ps(timeout_sec: float) -> Optional[List[ProcRow]]:
    """POSIX fallback backend. Uses `ps -eo ... -ww`, NOT `ps aux`.

    This is BUG 5's fix: `ps aux` truncates the `args` column to the output
    width, which silently cut the `--manager <key>` tail off any process
    launched from a long absolute venv path. `-ww` disables truncation entirely,
    and the explicit `-o` field list removes the header/column guesswork the
    old line-splitting parser relied on."""
    if not shutil.which("ps"):
        return None
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm=,args=", "-ww"],
            capture_output=True, text=True, timeout=timeout_sec, check=False,
        )
    except Exception:
        return None

    stdout = completed.stdout or ""
    if completed.returncode != 0 and not stdout.strip():
        return None

    rows: List[ProcRow] = []
    for line in stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, ppid, comm, cmd = parts[0], parts[1], parts[2].lower(), parts[3]
        if not pid.isdigit():
            continue
        if not _looks_like_python(comm, cmd):
            continue
        rows.append({"pid": pid, "ppid": ppid, "name": comm, "cmd": cmd})
    return rows


def _scan_via_powershell(base_dir: Any, timeout_sec: float) -> Optional[List[ProcRow]]:
    """Windows backend, kept so the developer workstation keeps working during
    the migration. Preserves the original tri-state semantics exactly: a clean
    run with empty output is a confirmed `[]`, while a spawn failure, timeout,
    non-zero exit with no output, or malformed JSON is an untrustworthy None."""
    import json  # noqa: PLC0415 -- only needed on this platform branch

    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and $_.CommandLine } | "
        "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            cwd=str(base_dir) if base_dir else None,
            capture_output=True, text=True, timeout=timeout_sec, check=False,
        )
    except Exception:
        return None

    raw = (completed.stdout or "").strip()
    if not raw:
        return None if completed.returncode != 0 else []
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if isinstance(data, dict):
        data = [data]

    rows: List[ProcRow] = []
    for row in data or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "pid": str(row.get("ProcessId", "")),
            "ppid": str(row.get("ParentProcessId", "")),
            "name": str(row.get("Name", "")).lower(),
            "cmd": str(row.get("CommandLine", "")),
        })
    return rows


def list_python_processes(
    *,
    base_dir: Any = None,
    timeout_sec: float = _DEFAULT_SCAN_TIMEOUT,
) -> Optional[List[ProcRow]]:
    """Enumerate running Python processes with their full command lines.

    Returns a LIST (possibly empty) when the scan completed and can be trusted,
    or None when no backend produced a trustworthy answer -- see the tri-state
    contract in the module docstring. Never raises.

    Backends are tried in descending order of reliability: psutil, then a direct
    `/proc` read on Linux, then `ps -eo ... -ww`, then PowerShell CIM on Windows.
    """
    backends: List[Any] = [_scan_via_psutil]
    if IS_WINDOWS:
        backends.append(lambda: _scan_via_powershell(base_dir, timeout_sec))
    else:
        if IS_LINUX:
            backends.append(_scan_via_proc)
        backends.append(lambda: _scan_via_ps(timeout_sec))

    for backend in backends:
        try:
            rows = backend()
        except Exception:
            rows = None
        if rows is not None:
            return rows
    return None


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def manager_key_from_cmdline(cmd: Any) -> Optional[str]:
    """Extract the manager key from a command line, or None for a non-manager
    process. Accepts `--manager <key>` and `--manager=<key>`, and is indifferent
    to any other arguments (such as the `--env <file>` pair that broke the old
    literal-substring matching in main.py -- BUG 1)."""
    match = _MANAGER_ARG_RE.search(str(cmd or ""))
    return match.group(1).lower() if match else None


def filter_project_procs(procs: Iterable[ProcRow], base_dir: Any) -> List[ProcRow]:
    """Keep only processes whose command line references this project tree."""
    base_norm = norm_path(base_dir)
    if not base_norm:
        return list(procs or [])
    return [p for p in (procs or []) if base_norm in norm_path(p.get("cmd", ""))]


def collapse_launcher_children(procs: Sequence[ProcRow]) -> List[ProcRow]:
    """Collapse interpreter-launcher parent/child pairs so each logical instance
    is counted exactly once.

    On Windows, running `venv/Scripts/python.exe` spawns a real Python child
    (e.g. `Python312/python.exe`) that reports an IDENTICAL command line, so a
    raw count doubles every service.

    This is BUG 6's fix. The original rule was "drop any process whose ppid is
    in the relevant set", which on Linux would delete every manager, because the
    controller is itself a relevant process and legitimately spawns managers as
    children. The rule here additionally requires the parent and child command
    lines to be IDENTICAL, which is precisely what makes a launcher pair a
    launcher pair -- correct on Windows, and safe on Linux, so no platform gate
    is needed any more.
    """
    rows = list(procs or [])
    by_pid: Dict[str, ProcRow] = {str(p.get("pid") or ""): p for p in rows if p.get("pid")}
    result: List[ProcRow] = []
    for proc in rows:
        parent = by_pid.get(str(proc.get("ppid") or ""))
        if parent is not None and norm_path(parent.get("cmd", "")) == norm_path(proc.get("cmd", "")):
            continue  # duplicate half of an interpreter-launcher pair
        result.append(proc)
    return result


def count_managers_by_key(procs: Iterable[ProcRow], base_dir: Any) -> Dict[str, int]:
    """Map manager key -> number of live runtime processes for that key. A value
    above 1 means a duplicate runtime, which is a genuine incident: two Telethon
    clients on one `.session` corrupt it."""
    counts: Dict[str, int] = {}
    relevant = collapse_launcher_children(filter_project_procs(procs, base_dir))
    for proc in relevant:
        cmd = norm_path(proc.get("cmd", ""))
        if "main.py" not in cmd:
            continue
        key = manager_key_from_cmdline(cmd)
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def find_manager_procs(
    manager_key: str,
    *,
    base_dir: Any,
    procs: Optional[Iterable[ProcRow]] = None,
) -> Optional[List[ProcRow]]:
    """Scan rows belonging to one manager's runtime. None means the scan was
    untrustworthy (unknown), which is NOT the same as an empty list.

    This is deliberately separate from `find_manager_pids`: LIVENESS must not
    depend on a row carrying a parseable pid. A backend or caller may supply a
    row with only a command line (the panel's cached scan is passed through
    verbatim, and selftests construct cmd-only rows), and treating such a row as
    "not running" would resurrect the exact false-process_down class of bug this
    module exists to eliminate."""
    key = str(manager_key or "").strip().lower()
    if not key:
        return []
    rows = list(procs) if procs is not None else list_python_processes(base_dir=base_dir)
    if rows is None:
        return None

    matched: List[ProcRow] = []
    for proc in collapse_launcher_children(filter_project_procs(rows, base_dir)):
        cmd = norm_path(proc.get("cmd", ""))
        if "main.py" not in cmd or manager_key_from_cmdline(cmd) != key:
            continue
        matched.append(proc)
    return matched


def find_manager_pids(
    manager_key: str,
    *,
    base_dir: Any,
    procs: Optional[Iterable[ProcRow]] = None,
) -> Optional[List[int]]:
    """PIDs of the runtime processes for one manager. None means the scan was
    untrustworthy (unknown), which is NOT the same as an empty list.

    Rows without a usable pid are omitted here (there is no pid to signal), so
    an EMPTY list does not prove the manager is stopped -- use
    `manager_process_running` for liveness and this function only for signalling.
    """
    matched = find_manager_procs(manager_key, base_dir=base_dir, procs=procs)
    if matched is None:
        return None

    pids: List[int] = []
    for proc in matched:
        try:
            pid = int(proc.get("pid") or 0)
        except (TypeError, ValueError):
            continue
        if pid > 0:
            pids.append(pid)
    return pids


def manager_process_running(
    manager_key: str,
    *,
    base_dir: Any,
    procs: Optional[Iterable[ProcRow]] = None,
) -> Optional[bool]:
    """Tri-state liveness for one manager runtime: True, False, or None when the
    scan could not be trusted. Prefers systemd when a unit is installed, because
    the unit's own state is authoritative and immune to cmdline parsing.

    Liveness is decided on MATCHED ROWS, not on parsed pids -- see
    `find_manager_procs` for why a pid-less row must still count as running."""
    key = str(manager_key or "").strip().lower()
    if not key:
        return False

    if procs is None:
        unit = manager_unit_name(key)
        if systemd_unit_exists(unit):
            active = systemd_is_active(unit)
            if active is not None:
                return active

    matched = find_manager_procs(key, base_dir=base_dir, procs=procs)
    return None if matched is None else bool(matched)


def script_process_running(
    script_name: str,
    *,
    base_dir: Any,
    procs: Optional[Iterable[ProcRow]] = None,
    exclude_manager_flag: bool = False,
) -> Optional[bool]:
    """Tri-state liveness for a service script (`panel_bot.py`, `main.py`, ...).

    `exclude_manager_flag=True` distinguishes the controller (`main.py` with no
    `--manager`) from a manager runtime, which shares the same script name."""
    needle = norm_path(script_name)
    if not needle:
        return False
    rows = list(procs) if procs is not None else list_python_processes(base_dir=base_dir)
    if rows is None:
        return None
    for proc in filter_project_procs(rows, base_dir):
        cmd = norm_path(proc.get("cmd", ""))
        if needle not in cmd:
            continue
        if exclude_manager_flag and manager_key_from_cmdline(cmd):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# systemd integration
# ---------------------------------------------------------------------------

def _systemctl_path() -> Optional[str]:
    return shutil.which("systemctl")


def systemd_available() -> bool:
    """Whether this host is running systemd as PID 1 and `systemctl` is usable.
    A container without systemd, or Windows, answers False and every caller
    falls back to direct process handling."""
    if IS_WINDOWS or not _systemctl_path():
        return False
    try:
        return Path("/run/systemd/system").exists()
    except OSError:
        return False


def manager_unit_name(manager_key: str) -> str:
    """Templated unit name for one manager runtime."""
    return SYSTEMD_MANAGER_UNIT_TEMPLATE.format(key=str(manager_key or "").strip().lower())


def _systemctl(args: Sequence[str], *, timeout_sec: float = 15.0) -> Optional[subprocess.CompletedProcess]:
    """Run `systemctl` with `--user` awareness. Returns None if systemctl could
    not be executed at all, so callers can distinguish "unit is inactive" from
    "we could not ask"."""
    systemctl = _systemctl_path()
    if not systemctl:
        return None
    scope = ["--user"] if os.environ.get("TPILOT_SYSTEMD_SCOPE", "").strip().lower() == "user" else []
    try:
        return subprocess.run(
            [systemctl, *scope, *args],
            capture_output=True, text=True, timeout=timeout_sec, check=False,
        )
    except Exception:
        return None


def systemd_unit_exists(unit: str) -> bool:
    """Whether the unit is known to systemd (installed and loadable)."""
    if not unit or not systemd_available():
        return False
    completed = _systemctl(["cat", unit])
    return bool(completed and completed.returncode == 0)


def systemd_is_active(unit: str) -> Optional[bool]:
    """True/False for an installed unit, or None when systemd could not be
    queried -- the tri-state contract again, so a broken query never reads as a
    confirmed "service down"."""
    if not unit:
        return None
    completed = _systemctl(["is-active", unit])
    if completed is None:
        return None
    state = (completed.stdout or "").strip().lower()
    if state in {"active", "activating", "reloading"}:
        return True
    if state in {"inactive", "failed", "deactivating", "unknown", "not-found"}:
        return False
    return None


def _systemd_action(action: str, unit: str) -> Tuple[bool, str]:
    completed = _systemctl([action, unit], timeout_sec=45.0)
    if completed is None:
        return False, "systemctl недоступен."
    if completed.returncode == 0:
        return True, "OK"
    detail = ((completed.stderr or "") + (completed.stdout or "")).strip()
    return False, f"systemctl {action} {unit}: {detail[:400] or 'ошибка без вывода'}"


def systemd_start(unit: str) -> Tuple[bool, str]:
    return _systemd_action("start", unit)


def systemd_stop(unit: str) -> Tuple[bool, str]:
    return _systemd_action("stop", unit)


def systemd_restart(unit: str) -> Tuple[bool, str]:
    return _systemd_action("restart", unit)


# ---------------------------------------------------------------------------
# Manager lifecycle
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Whether a PID is still running, without sending a real signal."""
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            import psutil  # noqa: PLC0415
            return psutil.pid_exists(pid)
        except Exception:
            return True  # cannot verify -> assume alive, never claim a false stop
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return True


def _terminate_pid(pid: int) -> None:
    if IS_WINDOWS:
        try:
            import psutil  # noqa: PLC0415
            psutil.Process(pid).terminate()
        except Exception:
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _kill_pid(pid: int) -> None:
    if IS_WINDOWS:
        try:
            import psutil  # noqa: PLC0415
            psutil.Process(pid).kill()
        except Exception:
            pass
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def stop_manager(
    manager_key: str,
    *,
    base_dir: Any,
    timeout_sec: float = _DEFAULT_STOP_TIMEOUT,
) -> Tuple[bool, str]:
    """Stop a manager runtime and VERIFY that it is gone.

    This is BUG 2's fix. The old implementation reported success whenever the
    kill command merely executed; it never re-scanned. That mattered because
    `_onboarding_stop_running_for_key` trusts this return value before opening a
    second Telethon client on the manager's `.session` file.

    Escalation: systemd `stop` when a unit is installed, otherwise SIGTERM ->
    grace period -> SIGKILL. Returns `(False, reason)` if any process survives.
    Telethon needs the graceful signal to close its SQLite session cleanly, so
    SIGKILL is only ever the last resort.
    """
    key = str(manager_key or "").strip().lower()
    if not key:
        return False, "Пустой ключ менеджера."

    unit = manager_unit_name(key)
    if systemd_unit_exists(unit):
        ok, msg = systemd_stop(unit)
        if not ok:
            return False, msg
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if systemd_is_active(unit) is False:
                # The unit is inactive, but a runtime started OUTSIDE systemd
                # (the Popen fallback below, or a leftover from the Windows-era
                # deployment) would not be covered by the unit at all. The
                # caller's next step may be opening a Telethon client on this
                # manager's .session, so fall through to a process-level sweep
                # instead of trusting the unit state alone.
                break
            time.sleep(0.5)
        else:
            return False, f"Юнит {unit} не перешёл в inactive за {int(timeout_sec)}с."

    pids = find_manager_pids(key, base_dir=base_dir)
    if pids is None:
        return False, "Не удалось прочитать список процессов — остановка не подтверждена."
    if not pids:
        return True, "OK"

    for pid in pids:
        _terminate_pid(pid)

    grace_deadline = time.monotonic() + max(1.0, timeout_sec - 3.0)
    while time.monotonic() < grace_deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return True, "OK"
        time.sleep(0.25)

    survivors = [pid for pid in pids if _pid_alive(pid)]
    for pid in survivors:
        _kill_pid(pid)

    hard_deadline = time.monotonic() + 3.0
    while time.monotonic() < hard_deadline:
        if not any(_pid_alive(pid) for pid in survivors):
            return True, "OK (SIGKILL)"
        time.sleep(0.25)

    still = [pid for pid in survivors if _pid_alive(pid)]
    if still:
        return False, f"Процессы не завершились даже после SIGKILL: {still}"
    return True, "OK (SIGKILL)"


def start_manager(
    manager_key: str,
    *,
    base_dir: Any,
    env_file: Any = None,
    log_path: Any = None,
    python_exe: Any = None,
) -> Tuple[bool, str]:
    """Start a manager runtime.

    Prefers the systemd templated unit, which gives automatic restart, resource
    accounting and journald logging. Without systemd (or without the unit
    installed) it falls back to a detached `Popen` in its own session, so the
    child survives the controller being restarted.
    """
    key = str(manager_key or "").strip().lower()
    if not key:
        return False, "Пустой ключ менеджера."

    unit = manager_unit_name(key)
    if systemd_unit_exists(unit):
        # systemd start is idempotent for an already-active unit, so no
        # duplicate can arise on this path.
        return systemd_start(unit)

    base = Path(str(base_dir or "."))
    entrypoint = base / "main.py"
    if not entrypoint.exists():
        return False, f"Не найден {entrypoint}"

    # SESSION-SAFETY GATE (fallback path only). Unlike systemd, a bare Popen has
    # no notion of "already running", so without this check a double start would
    # put two Telethon clients on one .session file and corrupt it. Callers are
    # expected to stop first, but this module must not depend on every caller
    # remembering to. An UNKNOWN scan (None) blocks the start too: the whole
    # point is to never guess "nothing is running".
    existing = manager_process_running(key, base_dir=base)
    if existing is None:
        return False, (
            "Не удалось проверить, запущен ли уже этот менеджер — запуск отменён, "
            "чтобы не открыть вторую сессию."
        )
    if existing:
        return False, "Менеджер уже запущен — повторный запуск отменён (защита .session)."

    interpreter = str(python_exe or venv_python(base))
    argv: List[str] = [interpreter, str(entrypoint)]
    if env_file:
        argv += ["--env", str(env_file)]
    argv += ["--manager", key]

    log_handle = None
    try:
        if log_path:
            target = Path(str(log_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            log_handle = target.open("a", encoding="utf-8")
        stream = log_handle if log_handle is not None else subprocess.DEVNULL

        popen_kwargs: Dict[str, Any] = {
            "cwd": str(base),
            "stdout": stream,
            "stderr": stream,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            if flags:
                popen_kwargs["creationflags"] = flags
        else:
            popen_kwargs["start_new_session"] = True

        subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 -- fixed argv, no shell
        return True, "OK"
    except Exception as exc:
        return False, f"Ошибка запуска: {exc!r}"
    finally:
        # The child inherited the file descriptor; closing our copy is correct
        # and keeps the controller from leaking one handle per spawn.
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass


def running_manager_keys(*, base_dir: Any) -> Optional[List[str]]:
    """Every manager key with at least one live runtime, or None if the scan
    failed.

    None is NOT an empty list. `stop_all` relies on that distinction: reporting
    "no managers were running" after a failed scan would leave live Telethon
    sessions behind while telling the operator the stack was cleanly stopped."""
    procs = list_python_processes(base_dir=base_dir)
    if procs is None:
        return None
    return sorted(count_managers_by_key(procs, base_dir))


def start_script(
    script_name: str,
    *,
    base_dir: Any,
    args: Optional[Sequence[str]] = None,
    log_path: Any = None,
    python_exe: Any = None,
) -> Tuple[bool, str]:
    """Start a service script (`panel_bot.py`, `health_server.py`, ...) detached.

    This is the no-systemd fallback only; on a normal Ubuntu deployment the unit
    files own these processes. Unlike `start_manager` there is no .session to
    corrupt here, so the duplicate check lives in the caller (tpilot_ctl), which
    can report it more precisely."""
    base = Path(str(base_dir or "."))
    entrypoint = base / str(script_name)
    if not entrypoint.exists():
        return False, f"Не найден {entrypoint}"

    argv: List[str] = [str(python_exe or venv_python(base)), str(entrypoint)]
    argv += [str(a) for a in (args or [])]

    log_handle = None
    try:
        if log_path:
            target = Path(str(log_path))
            target.parent.mkdir(parents=True, exist_ok=True)
            log_handle = target.open("a", encoding="utf-8")
        stream = log_handle if log_handle is not None else subprocess.DEVNULL

        popen_kwargs: Dict[str, Any] = {
            "cwd": str(base),
            "stdout": stream,
            "stderr": stream,
            "stdin": subprocess.DEVNULL,
        }
        if IS_WINDOWS:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            if flags:
                popen_kwargs["creationflags"] = flags
        else:
            popen_kwargs["start_new_session"] = True

        subprocess.Popen(argv, **popen_kwargs)  # noqa: S603 -- fixed argv, no shell
        return True, "OK"
    except Exception as exc:
        return False, f"Ошибка запуска: {exc!r}"
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass


def stop_script(
    script_name: str,
    *,
    base_dir: Any,
    exclude_manager_flag: bool = False,
    timeout_sec: float = _DEFAULT_STOP_TIMEOUT,
) -> Tuple[bool, str]:
    """Stop a service script and VERIFY it is gone, mirroring `stop_manager`.

    `exclude_manager_flag=True` is required for the controller: it runs the same
    `main.py` as every manager runtime, so without it a "stop the controller"
    request would sweep all managers with it."""
    needle = norm_path(script_name)
    if not needle:
        return False, "Пустое имя скрипта."

    procs = list_python_processes(base_dir=base_dir)
    if procs is None:
        return False, "Не удалось прочитать список процессов — остановка не подтверждена."

    pids: List[int] = []
    matched_without_pid = False
    for proc in filter_project_procs(procs, base_dir):
        cmd = norm_path(proc.get("cmd", ""))
        if needle not in cmd:
            continue
        if exclude_manager_flag and manager_key_from_cmdline(cmd):
            continue
        raw = str(proc.get("pid") or "").strip()
        if raw.isdigit():
            pids.append(int(raw))
        else:
            # Same rule as find_manager_procs: a row we matched but could not
            # attribute a pid to still proves something is running, so we must
            # not report a confident clean stop.
            matched_without_pid = True

    if not pids:
        if matched_without_pid:
            return False, "Процесс найден, но его PID не читается — остановка не подтверждена."
        return True, "OK"

    for pid in pids:
        _terminate_pid(pid)

    grace_deadline = time.monotonic() + max(1.0, timeout_sec - 3.0)
    while time.monotonic() < grace_deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return True, "OK"
        time.sleep(0.25)

    survivors = [pid for pid in pids if _pid_alive(pid)]
    for pid in survivors:
        _kill_pid(pid)

    hard_deadline = time.monotonic() + 3.0
    while time.monotonic() < hard_deadline:
        if not any(_pid_alive(pid) for pid in survivors):
            return True, "OK (SIGKILL)"
        time.sleep(0.25)

    still = [pid for pid in survivors if _pid_alive(pid)]
    if still:
        return False, f"Процессы не завершились даже после SIGKILL: {still}"
    return True, "OK (SIGKILL)"
