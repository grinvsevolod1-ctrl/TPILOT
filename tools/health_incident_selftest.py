# -*- coding: utf-8 -*-
"""tools/health_incident_selftest.py -- offline self-test for
"TPILOT G 20260718" (plan principle G): the persistent per-(manager_key,
signature) health-incident lifecycle that replaces the old in-memory hourly
cooldown (main.py's now-removed _health_agg_notified dict -- cooldown-only,
no lifecycle, reset on every controller restart).

Two layers are tested, per the task's own framing:

  1. Storage-level CRUD (storage.py's `ensure_health_incidents_table`,
     `health_incident_upsert_open`, `health_incident_mark_notified`,
     `health_incident_resolve`, `health_incident_get`) -- storage.py IS
     importable directly (no Telethon/env side effects), these are plain
     sync `def`s using raw sqlite3, called FOR REAL against a temp SQLite
     file. This proves the primitives in isolation.

  2. main.py orchestration (`_health_incident_handle` + its
     `_health_incident_notify` dependency) -- where the actual lifecycle
     DECISIONS live: the "signature = reason text before any ':<count>'
     suffix" derivation, the "notify once on open, reminder only after
     HEALTH_INCIDENT_REMINDER_INTERVAL_SEC" gate, and the "resolve only on
     unified_status==OK, notify only if something was actually open" gate.
     Testing only the storage CRUD would prove the primitives work but not
     that the orchestration actually calls them correctly -- these two
     async functions (main.py, ~lines 28594-28698) are extracted for real
     via ast.parse + ast.unparse + exec(), the same technique every other
     tools/*_selftest.py in this project uses for main.py (which cannot be
     imported directly). Confirmed via grep: neither name is redefined
     anywhere else in main.py, so this is genuinely the one active
     implementation, not a stale earlier override.

     `_health_incident_notify`'s only side effect beyond the DB is one
     `INSERT INTO panel_notifications(...)` via a real aiosqlite connection
     to TPILOT_DB_PATH (our temp DB) -- NOT faked; instead this file creates
     a minimal `panel_notifications` table up front with the EXACT column
     set the real INSERT statement uses (verified against main.py's own
     `CREATE TABLE IF NOT EXISTS panel_notifications(id INTEGER PRIMARY KEY
     AUTOINCREMENT, kind TEXT NOT NULL DEFAULT '', title TEXT NOT NULL
     DEFAULT '', body TEXT NOT NULL DEFAULT '', status TEXT NOT NULL
     DEFAULT 'new', created_at TEXT NOT NULL DEFAULT '', updated_at TEXT
     NOT NULL DEFAULT '')`, main.py ~line 12471) -- so a real INSERT
     genuinely succeeds and can be counted, rather than silently swallowed
     by `_health_incident_notify`'s own best-effort `except Exception:
     pass` (which would otherwise mask a "no such table" error as zero
     notifications sent, a false pass).

Time-elapsed simulation: rather than sleeping for
HEALTH_INCIDENT_REMINDER_INTERVAL_SEC (21600s by default), tests directly
backdate `last_notified_at` on the `health_incidents` row via raw sqlite
UPDATE, per the task's own suggested approach.

Never: real Telegram network, production DB/runtime/session/log access.

    python tools\\health_incident_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()

REAL_NAMES = {
    "_health_incident_handle", "_health_incident_notify", "_health_agg_utc_now_iso",
    "HEALTH_INCIDENT_REMINDER_INTERVAL_SEC", "_HAGG_OK", "_HAGG_WARNING_TEMPORARY", "_HAGG_UNKNOWN",
    # TPILOT FIX-3 20260718b: non-recoverable auth/identity incident coverage --
    # the manager-recovery classifier and its two persistent-incident helpers,
    # which reuse the SAME health_incidents lifecycle (open/notify-once/resolve)
    # already exercised above, under a dedicated "auth_action_required"
    # signature. All three real dependencies these need (_repl_storage,
    # TPILOT_DB_PATH, _health_agg_utc_now_iso, aiosqlite) are already provided
    # by build_main_ns below -- nothing new to fake.
    "_manager_recovery_classify", "_REPL_AUTH_INCIDENT_SIGNATURE",
    "_manager_recovery_open_auth_incident", "_manager_recovery_resolve_auth_incident",
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
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing}")
    return nodes


def _selftest_db_guard(db_path: str, base_dir, storage_mod) -> None:
    """Mandatory production-path DB guard, same convention as every other
    tools/*_selftest.py in this project."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def _ensure_panel_notifications_table(db_path: str) -> None:
    """Minimal real-schema panel_notifications table -- exact column set
    from main.py's own CREATE TABLE statement (~line 12471) -- so
    _health_incident_notify's real INSERT genuinely succeeds instead of
    being silently swallowed by its own except-Exception-pass."""
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


def build_main_ns(db_path: str) -> dict:
    import manager_registry
    import aiosqlite as _aiosqlite
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, storage)
    _ensure_panel_notifications_table(db_path)

    nodes = _extract_by_names(MAIN_SRC, REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    ns = {
        "os": os,
        "datetime": _dt,
        "timedelta": _td,
        "timezone": _tz,
        "aiosqlite": _aiosqlite,
        "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "_repl_storage": storage,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:health_incident>", "exec"), ns)
    return ns


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="health_incident_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def _panel_notifications_count(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM panel_notifications").fetchone()[0]
    finally:
        con.close()


def _panel_notifications_titles(db_path: str) -> list:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("SELECT title FROM panel_notifications ORDER BY id ASC").fetchall()]
    finally:
        con.close()


def _panel_notifications_bodies(db_path: str) -> list:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("SELECT body FROM panel_notifications ORDER BY id ASC").fetchall()]
    finally:
        con.close()


def _backdate_last_notified(db_path: str, manager_key: str, signature: str, seconds_ago: int) -> None:
    ts = (datetime.utcnow() - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE health_incidents SET last_notified_at=? WHERE manager_key=? AND signature=?",
            (ts, manager_key, signature),
        )
        con.commit()
    finally:
        con.close()


# ======================================================================
# GROUP 0: storage-level CRUD (real functions, no main.py extraction).
# ======================================================================

def test_group_0_storage_crud():
    print("\n-- Group 0: storage-level CRUD --")
    tmp_root, db_path = make_temp_env()
    try:
        storage.ensure_health_incidents_table(db_path)

        assert_missing = storage.health_incident_get("mgr0", "sigA", db_path=db_path)
        check("0a. health_incident_get on a nonexistent row returns None", assert_missing is None, assert_missing)

        row = storage.health_incident_upsert_open("mgr0", "sigA", db_path=db_path)
        check("0b. upsert_open creates a row with status='open'", row.get("status") == "open", row)
        check("0c. reminder_count starts at 0", int(row.get("reminder_count", -1)) == 0, row)
        check("0d. last_notified_at starts empty", row.get("last_notified_at") == "", row)
        opened_at_1 = row.get("opened_at")

        row_again = storage.health_incident_upsert_open("mgr0", "sigA", db_path=db_path)
        check("0e. upsert_open on an already-open row is idempotent (opened_at unchanged)",
              row_again.get("opened_at") == opened_at_1, (row, row_again))

        storage.health_incident_mark_notified("mgr0", "sigA", db_path=db_path)
        after_notify = storage.health_incident_get("mgr0", "sigA", db_path=db_path)
        check("0f. mark_notified (non-reminder) sets last_notified_at, leaves reminder_count at 0",
              bool(after_notify.get("last_notified_at")) and int(after_notify.get("reminder_count", -1)) == 0, after_notify)

        storage.health_incident_mark_notified("mgr0", "sigA", is_reminder=True, db_path=db_path)
        after_reminder = storage.health_incident_get("mgr0", "sigA", db_path=db_path)
        check("0g. mark_notified(is_reminder=True) increments reminder_count", int(after_reminder.get("reminder_count", -1)) == 1, after_reminder)

        resolved = storage.health_incident_resolve("mgr0", "sigA", db_path=db_path)
        check("0h. resolve() returns True for an open row", resolved is True, resolved)
        after_resolve = storage.health_incident_get("mgr0", "sigA", db_path=db_path)
        check("0i. resolved row has status='resolved' and resolved_at set",
              after_resolve.get("status") == "resolved" and bool(after_resolve.get("resolved_at")), after_resolve)

        resolved_again = storage.health_incident_resolve("mgr0", "sigA", db_path=db_path)
        check("0j. resolve() on an already-resolved row returns False (CAS, no-op)", resolved_again is False, resolved_again)

        # opened_at has 1-second string resolution (_now_iso() truncates microseconds) -- a
        # deliberately far-in-the-past marker avoids same-second flakiness that comparing
        # against a real "now" timestamp captured moments earlier would risk.
        stale_opened_at = "2020-01-01T00:00:00"
        con = sqlite3.connect(db_path)
        try:
            con.execute("UPDATE health_incidents SET opened_at=? WHERE manager_key=? AND signature=?", (stale_opened_at, "mgr0", "sigA"))
            con.commit()
        finally:
            con.close()
        reopened = storage.health_incident_upsert_open("mgr0", "sigA", db_path=db_path)
        check("0k. upsert_open on a resolved row reopens it (status back to 'open')", reopened.get("status") == "open", reopened)
        check("0l. reopened row has a fresh opened_at (not the deliberately stale value it was resolved with)",
              reopened.get("opened_at") != stale_opened_at, (stale_opened_at, reopened))
        check("0m. reopened row's reminder_count resets to 0", int(reopened.get("reminder_count", -1)) == 0, reopened)
        check("0n. reopened row's last_notified_at resets to empty", reopened.get("last_notified_at") == "", reopened)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 1/2: flap opens once, no hourly duplicate.
# ======================================================================

async def test_group_1_2_flap_opens_once_no_duplicate():
    print("\n-- Group 1/2: flap opens once; no hourly duplicate --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        mk, reason = "mgrh1", "recovery_flap:3_in_30min"

        await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail1")
        check("1a. first escalated tick creates an OPEN incident", storage.health_incident_get(mk, "recovery_flap", db_path=db_path).get("status") == "open", None)
        check("1b. exactly one notification sent", _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))
        row1 = storage.health_incident_get(mk, "recovery_flap", db_path=db_path)

        # Several more escalated ticks, well inside the reminder interval --
        # simulates the aggregator's own repeated ~60s ticks for an ongoing flap.
        for _ in range(4):
            await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail1")
        check("2a. no duplicate notification after several more escalated ticks "
              "(proves this is no longer the old hourly-cooldown behavior)",
              _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))
        row2 = storage.health_incident_get(mk, "recovery_flap", db_path=db_path)
        check("2b. opened_at unchanged across repeated ticks", row2.get("opened_at") == row1.get("opened_at"), (row1, row2))
        check("2c. reminder_count unchanged (no reminder fired yet)", row2.get("reminder_count") == row1.get("reminder_count"), (row1, row2))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 3: reminder fires after the reminder interval has genuinely elapsed.
# ======================================================================

async def test_group_3_reminder_after_long_interval():
    print("\n-- Group 3: reminder after long interval --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        mk, reason = "mgrh3", "recovery_flap:5_in_30min"
        interval = ns["HEALTH_INCIDENT_REMINDER_INTERVAL_SEC"]

        await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail")
        check("3a-setup. initial open sent exactly one notification", _panel_notifications_count(db_path) == 1, None)

        # Not yet due: backdate by less than the interval.
        _backdate_last_notified(db_path, mk, "recovery_flap", interval - 60)
        await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail")
        check("3b. still under the interval -> no reminder yet", _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))

        # Now genuinely past the interval.
        _backdate_last_notified(db_path, mk, "recovery_flap", interval + 60)
        await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail")
        check("3c. past the interval -> a second (reminder) notification IS sent", _panel_notifications_count(db_path) == 2, _panel_notifications_titles(db_path))
        row = storage.health_incident_get(mk, "recovery_flap", db_path=db_path)
        check("3d. reminder_count incremented to 1", int(row.get("reminder_count", -1)) == 1, row)
        titles = _panel_notifications_titles(db_path)
        check("3e. the reminder notification's title is marked as a repeat", "повтор" in titles[-1], titles)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 4: healthy recovery resolves the incident and notifies once.
# ======================================================================

async def test_group_4_resolve_on_healthy():
    print("\n-- Group 4: resolve on healthy recovery --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        mk, reason = "mgrh4", "recovery_flap:3_in_30min"

        await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail")
        check("4a-setup. incident is open", storage.health_incident_get(mk, "recovery_flap", db_path=db_path).get("status") == "open", None)
        count_before = _panel_notifications_count(db_path)

        await ns["_health_incident_handle"](mk, ns["_HAGG_OK"], False, "", "")
        row = storage.health_incident_get(mk, "recovery_flap", db_path=db_path)
        check("4b. row transitions to status='resolved'", row.get("status") == "resolved", row)
        check("4c. resolved_at is set", bool(row.get("resolved_at")), row)
        check("4d. exactly one recovery notification sent", _panel_notifications_count(db_path) == count_before + 1, _panel_notifications_titles(db_path))
        titles = _panel_notifications_titles(db_path)
        check("4e. the recovery notification mentions recovery", "восстановлен" in titles[-1], titles)

        # A further healthy tick with nothing open must NOT send a spurious
        # recovery notification (health_incident_resolve's CAS returns False).
        count_after_resolve = _panel_notifications_count(db_path)
        await ns["_health_incident_handle"](mk, ns["_HAGG_OK"], False, "", "")
        check("4f. a further healthy tick with nothing open sends no spurious recovery notification",
              _panel_notifications_count(db_path) == count_after_resolve, _panel_notifications_titles(db_path))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 5: reopen after resolve is a genuinely new incident + notifies.
# ======================================================================

async def test_group_5_reopen_after_resolve():
    print("\n-- Group 5: reopen only after resolved + genuinely new incident --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        mk, reason = "mgrh5", "recovery_flap:3_in_30min"

        await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail")
        row_open_1 = storage.health_incident_get(mk, "recovery_flap", db_path=db_path)
        await ns["_health_incident_handle"](mk, ns["_HAGG_OK"], False, "", "")
        row_resolved = storage.health_incident_get(mk, "recovery_flap", db_path=db_path)
        check("5a-setup. incident resolved before reopen test", row_resolved.get("status") == "resolved", row_resolved)
        stale_opened_at = "2020-01-01T00:00:00"  # deliberately-stale marker, avoids same-second flakiness
        con = sqlite3.connect(db_path)
        try:
            con.execute("UPDATE health_incidents SET opened_at=? WHERE manager_key=? AND signature=?", (stale_opened_at, mk, "recovery_flap"))
            con.commit()
        finally:
            con.close()
        count_before_reopen = _panel_notifications_count(db_path)

        await ns["_health_incident_handle"](mk, "WARNING_TEMPORARY", True, reason, "detail-reopen")
        row_reopened = storage.health_incident_get(mk, "recovery_flap", db_path=db_path)
        check("5b. a new escalated tick reopens the incident (status back to 'open')", row_reopened.get("status") == "open", row_reopened)
        check("5c. reopened incident has a fresh opened_at (not the deliberately stale value it was resolved with)",
              row_reopened.get("opened_at") != stale_opened_at, (stale_opened_at, row_reopened))
        check("5d. reminder_count reset to 0", int(row_reopened.get("reminder_count", -1)) == 0, row_reopened)
        check("5e. a notification was sent for the reopen", _panel_notifications_count(db_path) == count_before_reopen + 1, _panel_notifications_titles(db_path))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 6: restart persistence -- a fresh main_ns against the same db_path
# sees the same durable state (no in-memory component driving this).
# ======================================================================

async def test_group_6_restart_persistence():
    print("\n-- Group 6: restart persistence --")
    tmp_root, db_path = make_temp_env()
    try:
        ns1 = build_main_ns(db_path)
        mk, reason = "mgrh6", "no_health_data"

        await ns1["_health_incident_handle"](mk, "UNKNOWN", True, reason, "detail")
        row_from_first_process = storage.health_incident_get(mk, "no_health_data", db_path=db_path)
        check("6a-setup. incident is open after the first 'process'", row_from_first_process.get("status") == "open", row_from_first_process)

        # Fresh exec() into a brand-new namespace dict against the SAME
        # db_path -- simulates a controller restart with zero shared
        # Python-level state (no module-global dict, no closure) between
        # the two "processes".
        ns2 = build_main_ns(db_path)
        check("6b. a completely fresh main_ns object was built (no shared identity)", ns2 is not ns1, None)

        row_seen_by_second = storage.health_incident_get(mk, "no_health_data", db_path=db_path)
        check("6c. the second 'process' sees the identical durable state via health_incident_get",
              row_seen_by_second == row_from_first_process, (row_from_first_process, row_seen_by_second))

        # Drive one more escalated tick through the SECOND namespace's own
        # extracted _health_incident_handle -- it must recognize the
        # already-open incident (no duplicate notify), proving state lives
        # entirely in sqlite, not in either ns.
        count_before = _panel_notifications_count(db_path)
        await ns2["_health_incident_handle"](mk, "UNKNOWN", True, reason, "detail")
        check("6d. the second 'process' correctly treats the incident as already-open (no duplicate notify)",
              _panel_notifications_count(db_path) == count_before, _panel_notifications_titles(db_path))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 7: manager-specific signatures -- fully independent per manager_key.
# ======================================================================

async def test_group_7_manager_specific():
    print("\n-- Group 7: manager-specific signatures --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        reason = "recovery_flap:3_in_30min"

        await ns["_health_incident_handle"]("mgrh7a", "WARNING_TEMPORARY", True, reason, "detail")
        row_a_before = storage.health_incident_get("mgrh7a", "recovery_flap", db_path=db_path)
        check("7a-setup. manager A's incident is open", row_a_before.get("status") == "open", row_a_before)

        await ns["_health_incident_handle"]("mgrh7b", "WARNING_TEMPORARY", True, reason, "detail")
        row_b = storage.health_incident_get("mgrh7b", "recovery_flap", db_path=db_path)
        check("7b. manager B independently opens its OWN incident with the same signature", row_b.get("status") == "open", row_b)

        row_a_after = storage.health_incident_get("mgrh7a", "recovery_flap", db_path=db_path)
        check("7c. manager A's incident is completely unaffected by manager B's own open",
              row_a_after == row_a_before, (row_a_before, row_a_after))

        check("7d. two notifications total (one per manager, independent)", _panel_notifications_count(db_path) == 2, _panel_notifications_titles(db_path))

        # Resolving B must never touch A.
        await ns["_health_incident_handle"]("mgrh7b", ns["_HAGG_OK"], False, "", "")
        row_a_after_b_resolve = storage.health_incident_get("mgrh7a", "recovery_flap", db_path=db_path)
        check("7e. resolving manager B leaves manager A's still-open incident untouched",
              row_a_after_b_resolve.get("status") == "open" and row_a_after_b_resolve == row_a_after, row_a_after_b_resolve)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 8: _manager_recovery_classify -- non-recoverable auth/identity
# reason classes (TPILOT FIX-3 20260718b).
# ======================================================================

def test_group_8_recovery_classify_non_recoverable():
    print("\n-- Group 8: _manager_recovery_classify non-recoverable auth/identity classes --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)  # classify() is pure; db_path only needed to build the ns
        classify = ns["_manager_recovery_classify"]
        check("8a. reason_class='session_unauthorized' -> non_recoverable (NEW)",
              classify("session_unauthorized", "") == "non_recoverable", None)
        check("8b. reason_class='telegram_identity_mismatch' -> non_recoverable (NEW)",
              classify("telegram_identity_mismatch", "") == "non_recoverable", None)
        check("8c. regression: reason_class='auth_session' still -> non_recoverable (pre-existing case)",
              classify("auth_session", "") == "non_recoverable", None)
        check("8d. control: reason_class='connection_error' (genuinely recoverable) is unaffected",
              classify("connection_error", "") == "recoverable", None)
        check("8e. control: unrelated reason_class + unrelated error text -> recoverable (final fallback)",
              classify("some_other_reason", "some other error") == "recoverable", None)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 9: _manager_recovery_open_auth_incident -- opens once, no hourly
# duplicate (same "no repeated identical notifications" requirement as
# Group 1/2, reused here for the auth_action_required signature).
# ======================================================================

async def test_group_9_open_auth_incident_notifies_once():
    print("\n-- Group 9: _manager_recovery_open_auth_incident opens once, no duplicate --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        key, reason_class = "mgra9", "session_unauthorized"
        sig = ns["_REPL_AUTH_INCIDENT_SIGNATURE"]

        await ns["_manager_recovery_open_auth_incident"](key, reason_class, "some auth error detail")
        row = storage.health_incident_get(key, sig, db_path=db_path)
        check("9a. a NEW health_incidents row opens with signature 'auth_action_required', status='open'",
              row is not None and sig == "auth_action_required" and row.get("status") == "open", row)
        check("9b. exactly ONE notification is sent", _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))
        bodies = _panel_notifications_bodies(db_path)
        check("9c. the notification body mentions the manager key", key in bodies[-1], bodies)
        check("9d. the notification body mentions the reason_class", reason_class in bodies[-1], bodies)

        # Calling again immediately (same key, still open) must insert NO
        # additional notification -- the "no repeated hourly identical
        # notifications" requirement.
        await ns["_manager_recovery_open_auth_incident"](key, reason_class, "some auth error detail")
        check("9e. calling again immediately (already open) sends NO additional notification",
              _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))
        row_again = storage.health_incident_get(key, sig, db_path=db_path)
        check("9f. opened_at unchanged across the repeated call (idempotent upsert_open)",
              row_again.get("opened_at") == row.get("opened_at"), (row, row_again))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 10: _manager_recovery_resolve_auth_incident -- resolves + notifies
# once; no-op (no exception, no notification) when nothing was open.
# ======================================================================

async def test_group_10_resolve_auth_incident():
    print("\n-- Group 10: _manager_recovery_resolve_auth_incident --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        sig = ns["_REPL_AUTH_INCIDENT_SIGNATURE"]
        key, reason_class = "mgra10", "telegram_identity_mismatch"

        # 10a: resolving a key that was never opened is a safe no-op.
        await ns["_manager_recovery_resolve_auth_incident"]("mgra10_never_opened")
        check("10a. resolving a never-opened key raises no exception and sends no notification",
              _panel_notifications_count(db_path) == 0, _panel_notifications_titles(db_path))

        await ns["_manager_recovery_open_auth_incident"](key, reason_class, "identity mismatch detail")
        check("10b-setup. incident is open before resolve",
              storage.health_incident_get(key, sig, db_path=db_path).get("status") == "open", None)
        count_before_resolve = _panel_notifications_count(db_path)

        await ns["_manager_recovery_resolve_auth_incident"](key)
        row = storage.health_incident_get(key, sig, db_path=db_path)
        check("10c. the incident resolves (status='resolved', resolved_at set)",
              row.get("status") == "resolved" and bool(row.get("resolved_at")), row)
        check("10d. exactly ONE recovery notification is sent",
              _panel_notifications_count(db_path) == count_before_resolve + 1, _panel_notifications_titles(db_path))
        bodies = _panel_notifications_bodies(db_path)
        check("10e. the recovery notification body mentions the manager key", key in bodies[-1], bodies)

        # 10f: calling again on an already-resolved key is a no-op (CAS).
        count_after_resolve = _panel_notifications_count(db_path)
        await ns["_manager_recovery_resolve_auth_incident"](key)
        check("10f. resolving again on an already-resolved key sends no additional notification and raises no exception",
              _panel_notifications_count(db_path) == count_after_resolve, _panel_notifications_titles(db_path))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 11: full lifecycle simulation (classify -> open -> resolve), driven
# through the three REAL extracted functions in the same order main.py's own
# _manager_recovery_once calls them.
# ======================================================================

async def test_group_11_full_lifecycle_simulation():
    """Scope decision (documented per the task's own framing): the full
    `_manager_recovery_once` supervisor loop is NOT extracted/exercised here.
    It would require faking a large surface unrelated to the auth-incident
    behavior itself -- manager_list_rows, _manager_process_running,
    _spawn_manager_process, the monotonic-time-driven cooldown/attempt-count
    dicts, MANAGER_RECOVERY_* env-derived constants. The loop's own
    "non_recoverable -> skip restart entirely" behavior (main.py ~28268-28280)
    is a plain `if verdict == "non_recoverable": ...; continue` gated purely
    by `_manager_recovery_classify`'s return value -- already locked down by
    Group 8 above (classify returns non_recoverable for these reason
    classes) -- and the loop's process-present branch (main.py ~28230-28237)
    unconditionally calls `_manager_recovery_resolve_auth_incident` before
    any restart-related logic runs. Composing the three real functions
    directly, in the real call order, proves the actual behavior (incident
    opens on non_recoverable classification, stays deduped across repeated
    non_recoverable ticks, resolves once the process is later observed
    present) without needing a second, heavier harness that would only
    re-prove the same trivial `if/continue` control flow already covered by
    the original A-G round's own tests for the loop's structure, per this
    project's "don't duplicate coverage" convention.
    """
    print("\n-- Group 11: full lifecycle simulation (classify -> open -> resolve) --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        sig = ns["_REPL_AUTH_INCIDENT_SIGNATURE"]
        key, reason_class, error_text = "mgra11", "session_unauthorized", "AuthKeyUnregisteredError: ..."

        # Tick 1: manager process absent, start_status says session_unauthorized.
        verdict = ns["_manager_recovery_classify"](reason_class, error_text)
        check("11a. classify() returns non_recoverable for this tick's reason_class", verdict == "non_recoverable", verdict)
        if verdict == "non_recoverable":
            await ns["_manager_recovery_open_auth_incident"](key, reason_class, error_text)
        row_after_tick1 = storage.health_incident_get(key, sig, db_path=db_path)
        check("11b. after tick 1, the actionable incident is open",
              row_after_tick1 is not None and row_after_tick1.get("status") == "open", row_after_tick1)
        check("11c. after tick 1, exactly one notification (the 'requires re-login' notice) was sent",
              _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))

        # A couple more ticks while the manager is still absent with the same
        # reason -- verdict stays non_recoverable every time (no restart
        # attempt is ever made -- there is no attempt-counter/spawn call in
        # this simulated sequence at all, matching the real loop's early
        # `continue`), and the notification must never duplicate.
        for _ in range(2):
            verdict = ns["_manager_recovery_classify"](reason_class, error_text)
            check("11d. verdict remains non_recoverable on repeated ticks (no restart path is ever reached)",
                  verdict == "non_recoverable", verdict)
            if verdict == "non_recoverable":
                await ns["_manager_recovery_open_auth_incident"](key, reason_class, error_text)
        check("11e. repeated non_recoverable ticks send no duplicate notification",
              _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))

        # Tick N: the manager is later observed running again (fresh
        # authorized start) -- the real loop calls resolve_auth_incident
        # unconditionally whenever the process is present.
        await ns["_manager_recovery_resolve_auth_incident"](key)
        row_resolved = storage.health_incident_get(key, sig, db_path=db_path)
        check("11f. once the process is observed present, the incident resolves", row_resolved.get("status") == "resolved", row_resolved)
        check("11g. a recovery notification was sent (total now 2)", _panel_notifications_count(db_path) == 2, _panel_notifications_titles(db_path))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 12: reason-transition -- an existing OPEN recovery_flap incident and
# a newly-opened auth_action_required incident for the SAME manager coexist
# independently, and both resolve once the manager is genuinely fine again.
# ======================================================================

async def test_group_12_reason_transition_coexisting_incidents():
    print("\n-- Group 12: reason-transition -- recovery_flap and auth_action_required coexist --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path)
        sig = ns["_REPL_AUTH_INCIDENT_SIGNATURE"]
        key = "mgra12"

        # Seed a pre-existing OPEN recovery_flap incident directly, as if the
        # manager had been flapping earlier (matches this file's own
        # storage-level-seed convention, e.g. Group 0's direct backdating).
        storage.health_incident_upsert_open(key, "recovery_flap", db_path=db_path)
        flap_row_before = storage.health_incident_get(key, "recovery_flap", db_path=db_path)
        check("12a-setup. recovery_flap incident is open before the transition", flap_row_before.get("status") == "open", flap_row_before)

        # The SAME manager now transitions to a non_recoverable auth/identity
        # reason class.
        reason_class = "session_unauthorized"
        verdict = ns["_manager_recovery_classify"](reason_class, "")
        check("12b. the new reason_class classifies as non_recoverable", verdict == "non_recoverable", verdict)
        await ns["_manager_recovery_open_auth_incident"](key, reason_class, "detail")

        auth_row = storage.health_incident_get(key, sig, db_path=db_path)
        check("12c. a SEPARATE 'auth_action_required' incident opens for the same manager_key",
              auth_row is not None and auth_row.get("status") == "open", auth_row)
        check("12d. the signature genuinely differs from 'recovery_flap' -- both (manager_key, signature) rows coexist",
              sig != "recovery_flap", sig)

        flap_row_after_open = storage.health_incident_get(key, "recovery_flap", db_path=db_path)
        check("12e. the old recovery_flap row's own state is completely undisturbed by the new incident opening",
              flap_row_after_open == flap_row_before, (flap_row_before, flap_row_after_open))

        # The manager is later observed genuinely healthy: the flap
        # incident's own resolve path fires (unified_status==OK via
        # _health_incident_handle -- same trigger Group 4 already exercises),
        # AND the process is also observed present (triggering
        # resolve_auth_incident). Both incidents must end up resolved -- the
        # old flap incident must not remain "the active actionable incident"
        # forever once the manager is genuinely fine again, even though the
        # two incident types are tracked/resolved via two separate code
        # paths.
        await ns["_health_incident_handle"](key, ns["_HAGG_OK"], False, "", "")
        await ns["_manager_recovery_resolve_auth_incident"](key)

        flap_row_final = storage.health_incident_get(key, "recovery_flap", db_path=db_path)
        auth_row_final = storage.health_incident_get(key, sig, db_path=db_path)
        check("12f. the recovery_flap incident is resolved once the manager is genuinely healthy",
              flap_row_final.get("status") == "resolved", flap_row_final)
        check("12g. the auth_action_required incident is ALSO resolved once the process is observed present",
              auth_row_final.get("status") == "resolved", auth_row_final)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Static / regression-lock checks.
# ======================================================================

def test_static_no_override_stacking():
    print("\n-- Static: no override stacking --")
    tree = ast.parse(MAIN_SRC)
    from collections import Counter
    defs = Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for name in ("_health_incident_handle", "_health_incident_notify",
                 "_manager_recovery_classify", "_manager_recovery_open_auth_incident",
                 "_manager_recovery_resolve_auth_incident"):
        check(f"static. {name} defined exactly once at module top level", defs.get(name, 0) == 1, defs.get(name, 0))


def main() -> int:
    test_group_0_storage_crud()
    asyncio.run(test_group_1_2_flap_opens_once_no_duplicate())
    asyncio.run(test_group_3_reminder_after_long_interval())
    asyncio.run(test_group_4_resolve_on_healthy())
    asyncio.run(test_group_5_reopen_after_resolve())
    asyncio.run(test_group_6_restart_persistence())
    asyncio.run(test_group_7_manager_specific())
    test_group_8_recovery_classify_non_recoverable()
    asyncio.run(test_group_9_open_auth_incident_notifies_once())
    asyncio.run(test_group_10_resolve_auth_incident())
    asyncio.run(test_group_11_full_lifecycle_simulation())
    asyncio.run(test_group_12_reason_transition_coexisting_incidents())
    test_static_no_override_stacking()

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
