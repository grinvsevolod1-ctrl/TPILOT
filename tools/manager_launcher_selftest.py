"""Offline selftest for the startup-isolation contract (manager_launcher.py).

No network, no spend, no real processes, no real sleeping: temp dirs for
start_status.json and monkeypatched liveness/stop hooks.

WHY THIS SUITE IS STRICT
------------------------
The contract it guards decides, on every startup, whether one broken manager is
skipped or the whole stack is aborted. Getting it wrong in the permissive
direction is the expensive one: a shared regression misread as "6 independent
manager-scoped failures" would report SUCCESS with nothing running. So every
gate is asserted to FAIL CLOSED (exit 1 / global), and the "all managers
skipped" rule is asserted separately.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import manager_launcher as ml  # noqa: E402
import manager_registry  # noqa: E402
import process_control  # noqa: E402
import tpilot_ctl  # noqa: E402

_checks = 0
_failures = []


def check(cond, label):
    global _checks
    _checks += 1
    if cond:
        print(f"  ok  {label}")
    else:
        print(f"  FAIL {label}")
        _failures.append(label)


def write_status(root: Path, key: str, **fields):
    """Write a start_status.json exactly where main.py writes it."""
    d = root / "runtime" / "managers" / key
    d.mkdir(parents=True, exist_ok=True)
    path = d / "start_status.json"
    payload = {"manager_key": key, "phase": "starting",
               "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_raw(root: Path, key: str, text: str):
    d = root / "runtime" / "managers" / key
    d.mkdir(parents=True, exist_ok=True)
    path = d / "start_status.json"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
print("== resolve_start_failure: every gate must fail closed to GLOBAL ==")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    key = "foxy1"
    launch = datetime.now(timezone.utc)

    # Gate 1: no file at all.
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "missing status file -> global")
    check("no start_status.json" in r.detail, "missing file names the reason")

    # Gate 2: unparsable / truncated (the mid-write shape of a shared regression).
    write_raw(root, key, '{"manager_key": "foxy1", "pha')
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "truncated JSON -> global")

    write_raw(root, key, "")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "empty file -> global")

    write_raw(root, key, "[1,2,3]")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "JSON that is not an object -> global")

    # Gate 3: phase not one main.py writes.
    write_status(root, key, phase="bogus")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "unknown phase -> global")

    write_status(root, key, phase="")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "empty phase -> global")

    # Gate 4: file belongs to another manager.
    write_status(root, key, phase="exited", manager_key="someoneelse")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "foreign manager_key -> global")

    # Gate 5: unparsable updated_at.
    write_status(root, key, phase="exited", updated_at="not-a-date")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "unparsable updated_at -> global")

    # A naive (offset-less) timestamp must be rejected, not assumed UTC: assuming
    # would reintroduce the Kyiv-offset bug that let stale files look fresh.
    write_status(root, key, phase="exited",
                 updated_at=datetime.now().replace(microsecond=0).isoformat())
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "offset-less updated_at -> global (not assumed UTC)")

    # Gate 6: stale file from a previous run.
    stale = (launch - timedelta(minutes=30)).replace(microsecond=0).isoformat()
    write_status(root, key, phase="exited", updated_at=stale)
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "stale updated_at -> global")
    check("stale" in r.detail, "stale case names the reason")

    # Just inside the 5s slack: still isolatable (second-level truncation).
    fresh = (launch - timedelta(seconds=3)).replace(microsecond=0).isoformat()
    write_status(root, key, phase="exited", updated_at=fresh,
                 reason_class="session_unauthorized")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_MANAGER_SCOPED, "3s-old file inside slack -> manager-scoped")

    # Just outside the slack.
    edge = (launch - timedelta(seconds=20)).replace(microsecond=0).isoformat()
    write_status(root, key, phase="exited", updated_at=edge)
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_GLOBAL, "20s-old file outside slack -> global")

print()
print("== valid evidence grants isolation; reason_class only LABELS ==")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    key = "foxy1"
    launch = datetime.now(timezone.utc)

    for rc in ("session_unauthorized", "auth_session", "proxy_timeout",
               "connection_error", "process_exited", "telegram_identity_mismatch"):
        write_status(root, key, phase="exited", reason_class=rc)
        r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
        check(r.exit_code == ml.EXIT_MANAGER_SCOPED, f"known reason_class {rc} -> manager-scoped")
        check(r.classification == "manager_scoped", f"{rc} labelled manager_scoped")

    # THE CENTRAL RULE: an UNRECOGNIZED reason_class still gets isolation,
    # because the file's existence -- not its contents -- is the evidence that
    # the shared startup boundary was crossed. classification only labels it.
    write_status(root, key, phase="exited", reason_class="brand_new_thing")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_MANAGER_SCOPED,
          "UNRECOGNIZED reason_class still -> manager-scoped (file existence is the evidence)")
    check(r.classification == "unknown", "unrecognized reason_class labelled 'unknown'")

    # 'unknown' must never be pre-approved in the registry's taxonomy.
    check(manager_registry.classify_start_failure("unknown") == "unknown",
          "registry: 'unknown' is not a pre-approved manager-scoped class")
    check("unknown" not in manager_registry.MANAGER_SCOPED_START_FAILURES,
          "registry: 'unknown' absent from MANAGER_SCOPED_START_FAILURES")

    # phase=starting with no reason_class is still isolatable evidence.
    write_status(root, key, phase="starting")
    r = ml.resolve_start_failure(key, base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_MANAGER_SCOPED, "phase=starting -> manager-scoped")
    check("phase=starting" in r.detail, "no reason_class falls back to labelling the phase")

    # Key normalization: the file may carry a differently-cased key.
    write_status(root, key, phase="exited", manager_key="FOXY1")
    r = ml.resolve_start_failure("FOXY1", base_dir=root, launch_utc=launch, detail="d")
    check(r.exit_code == ml.EXIT_MANAGER_SCOPED, "manager_key compared case-insensitively")

print()
print("== log wording can never contradict the exit code (F1 C-1a) ==")

check("SKIPPED MANAGER" in ml.StartOutcome(ml.EXIT_MANAGER_SCOPED, "d", "manager_scoped").log_line("k"),
      "exit 3 prints SKIPPED MANAGER")
check("FAILED GLOBALLY" in ml.StartOutcome(ml.EXIT_GLOBAL, "d").log_line("k"),
      "exit 1 prints FAILED GLOBALLY")
check("SKIPPED" not in ml.StartOutcome(ml.EXIT_GLOBAL, "d").log_line("k"),
      "exit 1 never says SKIPPED (the original C-1a bug)")
check("STARTED MANAGER" in ml.StartOutcome(ml.EXIT_OK, "phase=running").log_line("k"),
      "exit 0 prints STARTED MANAGER")

print()
print("== verify_start: readiness polling ==")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    key = "foxy1"
    launch = datetime.now(timezone.utc)
    orig_running = process_control.manager_process_running
    orig_stop = process_control.stop_manager
    try:
        # Ready immediately.
        for phase in ("connected", "running"):
            write_status(root, key, phase=phase)
            process_control.manager_process_running = lambda *a, **k: True
            r = ml.verify_start(key, base_dir=root, launch_utc=launch,
                                poll_max_sec=3, sleep=lambda *_: None)
            check(r.exit_code == ml.EXIT_OK, f"phase={phase} -> exit 0")

        # Dead process, valid fresh evidence -> isolatable.
        write_status(root, key, phase="starting", reason_class="proxy_timeout")
        process_control.manager_process_running = lambda *a, **k: False
        r = ml.verify_start(key, base_dir=root, launch_utc=launch,
                            poll_max_sec=3, sleep=lambda *_: None)
        check(r.exit_code == ml.EXIT_MANAGER_SCOPED, "died with fresh evidence -> manager-scoped")

        # Dead process, NO evidence -> global.
        (root / "runtime" / "managers" / key / "start_status.json").unlink()
        r = ml.verify_start(key, base_dir=root, launch_utc=launch,
                            poll_max_sec=3, sleep=lambda *_: None)
        check(r.exit_code == ml.EXIT_GLOBAL, "died with no evidence -> global")

        # UNKNOWN liveness must NOT be read as death: a failed scan aborting a
        # healthy startup is the tri-state bug this guards.
        write_status(root, key, phase="starting")
        process_control.manager_process_running = lambda *a, **k: None
        stop_calls = []
        process_control.stop_manager = lambda k_, **kw: (stop_calls.append(k_), (True, "OK"))[1]
        r = ml.verify_start(key, base_dir=root, launch_utc=launch,
                            poll_max_sec=2, sleep=lambda *_: None)
        check(r.exit_code == ml.EXIT_MANAGER_SCOPED,
              "UNKNOWN liveness keeps polling, then times out on its own evidence")
        check(len(stop_calls) == 1,
              "timeout sweeps EVERY matching process, not just the launched pid (F1 H9)")

        # Readiness is checked BEFORE liveness: a manager that reached running
        # and then exited still started successfully.
        write_status(root, key, phase="running")
        process_control.manager_process_running = lambda *a, **k: False
        r = ml.verify_start(key, base_dir=root, launch_utc=launch,
                            poll_max_sec=3, sleep=lambda *_: None)
        check(r.exit_code == ml.EXIT_OK, "running-then-exited still counts as started")
    finally:
        process_control.manager_process_running = orig_running
        process_control.stop_manager = orig_stop

print()
print("== launch_and_verify: an unverified stop must block the start ==")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "main.py").write_text("# stub\n", encoding="utf-8")
    orig_stop = process_control.stop_manager
    orig_start = process_control.start_manager
    try:
        started = []
        process_control.start_manager = lambda k, **kw: (started.append(k), (True, "OK"))[1]

        process_control.stop_manager = lambda k, **kw: (False, "не подтверждена")
        r = ml.launch_and_verify("foxy1", base_dir=root, poll_max_sec=1)
        check(r.exit_code == ml.EXIT_GLOBAL, "unverified stop -> global failure")
        check(not started, "unverified stop never reaches start (protects .session)")
        check(".session" in r.detail, "the refusal explains the .session risk")

        # A spawn failure is global, not isolatable: it points at the venv or
        # entrypoint, which would break every other manager too.
        process_control.stop_manager = lambda k, **kw: (True, "OK")
        process_control.start_manager = lambda k, **kw: (False, "no venv")
        r = ml.launch_and_verify("foxy1", base_dir=root, poll_max_sec=1)
        check(r.exit_code == ml.EXIT_GLOBAL, "spawn failure -> global (not isolatable)")

        # Empty key is rejected before anything else happens.
        r = ml.launch_and_verify("", base_dir=root, poll_max_sec=1)
        check(r.exit_code == ml.EXIT_GLOBAL, "empty manager key -> global")
    finally:
        process_control.stop_manager = orig_stop
        process_control.start_manager = orig_start

print()
print("== stale status file is cleared before a launch ==")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    p = write_status(root, "foxy1", phase="exited")
    check(p.exists(), "status file exists before clear")
    ml.clear_start_status("foxy1", base_dir=root)
    check(not p.exists(), "clear_start_status removes it")
    ml.clear_start_status("foxy1", base_dir=root)  # must not raise
    check(True, "clearing an absent file is a no-op, never raises")

print()
print("== start_all: the three startup rules from start_everything.ps1 ==")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    orig_keys = tpilot_ctl._active_manager_keys
    orig_start_service = tpilot_ctl.start_service
    orig_start_manager = tpilot_ctl.start_manager
    try:
        services = []
        tpilot_ctl.start_service = lambda n, **kw: (services.append(n), True)[1]

        # RULE 1: zero active managers is not a failure -- core still comes up.
        tpilot_ctl._active_manager_keys = lambda env_file: []
        tpilot_ctl.start_manager = lambda k, **kw: ml.EXIT_OK
        services.clear()
        rc = tpilot_ctl.start_all(env_file=".env", timeout=1)
        check(rc == 0, "zero active managers -> success (not fatal)")
        check("controller" in services and "panel" in services,
              "core services still start with zero managers")

        # RULE 2: one manager-scoped failure among several is skipped.
        tpilot_ctl._active_manager_keys = lambda env_file: ["a", "b", "c"]
        tpilot_ctl.start_manager = lambda k, **kw: (
            ml.EXIT_MANAGER_SCOPED if k == "b" else ml.EXIT_OK)
        services.clear()
        rc = tpilot_ctl.start_all(env_file=".env", timeout=1)
        check(rc == 0, "one manager-scoped failure -> startup continues")
        check("watchdog" in services, "remaining services still start after a skip")

        # A global failure aborts immediately and does NOT start the rest.
        tpilot_ctl.start_manager = lambda k, **kw: (
            ml.EXIT_GLOBAL if k == "a" else ml.EXIT_OK)
        services.clear()
        rc = tpilot_ctl.start_all(env_file=".env", timeout=1)
        check(rc == 1, "a global manager failure aborts the startup")
        check("watchdog" not in services, "abort happens before the later services start")

        # RULE 3: ALL managers manager-scoped -> treat as shared regression.
        tpilot_ctl.start_manager = lambda k, **kw: ml.EXIT_MANAGER_SCOPED
        services.clear()
        rc = tpilot_ctl.start_all(env_file=".env", timeout=1)
        check(rc == 1, "every manager skipped -> abort (likely a shared regression)")
        check("watchdog" not in services,
              "never reports success with zero managers actually running")

        # Controller failing aborts before any manager is touched.
        attempted = []
        tpilot_ctl.start_manager = lambda k, **kw: (attempted.append(k), ml.EXIT_OK)[1]
        tpilot_ctl.start_service = lambda n, **kw: n != "controller"
        rc = tpilot_ctl.start_all(env_file=".env", timeout=1)
        check(rc == 1, "controller failure aborts")
        check(not attempted, "no manager is started when the controller failed")

        # A registry read error is fatal, not silently "zero managers".
        tpilot_ctl.start_service = lambda n, **kw: True
        def boom(env_file):
            raise RuntimeError("db gone")
        tpilot_ctl._active_manager_keys = boom
        rc = tpilot_ctl.start_all(env_file=".env", timeout=1)
        check(rc == 1, "unreadable registry -> abort (never treated as zero managers)")
    finally:
        tpilot_ctl._active_manager_keys = orig_keys
        tpilot_ctl.start_service = orig_start_service
        tpilot_ctl.start_manager = orig_start_manager

print()
print("== stop_all: a failed scan must not report a clean stop ==")

orig_running_keys = process_control.running_manager_keys
orig_stop_service = tpilot_ctl.stop_service
try:
    tpilot_ctl.stop_service = lambda n: True
    process_control.running_manager_keys = lambda **kw: None
    rc = tpilot_ctl.stop_all()
    check(rc == 1, "UNKNOWN manager scan -> stop_all fails loudly")

    process_control.running_manager_keys = lambda **kw: []
    rc = tpilot_ctl.stop_all()
    check(rc == 0, "genuinely empty manager list -> clean stop")
finally:
    process_control.running_manager_keys = orig_running_keys
    tpilot_ctl.stop_service = orig_stop_service

print()
print("== restart-all must not start on top of a half-stopped stack ==")

orig_stop_all = tpilot_ctl.stop_all
orig_start_all = tpilot_ctl.start_all
try:
    calls = []
    tpilot_ctl.stop_all = lambda: (calls.append("stop"), 1)[1]
    tpilot_ctl.start_all = lambda **kw: (calls.append("start"), 0)[1]
    rc = tpilot_ctl.main(["restart-all"])
    check(rc == 1, "restart-all propagates a stop failure")
    check("start" not in calls, "restart-all does not start after a failed stop")
finally:
    tpilot_ctl.stop_all = orig_stop_all
    tpilot_ctl.start_all = orig_start_all

print()
print("== controller vs manager: same script name, different processes ==")

check(tpilot_ctl.SERVICE_SCRIPTS["controller"] == "main.py",
      "controller runs main.py (shared with every manager runtime)")
rows = [
    {"pid": "1", "ppid": "0", "comm": "python", "cmd": "/opt/tpilot/venv/bin/python /opt/tpilot/main.py --env /opt/tpilot/.env.TPilot"},
    {"pid": "2", "ppid": "1", "comm": "python", "cmd": "/opt/tpilot/venv/bin/python /opt/tpilot/main.py --env /opt/tpilot/.env.TPilot --manager foxy1"},
]
check(process_control.script_process_running("main.py", base_dir="/opt/tpilot", procs=rows,
                                             exclude_manager_flag=True) is True,
      "controller detected with exclude_manager_flag")
only_manager = [rows[1]]
check(process_control.script_process_running("main.py", base_dir="/opt/tpilot", procs=only_manager,
                                             exclude_manager_flag=True) is False,
      "a manager runtime alone is NOT mistaken for the controller")
check(process_control.script_process_running("main.py", base_dir="/opt/tpilot", procs=only_manager,
                                             exclude_manager_flag=False) is True,
      "without the flag the same row does match main.py")

print()
print("== stop_script: the controller stop must not sweep managers ==")

import inspect  # noqa: E402
src = inspect.getsource(process_control.stop_script)
check("exclude_manager_flag" in src, "stop_script honours exclude_manager_flag")
check("остановка не подтверждена" in src, "stop_script fails closed on an unreadable scan")
check("matched_without_pid" in src, "a pid-less match still blocks a confident clean stop")

sig = inspect.signature(tpilot_ctl.stop_service)
check("name" in sig.parameters, "stop_service takes a service name")
csrc = inspect.getsource(tpilot_ctl.stop_service)
check('exclude_manager_flag=(name == "controller")' in csrc,
      "tpilot_ctl passes exclude_manager_flag ONLY for the controller")

print()
print("== CLI surface replaces the .bat/.ps1 set ==")

parser = tpilot_ctl.build_parser()
for cmd in ("start-all", "stop-all", "restart-all", "status", "doctor",
            "start", "stop", "restart", "start-manager", "stop-manager",
            "restart-manager", "list-managers"):
    try:
        parser.parse_args([cmd, "x"] if cmd in
                          ("start", "stop", "restart", "start-manager",
                           "stop-manager", "restart-manager") else [cmd])
        parsed = True
    except SystemExit:
        parsed = cmd in ("start", "stop", "restart")  # invalid service choice is fine
    check(parsed, f"CLI accepts '{cmd}'")

check(set(tpilot_ctl.SERVICE_UNITS) == set(tpilot_ctl.SERVICE_SCRIPTS),
      "every service has both a unit and a fallback script")
check(tpilot_ctl.SERVICE_START_ORDER[0] == "controller",
      "controller starts first (it drains the panel_commands queue)")
check(tpilot_ctl.SERVICE_START_ORDER[-1] == "watchdog",
      "watchdog starts last (it judges the runtimes it must not race)")
check(tpilot_ctl.SERVICE_STOP_ORDER[0] == "watchdog",
      "watchdog stops first (so it cannot resurrect what we are stopping)")

print()
print("== exit codes are the documented public contract ==")
check((ml.EXIT_OK, ml.EXIT_MANAGER_SCOPED, ml.EXIT_GLOBAL) == (0, 3, 1),
      "0=ok, 3=manager-scoped, 1=global (matches start_manager.ps1)")

print()
print("=" * 62)
if _failures:
    print(f"FAILED {len(_failures)}/{_checks}")
    for f in _failures:
        print(f"  - {f}")
    raise SystemExit(1)
print(f"ALL {_checks} CHECKS PASSED")
