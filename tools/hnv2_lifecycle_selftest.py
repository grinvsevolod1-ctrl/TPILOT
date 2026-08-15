# -*- coding: utf-8 -*-
"""tools/hnv2_lifecycle_selftest.py -- offline self-test for the Health
Notification System V2 persistent incident state machine
(_hnv2_incident_step, _hnv2_drive_observed_open, _hnv2_advance_recovery,
_hnv2_family_recovery_evidence, _hnv2_maybe_notify) -- the Phase 6
orchestration layer built on top of the Phase 1 storage primitives, Phase 4
classifier, and Phase 5 watermark/spawn-verify mechanics.

Covers approved-plan selftest scenarios 7, 8, 9, 10, 11, 14, plus the
REVISED Recovery Ownership Matrix negative/positive test requirements
(owner directive 2026-08-07): for representative families across the
matrix, proves that generic runtime readiness alone can never resolve an
incident whose authoritative signal belongs to another subsystem, and that
each family recovers ONLY from its own owner's evidence.

main.py cannot be imported directly -- extracted via ast.parse +
ast.unparse + exec(), same technique as every other tools/*_selftest.py.
storage.py IS directly importable and used for REAL against a temp SQLite
file. `_manager_runtime_ready_once` and the process probe (`subprocess`)
are faked -- no real Telethon/queue round-trip, no real process spawned or
queried, no network.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR_REAL = Path(__file__).resolve().parent.parent
if str(BASE_DIR_REAL) not in sys.path:
    sys.path.insert(0, str(BASE_DIR_REAL))

import storage  # noqa: E402

FAILURES: list = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR_REAL / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
MAIN_TREE = ast.parse(MAIN_SRC)

REAL_NAMES = {
    "HNV2_ENABLED", "HNV2_ACCOUNT_MAX_AGE_SEC", "HNV2_PROXY_MAX_AGE_SEC",
    "HNV2_STUCK_STARTING_SEC", "HNV2_CONNECTED_MAX_AGE_SEC", "HNV2_EXIT_EVIDENCE_MAX_AGE_SEC",
    "HNV2_STABLE_SEC", "HNV2_STABLE_SEC_SQLITE_LOCK", "HNV2_SPAWN_VERIFY_DELAY_SEC",
    "HNV2_READY_PROBE_MIN_INTERVAL_SEC", "HNV2_DAILY_PROBLEM_CAP",
    "HNV2_UNKNOWN_CONFIRM_SEC", "HNV2_UNKNOWN_MIN_OBSERVATIONS",
    "HNV2_FAMILY_SESSION_UNAUTHORIZED", "HNV2_FAMILY_ACCOUNT_BLOCKED",
    "HNV2_FAMILY_PEERFLOOD", "HNV2_FAMILY_FLOODWAIT", "HNV2_FAMILY_PROXY_AUTH_FAILED",
    "HNV2_FAMILY_PROXY_UNAVAILABLE", "HNV2_FAMILY_SQLITE_LOCK", "HNV2_FAMILY_NETWORK_TIMEOUT",
    "HNV2_FAMILY_WORKER_STUCK_STARTING", "HNV2_FAMILY_WORKER_CRASH", "HNV2_FAMILY_RECOVERY_FLAP",
    "HNV2_FAMILY_HEALTH_MISSING_STALE", "HNV2_FAMILY_OK", "HNV2_FAMILY_UNKNOWN",
    "_HNV2_SESSION_MARKS", "_HNV2_ACCOUNT_BLOCKED_MARKS", "_HNV2_PROXY_AUTH_MARKS",
    "_HNV2_NETWORK_TIMEOUT_MARKS", "_HNV2_SQLITE_LOCK_MARKS", "_HNV2_SQLITE_MALFORMED_MARKS",
    "_HNV2_FLOODWAIT_TEXT_MARKS",
    "_hnv2_process_state", "_hnv2_collect_evidence", "_hnv2_result",
    "_hnv2_classify_root_cause", "_hnv2_normalize_sig_component", "_hnv2_signature",
    "_hnv2_count_new_recovery_events", "_hnv2_recovery_log_len", "_hnv2_spawns_appended_since",
    "_hnv2_last_spawn_verify", "_hnv2_rate_limited_ready_probe", "_hnv2_last_ready_probe",
    "_hnv2_family_recovery_evidence", "_hnv2_notify", "_hnv2_maybe_notify",
    "_hnv2_drive_observed_open", "_hnv2_advance_recovery", "_hnv2_incident_step",
    "_HNV2_CONFIRM_TABLE", "_HNV2_RECOMMENDED_ACTION", "_hnv2_stable_sec_for_family",
    "TP_HG_CHECK_INTERVAL_SEC", "TPAG_V2_MONITOR_INTERVAL_SEC",
    "TP_HG_STATUS_OK", "TP_HG_STATUS_WARNING", "TP_HG_STATUS_LIMITED",
    "TP_HG_STATUS_BLOCKED", "TP_HG_STATUS_UNKNOWN",
    "_tp_hg_parse_iso", "_tp_hg_restriction_family", "_tp_hg_stable_error_text",
    "_TP_HG_FAMILY_PEERFLOOD", "_TP_HG_FAMILY_FLOODWAIT", "_TP_HG_FAMILY_BLOCKED",
    "_TP_HG_FAMILY_WARNING", "_TP_HG_FAMILY_UNKNOWN",
    "HEALTH_AGG_RECOVERY_FLAP_THRESHOLD", "HEALTH_AGG_RECOVERY_WINDOW_MIN",
    "HEALTH_INCIDENT_REMINDER_INTERVAL_SEC",
    "_manager_recovery_log", "_manager_recovery_read_start_status",
    "_health_agg_append_log", "_health_agg_runtime_dir", "_health_agg_utc_now_iso",
    # PeerFlood/lifecycle protected names -- re-verified from THIS file's
    # own extraction too.
    "_tp_hg_send_allowed", "_send_manager_private",
    "_process_profile_reminders_once", "_process_post_manual_followups_once",
    "_health_incident_handle", "_health_incident_notify", "_m212a_write_start_status",
    "_manager_recovery_classify", "_manager_recovery_open_auth_incident",
    "_manager_recovery_resolve_auth_incident",
}


def _extract_by_names(src: str, names: set) -> list:
    tree = ast.parse(src)
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm and nm in names:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names:
            nodes.append(n)
            seen.add(n.targets[0].id)
            continue
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
            nodes.append(n)
            seen.add(n.target.id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing}")
    return nodes


class _FakeReady:
    """Stand-in for _manager_runtime_ready_once. `.default_ok` controls the
    default outcome; per-key overrides via `.per_key`."""
    def __init__(self):
        self.calls: list = []
        self.default_ok = True
        self.per_key: dict = {}

    async def __call__(self, key, *, expected_tgid=0, start_status_max_age_sec=120, ping_timeout_sec=20, heartbeat_advisory=False):
        self.calls.append({"key": key, "heartbeat_advisory": heartbeat_advisory})
        ok = self.per_key.get(key, self.default_ok)
        return {"ok": bool(ok), "error_class": "" if ok else "runtime_not_ready", "detail": "OK" if ok else "not ready", "signal": ""}


class _FakeSubprocessResult:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


class _FakeSubprocessModule:
    """Controls _hnv2_process_state's outcome. `.default_running` sets the
    steady-state answer; `.per_key` overrides for specific manager keys
    (the fake script embeds the key, so we parse it back out of args)."""
    def __init__(self):
        self.run_calls: list = []
        self.default_running = True
        self.per_key: dict = {}

    def run(self, args, **kwargs):
        self.run_calls.append((args, kwargs))
        script = args[-1] if args else ""
        key = ""
        if "$k='" in script:
            key = script.split("$k='", 1)[1].split("'", 1)[0]
        running = self.per_key.get(key, self.default_running)
        return _FakeSubprocessResult(0, "1" if running else "")


def build_ns(fake_base_dir: Path, db_path: str):
    import manager_registry

    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    prod_db_dir = os.path.abspath(os.path.join(str(BASE_DIR_REAL), "db"))
    target = os.path.abspath(db_path)
    assert not (target == prod_db_dir or target.startswith(prod_db_dir + os.sep)), \
        f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"

    nodes = _extract_by_names(MAIN_SRC, REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    fake_ready = _FakeReady()
    fake_subprocess = _FakeSubprocessModule()

    import aiosqlite as _aiosqlite

    import re as _re

    ns = {
        "os": os, "sys": sys, "json": json, "re": _re, "_hnv2_json": json, "_hagg_json": json,
        "subprocess": fake_subprocess, "aiosqlite": _aiosqlite,
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "asyncio": asyncio, "time": __import__("time"),
        "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple,
        "BASE_DIR": fake_base_dir, "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "_manager_runtime_ready_once": fake_ready,
        "_repl_storage": storage,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:hnv2_lifecycle>", "exec"), ns)
    ns["__fake_ready__"] = fake_ready
    ns["__fake_subprocess__"] = fake_subprocess
    return ns


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="hnv2_lifecycle_selftest_"))
    (tmp_root / "runtime").mkdir(parents=True, exist_ok=True)
    db_path = str(tmp_root / "data_tpilot.db")
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS panel_notifications("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL DEFAULT '',"
            " title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',"
            " status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT '',"
            " updated_at TEXT NOT NULL DEFAULT '')"
        )
        con.commit()
    finally:
        con.close()
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


NOW = datetime.now(timezone.utc)


def _fresh_iso(offset_seconds: int = 0) -> str:
    """ALWAYS uses the REAL current time (never the stale module-level
    NOW captured at import time) -- required for any timestamp that must
    compare as newer than an incident's `opened_at`, which storage writes
    using real wall-clock time at the moment the incident actually opens,
    potentially many seconds (an entire test suite run) after this
    module was imported. Using module-level NOW + a small offset for such
    a comparison is fragile: once enough suite time has elapsed, NOW +
    offset can end up BEFORE opened_at, making genuinely-fresh evidence
    look stale."""
    return _iso(datetime.now(timezone.utc) + timedelta(seconds=offset_seconds))


def _write_ss(tmp_root: Path, key: str, ss: dict) -> None:
    d = tmp_root / "runtime" / "managers" / key
    d.mkdir(parents=True, exist_ok=True)
    (d / "start_status.json").write_text(json.dumps(ss), encoding="utf-8")


def _panel_notifications(db_path: str) -> list:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute("SELECT id, kind, title, body FROM panel_notifications ORDER BY id ASC").fetchall()
        return [{"id": r[0], "kind": r[1], "title": r[2], "body": r[3]} for r in rows]
    finally:
        con.close()


def _problem_notifications(db_path: str) -> list:
    return [n for n in _panel_notifications(db_path) if not n["title"].startswith("✅")]


def _recovery_notifications(db_path: str) -> list:
    return [n for n in _panel_notifications(db_path) if n["title"].startswith("✅")]


async def _tick(ns: dict, key: str, mrow: dict, hrow: dict) -> None:
    await ns["_hnv2_incident_step"](key, mrow, hrow)


def _incident_row(db_path: str, key: str, sig: str) -> Optional[dict]:
    return storage.health_incident_v2_get(key, sig, db_path=db_path)


def _backdate_last_notified(db_path: str, key: str, sig: str, seconds_ago: int) -> None:
    ts = (datetime.utcnow() - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()
    con = sqlite3.connect(db_path)
    try:
        con.execute("UPDATE health_incidents SET last_notified_at=? WHERE manager_key=? AND signature=?", (ts, key, sig))
        con.commit()
    finally:
        con.close()


def _backdate_confirm_due(db_path: str, key: str, sig: str, seconds_ago: int) -> None:
    ts = (datetime.utcnow() - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()
    con = sqlite3.connect(db_path)
    try:
        con.execute("UPDATE health_incidents SET confirm_due_at=? WHERE manager_key=? AND signature=?", (ts, key, sig))
        con.commit()
    finally:
        con.close()


def _drive_to_open(ns: dict, db_path: str, key: str, mrow: dict, hrow: dict, family: str, evidence_key: str) -> Optional[dict]:
    """Repeatedly ticks the SAME (family, evidence_key) evidence until the
    incident reaches status='open', honoring BOTH that family's
    CONFIRM_TICKS (observed_count) and CONFIRM_SEC (confirm_due_at) from
    _HNV2_CONFIRM_TABLE -- families other than session_unauthorized/
    account_blocked/worker_stuck_starting/recovery_flap require more than
    one tick (e.g. worker_crash=3, proxy/network/sqlite_lock=4,
    health_missing_stale=10) per the approved plan's CONFIRM_SEC/
    CONFIRM_TICKS table. Backdates confirm_due_at between ticks so the
    test never needs a real sleep. Returns the final row (or None)."""
    confirm_sec, confirm_ticks = ns["_HNV2_CONFIRM_TABLE"].get(family, (0, 1))
    sig = ns["_hnv2_signature"](family, evidence_key)
    row = None
    for _ in range(confirm_ticks + 1):
        asyncio.run(_tick(ns, key, mrow, hrow))
        row = _incident_row(db_path, key, sig)
        if row and row.get("status") == "open":
            return row
        if confirm_sec > 0 and row and row.get("status") == "observed":
            _backdate_confirm_due(db_path, key, sig, confirm_sec + 5)
    return row


# ======================================================================
# Scenario 7/8: max 3 problem notifications per Kyiv calendar day per
# canonical incident, 4th observation silent, quota gate runs BEFORE the
# incident claim (last_notified_at byte-identical after a denied claim).
# ======================================================================

def test_scenario_7_8_daily_cap():
    print("\n-- Scenario 7/8: max 3/day, 4th silent --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr78"
        # session_unauthorized: CONFIRM_TICKS=1/CONFIRM_SEC=0 -- confirms
        # and opens in a single tick, the simplest family for a test that
        # only cares about the QUOTA mechanics, not the classifier.
        ss = {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)}
        _write_ss(tmp_root, key, ss)
        mrow, hrow = {}, {}

        sig = ns["_hnv2_signature"]("session_unauthorized", "session_unauthorized")
        row = _drive_to_open(ns, db_path, key, mrow, hrow, "session_unauthorized", "session_unauthorized")
        check("7a. session_unauthorized confirms and opens in a single tick (confirm_ticks=1)", row and row.get("status") == "open", row)
        check("7b. exactly one problem notification sent so far", len(_problem_notifications(db_path)) == 1, _problem_notifications(db_path))

        # Force two reminders by backdating last_notified_at past the
        # reminder interval, then re-ticking (each re-tick re-derives the
        # SAME family/signature since nothing about the evidence changed).
        for _ in range(2):
            _backdate_last_notified(db_path, key, sig, ns["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"] + 60)
            asyncio.run(_tick(ns, key, mrow, hrow))
        check("7c. three problem notifications total (1 open + 2 reminders)", len(_problem_notifications(db_path)) == 3, _problem_notifications(db_path))

        _backdate_last_notified(db_path, key, sig, ns["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"] + 60)
        before = _panel_notifications(db_path)
        row_before = _incident_row(db_path, key, sig)
        asyncio.run(_tick(ns, key, mrow, hrow))
        after = _panel_notifications(db_path)
        row_after = _incident_row(db_path, key, sig)
        check("8a. the 4th notification attempt is silently denied (still 3 total)", len(_problem_notifications(db_path)) == 3, after)
        check("8b. no new panel_notifications row was created at all", len(after) == len(before), (before, after))
        check("8c. last_notified_at is byte-identical after the denied 4th attempt (quota gate runs BEFORE the incident claim)",
              row_after.get("last_notified_at") == row_before.get("last_notified_at"), (row_before, row_after))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 9: recovery notification does not consume the problem quota.
# ======================================================================

def test_scenario_9_recovery_exempt_from_quota():
    print("\n-- Scenario 9: recovery notification is exempt from the daily quota --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr9"
        sig = ns["_hnv2_signature"]("session_unauthorized", "session_unauthorized")
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)})
        mrow_bad, hrow_bad = {}, {}

        # Exhaust the quota (3 problem notifications).
        asyncio.run(_tick(ns, key, mrow_bad, hrow_bad))
        for _ in range(2):
            _backdate_last_notified(db_path, key, sig, ns["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"] + 60)
            asyncio.run(_tick(ns, key, mrow_bad, hrow_bad))
        check("9-setup. quota exhausted at 3", len(_problem_notifications(db_path)) == 3, None)

        # Now drive it to recovery: a fresh successful re-connect (its own
        # authoritative evidence) + a passing ready-probe.
        opened_row = _incident_row(db_path, key, sig)
        opened_at = opened_row.get("opened_at")
        ns["__fake_ready__"].default_ok = True
        fresh_connected_at = _fresh_iso(5)
        _write_ss(tmp_root, key, {"phase": "connected", "last_connected_at": fresh_connected_at, "pid": 999})
        asyncio.run(_tick(ns, key, mrow_bad, hrow_bad))
        row_rp = _incident_row(db_path, key, sig)
        check("9a. incident enters recovery_pending once positive evidence appears", row_rp.get("status") == "recovery_pending", row_rp)

        # Advance past the stability window by backdating
        # recovery_pending_since, then tick again.
        stable_sec = ns["_hnv2_stable_sec_for_family"]("session_unauthorized")
        con = sqlite3.connect(db_path)
        try:
            past = (datetime.utcnow() - timedelta(seconds=stable_sec + 30)).replace(microsecond=0).isoformat()
            con.execute("UPDATE health_incidents SET recovery_pending_since=? WHERE manager_key=? AND signature=?", (past, key, sig))
            con.commit()
        finally:
            con.close()
        asyncio.run(_tick(ns, key, mrow_bad, {}))
        row_resolved = _incident_row(db_path, key, sig)
        check("9b. incident is RESOLVED after the stability window elapses", row_resolved.get("status") == "resolved", row_resolved)
        recoveries = _recovery_notifications(db_path)
        check("9c. exactly one recovery notification sent", len(recoveries) == 1, recoveries)
        check("9d. quota's sent_count is STILL 3 (recovery consumed nothing)",
              True, None)  # verified structurally below
        kyiv_date = storage.w3_business_date("Europe/Kyiv")
        con = sqlite3.connect(db_path)
        try:
            sent = con.execute("SELECT sent_count FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?", (key, sig, kyiv_date)).fetchone()
        finally:
            con.close()
        check("9e. health_notify_quota.sent_count is exactly 3 after the recovery notify", sent and sent[0] == 3, sent)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 10: a distinct root cause is independently alertable even
# while another signature's quota is fully exhausted.
# ======================================================================

def test_scenario_10_distinct_causes_independent():
    print("\n-- Scenario 10: distinct root causes have independent quotas --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr10"
        ns["__fake_subprocess__"].default_running = False
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "process_exited", "last_exit_at": _iso(NOW)})
        mrow_bad = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}
        row_a = _drive_to_open(ns, db_path, key, mrow_bad, {}, "worker_crash", "process_exited")
        sig_a = ns["_hnv2_signature"]("worker_crash", "process_exited")
        check("10-setup-open. sig_a (worker_crash) reaches OPEN (confirm_ticks=3)", row_a and row_a.get("status") == "open", row_a)
        for _ in range(2):
            _backdate_last_notified(db_path, key, sig_a, ns["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"] + 60)
            asyncio.run(_tick(ns, key, mrow_bad, {}))
        check("10-setup. sig_a (worker_crash) quota exhausted at 3", len(_problem_notifications(db_path)) == 3, None)

        # A DIFFERENT root cause for the SAME manager: session_unauthorized.
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)})
        asyncio.run(_tick(ns, key, {}, {}))
        sig_b = ns["_hnv2_signature"]("session_unauthorized", "session_unauthorized")
        row_b = _incident_row(db_path, key, sig_b)
        check("10a. the distinct cause (session_unauthorized) opens despite sig_a's exhaustion", row_b and row_b.get("status") == "open", row_b)
        check("10b. four total problem notifications now exist (3 from sig_a + 1 from sig_b)", len(_problem_notifications(db_path)) == 4, _problem_notifications(db_path))

        con = sqlite3.connect(db_path)
        try:
            n_quota_rows = con.execute("SELECT COUNT(*) FROM health_notify_quota WHERE manager_key=?", (key,)).fetchone()[0]
        finally:
            con.close()
        check("10c. two independent quota rows exist for this manager (no global cap)", n_quota_rows == 2, n_quota_rows)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 11: a controller restart does not reset the daily quota.
# ======================================================================

def test_scenario_11_restart_persistence():
    print("\n-- Scenario 11: restart does not reset the daily quota --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr11"
        sig = ns["_hnv2_signature"]("session_unauthorized", "session_unauthorized")
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)})
        mrow_bad = {}
        asyncio.run(_tick(ns, key, mrow_bad, {}))
        for _ in range(2):
            _backdate_last_notified(db_path, key, sig, ns["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"] + 60)
            asyncio.run(_tick(ns, key, mrow_bad, {}))
        check("11-setup. quota exhausted at 3", len(_problem_notifications(db_path)) == 3, None)

        # Simulate a controller restart: build a FRESH ns (fresh in-process
        # state, e.g. _hnv2_last_ready_probe/_HNV2_BOOTSTRAP_DONE all reset)
        # against the SAME on-disk DB.
        ns2 = build_ns(tmp_root, db_path)
        _backdate_last_notified(db_path, key, sig, ns2["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"] + 60)
        asyncio.run(_tick(ns2, key, mrow_bad, {}))
        check("11a. the 4th attempt is STILL denied after a simulated restart (quota persisted on disk)", len(_problem_notifications(db_path)) == 3, _problem_notifications(db_path))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 14: manager isolation -- mgr_a's exhausted quota / open
# incident / watermark must never affect mgr_b.
# ======================================================================

def test_scenario_14_manager_isolation():
    print("\n-- Scenario 14: manager A cannot affect manager B --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key_a, key_b = "mgr14a", "mgr14b"
        sig = ns["_hnv2_signature"]("session_unauthorized", "session_unauthorized")
        _write_ss(tmp_root, key_a, {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)})
        mrow_bad = {}
        asyncio.run(_tick(ns, key_a, mrow_bad, {}))
        for _ in range(2):
            _backdate_last_notified(db_path, key_a, sig, ns["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"] + 60)
            asyncio.run(_tick(ns, key_a, mrow_bad, {}))
        check("14-setup. mgr_a quota exhausted, incident open", len(_problem_notifications(db_path)) == 3, None)

        # mgr_b: healthy the whole time.
        _write_ss(tmp_root, key_b, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        ns["__fake_subprocess__"].default_running = True
        hrow_ok = {"health_status": "ok", "last_check_at": _iso(NOW)}
        mrow_ok = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}
        asyncio.run(_tick(ns, key_b, mrow_ok, hrow_ok))

        row_a = _incident_row(db_path, key_a, sig)
        row_b = _incident_row(db_path, key_b, sig)
        check("14a. mgr_a's incident is unaffected by mgr_b's tick", row_a and row_a.get("status") == "open", row_a)
        check("14b. mgr_b has NO incident of the same signature (classified healthy)", row_b is None, row_b)

        con = sqlite3.connect(db_path)
        try:
            b_quota = con.execute("SELECT COUNT(*) FROM health_notify_quota WHERE manager_key=?", (key_b,)).fetchone()[0]
        finally:
            con.close()
        check("14c. mgr_b has zero quota rows (its own state is untouched by mgr_a's exhaustion)", b_quota == 0, b_quota)

        # health_incident_resolve_all_for_manager('mgr_a') must never sweep
        # its hv2:* incident (the legacy-namespace filter isolation proven
        # already at the storage layer -- re-verified end-to-end here).
        n = storage.health_incident_resolve_all_for_manager(key_a, db_path=db_path)
        check("14d. legacy resolve-all-for-manager returns 0 for mgr_a (does not touch hv2:* rows)", n == 0, n)
        row_a_after = _incident_row(db_path, key_a, sig)
        check("14e. mgr_a's hv2:* incident remains OPEN after the legacy sweep", row_a_after.get("status") == "open", row_a_after)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Recovery Ownership Matrix -- REQUIRED NEGATIVE TESTS (owner directive).
# Each: open an incident of the given family, supply the misleading
# "generic readiness" evidence, tick repeatedly, and assert the incident
# STAYS OPEN (never reaches recovery_pending/resolved, zero recovery
# notifications).
# ======================================================================

def test_ownership_negative_account_blocked_runtime_ready_insufficient():
    print("\n-- Ownership-neg: account_blocked + runtime_ready=True but still BLOCKED -> stays OPEN --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_ab_neg"
        hrow = {"health_status": "blocked", "error_class": "UserDeactivatedBanError"}
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        ns["__fake_subprocess__"].default_running = True
        ns["__fake_ready__"].default_ok = True  # runtime_ready=True throughout
        asyncio.run(_tick(ns, key, {}, hrow))
        sig = ns["_hnv2_signature"]("account_blocked", "userdeactivatedbanerror")
        row = _incident_row(db_path, key, sig)
        check("neg-ab-1. account_blocked opens", row and row.get("status") == "open", row)
        for _ in range(5):
            asyncio.run(_tick(ns, key, {}, hrow))  # hrow.health_status stays 'blocked' every tick
        row2 = _incident_row(db_path, key, sig)
        check("neg-ab-2. STILL open after 5 more ticks with runtime_ready=True but health_status still BLOCKED", row2.get("status") == "open", row2)
        check("neg-ab-3. zero recovery notifications", len(_recovery_notifications(db_path)) == 0, _recovery_notifications(db_path))
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_account_blocked_sticky_heartbeat():
    print("\n-- Ownership-neg: account_blocked + sticky-guard heartbeat update (last_check_at moves, last_ok_at does not) -> stays OPEN --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_ab_sticky"
        hrow = {"health_status": "blocked", "error_class": "UserDeactivatedBanError", "last_check_at": _iso(NOW)}
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        asyncio.run(_tick(ns, key, {}, hrow))
        sig = ns["_hnv2_signature"]("account_blocked", "userdeactivatedbanerror")
        # Simulate the REAL sticky-downgrade guard's observable effect:
        # last_check_at keeps advancing every heartbeat, health_status and
        # last_ok_at do NOT move (main.py's _tp_hg_update_status
        # 21163-21171 behaviour).
        for i in range(5):
            hrow["last_check_at"] = _iso(NOW + timedelta(minutes=i + 1))
            asyncio.run(_tick(ns, key, {}, hrow))
        row = _incident_row(db_path, key, sig)
        check("neg-ab-sticky-1. STILL open despite 5 heartbeat-only updates (last_ok_at never moved)", row.get("status") == "open", row)
        check("neg-ab-sticky-2. zero recovery notifications", len(_recovery_notifications(db_path)) == 0, _recovery_notifications(db_path))
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_sqlite_lock_runtime_ready_insufficient():
    print("\n-- Ownership-neg: sqlite_lock + runtime_ready=True but no affirmative non-lock evidence -> stays OPEN --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_lock_neg"
        ss = {"phase": "exited", "last_exit_error": "sqlite3.OperationalError: database is locked", "last_exit_at": _iso(NOW)}
        _write_ss(tmp_root, key, ss)
        ns["__fake_subprocess__"].default_running = True
        ns["__fake_ready__"].default_ok = True
        row = _drive_to_open(ns, db_path, key, {}, {}, "sqlite_lock", "locked")
        sig = ns["_hnv2_signature"]("sqlite_lock", "locked")
        check("neg-lock-1. sqlite_lock opens (confirm_ticks=4)", row and row.get("status") == "open", row)
        # runtime_ready stays True the whole time, phase flips to running,
        # but hrow.last_check_at is NEVER supplied (no affirmative DB
        # write evidence) -- must never resolve on readiness alone.
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        for _ in range(5):
            asyncio.run(_tick(ns, key, {}, {}))
        row2 = _incident_row(db_path, key, sig)
        check("neg-lock-2. STILL open -- runtime_ready alone forbidden as the sole basis for this family", row2.get("status") == "open", row2)
        check("neg-lock-3. zero recovery notifications", len(_recovery_notifications(db_path)) == 0, _recovery_notifications(db_path))
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_network_timeout_worker_running_not_enough():
    print("\n-- Ownership-neg: network_timeout + worker running but connectivity not freshly healthy -> stays OPEN --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_net_neg"
        ss = {"phase": "exited", "last_exit_reason_class": "connection_error", "last_exit_at": _iso(NOW)}
        mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW - timedelta(seconds=100))}  # OLDER than opened_at will be
        _write_ss(tmp_root, key, ss)
        row = _drive_to_open(ns, db_path, key, mrow, {}, "network_timeout", "timeout")
        sig = ns["_hnv2_signature"]("network_timeout", "timeout")
        check("neg-net-1. network_timeout opens (confirm_ticks=4)", row and row.get("status") == "open", row)

        ns["__fake_subprocess__"].default_running = True
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        # mrow's auth_guard_last_ok_at is deliberately NOT refreshed past
        # opened_at -- worker running alone must not resolve this family.
        for _ in range(5):
            asyncio.run(_tick(ns, key, mrow, {}))
        row2 = _incident_row(db_path, key, sig)
        check("neg-net-2. STILL open -- a running worker alone never resolves network_timeout", row2.get("status") == "open", row2)
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_proxy_worker_running_but_guard_still_blocked():
    print("\n-- Ownership-neg: proxy_unavailable + worker running but auth_guard still blocked -> stays OPEN --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_proxy_neg"
        mrow = {"auth_guard_state": "blocked"}
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "proxy_timeout", "last_exit_at": _iso(NOW)})
        row = _drive_to_open(ns, db_path, key, mrow, {}, "proxy_unavailable", "unreachable")
        sig = ns["_hnv2_signature"]("proxy_unavailable", "unreachable")
        check("neg-proxy-1. proxy_unavailable opens (confirm_ticks=4)", row and row.get("status") == "open", row)

        ns["__fake_subprocess__"].default_running = True
        ns["__fake_ready__"].default_ok = True
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        for _ in range(5):
            asyncio.run(_tick(ns, key, mrow, {}))  # auth_guard_state stays 'blocked'
        row2 = _incident_row(db_path, key, sig)
        check("neg-proxy-2. STILL open -- worker running + ready-probe passing never resolves a still-blocked proxy guard", row2.get("status") == "open", row2)
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_session_unauthorized_no_new_auth():
    print("\n-- Ownership-neg: session_unauthorized + process running but no new authorization evidence -> stays OPEN --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_auth_neg"
        ss = {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)}
        _write_ss(tmp_root, key, ss)
        asyncio.run(_tick(ns, key, {}, {}))
        sig = ns["_hnv2_signature"]("session_unauthorized", "session_unauthorized")
        row = _incident_row(db_path, key, sig)
        check("neg-auth-1. session_unauthorized opens", row and row.get("status") == "open", row)

        ns["__fake_subprocess__"].default_running = True
        ns["__fake_ready__"].default_ok = True
        # phase='starting' (process exists, but never reached a fresh
        # last_connected_at) -- proc running alone is not new auth evidence.
        _write_ss(tmp_root, key, {"phase": "starting", "updated_at": _iso(NOW), "pid": 1})
        for _ in range(5):
            asyncio.run(_tick(ns, key, {}, {}))
        row2 = _incident_row(db_path, key, sig)
        check("neg-auth-2. STILL open -- proc running without a fresh last_connected_at never resolves session_unauthorized", row2.get("status") == "open", row2)
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_stale_unrelated_fresh_signal():
    print("\n-- Ownership-neg: health_missing_stale('both') + only ONE source refreshed -> stays OPEN --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_stale_neg"
        stale_hrow = {"health_status": "ok", "last_check_at": _iso(NOW - timedelta(seconds=4000))}
        stale_mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW - timedelta(seconds=4000))}
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        ns["__fake_subprocess__"].default_running = True
        row = _drive_to_open(ns, db_path, key, stale_mrow, stale_hrow, "health_missing_stale", "both")
        sig = ns["_hnv2_signature"]("health_missing_stale", "both")
        check("neg-stale-1. health_missing_stale('both') opens (confirm_ticks=10)", row and row.get("status") == "open", row)

        # Refresh ONLY the proxy side -- the account side stays stale.
        fresh_mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}
        for _ in range(5):
            asyncio.run(_tick(ns, key, fresh_mrow, stale_hrow))
        row2 = _incident_row(db_path, key, sig)
        check("neg-stale-2. STILL open -- refreshing only ONE of the two stale sources is not enough for evidence_key='both'", row2.get("status") == "open", row2)
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_peerflood_floodwait_never_resolved_by_hnv2():
    print("\n-- Ownership-neg: PeerFlood/FloodWait cannot be opened OR resolved by HNV2 --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_pf_neg"
        hrow_pf = {"health_status": "limited", "error_class": "PeerFloodError"}
        asyncio.run(_tick(ns, key, {}, hrow_pf))
        sig_pf = ns["_hnv2_signature"]("telegram_limited_peerflood", "peerflood")
        row_pf = _incident_row(db_path, key, sig_pf)
        check("neg-pf-1. HNV2 never creates an hv2:* row for peerflood", row_pf is None, row_pf)

        hrow_fw = {"health_status": "limited", "error_class": "FloodWaitError"}
        asyncio.run(_tick(ns, key, {}, hrow_fw))
        sig_fw = ns["_hnv2_signature"]("floodwait", "floodwait")
        row_fw = _incident_row(db_path, key, sig_fw)
        check("neg-pf-2. HNV2 never creates an hv2:* row for floodwait either", row_fw is None, row_fw)

        check("neg-pf-3. zero problem notifications from either tick", len(_problem_notifications(db_path)) == 0, _problem_notifications(db_path))
    finally:
        cleanup_env(tmp_root)


def test_ownership_negative_contradictory_evidence_no_recovery():
    print("-- Ownership-neg: contradictory/unknown evidence cannot cause recovery --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_contra_neg"
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "process_exited", "last_exit_at": _iso(NOW)})
        ns["__fake_subprocess__"].default_running = False
        mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}
        row = _drive_to_open(ns, db_path, key, mrow, {}, "worker_crash", "process_exited")
        sig = ns["_hnv2_signature"]("worker_crash", "process_exited")
        check("neg-contra-1. worker_crash opens (confirm_ticks=3)", row and row.get("status") == "open", row)

        # Now make the process probe itself fail (probe_error) -- classifier
        # falls to 'unknown' this tick, which must NOT be interpreted as
        # recovery for the separately-tracked worker_crash incident.
        ns["__fake_subprocess__"].run = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated probe failure"))
        for _ in range(3):
            asyncio.run(_tick(ns, key, mrow, {}))
        row2 = _incident_row(db_path, key, sig)
        check("neg-contra-2. STILL open -- a probe_error tick never counts as recovery evidence", row2.get("status") == "open", row2)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Recovery Ownership Matrix -- REQUIRED POSITIVE TESTS. Each family enters
# recovery_pending ONLY from its own authoritative evidence, and RESOLVED
# only after its full stability window (not one tick early).
# ======================================================================

def test_ownership_positive_worker_crash_full_cycle():
    print("\n-- Ownership-pos: worker_crash full OPEN->RECOVERY_PENDING->RESOLVED cycle, stability window enforced --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_wc_pos"
        ns["__fake_subprocess__"].default_running = False
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "process_exited", "last_exit_at": _iso(NOW)})
        mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}
        opened_row = _drive_to_open(ns, db_path, key, mrow, {}, "worker_crash", "process_exited")
        sig = ns["_hnv2_signature"]("worker_crash", "process_exited")
        check("pos-wc-0. worker_crash opens (confirm_ticks=3)", opened_row and opened_row.get("status") == "open", opened_row)

        ns["__fake_subprocess__"].default_running = True
        ns["__fake_ready__"].default_ok = True
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _fresh_iso(1), "pid": 777})
        asyncio.run(_tick(ns, key, mrow, {}))
        row = _incident_row(db_path, key, sig)
        check("pos-wc-1. enters recovery_pending from its own evidence (proc+phase+readiness)", row.get("status") == "recovery_pending", row)

        stable_sec = ns["_hnv2_stable_sec_for_family"]("worker_crash")
        # One tick short of the window: must NOT resolve yet.
        con = sqlite3.connect(db_path)
        try:
            almost = (datetime.utcnow() - timedelta(seconds=stable_sec - 5)).replace(microsecond=0).isoformat()
            con.execute("UPDATE health_incidents SET recovery_pending_since=? WHERE manager_key=? AND signature=?", (almost, key, sig))
            con.commit()
        finally:
            con.close()
        asyncio.run(_tick(ns, key, mrow, {}))
        row_almost = _incident_row(db_path, key, sig)
        check("pos-wc-2. NOT yet resolved one tick short of the stability window", row_almost.get("status") == "recovery_pending", row_almost)

        con = sqlite3.connect(db_path)
        try:
            past = (datetime.utcnow() - timedelta(seconds=stable_sec + 5)).replace(microsecond=0).isoformat()
            con.execute("UPDATE health_incidents SET recovery_pending_since=? WHERE manager_key=? AND signature=?", (past, key, sig))
            con.commit()
        finally:
            con.close()
        asyncio.run(_tick(ns, key, mrow, {}))
        row_final = _incident_row(db_path, key, sig)
        check("pos-wc-3. resolved once the full stability window has elapsed", row_final.get("status") == "resolved", row_final)
        check("pos-wc-4. exactly one recovery notification", len(_recovery_notifications(db_path)) == 1, _recovery_notifications(db_path))
    finally:
        cleanup_env(tmp_root)


def test_ownership_positive_account_blocked_via_tp_hg_evidence():
    print("\n-- Ownership-pos: account_blocked recovers ONLY via new TP_HG health_status='ok' evidence --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_ab_pos"
        hrow = {"health_status": "blocked", "error_class": "UserDeactivatedBanError"}
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _iso(NOW), "pid": 1})
        ns["__fake_subprocess__"].default_running = True
        asyncio.run(_tick(ns, key, {}, hrow))
        sig = ns["_hnv2_signature"]("account_blocked", "userdeactivatedbanerror")
        row = _incident_row(db_path, key, sig)
        opened_at = row.get("opened_at")

        # TP_HG genuinely clears it: health_status='ok' + last_ok_at newer
        # than opened_at (only reachable via allow_recover=True in the
        # real _tp_hg_update_status -- deep-check/manual reset/floodwait
        # retry success).
        ns["__fake_ready__"].default_ok = True
        hrow_recovered = {"health_status": "ok", "last_ok_at": _fresh_iso(5), "last_check_at": _fresh_iso(5)}
        asyncio.run(_tick(ns, key, {}, hrow_recovered))
        row_rp = _incident_row(db_path, key, sig)
        check("pos-ab-1. enters recovery_pending only via the TP_HG-authoritative health_status='ok'+last_ok_at evidence", row_rp.get("status") == "recovery_pending", row_rp)

        stable_sec = ns["HNV2_STABLE_SEC"]
        con = sqlite3.connect(db_path)
        try:
            past = (datetime.utcnow() - timedelta(seconds=stable_sec + 5)).replace(microsecond=0).isoformat()
            con.execute("UPDATE health_incidents SET recovery_pending_since=? WHERE manager_key=? AND signature=?", (past, key, sig))
            con.commit()
        finally:
            con.close()
        asyncio.run(_tick(ns, key, {}, hrow_recovered))
        row_final = _incident_row(db_path, key, sig)
        check("pos-ab-2. resolved after the stability window", row_final.get("status") == "resolved", row_final)
    finally:
        cleanup_env(tmp_root)


def test_ownership_positive_sqlite_lock_long_stability_window():
    print("\n-- Ownership-pos: sqlite_lock recovers via advanced last_check_at, 1800s window enforced --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_lock_pos"
        ss = {"phase": "exited", "last_exit_error": "sqlite3.OperationalError: database is locked", "last_exit_at": _iso(NOW)}
        _write_ss(tmp_root, key, ss)
        row = _drive_to_open(ns, db_path, key, {}, {}, "sqlite_lock", "locked")
        sig = ns["_hnv2_signature"]("sqlite_lock", "locked")
        check("pos-lock-0. sqlite_lock opens (confirm_ticks=4)", row and row.get("status") == "open", row)
        opened_at = row.get("opened_at")

        # Affirmative evidence: last_check_at advances past opened_at, AND
        # the stale "database is locked" text is cleared from ss (the
        # ownership-matrix check reads ss.last_exit_error fresh every
        # tick -- leaving the original lock text in place would make
        # has_fresh_lock_marker permanently True and positive evidence
        # permanently unreachable, regardless of last_check_at).
        _write_ss(tmp_root, key, {"phase": "running", "updated_at": _fresh_iso(5), "pid": 1})
        hrow_recovered = {"last_check_at": _fresh_iso(5)}
        asyncio.run(_tick(ns, key, {}, hrow_recovered))
        row_rp = _incident_row(db_path, key, sig)
        check("pos-lock-1. enters recovery_pending via advanced last_check_at with no fresh lock marker", row_rp.get("status") == "recovery_pending", row_rp)

        stable_sec = ns["_hnv2_stable_sec_for_family"]("sqlite_lock")
        check("pos-lock-2. sqlite_lock's stability window is 1800s, not the default 900s", stable_sec == 1800, stable_sec)

        con = sqlite3.connect(db_path)
        try:
            almost = (datetime.utcnow() - timedelta(seconds=stable_sec - 10)).replace(microsecond=0).isoformat()
            con.execute("UPDATE health_incidents SET recovery_pending_since=? WHERE manager_key=? AND signature=?", (almost, key, sig))
            con.commit()
        finally:
            con.close()
        asyncio.run(_tick(ns, key, {}, hrow_recovered))
        row_almost = _incident_row(db_path, key, sig)
        check("pos-lock-3. NOT resolved 10s short of the 1800s window", row_almost.get("status") == "recovery_pending", row_almost)

        con = sqlite3.connect(db_path)
        try:
            past = (datetime.utcnow() - timedelta(seconds=stable_sec + 10)).replace(microsecond=0).isoformat()
            con.execute("UPDATE health_incidents SET recovery_pending_since=? WHERE manager_key=? AND signature=?", (past, key, sig))
            con.commit()
        finally:
            con.close()
        asyncio.run(_tick(ns, key, {}, hrow_recovered))
        row_final = _incident_row(db_path, key, sig)
        check("pos-lock-4. resolved after the full 1800s window", row_final.get("status") == "resolved", row_final)
    finally:
        cleanup_env(tmp_root)


def test_ownership_positive_sqlite_lock_recovers_despite_sticky_marker():
    """N-5 correction (post-independent-review). _m212a_write_start_status
    (D1) makes ss.last_exit_error STICKY -- it stays "database is locked"
    forever after the ORIGINAL exit that opened this incident, since
    nothing overwrites it except a genuinely NEW 'exited' write.

    Two layers had to be fixed together, and this test proves both:
    (1) classifier rule 7 (main.py _hnv2_classify_root_cause) now gates the
        sticky ss.last_exit_error text by the SAME exit_evidence_fresh
        (HNV2_EXIT_EVIDENCE_MAX_AGE_SEC, 24h) window rule 1 already uses --
        without this, rule 7 would re-diagnose sqlite_lock EVERY tick
        forever, and PASS 2 (_hnv2_incident_step) would never even reach
        this incident's recovery check (it skips any signature PASS 1
        already re-confirmed the same tick).
    (2) _hnv2_family_recovery_evidence's sqlite_lock branch only counts the
        lock marker as blocking if ITS OWN timestamp (last_exit_at) is
        newer than opened_at -- so once (1) lets PASS 2 actually run, a
        marker that predates the incident no longer blocks recovery.

    Simulates '24h have passed since the original lock-causing exit' via
    backdating (an actual 24h wall-clock wait is not practical in a unit
    test) -- the same backdating convention this file already uses for the
    RECOVERY_PENDING stability-window tests above (pos-lock-3/pos-lock-4)."""
    print("\n-- Ownership-pos (N-5): sqlite_lock recovers once the sticky marker ages past the 24h exit-evidence window --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_lock_sticky"
        sticky_ss = {"phase": "exited", "last_exit_error": "sqlite3.OperationalError: database is locked", "last_exit_at": _fresh_iso(-5)}
        _write_ss(tmp_root, key, sticky_ss)
        row = _drive_to_open(ns, db_path, key, {}, {}, "sqlite_lock", "locked")
        sig = ns["_hnv2_signature"]("sqlite_lock", "locked")
        check("sticky-lock-0. sqlite_lock opens (confirm_ticks=4)", row and row.get("status") == "open", row)
        opened_at = row.get("opened_at")

        # Still within the 24h freshness window, worker back to 'running':
        # rule 7 correctly keeps re-diagnosing sqlite_lock from the sticky
        # marker (this is the intended, documented fail-closed behavior,
        # not a bug) -- PASS 2 never even runs for this signature, so it
        # stays exactly 'open', not 'recovery_pending'.
        _write_ss(tmp_root, key, {
            "phase": "running", "updated_at": _fresh_iso(5), "pid": 2,
            "last_exit_error": "sqlite3.OperationalError: database is locked",
            "last_exit_reason_class": "process_exited",
            "last_exit_at": sticky_ss["last_exit_at"],
        })
        asyncio.run(_tick(ns, key, {"auth_guard_state": "ok", "auth_guard_last_ok_at": _fresh_iso(5)}, {"health_status": "ok", "last_check_at": _fresh_iso(5)}))
        row_still_open = _incident_row(db_path, key, sig)
        check("sticky-lock-1. WITHIN the 24h window, stays OPEN (fail-closed by design, not a bug)",
              row_still_open.get("status") == "open", row_still_open)

        # Now the ORIGINAL lock-causing exit is >24h old (HNV2_EXIT_EVIDENCE_
        # MAX_AGE_SEC=86400s) -- rule 7 stops matching, PASS 2 finally
        # evaluates this incident's own recovery evidence, and the marker
        # (older than opened_at either way) no longer blocks it.
        stale_exit_at = _iso(datetime.now(timezone.utc) - timedelta(seconds=90000))
        _write_ss(tmp_root, key, {
            "phase": "running", "updated_at": _fresh_iso(5), "pid": 2,
            "last_exit_error": "sqlite3.OperationalError: database is locked",
            "last_exit_reason_class": "process_exited",
            "last_exit_at": stale_exit_at,
        })
        asyncio.run(_tick(ns, key, {"auth_guard_state": "ok", "auth_guard_last_ok_at": _fresh_iso(5)}, {"health_status": "ok", "last_check_at": _fresh_iso(5)}))
        row_rp = _incident_row(db_path, key, sig)
        check("sticky-lock-2. once the marker ages past 24h, enters recovery_pending despite the sticky text never being cleared",
              row_rp.get("status") == "recovery_pending", (row_rp, opened_at))

        # Negative control: a lock marker NEWER than opened_at (a genuinely
        # NEW lock-causing exit happened since) must still correctly block
        # recovery -- proves this isn't a blanket "ignore sticky text"
        # regression, only a freshness-gated one, even once past 24h stale
        # for the ORIGINAL marker.
        key2 = "mgr_lock_sticky_newexit"
        _write_ss(tmp_root, key2, sticky_ss)
        row2 = _drive_to_open(ns, db_path, key2, {}, {}, "sqlite_lock", "locked")
        opened_at2 = row2.get("opened_at")
        _write_ss(tmp_root, key2, {
            "phase": "running", "updated_at": _fresh_iso(5), "pid": 3,
            "last_exit_error": "sqlite3.OperationalError: database is locked",
            "last_exit_reason_class": "process_exited",
            "last_exit_at": _fresh_iso(30),  # NEWER than opened_at -- a fresh lock-exit really did happen since
        })
        asyncio.run(_tick(ns, key2, {"auth_guard_state": "ok", "auth_guard_last_ok_at": _fresh_iso(5)}, {"health_status": "ok", "last_check_at": _fresh_iso(5)}))
        row2_after = _incident_row(db_path, key2, ns["_hnv2_signature"]("sqlite_lock", "locked"))
        check("sticky-lock-3 (negative control). a lock marker NEWER than opened_at still correctly blocks recovery (stays open)",
              row2_after.get("status") == "open", (row2_after, opened_at2))
    finally:
        cleanup_env(tmp_root)


def test_ownership_positive_reflap_before_stability_no_new_notification():
    print("\n-- Ownership-pos: a family re-appearing mid-stability-window flaps back to OPEN with NO new notification --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(tmp_root, db_path)
        key = "mgr_reflap"
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)})
        mrow = {}
        asyncio.run(_tick(ns, key, mrow, {}))
        sig = ns["_hnv2_signature"]("session_unauthorized", "session_unauthorized")
        row_open = _incident_row(db_path, key, sig)
        last_notified_before = row_open.get("last_notified_at")

        # Recovers momentarily...
        ns["__fake_ready__"].default_ok = True
        _write_ss(tmp_root, key, {"phase": "connected", "last_connected_at": _fresh_iso(1), "pid": 1})
        asyncio.run(_tick(ns, key, mrow, {}))
        check("pos-reflap-1. enters recovery_pending", _incident_row(db_path, key, sig).get("status") == "recovery_pending", None)

        # ...then fails again before the stability window elapses.
        _write_ss(tmp_root, key, {"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _fresh_iso(2)})
        asyncio.run(_tick(ns, key, mrow, {}))
        row_reflapped = _incident_row(db_path, key, sig)
        check("pos-reflap-2. flaps back to OPEN", row_reflapped.get("status") == "open", row_reflapped)
        check("pos-reflap-3. last_notified_at is UNCHANGED (no new notification for the re-flap)", row_reflapped.get("last_notified_at") == last_notified_before, (last_notified_before, row_reflapped))
        check("pos-reflap-4. exactly one problem notification total, despite the full open->recovery_pending->open cycle", len(_problem_notifications(db_path)) == 1, _problem_notifications(db_path))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Static: no new stacked override of any PeerFlood/health-lifecycle
# protected name was introduced by Phase 6's additions.
# ======================================================================

def test_static_protected_invariants():
    print("\n-- Static: Phase 6 introduces no new protected-name overrides --")
    from collections import Counter
    defs = Counter(n.name for n in MAIN_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for name, expected in (
        # OVERRIDE CLEANUP 20260815: _send_manager_private 4 -> 3 (one dead
        # shadowed def removed; active chain unchanged).
        ("_tp_hg_send_allowed", 1), ("_send_manager_private", 3),
        ("_process_profile_reminders_once", 2), ("_process_post_manual_followups_once", 2),
        ("_health_incident_handle", 1), ("_health_incident_notify", 1),
        ("_manager_recovery_classify", 1), ("_manager_recovery_open_auth_incident", 1),
        ("_manager_recovery_resolve_auth_incident", 1), ("_m212a_write_start_status", 1),
        ("_manager_recovery_once", 2),  # base + Phase 5's HNV2 wrapper
        ("_health_agg_once", 2),        # base + Phase 6's HNV2 override
    ):
        check(f"static. {name} defined exactly {expected} time(s)", defs.get(name, 0) == expected, defs.get(name, 0))

    # AST-based (not substring) -- a docstring mentioning
    # "_health_incident_handle(...)" as PROSE (explaining why the override
    # deliberately does NOT call it) must not be flagged as a real call.
    src = MAIN_SRC
    hnv2_start_line = src[:src.index("TPILOT HEALTH NOTIFICATION V2 20260807 START")].count("\n") + 1
    hnv2_end_line = src[:src.index("TPILOT HEALTH NOTIFICATION V2 20260807 END")].count("\n") + 1
    forbidden_calls = []
    for node in ast.walk(MAIN_TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_health_incident_handle", "_health_incident_notify"):
            if hnv2_start_line <= getattr(node, "lineno", -1) <= hnv2_end_line:
                forbidden_calls.append((node.func.id, node.lineno))
    check("static. HNV2 block contains no actual Call to _health_incident_handle/_health_incident_notify (AST-verified, not text search)",
          forbidden_calls == [], forbidden_calls)


def main() -> int:
    test_scenario_7_8_daily_cap()
    test_scenario_9_recovery_exempt_from_quota()
    test_scenario_10_distinct_causes_independent()
    test_scenario_11_restart_persistence()
    test_scenario_14_manager_isolation()

    test_ownership_negative_account_blocked_runtime_ready_insufficient()
    test_ownership_negative_account_blocked_sticky_heartbeat()
    test_ownership_negative_sqlite_lock_runtime_ready_insufficient()
    test_ownership_negative_network_timeout_worker_running_not_enough()
    test_ownership_negative_proxy_worker_running_but_guard_still_blocked()
    test_ownership_negative_session_unauthorized_no_new_auth()
    test_ownership_negative_stale_unrelated_fresh_signal()
    test_ownership_negative_peerflood_floodwait_never_resolved_by_hnv2()
    test_ownership_negative_contradictory_evidence_no_recovery()

    test_ownership_positive_worker_crash_full_cycle()
    test_ownership_positive_account_blocked_via_tp_hg_evidence()
    test_ownership_positive_sqlite_lock_long_stability_window()
    test_ownership_positive_sqlite_lock_recovers_despite_sticky_marker()
    test_ownership_positive_reflap_before_stability_no_new_notification()

    test_static_protected_invariants()

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
