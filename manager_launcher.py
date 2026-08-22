"""Manager start verification + startup-failure isolation, ported from
start_manager.ps1 (UBUNTU MIGRATION STAGE 2).

WHY THIS MODULE EXISTS
----------------------
start_manager.ps1 was not "a launcher script". It carried the project's
**startup isolation contract**, built up through a series of production
incidents (TPILOT RESTART ISOLATION 20260809, TPILOT F1 SAFETY 20260810 C-1a and
H9). Deleting the .ps1 in favour of `systemctl start` would have silently thrown
that contract away: systemd with Type=simple reports success the moment the
process is forked, long before Telethon has connected, and it has no notion of
"this failure is scoped to one manager" vs "the shared codebase is broken".

The contract, preserved here verbatim in its decision structure:

  exit 0  STARTED         -- start_status.json reached phase connected/running.
  exit 3  MANAGER-SCOPED  -- this one manager failed; the caller SKIPS it and
                             continues starting the others.
  exit 1  GLOBAL          -- shared/global failure; the caller ABORTS the whole
                             startup instead of crash-looping every manager.

THE CENTRAL RULE (do not "simplify" this)
-----------------------------------------
Isolation is granted on the EXISTENCE AND INTEGRITY of start_status.json, never
on whether its reason_class is recognized. main.py writes that file from inside
main() *before* asyncio.run(main()), so its mere presence already proves the
shared runtime -- imports, .env, venv -- loaded successfully for this process. A
failure recorded after that point can only be scoped to this one manager.

classify_start_failure() is used ONLY to label the log line. It must never
influence the exit code. A truncated, foreign or stale status file proves
nothing and therefore FAILS CLOSED to global (exit 1): a process that died
mid-write is exactly the shape a shared regression takes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import manager_registry
import process_control

__all__ = [
    "EXIT_OK",
    "EXIT_GLOBAL",
    "EXIT_MANAGER_SCOPED",
    "POLL_MAX_SEC",
    "STALE_SLACK_SEC",
    "VALID_PHASES",
    "StartOutcome",
    "read_start_status",
    "resolve_start_failure",
    "start_status_path",
    "verify_start",
    "launch_and_verify",
]

# Exit codes are a PUBLIC contract: tpilot_ctl and any operator script branch on
# them. 3 (not 2) for manager-scoped, matching start_manager.ps1 exactly.
EXIT_OK = 0
EXIT_GLOBAL = 1
EXIT_MANAGER_SCOPED = 3

POLL_MAX_SEC = int(os.getenv("TPILOT_START_POLL_MAX_SEC") or "60")
POLL_INTERVAL_SEC = 1.0

# 5s of slack absorbs updated_at's second-level truncation. A genuinely stale
# file is minutes or hours old -- orders of magnitude beyond this.
STALE_SLACK_SEC = 5

# The phases main.py actually writes. Seeing one of these is the proof that the
# shared startup boundary was crossed.
VALID_PHASES = ("starting", "connected", "running", "exited")
READY_PHASES = ("connected", "running")


class StartOutcome:
    """Result of a start verification: an exit code plus operator-facing text."""

    def __init__(self, exit_code: int, detail: str, classification: str = "global",
                 phase: str = "", reason_class: str = "") -> None:
        self.exit_code = int(exit_code)
        self.detail = str(detail or "")
        self.classification = str(classification or "")
        self.phase = str(phase or "")
        self.reason_class = str(reason_class or "")

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK

    @property
    def isolatable(self) -> bool:
        """True when the caller may skip this manager and keep going."""
        return self.exit_code == EXIT_MANAGER_SCOPED

    def log_line(self, manager_key: str) -> str:
        """Single place that turns an outcome into text.

        TPILOT F1 SAFETY 20260810 (C-1a): the wording can never contradict the
        exit code again. Exit 1 used to be printed as "SKIPPED MANAGER" even
        though nothing was skipped -- the whole startup was about to abort."""
        if self.exit_code == EXIT_OK:
            return f"STARTED MANAGER {manager_key} {self.detail}".rstrip()
        if self.exit_code == EXIT_MANAGER_SCOPED:
            return f"SKIPPED MANAGER {manager_key} ({self.classification}): {self.detail}"
        return f"MANAGER {manager_key} FAILED GLOBALLY (aborting startup): {self.detail}"


def start_status_path(manager_key: str, *, base_dir: Any) -> Path:
    """Location main.py writes: runtime/managers/<key>/start_status.json."""
    key = manager_registry.normalize_manager_key(manager_key or "")
    return Path(str(base_dir)) / "runtime" / "managers" / key / "start_status.json"


def read_start_status(manager_key: str, *, base_dir: Any) -> Optional[Dict[str, Any]]:
    """Parse start_status.json. None means absent/unreadable/not-an-object.

    None is deliberately indistinguishable from "malformed" here: both are
    treated as no evidence by resolve_start_failure, which is what makes the
    fail-closed rule work."""
    path = start_status_path(manager_key, base_dir=base_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def clear_start_status(manager_key: str, *, base_dir: Any) -> None:
    """Remove a stale status file before a launch. Best-effort, never raises --
    exactly like the .ps1's try/catch. resolve_start_failure therefore cannot
    assume this succeeded, which is why the freshness gate below exists."""
    try:
        start_status_path(manager_key, base_dir=base_dir).unlink()
    except Exception:
        pass


def _parse_updated_at(raw: Any) -> Optional[datetime]:
    """Parse start_status.json's updated_at into an aware UTC datetime.

    main.py writes datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    i.e. an offset-aware ISO-8601 string. A value WITHOUT an offset is rejected
    rather than assumed to be UTC: guessing the zone is how the .ps1 originally
    mis-read local time as UTC (Kyiv +3) and could see a stale file as fresh."""
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except Exception:
            return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def resolve_start_failure(
    manager_key: str,
    *,
    base_dir: Any,
    launch_utc: datetime,
    detail: str,
) -> StartOutcome:
    """Decide whether a start failure is isolatable (exit 3) or global (exit 1).

    Ported gate-for-gate from Resolve-StartFailureExit in start_manager.ps1.
    Every gate FAILS CLOSED to global: isolation is a privilege granted only by
    positive, self-consistent, fresh evidence that this specific manager got
    past the shared startup boundary."""
    key = manager_registry.normalize_manager_key(manager_key or "")

    # (1)+(2) The file must exist AND parse. A truncated/garbage/empty file
    # proves NOTHING -- a process that died mid-write is exactly the shape a
    # shared regression takes.
    status = read_start_status(key, base_dir=base_dir)
    if status is None:
        if not start_status_path(key, base_dir=base_dir).exists():
            return StartOutcome(
                EXIT_GLOBAL,
                f"{detail} -- no start_status.json (shared/global failure)")
        return StartOutcome(
            EXIT_GLOBAL,
            f"{detail} -- status file unreadable or invalid JSON "
            "(treated as shared/global, not isolatable)")

    phase = str(status.get("phase") or "").strip().lower()
    reason_class = str(status.get("reason_class") or "").strip()
    error = str(status.get("error") or "")

    # (3) The phase must be one main.py actually writes -- the proof that the
    # shared startup boundary was crossed.
    if phase not in VALID_PHASES:
        return StartOutcome(
            EXIT_GLOBAL,
            f"{detail} -- status file has no valid phase ('{phase}') "
            "(treated as shared/global, not isolatable)")

    # (4) It must belong to THIS manager. Both sides are normalized lowercase.
    file_key = manager_registry.normalize_manager_key(status.get("manager_key") or "")
    if file_key != key:
        return StartOutcome(
            EXIT_GLOBAL,
            f"{detail} -- status file belongs to '{file_key}', not '{key}' "
            "(treated as shared/global, not isolatable)")

    # (5)+(6) It must belong to THIS launch attempt. The stale-file delete is
    # best-effort, so a leftover file from a previous run can survive; without
    # this gate an old, unrelated failure could isolate a manager that never
    # even started now.
    updated = _parse_updated_at(status.get("updated_at"))
    if updated is None:
        return StartOutcome(
            EXIT_GLOBAL,
            f"{detail} -- status file has unparsable updated_at "
            f"('{status.get('updated_at')}') (treated as shared/global, not isolatable)")
    if updated < launch_utc - timedelta(seconds=STALE_SLACK_SEC):
        return StartOutcome(
            EXIT_GLOBAL,
            f"{detail} -- status file is stale (updated_at={status.get('updated_at')} "
            "predates this launch) (treated as shared/global, not isolatable)")

    # Only here is manager-scoped isolation legitimate. reason_class still does
    # NOT decide the exit code -- it only labels the log line.
    label = f"reason_class={reason_class} error={error}" if reason_class else f"phase={phase}"
    classification = manager_registry.classify_start_failure(reason_class) or "unknown"
    return StartOutcome(EXIT_MANAGER_SCOPED, f"{detail} -- {label}",
                        classification=classification, phase=phase,
                        reason_class=reason_class)


def verify_start(
    manager_key: str,
    *,
    base_dir: Any,
    launch_utc: datetime,
    poll_max_sec: int = POLL_MAX_SEC,
    sleep: Any = time.sleep,
    now: Any = None,
) -> StartOutcome:
    """Poll start_status.json until the manager is ready, dead, or the window
    expires. `sleep`/`now` are injectable so selftests never wait in real time.

    Success (exit 0) is granted ONLY on a confirmed connected/running phase --
    never on "the process still seems to be alive"."""
    key = manager_registry.normalize_manager_key(manager_key or "")
    clock = now or (lambda: datetime.now(timezone.utc))
    deadline_polls = max(1, int(poll_max_sec))

    for elapsed in range(1, deadline_polls + 1):
        sleep(POLL_INTERVAL_SEC)

        status = read_start_status(key, base_dir=base_dir)
        phase = str((status or {}).get("phase") or "").strip().lower()

        # Readiness is checked BEFORE liveness: a manager that reached
        # connected/running and then exited a moment later still started
        # successfully, and reporting that as a startup failure would make the
        # caller kill a healthy respawn.
        if phase in READY_PHASES:
            return StartOutcome(EXIT_OK, f"phase={phase} elapsed={elapsed}s",
                                classification="started", phase=phase)

        if phase == "exited":
            # The file parsed here, but resolve_start_failure re-validates
            # ownership and freshness too -- it can still return global.
            return resolve_start_failure(key, base_dir=base_dir, launch_utc=launch_utc,
                                         detail="exited during startup")

        # Liveness. UNKNOWN (None) is NOT treated as death: a failed scan must
        # never be allowed to abort a healthy startup.
        alive = process_control.manager_process_running(key, base_dir=base_dir)
        if alive is False:
            return resolve_start_failure(key, base_dir=base_dir, launch_utc=launch_utc,
                                         detail="process exited during startup")
        # phase=starting, or no file yet, or unknown liveness: keep polling.

    # Window expired without ever reaching connected/running.
    #
    # TPILOT F1 SAFETY 20260810 (H9): stop EVERY process matching this manager,
    # not just the pid we launched. On Windows the launcher pid differs from the
    # real runtime child; leaving that child alive while printing SKIPPED
    # MANAGER left a live Telethon session behind and directly contradicted the
    # F1 contract. process_control.stop_manager sweeps all matches on both
    # platforms.
    process_control.stop_manager(key, base_dir=base_dir)
    return resolve_start_failure(
        key, base_dir=base_dir, launch_utc=launch_utc,
        detail=f"did not reach connected/running within {deadline_polls}s")


def launch_and_verify(
    manager_key: str,
    *,
    base_dir: Any,
    env_file: str = ".env.TPilot",
    poll_max_sec: int = POLL_MAX_SEC,
) -> StartOutcome:
    """Full launch: stop leftovers, clear stale status, start, verify readiness.

    A spawn failure is GLOBAL, not isolatable: being unable to start a process
    at all points at the venv, the entrypoint or the unit file -- shared things
    that will break every other manager too."""
    key = manager_registry.normalize_manager_key(manager_key or "")
    if not key:
        return StartOutcome(EXIT_GLOBAL, "manager key is empty")

    base = Path(str(base_dir))
    log_dir = base / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        (base / "runtime" / "managers" / key).mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return StartOutcome(EXIT_GLOBAL, f"cannot create runtime directories: {exc!r}")

    # Stop any existing runtime first. A stop we cannot VERIFY is fatal: starting
    # anyway would put two Telethon clients on one .session file.
    stopped, stop_msg = process_control.stop_manager(key, base_dir=base)
    if not stopped:
        return StartOutcome(
            EXIT_GLOBAL,
            f"could not confirm the previous runtime was stopped ({stop_msg}) -- "
            "refusing to start a second one on the same .session")

    clear_start_status(key, base_dir=base)

    # Anchor freshness AFTER the stop and BEFORE the start, in real UTC. The
    # .ps1 originally compared against a local-time string with "UTC" glued on,
    # which was off by the Kyiv offset and could accept a stale file.
    launch_utc = datetime.now(timezone.utc)

    started, start_msg = process_control.start_manager(
        key, base_dir=base, env_file=env_file,
        log_path=str(log_dir / f"manager_{key}.launcher.log"))
    if not started:
        return StartOutcome(EXIT_GLOBAL, f"failed to spawn: {start_msg}")

    return verify_start(key, base_dir=base, launch_utc=launch_utc,
                        poll_max_sec=poll_max_sec)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="manager_launcher.py",
        description="Start one manager runtime and verify it reached readiness. "
                    "Exit codes: 0 started, 3 manager-scoped failure (skip it and "
                    "continue), 1 global failure (abort the startup).")
    parser.add_argument("manager_key")
    parser.add_argument("--env", default=".env.TPilot",
                        help="env file passed through to the runtime")
    parser.add_argument("--base-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--timeout", type=int, default=POLL_MAX_SEC,
                        help=f"readiness poll window in seconds (default {POLL_MAX_SEC})")
    parser.add_argument("--verify-only", action="store_true",
                        help="do not launch; only verify an already-running manager")
    args = parser.parse_args(argv)

    key = manager_registry.normalize_manager_key(args.manager_key or "")
    if not key:
        print("MANAGER KEY IS EMPTY", file=sys.stderr)
        return EXIT_GLOBAL

    if args.verify_only:
        outcome = verify_start(key, base_dir=args.base_dir,
                               launch_utc=datetime.now(timezone.utc),
                               poll_max_sec=args.timeout)
    else:
        outcome = launch_and_verify(key, base_dir=args.base_dir, env_file=args.env,
                                    poll_max_sec=args.timeout)

    print(outcome.log_line(key))
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
