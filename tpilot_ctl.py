#!/usr/bin/env python3
"""tpilot-ctl -- single operator entrypoint for the whole TPilot stack.

UBUNTU MIGRATION STAGE 2: replaces all 30 .bat/.ps1 scripts with one CLI that
behaves identically on Ubuntu and Windows.

    tpilot-ctl start-all              # was start_everything.ps1 / start_all.bat
    tpilot-ctl stop-all               # was stop_everything.ps1 / stop_all.bat
    tpilot-ctl restart-all            # was restart_everything.bat
    tpilot-ctl start|stop|restart <service>
    tpilot-ctl start-manager <key>    # was start_manager.ps1  (exit 0/3/1)
    tpilot-ctl stop-manager <key>     # was stop_manager.bat
    tpilot-ctl restart-manager <key>  # was restart_manager.bat
    tpilot-ctl status                 # process + unit overview
    tpilot-ctl doctor                 # preflight report

WHAT IS DELIBERATELY *NOT* SIMPLIFIED
-------------------------------------
start_everything.ps1 encoded three startup rules that came out of real
incidents. All three are preserved in start_all() below, and each is marked in
place:

  1. Zero active managers is NOT a failure -- core services must still come up so
     an admin can re-enable a manager through the panel.
  2. A manager-scoped failure (exit 3) skips that ONE manager; the rest of the
     stack still starts.
  3. But if EVERY manager fails manager-scoped, that is almost certainly a shared
     regression the per-manager check could not distinguish -- so it ABORTS
     rather than reporting success with nothing actually running.

Exit codes: 0 success, 1 failure. `start-manager` additionally propagates 3 for a
manager-scoped failure, so scripts can branch on it exactly as before.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import manager_launcher  # noqa: E402
import manager_registry  # noqa: E402
import process_control  # noqa: E402

# Startup order matters and is not cosmetic:
#   - the controller executes the panel_commands queue, so it comes first;
#   - the watchdog decides whether things are alive, so it comes LAST, after the
#     runtimes it judges have had a chance to settle.
SERVICE_START_ORDER = ("controller", "manager-bot", "health", "panel", "partner-bot", "watchdog")
SERVICE_STOP_ORDER = tuple(reversed(SERVICE_START_ORDER))

SERVICE_UNITS: Dict[str, str] = {
    "controller": "tpilot-controller.service",
    "panel": "tpilot-panel.service",
    "manager-bot": "tpilot-manager-bot.service",
    "partner-bot": "tpilot-partner-bot.service",
    "watchdog": "tpilot-watchdog.service",
    "health": "tpilot-health.service",
}

# Fallback (no systemd): the script each service runs, and whether it is the
# controller (which takes --env but no --manager).
SERVICE_SCRIPTS: Dict[str, str] = {
    "controller": "main.py",
    "panel": "panel_bot.py",
    "manager-bot": "manager_bot.py",
    "partner-bot": "partner_stat_bot.py",
    "watchdog": "soft_watchdog_pinger.py",
    "health": "health_server.py",
}

ENV_FILE_DEFAULT = os.getenv("TPILOT_ENV_FILE") or ".env.TPilot"


def _c(text: str, code: str) -> str:
    """Colourize only for a real TTY, so log files and journald stay clean."""
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def info(msg: str) -> None:
    print(_c("==>", "1;34"), msg, flush=True)


def ok(msg: str) -> None:
    print(_c("  OK", "1;32"), msg, flush=True)


def warn(msg: str) -> None:
    print(_c("  WARN", "1;33"), msg, flush=True)


def fail(msg: str) -> None:
    print(_c("  FAIL", "1;31"), msg, file=sys.stderr, flush=True)


def _env_path(env_file: str) -> str:
    path = Path(env_file)
    return str(path if path.is_absolute() else BASE_DIR / path)


def _active_manager_keys(env_file: str) -> List[str]:
    """Active+enabled manager keys, read through the same helper the rest of the
    project uses (no duplicate SQL, no second notion of 'active')."""
    db_path = manager_registry.resolve_tpilot_db_path(_env_path(env_file))
    rows = manager_registry.list_manager_rows_from_db_sync(
        db_path, only_enabled=True, only_active=True)
    keys = []
    for row in rows:
        key = manager_registry.normalize_manager_key(row.get("manager_key") or "")
        if key:
            keys.append(key)
    return keys


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------

def start_service(name: str, *, env_file: str) -> bool:
    unit = SERVICE_UNITS.get(name)
    script = SERVICE_SCRIPTS.get(name)
    if not unit or not script:
        fail(f"unknown service '{name}' (known: {', '.join(sorted(SERVICE_UNITS))})")
        return False

    if process_control.systemd_unit_exists(unit):
        success, msg = process_control.systemd_start(unit)
        (ok if success else fail)(f"{name}: {msg}")
        return success

    # Fallback: no systemd. Refuse to start a duplicate -- two controllers would
    # both drain the panel_commands queue and execute commands twice.
    running = process_control.script_process_running(
        script, base_dir=BASE_DIR,
        exclude_manager_flag=(name == "controller"))
    if running is None:
        fail(f"{name}: cannot determine whether it is already running -- not starting")
        return False
    if running:
        ok(f"{name}: already running")
        return True

    args = ["--env", _env_path(env_file)] if name == "controller" else []
    success, msg = process_control.start_script(
        script, base_dir=BASE_DIR, args=args,
        log_path=str(BASE_DIR / "logs" / f"{name}.log"))
    (ok if success else fail)(f"{name}: {msg}")
    return success


def stop_service(name: str) -> bool:
    unit = SERVICE_UNITS.get(name)
    script = SERVICE_SCRIPTS.get(name)
    if not unit or not script:
        fail(f"unknown service '{name}'")
        return False

    if process_control.systemd_unit_exists(unit):
        success, msg = process_control.systemd_stop(unit)
        (ok if success else fail)(f"{name}: {msg}")
        return success

    success, msg = process_control.stop_script(
        script, base_dir=BASE_DIR,
        exclude_manager_flag=(name == "controller"))
    (ok if success else fail)(f"{name}: {msg}")
    return success


# --------------------------------------------------------------------------
# managers
# --------------------------------------------------------------------------

def start_manager(key: str, *, env_file: str, timeout: int) -> int:
    """Start one manager and verify readiness. Returns manager_launcher's exit
    code (0 / 3 / 1) so callers keep the original branching contract."""
    outcome = manager_launcher.launch_and_verify(
        key, base_dir=BASE_DIR, env_file=_env_path(env_file), poll_max_sec=timeout)
    line = outcome.log_line(key)
    (ok if outcome.ok else (warn if outcome.isolatable else fail))(line)
    return outcome.exit_code


def stop_manager(key: str) -> bool:
    normalized = manager_registry.normalize_manager_key(key or "")
    if not normalized:
        fail("empty manager key")
        return False
    success, msg = process_control.stop_manager(normalized, base_dir=BASE_DIR)
    (ok if success else fail)(f"manager {normalized}: {msg}")
    return success


# --------------------------------------------------------------------------
# whole-stack operations
# --------------------------------------------------------------------------

def stop_all() -> int:
    info("stopping managers")
    keys = process_control.running_manager_keys(base_dir=BASE_DIR)
    if keys is None:
        fail("cannot enumerate running managers -- refusing to report a clean stop")
        return 1
    failures = [k for k in keys if not stop_manager(k)]
    if not keys:
        ok("no manager runtimes were running")

    info("stopping services")
    for name in SERVICE_STOP_ORDER:
        if not stop_service(name):
            failures.append(name)

    if failures:
        fail(f"could not stop: {', '.join(failures)}")
        return 1
    info("STOP ALL OK")
    return 0


def start_all(*, env_file: str, timeout: int) -> int:
    info(f"START ALL (base={BASE_DIR})")

    try:
        keys = _active_manager_keys(env_file)
    except Exception as exc:
        fail(f"cannot read the manager registry: {exc!r}")
        return 1

    # RULE 1 (TPILOT RESTART ISOLATION 20260809): zero active managers is NOT a
    # startup failure. Core services must still come up so an admin can add or
    # re-enable a manager through the panel. This was originally a fatal throw.
    if not keys:
        warn("no active managers in the registry -- continuing with core services only")

    if not start_service("controller", env_file=env_file):
        fail("controller did not start -- aborting")
        return 1

    skipped: List[str] = []
    for key in keys:
        code = start_manager(key, env_file=env_file, timeout=timeout)
        if code == manager_launcher.EXIT_OK:
            continue
        # RULE 2: a manager-scoped failure skips only that manager.
        if code == manager_launcher.EXIT_MANAGER_SCOPED:
            skipped.append(key)
            continue
        # A global failure means shared breakage (bad shared code, missing venv,
        # broken .env): starting the remaining managers would just crash-loop
        # every one of them.
        fail(f"manager {key} failed with a global/shared error (exit {code}) -- "
             f"aborting startup; see logs/manager_{key}.launcher.log")
        return 1

    # RULE 3: if EVERY manager was isolated as manager-scoped, that is far more
    # likely one shared regression than N independent faults. Reporting SUCCESS
    # with zero managers actually running is the failure mode this prevents.
    if keys and len(skipped) == len(keys):
        fail(f"all {len(keys)} manager(s) failed to start (manager-scoped each) -- "
             f"treating as a shared regression, aborting. Skipped: {', '.join(skipped)}")
        return 1

    for name in SERVICE_START_ORDER:
        if name == "controller":
            continue
        if not start_service(name, env_file=env_file):
            fail(f"service {name} did not start -- aborting")
            return 1

    if skipped:
        info(f"START ALL OK (skipped: {', '.join(skipped)})")
    else:
        info("START ALL OK")
    return 0


def status(*, env_file: str) -> int:
    procs = process_control.list_python_processes(base_dir=BASE_DIR)
    if procs is None:
        # The tri-state contract surfaces here too: never print a confident
        # "everything is down" when the scan itself failed.
        fail("process scan unavailable -- status UNKNOWN (not 'stopped')")
        return 1

    info("services")
    for name in SERVICE_START_ORDER:
        unit = SERVICE_UNITS[name]
        if process_control.systemd_unit_exists(unit):
            active = process_control.systemd_is_active(unit)
            label = "UNKNOWN" if active is None else ("running" if active else "stopped")
            print(f"    {name:<12} {label:<8} (systemd: {unit})")
            continue
        alive = process_control.script_process_running(
            SERVICE_SCRIPTS[name], base_dir=BASE_DIR, procs=procs,
            exclude_manager_flag=(name == "controller"))
        label = "UNKNOWN" if alive is None else ("running" if alive else "stopped")
        print(f"    {name:<12} {label:<8} (no unit; process scan)")

    info("managers")
    counts = process_control.count_managers_by_key(procs, BASE_DIR)
    try:
        registered = _active_manager_keys(env_file)
    except Exception as exc:
        warn(f"registry unreadable ({exc!r}) -- showing running processes only")
        registered = []

    for key in sorted(set(registered) | set(counts)):
        n = counts.get(key, 0)
        if n == 0:
            label = "stopped"
        elif n == 1:
            label = "running"
        else:
            # Surfaced loudly: two runtimes on one .session is the corruption
            # scenario the whole Stage 1 fix exists to prevent.
            label = f"DUPLICATE x{n}"
        suffix = "" if key in registered else "  (not active in registry)"
        print(f"    {key:<12} {label}{suffix}")
    if not registered and not counts:
        print("    (none)")
    return 0


def doctor(*, env_file: str) -> int:
    """Run the preflight report. Kept as a thin wrapper so operators have one
    entrypoint instead of remembering a second script name."""
    try:
        import preflight_check
    except Exception as exc:
        fail(f"cannot import preflight_check: {exc!r}")
        return 1
    runner = getattr(preflight_check, "main", None)
    if not callable(runner):
        fail("preflight_check has no main()")
        return 1
    try:
        return int(runner() or 0)
    except SystemExit as exc:  # argparse-style exits inside preflight
        return int(exc.code or 0)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tpilot-ctl",
        description="Operate the TPilot stack (replaces the .bat/.ps1 scripts).")
    parser.add_argument("--env", default=ENV_FILE_DEFAULT,
                        help=f"env file (default {ENV_FILE_DEFAULT})")
    parser.add_argument("--timeout", type=int, default=manager_launcher.POLL_MAX_SEC,
                        help="manager readiness poll window in seconds")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("start-all", help="start the whole stack, with startup isolation")
    sub.add_parser("stop-all", help="stop the whole stack")
    sub.add_parser("restart-all", help="stop-all then start-all")
    sub.add_parser("status", help="show service and manager state")
    sub.add_parser("doctor", help="run the preflight report")

    for name in ("start", "stop", "restart"):
        p = sub.add_parser(name, help=f"{name} one service")
        p.add_argument("service", choices=sorted(SERVICE_UNITS))

    for name in ("start-manager", "stop-manager", "restart-manager"):
        p = sub.add_parser(name, help=f"{name.replace('-', ' ')}")
        p.add_argument("manager_key")

    sub.add_parser("list-managers", help="print active manager keys")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command

    if cmd == "start-all":
        return start_all(env_file=args.env, timeout=args.timeout)
    if cmd == "stop-all":
        return stop_all()
    if cmd == "restart-all":
        rc = stop_all()
        if rc != 0:
            fail("stop-all failed -- not starting again (a half-stopped stack must "
                 "not be restarted on top of itself)")
            return rc
        return start_all(env_file=args.env, timeout=args.timeout)

    if cmd == "status":
        return status(env_file=args.env)
    if cmd == "doctor":
        return doctor(env_file=args.env)

    if cmd == "start":
        return 0 if start_service(args.service, env_file=args.env) else 1
    if cmd == "stop":
        return 0 if stop_service(args.service) else 1
    if cmd == "restart":
        if not stop_service(args.service):
            return 1
        return 0 if start_service(args.service, env_file=args.env) else 1

    if cmd == "start-manager":
        # Propagates 3 for a manager-scoped failure -- callers branch on it.
        return start_manager(args.manager_key, env_file=args.env, timeout=args.timeout)
    if cmd == "stop-manager":
        return 0 if stop_manager(args.manager_key) else 1
    if cmd == "restart-manager":
        if not stop_manager(args.manager_key):
            return 1
        return start_manager(args.manager_key, env_file=args.env, timeout=args.timeout)

    if cmd == "list-managers":
        try:
            for key in _active_manager_keys(args.env):
                print(key)
        except Exception as exc:
            fail(f"cannot read the manager registry: {exc!r}")
            return 1
        return 0

    fail(f"unhandled command: {cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
