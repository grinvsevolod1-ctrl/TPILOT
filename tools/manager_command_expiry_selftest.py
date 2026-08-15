# -*- coding: utf-8 -*-
"""tools/manager_command_expiry_selftest.py -- offline self-test for
"TPILOT F 20260718" (plan principle F): the panel_commands / manager_commands
expired-command sweeper, plus the commit-deadline expiry selection query that
feeds the same controller sweep loop's automatic-rollback trigger.

Technique: unlike main.py, storage.py IS importable directly (no Telethon/
env side effects at import time) -- every function under test here is a
plain `async def` (or, for the replacement-commit-deadline query, a plain
sync `def`) sqlite helper, called FOR REAL against a throwaway temp SQLite
file, matching tools/manager_profile_sync_selftest.py's own "storage.py is
importable" convention. No AST extraction is needed for this file.

Scope decision (main.py orchestration vs. storage-layer only):
  main.py's actual sweep loop (`_manager_command_sweep_loop`) and the
  commit-deadline auto-rollback executor it calls (`_repl_commit_deadline_
  sweep_once` -> `_repl4_rollback`) are NOT extracted/exercised here.
  `_repl_commit_deadline_sweep_once` (main.py, ~line 34893) is a thin,
  fully-covered pass-through: it calls `storage.replacement_list_commit_
  phase_past_deadline(now_iso)` for the selection, then `_repl4_rollback`
  for each row -- and `_repl4_rollback` itself is already thoroughly covered
  by a parallel selftest (tools/manager_replacement_commit_selftest.py's own
  Group 12 "rollback" tests). Duplicating that here would test the same
  rollback internals twice for no gain. Testing storage.
  replacement_list_commit_phase_past_deadline directly -- the actual
  selection/filter logic driving what the sweep picks up -- fully proves the
  "sweep/expiry mechanics" this file is scoped to, without needing a fake
  for `_manager_process_running`/`_stop_manager_process`/`manager_get`/etc.
  Likewise `manager_queue_take_next`'s own expired-row exclusion and
  `manager_queue_mark_timeouts`'s sweep transition are both provable
  entirely at the storage layer -- main.py's `_manager_command_loop` (its
  own final expiry guard at claim/execution time) adds no additional
  selection logic on top of what manager_queue_take_next's SQL WHERE clause
  already guarantees, so it is not separately extracted either.

Never: real Telegram network, production DB/runtime/session/log access.

    python tools\\manager_command_expiry_selftest.py
"""
from __future__ import annotations

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


def _selftest_db_guard(db_path: str, base_dir, storage_mod) -> None:
    """Mandatory production-path DB guard, same convention as every other
    tools/*_selftest.py in this project."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="cmd_expiry_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, storage)
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _now() -> datetime:
    return datetime.utcnow()


def _seed_replacement_row(db_path: str, *, operation_id: str, old_manager_key: str, status: str,
                           commit_deadline_at: str, stage: str = "") -> None:
    """Direct row insert (bypassing the full replacement_create/advance
    state-machine, matching the "arbitrary durable state" technique already
    used by tools/manager_replacement_commit_selftest.py's own
    seed_ready_commit_op) -- this file only needs a manager_replacements row
    with a given status/commit_deadline_at (and, for TPILOT FIX-4 20260718b's
    stage-exclusion coverage, a given stage), not a fully-valid operation.
    stage defaults to '' (matches the column's own DEFAULT for pre-existing
    rows / rows never touched by the stage machinery)."""
    storage.ensure_replacement_tables(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO manager_replacements(operation_id, status, stage, old_manager_key, commit_deadline_at, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (operation_id, status, stage, old_manager_key, commit_deadline_at, _iso(_now()), _iso(_now())),
        )
        con.commit()
    finally:
        con.close()


# ======================================================================
# GROUP 1: manager_queue_take_next never returns an expired command.
# ======================================================================

async def test_group_1_take_next_excludes_expired():
    print("\n-- Group 1: manager_queue_take_next excludes expired commands --")
    tmp_root, db_path = make_temp_env()
    try:
        past = _iso(_now() - timedelta(minutes=5))
        nonce = await storage.manager_queue_put(
            target_key="mgrA", command="ping", expires_at=past, db_path=db_path,
        )
        row = await storage.manager_queue_take_next("mgrA", db_path=db_path)
        check("1a. an already-expired 'new' command is never returned by manager_queue_take_next"
              " (locked-in regression -- foundational to the whole fix)", row is None, row)

        got = await storage.manager_queue_get(nonce, db_path=db_path)
        check("1b. the row itself is untouched (still status='new', not silently claimed)",
              got is not None and got.get("status") == "new", got)
    finally:
        cleanup_env(tmp_root)


async def test_group_1b_take_next_returns_unexpired():
    print("\n-- Group 1b: control case -- an unexpired command IS returned --")
    tmp_root, db_path = make_temp_env()
    try:
        future = _iso(_now() + timedelta(minutes=5))
        nonce = await storage.manager_queue_put(
            target_key="mgrB", command="ping", expires_at=future, db_path=db_path,
        )
        row = await storage.manager_queue_take_next("mgrB", db_path=db_path)
        check("1b-1. an unexpired command IS returned (proves 1a isn't vacuously true)", row is not None and row.get("nonce") == nonce, row)
        check("1b-2. taking it marks it 'processing'", row is not None and row.get("status") == "processing", row)

        no_expiry_nonce = await storage.manager_queue_put(target_key="mgrB2", command="ping", db_path=db_path)
        row2 = await storage.manager_queue_take_next("mgrB2", db_path=db_path)
        check("1b-3. a command with NO expires_at at all is returned normally", row2 is not None and row2.get("nonce") == no_expiry_nonce, row2)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 2: manager_queue_mark_timeouts transitions expired rows exactly once.
# ======================================================================

async def test_group_2_mark_timeouts_once():
    print("\n-- Group 2: manager_queue_mark_timeouts transitions exactly once --")
    tmp_root, db_path = make_temp_env()
    try:
        past = _iso(_now() - timedelta(minutes=5))
        nonce = await storage.manager_queue_put(target_key="mgrC", command="ping", expires_at=past, db_path=db_path)

        n1 = await storage.manager_queue_mark_timeouts(db_path=db_path)
        check("2a. first sweep reports rowcount > 0", n1 > 0, n1)
        row1 = await storage.manager_queue_get(nonce, db_path=db_path)
        check("2b. row transitioned to status='timeout'", row1 is not None and row1.get("status") == "timeout", row1)
        check("2c. finished_at was stamped", row1 is not None and bool(row1.get("finished_at")), row1)
        first_finished_at = row1.get("finished_at")
        first_error_text = row1.get("error_text")

        n2 = await storage.manager_queue_mark_timeouts(db_path=db_path)
        check("2d. second sweep on the same (now-terminal) row reports rowcount == 0 (no re-processing)", n2 == 0, n2)
        row2 = await storage.manager_queue_get(nonce, db_path=db_path)
        check("2e. row stays 'timeout', finished_at/error_text unchanged by the second sweep",
              row2.get("status") == "timeout" and row2.get("finished_at") == first_finished_at and row2.get("error_text") == first_error_text,
              row2)
    finally:
        cleanup_env(tmp_root)


async def test_group_2b_mark_timeouts_catches_processing():
    print("\n-- Group 2b: a claimed-but-never-finished ('processing') row past expiry is ALSO caught --")
    tmp_root, db_path = make_temp_env()
    try:
        future_when_claimed = _iso(_now() + timedelta(seconds=1))
        nonce = await storage.manager_queue_put(target_key="mgrD", command="ping", expires_at=future_when_claimed, db_path=db_path)
        row = await storage.manager_queue_take_next("mgrD", db_path=db_path)
        check("2b-setup. row was claimed (now 'processing')", row is not None and row.get("status") == "processing", row)

        # Simulate time passing: the worker never called manager_queue_finish,
        # and expires_at has now passed.
        past = _iso(_now() - timedelta(minutes=1))
        con = sqlite3.connect(db_path)
        try:
            con.execute("UPDATE manager_commands SET expires_at=? WHERE nonce=?", (past, nonce))
            con.commit()
        finally:
            con.close()

        n = await storage.manager_queue_mark_timeouts(db_path=db_path)
        check("2b-1. sweep catches the abandoned 'processing' row too", n > 0, n)
        after = await storage.manager_queue_get(nonce, db_path=db_path)
        check("2b-2. it transitions to 'timeout' (matches the real SQL's status IN ('new','processing') clause)",
              after is not None and after.get("status") == "timeout", after)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 3: manager_queue_finalize_all_for_manager -- manager-scoped
# abandonment, ignores expires_at entirely, never touches other managers.
# ======================================================================

async def test_group_3_finalize_all_for_manager():
    print("\n-- Group 3: manager_queue_finalize_all_for_manager --")
    tmp_root, db_path = make_temp_env()
    try:
        far_future = _iso(_now() + timedelta(days=365))
        n1 = await storage.manager_queue_put(target_key="mgrE", command="a", expires_at=far_future, db_path=db_path)
        n2 = await storage.manager_queue_put(target_key="mgrE", command="b", db_path=db_path)  # no expiry at all
        row_processing = await storage.manager_queue_take_next("mgrE", db_path=db_path)
        n_other = await storage.manager_queue_put(target_key="mgrF", command="c", expires_at=far_future, db_path=db_path)

        finalized = await storage.manager_queue_finalize_all_for_manager("mgrE", db_path=db_path)
        check("3a. finalizes ALL pending (new+processing) commands for the manager", finalized == 2, finalized)

        row1 = await storage.manager_queue_get(n1, db_path=db_path)
        row2 = await storage.manager_queue_get(n2, db_path=db_path)
        check("3b. the far-future-expiry 'new' row was still finalized (manager-scoped, not expiry-scoped)",
              row1 is not None and row1.get("status") == "timeout", row1)
        check("3c. the no-expiry row was finalized too", row2 is not None and row2.get("status") == "timeout", row2)
        check("3d. error_text defaults to 'rollback' when not given", row1.get("error_text") == "rollback", row1)

        row_other = await storage.manager_queue_get(n_other, db_path=db_path)
        check("3e. a different manager_key's command is completely untouched", row_other is not None and row_other.get("status") == "new", row_other)

        finalized_again = await storage.manager_queue_finalize_all_for_manager("mgrE", db_path=db_path)
        check("3f. re-running on an already-finalized manager is a safe no-op (rowcount 0)", finalized_again == 0, finalized_again)

        finalized_empty = await storage.manager_queue_finalize_all_for_manager("mgr_never_had_commands", db_path=db_path)
        check("3g. a manager_key with no commands at all is a safe no-op", finalized_empty == 0, finalized_empty)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 4: manager_queue_cleanup_finished -- retention-window pruning.
# ======================================================================

async def test_group_4_cleanup_finished():
    print("\n-- Group 4: manager_queue_cleanup_finished --")
    tmp_root, db_path = make_temp_env()
    try:
        n_old_done = await storage.manager_queue_put(target_key="mgrG", command="a", db_path=db_path)
        await storage.manager_queue_take_next("mgrG", db_path=db_path)
        await storage.manager_queue_finish(n_old_done, worker_key="mgrG", ok=True, db_path=db_path)

        n_recent_done = await storage.manager_queue_put(target_key="mgrG", command="b", db_path=db_path)
        await storage.manager_queue_take_next("mgrG", db_path=db_path)
        await storage.manager_queue_finish(n_recent_done, worker_key="mgrG", ok=True, db_path=db_path)

        n_still_new = await storage.manager_queue_put(target_key="mgrG", command="c", db_path=db_path)

        n_still_processing = await storage.manager_queue_put(target_key="mgrG", command="d", db_path=db_path)
        await storage.manager_queue_take_next("mgrG", db_path=db_path)

        # Backdate the "old" finished row far past any reasonable retention
        # window, and leave the "recent" one just-finished.
        very_old = _iso(_now() - timedelta(days=30))
        # Also backdate the still-new/still-processing rows' created_at, to
        # prove age alone never causes cleanup on a non-terminal row.
        con = sqlite3.connect(db_path)
        try:
            con.execute("UPDATE manager_commands SET finished_at=? WHERE nonce=?", (very_old, n_old_done))
            con.execute("UPDATE manager_commands SET created_at=? WHERE nonce IN (?,?)", (very_old, n_still_new, n_still_processing))
            con.commit()
        finally:
            con.close()

        deleted = await storage.manager_queue_cleanup_finished(older_than_sec=86400, db_path=db_path)
        check("4a. exactly the old finished row was deleted", deleted == 1, deleted)

        check("4b. the old finished row is gone", await storage.manager_queue_get(n_old_done, db_path=db_path) is None, None)
        check("4c. the recent finished row (well within retention) survives",
              await storage.manager_queue_get(n_recent_done, db_path=db_path) is not None, None)
        check("4d. a still-'new' row survives regardless of age", await storage.manager_queue_get(n_still_new, db_path=db_path) is not None, None)
        check("4e. a still-'processing' row survives regardless of age",
              await storage.manager_queue_get(n_still_processing, db_path=db_path) is not None, None)

        deleted_again = await storage.manager_queue_cleanup_finished(older_than_sec=86400, db_path=db_path)
        check("4f. re-running cleanup immediately after is a safe no-op", deleted_again == 0, deleted_again)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 5: storage.replacement_list_commit_phase_past_deadline.
# ======================================================================

async def test_group_5_commit_phase_past_deadline():
    print("\n-- Group 5: replacement_list_commit_phase_past_deadline --")
    tmp_root, db_path = make_temp_env()
    try:
        now_iso = _iso(_now())
        past = _iso(_now() - timedelta(minutes=10))
        future = _iso(_now() + timedelta(hours=1))

        _seed_replacement_row(db_path, operation_id="op-past-committing", old_manager_key="old1", status="committing", commit_deadline_at=past)
        _seed_replacement_row(db_path, operation_id="op-past-linkspending", old_manager_key="old2", status="links_pending", commit_deadline_at=past)
        _seed_replacement_row(db_path, operation_id="op-past-linksready", old_manager_key="old3", status="links_ready", commit_deadline_at=past)
        _seed_replacement_row(db_path, operation_id="op-future", old_manager_key="old4", status="committing", commit_deadline_at=future)
        _seed_replacement_row(db_path, operation_id="op-empty-deadline", old_manager_key="old5", status="committing", commit_deadline_at="")
        _seed_replacement_row(db_path, operation_id="op-wrong-status-ready", old_manager_key="old6", status="ready_commit", commit_deadline_at=past)
        _seed_replacement_row(db_path, operation_id="op-wrong-status-cutover", old_manager_key="old7", status="cutover_done", commit_deadline_at=past)
        _seed_replacement_row(db_path, operation_id="op-wrong-status-failed", old_manager_key="old8", status="failed", commit_deadline_at=past)

        rows = storage.replacement_list_commit_phase_past_deadline(now_iso, db_path=db_path)
        ids = {r["operation_id"] for r in rows}

        check("5a. committing/past-deadline row IS returned", "op-past-committing" in ids, ids)
        check("5b. links_pending/past-deadline row IS returned", "op-past-linkspending" in ids, ids)
        check("5c. links_ready/past-deadline row IS returned", "op-past-linksready" in ids, ids)
        check("5d. a future-deadline row is NOT returned", "op-future" not in ids, ids)
        check("5e. an empty-commit_deadline_at row is NOT returned (no deadline to expire)", "op-empty-deadline" not in ids, ids)
        check("5f. a non-matching status (ready_commit) is NOT returned even past deadline", "op-wrong-status-ready" not in ids, ids)
        check("5g. a non-matching status (cutover_done) is NOT returned even past deadline", "op-wrong-status-cutover" not in ids, ids)
        check("5h. a non-matching status (failed) is NOT returned even past deadline", "op-wrong-status-failed" not in ids, ids)
        check("5i. exactly the 3 expected rows are returned, nothing extra", len(ids) == 3, ids)

        rows_again = storage.replacement_list_commit_phase_past_deadline(now_iso, db_path=db_path)
        ids_again = {r["operation_id"] for r in rows_again}
        check("5j. idempotent: a read-only selection query returns the identical set on a second call "
              "(nothing here mutates state -- the real caller's own rollback step is what would advance "
              "status, tested separately in manager_replacement_commit_selftest.py's Group 12)",
              ids_again == ids, (ids, ids_again))

        # TPILOT FIX-4 20260718b: stage-exclusion coverage. status='links_ready'
        # + commit_deadline_at in the past would normally match, but a stage
        # that already reached the durable cutover point-of-no-return
        # ('cutover_started' / 'old_manager_retired') must be excluded even
        # though status alone still says 'links_ready' -- the SQL-level half
        # of the sweeper/cutover race fix (main.py's _repl4_rollback
        # independently refuses on the same condition).
        _seed_replacement_row(db_path, operation_id="op-stage-cutover-started", old_manager_key="old9",
                               status="links_ready", commit_deadline_at=past, stage="cutover_started")
        _seed_replacement_row(db_path, operation_id="op-stage-old-retired", old_manager_key="old10",
                               status="links_ready", commit_deadline_at=past, stage="old_manager_retired")
        _seed_replacement_row(db_path, operation_id="op-stage-revalidated", old_manager_key="old11",
                               status="links_ready", commit_deadline_at=past, stage="revalidated")

        rows_stage = storage.replacement_list_commit_phase_past_deadline(now_iso, db_path=db_path)
        ids_stage = {r["operation_id"] for r in rows_stage}
        check("5k. stage='cutover_started' + past-deadline + status='links_ready' is EXCLUDED "
              "(point-of-no-return reached, sweeper must not roll it back)",
              "op-stage-cutover-started" not in ids_stage, ids_stage)
        check("5l. stage='old_manager_retired' + past-deadline + status='links_ready' is EXCLUDED "
              "(point-of-no-return reached, sweeper must not roll it back)",
              "op-stage-old-retired" not in ids_stage, ids_stage)
        check("5m. regression control: stage='revalidated' (a normal pre-cutover stage) + past-deadline "
              "+ status='links_ready' IS still returned -- proves the exclusion is scoped to exactly the "
              "two irreversible stage values and does not over-exclude",
              "op-stage-revalidated" in ids_stage, ids_stage)
    finally:
        cleanup_env(tmp_root)


def main() -> int:
    asyncio.run(test_group_1_take_next_excludes_expired())
    asyncio.run(test_group_1b_take_next_returns_unexpired())
    asyncio.run(test_group_2_mark_timeouts_once())
    asyncio.run(test_group_2b_mark_timeouts_catches_processing())
    asyncio.run(test_group_3_finalize_all_for_manager())
    asyncio.run(test_group_4_cleanup_finished())
    asyncio.run(test_group_5_commit_phase_past_deadline())

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
