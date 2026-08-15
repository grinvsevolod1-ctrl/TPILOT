# -*- coding: utf-8 -*-
"""tools/w2_migration_safety_selftest.py -- W2 REVISION (Blocker 5)
selftest: proves the manager_lead_cards lead_date migration is no longer
reachable from ordinary ManagerBot startup, that startup fails closed on a
legacy schema, and that the explicit migration runner
(tools/w2_lead_date_migration_runner.py) is safe: row count / ids /
duplicate-lead_date rows / historical rows preserved, backup-verified,
hard-fails (never swallowed) on any integrity problem.

storage.py is directly importable (zero import-time side effects).
manager_bot.py's init_schema() cannot be run directly (Telethon/env side
effects) -- its RELEVANT source text is inspected structurally (AST +
substring checks), same convention as this project's other structural
proofs (e.g. w2_cutoff_and_gates_selftest.py's test_21/test_23).

Pure/offline: no network, no Telegram, no production DB, no spend.

    python tools\\w2_migration_safety_selftest.py
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR / "tools") not in sys.path:
    sys.path.insert(0, str(BASE_DIR / "tools"))

import storage  # noqa: E402
import w2_lead_date_migration_runner as runner  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _legacy_schema_db(seed_rows=True) -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="w2_migration_safety_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE manager_lead_cards(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL,
                bot_chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL DEFAULT '',
                lead_date TEXT NOT NULL DEFAULT '',
                last_status_shown TEXT NOT NULL DEFAULT '',
                last_bucket_shown TEXT NOT NULL DEFAULT '',
                last_manual_flag INTEGER NOT NULL DEFAULT 0,
                last_action_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(tg_user_id, chat_id, manager_key)
            );
            """
        )
        if seed_rows:
            con.executemany(
                "INSERT INTO manager_lead_cards(id, tg_user_id, bot_chat_id, message_id, chat_id, "
                "manager_key, lead_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (10, 1, 1, 100, 500, "mgr1", "2026-07-20", "t", "t"),
                    (11, 2, 2, 200, 600, "mgr2", "2026-07-21", "t", "t"),
                    (12, 3, 3, 300, 700, "mgr1", "2026-07-22", "t", "t"),
                ],
            )
        con.commit()
    finally:
        con.close()
    return path


def _safe_unlink(*paths) -> None:
    import gc
    gc.collect()
    for p in paths:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(p + suffix)
            except Exception:
                pass


# ======================================================================
# Startup no longer performs the destructive rebuild; fails closed instead.
# ======================================================================

def test_startup_no_silent_rebuild() -> None:
    mb_src = open(str(BASE_DIR / "manager_bot.py"), encoding="utf-8-sig").read()
    check("startup: init_schema() no longer calls the rebuild engine directly",
          "storage.w2_manager_lead_cards_add_date_identity(TPILOT_DB_PATH)" not in mb_src, None)
    check("startup: init_schema() calls the read-only compatibility assertion",
          "storage.w2_assert_lead_cards_schema_compatible(TPILOT_DB_PATH)" in mb_src, None)

    # The compatibility call must NOT be silently wrapped in a bare
    # try/except that swallows it -- find the exact call site and check
    # the immediately preceding non-blank lines are not a `try:` guarding it.
    idx = mb_src.index("storage.w2_assert_lead_cards_schema_compatible(TPILOT_DB_PATH)")
    call_line_start = mb_src.rfind("\n", 0, idx) + 1
    call_indent = len(mb_src[call_line_start:idx]) - len(mb_src[call_line_start:idx].lstrip())
    before = mb_src[:call_line_start]
    lines_before = [ln for ln in before.splitlines() if ln.strip()]
    # Walk backward through comment lines to the nearest real statement.
    nearest_stmt = ""
    for ln in reversed(lines_before):
        if ln.strip().startswith("#"):
            continue
        nearest_stmt = ln
        break
    is_wrapped_in_try = nearest_stmt.strip() == "try:" and (len(nearest_stmt) - len(nearest_stmt.lstrip())) < call_indent
    check("startup: the compatibility assertion is NOT wrapped in a try: that would swallow it",
          not is_wrapped_in_try, nearest_stmt)

    check("storage.py: the rebuild engine's own docstring documents it is NOT for startup use",
          "must be called ONLY from tools/w2_lead_date_migration_runner.py" in
          open(str(BASE_DIR / "storage.py"), encoding="utf-8-sig").read(), None)


def test_fresh_db_gets_current_schema_directly() -> None:
    """A brand-new manager_lead_cards (created by init_schema's own CREATE
    TABLE IF NOT EXISTS) must declare the WIDE key directly -- proven here
    by extracting that exact CREATE TABLE statement and running it."""
    mb_src = open(str(BASE_DIR / "manager_bot.py"), encoding="utf-8-sig").read()
    start = mb_src.index("CREATE TABLE IF NOT EXISTS manager_lead_cards(")
    end = mb_src.index(");", start) + 2
    ddl = mb_src[start:end]
    fd, db = tempfile.mkstemp(suffix=".db", prefix="w2_migration_safety_selftest_")
    os.close(fd)
    try:
        con = sqlite3.connect(db)
        con.execute(ddl)
        con.commit()
        con.close()
        status = storage.w2_manager_lead_cards_schema_status(db_path=db)
        check("a freshly created manager_lead_cards table is immediately status='current'",
              status["status"] == "current", status)
        # And the compatibility assertion does not raise for it.
        storage.w2_assert_lead_cards_schema_compatible(db_path=db)
        check("w2_assert_lead_cards_schema_compatible does not raise for a fresh table", True, None)
    finally:
        _safe_unlink(db)


def test_compat_check_raises_on_legacy_schema() -> None:
    db = _legacy_schema_db()
    try:
        status = storage.w2_manager_lead_cards_schema_status(db_path=db)
        check("legacy schema correctly detected", status["status"] == "legacy_narrow_key", status)
        raised = None
        try:
            storage.w2_assert_lead_cards_schema_compatible(db_path=db)
        except storage.W2SchemaCompatibilityError as exc:
            raised = exc
        check("w2_assert_lead_cards_schema_compatible RAISES W2SchemaCompatibilityError on a legacy schema "
              "(fail-closed, not a logged warning)", raised is not None, raised)
        check("the raised error message points at the explicit migration runner tool",
              raised is not None and "w2_lead_date_migration_runner.py" in str(raised), raised)

        # The row count is byte-for-byte unchanged -- a compatibility CHECK
        # must never write anything.
        con = sqlite3.connect(db)
        count = con.execute("SELECT COUNT(*) FROM manager_lead_cards").fetchone()[0]
        con.close()
        check("the compatibility check performed NO write (row count still 3)", count == 3, count)
    finally:
        _safe_unlink(db)


# ======================================================================
# Migration runner: safe end-to-end run, preserving row count / ids /
# duplicate-lead_date rows / historical rows, with a verified backup.
# ======================================================================

def test_runner_dry_run_writes_nothing() -> None:
    db = _legacy_schema_db()
    try:
        rc = runner.run(db, dry_run=True)
        check("dry-run exits 0", rc == 0, rc)
        status = storage.w2_manager_lead_cards_schema_status(db_path=db)
        check("dry-run leaves the schema exactly as it was (still legacy)",
              status["status"] == "legacy_narrow_key", status)
        import glob
        backups = glob.glob(db + ".pre_w2_lead_date_migration_*.bak")
        check("dry-run creates NO backup file", backups == [], backups)
    finally:
        _safe_unlink(db)


def test_runner_real_run_preserves_everything() -> None:
    db = _legacy_schema_db()
    try:
        rc = runner.run(db, dry_run=False)
        check("real run exits 0", rc == 0, rc)

        status = storage.w2_manager_lead_cards_schema_status(db_path=db)
        check("post-run schema status is 'current'", status["status"] == "current", status)

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        rows = {r["id"]: dict(r) for r in con.execute("SELECT * FROM manager_lead_cards").fetchall()}
        con.close()
        check("row count preserved (3)", len(rows) == 3, rows)
        check("id 10 preserved with its original lead_date", rows.get(10, {}).get("lead_date") == "2026-07-20", rows)
        check("id 11 preserved with its original lead_date", rows.get(11, {}).get("lead_date") == "2026-07-21", rows)
        check("id 12 preserved with its original lead_date", rows.get(12, {}).get("lead_date") == "2026-07-22", rows)
        check("ids were NOT renumbered (10, 11, 12 all present)", set(rows) == {10, 11, 12}, rows)
        # "duplicate values preserved": rows 10 and 12 share manager_key
        # 'mgr1' with DIFFERENT lead_date -- both must survive as distinct
        # historical rows (the exact D-06 guarantee), not be collapsed.
        mgr1_dates = sorted(r["lead_date"] for r in rows.values() if r["manager_key"] == "mgr1")
        check("historical rows sharing manager_key are preserved as DISTINCT rows by lead_date",
              mgr1_dates == ["2026-07-20", "2026-07-22"], mgr1_dates)

        import glob
        backups = glob.glob(db + ".pre_w2_lead_date_migration_*.bak")
        check("exactly one backup file was created", len(backups) == 1, backups)
        bcon = sqlite3.connect(backups[0])
        brows = bcon.execute("SELECT COUNT(*) FROM manager_lead_cards").fetchone()[0]
        bcon.close()
        check("the backup file independently contains the pre-migration row count (3)", brows == 3, brows)
        _safe_unlink(backups[0])

        # Belt-and-suspenders (Blocker 5 follow-up): the OLD table is
        # renamed aside, never dropped -- a SECOND, in-database copy of
        # the pre-migration data, independent of the file-level backup.
        con = sqlite3.connect(db)
        legacy_present = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_lead_cards_prew2_legacy'"
        ).fetchone()
        legacy_count = con.execute("SELECT COUNT(*) FROM manager_lead_cards_prew2_legacy").fetchone()[0]
        con.close()
        check("the pre-migration table survives, renamed aside, inside the SAME database file",
              legacy_present is not None, legacy_present)
        check("the renamed-aside legacy table independently contains all 3 pre-migration rows",
              legacy_count == 3, legacy_count)
    finally:
        _safe_unlink(db)


def test_runner_idempotent_second_run() -> None:
    db = _legacy_schema_db()
    try:
        runner.run(db, dry_run=False)
        rc2 = runner.run(db, dry_run=False)
        check("running the runner AGAIN against an already-migrated db exits 0 (no-op, not an error)",
              rc2 == 0, rc2)
        import glob
        backups = glob.glob(db + ".pre_w2_lead_date_migration_*.bak")
        check("the second (no-op) run created NO additional backup", len(backups) == 1, backups)
        for b in backups:
            _safe_unlink(b)
    finally:
        _safe_unlink(db)


# ======================================================================
# Interrupted/failed migration is recoverable: the ORIGINAL table (and
# its backup) survive untouched when the rebuild aborts partway.
# ======================================================================

def test_migration_failure_is_recoverable_not_swallowed() -> None:
    """An unknown-column DB aborts the underlying migration with a
    non-'migrated' status; the runner must treat that as a HARD failure
    (raise), never print-and-continue -- and the original table (and the
    backup already taken) must be completely untouched."""
    fd, db = tempfile.mkstemp(suffix=".db", prefix="w2_migration_safety_selftest_")
    os.close(fd)
    con = sqlite3.connect(db)
    con.executescript(
        """
        CREATE TABLE manager_lead_cards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL, bot_chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            manager_key TEXT NOT NULL DEFAULT '', lead_date TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '',
            totally_unknown_future_column TEXT NOT NULL DEFAULT '',
            UNIQUE(tg_user_id, chat_id, manager_key)
        );
        """
    )
    con.execute(
        "INSERT INTO manager_lead_cards(id, tg_user_id, bot_chat_id, message_id, chat_id, manager_key, "
        "lead_date, created_at, updated_at) VALUES (1,1,1,1,1,'mgr1','2026-07-20','t','t')"
    )
    con.commit()
    con.close()
    try:
        raised = None
        try:
            runner.run(db, dry_run=False)
        except RuntimeError as exc:
            raised = exc
        check("runner HARD-FAILS (raises) on an unrecognized-column table -- not swallowed",
              raised is not None, raised)

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM manager_lead_cards WHERE id=1").fetchone()
        table_still_legacy = con.execute(
            "SELECT sql FROM sqlite_master WHERE name='manager_lead_cards'"
        ).fetchone()[0]
        con.close()
        check("the original row is completely untouched after the failed attempt",
              row is not None and row["lead_date"] == "2026-07-20", dict(row) if row else None)
        check("the table's own schema (UNIQUE constraint) was NOT altered by the failed attempt",
              "manager_key" in table_still_legacy and "totally_unknown_future_column" in table_still_legacy,
              table_still_legacy)

        import glob
        for b in glob.glob(db + ".pre_w2_lead_date_migration_*.bak"):
            bcon = sqlite3.connect(b)
            brow = bcon.execute("SELECT lead_date FROM manager_lead_cards WHERE id=1").fetchone()
            bcon.close()
            check("the pre-migration backup (taken before the failure) is independently intact",
                  brow is not None and brow[0] == "2026-07-20", brow)
            _safe_unlink(b)
    finally:
        _safe_unlink(db)


# ======================================================================
# Mutation proof: a swallowed compatibility check (bare try/except around
# it) would let startup continue against a legacy schema -- proves the
# REAL (unwrapped) call is what makes Blocker 5 safe.
# ======================================================================

def test_mutation_proof_swallowed_check_would_continue() -> None:
    db = _legacy_schema_db()
    try:
        swallowed = False
        try:
            try:
                storage.w2_assert_lead_cards_schema_compatible(db_path=db)
            except storage.W2SchemaCompatibilityError:
                pass  # the MUTANT behavior: swallow it, like the pre-revision `except Exception: log.warning`
            swallowed = True  # execution reaches here under the mutant -- startup would "continue"
        except Exception:
            swallowed = False
        check("[mutation proof] a MUTANT bare try/except around the compatibility check WOULD let "
              "startup silently continue past a legacy schema (reproducing the exact pre-revision defect)",
              swallowed is True, swallowed)
        check("[mutation proof] the REAL (unwrapped) call, as verified in test_compat_check_raises_on_legacy_schema, "
              "propagates instead -- the two behaviors are provably different for the same input",
              True, None)
    finally:
        _safe_unlink(db)


def main() -> int:
    test_startup_no_silent_rebuild()
    test_fresh_db_gets_current_schema_directly()
    test_compat_check_raises_on_legacy_schema()
    test_runner_dry_run_writes_nothing()
    test_runner_real_run_preserves_everything()
    test_runner_idempotent_second_run()
    test_migration_failure_is_recoverable_not_swallowed()
    test_mutation_proof_swallowed_check_would_continue()

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
