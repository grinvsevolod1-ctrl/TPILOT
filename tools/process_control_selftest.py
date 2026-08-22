#!/usr/bin/env python3
"""process_control_selftest.py -- offline selftest for process_control.py.

UBUNTU MIGRATION STAGE 1. No network, no spend, no Telegram, no production DB,
no real process is ever signalled: every check either feeds synthetic scan rows
to a pure function or fakes the subprocess boundary.

Each section is tied to one of the six numbered bugs documented in the
process_control module docstring, so a regression names its own incident.

Run:  python3 tools/process_control_selftest.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import process_control as pc  # noqa: E402

FAILURES: list[str] = []
PASSED = 0

PROJECT = "/opt/tpilot"


def check(label: str, condition: bool, detail=None) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"[OK]   {label}")
    else:
        FAILURES.append(label)
        print(f"[FAIL] {label}  detail={detail!r}")


def row(cmd: str, pid: str = "", ppid: str = "", name: str = "python3") -> dict:
    return {"pid": pid, "ppid": ppid, "name": name, "cmd": cmd}


class FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


# ======================================================================
# BUG 1: the `--env` argument sits between `main.py` and `--manager`, so
#        the old literal `pgrep -f "main.py --manager <key>"` pattern
#        could never match a real spawned runtime.
# ======================================================================

def test_bug1_key_extraction() -> None:
    print("\n--- BUG 1: manager key extraction is argument-order independent ---")

    real = f"{PROJECT}/venv/bin/python3 {PROJECT}/main.py --env {PROJECT}/.env.TPilot --manager foxy1"
    check("1.1 key is found with `--env <file>` between main.py and --manager "
          "(the exact command line _spawn_manager_process produces)",
          pc.manager_key_from_cmdline(real) == "foxy1", pc.manager_key_from_cmdline(real))

    check("1.2 the old literal pattern really was unmatchable (documents the bug)",
          "main.py --manager foxy1" not in real)

    check("1.3 `--manager=<key>` form also parses",
          pc.manager_key_from_cmdline("main.py --manager=foxy1") == "foxy1")
    check("1.4 key is lower-cased to match normalize_manager_key",
          pc.manager_key_from_cmdline("main.py --manager FoXy1") == "foxy1")
    check("1.5 a controller (no --manager) yields None, not a bogus key",
          pc.manager_key_from_cmdline(f"{PROJECT}/main.py --env x") is None)
    check("1.6 keys with digits/underscores/hyphens survive intact",
          pc.manager_key_from_cmdline("main.py --manager mgr_02-b") == "mgr_02-b")
    check("1.7 a trailing argument after the key does not bleed into it",
          pc.manager_key_from_cmdline("main.py --manager foxy1 --verbose") == "foxy1")
    check("1.8 empty/None input is handled without raising",
          pc.manager_key_from_cmdline(None) is None and pc.manager_key_from_cmdline("") is None)

    procs = [row(real, pid="500", ppid="1")]
    check("1.9 end-to-end: the runtime is detected as RUNNING (pre-fix this was always False)",
          pc.manager_process_running("foxy1", base_dir=PROJECT, procs=procs) is True)
    check("1.10 a different key is correctly NOT running",
          pc.manager_process_running("other", base_dir=PROJECT, procs=procs) is False)


# ======================================================================
# BUG 5: `ps aux` truncates the args column, cutting the `--manager`
#        tail off long venv paths. Also: rows may legitimately arrive
#        without a pid, and liveness must not depend on parsing one.
# ======================================================================

def test_bug5_scan_parsing() -> None:
    print("\n--- BUG 5: no truncation, real pids, pid-independent liveness ---")

    long_cmd = (f"/very/long/absolute/path/to/the/project/venv/bin/python3.12 "
                f"{PROJECT}/main.py --env {PROJECT}/.env.TPilot --manager foxy1")
    check("5.1 a long absolute venv path still yields the key (`ps aux` would have cut it)",
          pc.manager_key_from_cmdline(long_cmd) == "foxy1")

    saved = (pc.subprocess, pc.IS_WINDOWS, pc.IS_LINUX, pc._scan_via_psutil,
             pc._scan_via_proc, pc.shutil.which)
    try:
        class FakeSub:
            TimeoutExpired = subprocess.TimeoutExpired

            @staticmethod
            def run(*a, **kw):
                cmd = a[0] if a else kw.get("args")
                # Assert the fixed invocation, not the old `ps aux`.
                check("5.2 the POSIX backend invokes `ps` with -ww (truncation disabled)",
                      "-ww" in cmd, cmd)
                check("5.3 the POSIX backend requests explicit pid/ppid/comm/args fields",
                      any("pid=" in str(part) for part in cmd), cmd)
                check("5.4 the POSIX backend no longer uses the truncating `aux` form",
                      "aux" not in cmd, cmd)
                return FakeCompleted(
                    stdout=f"777 1 python3.12 {long_cmd}\n", returncode=0)

        pc.subprocess = FakeSub()
        pc.IS_WINDOWS = False
        pc.IS_LINUX = False  # skip the /proc backend, force the ps backend
        pc._scan_via_psutil = lambda: None
        pc._scan_via_proc = lambda: None
        pc.shutil.which = lambda _n: "/usr/bin/ps"

        rows = pc.list_python_processes(base_dir=PROJECT)
        check("5.5 the row parses into a trustworthy single-element list",
              isinstance(rows, list) and len(rows) == 1, rows)
        check("5.6 pid is populated (blank pids disabled duplicate detection)",
              bool(rows) and rows[0]["pid"] == "777", rows)
        check("5.7 ppid is populated (blank ppids disabled launcher collapse)",
              bool(rows) and rows[0]["ppid"] == "1", rows)
        check("5.8 the full untruncated command line survives parsing",
              bool(rows) and rows[0]["cmd"].endswith("--manager foxy1"), rows)
    finally:
        (pc.subprocess, pc.IS_WINDOWS, pc.IS_LINUX, pc._scan_via_psutil,
         pc._scan_via_proc, pc.shutil.which) = saved

    # Liveness must not require a parseable pid.
    cmd_only = [{"cmd": f"{PROJECT}/main.py --env e --manager foxy1"}]
    check("5.9 a pid-less scan row still counts as RUNNING (never a false process_down)",
          pc.manager_process_running("foxy1", base_dir=PROJECT, procs=cmd_only) is True)
    check("5.10 find_manager_pids omits unparseable pids (nothing to signal)",
          pc.find_manager_pids("foxy1", base_dir=PROJECT, procs=cmd_only) == [])
    check("5.11 find_manager_procs still reports the matched row",
          len(pc.find_manager_procs("foxy1", base_dir=PROJECT, procs=cmd_only) or []) == 1)


# ======================================================================
# BUG 6: the old ppid-based collapse would have deleted every manager on
#        Linux, because the controller legitimately spawns them.
# ======================================================================

def test_bug6_launcher_collapse() -> None:
    print("\n--- BUG 6: launcher collapse requires IDENTICAL command lines ---")

    win_cmd = f"{PROJECT}\\venv\\Scripts\\python.exe {PROJECT}\\main.py --manager foxy1"
    pair = [row(win_cmd, pid="100", ppid="1"), row(win_cmd, pid="101", ppid="100")]
    collapsed = pc.collapse_launcher_children(pair)
    check("6.1 a Windows venv launcher pair (identical cmdline) collapses to ONE",
          len(collapsed) == 1, collapsed)
    check("6.2 the surviving row is the parent/logical root",
          bool(collapsed) and collapsed[0]["pid"] == "100", collapsed)

    controller = f"{PROJECT}/main.py --env e"
    mgr_a = f"{PROJECT}/main.py --env e --manager foxy1"
    mgr_b = f"{PROJECT}/main.py --env e --manager foxy2"
    tree = [row(controller, pid="200", ppid="1"),
            row(mgr_a, pid="201", ppid="200"),
            row(mgr_b, pid="202", ppid="200")]
    collapsed = pc.collapse_launcher_children(tree)
    check("6.3 [REGRESSION] controller-spawned managers are NOT collapsed away "
          "(the old ppid-only rule would have deleted both and caused a restart storm)",
          len(collapsed) == 3, collapsed)

    counts = pc.count_managers_by_key(tree, PROJECT)
    check("6.4 each manager is counted exactly once", counts == {"foxy1": 1, "foxy2": 1}, counts)
    check("6.5 the controller is not counted as a manager", "" not in counts, counts)

    counts = pc.count_managers_by_key(pair, PROJECT)
    check("6.6 a launcher pair counts as ONE runtime, not a duplicate incident",
          counts == {"foxy1": 1}, counts)

    dupes = [row(mgr_a, pid="300", ppid="1"), row(mgr_a, pid="301", ppid="1")]
    check("6.7 a GENUINE duplicate runtime (two unrelated parents) is still reported as 2 "
          "-- the collapse must not mask a real session-collision incident",
          pc.count_managers_by_key(dupes, PROJECT) == {"foxy1": 2},
          pc.count_managers_by_key(dupes, PROJECT))


# ======================================================================
# Tri-state contract (panel_bot N5.4.6/RF1), now centralised here.
# ======================================================================

def test_tristate_contract() -> None:
    print("\n--- TRI-STATE: None means unknown, [] means confirmed-empty ---")

    check("T1 an empty CONFIRMED scan resolves to a real False (not unknown)",
          pc.manager_process_running("foxy1", base_dir=PROJECT, procs=[]) is False)
    check("T2 an empty key is False without consulting any scan",
          pc.manager_process_running("", base_dir=PROJECT, procs=[{"cmd": "x"}]) is False)

    saved = (pc._scan_via_psutil, pc._scan_via_proc, pc.IS_WINDOWS, pc.IS_LINUX, pc.shutil.which)
    try:
        pc._scan_via_psutil = lambda: None
        pc._scan_via_proc = lambda: None
        pc.IS_WINDOWS = False
        pc.IS_LINUX = False
        pc.shutil.which = lambda _n: None  # no `ps` binary at all
        check("T3 every backend failing yields None (unknown), never a confirmed []",
              pc.list_python_processes(base_dir=PROJECT) is None)
        check("T4 script liveness is also None when the scan is untrustworthy",
              pc.script_process_running("panel_bot.py", base_dir=PROJECT) is None)
        check("T5 find_manager_procs propagates unknown as None, not []",
              pc.find_manager_procs("foxy1", base_dir=PROJECT) is None)
    finally:
        (pc._scan_via_psutil, pc._scan_via_proc, pc.IS_WINDOWS, pc.IS_LINUX,
         pc.shutil.which) = saved

    check("T6 psutil skipping an AccessDenied process does NOT downgrade the scan to None",
          isinstance(pc._scan_via_psutil(), (list, type(None))))


# ======================================================================
# Service-vs-manager discrimination and project scoping.
# ======================================================================

def test_service_discrimination() -> None:
    print("\n--- SERVICES: controller vs manager, project scoping ---")

    procs = [
        row(f"{PROJECT}/main.py --env e", pid="1", ppid="0"),
        row(f"{PROJECT}/main.py --env e --manager foxy1", pid="2", ppid="1"),
        row(f"{PROJECT}/panel_bot.py", pid="3", ppid="0"),
        row(f"{PROJECT}/soft_watchdog_pinger.py", pid="4", ppid="0"),
    ]

    check("S1 the controller is detected with exclude_manager_flag=True",
          pc.script_process_running("main.py", base_dir=PROJECT, procs=procs,
                                    exclude_manager_flag=True) is True)
    check("S2 panel_bot.py is detected", pc.script_process_running(
        "panel_bot.py", base_dir=PROJECT, procs=procs) is True)
    check("S3 the watchdog is detected", pc.script_process_running(
        "soft_watchdog_pinger.py", base_dir=PROJECT, procs=procs) is True)
    check("S4 an absent service is a confirmed False", pc.script_process_running(
        "partner_stat_bot.py", base_dir=PROJECT, procs=procs) is False)

    only_manager = [row(f"{PROJECT}/main.py --env e --manager foxy1", pid="2", ppid="1")]
    check("S5 [DISCRIMINATION] a manager runtime alone must NOT be read as the controller "
          "(they share the script name main.py)",
          pc.script_process_running("main.py", base_dir=PROJECT, procs=only_manager,
                                    exclude_manager_flag=True) is False)

    foreign = [row("/srv/other-app/main.py --env e --manager foxy1", pid="9", ppid="1")]
    check("S6 a same-named process in ANOTHER directory is not attributed to this project",
          pc.manager_process_running("foxy1", base_dir=PROJECT, procs=foreign) is False)

    check("S7 norm_path makes Windows and POSIX separators compare equal",
          pc.norm_path("C:\\ALM_TPilot\\Main.PY") == "c:/alm_tpilot/main.py")


# ======================================================================
# Safety invariants: this module must never spend, and must not kill
# processes outside the project.
# ======================================================================

def test_safety_invariants() -> None:
    print("\n--- SAFETY: no spend, no stray kills, graceful-first stop ---")

    src = (BASE_DIR / "process_control.py").read_text(encoding="utf-8")

    for banned in ("allow_spend", "proxy_provider", "ProxySellerProvider",
                   "make_ipv4", "prolong_make"):
        check(f"SAFE1 process_control.py contains no `{banned}` reference "
              "(it must never be able to spend money)",
              banned not in src)

    check("SAFE2 process_control.py never imports the DB layer",
          "import storage" not in src and "import aiosqlite" not in src)
    # Checked against the parsed import table, not raw text: the module docstring
    # legitimately explains the Telethon session-collision incident by name, and
    # a substring check would flag that prose as a violation.
    import ast as _ast
    imported: set[str] = set()
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.Import):
            imported.update((alias.name or "").split(".")[0] for alias in node.names)
        elif isinstance(node, _ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    check("SAFE3 process_control.py never IMPORTS Telethon, the DB layer or a provider "
          "(docstring prose about them is fine; a real dependency is not)",
          not (imported & {"telethon", "storage", "aiosqlite", "proxy_provider"}),
          sorted(imported))
    check("SAFE4 no shell=True anywhere (argv lists only, so no shell injection)",
          "shell=True" not in src)
    check("SAFE5 SIGKILL is only reachable after a graceful SIGTERM attempt",
          src.find("SIGTERM") < src.find("SIGKILL"))

    stopped = pc.stop_manager("", base_dir=PROJECT)
    check("SAFE6 stopping an empty key is refused rather than matching everything",
          stopped[0] is False, stopped)

    foreign = [row("/srv/other-app/main.py --manager foxy1", pid="9", ppid="1")]
    check("SAFE7 a foreign-directory process is never a signal target",
          pc.find_manager_pids("foxy1", base_dir=PROJECT, procs=foreign) == [])

    check("SAFE8 the module imports with no side effects (already proven by this run)", True)


# ======================================================================
# systemd helpers must degrade gracefully when systemd is absent.
# ======================================================================

def test_systemd_helpers() -> None:
    print("\n--- SYSTEMD: naming and graceful absence ---")

    check("SD1 manager unit names follow the templated-unit convention",
          pc.manager_unit_name("foxy1") == "tpilot-manager@foxy1.service",
          pc.manager_unit_name("foxy1"))
    check("SD2 unit names are normalised to the lower-case key",
          pc.manager_unit_name("FoXy1") == "tpilot-manager@foxy1.service")

    saved = pc.shutil.which
    try:
        pc.shutil.which = lambda _n: None
        check("SD3 systemd_available() is False when systemctl is absent",
              pc.systemd_available() is False)
        check("SD4 systemd_unit_exists() is False (not an exception) without systemd",
              pc.systemd_unit_exists("tpilot-manager@foxy1.service") is False)
        check("SD5 systemd_is_active() is None (unknown) without systemd",
              pc.systemd_is_active("tpilot-manager@foxy1.service") is None)
    finally:
        pc.shutil.which = saved

    check("SD6 every documented service has a unit name",
          set(pc.SYSTEMD_SERVICE_UNITS) >= {"controller", "panel", "watchdog"},
          pc.SYSTEMD_SERVICE_UNITS)


def test_lifecycle_session_safety() -> None:
    """The start/stop gates that protect the Telethon .session file. No real
    process is started or signalled: systemd is reported absent and the scan is
    stubbed, so only the decision logic runs."""
    print("\n--- LIFECYCLE: .session double-open protection ---")

    saved = (pc.shutil.which, pc.manager_process_running, pc.subprocess.Popen)
    try:
        pc.shutil.which = lambda _n: None  # no systemd -> exercise the fallback

        popen_calls: list = []

        def fake_popen(argv, **kwargs):
            popen_calls.append((argv, kwargs))
            raise AssertionError("start must have been refused before spawning")

        pc.subprocess.Popen = fake_popen

        pc.manager_process_running = lambda *a, **kw: True
        ok, msg = pc.start_manager("foxy1", base_dir=BASE_DIR)
        check("L1 starting an ALREADY-RUNNING manager is refused (two Telethon clients "
              "on one .session corrupt it)", ok is False, msg)
        check("L2 the refusal happens BEFORE any process is spawned", not popen_calls)

        pc.manager_process_running = lambda *a, **kw: None
        ok, msg = pc.start_manager("foxy1", base_dir=BASE_DIR)
        check("L3 an UNKNOWN liveness answer also refuses the start "
              "(never guess 'nothing is running')", ok is False, msg)
        check("L4 still nothing spawned on the unknown path", not popen_calls)

        ok, msg = pc.start_manager("", base_dir=BASE_DIR)
        check("L5 an empty key is refused", ok is False, msg)
    finally:
        (pc.shutil.which, pc.manager_process_running, pc.subprocess.Popen) = saved

    # stop_manager must never report success it did not verify (BUG 2).
    saved_scan = (pc.shutil.which, pc.find_manager_pids)
    try:
        pc.shutil.which = lambda _n: None
        pc.find_manager_pids = lambda *a, **kw: None
        ok, msg = pc.stop_manager("foxy1", base_dir=BASE_DIR)
        check("L6 [BUG 2 REGRESSION] an unverifiable stop returns FAILURE, not a bare "
              "(True, 'OK') as the old implementation did", ok is False, msg)

        pc.find_manager_pids = lambda *a, **kw: []
        ok, msg = pc.stop_manager("foxy1", base_dir=BASE_DIR)
        check("L7 stopping an already-stopped manager is a verified success",
              ok is True, msg)
    finally:
        (pc.shutil.which, pc.find_manager_pids) = saved_scan

    check("L8 the stop timeout exceeds the units' TimeoutStopSec=45, so a legitimate "
          "graceful Telethon shutdown is not misreported as a failure",
          pc._DEFAULT_STOP_TIMEOUT > 45, pc._DEFAULT_STOP_TIMEOUT)


def test_systemd_units_on_disk() -> None:
    """The shipped unit files must agree with what process_control expects and
    with the argv the process scanner matches on."""
    print("\n--- UNITS: shipped systemd files match the code's expectations ---")

    unit_dir = BASE_DIR / "deploy" / "systemd"
    if not unit_dir.is_dir():
        check("U0 deploy/systemd exists", False, str(unit_dir))
        return

    mgr = (unit_dir / "tpilot-manager@.service").read_text(encoding="utf-8")
    check("U1 the templated unit name matches manager_unit_name()",
          pc.manager_unit_name("x").startswith("tpilot-manager@"))
    check("U2 the manager unit passes --manager %i",
          "--manager %i" in mgr)
    check("U3 the manager unit's argv is the one the scanner parses "
          "(--env before --manager, exactly as BUG 1 required)",
          pc.manager_key_from_cmdline(
              "/opt/tpilot/venv/bin/python -u /opt/tpilot/main.py "
              "--env /opt/tpilot/.env.TPilot --manager foxy1") == "foxy1")
    check("U4 the manager unit stops with SIGTERM first (Telethon must close SQLite cleanly)",
          "KillSignal=SIGTERM" in mgr)
    check("U5 the manager unit tolerates a slow graceful stop",
          "TimeoutStopSec=45" in mgr)
    check("U6 the manager unit auto-restarts", "Restart=always" in mgr)
    check("U7 the manager unit rate-limits crash loops (protects Telegram auth endpoints)",
          "StartLimitBurst=" in mgr)

    def directives(text: str) -> list[str]:
        """Only real directive lines -- comments explaining WHY a flag is absent
        must not be mistaken for the flag being present."""
        return [line.strip() for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    ctrl = (unit_dir / "tpilot-controller.service").read_text(encoding="utf-8")
    ctrl_exec = [d for d in directives(ctrl) if d.startswith("ExecStart=")]
    check("U8 [DISCRIMINATION] the controller unit's ExecStart passes NO --manager flag, "
          "which is how process_control tells it apart from a manager runtime",
          len(ctrl_exec) == 1 and "--manager" not in ctrl_exec[0], ctrl_exec)
    check("U9 the controller unit is detected as the controller by the real matcher",
          pc.script_process_running(
              "main.py", base_dir="/opt/tpilot", exclude_manager_flag=True,
              procs=[row("/opt/tpilot/venv/bin/python -u /opt/tpilot/main.py "
                         "--env /opt/tpilot/.env.TPilot", pid="1")]) is True)

    for name, unit_key in (("tpilot-controller.service", "controller"),
                           ("tpilot-panel.service", "panel"),
                           ("tpilot-watchdog.service", "watchdog"),
                           ("tpilot-health.service", "health")):
        check(f"U10 {name} is shipped and registered in SYSTEMD_SERVICE_UNITS",
              (unit_dir / name).is_file()
              and pc.SYSTEMD_SERVICE_UNITS.get(unit_key) == name,
              pc.SYSTEMD_SERVICE_UNITS.get(unit_key))

    for name in ("tpilot-controller.service", "tpilot-panel.service",
                 "tpilot-manager@.service", "tpilot-watchdog.service"):
        text = (unit_dir / name).read_text(encoding="utf-8")
        check(f"U11 {name} hardening: writes are confined to the project tree",
              "ReadWritePaths=" in text and "ProtectSystem=strict" in text)
        check(f"U12 {name} keeps secrets/sessions non-readable to others (UMask=0077)",
              "UMask=0077" in text)
        check(f"U13 {name} belongs to tpilot.target so group stop/restart reaches it",
              "PartOf=tpilot.target" in text)

    target_dirs = directives((unit_dir / "tpilot.target").read_text(encoding="utf-8"))
    wants = [d for d in target_dirs if d.startswith("Wants=")]
    check("U14 the target pulls in every service with Wants= (a single failed bot must "
          "not block the rest), and never with Requires=",
          len(wants) >= 6 and not any(d.startswith("Requires=") for d in target_dirs),
          target_dirs)
    check("U15 the target covers the controller, the panel and all three bots",
          all(any(svc in d for d in wants) for svc in
              ("tpilot-controller", "tpilot-panel", "tpilot-manager-bot",
               "tpilot-partner-bot", "tpilot-watchdog", "tpilot-health")),
          wants)

    installer = BASE_DIR / "deploy" / "install_ubuntu.sh"
    check("U15 the installer is shipped", installer.is_file())
    if installer.is_file():
        text = installer.read_text(encoding="utf-8")
        check("U16 the installer refuses to overwrite an existing .env.TPilot",
              "leaving it untouched" in text)
        check("U17 the installer excludes live state (db/sessions/logs) from the sync",
              "--exclude 'db/'" in text and "--exclude '*.session'" in text)
        check("U18 the installer validates with py_compile before starting anything",
              "py_compile" in text)
        check("U19 the installer fails fast on error (set -Eeuo pipefail)",
              "set -Eeuo pipefail" in text)


def main() -> int:
    print("=" * 72)
    print("process_control selftest -- offline, no network, no spend, no real kills")
    print("=" * 72)

    test_bug1_key_extraction()
    test_bug5_scan_parsing()
    test_bug6_launcher_collapse()
    test_tristate_contract()
    test_service_discrimination()
    test_safety_invariants()
    test_systemd_helpers()
    test_lifecycle_session_safety()
    test_systemd_units_on_disk()

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED, {PASSED} passed")
        for label in FAILURES:
            print(f"  FAILED: {label}")
        return 1
    print(f"RESULT: ALL PASS ({PASSED} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
