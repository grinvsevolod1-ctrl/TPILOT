# -*- coding: utf-8 -*-
"""tools/hnv2_startstatus_evidence_selftest.py -- offline self-test for the
Health Notification System V2 D1 fix: authoritative last-exit evidence must
survive a respawn.

Before the fix, `_m212a_write_start_status(key, "starting")` hard-wrote
`reason_class=""` and `error=""` on every phase transition (main.py, the
`payload` literal), so the health aggregator lost the `session_unauthorized`
evidence the moment a manager respawned. The fix adds three STICKY,
ADDITIVE keys -- `last_exit_reason_class`, `last_exit_error`,
`last_exit_at` -- that are only ever advanced by an 'exited' write and
otherwise survive every subsequent write untouched, plus a `respawn_seq`
counter and a one-time backfill for legacy files.

This extracts the REAL `_m212a_write_start_status` (main.py, single def --
locked by tools/manager_runtime_start_selftest.py's own static check) via
ast.parse + ast.unparse + exec, the established technique for testing code
that cannot be imported directly. `__file__` in the exec namespace is
pointed at a path inside a throwaway temp directory, so the function's own
`base_dir = dirname(abspath(__file__))` computation resolves entirely
within the sandbox -- this NEVER touches the real C:\\ALM_TPilot\\runtime\\
directory, honoring the "runtime\\ is protected" constraint.
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()


def _extract_def(src: str, name: str) -> ast.AST:
    tree = ast.parse(src)
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    assert len(defs) == 1, f"expected exactly one top-level def of {name!r}, found {len(defs)}"
    return defs[0]


def build_write_start_status_fn(fake_main_file: str):
    """Extracts the real _m212a_write_start_status and execs it in a fresh
    namespace whose __file__ points at `fake_main_file` (inside a temp
    sandbox) -- so its internal `dirname(abspath(__file__))` resolves to
    the sandbox, never the real repo."""
    node = _extract_def(MAIN_SRC, "_m212a_write_start_status")
    src = ast.unparse(node)
    ns = {"__file__": fake_main_file, "__name__": "<hnv2_startstatus_evidence>"}
    exec(compile(src, f"<{MAIN_PATH}:hnv2_startstatus_evidence>", "exec"), ns)
    return ns["_m212a_write_start_status"]


def _selftest_runtime_guard(fake_main_file: str, tmp_root: Path) -> None:
    """Mandatory production-path guard, adapted for this test's sandbox
    convention (there is no DB_PATH here -- the resource being protected is
    C:\\ALM_TPilot\\runtime\\, not a DB file)."""
    real_runtime = os.path.abspath(os.path.join(str(BASE_DIR), "runtime"))
    sandbox_runtime = os.path.abspath(os.path.join(os.path.dirname(fake_main_file), "runtime"))
    assert sandbox_runtime != real_runtime and not sandbox_runtime.startswith(real_runtime + os.sep), \
        f"refusing to run selftest against the real runtime dir: {sandbox_runtime}"
    assert str(tmp_root) in sandbox_runtime, (tmp_root, sandbox_runtime)


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="hnv2_startstatus_selftest_"))
    fake_main_file = str(tmp_root / "fake_main.py")
    return tmp_root, fake_main_file


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def _status_path(fake_main_file: str, key: str) -> Path:
    return Path(os.path.dirname(fake_main_file)) / "runtime" / "managers" / key.lower() / "start_status.json"


def _read_status(fake_main_file: str, key: str) -> dict:
    p = _status_path(fake_main_file, key)
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f) or {}


# ======================================================================
# Group 1: the exact respawn sequence from the plan --
# exited(session_unauthorized) -> starting -> starting -> connected --
# last_exit_reason_class must survive all three subsequent writes.
# ======================================================================

def test_group_1_evidence_survives_respawn():
    print("\n-- Group 1: last-exit evidence survives respawn --")
    tmp_root, fake_main_file = make_temp_env()
    _selftest_runtime_guard(fake_main_file, tmp_root)
    try:
        write = build_write_start_status_fn(fake_main_file)
        key = "support"

        # 1. exited with session_unauthorized (this is exactly what
        #    _m212a_fail_startup writes).
        write(key, "exited", reason_class="session_unauthorized",
              error="Сессия не авторизована, номер телефона не задан -- нужен вход.")
        s1 = _read_status(fake_main_file, key)
        check("1a. exited write: reason_class present", s1.get("reason_class") == "session_unauthorized", s1)
        check("1b. exited write: last_exit_reason_class mirrors reason_class", s1.get("last_exit_reason_class") == "session_unauthorized", s1)
        check("1c. exited write: last_exit_error captured", "не авторизована" in s1.get("last_exit_error", ""), s1)
        check("1d. exited write: last_exit_at set", bool(s1.get("last_exit_at")), s1)
        check("1e. exited write: respawn_seq starts at 0 (only 'starting' increments it)", s1.get("respawn_seq") == 0, s1)

        # 2. Respawn: phase='starting' -- THIS is the write that used to
        #    destroy the evidence (main.py:38479, _m212a_write_start_status
        #    called with no reason_class/error kwargs).
        write(key, "starting")
        s2 = _read_status(fake_main_file, key)
        check("2a. starting write: reason_class blanked (unchanged legacy semantics)", s2.get("reason_class") == "", s2)
        check("2b. starting write: error blanked (unchanged legacy semantics)", s2.get("error") == "", s2)
        check("2c. THE FIX: last_exit_reason_class survives the starting write", s2.get("last_exit_reason_class") == "session_unauthorized", s2)
        check("2d. THE FIX: last_exit_error survives the starting write", "не авторизована" in s2.get("last_exit_error", ""), s2)
        check("2e. THE FIX: last_exit_at survives the starting write (unchanged from the exited write)", s2.get("last_exit_at") == s1.get("last_exit_at"), (s1, s2))
        check("2f. respawn_seq increments to 1 on a 'starting' write", s2.get("respawn_seq") == 1, s2)

        # 3. A second 'starting' write (e.g. supervisor retried again before
        #    the process reached 'connected') -- evidence must still hold,
        #    and respawn_seq must keep advancing (proves it, not the process
        #    reaching 'connected', is what a verifier can use to detect a
        #    genuinely new spawn attempt).
        write(key, "starting")
        s3 = _read_status(fake_main_file, key)
        check("3a. second starting write: last_exit_reason_class still survives", s3.get("last_exit_reason_class") == "session_unauthorized", s3)
        check("3b. second starting write: respawn_seq advances to 2", s3.get("respawn_seq") == 2, s3)

        # 4. connected -- evidence must STILL be present (a live classifier
        #    reads last_exit_* regardless of current phase; freshness/live
        #    overrides happen in the HNV2 classifier, not by erasing history
        #    here).
        write(key, "connected", last_connected_at="2026-08-07T09:00:00+00:00")
        s4 = _read_status(fake_main_file, key)
        check("4a. connected write: last_exit_reason_class still present", s4.get("last_exit_reason_class") == "session_unauthorized", s4)
        check("4b. connected write: last_connected_at set as usual (unchanged legacy field)", s4.get("last_connected_at") == "2026-08-07T09:00:00+00:00", s4)
        check("4c. connected write: respawn_seq unchanged (only 'starting' advances it)", s4.get("respawn_seq") == 2, s4)
        check("4d. connected write: phase reflects the new phase", s4.get("phase") == "connected", s4)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Group 2: a genuinely NEW exit (e.g. the manager later dies of a real
# process crash) must overwrite the sticky evidence with the new reason --
# it is sticky, not permanently frozen to the first-ever failure.
# ======================================================================

def test_group_2_new_exit_overwrites_sticky_evidence():
    print("\n-- Group 2: a new 'exited' write overwrites the sticky evidence --")
    tmp_root, fake_main_file = make_temp_env()
    try:
        write = build_write_start_status_fn(fake_main_file)
        key = "mgr_recover"

        write(key, "exited", reason_class="session_unauthorized", error="old failure")
        write(key, "starting")
        write(key, "connected", last_connected_at="2026-08-07T09:00:00+00:00")
        write(key, "running")
        s_running = _read_status(fake_main_file, key)
        check("2a-setup. reached running with old sticky evidence still present", s_running.get("last_exit_reason_class") == "session_unauthorized", s_running)

        # A genuinely new crash.
        write(key, "exited", reason_class="proxy_timeout", error="SOCKS handshake failed")
        s_new = _read_status(fake_main_file, key)
        check("2b. a new exited write replaces last_exit_reason_class with the NEW reason", s_new.get("last_exit_reason_class") == "proxy_timeout", s_new)
        check("2c. a new exited write replaces last_exit_error with the NEW error", s_new.get("last_exit_error") == "SOCKS handshake failed", s_new)
        # last_exit_at for a fresh 'exited' write is always `now` at write
        # time (main.py: `_sticky_at = ... now` when phase=="exited"), which
        # this test's fast, sub-second-resolution calls can make identical
        # to the PRIOR exit's timestamp (microsecond=0 truncation) even
        # though it genuinely re-derives each time -- so the real invariant
        # to prove is "last_exit_at equals THIS write's own updated_at", not
        # "differs from the previous write's timestamp".
        check("2d. last_exit_at is re-derived from THIS exit (equals its own updated_at), not stale from the prior exit", s_new.get("last_exit_at") == s_new.get("updated_at"), s_new)

        # And it survives the next starting write, same as before.
        write(key, "starting")
        s_after = _read_status(fake_main_file, key)
        check("2e. the NEW sticky evidence survives the next starting write", s_after.get("last_exit_reason_class") == "proxy_timeout", s_after)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Group 3: one-time backfill for a legacy file written before this fix
# existed (phase='exited' with the old reason_class/error, but no
# last_exit_* keys at all) -- the fix must work on the very first respawn
# after deploy, without waiting for a fresh exit.
# ======================================================================

def test_group_3_legacy_backfill():
    print("\n-- Group 3: one-time backfill on a pre-fix legacy file --")
    tmp_root, fake_main_file = make_temp_env()
    try:
        write = build_write_start_status_fn(fake_main_file)
        key = "mgr_legacy_upgrade"
        path = _status_path(fake_main_file, key)
        os.makedirs(path.parent, exist_ok=True)

        # Simulate a file written by the OLD (pre-fix) code: phase='exited'
        # with reason_class/error populated, but none of the new sticky
        # keys -- exactly what every manager's start_status.json looks like
        # at the moment this fix is deployed.
        legacy_payload = {
            "manager_key": key,
            "phase": "exited",
            "updated_at": "2026-08-07T08:00:00+00:00",
            "pid": 1234,
            "reason_class": "session_unauthorized",
            "error": "legacy pre-fix error text",
            "last_connected_at": "",
            "last_exit_at": "2026-08-07T08:00:00+00:00",
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(legacy_payload, f)

        # First write after the fix is deployed: a respawn ('starting').
        # This must backfill last_exit_* from the legacy reason_class/error
        # BEFORE they get blanked by this same write's own payload.
        write(key, "starting")
        s = _read_status(fake_main_file, key)
        check("3a. backfill: last_exit_reason_class populated from the legacy reason_class", s.get("last_exit_reason_class") == "session_unauthorized", s)
        check("3b. backfill: last_exit_error populated from the legacy error", s.get("last_exit_error") == "legacy pre-fix error text", s)
        check("3c. backfill: last_exit_at populated from the legacy last_exit_at", s.get("last_exit_at") == "2026-08-07T08:00:00+00:00", s)
        check("3d. backfill: respawn_seq starts counting from 0 -> 1 (legacy file had none)", s.get("respawn_seq") == 1, s)

        # The backfill must be ONE-TIME: a second starting write must not
        # re-derive from the now-blanked legacy reason_class/error (which
        # would silently look like it still works by accident) -- it must
        # come from the already-backfilled last_exit_* keys.
        write(key, "starting")
        s2 = _read_status(fake_main_file, key)
        check("3e. backfilled evidence survives a second respawn same as freshly-written evidence would", s2.get("last_exit_reason_class") == "session_unauthorized", s2)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Group 4: downstream readers still parse the new file correctly.
# preflight_check._read_start_status and soft_watchdog_pinger's inline
# reader are both plain `json.load(...).get(...)` with no schema
# validation -- this proves that concretely against a REAL file produced
# by the real (extracted) writer, not just by inspection.
# ======================================================================

def test_group_4_downstream_readers_unaffected():
    print("\n-- Group 4: downstream readers tolerate the new additive keys --")
    tmp_root, fake_main_file = make_temp_env()
    try:
        write = build_write_start_status_fn(fake_main_file)
        key = "mgr_reader_check"
        write(key, "exited", reason_class="proxy_timeout", error="SOCKS handshake failed")
        write(key, "starting")

        path = _status_path(fake_main_file, key)

        # preflight_check._read_start_status body, verbatim (main.py cannot
        # be imported, but this one is a plain json.load + .get() -- proving
        # the shape parses is sufficient; the function's own logic is
        # untouched by this change).
        with open(path, "r", encoding="utf-8") as f:
            pf_view = json.load(f) or {}
        check("4a. preflight_check-style plain json.load succeeds on the new file shape", isinstance(pf_view, dict) and pf_view, pf_view)
        check("4b. preflight_check-style read still sees reason_class as before ('' after a starting write)", pf_view.get("reason_class") == "", pf_view)
        check("4c. preflight_check-style read can additionally see the new last_exit_reason_class if it chooses to", pf_view.get("last_exit_reason_class") == "proxy_timeout", pf_view)

        # soft_watchdog_pinger's inline reader: `rc = str(ss.get('reason_class') or '').strip()`
        rc = str(pf_view.get("reason_class") or "").strip()
        check("4d. soft_watchdog_pinger-style `ss.get('reason_class')` still works unchanged (empty after 'starting', as always)", rc == "", rc)

        # And the classic _manager_recovery_read_start_status style (plain
        # json.load, no .get() defaults needed since it returns the dict
        # directly) also just works.
        check("4e. a raw dict consumer sees all legacy keys present and unchanged in type", isinstance(pf_view.get("phase"), str) and isinstance(pf_view.get("pid"), int), pf_view)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Group 5: static -- confirms this fix is the ONLY change to the function
# (single def, matches the plan's "in-place edit" requirement, not a new
# stacked override) and that the sandbox guard technique used above is
# sound (the real BASE_DIR is never touched).
# ======================================================================

def _find_payload_dict(fn_node: ast.AST) -> ast.Dict:
    """Locates the `payload: dict = {...}` assignment inside the function
    body and returns its Dict AST node -- structural lookup, not a string
    match, so it is immune to ast.unparse's quote-style normalization."""
    for n in ast.walk(fn_node):
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == "payload" and isinstance(n.value, ast.Dict):
            return n.value
    raise AssertionError("could not locate the `payload: dict = {...}` literal")


def test_group_5_static():
    print("\n-- Group 5: static checks --")
    tree = ast.parse(MAIN_SRC)
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_m212a_write_start_status"]
    check("5a. _m212a_write_start_status is still defined exactly once (in-place edit, not a new override)", len(defs) == 1, len(defs))
    src = ast.unparse(defs[0]) if defs else ""
    check("5b. the active def contains the new sticky last_exit_reason_class key", "last_exit_reason_class" in src, None)
    check("5c. the active def contains the new sticky last_exit_error key", "last_exit_error" in src, None)
    check("5d. the active def contains the respawn_seq counter", "respawn_seq" in src, None)

    payload_dict = _find_payload_dict(defs[0]) if defs else None
    literal = {}
    if payload_dict is not None:
        for k_node, v_node in zip(payload_dict.keys, payload_dict.values):
            if isinstance(k_node, ast.Constant) and isinstance(v_node, ast.Constant):
                literal[k_node.value] = v_node.value
    check("5e. reason_class/error are still blanked as literal empty strings in the payload dict (legacy semantics preserved)",
          literal.get("reason_class") == "" and literal.get("error") == "", literal)


def main() -> int:
    test_group_1_evidence_survives_respawn()
    test_group_2_new_exit_overwrites_sticky_evidence()
    test_group_3_legacy_backfill()
    test_group_4_downstream_readers_unaffected()
    test_group_5_static()

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
