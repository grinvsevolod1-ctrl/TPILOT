# -*- coding: utf-8 -*-
"""tools/w2_cutoff_and_gates_selftest.py -- offline selftest for the W2
("Access and Delivery", frozen master plan) cutoff observability
(storage.w2_cutoff_columns_ensure / w2_cutoff_observability_snapshot,
D-24/D-03/G) and the synthetic data-delta gates (Phase H): before/after
population snapshots around storage.w2_manager_bot_access_repair_backlog,
with explicit STOP-condition checks matching MASTER_PLAN_FREEZE/10's own
10.1/10.11 rules. Also covers the "no DELETE introduced" and "banned
names / active-definition" source-level proofs (scenarios 21/23), and
records that the pre-existing W1/delivery/access-grant selftests are run
as part of the same regression pass (scenario 22 -- verified by
09_REGRESSION_RESULTS.md, not re-asserted here as a duplicate).

storage.py is directly importable (zero import-time side effects).

Pure/offline: no network, no Telegram, no production DB, no spend.

    python tools\\w2_cutoff_and_gates_selftest.py
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="w2_cutoff_gates_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE access_users(
                tg_user_id INTEGER PRIMARY KEY, scope_mode TEXT NOT NULL DEFAULT 'selected',
                is_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE access_targets(
                tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, manager_key)
            );
            CREATE TABLE manager_bot_access(
                tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL DEFAULT '',
                can_receive_cards INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
                event_cutoff_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tg_user_id, manager_key)
            );
            CREATE TABLE manager_bot_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '', manager_key TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0, lead_date TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE managers(
                manager_key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active', is_enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _seed_event(db, id_, mk):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO manager_bot_events(id, event_key, manager_key) VALUES (?,?,?)", (id_, f"k{id_}", mk))
    con.commit()
    con.close()


def _seed_mba(db, uid, mk, *, can_receive=1, revoked=0, cutoff=0):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id) VALUES (?,?,?,?,?)",
        (uid, mk, can_receive, revoked, cutoff),
    )
    con.commit()
    con.close()


def _seed_access_user(db, uid, *, is_enabled=1):
    con = sqlite3.connect(db)
    con.execute("INSERT INTO access_users(tg_user_id, is_enabled) VALUES (?,?)", (uid, is_enabled))
    con.commit()
    con.close()


# ======================================================================
# 16. Cutoff suppression is observable -- exact count, cutoff_id, and
#     manager_state, without changing event_cutoff_id or re-delivering.
# ======================================================================

def _scalar(db, sql, params=()):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


def _safe_unlink(db) -> None:
    """Best-effort cleanup: WAL mode (enabled by storage._bsl_connect) can
    leave -wal/-shm sidecar files and, on Windows specifically, a brief
    handle-release delay after the last connection closes. Never let
    cleanup failure count as a test failure."""
    import gc
    gc.collect()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db + suffix)
        except Exception:
            pass


def test_16_cutoff_suppression_observable() -> None:
    db = _make_temp_db()
    try:
        for i in range(1, 6):
            _seed_event(db, i, "mgra")
        _seed_mba(db, 100, "mgra", cutoff=3)  # events 1,2,3 suppressed; 4,5 eligible
        con = sqlite3.connect(db)
        con.execute("INSERT INTO managers(manager_key, status, is_enabled) VALUES ('mgra', 'active', 1)")
        con.commit()
        con.close()

        ens = storage.w2_cutoff_columns_ensure(db_path=db)
        check("16. cutoff columns ensure adds cutoff_reason and cutoff_set_at", set(ens["added_columns"]) == {"cutoff_reason", "cutoff_set_at"}, ens)

        before_cutoff_id = _scalar(db, "SELECT event_cutoff_id FROM manager_bot_access WHERE tg_user_id=100")
        snap = storage.w2_cutoff_observability_snapshot(db_path=db)
        check("16. snapshot reports exactly one (uid, manager_key) pair", len(snap) == 1, snap)
        entry = snap[0]
        check("16. suppressed_count is exactly 3 (events with id<=cutoff)", entry["suppressed_count"] == 3, entry)
        check("16. cutoff_id matches the stored event_cutoff_id (3)", entry["cutoff_id"] == 3, entry)
        check("16. manager_state reflects the real managers row (active:1)", entry["manager_state"] == "active:1", entry)
        check("16. no raw tg_user_id in the snapshot output", "100" not in str(entry.get("actor_ref")), entry)

        after_cutoff_id = _scalar(db, "SELECT event_cutoff_id FROM manager_bot_access WHERE tg_user_id=100")
        check("16. event_cutoff_id is BYTE-FOR-BYTE unchanged by computing the snapshot (D-24 prohibition)",
              before_cutoff_id == after_cutoff_id == 3, (before_cutoff_id, after_cutoff_id))

        log_count = _scalar(db, "SELECT COUNT(*) FROM manager_bot_cutoff_log")
        check("16. a persisted observability row was appended to manager_bot_cutoff_log",
              log_count == 1, log_count)
    finally:
        _safe_unlink(db)


def test_16b_cutoff_deleted_manager_state_visible() -> None:
    db = _make_temp_db()
    try:
        _seed_event(db, 1, "mgrb")
        _seed_mba(db, 200, "mgrb", cutoff=1)
        storage.w2_cutoff_columns_ensure(db_path=db)
        # No 'managers' row at all for mgrb -- simulates a deleted manager.
        snap = storage.w2_cutoff_observability_snapshot(db_path=db, persist=False)
        entry = next((r for r in snap if r["manager_key"] == "mgrb"), None)
        check("16b. a cutoff-suppressed pair whose manager no longer exists reports manager_state='unknown' "
              "(observable, not silently blank)", entry is not None and entry["manager_state"] == "unknown", entry)
        log_count = _scalar(db, "SELECT COUNT(*) FROM manager_bot_cutoff_log")
        check("16b. persist=False writes nothing to the log table", log_count == 0, log_count)
    finally:
        _safe_unlink(db)


# ======================================================================
# Phase H: synthetic data-delta gate around the backlog repair -- before/
# after snapshot, STOP conditions (revoked/disabled counts unchanged,
# access_targets count never decreases), dry_run vs real run.
# ======================================================================

def test_h_backlog_repair_data_delta_gate() -> None:
    db = _make_temp_db()
    try:
        # Class 1 (harmful, D-02): active grant, no access_targets row.
        _seed_access_user(db, 10)
        _seed_mba(db, 10, "mgr_missing_target", can_receive=1, revoked=0)
        # Revoked row -- must NEVER be touched.
        _seed_access_user(db, 11)
        _seed_mba(db, 11, "mgr_revoked", can_receive=1, revoked=1)
        # Disabled access_users row -- must NEVER be touched.
        _seed_access_user(db, 12, is_enabled=0)
        _seed_mba(db, 12, "mgr_user_disabled", can_receive=1, revoked=0)
        # Already-consistent row -- untouched, not a candidate.
        _seed_access_user(db, 13)
        _seed_mba(db, 13, "mgr_ok", can_receive=1, revoked=0)
        con = sqlite3.connect(db)
        con.execute("INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (13, 'mgr_ok', '')")
        con.commit()
        con.close()

        before = {
            "access_targets": _scalar(db, "SELECT COUNT(*) FROM access_targets"),
            "revoked": _scalar(db, "SELECT COUNT(*) FROM manager_bot_access WHERE revoked=1"),
            "disabled": _scalar(db, "SELECT COUNT(*) FROM access_users WHERE is_enabled=0"),
        }

        dry = storage.w2_manager_bot_access_repair_backlog(db_path=db, dry_run=True)
        check("H. dry_run computes exactly 1 candidate (only the D-02 harmful class)", dry["candidate_count"] == 1, dry)
        check("H. dry_run performs NO write (access_targets count unchanged)",
              _scalar(db, "SELECT COUNT(*) FROM access_targets") == before["access_targets"], dry)

        real = storage.w2_manager_bot_access_repair_backlog(db_path=db, dry_run=False)
        check("H. real run status='repaired'", real["status"] == "repaired", real)
        check("H. real run repairs exactly 1 row", real["repaired_count"] == 1, real)

        after = {
            "access_targets": _scalar(db, "SELECT COUNT(*) FROM access_targets"),
            "revoked": _scalar(db, "SELECT COUNT(*) FROM manager_bot_access WHERE revoked=1"),
            "disabled": _scalar(db, "SELECT COUNT(*) FROM access_users WHERE is_enabled=0"),
        }
        check("H. [STOP-condition] revoked-row count is BYTE-FOR-BYTE unchanged (I-36)",
              after["revoked"] == before["revoked"], (before, after))
        check("H. [STOP-condition] disabled access_users count is BYTE-FOR-BYTE unchanged",
              after["disabled"] == before["disabled"], (before, after))
        check("H. [STOP-condition] access_targets count only INCREASED, by exactly 1",
              after["access_targets"] == before["access_targets"] + 1, (before, after))

        row = _scalar(db, "SELECT COUNT(*) FROM access_targets WHERE tg_user_id=10 AND manager_key='mgr_missing_target'")
        revoked_row_untouched = _scalar(db, "SELECT COUNT(*) FROM access_targets WHERE tg_user_id=11 AND manager_key='mgr_revoked'")
        disabled_row_untouched = _scalar(db, "SELECT COUNT(*) FROM access_targets WHERE tg_user_id=12 AND manager_key='mgr_user_disabled'")
        check("H. the correct row (uid=10) was actually repaired", row == 1, row)
        check("H. the REVOKED row was NOT repaired (never auto-restored)", revoked_row_untouched == 0, revoked_row_untouched)
        check("H. the user-DISABLED row was NOT repaired", disabled_row_untouched == 0, disabled_row_untouched)

        # Idempotent: running again finds nothing left to do.
        again = storage.w2_manager_bot_access_repair_backlog(db_path=db, dry_run=False)
        check("H. re-running the repair is idempotent (status='no_op', 0 candidates left)",
              again["status"] == "no_op" and again["candidate_count"] == 0, again)
    finally:
        _safe_unlink(db)


# ======================================================================
# 21. No DELETE is introduced anywhere in the W2 storage.py block, for
#     "queue cleanup" or any other purpose.
# ======================================================================

def test_21_no_delete_in_w2_block() -> None:
    src = open(str(BASE_DIR / "storage.py"), encoding="utf-8-sig").read()
    start = src.index("# --- TPILOT W2 ACCESS & DELIVERY 20260729 START ---")
    end = src.index("# --- TPILOT W2 ACCESS & DELIVERY 20260729 END ---", start)
    block = src[start:end]
    deletes = re.findall(r"\bDELETE\s+FROM\b", block, flags=re.IGNORECASE)
    check("21. the W2 storage.py block contains ZERO 'DELETE FROM' statements", len(deletes) == 0, deletes)

    mb_src = open(str(BASE_DIR / "manager_bot.py"), encoding="utf-8-sig").read()
    mstart = mb_src.index("# --- TPILOT W2 ACCESS & DELIVERY 20260729 START ---")
    mend = mb_src.index("# --- TPILOT W2 ACCESS & DELIVERY 20260729 END ---", mstart)
    mblock = mb_src[mstart:mend]
    mdeletes = re.findall(r"\bDELETE\s+FROM\b", mblock, flags=re.IGNORECASE)
    check("21. the W2 manager_bot.py block contains ZERO 'DELETE FROM' statements", len(mdeletes) == 0, mdeletes)


# ======================================================================
# 23. Active-definition proof: the two permanently-banned names (D-04)
#     are still absent from manager_bot.py, and every W2-touched function
#     name still resolves to EXACTLY ONE active definition (no accidental
#     duplicate def introduced by this patch).
# ======================================================================

def test_23_active_definition_proof() -> None:
    mb_src = open(str(BASE_DIR / "manager_bot.py"), encoding="utf-8-sig").read()
    for banned in ("_reconcile_reconnected_manager", "_auto_grant_manager_bot_access"):
        defs = re.findall(rf"^def {banned}\(", mb_src, flags=re.MULTILINE)
        check(f"23. banned name {banned!r} still has ZERO function DEFINITIONS (comment-only mentions are fine)",
              len(defs) == 0, defs)

    tree = ast.parse(mb_src)
    top_defs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for name in ("_mb_classify_delivery_error", "_mb_backoff_seconds_with_jitter", "_mb_claim_event",
                 "_linked_manager_keys", "_mark_send_attempt"):
        count = top_defs.count(name)
        check(f"23. new/modified W2 function {name!r} has EXACTLY ONE top-level definition (no accidental duplicate)",
              count == 1, count)

    storage_src = open(str(BASE_DIR / "storage.py"), encoding="utf-8-sig").read()
    stree = ast.parse(storage_src)
    stop_defs = [n.name for n in stree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for name in ("w2_resolve_delivery_manager_keys", "w2_detect_access_divergence",
                 "w2_manager_bot_access_repair_backlog", "w2_manager_lead_cards_add_date_identity",
                 "w2_cutoff_columns_ensure", "w2_cutoff_observability_snapshot"):
        count = stop_defs.count(name)
        check(f"23. new W2 storage.py function {name!r} has EXACTLY ONE top-level definition",
              count == 1, count)


def main() -> int:
    test_16_cutoff_suppression_observable()
    test_16b_cutoff_deleted_manager_state_visible()
    test_h_backlog_repair_data_delta_gate()
    test_21_no_delete_in_w2_block()
    test_23_active_definition_proof()

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
