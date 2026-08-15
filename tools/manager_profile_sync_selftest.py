# -*- coding: utf-8 -*-
"""tools/manager_profile_sync_selftest.py -- offline self-test for the
"Telegram manager profile-name sync" patch:
  * storage.manager_sync_telegram_profile_in_db (registry-DB profile sync)
  * storage.manager_rename now setting display_name_source='manual'
  * the additive managers.display_name_source migration
  * partner_stat_bot._manager_account_label rendering display_name + @username

Business rule under test: a manager's Telegram-reported name (first/last/username)
must sync into display_name automatically, EXCEPT when an admin has set a manual
alias via manager_rename (display_name_source='manual') -- that alias must never
be overwritten by a later Telegram-profile sync.

Techniques (matching the project's own tools/*_selftest.py conventions):
* storage.py IS importable -> real async helpers exercised against throwaway
  temporary SQLite files (never the real project DB);
* partner_stat_bot.py is NOT importable (Telethon/env side effects at import)
  -> _manager_account_label (+ its _norm_key dependency) is AST-extracted and
  exec'd, same technique as tools/reserve_release_selftest.py.

Pure/offline: no network, no Telegram, no real DB, no real API calls.

    python3.12 tools\\manager_profile_sync_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage

PARTNER_STAT_BOT_PY = BASE_DIR / "partner_stat_bot.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def extract_and_exec(path: Path, names: set[str], extra_ns: dict) -> dict:
    """AST-extract the LAST (active) def of each requested name and exec into a
    seeded namespace -- same technique as tools/reserve_release_selftest.py."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    picked: dict = {}
    for node in tree.body:
        if getattr(node, "name", None) in names:
            picked[node.name] = node  # later defs overwrite earlier -> last wins
    if set(picked) != names:
        raise AssertionError(f"expected {names}, found {set(picked)} in {path}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in names)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path.name}>", "exec"), ns)
    return ns


def _row(tmp_db: str, key: str) -> dict:
    con = sqlite3.connect(tmp_db)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM managers WHERE manager_key=?", (key,)).fetchone()
        return dict(r) if r else {}
    finally:
        con.close()


def run_storage_checks(tmp_db: str) -> None:
    print("\n-- Storage: manager_sync_telegram_profile_in_db / manager_rename (real helpers, temp SQLite) --")

    # manager_add/manager_rename target the module-level storage.DB_PATH (not a
    # db_path parameter -- only manager_sync_telegram_profile_in_db takes one
    # explicitly, matching how the real controller process calls it against
    # TPILOT_DB_PATH). Point the module global at our throwaway temp file so every
    # helper in this test operates on the SAME file, never the real project DB.
    storage.DB_PATH = tmp_db

    # ------------------------------------------------------------------
    # 1. manager_add seeds display_name_source='auto' by default (schema default,
    #    no explicit column in the INSERT statement).
    # ------------------------------------------------------------------
    asyncio.run(storage.manager_add(
        manager_key="mgr_a", display_name="mgr_a", phone="", status="new",
        session_path="", db_path=tmp_db, workdir="", log_path="",
    ))
    row = _row(tmp_db, "mgr_a")
    check("1. new manager row defaults to display_name_source='auto'",
          row.get("display_name_source") == "auto", repr(row.get("display_name_source")))
    check("1b. new manager row's display_name equals the key (pre-sync placeholder)",
          row.get("display_name") == "mgr_a", repr(row.get("display_name")))

    # ------------------------------------------------------------------
    # 2/3. Auto sync fills an empty/key-equal display_name from the Telegram name,
    #    and updates telegram_username/first_name/last_name/tg_user_id.
    # ------------------------------------------------------------------
    asyncio.run(storage.manager_sync_telegram_profile_in_db(
        tmp_db, "mgr_a",
        tg_user_id=555001, telegram_username="ivan_tg", first_name="Иван", last_name="Петров",
        phone="+70001112233", status="active", last_login_at="2026-07-11T10:00:00",
    ))
    row = _row(tmp_db, "mgr_a")
    check("2. auto sync (source='auto') updates display_name from Telegram first+last name",
          row.get("display_name") == "Иван Петров", repr(row.get("display_name")))
    check("2b. display_name_source stays 'auto' after an auto sync", row.get("display_name_source") == "auto")
    check("2c. telegram_username updated", row.get("telegram_username") == "ivan_tg", repr(row.get("telegram_username")))
    check("2d. first_name updated", row.get("first_name") == "Иван", repr(row.get("first_name")))
    check("2e. last_name updated", row.get("last_name") == "Петров", repr(row.get("last_name")))
    check("2f. tg_user_id updated", row.get("tg_user_id") == 555001, repr(row.get("tg_user_id")))
    check("2g. phone/status/last_login_at updated (preserved prior behavior)",
          row.get("phone") == "+70001112233" and row.get("status") == "active"
          and row.get("last_login_at") == "2026-07-11T10:00:00")

    # ------------------------------------------------------------------
    # 3b. A REAL Telegram rename (the actual bug being fixed): sync again with a
    #    DIFFERENT first/last name must update display_name again (still auto).
    # ------------------------------------------------------------------
    asyncio.run(storage.manager_sync_telegram_profile_in_db(
        tmp_db, "mgr_a", tg_user_id=555001, telegram_username="ivan_tg",
        first_name="Иван", last_name="Сидоров",
    ))
    row = _row(tmp_db, "mgr_a")
    check("3. a second Telegram rename (Петров -> Сидоров) propagates into display_name -- THE BUG BEING FIXED",
          row.get("display_name") == "Иван Сидоров", repr(row.get("display_name")))

    # ------------------------------------------------------------------
    # 6. manager_rename marks display_name_source='manual'.
    # ------------------------------------------------------------------
    asyncio.run(storage.manager_rename("mgr_a", "Директор Иван"))
    row = _row(tmp_db, "mgr_a")
    check("6. manager_rename sets display_name_source='manual'", row.get("display_name_source") == "manual")
    check("6b. manager_rename applies the admin's alias text", row.get("display_name") == "Директор Иван", repr(row.get("display_name")))

    # ------------------------------------------------------------------
    # 4/5b. Manual alias protection: a further Telegram rename (even with a
    #    completely different name) must NOT touch display_name, but username/
    #    first/last/tg_user_id must still refresh normally.
    # ------------------------------------------------------------------
    asyncio.run(storage.manager_sync_telegram_profile_in_db(
        tmp_db, "mgr_a", tg_user_id=555002, telegram_username="ivan_new_handle",
        first_name="Ivan", last_name="Newname",
    ))
    row = _row(tmp_db, "mgr_a")
    check("4. manual alias ('Директор Иван') is preserved despite a Telegram rename",
          row.get("display_name") == "Директор Иван", repr(row.get("display_name")))
    check("4b. display_name_source remains 'manual' (sync never flips it back to auto)",
          row.get("display_name_source") == "manual")
    check("5b. telegram_username STILL updates while display_name is manually locked",
          row.get("telegram_username") == "ivan_new_handle", repr(row.get("telegram_username")))
    check("5c. first_name/last_name STILL update while display_name is manually locked",
          row.get("first_name") == "Ivan" and row.get("last_name") == "Newname")
    check("5d. tg_user_id STILL updates while display_name is manually locked",
          row.get("tg_user_id") == 555002, repr(row.get("tg_user_id")))

    # ------------------------------------------------------------------
    # 5. Fallback chain: no first/last -> "@username"; no first/last/username ->
    #    manager_key. Exercised on a fresh 'auto' manager (mgr_b).
    # ------------------------------------------------------------------
    asyncio.run(storage.manager_add(
        manager_key="mgr_b", display_name="mgr_b", phone="", status="new",
        session_path="", db_path=tmp_db, workdir="", log_path="",
    ))
    asyncio.run(storage.manager_sync_telegram_profile_in_db(
        tmp_db, "mgr_b", tg_user_id=555003, telegram_username="just_a_handle",
        first_name="", last_name="",
    ))
    row = _row(tmp_db, "mgr_b")
    check("5. no first/last name -> display_name falls back to '@username'",
          row.get("display_name") == "@just_a_handle", repr(row.get("display_name")))

    asyncio.run(storage.manager_add(
        manager_key="mgr_c", display_name="mgr_c", phone="", status="new",
        session_path="", db_path=tmp_db, workdir="", log_path="",
    ))
    asyncio.run(storage.manager_sync_telegram_profile_in_db(
        tmp_db, "mgr_c", tg_user_id=555004, telegram_username="", first_name="", last_name="",
    ))
    row = _row(tmp_db, "mgr_c")
    check("5e. no first/last/username at all -> display_name falls back to manager_key",
          row.get("display_name") == "mgr_c", repr(row.get("display_name")))

    # ------------------------------------------------------------------
    # 7. Unrelated manager row is untouched by any of the above operations.
    # ------------------------------------------------------------------
    asyncio.run(storage.manager_add(
        manager_key="mgr_untouched", display_name="Original Name", phone="+79990000000",
        status="active", session_path="", db_path=tmp_db, workdir="", log_path="",
    ))
    before = _row(tmp_db, "mgr_untouched")
    asyncio.run(storage.manager_sync_telegram_profile_in_db(
        tmp_db, "mgr_a", tg_user_id=1, telegram_username="noise", first_name="Noise", last_name="Noise",
    ))
    after = _row(tmp_db, "mgr_untouched")
    check("7. an unrelated manager row is completely unaffected by another manager's sync", before == after, f"{before!r} != {after!r}")


def run_migration_checks() -> None:
    print("\n-- Migration: display_name_source additive/idempotent (real storage.py path) --")

    # ------------------------------------------------------------------
    # 8. Idempotent when the column already exists: run sync twice in a row on a
    #    freshly-created (already-current-schema) DB -- must not raise.
    # ------------------------------------------------------------------
    tmp_db2 = tempfile.mktemp(suffix="_mgrsync_migration.db")
    try:
        storage.DB_PATH = tmp_db2  # manager_add targets the module global, see run_storage_checks
        asyncio.run(storage.manager_add(
            manager_key="mgr_idem", display_name="mgr_idem", phone="", status="new",
            session_path="", db_path=tmp_db2, workdir="", log_path="",
        ))
        asyncio.run(storage.manager_sync_telegram_profile_in_db(tmp_db2, "mgr_idem", first_name="A", last_name="B"))
        asyncio.run(storage.manager_sync_telegram_profile_in_db(tmp_db2, "mgr_idem", first_name="A", last_name="B"))
        row = _row(tmp_db2, "mgr_idem")
        check("8. running the sync twice against an already-migrated DB does not raise and is idempotent",
              row.get("display_name") == "A B", repr(row))
    finally:
        try:
            if os.path.exists(tmp_db2):
                os.remove(tmp_db2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 9. Legacy DB: a managers table created WITHOUT display_name_source (the
    #    pre-patch shape) must gain the column via the guarded ALTER the very
    #    first time any managers-table helper touches it, with the existing row
    #    backfilled to the 'auto' default -- exercised through the real public
    #    entrypoint (manager_sync_telegram_profile_in_db), not private internals.
    # ------------------------------------------------------------------
    tmp_db3 = tempfile.mktemp(suffix="_mgrsync_legacy.db")
    try:
        con = sqlite3.connect(tmp_db3)
        con.execute(
            "CREATE TABLE managers(id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT UNIQUE NOT NULL, "
            "display_name TEXT NOT NULL DEFAULT '', phone TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new', "
            "session_path TEXT DEFAULT '', db_path TEXT DEFAULT '', workdir TEXT DEFAULT '', log_path TEXT DEFAULT '', "
            "is_enabled INTEGER NOT NULL DEFAULT 1, manual_stopped INTEGER NOT NULL DEFAULT 0, owner_user_id INTEGER, "
            "tg_user_id INTEGER, telegram_username TEXT DEFAULT '', first_name TEXT DEFAULT '', last_name TEXT DEFAULT '', "
            "last_login_at TEXT DEFAULT '', last_error TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT '', "
            "updated_at TEXT NOT NULL DEFAULT '')"
        )
        con.execute(
            "INSERT INTO managers(manager_key, display_name, created_at, updated_at) VALUES('mgr_legacy','mgr_legacy','t','t')"
        )
        cols_before = {r[1] for r in con.execute("PRAGMA table_info(managers)").fetchall()}
        con.commit()
        con.close()
        check("9a. legacy table genuinely lacks display_name_source before the sync runs",
              "display_name_source" not in cols_before, str(cols_before))

        asyncio.run(storage.manager_sync_telegram_profile_in_db(
            tmp_db3, "mgr_legacy", telegram_username="legacy_user", first_name="Legacy", last_name="User",
        ))

        con = sqlite3.connect(tmp_db3)
        cols_after = {r[1] for r in con.execute("PRAGMA table_info(managers)").fetchall()}
        row = dict(zip(
            [d[0] for d in con.execute("SELECT * FROM managers WHERE manager_key='mgr_legacy'").description],
            con.execute("SELECT * FROM managers WHERE manager_key='mgr_legacy'").fetchone(),
        ))
        con.close()
        check("9b. the guarded ALTER adds display_name_source to a legacy table on first touch",
              "display_name_source" in cols_after, str(cols_after))
        check("9c. the pre-existing legacy row is backfilled to the 'auto' default",
              row.get("display_name_source") == "auto", repr(row.get("display_name_source")))
        check("9d. the legacy row's display_name synced from Telegram right away (source was backfilled to 'auto')",
              row.get("display_name") == "Legacy User", repr(row.get("display_name")))
    finally:
        try:
            if os.path.exists(tmp_db3):
                os.remove(tmp_db3)
        except Exception:
            pass


def run_partner_label_checks() -> None:
    print("\n-- PartnerBot: _manager_account_label (real function via AST, no DB) --")

    ns = extract_and_exec(PARTNER_STAT_BOT_PY, {"_manager_account_label", "_norm_key"}, {"re": __import__("re")})
    label = ns["_manager_account_label"]

    check("7a. display_name + differing @username -> 'Display | @username'",
          label({"display_name": "Иван Сидоров", "telegram_username": "ivan_tg"}) == "Иван Сидоров | @ivan_tg")
    check("username-only -> '@username'", label({"display_name": "", "telegram_username": "only_user"}) == "@only_user")
    check("display_name-only -> 'Display'", label({"display_name": "Только Имя", "telegram_username": ""}) == "Только Имя")
    check("neither present -> falls back to manager_key",
          label({"display_name": "", "telegram_username": "", "manager_key": "mgr_z"}) == "mgr_z")
    check("neither present and no manager_key -> fallback_key param is honored",
          label({"display_name": "", "telegram_username": ""}, fallback_key="mgr_fb") == "mgr_fb")
    check("display_name identical to username -> collapses to '@username' (no redundant duplication)",
          label({"display_name": "sameword", "telegram_username": "sameword"}) == "@sameword")


def run_safety_checks(tmp_paths: list[str]) -> None:
    print("\n-- Safety: only temp SQLite files were used --")
    for p in tmp_paths:
        real = os.path.realpath(p)
        check(f"8. temp DB path is under the OS temp dir, never db/data_tpilot.db or db/data.db: {os.path.basename(p)}",
              os.path.realpath(tempfile.gettempdir()) in real
              and "data_tpilot.db" not in real and (os.sep + "db" + os.sep) not in real, real)


def main() -> int:
    original_db_path = storage.DB_PATH
    tmp_db = tempfile.mktemp(suffix="_mgrsync_selftest.db")
    try:
        run_storage_checks(tmp_db)
        run_migration_checks()
        run_partner_label_checks()
        run_safety_checks([tmp_db])
    finally:
        storage.DB_PATH = original_db_path  # never leave the module pointed at a temp file
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except Exception:
            pass

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
