# -*- coding: utf-8 -*-
"""tools/manager_replacement_storage_selftest.py -- offline self-test for the
Stage 1 manager_replacements / replacement_notify_ack schema and state-machine
helpers in storage.py (2026-07-15 architecture audit, corrected per the
independent Stage 1 review 2026-07-15b: B1 state ordering + R1-R6 gaps).

Scope under test (storage.py only):
* ensure_replacement_tables -- idempotent schema creation + additive column
  migration for an earlier-revision local table (_repl_ensure_stage1_columns).
* replacement_create -- idempotent on operation_id; "one active replacement
  per old_manager_key" (ux_repl_active_old).
* replacement_get / replacement_get_by_id / replacement_get_by_new_key /
  replacement_get_active_for_old_key -- read helpers.
* replacement_advance -- CAS forward transitions along the CORRECTED chain
  draft -> auth_phone -> auth_code -> {auth_pass ->} identity_ok ->
  ready_commit -> committing -> links_pending -> links_ready -> cutover_done
  -> notified; to_status='done' is always rejected (must use
  replacement_finalize); stage-skipping and stale CAS are rejected/False.
* replacement_update_links -- required_links/ready_links tracking, preserve-
  first links_ready_at, clamping, dead-operation rejection.
* replacement_finalize -- guarded notified -> done, all completion
  prerequisites enforced atomically, idempotent retry.
* replacement_cancel -- pre-commit only.
* replacement_fail -- any non-terminal status, bounded error fields.
* replacement_list_for_source / replacement_list_notify_eligible_for_source
  -- eligibility now begins at cutover_done, never links_ready.
* replacement_ack_insert -- source- and status-validated, single guarded
  INSERT...SELECT (never read-then-write); replacement_ack_exists;
  replacement_ack_unacked_ids; replacement_ack_prune (7-day-style pruning,
  caller-supplied cutoff).

Technique: every storage.py helper in this module accepts an explicit
db_path=... parameter -- these tests pass a fresh temp SQLite path directly
to each call, no monkeypatching required. Real execution via direct
`import storage`.

Pure/offline: no network, no Telegram, no production DB/runtime/session/logs,
no real proxy/API calls.

    python tools\\manager_replacement_storage_selftest.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage as st

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _selftest_db_guard(db_path: str, base_dir, storage_mod=None, *, temp_root: str = "") -> None:
    """Fail-fast production-path guard (2026-07-16, matches the identical
    guard already deployed in the sibling replacement selftests --
    manager_replacement_backend_selftest.py etc.): refuses any db_path
    located under base_dir/db (the project's own db/ directory), using
    os.path.commonpath (Windows-safe path-component comparison) rather than
    a naive string startswith -- a sibling directory like .../dbfoo is
    never a false match. storage_mod is optional: this file passes an
    explicit db_path=... to every storage.py call rather than mutating
    storage.DB_PATH/QUEUE_DB_PATH globally, so most callers omit it; when a
    storage module IS passed, both DB_PATH and (if the module defines it)
    QUEUE_DB_PATH must already equal db_path. temp_root, when given, also
    requires db_path to resolve inside it. Raises AssertionError on any
    violation -- callers must call this BEFORE any schema/fixture write."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    try:
        common = os.path.commonpath([prod_db_dir, target])
    except ValueError:
        common = ""  # different drives on Windows -> definitely not nested
    assert common != prod_db_dir, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    if storage_mod is not None:
        assert getattr(storage_mod, "DB_PATH", None) == db_path, \
            (getattr(storage_mod, "DB_PATH", None), db_path)
        queue_path = getattr(storage_mod, "QUEUE_DB_PATH", None)
        if queue_path is not None:
            assert queue_path == db_path, (queue_path, db_path)
    if temp_root:
        root = os.path.abspath(str(temp_root))
        try:
            root_common = os.path.commonpath([root, target])
        except ValueError:
            root_common = ""
        assert root_common == root, f"selftest db path must stay inside its temp root {root}: {db_path}"


def _tmp_db() -> str:
    path = tempfile.mktemp(suffix="_manager_replacement_storage_selftest.db")
    _selftest_db_guard(path, BASE_DIR, temp_root=tempfile.gettempdir())
    return path


def _opid(tag: str = "op") -> str:
    return f"{tag}-{uuid.uuid4().hex[:12]}"


def _row_count(db_path: str, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()


def _advance_chain(db, op, steps):
    """steps: list of (from, to, stage, fields) -- applies each in order,
    asserting every one succeeds. Small shared helper to keep test bodies
    focused on what's actually being verified."""
    for frm, to, stage, fields in steps:
        ok = st.replacement_advance(op, frm, to, stage=stage, fields=fields, db_path=db)
        check(f"setup: {op[:12]} {frm}->{to}", ok is True, repr((frm, to, ok)))


def _make_ready_operation(db, tag, old_key, *, ready=15, required=15):
    """Creates an operation and drives it to 'notified' with full link
    readiness and identity fields set -- the exact precondition
    replacement_finalize requires. Returns the operation_id."""
    op = _opid(tag)
    st.replacement_create(op, old_key, source_key="src1", db_path=db)
    _advance_chain(db, op, [
        ("draft", "auth_phone", "p", None),
        ("auth_phone", "auth_code", "c", None),
        ("auth_code", "identity_ok", "id", None),
        ("identity_ok", "ready_commit", "ready",
         {"new_manager_key": f"{tag}_new", "new_display_name": "Тест", "new_username": f"{tag}_new_u"}),
        ("ready_commit", "committing", "commit", None),
        ("committing", "links_pending", "wait", None),
    ])
    st.replacement_update_links(op, ready, required_links=required, db_path=db)
    if ready >= required:
        _advance_chain(db, op, [
            ("links_pending", "links_ready", "ok", None),
            ("links_ready", "cutover_done", "cutover", None),
            ("cutover_done", "notified", "notify", None),
        ])
    return op


# ======================================================================
# 1. Schema idempotency + exact column set
# ======================================================================

def test_schema_idempotent():
    print("\n-- 1: ensure_replacement_tables idempotent, exact column set --")
    db = _tmp_db()
    try:
        st.ensure_replacement_tables(db_path=db)
        st.ensure_replacement_tables(db_path=db)  # second call must not raise
        con = sqlite3.connect(db)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            repl_cols = {r[1] for r in con.execute("PRAGMA table_info(manager_replacements)").fetchall()}
            ack_cols = {r[1] for r in con.execute("PRAGMA table_info(replacement_notify_ack)").fetchall()}
        finally:
            con.close()
        check("1a. manager_replacements table created", "manager_replacements" in names, repr(names))
        check("1b. replacement_notify_ack table created", "replacement_notify_ack" in names, repr(names))

        expected_repl_cols = {
            "id", "operation_id", "status", "stage", "source_key", "old_manager_key",
            "new_manager_key", "old_display_name", "old_username", "new_display_name",
            "new_username", "new_tg_user_id", "proxy_mode", "proxy_ref", "proxy_confirmed",
            "target_date", "required_links", "ready_links",
            "links_ready_at", "completed_at", "created_by_user_id", "error_stage",
            "error_text", "created_at", "updated_at",
            # TPILOT C/F 20260718: additive rollback/deadline tracking columns
            # (automatic-rollback + commit-deadline sweep). Non-secret,
            # free-text diagnostic/timestamp fields -- same category as the
            # pre-existing error_stage/error_text above.
            "rollback_stage", "rollback_at", "commit_deadline_at",
        }
        check("1c. manager_replacements has exactly the required columns (incl. Stage 2 fix R2/R3, TPILOT C/F 20260718)",
              repl_cols == expected_repl_cols, repr(repl_cols ^ expected_repl_cols))

        expected_ack_cols = {"user_id", "replacement_id", "source_key", "acknowledged_at", "created_at"}
        check("1d. replacement_notify_ack has exactly the required columns",
              ack_cols == expected_ack_cols, repr(ack_cols ^ expected_ack_cols))

        # proxy_mode/proxy_ref/proxy_confirmed (Stage 2 fix R2) are
        # legitimate NON-SECRET route-tracking columns, never credentials --
        # explicitly allowed here. A genuine credential-shaped column
        # (phone, code, password, proxy_host/port/username/password,
        # session path, 2FA, auth key, callback) is still forbidden.
        allowed_cols = {"proxy_mode", "proxy_ref", "proxy_confirmed"}
        forbidden_substrings = ("phone", "code", "password", "2fa", "proxy", "session", "auth_key", "callback")
        all_cols_lower = {c.lower() for c in (repl_cols | ack_cols) if c not in allowed_cols}
        leaked = [c for c in all_cols_lower for f in forbidden_substrings if f in c]
        check("1e. no column name suggests phone/code/2FA/proxy-credential/session/auth-key/callback storage",
              not leaked, repr(leaked))

        db2 = _tmp_db()
        row = st.replacement_get(_opid(), db_path=db2)
        check("1f. lazy schema init via replacement_get on a brand-new db (no crash, returns None)",
              row is None, repr(row))
        Path(db2).unlink(missing_ok=True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 2. Schema upgrade for an earlier-revision local Stage 1 table
# ======================================================================

def test_schema_migration_existing_table():
    print("\n-- 2: additive column migration for a pre-existing old-shape table --")
    db = _tmp_db()
    try:
        # Manually build the OLD (pre-fix) Stage 1 shape: no required_links/
        # ready_links, ack table with acked_at (no source_key/created_at).
        con = sqlite3.connect(db)
        con.execute("""
            CREATE TABLE manager_replacements(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                stage TEXT NOT NULL DEFAULT '',
                source_key TEXT NOT NULL DEFAULT '',
                old_manager_key TEXT NOT NULL,
                new_manager_key TEXT NOT NULL DEFAULT '',
                old_display_name TEXT NOT NULL DEFAULT '',
                old_username TEXT NOT NULL DEFAULT '',
                new_display_name TEXT NOT NULL DEFAULT '',
                new_username TEXT NOT NULL DEFAULT '',
                target_date TEXT NOT NULL DEFAULT '',
                links_ready_at TEXT DEFAULT '',
                completed_at TEXT DEFAULT '',
                created_by_user_id INTEGER,
                error_stage TEXT DEFAULT '',
                error_text TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            )
        """)
        con.execute(
            "INSERT INTO manager_replacements(operation_id, old_manager_key, created_at, updated_at)"
            " VALUES('preexisting-op','oldkeypre','2026-07-01T00:00:00','2026-07-01T00:00:00')"
        )
        con.execute("""
            CREATE TABLE replacement_notify_ack(
                user_id INTEGER NOT NULL,
                replacement_id INTEGER NOT NULL,
                acked_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, replacement_id)
            )
        """)
        con.execute("INSERT INTO replacement_notify_ack(user_id, replacement_id, acked_at) VALUES(1, 999, '2026-07-01T00:00:00')")
        con.commit()
        con.close()

        # Now run the CURRENT ensure_replacement_tables against this old-shape db.
        st.ensure_replacement_tables(db_path=db)

        con2 = sqlite3.connect(db)
        con2.row_factory = sqlite3.Row
        try:
            repl_cols = {r[1] for r in con2.execute("PRAGMA table_info(manager_replacements)").fetchall()}
            ack_cols = {r[1] for r in con2.execute("PRAGMA table_info(replacement_notify_ack)").fetchall()}
            pre_row = con2.execute("SELECT * FROM manager_replacements WHERE operation_id='preexisting-op'").fetchone()
            ack_row = con2.execute("SELECT * FROM replacement_notify_ack WHERE user_id=1 AND replacement_id=999").fetchone()
        finally:
            con2.close()

        check("2a. required_links column added", "required_links" in repl_cols, repr(repl_cols))
        check("2b. ready_links column added", "ready_links" in repl_cols, repr(repl_cols))
        check("2c. pre-existing row got default required_links=15", pre_row["required_links"] == 15, repr(dict(pre_row)))
        check("2d. pre-existing row got default ready_links=0", pre_row["ready_links"] == 0, repr(dict(pre_row)))
        check("2e. pre-existing row's old_manager_key untouched", pre_row["old_manager_key"] == "oldkeypre", repr(dict(pre_row)))

        # Stage 2 fix R2/R3 additive migration onto the SAME old-shape table.
        check("2e2. new_tg_user_id column added", "new_tg_user_id" in repl_cols, repr(repl_cols))
        check("2e3. proxy_mode column added", "proxy_mode" in repl_cols, repr(repl_cols))
        check("2e4. proxy_ref column added", "proxy_ref" in repl_cols, repr(repl_cols))
        check("2e5. proxy_confirmed column added", "proxy_confirmed" in repl_cols, repr(repl_cols))
        check("2e6. pre-existing row got default new_tg_user_id=NULL", pre_row["new_tg_user_id"] is None, repr(dict(pre_row)))
        check("2e7. pre-existing row got default proxy_mode=''", pre_row["proxy_mode"] == "", repr(dict(pre_row)))
        check("2e8. pre-existing row got default proxy_ref=''", pre_row["proxy_ref"] == "", repr(dict(pre_row)))
        check("2e9. pre-existing row got default proxy_confirmed=0", pre_row["proxy_confirmed"] == 0, repr(dict(pre_row)))

        check("2f. ack table source_key column added", "source_key" in ack_cols, repr(ack_cols))
        check("2g. ack table created_at column added", "created_at" in ack_cols, repr(ack_cols))
        check("2h. ack table acked_at renamed to acknowledged_at", "acknowledged_at" in ack_cols and "acked_at" not in ack_cols, repr(ack_cols))
        check("2i. pre-existing ack row survived the rename with its timestamp intact",
              ack_row is not None and ack_row["acknowledged_at"] == "2026-07-01T00:00:00", repr(dict(ack_row) if ack_row else None))

        # Repeated migration call is a safe no-op.
        st.ensure_replacement_tables(db_path=db)
        check("2j. repeated ensure_replacement_tables on the migrated db is still a no-op crash-wise", True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 3. replacement_create: idempotency, one-active-per-primary
# ======================================================================

def test_create_idempotent_and_one_active_per_primary():
    print("\n-- 3: replacement_create idempotency + one-active-per-old-key --")
    db = _tmp_db()
    try:
        op1 = _opid("create1")
        row1 = st.replacement_create(op1, "primary1", source_key="src1",
                                      old_display_name="Иван", old_username="ivan_x",
                                      created_by_user_id=42, db_path=db)
        check("3a. first create returns a row", row1 is not None, repr(row1))
        check("3b. status defaults to draft", row1 and row1.get("status") == "draft", repr(row1))
        check("3c. required_links defaults to 15", row1 and row1.get("required_links") == 15, repr(row1))
        check("3d. ready_links defaults to 0", row1 and row1.get("ready_links") == 0, repr(row1))
        check("3e. old_display_name/old_username snapshotted", row1 and row1.get("old_display_name") == "Иван"
              and row1.get("old_username") == "ivan_x", repr(row1))

        row1_retry = st.replacement_create(op1, "primary1", source_key="src1",
                                            old_display_name="Иван", old_username="ivan_x",
                                            created_by_user_id=42, db_path=db)
        check("3f. same operation_id returns the existing row, not a new one",
              row1_retry and row1_retry.get("id") == row1.get("id"), repr((row1, row1_retry)))
        check("3g. exactly one row exists after the idempotent retry",
              _row_count(db, "manager_replacements") == 1, _row_count(db, "manager_replacements"))

        op2 = _opid("create2")
        row2 = st.replacement_create(op2, "primary1", source_key="src1", db_path=db)
        check("3h. a second, different operation_id for the same old_manager_key returns None",
              row2 is None, repr(row2))
        check("3i. table still has exactly one row", _row_count(db, "manager_replacements") == 1)

        active = st.replacement_get_active_for_old_key("primary1", db_path=db)
        check("3j. get_active_for_old_key returns the active (draft) row",
              active and active.get("operation_id") == op1, repr(active))

        ok_cancel = st.replacement_cancel(op1, db_path=db)
        check("3k. op1 cancels successfully from draft", ok_cancel is True)
        op3 = _opid("create3")
        row3 = st.replacement_create(op3, "primary1", source_key="src1", db_path=db)
        check("3l. after op1 is terminal (cancelled), a new operation for the same primary succeeds",
              row3 is not None, repr(row3))

        try:
            st.replacement_create("", "primary1", db_path=db)
            check("3m. empty operation_id raises ValueError", False, "no exception raised")
        except ValueError:
            check("3m. empty operation_id raises ValueError", True)
        try:
            st.replacement_create(_opid(), "", db_path=db)
            check("3n. empty old_manager_key raises ValueError", False, "no exception raised")
        except ValueError:
            check("3n. empty old_manager_key raises ValueError", True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 4. Read helpers: get / get_by_id / get_by_new_key / get_active_for_old_key
# ======================================================================

def test_read_helpers():
    print("\n-- 4: replacement_get / get_by_id / get_by_new_key / get_active_for_old_key --")
    db = _tmp_db()
    try:
        check("4a. get on unknown operation_id returns None", st.replacement_get(_opid("nope"), db_path=db) is None)
        check("4b. get_active_for_old_key on unknown key returns None",
              st.replacement_get_active_for_old_key("nobody", db_path=db) is None)
        check("4c. get_by_id(0) returns None", st.replacement_get_by_id(0, db_path=db) is None)
        check("4d. get_by_id(-1) returns None", st.replacement_get_by_id(-1, db_path=db) is None)
        check("4e. get_by_new_key on unknown key returns None",
              st.replacement_get_by_new_key("ghostkey", db_path=db) is None)

        op = _opid("rd1")
        row = st.replacement_create(op, "primaryRD", source_key="src1", db_path=db)
        rid = row["id"]

        by_id = st.replacement_get_by_id(rid, db_path=db)
        check("4f. get_by_id hit returns the correct row", by_id and by_id["operation_id"] == op, repr(by_id))

        st.replacement_advance(op, "draft", "auth_phone", db_path=db)
        st.replacement_advance(op, "auth_phone", "auth_code", db_path=db)
        st.replacement_advance(op, "auth_code", "identity_ok", db_path=db)
        st.replacement_advance(op, "identity_ok", "ready_commit", fields={"new_manager_key": "rdnewkey"}, db_path=db)

        by_key = st.replacement_get_by_new_key("rdnewkey", db_path=db)
        check("4g. get_by_new_key hit returns the correct row", by_key and by_key["operation_id"] == op, repr(by_key))

        check("4h. get_by_new_key is an EXACT match -- a prefix does not match",
              st.replacement_get_by_new_key("rdnew", db_path=db) is None)

        # A DONE row's new_manager_key must remain retrievable forever.
        op_done = _make_ready_operation(db, "rdready", "primaryRDdone")
        st.replacement_finalize(op_done, db_path=db)
        by_key_done = st.replacement_get_by_new_key("rdready_new", db_path=db)
        check("4i. a historical DONE row is retrievable by new_manager_key",
              by_key_done and by_key_done["status"] == "done", repr(by_key_done))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 5. B1: corrected state ordering
# ======================================================================

def test_state_ordering_b1():
    print("\n-- 5: B1 -- corrected state ordering (cutover before notify, notify before done) --")
    db = _tmp_db()
    try:
        op = _opid("ord1")
        st.replacement_create(op, "primaryOrd1", source_key="src1", db_path=db)
        _advance_chain(db, op, [
            ("draft", "auth_phone", "p", None),
            ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready",
             {"new_manager_key": "ord1_new", "new_display_name": "О1", "new_username": "ord1_u"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op, 15, db_path=db)
        st.replacement_advance(op, "links_pending", "links_ready", db_path=db)

        try:
            st.replacement_advance(op, "links_ready", "notified", db_path=db)
            check("5a. links_ready -> notified is REJECTED (old wrong ordering)", False, "no exception")
        except ValueError:
            check("5a. links_ready -> notified is REJECTED (old wrong ordering)", True)
        row = st.replacement_get(op, db_path=db)
        check("5a2. status unchanged (still links_ready) after the rejected jump", row["status"] == "links_ready", repr(row))

        ok1 = st.replacement_advance(op, "links_ready", "cutover_done", db_path=db)
        check("5b. links_ready -> cutover_done is ACCEPTED", ok1 is True)

        ok2 = st.replacement_advance(op, "cutover_done", "notified", db_path=db)
        check("5c. cutover_done -> notified is ACCEPTED", ok2 is True)

        try:
            st.replacement_advance(op, "notified", "done", db_path=db)
            check("5d. notified -> done via generic advance() is REJECTED", False, "no exception")
        except ValueError:
            check("5d. notified -> done via generic advance() is REJECTED", True)
        row2 = st.replacement_get(op, db_path=db)
        check("5d2. status unchanged (still notified), never silently finalized", row2["status"] == "notified", repr(row2))

        ok3 = st.replacement_finalize(op, db_path=db)
        check("5e. notified -> done via the guarded replacement_finalize() succeeds", ok3 is True)

        # No direct links_ready -> done, on a fresh operation.
        op2 = _opid("ord2")
        st.replacement_create(op2, "primaryOrd2", source_key="src1", db_path=db)
        _advance_chain(db, op2, [
            ("draft", "auth_phone", "p", None),
            ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "ord2_new"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op2, 15, db_path=db)
        st.replacement_advance(op2, "links_pending", "links_ready", db_path=db)
        try:
            st.replacement_advance(op2, "links_ready", "done", db_path=db)
            check("5f. no direct links_ready -> done via advance()", False, "no exception")
        except ValueError:
            check("5f. no direct links_ready -> done via advance()", True)

        # No-2FA route still works.
        op3 = _opid("ord3")
        st.replacement_create(op3, "primaryOrd3", source_key="src1", db_path=db)
        st.replacement_advance(op3, "draft", "auth_phone", db_path=db)
        st.replacement_advance(op3, "auth_phone", "auth_code", db_path=db)
        ok4 = st.replacement_advance(op3, "auth_code", "identity_ok", db_path=db)
        check("5g. no-2FA route auth_code -> identity_ok still works", ok4 is True)

        # WITH 2FA route still works.
        op4 = _opid("ord4")
        st.replacement_create(op4, "primaryOrd4", source_key="src1", db_path=db)
        st.replacement_advance(op4, "draft", "auth_phone", db_path=db)
        st.replacement_advance(op4, "auth_phone", "auth_code", db_path=db)
        ok5 = st.replacement_advance(op4, "auth_code", "auth_pass", db_path=db)
        ok6 = st.replacement_advance(op4, "auth_pass", "identity_ok", db_path=db)
        check("5h. 2FA route auth_code -> auth_pass -> identity_ok still works", ok5 is True and ok6 is True)

        # Stage skipping remains rejected.
        op5 = _opid("ord5")
        st.replacement_create(op5, "primaryOrd5", source_key="src1", db_path=db)
        try:
            st.replacement_advance(op5, "draft", "committing", db_path=db)
            check("5i. stage-skip (draft -> committing) remains rejected", False, "no exception")
        except ValueError:
            check("5i. stage-skip (draft -> committing) remains rejected", True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 6. replacement_advance: stale CAS races, forbidden fields, immutability
# ======================================================================

def test_advance_races_and_immutability():
    print("\n-- 6: stale CAS races, forbidden/immutable fields --")
    db = _tmp_db()
    try:
        op = _opid("bad")
        st.replacement_create(op, "primary4", source_key="src1", old_display_name="Orig",
                               old_username="orig_u", created_by_user_id=7, db_path=db)

        ok1 = st.replacement_advance(op, "draft", "auth_phone", db_path=db)
        ok2 = st.replacement_advance(op, "draft", "auth_phone", db_path=db)  # stale
        check("6a. first draft->auth_phone succeeds", ok1 is True)
        check("6b. second (stale) draft->auth_phone returns False, not an exception", ok2 is False)

        ok3 = st.replacement_advance(_opid("ghost"), "auth_phone", "auth_code", db_path=db)
        check("6c. advance on an unknown operation_id returns False", ok3 is False)

        for forbidden_field in ("old_manager_key", "old_display_name", "old_username",
                                 "operation_id", "created_by_user_id", "id",
                                 "links_ready_at", "completed_at", "required_links", "ready_links"):
            try:
                st.replacement_advance(op, "auth_phone", "auth_code",
                                        fields={forbidden_field: "hacked"}, db_path=db)
                check(f"6d. forbidden/immutable field {forbidden_field!r} raises ValueError", False, "no exception")
            except ValueError:
                check(f"6d. forbidden/immutable field {forbidden_field!r} raises ValueError", True)

        row = st.replacement_get(op, db_path=db)
        check("6e. row unaffected by any of the rejected fields dicts (still auth_phone, identity intact)",
              row["status"] == "auth_phone" and row["old_manager_key"] == "primary4"
              and row["old_display_name"] == "Orig" and row["old_username"] == "orig_u"
              and row["created_by_user_id"] == 7, repr(row))

        try:
            st.replacement_advance("", "draft", "auth_phone", db_path=db)
            check("6f. empty operation_id raises ValueError", False, "no exception")
        except ValueError:
            check("6f. empty operation_id raises ValueError", True)

        try:
            st.replacement_advance(op, "auth_phone", "done", db_path=db)
            check("6g. to_status='done' via generic advance always raises ValueError", False, "no exception")
        except ValueError:
            check("6g. to_status='done' via generic advance always raises ValueError", True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 7. new_manager_key uniqueness (ux_repl_new_key)
# ======================================================================

def test_new_manager_key_uniqueness():
    print("\n-- 7: new_manager_key uniqueness across active operations --")
    db = _tmp_db()
    try:
        opA = _opid("keyA")
        opB = _opid("keyB")
        st.replacement_create(opA, "primaryA", source_key="src1", db_path=db)
        st.replacement_create(opB, "primaryB", source_key="src1", db_path=db)
        for op in (opA, opB):
            st.replacement_advance(op, "draft", "auth_phone", db_path=db)
            st.replacement_advance(op, "auth_phone", "auth_code", db_path=db)
            st.replacement_advance(op, "auth_code", "identity_ok", db_path=db)

        ok_a = st.replacement_advance(opA, "identity_ok", "ready_commit",
                                       fields={"new_manager_key": "sharedkey"}, db_path=db)
        check("7a. first operation claims new_manager_key='sharedkey'", ok_a is True)

        try:
            st.replacement_advance(opB, "identity_ok", "ready_commit",
                                    fields={"new_manager_key": "sharedkey"}, db_path=db)
            check("7b. a second active operation claiming the SAME new_manager_key raises ReplacementConflict",
                  False, "no exception raised")
        except st.ReplacementConflict:
            check("7b. a second active operation claiming the SAME new_manager_key raises ReplacementConflict", True)

        row_b = st.replacement_get(opB, db_path=db)
        check("7c. the blocked operation's own status is unchanged (still identity_ok)",
              row_b.get("status") == "identity_ok", repr(row_b))

        st.replacement_fail(opA, "commit", "simulated failure", db_path=db)
        ok_b_retry = st.replacement_advance(opB, "identity_ok", "ready_commit",
                                             fields={"new_manager_key": "sharedkey"}, db_path=db)
        check("7d. after opA fails, opB can claim the now-freed new_manager_key", ok_b_retry is True, repr(ok_b_retry))

        opC = _make_ready_operation(db, "keyC", "primaryC")
        okc = st.replacement_finalize(opC, db_path=db)
        check("7e. opC reaches done", okc is True)

        opD = _opid("keyD")
        st.replacement_create(opD, "primaryD", source_key="src1", db_path=db)
        for frm, to in [("draft", "auth_phone"), ("auth_phone", "auth_code"), ("auth_code", "identity_ok")]:
            st.replacement_advance(opD, frm, to, db_path=db)
        try:
            st.replacement_advance(opD, "identity_ok", "ready_commit",
                                    fields={"new_manager_key": "keyC_new"}, db_path=db)
            check("7f. a DONE operation's new_manager_key can never be reclaimed by another operation",
                  False, "no exception raised -- reused a live manager's key")
        except st.ReplacementConflict:
            check("7f. a DONE operation's new_manager_key can never be reclaimed by another operation", True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 7b. Stage 2 fix R2/R3: new_tg_user_id uniqueness (ux_repl_new_tgid) and
#     the durable proxy_mode/proxy_ref/proxy_confirmed mutable fields
# ======================================================================

def test_new_tg_user_id_uniqueness():
    print("\n-- 7b: new_tg_user_id uniqueness across active operations (Stage 2 fix R3) --")
    db = _tmp_db()
    try:
        opA = _opid("tgidA")
        opB = _opid("tgidB")
        st.replacement_create(opA, "primaryTgidA", source_key="src1", db_path=db)
        st.replacement_create(opB, "primaryTgidB", source_key="src1", db_path=db)
        for op in (opA, opB):
            st.replacement_advance(op, "draft", "auth_phone", db_path=db)
            st.replacement_advance(op, "auth_phone", "auth_code", db_path=db)
            st.replacement_advance(op, "auth_code", "identity_ok", db_path=db)

        ok_a = st.replacement_advance(opA, "identity_ok", "ready_commit",
                                       fields={"new_manager_key": "tgidkeyA", "new_tg_user_id": 555111}, db_path=db)
        check("7b-a. first operation claims new_tg_user_id=555111", ok_a is True)

        try:
            st.replacement_advance(opB, "identity_ok", "ready_commit",
                                    fields={"new_manager_key": "tgidkeyB", "new_tg_user_id": 555111}, db_path=db)
            check("7b-b. a second active operation claiming the SAME new_tg_user_id raises ReplacementConflict",
                  False, "no exception raised")
        except st.ReplacementConflict as e:
            check("7b-b. a second active operation claiming the SAME new_tg_user_id raises ReplacementConflict", True)
            check("7b-b2. the conflict message names new_tg_user_id (not new_manager_key)",
                  "new_tg_user_id" in str(e), str(e))

        row_b = st.replacement_get(opB, db_path=db)
        check("7b-c. the blocked operation's own status is unchanged (still identity_ok)",
              row_b.get("status") == "identity_ok", repr(row_b))

        st.replacement_fail(opA, "commit", "simulated failure", db_path=db)
        ok_b_retry = st.replacement_advance(opB, "identity_ok", "ready_commit",
                                             fields={"new_manager_key": "tgidkeyB", "new_tg_user_id": 555111}, db_path=db)
        check("7b-d. after opA fails, opB can claim the now-freed new_tg_user_id", ok_b_retry is True, repr(ok_b_retry))

        opC = _opid("tgidC")
        st.replacement_create(opC, "primaryTgidC", source_key="src1", db_path=db)
        for frm, to in [("draft", "auth_phone"), ("auth_phone", "auth_code"), ("auth_code", "identity_ok")]:
            st.replacement_advance(opC, frm, to, db_path=db)
        try:
            st.replacement_advance(opC, "identity_ok", "ready_commit",
                                    fields={"new_manager_key": "tgidkeyC", "new_tg_user_id": 555111}, db_path=db)
            check("7b-e. opB (non-terminal) still blocks a THIRD operation from claiming the same new_tg_user_id",
                  False, "no exception raised")
        except st.ReplacementConflict:
            check("7b-e. opB (non-terminal) still blocks a THIRD operation from claiming the same new_tg_user_id", True)
    finally:
        Path(db).unlink(missing_ok=True)


def test_durable_proxy_mutable_fields():
    print("\n-- 7c: durable proxy_mode/proxy_ref/proxy_confirmed via replacement_advance (Stage 2 fix R2) --")
    db = _tmp_db()
    try:
        op = _opid("proxyfields")
        st.replacement_create(op, "primaryProxyFields", source_key="src1", db_path=db)
        ok = st.replacement_advance(
            op, "draft", "auth_phone",
            fields={"new_manager_key": "proxykey1", "new_display_name": "Имя",
                    "proxy_mode": "proxy", "proxy_ref": "pool", "proxy_confirmed": 1},
            db_path=db,
        )
        check("7c-a. draft->auth_phone can set proxy_mode/proxy_ref/proxy_confirmed in one CAS", ok is True)
        row = st.replacement_get(op, db_path=db)
        check("7c-b. proxy_mode persisted", row.get("proxy_mode") == "proxy", repr(row))
        check("7c-c. proxy_ref persisted", row.get("proxy_ref") == "pool", repr(row))
        check("7c-d. proxy_confirmed persisted as 1", row.get("proxy_confirmed") == 1, repr(row))

        try:
            st.replacement_advance(op, "auth_phone", "auth_code", fields={"bogus_field": "x"}, db_path=db)
            check("7c-e. an unknown/forbidden field name in fields raises ValueError", False, "no exception raised")
        except ValueError:
            check("7c-e. an unknown/forbidden field name in fields raises ValueError", True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 8. replacement_cancel: pre-commit only
# ======================================================================

def test_cancel_rules():
    print("\n-- 8: replacement_cancel -- allowed pre-commit, blocked from committing onward --")
    db = _tmp_db()
    try:
        op = _opid("cancel1")
        st.replacement_create(op, "primaryE", source_key="src1", db_path=db)
        ok = st.replacement_cancel(op, db_path=db)
        check("8a. cancel succeeds from draft", ok is True)
        row = st.replacement_get(op, db_path=db)
        check("8a2. status is cancelled", row.get("status") == "cancelled", repr(row))

        ok_again = st.replacement_cancel(op, db_path=db)
        check("8b. cancelling an already-cancelled operation returns False (no double-terminal)", ok_again is False)

        op2 = _opid("cancel2")
        st.replacement_create(op2, "primaryF", source_key="src1", db_path=db)
        for frm, to in [("draft", "auth_phone"), ("auth_phone", "auth_code"),
                        ("auth_code", "identity_ok"), ("identity_ok", "ready_commit")]:
            st.replacement_advance(op2, frm, to, db_path=db)
        ok2 = st.replacement_cancel(op2, db_path=db)
        check("8c. cancel succeeds from ready_commit (last pre-commit stage)", ok2 is True)

        op3 = _opid("cancel3")
        st.replacement_create(op3, "primaryG", source_key="src1", db_path=db)
        for frm, to in [("draft", "auth_phone"), ("auth_phone", "auth_code"),
                        ("auth_code", "identity_ok"), ("identity_ok", "ready_commit"),
                        ("ready_commit", "committing")]:
            st.replacement_advance(op3, frm, to, db_path=db)
        ok3 = st.replacement_cancel(op3, db_path=db)
        check("8d. cancel is BLOCKED once status is committing (durable, no user-cancel)", ok3 is False)
        row3 = st.replacement_get(op3, db_path=db)
        check("8d2. status remains committing, not silently cancelled", row3.get("status") == "committing", repr(row3))

        op4 = _make_ready_operation(db, "cancel4", "primaryG2")
        st.replacement_finalize(op4, db_path=db)
        ok4 = st.replacement_cancel(op4, db_path=db)
        check("8e. cancel is blocked on a DONE operation", ok4 is False)

        ok5 = st.replacement_cancel(_opid("ghost"), db_path=db)
        check("8f. cancel on unknown operation_id returns False", ok5 is False)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 9. replacement_fail: any non-terminal status, bounded error fields
# ======================================================================

def test_fail_rules():
    print("\n-- 9: replacement_fail -- any non-terminal status, bounded/normalized error fields --")
    db = _tmp_db()
    try:
        op = _opid("fail1")
        st.replacement_create(op, "primaryH", source_key="src1", db_path=db)
        ok = st.replacement_fail(op, "auth_phone", "PhoneNumberBannedError", db_path=db)
        check("9a. fail succeeds from draft", ok is True)
        row = st.replacement_get(op, db_path=db)
        check("9b. status is failed", row.get("status") == "failed", repr(row))
        check("9c. error_stage recorded", row.get("error_stage") == "auth_phone", repr(row))
        check("9d. error_text recorded", row.get("error_text") == "PhoneNumberBannedError", repr(row))

        ok2 = st.replacement_fail(op, "later_stage", "second failure", db_path=db)
        check("9e. failing an already-failed operation returns False (no overwrite)", ok2 is False)
        row2 = st.replacement_get(op, db_path=db)
        check("9f. error_stage/text from the FIRST failure are preserved, not overwritten",
              row2.get("error_stage") == "auth_phone" and row2.get("error_text") == "PhoneNumberBannedError", repr(row2))

        op2 = _opid("fail2")
        st.replacement_create(op2, "primaryI", source_key="src1", db_path=db)
        for frm, to in [("draft", "auth_phone"), ("auth_phone", "auth_code"),
                        ("auth_code", "identity_ok"), ("identity_ok", "ready_commit"),
                        ("ready_commit", "committing"), ("committing", "links_pending")]:
            st.replacement_advance(op2, frm, to, db_path=db)
        ok3 = st.replacement_fail(op2, "links_pending", "bizlink generation timeout", db_path=db)
        check("9g. fail succeeds from links_pending (deep, non-terminal)", ok3 is True)

        op3 = _make_ready_operation(db, "fail3", "primaryJ")
        st.replacement_finalize(op3, db_path=db)
        ok4 = st.replacement_fail(op3, "post_done", "should never apply", db_path=db)
        check("9h. fail on a DONE operation returns False", ok4 is False)

        # Bounded fields.
        op4 = _opid("fail4")
        st.replacement_create(op4, "primaryK", source_key="src1", db_path=db)
        long_stage = "x" * 200
        long_text = "y" * 2000
        st.replacement_fail(op4, "  " + long_stage + "  ", "  " + long_text + "  ", db_path=db)
        row4 = st.replacement_get(op4, db_path=db)
        check("9i. error_stage truncated to 64 chars", len(row4["error_stage"]) == 64, len(row4["error_stage"]))
        check("9j. error_text truncated to 500 chars", len(row4["error_text"]) == 500, len(row4["error_text"]))
        check("9k. leading/trailing whitespace trimmed before truncation",
              row4["error_stage"] == long_stage[:64] and row4["error_text"] == long_text[:500], repr(row4))

        op5 = _opid("fail5")
        st.replacement_create(op5, "primaryL", source_key="src1", db_path=db)
        st.replacement_fail(op5, "", "", db_path=db)
        row5 = st.replacement_get(op5, db_path=db)
        check("9l. empty error_stage normalizes to 'unknown'", row5["error_stage"] == "unknown", repr(row5))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 10. Link-progress tracking (R1)
# ======================================================================

def test_link_progress():
    print("\n-- 10: replacement_update_links -- readiness, clamping, preserve-first timestamp --")
    db = _tmp_db()
    try:
        op = _opid("lp1")
        st.replacement_create(op, "primaryLP1", source_key="src1", db_path=db)

        row0 = st.replacement_update_links(op, 0, db_path=db)
        check("10a. 0/15 is valid, no links_ready_at", row0["ready_links"] == 0 and not row0["links_ready_at"], repr(row0))

        row14 = st.replacement_update_links(op, 14, db_path=db)
        check("10b. 14/15 does not set links_ready_at", row14["ready_links"] == 14 and not row14["links_ready_at"], repr(row14))

        row15 = st.replacement_update_links(op, 15, links_ready_at="2026-07-15T10:00:00", db_path=db)
        check("10c. 15/15 sets links_ready_at", row15["ready_links"] == 15 and row15["links_ready_at"] == "2026-07-15T10:00:00", repr(row15))

        row15b = st.replacement_update_links(op, 15, links_ready_at="2026-07-15T99:99:99-should-not-be-used", db_path=db)
        check("10d. repeated 15/15 preserves the ORIGINAL links_ready_at", row15b["links_ready_at"] == "2026-07-15T10:00:00", repr(row15b))

        row16 = st.replacement_update_links(op, 16, db_path=db)
        check("10e. 16/15 is clamped to 15", row16["ready_links"] == 15, repr(row16))

        row_regress = st.replacement_update_links(op, 14, db_path=db)
        check("10f. regression 15->14 preserves the ORIGINAL links_ready_at (never erased)",
              row_regress["ready_links"] == 14 and row_regress["links_ready_at"] == "2026-07-15T10:00:00", repr(row_regress))

        # required_links=0 is rejected when explicitly supplied.
        op2 = _opid("lp2")
        st.replacement_create(op2, "primaryLP2", source_key="src1", db_path=db)
        try:
            st.replacement_update_links(op2, 5, required_links=0, db_path=db)
            check("10g. required_links=0 (explicit) raises ValueError", False, "no exception")
        except ValueError:
            check("10g. required_links=0 (explicit) raises ValueError", True)

        try:
            st.replacement_update_links(op2, -1, db_path=db)
            check("10h. negative ready_links raises ValueError", False, "no exception")
        except ValueError:
            check("10h. negative ready_links raises ValueError", True)

        try:
            st.replacement_update_links(op2, "15", db_path=db)
            check("10i. non-integer ready_links (str) raises ValueError", False, "no exception")
        except ValueError:
            check("10i. non-integer ready_links (str) raises ValueError", True)

        try:
            st.replacement_update_links(op2, True, db_path=db)
            check("10j. bool ready_links raises ValueError (bool is not a real int here)", False, "no exception")
        except ValueError:
            check("10j. bool ready_links raises ValueError (bool is not a real int here)", True)

        # required_links can be customized (not always 15).
        op3 = _opid("lp3")
        st.replacement_create(op3, "primaryLP3", source_key="src1", db_path=db)
        row3 = st.replacement_update_links(op3, 3, required_links=3, db_path=db)
        check("10k. custom required_links=3, 3/3 sets links_ready_at", row3["required_links"] == 3
              and row3["ready_links"] == 3 and row3["links_ready_at"], repr(row3))

        # Failed/cancelled operations reject link updates.
        op4 = _opid("lp4")
        st.replacement_create(op4, "primaryLP4", source_key="src1", db_path=db)
        st.replacement_fail(op4, "auth_phone", "banned", db_path=db)
        result = st.replacement_update_links(op4, 15, db_path=db)
        check("10l. link update on a FAILED operation returns None (rejected)", result is None, repr(result))

        op5 = _opid("lp5")
        st.replacement_create(op5, "primaryLP5", source_key="src1", db_path=db)
        st.replacement_cancel(op5, db_path=db)
        result2 = st.replacement_update_links(op5, 15, db_path=db)
        check("10m. link update on a CANCELLED operation returns None (rejected)", result2 is None, repr(result2))

        result3 = st.replacement_update_links(_opid("ghost"), 15, db_path=db)
        check("10n. link update on an unknown operation_id returns None", result3 is None, repr(result3))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 11. Finalization guards (R4)
# ======================================================================

def test_finalize_guards():
    print("\n-- 11: replacement_finalize -- completion prerequisites, idempotent retry --")
    db = _tmp_db()
    try:
        # Missing new_manager_key.
        op1 = _opid("fin1")
        st.replacement_create(op1, "primaryF1", source_key="src1", db_path=db)
        _advance_chain(db, op1, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_display_name": "НетКлюча"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op1, 15, db_path=db)
        _advance_chain(db, op1, [
            ("links_pending", "links_ready", "ok", None),
            ("links_ready", "cutover_done", "cutover", None),
            ("cutover_done", "notified", "notify", None),
        ])
        ok1 = st.replacement_finalize(op1, db_path=db)
        check("11a. finalize rejects an operation with EMPTY new_manager_key", ok1 is False)
        check("11a2. status remains notified, not silently done",
              st.replacement_get(op1, db_path=db)["status"] == "notified")

        # Missing new_display_name.
        op2 = _opid("fin2")
        st.replacement_create(op2, "primaryF2", source_key="src1", db_path=db)
        _advance_chain(db, op2, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "fin2_new"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op2, 15, db_path=db)
        _advance_chain(db, op2, [
            ("links_pending", "links_ready", "ok", None),
            ("links_ready", "cutover_done", "cutover", None),
            ("cutover_done", "notified", "notify", None),
        ])
        ok2 = st.replacement_finalize(op2, db_path=db)
        check("11b. finalize rejects an operation with EMPTY new_display_name", ok2 is False)

        # 14/15 links.
        op3 = _opid("fin3")
        st.replacement_create(op3, "primaryF3", source_key="src1", db_path=db)
        _advance_chain(db, op3, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "fin3_new", "new_display_name": "Ф3"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op3, 14, db_path=db)
        # links_pending -> links_ready itself requires no readiness check at
        # the advance level (that's a caller/business decision elsewhere) --
        # but finalize's OWN guard must still catch 14/15 regardless of how
        # far the status advanced.
        ok3a = st.replacement_advance(op3, "links_pending", "links_ready", db_path=db)
        check("11c-setup. links_pending -> links_ready transition itself succeeds", ok3a is True)
        st.replacement_advance(op3, "links_ready", "cutover_done", db_path=db)
        st.replacement_advance(op3, "cutover_done", "notified", db_path=db)
        ok3 = st.replacement_finalize(op3, db_path=db)
        check("11c. finalize rejects 14/15 links (ready_links < required_links)", ok3 is False)

        # Wrong current status (e.g. still links_pending).
        op4 = _opid("fin4")
        st.replacement_create(op4, "primaryF4", source_key="src1", db_path=db)
        _advance_chain(db, op4, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "fin4_new", "new_display_name": "Ф4"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op4, 15, db_path=db)
        ok4 = st.replacement_finalize(op4, db_path=db)
        check("11d. finalize rejects a wrong current status (still links_pending, not notified)", ok4 is False)

        # Successful finalize + idempotent retry + first completed_at preserved.
        op5 = _make_ready_operation(db, "fin5", "primaryF5")
        ok5 = st.replacement_finalize(op5, db_path=db)
        check("11e. finalize succeeds when all prerequisites hold", ok5 is True)
        row5 = st.replacement_get(op5, db_path=db)
        first_completed_at = row5["completed_at"]
        check("11f. completed_at is set on first successful finalize", bool(first_completed_at), repr(row5))
        check("11g. status is done", row5["status"] == "done", repr(row5))

        ok5_retry = st.replacement_finalize(op5, completed_at="2099-01-01T00:00:00-should-not-be-used", db_path=db)
        check("11h. repeated finalize call after done is deterministic and returns True (idempotent)", ok5_retry is True)
        row5_retry = st.replacement_get(op5, db_path=db)
        check("11i. completed_at is NEVER overwritten on retry (first value preserved)",
              row5_retry["completed_at"] == first_completed_at, repr(row5_retry))

        # done cannot become failed/cancelled (cross-check with fail/cancel).
        check("11j. a done operation cannot be failed", st.replacement_fail(op5, "x", "y", db_path=db) is False)
        check("11k. a done operation cannot be cancelled", st.replacement_cancel(op5, db_path=db) is False)

        # finalize on totally unknown operation_id.
        ok_ghost = st.replacement_finalize(_opid("ghost"), db_path=db)
        check("11l. finalize on an unknown operation_id returns False", ok_ghost is False)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 12. replacement_list_for_source / notify-eligible (begins at cutover_done)
# ======================================================================

def test_list_and_notify_eligible():
    print("\n-- 12: replacement_list_for_source / notify-eligible begins at cutover_done --")
    db = _tmp_db()
    try:
        # op1: only reaches links_ready -- must NOT be eligible (post-B1 fix).
        op1 = _opid("list1")
        st.replacement_create(op1, "primaryK", source_key="srcX", db_path=db)
        _advance_chain(db, op1, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "list1_new"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op1, 15, db_path=db)
        st.replacement_advance(op1, "links_pending", "links_ready", db_path=db)

        # op2: reaches cutover_done -- eligible.
        op2 = _opid("list2")
        st.replacement_create(op2, "primaryL", source_key="srcX", db_path=db)
        _advance_chain(db, op2, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "list2_new"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op2, 15, db_path=db)
        st.replacement_advance(op2, "links_pending", "links_ready", db_path=db)
        st.replacement_advance(op2, "links_ready", "cutover_done", db_path=db)

        # op3: failed -- never eligible.
        op3 = _opid("list3")
        st.replacement_create(op3, "primaryM", source_key="srcX", db_path=db)
        st.replacement_fail(op3, "auth_phone", "banned", db_path=db)

        # op4: different source entirely.
        op4 = _opid("list4")
        st.replacement_create(op4, "primaryN", source_key="srcY", db_path=db)

        all_x = st.replacement_list_for_source("srcX", db_path=db)
        check("12a. list_for_source(srcX) returns exactly 3 rows (op1,op2,op3)", len(all_x) == 3, repr(len(all_x)))

        eligible_x = st.replacement_list_notify_eligible_for_source("srcX", db_path=db)
        eligible_ops = {r["operation_id"] for r in eligible_x}
        check("12b. notify-eligible for srcX contains ONLY op2 (cutover_done) -- NOT op1 (only links_ready)",
              eligible_ops == {op2}, repr(eligible_ops))

        eligible_y = st.replacement_list_notify_eligible_for_source("srcY", db_path=db)
        check("12c. srcY has zero notify-eligible rows (op4 still draft)", eligible_y == [], repr(eligible_y))

        none_source = st.replacement_list_for_source("does_not_exist", db_path=db)
        check("12d. an unrelated source returns an empty list", none_source == [], repr(none_source))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 13. Acknowledgement: source + status validation (R3)
# ======================================================================

def test_ack_source_and_status_validation():
    print("\n-- 13: replacement_ack_insert -- source AND status validated, per-user, idempotent --")
    db = _tmp_db()
    try:
        op_cutover = _opid("ackc")
        st.replacement_create(op_cutover, "primaryAckC", source_key="srcAck", db_path=db)
        _advance_chain(db, op_cutover, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "ackc_new"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op_cutover, 15, db_path=db)
        st.replacement_advance(op_cutover, "links_pending", "links_ready", db_path=db)
        st.replacement_advance(op_cutover, "links_ready", "cutover_done", db_path=db)
        rid_cutover = st.replacement_get(op_cutover, db_path=db)["id"]

        op_notified = _opid("ackn")
        st.replacement_create(op_notified, "primaryAckN", source_key="srcAck", db_path=db)
        _advance_chain(db, op_notified, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "ackn_new"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op_notified, 15, db_path=db)
        _advance_chain(db, op_notified, [
            ("links_pending", "links_ready", "ok", None),
            ("links_ready", "cutover_done", "cutover", None),
            ("cutover_done", "notified", "notify", None),
        ])
        rid_notified = st.replacement_get(op_notified, db_path=db)["id"]

        op_done = _make_ready_operation(db, "ackd", "primaryAckD")
        st.replacement_finalize(op_done, db_path=db)
        # Note: _make_ready_operation used source_key="src1", not "srcAck".
        rid_done = st.replacement_get(op_done, db_path=db)["id"]

        check("13a. ack succeeds for correct source + cutover_done",
              st.replacement_ack_insert(100, rid_cutover, "srcAck", db_path=db) is True)
        check("13b. ack succeeds for correct source + notified",
              st.replacement_ack_insert(100, rid_notified, "srcAck", db_path=db) is True)
        check("13c. ack succeeds for correct source + done",
              st.replacement_ack_insert(100, rid_done, "src1", db_path=db) is True)

        # Wrong source.
        check("13d. ack REJECTED for wrong source_key (correct id, wrong source)",
              st.replacement_ack_insert(200, rid_cutover, "totally_wrong_source", db_path=db) is False)
        check("13d2. no ack row was created by the wrong-source attempt",
              st.replacement_ack_exists(200, rid_cutover, db_path=db) is False)

        # Draft/links_ready/failed/cancelled -- not notify-eligible, must reject.
        op_draft = _opid("ackdr")
        st.replacement_create(op_draft, "primaryAckDr", source_key="srcAck", db_path=db)
        rid_draft = st.replacement_get(op_draft, db_path=db)["id"]
        check("13e. ack REJECTED for a draft (never-progressed) operation",
              st.replacement_ack_insert(100, rid_draft, "srcAck", db_path=db) is False)

        op_links_ready = _opid("acklr")
        st.replacement_create(op_links_ready, "primaryAckLR", source_key="srcAck", db_path=db)
        _advance_chain(db, op_links_ready, [
            ("draft", "auth_phone", "p", None), ("auth_phone", "auth_code", "c", None),
            ("auth_code", "identity_ok", "id", None),
            ("identity_ok", "ready_commit", "ready", {"new_manager_key": "acklr_new"}),
            ("ready_commit", "committing", "commit", None),
            ("committing", "links_pending", "wait", None),
        ])
        st.replacement_update_links(op_links_ready, 15, db_path=db)
        st.replacement_advance(op_links_ready, "links_pending", "links_ready", db_path=db)
        rid_links_ready = st.replacement_get(op_links_ready, db_path=db)["id"]
        check("13f. ack REJECTED for links_ready (cutover not yet done)",
              st.replacement_ack_insert(100, rid_links_ready, "srcAck", db_path=db) is False)

        op_failed = _opid("ackf")
        st.replacement_create(op_failed, "primaryAckF", source_key="srcAck", db_path=db)
        st.replacement_fail(op_failed, "auth_phone", "banned", db_path=db)
        rid_failed = st.replacement_get(op_failed, db_path=db)["id"]
        check("13g. ack REJECTED for a failed operation", st.replacement_ack_insert(100, rid_failed, "srcAck", db_path=db) is False)

        op_cancelled = _opid("ackcnl")
        st.replacement_create(op_cancelled, "primaryAckCnl", source_key="srcAck", db_path=db)
        st.replacement_cancel(op_cancelled, db_path=db)
        rid_cancelled = st.replacement_get(op_cancelled, db_path=db)["id"]
        check("13h. ack REJECTED for a cancelled operation", st.replacement_ack_insert(100, rid_cancelled, "srcAck", db_path=db) is False)

        # Missing replacement id.
        check("13i. ack REJECTED for a non-existent replacement id",
              st.replacement_ack_insert(100, 999999, "srcAck", db_path=db) is False)

        # Repeated valid ack is idempotent.
        check("13j. repeated valid ack (same user, same row, same source) is idempotent (True again)",
              st.replacement_ack_insert(100, rid_cutover, "srcAck", db_path=db) is True)
        check("13j2. still exactly one ack row for that (user, replacement)",
              _row_count(db, "replacement_notify_ack") == 3)  # 100/rid_cutover, 100/rid_notified, 100/rid_done(src1)

        # User isolation.
        check("13k. user 200 has NOT acked rid_cutover (per-user, not global)",
              st.replacement_ack_exists(200, rid_cutover, db_path=db) is False)
        check("13l. user 200 CAN ack rid_cutover independently",
              st.replacement_ack_insert(200, rid_cutover, "srcAck", db_path=db) is True)
        check("13m. user 100's ack is untouched by user 200's ack",
              st.replacement_ack_exists(100, rid_cutover, db_path=db) is True)

        # Invalid user_id.
        check("13n. ack with invalid user_id (0) returns False", st.replacement_ack_insert(0, rid_cutover, "srcAck", db_path=db) is False)
        check("13o. ack with empty source_key returns False", st.replacement_ack_insert(300, rid_cutover, "", db_path=db) is False)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 14. replacement_ack_unacked_ids
# ======================================================================

def test_ack_unacked_ids():
    print("\n-- 14: replacement_ack_unacked_ids -- per-user unread set --")
    db = _tmp_db()
    try:
        op = _make_ready_operation(db, "unread1", "primaryUnread1")
        # op is 'notified' after _make_ready_operation -- notify-eligible.
        rid = st.replacement_get(op, db_path=db)["id"]

        check("14a. not yet acked by user 100", st.replacement_ack_exists(100, rid, db_path=db) is False)
        st.replacement_ack_insert(100, rid, "src1", db_path=db)
        check("14b. now acked by user 100", st.replacement_ack_exists(100, rid, db_path=db) is True)

        op2 = _make_ready_operation(db, "unread2", "primaryUnread2")
        rid2 = st.replacement_get(op2, db_path=db)["id"]

        unacked_100 = st.replacement_ack_unacked_ids(100, [rid, rid2], db_path=db)
        check("14c. unacked_ids for user 100 excludes the acked id, includes the unacked one",
              unacked_100 == [rid2], repr(unacked_100))

        unacked_200 = st.replacement_ack_unacked_ids(200, [rid, rid2], db_path=db)
        check("14d. unacked_ids for user 200 (acked nothing) includes both", unacked_200 == [rid, rid2], repr(unacked_200))

        multi = st.replacement_ack_unacked_ids(200, [rid2, rid, rid, -1, 0], db_path=db)
        check("14e. unacked_ids preserves input order, dedups, drops invalid ids", multi == [rid2, rid], repr(multi))

        check("14f. invalid user_id (<=0) returns empty list", st.replacement_ack_unacked_ids(0, [rid], db_path=db) == [])
        check("14g. ack_exists with invalid ids returns False", st.replacement_ack_exists(0, 0, db_path=db) is False)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 15. Seven-day acknowledgement pruning (R2)
# ======================================================================

def test_ack_prune():
    print("\n-- 15: replacement_ack_prune -- boundary cutoff, only ack table touched --")
    db = _tmp_db()
    try:
        op_old = _make_ready_operation(db, "prune_old", "primaryPruneOld")
        op_boundary = _make_ready_operation(db, "prune_boundary", "primaryPruneBoundary")
        op_new = _make_ready_operation(db, "prune_new", "primaryPruneNew")
        rid_old = st.replacement_get(op_old, db_path=db)["id"]
        rid_boundary = st.replacement_get(op_boundary, db_path=db)["id"]
        rid_new = st.replacement_get(op_new, db_path=db)["id"]

        # Insert ack rows directly with controlled acknowledged_at timestamps
        # (replacement_ack_insert always uses "now" -- for a deterministic
        # boundary test we write the timestamps directly, which is fair
        # since replacement_ack_prune's own contract only cares about the
        # stored acknowledged_at column, not how it got there).
        st.replacement_ack_insert(1, rid_old, "src1", db_path=db)
        st.replacement_ack_insert(1, rid_boundary, "src1", db_path=db)
        st.replacement_ack_insert(1, rid_new, "src1", db_path=db)
        con = sqlite3.connect(db)
        con.execute("UPDATE replacement_notify_ack SET acknowledged_at='2026-07-01T00:00:00' WHERE replacement_id=?", (rid_old,))
        con.execute("UPDATE replacement_notify_ack SET acknowledged_at='2026-07-08T00:00:00' WHERE replacement_id=?", (rid_boundary,))
        con.execute("UPDATE replacement_notify_ack SET acknowledged_at='2026-07-15T00:00:00' WHERE replacement_id=?", (rid_new,))
        con.commit()
        con.close()

        cutoff = "2026-07-08T00:00:00"
        deleted = st.replacement_ack_prune(cutoff, db_path=db)
        check("15a. exactly one row deleted (only the strictly-older one)", deleted == 1, repr(deleted))
        check("15b. the OLD ack row (< cutoff) is gone", st.replacement_ack_exists(1, rid_old, db_path=db) is False)
        check("15c. the BOUNDARY ack row (== cutoff) is KEPT", st.replacement_ack_exists(1, rid_boundary, db_path=db) is True)
        check("15d. the NEW ack row (> cutoff) is KEPT", st.replacement_ack_exists(1, rid_new, db_path=db) is True)

        check("15e. manager_replacements is untouched by pruning (all 3 rows still exist)",
              st.replacement_get(op_old, db_path=db) is not None
              and st.replacement_get(op_boundary, db_path=db) is not None
              and st.replacement_get(op_new, db_path=db) is not None)

        check("15f. empty cutoff deletes nothing (fails closed)", st.replacement_ack_prune("", db_path=db) == 0)
        check("15g. re-pruning with the same cutoff deletes 0 more rows (already gone)",
              st.replacement_ack_prune(cutoff, db_path=db) == 0)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 16. Concurrency: DB-level constraint verification via two connections
# ======================================================================

def test_concurrency_two_connections():
    print("\n-- 16: two independent sqlite3 connections -- DB-level uniqueness, not just Python pre-checks --")
    db = _tmp_db()
    try:
        st.ensure_replacement_tables(db_path=db)
        now = st._now_iso()

        con_a = sqlite3.connect(db, timeout=5)
        con_b = sqlite3.connect(db, timeout=5)
        try:
            con_a.execute(
                "INSERT INTO manager_replacements(operation_id, status, old_manager_key, created_at, updated_at)"
                " VALUES('conc-op-a','draft','conc-primary', ?, ?)", (now, now),
            )
            con_a.commit()
            raised = False
            try:
                con_b.execute(
                    "INSERT INTO manager_replacements(operation_id, status, old_manager_key, created_at, updated_at)"
                    " VALUES('conc-op-b','draft','conc-primary', ?, ?)", (now, now),
                )
                con_b.commit()
            except sqlite3.IntegrityError:
                raised = True
                con_b.rollback()  # release con_b's implicit transaction/lock before reusing it
            check("16a. a SECOND raw connection cannot insert a second active row for the same "
                  "old_manager_key -- ux_repl_active_old is enforced at the DB level, not just in Python",
                  raised is True)

            con_a.execute(
                "UPDATE manager_replacements SET new_manager_key='conc-new', status='ready_commit'"
                " WHERE operation_id='conc-op-a'"
            )
            con_a.commit()
            raised2 = False
            try:
                con_b.execute(
                    "INSERT INTO manager_replacements(operation_id, status, old_manager_key, new_manager_key, created_at, updated_at)"
                    " VALUES('conc-op-c','ready_commit','conc-primary-2','conc-new', ?, ?)", (now, now),
                )
                con_b.commit()
            except sqlite3.IntegrityError:
                raised2 = True
                con_b.rollback()
            check("16b. a SECOND raw connection cannot claim the same new_manager_key -- "
                  "ux_repl_new_key is enforced at the DB level", raised2 is True)
        finally:
            con_a.close()
            con_b.close()

        # CAS has exactly one winner even when simulated as two sequential
        # connections racing the same UPDATE ... WHERE status=? predicate.
        op = _opid("cas_race")
        st.replacement_create(op, "primaryCasRace", source_key="src1", db_path=db)
        con_c = sqlite3.connect(db, timeout=5)
        con_d = sqlite3.connect(db, timeout=5)
        try:
            cur_c = con_c.execute(
                "UPDATE manager_replacements SET status='auth_phone', updated_at=?"
                " WHERE operation_id=? AND status='draft'", (now, op),
            )
            con_c.commit()
            cur_d = con_d.execute(
                "UPDATE manager_replacements SET status='auth_phone', updated_at=?"
                " WHERE operation_id=? AND status='draft'", (now, op),
            )
            con_d.commit()
            check("16c. exactly one of two racing CAS UPDATEs actually matched a row (one winner)",
                  (cur_c.rowcount, cur_d.rowcount) in ((1, 0), (0, 1)), repr((cur_c.rowcount, cur_d.rowcount)))
        finally:
            con_c.close()
            con_d.close()
    finally:
        Path(db).unlink(missing_ok=True)


def test_dbguard() -> None:
    """Direct unit test of _selftest_db_guard's own logic against every
    unsafe scenario, without ever touching the real storage.DB_PATH/
    QUEUE_DB_PATH globals or creating a file under the project's db/."""
    class _FakeStorage:
        def __init__(self, db_path, queue_db_path):
            self.DB_PATH = db_path
            self.QUEUE_DB_PATH = queue_db_path

    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    prod_nested = os.path.join(prod_dir, "nested", "sub", "x.db")
    temp_root = tempfile.mkdtemp(prefix="dbguard_storage_probe_")
    safe_path = os.path.join(temp_root, "safe.db")
    outside_temp_root_path = os.path.join(tempfile.gettempdir(), "dbguard_storage_outside.db")
    sibling_path = os.path.join(str(BASE_DIR), "dbfoo", "x.db")

    def _raises(db_path, storage_mod=None, temp_root_arg: str = "") -> bool:
        try:
            _selftest_db_guard(db_path, BASE_DIR, storage_mod, temp_root=temp_root_arg)
            return False
        except AssertionError:
            return True

    check("dbguard-1. valid temp path passes (no storage_mod, no temp_root)", not _raises(safe_path))
    check("dbguard-2. production DB file rejected", _raises(prod_file))
    check("dbguard-3. nested production DB path rejected", _raises(prod_nested))
    check("dbguard-3b. bare production db/ directory itself rejected", _raises(prod_dir))
    fake_ok = _FakeStorage(safe_path, safe_path)
    check("dbguard-4. DB_PATH mismatch rejected", _raises(safe_path, _FakeStorage("other.db", safe_path)))
    check("dbguard-5. QUEUE_DB_PATH mismatch rejected", _raises(safe_path, _FakeStorage(safe_path, "other.db")))
    check("dbguard-6. both globals wrong rejected", _raises(safe_path, _FakeStorage("other.db", "other2.db")))
    check("dbguard-6b. matching storage_mod globals pass", not _raises(safe_path, fake_ok))
    check("dbguard-7. similarly named sibling directory ('dbfoo') allowed", not _raises(sibling_path))
    prod_mtime_before = os.path.getmtime(prod_file) if os.path.exists(prod_file) else None
    _raises(prod_file)  # the guard call itself must never touch the real file
    prod_mtime_after = os.path.getmtime(prod_file) if os.path.exists(prod_file) else None
    check("dbguard-8. the guard never opens/modifies the real production file", prod_mtime_before == prod_mtime_after, (prod_mtime_before, prod_mtime_after))
    check("dbguard-9. path outside its declared temp_root is rejected", _raises(outside_temp_root_path, temp_root_arg=temp_root))
    check("dbguard-10. path inside its declared temp_root passes", not _raises(safe_path, temp_root_arg=temp_root))
    try:
        os.rmdir(temp_root)
    except Exception:
        pass


def main() -> int:
    test_dbguard()
    test_schema_idempotent()
    test_schema_migration_existing_table()
    test_create_idempotent_and_one_active_per_primary()
    test_read_helpers()
    test_state_ordering_b1()
    test_advance_races_and_immutability()
    test_new_manager_key_uniqueness()
    test_new_tg_user_id_uniqueness()
    test_durable_proxy_mutable_fields()
    test_cancel_rules()
    test_fail_rules()
    test_link_progress()
    test_finalize_guards()
    test_list_and_notify_eligible()
    test_ack_source_and_status_validation()
    test_ack_unacked_ids()
    test_ack_prune()
    test_concurrency_two_connections()

    total = len(FAILURES)
    print(f"\n{'='*70}")
    if FAILURES:
        print(f"SELFTEST FAIL: {total} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
