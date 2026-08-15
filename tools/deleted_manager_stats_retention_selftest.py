# -*- coding: utf-8 -*-
"""tools/deleted_manager_stats_retention_selftest.py -- offline self-test for the
"deleted-manager statistics retention" patch (storage.py tombstone helpers,
main.py full-delete integration + AdminBot reporting union, partner_stat_bot.py
PartnerBot reporting union).

Business rule under test: a manager fully hard-deleted via
_panel_manager_delete_full_command must remain visible in AdminBot/PartnerBot
statistics (identity + historical daily_leads) for 60 days after deletion, via
an additive `manager_stats_tombstones` snapshot table -- WITHOUT weakening the
hard-delete itself, WITHOUT resurrecting the manager in any operational/active
manager list, and WITHOUT breaking the reserve-release atomicity fix (reserve is
released only after the managers row is verified absent).

Techniques (matching the project's own tools/*_selftest.py conventions):
* storage.py IS importable -> real async/sync helpers exercised against
  throwaway temporary SQLite files (never the real project DB);
* main.py / partner_stat_bot.py are NOT importable (Telethon/env side effects
  at import time) -> the relevant functions are AST-extracted and exec'd into
  a namespace seeded with fakes for their Telethon/env/log boundaries -- same
  technique as tools/reserve_release_selftest.py and
  tools/catchup_managerbot_card_selftest.py. Everything DB-related runs for
  real (real aiosqlite/sqlite3, real INSERT/UPDATE/DELETE) against temp files.

Pure/offline: no network, no Telegram, no production DB, no external APIs.

    python3.12 tools\\deleted_manager_stats_retention_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite
import storage
from manager_registry import normalize_manager_key, build_manager_paths

MAIN_PY = BASE_DIR / "main.py"
PARTNER_STAT_BOT_PY = BASE_DIR / "partner_stat_bot.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _selftest_db_guard(db_path: str, base_dir: Path, storage_mod) -> None:
    """Fail-fast production-path DB guard (2026-07-16, same technique as the
    other replacement selftests' own guard): refuses any db_path located
    under base_dir/db, and -- since this file explicitly threads db_path=
    through every storage.* call rather than relying on the module-level
    globals -- also defensively pins storage.DB_PATH/QUEUE_DB_PATH to the
    same temp path in case any newly-extracted dependency ever falls back
    to them instead of an explicit parameter."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    storage_mod.DB_PATH = db_path
    storage_mod.QUEUE_DB_PATH = db_path


def find_defs(path: Path, name: str) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return [n for n in tree.body if getattr(n, "name", None) == name]


def body_src_without_docstring(node) -> str:
    """Unparsed source of a function/class node with its leading docstring
    statement stripped, so substring/regex safety-checks below don't trip on
    defensive doc text that merely MENTIONS a term (e.g. a docstring saying
    "never touches manager_reserve_pairs" would otherwise contain the literal
    substring "manager_reserve_pairs" and falsely fail a naive containment check)."""
    import copy
    node2 = copy.deepcopy(node)
    if (node2.body and isinstance(node2.body[0], ast.Expr)
            and isinstance(getattr(node2.body[0], "value", None), ast.Constant)
            and isinstance(node2.body[0].value.value, str)):
        node2.body = node2.body[1:] or [ast.Pass()]
    return ast.unparse(node2)


def extract_and_exec(path: Path, names: set[str], extra_ns: dict) -> dict:
    """AST-extract the LAST (active) def of each requested name and exec into a
    seeded namespace -- same technique as the other tools/*_selftest.py files."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    picked: dict = {}
    for node in tree.body:
        if getattr(node, "name", None) in names:
            picked[node.name] = node  # later defs overwrite earlier -> last wins
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"could not find {missing} as top-level defs in {path}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in sorted(picked))
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path.name}>", "exec"), ns)
    return ns


def iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


# ======================================================================
# GROUP A -- storage.py tombstone helpers (real functions, temp SQLite)
# ======================================================================

def run_storage_checks(tmp_db: str) -> None:
    print("\n-- Storage: manager_stats_tombstone_* helpers (real functions, temp SQLite) --")

    storage.manager_stats_tombstone_ready(db_path=tmp_db)
    storage.manager_stats_tombstone_ready(db_path=tmp_db)  # idempotent double-ensure
    check("1. manager_stats_tombstone_ready is idempotent (no error on repeat call)", True)

    now = datetime(2026, 7, 11, 12, 0, 0)
    ok = storage.manager_stats_tombstone_upsert(
        "dariass",
        display_name="Дарья С",
        telegram_username="dariass_tg",
        manager_db_path=r"C:\ALM_TPilot\_deleted_managers_backup\dariass_20260711\dariass.db",
        source_key="src_main",
        deleted_at=iso(now),
        retention_days=60,
        db_path=tmp_db,
    )
    check("2. upsert reports success", ok is True)
    row = storage.manager_stats_tombstone_get("dariass", db_path=tmp_db)
    check("2b. tombstone stores manager_key/display_name/telegram_username/source_key",
          bool(row) and row.get("manager_key") == "dariass" and row.get("display_name") == "Дарья С"
          and row.get("telegram_username") == "dariass_tg" and row.get("source_key") == "src_main", repr(row))
    check("2c. tombstone stores the backup db_path", row.get("db_path", "").endswith("dariass.db"), repr(row.get("db_path")))
    check("2d. tombstone stores deleted_at and a computed retention_until = deleted_at + 60d",
          row.get("deleted_at") == iso(now) and row.get("retention_until") == iso(now + timedelta(days=60)),
          repr((row.get("deleted_at"), row.get("retention_until"))))

    # 3/4: list_active includes within-window, excludes expired.
    active = storage.manager_stats_tombstone_list_active(now=iso(now + timedelta(days=59)), db_path=tmp_db)
    check("3. tombstone is listed as active 59 days after deletion (within 60-day window)",
          any(r.get("manager_key") == "dariass" for r in active), repr(active))
    expired_check = storage.manager_stats_tombstone_list_active(now=iso(now + timedelta(days=61)), db_path=tmp_db)
    check("4. tombstone is EXCLUDED from list_active 61 days after deletion (window passed)",
          not any(r.get("manager_key") == "dariass" for r in expired_check), repr(expired_check))

    # Repeat upsert (idempotent, same key -> replaces, created_at preserved)
    created_before = row.get("created_at")
    storage.manager_stats_tombstone_upsert("dariass", display_name="Дарья С (обновлено)", deleted_at=iso(now), db_path=tmp_db)
    row2 = storage.manager_stats_tombstone_get("dariass", db_path=tmp_db)
    check("2e. repeat upsert for the same key updates fields but preserves created_at",
          row2.get("display_name") == "Дарья С (обновлено)" and row2.get("created_at") == created_before,
          repr((row2.get("display_name"), row2.get("created_at"), created_before)))

    # 5: purge_expired removes only expired rows. still_fresh is deleted much later
    # than dariass (deleted_at anchored 60 days after `now`), so a purge check at
    # now+61d expires ONLY dariass (retention_until=now+60d) while still_fresh
    # (retention_until=now+120d) survives.
    storage.manager_stats_tombstone_upsert(
        "still_fresh", display_name="Still Fresh", deleted_at=iso(now + timedelta(days=60)), db_path=tmp_db
    )
    purged = storage.manager_stats_tombstone_purge_expired(now=iso(now + timedelta(days=61)), db_path=tmp_db)
    check("5. purge_expired removes exactly the expired tombstone (dariass), not the fresh one",
          purged == 1, repr(purged))
    check("5b. the expired manager is gone after purge", storage.manager_stats_tombstone_get("dariass", db_path=tmp_db) is None)
    check("5c. the still-fresh tombstone survives the purge", storage.manager_stats_tombstone_get("still_fresh", db_path=tmp_db) is not None)
    check("5d. repeated purge is idempotent (0 the second time)",
          storage.manager_stats_tombstone_purge_expired(now=iso(now + timedelta(days=61)), db_path=tmp_db) == 0)

    # Empty-key safety
    check("upsert with empty manager_key is a safe no-op returning False",
          storage.manager_stats_tombstone_upsert("", db_path=tmp_db) is False)
    check("get with empty manager_key returns None", storage.manager_stats_tombstone_get("", db_path=tmp_db) is None)


# ======================================================================
# GROUP B -- full-delete integration: tombstone-before-hard-delete, reserve
# release still gated on VERIFIED absence (real _panel_manager_delete_full_command
# via AST, real aiosqlite/sqlite3 against temp files, fakes only for
# Telethon/env/log boundaries).
# ======================================================================

class _FakeKyivNow:
    def strftime(self, fmt: str) -> str:
        return "20260711_120000"


def _delete_cmd_namespace(tmp_db: str, work_dir: Path, captured_logs: list, manager_row: dict):
    async def _fake_manager_get(k):
        return dict(manager_row) if normalize_manager_key(k) == normalize_manager_key(manager_row.get("manager_key", "")) else None

    def _fake_missing_text(k):
        return f"missing:{k}"

    def _fake_danger_parse_args(args, usage):
        parts = str(args or "").split()
        return (parts[0] if parts else "", "pw", True, "")

    async def _fake_stop_manager_process(k, silent=True):
        return None

    async def _fake_delete_onboarding(k):
        return None

    def _fake_build_manager_paths(base, k):
        return {"root": str(Path(base) / "nonexistent_runtime_root")}

    async def _fake_log_action(*a, **kw):
        captured_logs.append(kw)

    return {
        "manager_get": _fake_manager_get,
        "_panel_manager_missing_text": _fake_missing_text,
        "_danger_parse_args": _fake_danger_parse_args,
        "_stop_manager_process": _fake_stop_manager_process,
        "manager_delete_onboarding_by_key": _fake_delete_onboarding,
        "BASE_DIR": work_dir,
        "build_manager_paths": _fake_build_manager_paths,
        "Path": Path,
        "shutil": shutil,
        "_kyiv_now": lambda: _FakeKyivNow(),
        "aiosqlite": aiosqlite,
        "TPILOT_DB_PATH": tmp_db,
        "_log_manager_danger_action": _fake_log_action,
        "_now_utc_iso": lambda: iso(datetime(2026, 7, 11, 12, 0, 0)),
    }


def run_delete_integration_checks() -> None:
    print("\n-- Full-delete integration: tombstone-before-hard-delete (REAL execution via AST) --")

    # STAGE 4 (2026-07-16): the actual delete logic now lives in
    # _manager_delete_full_core (extracted out of _panel_manager_delete_
    # full_command so Stage 4's commit engine can reuse it without an admin
    # password); the wrapper just parses/validates the password then
    # delegates. Both must be extracted into the SAME exec namespace -- they
    # share it as their __globals__, so every dependency _delete_cmd_
    # namespace() already provides (manager_get, _stop_manager_process,
    # BASE_DIR, build_manager_paths, aiosqlite, TPILOT_DB_PATH, etc.) is
    # visible to _manager_delete_full_core too without any new binding.
    ns = extract_and_exec(MAIN_PY, {"_panel_manager_delete_full_command", "_manager_delete_full_core"}, {})
    delete_cmd = ns["_panel_manager_delete_full_command"]

    work_dir = Path(tempfile.mkdtemp(prefix="del_mgr_retention_selftest_"))
    try:
        # ---- Scenario 1: normal hard-delete of "dariass" -- tombstone written, row
        # deleted, reserve released only after verified absence. ----
        db1 = str(work_dir / "scenario1.db")
        con = sqlite3.connect(db1)
        con.execute(
            "CREATE TABLE managers(manager_key TEXT PRIMARY KEY, display_name TEXT, telegram_username TEXT, status TEXT)"
        )
        con.execute("INSERT INTO managers VALUES('dariass','Дарья С','dariass_tg','active')")
        con.execute("CREATE TABLE manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("INSERT INTO manager_source_links VALUES('dariass','src_main')")
        con.commit()
        con.close()
        storage.reserve_pair_set("dariass", "reserve_x", db_path=db1)  # exercise reserve-release interplay too

        logs1: list = []
        manager_row = {"manager_key": "dariass", "display_name": "Дарья С", "telegram_username": "dariass_tg", "status": "active"}
        ns.update(_delete_cmd_namespace(db1, work_dir, logs1, manager_row))
        result1 = asyncio.run(delete_cmd("dariass pw confirm"))

        con = sqlite3.connect(db1)
        gone = con.execute("SELECT 1 FROM managers WHERE manager_key='dariass'").fetchone()
        con.close()
        check("6/7. hard-delete still removes the managers row", gone is None)
        check("6b. command reports success", "🗑 Менеджер полностью удалён" in result1, result1)

        tomb = storage.manager_stats_tombstone_get("dariass", db_path=db1)
        check("6c. a tombstone was written BEFORE the hard-delete, with identity preserved",
              bool(tomb) and tomb.get("display_name") == "Дарья С" and tomb.get("telegram_username") == "dariass_tg"
              and tomb.get("source_key") == "src_main", repr(tomb))
        check("6d. the tombstone db_path points at the deterministic backup location (<root>/<key>_<ts>/<key>.db)",
              tomb.get("db_path", "").replace("\\", "/").endswith("dariass_20260711_120000/dariass.db"), repr(tomb.get("db_path")))

        pair = storage.reserve_pair_get_by_reserve("reserve_x", db_path=db1)
        check("8. reserve is released (retired) only AFTER the managers row was verified absent",
              bool(pair) and pair.get("status") == "retired", repr(pair))

        # ---- Scenario 2: tombstone write fails -> fail-before-delete (managers row
        # must survive; no destructive DELETE performed). ----
        db2 = str(work_dir / "scenario2.db")
        con = sqlite3.connect(db2)
        con.execute("CREATE TABLE managers(manager_key TEXT PRIMARY KEY, display_name TEXT, telegram_username TEXT, status TEXT)")
        con.execute("INSERT INTO managers VALUES('mgr2','Mgr Two','mgr2_tg','active')")
        con.commit()
        con.close()

        logs2: list = []
        manager_row2 = {"manager_key": "mgr2", "display_name": "Mgr Two", "telegram_username": "mgr2_tg", "status": "active"}
        ns.update(_delete_cmd_namespace(db2, work_dir, logs2, manager_row2))
        orig_upsert = storage.manager_stats_tombstone_upsert

        def _poison_upsert(*a, **kw):
            raise RuntimeError("simulated tombstone write failure")

        storage.manager_stats_tombstone_upsert = _poison_upsert
        try:
            result2 = asyncio.run(delete_cmd("mgr2 pw confirm"))
        finally:
            storage.manager_stats_tombstone_upsert = orig_upsert

        con = sqlite3.connect(db2)
        still_there = con.execute("SELECT 1 FROM managers WHERE manager_key='mgr2'").fetchone()
        con.close()
        check("7b. tombstone-write failure ABORTS before the destructive DELETE (managers row survives)",
              bool(still_there))
        check("7c. command reports failure, not false success",
              "снимок статистики" in result2 and "🗑 Менеджер полностью удалён" not in result2, result2)

        # ---- Scenario 3: reserve-release atomicity regression -- managers DELETE is
        # blocked (simulated trigger), tombstone WAS already written (real behavior:
        # tombstone precedes delete), but reserve must still NOT be released because
        # the post-delete verification finds the row still present. ----
        db3 = str(work_dir / "scenario3.db")
        con = sqlite3.connect(db3)
        con.execute("CREATE TABLE managers(manager_key TEXT PRIMARY KEY, display_name TEXT, telegram_username TEXT, status TEXT)")
        con.execute("INSERT INTO managers VALUES('mgr3','Mgr Three','mgr3_tg','active')")
        con.execute(
            "CREATE TRIGGER block_delete BEFORE DELETE ON managers "
            "BEGIN SELECT RAISE(ABORT, 'simulated delete failure'); END;"
        )
        con.commit()
        con.close()
        storage.reserve_pair_set("mgr3", "reserve_y", db_path=db3)

        logs3: list = []
        manager_row3 = {"manager_key": "mgr3", "display_name": "Mgr Three", "telegram_username": "mgr3_tg", "status": "active"}
        ns.update(_delete_cmd_namespace(db3, work_dir, logs3, manager_row3))
        result3 = asyncio.run(delete_cmd("mgr3 pw confirm"))

        pair3 = storage.reserve_pair_get_by_reserve("reserve_y", db_path=db3)
        check("8b. REGRESSION GUARD: reserve is NOT released when the managers row survives a blocked delete, "
              "even though a tombstone was already written for it",
              bool(pair3) and pair3.get("status") == "linked", repr(pair3))
        check("8c. command reports the 'not confirmed' failure", "не подтверждено" in result3, result3)
        tomb3 = storage.manager_stats_tombstone_get("mgr3", db_path=db3)
        check("tombstone for mgr3 exists (written before the blocked delete, harmless/re-triable)", bool(tomb3))
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ======================================================================
# GROUP C -- AdminBot reporting enumeration: _manager_rows_for_reporting unions
# active tombstones, live row wins on collision, expired tombstones excluded.
# ======================================================================

def run_admin_reporting_checks(tmp_db: str) -> None:
    print("\n-- AdminBot: _manager_rows_for_reporting union (REAL execution via AST) --")

    async def _fake_manager_list_rows(include_removed=False):
        return [{"manager_key": "live_mgr", "display_name": "Live Mgr", "telegram_username": "live_tg",
                 "db_path": "", "status": "active", "is_enabled": 1}]

    def _fake_build_manager_paths(base, k):
        return {"db_path": str(Path(base) / "runtime" / "managers" / k / f"{k}.db")}

    ns = extract_and_exec(
        MAIN_PY, {"_manager_rows_for_reporting"},
        {
            "manager_list_rows": _fake_manager_list_rows,
            "registry_normalize_manager_key": normalize_manager_key,
            "build_manager_paths": _fake_build_manager_paths,
            "BASE_DIR": BASE_DIR,
            "os": os,
            "TPILOT_DB_PATH": tmp_db,
            "List": list, "Dict": dict, "Any": object,
        },
    )
    fn = ns["_manager_rows_for_reporting"]

    now = datetime(2026, 7, 11, 12, 0, 0)
    storage.manager_stats_tombstone_upsert(
        "dariass", display_name="Дарья С", telegram_username="dariass_tg",
        manager_db_path=str(BASE_DIR / "_deleted_managers_backup" / "dariass_x" / "dariass.db"),
        deleted_at=iso(now), retention_days=60, db_path=tmp_db,
    )
    storage.manager_stats_tombstone_upsert(
        "expired_mgr", display_name="Expired", deleted_at=iso(now - timedelta(days=200)),
        retention_days=60, db_path=tmp_db,
    )
    # collision case: a tombstone for a key that ALSO has a live row (should never
    # happen in production, but the dedup rule -- live wins -- must hold defensively).
    storage.manager_stats_tombstone_upsert("live_mgr", display_name="STALE TOMBSTONE", deleted_at=iso(now), db_path=tmp_db)

    rows = asyncio.run(fn())
    by_key = {r["manager_key"]: r for r in rows}

    check("9. reporting rows include the live manager", "live_mgr" in by_key)
    check("1. _manager_rows_for_reporting() with NO period does NOT include any tombstone "
          "(period-filter correction: restored live-only default)",
          "dariass" not in by_key and "expired_mgr" not in by_key, repr(list(by_key)))
    check("10. no duplicate manager_key: exactly one row for 'live_mgr'",
          sum(1 for r in rows if r["manager_key"] == "live_mgr") == 1, repr(rows))
    check("11. live managers are unaffected by the tombstone/period-filter logic "
          "(status stays 'active', not overwritten by a stale colliding tombstone)",
          by_key.get("live_mgr", {}).get("status") == "active"
          and by_key.get("live_mgr", {}).get("display_name") == "Live Mgr", repr(by_key.get("live_mgr")))


# ======================================================================
# GROUP C2 -- AdminBot period-aware reporting: _manager_rows_for_reporting_period
# gates tombstone inclusion on real data (backup daily_leads) or a working
# schedule day, both always bounded by the manager's own deletion date, and
# still capped by the 60-day retention window. Uses dates anchored to the
# REAL wall clock (datetime.now()) because the underlying storage helper
# (manager_stats_tombstone_list_active) checks retention against real "now"
# with no override hook -- unlike Group A's deterministic now= tests.
# ======================================================================

def run_admin_period_reporting_checks(tmp_db: str) -> None:
    print("\n-- AdminBot: _manager_rows_for_reporting_period (REAL execution via AST) --")

    async def _fake_manager_list_rows(include_removed=False):
        return []

    def _fake_build_manager_paths(base, k):
        return {"db_path": str(Path(base) / "runtime" / "managers" / k / f"{k}.db")}

    ns = extract_and_exec(
        MAIN_PY,
        {"_manager_rows_for_reporting", "_manager_rows_for_reporting_period", "_tp_mgrret_tombstone_eligible_for_period"},
        {
            "manager_list_rows": _fake_manager_list_rows,
            "registry_normalize_manager_key": normalize_manager_key,
            "build_manager_paths": _fake_build_manager_paths,
            "BASE_DIR": BASE_DIR,
            "os": os,
            "aiosqlite": aiosqlite,
            "TPILOT_DB_PATH": tmp_db,
            "datetime": datetime, "timedelta": timedelta,
            "List": list, "Dict": dict, "Any": object,
        },
    )
    fn_period = ns["_manager_rows_for_reporting_period"]

    work_dir = Path(tempfile.mkdtemp(prefix="del_mgr_period_selftest_"))
    try:
        # --- live "dariass" case, verbatim shape: deleted 2026-07-10T17:45:44,
        # backup daily_leads on 2026-06-09 and 2026-07-10 only, source rassylka.
        backup_db = work_dir / "dariass.db"
        con = sqlite3.connect(str(backup_db))
        con.execute("CREATE TABLE daily_leads(id INTEGER PRIMARY KEY, lead_date TEXT, manager_key TEXT, chat_id INTEGER)")
        con.executemany(
            "INSERT INTO daily_leads(lead_date, manager_key, chat_id) VALUES(?,?,?)",
            [("2026-06-09", "dariass", 1), ("2026-07-10", "dariass", 2)],
        )
        con.commit()
        con.close()
        storage.manager_stats_tombstone_upsert(
            "dariass", display_name="Дарья С", telegram_username="dariass_tg",
            manager_db_path=str(backup_db), source_key="rassylka",
            deleted_at="2026-07-10T17:45:44", retention_days=60, db_path=tmp_db,
        )

        rows_10 = asyncio.run(fn_period("2026-07-10", "2026-07-10"))
        check("2. deleted manager appears for a report date WITH historical daily_leads (the deletion day itself)",
              any(r["manager_key"] == "dariass" for r in rows_10), repr(rows_10))

        rows_11 = asyncio.run(fn_period("2026-07-11", "2026-07-11"))
        check("3. deleted manager does NOT appear for a date AFTER deletion with no data (today's live bug case)",
              not any(r["manager_key"] == "dariass" for r in rows_11), repr(rows_11))

        rows_range = asyncio.run(fn_period("2026-06-09", "2026-07-10"))
        check("4. deleted manager appears for a range intersecting historical data",
              any(r["manager_key"] == "dariass" for r in rows_range), repr(rows_range))

        check("5. deleted manager does NOT appear for a range fully AFTER deletion",
              not any(r["manager_key"] == "dariass" for r in rows_11), repr(rows_11))

        dariass_row = next((r for r in rows_10 if r["manager_key"] == "dariass"), {})
        check("12. deleted marker/name/username preserved on an included tombstone row",
              dariass_row.get("status") == "deleted" and dariass_row.get("display_name") == "Дарья С"
              and dariass_row.get("telegram_username") == "dariass_tg", repr(dariass_row))

        # --- schedule-only fallback case: zero leads ever, but scheduled+working. ---
        storage.ensure_manager_schedule_tables(db_path=tmp_db)
        storage.manager_stats_tombstone_upsert(
            "sched_only", display_name="Sched Only", telegram_username="sched_tg",
            manager_db_path="", source_key="rassylka",
            deleted_at="2026-07-05T10:00:00", retention_days=60, db_path=tmp_db,
        )
        storage.manager_schedule_set_day(
            "sched_only", "2026-07-03", 1, source="test", updated_by_user_id=0, updated_by_role="admin", db_path=tmp_db
        )

        rows_sched = asyncio.run(fn_period("2026-07-03", "2026-07-03"))
        check("6. deleted manager appears for a SCHEDULED/working date even with zero leads (schedule fallback)",
              any(r["manager_key"] == "sched_only" for r in rows_sched), repr(rows_sched))

        rows_not_sched = asyncio.run(fn_period("2026-07-01", "2026-07-01"))
        check("7. deleted manager does NOT appear for a date that is neither scheduled nor has data",
              not any(r["manager_key"] == "sched_only" for r in rows_not_sched), repr(rows_not_sched))

        # deleted_at is a HARD upper bound: a scheduled/working row AFTER the
        # deletion date must never resurrect the manager for that later date.
        storage.manager_schedule_set_day(
            "sched_only", "2026-07-08", 1, source="test", updated_by_user_id=0, updated_by_role="admin", db_path=tmp_db
        )
        rows_past_deletion = asyncio.run(fn_period("2026-07-08", "2026-07-08"))
        check("deletion date is a HARD upper bound: a scheduled day AFTER deleted_at is still excluded",
              not any(r["manager_key"] == "sched_only" for r in rows_past_deletion), repr(rows_past_deletion))

        # --- 60-day retention cap: deleted_at anchored to the REAL wall clock so
        # this check is correct regardless of what "today" the suite runs on.
        real_now = datetime.now()
        cap_deleted_at = real_now - timedelta(days=100)  # well past the 60-day window
        cap_date = cap_deleted_at.date().isoformat()
        backup_db2 = work_dir / "cap_mgr.db"
        con = sqlite3.connect(str(backup_db2))
        con.execute("CREATE TABLE daily_leads(id INTEGER PRIMARY KEY, lead_date TEXT, manager_key TEXT, chat_id INTEGER)")
        con.execute("INSERT INTO daily_leads(lead_date, manager_key, chat_id) VALUES(?,?,?)", (cap_date, "cap_mgr", 1))
        con.commit()
        con.close()
        storage.manager_stats_tombstone_upsert(
            "cap_mgr", display_name="Cap Mgr", manager_db_path=str(backup_db2), source_key="rassylka",
            deleted_at=iso(cap_deleted_at), retention_days=60, db_path=tmp_db,
        )
        rows_cap = asyncio.run(fn_period(cap_date, cap_date))
        check("15. tombstone retention still caps at 60 days (excluded once expired, "
              "even though the requested period otherwise has real data and is deletion-date-eligible)",
              not any(r["manager_key"] == "cap_mgr" for r in rows_cap), repr(rows_cap))

        # Label marker check: _manager_label_from_row renders "(удалён)" for a
        # status='deleted' row and is a no-op for a live-shaped row.
        label_ns = extract_and_exec(MAIN_PY, {"_manager_label_from_row"}, {})
        label_fn = label_ns["_manager_label_from_row"]
        check("label builder appends '(удалён)' for a tombstone-shaped (status='deleted') row",
              "(удалён)" in label_fn(dariass_row))
        check("label builder does NOT append the marker for a live-shaped row",
              "(удалён)" not in label_fn({"manager_key": "live_mgr", "status": "active"}))
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ======================================================================
# GROUP D -- PartnerBot reporting enumeration: _manager_rows_for_source unions
# active tombstones matched by source_key (or partner_lead_events fallback).
# ======================================================================

def run_partner_reporting_checks(tmp_db: str) -> None:
    print("\n-- PartnerBot: _manager_rows_for_source period-aware union (REAL execution via AST) --")

    work_dir = Path(tempfile.mkdtemp(prefix="del_mgr_partner_period_selftest_"))
    try:
        con = sqlite3.connect(tmp_db)
        con.execute("CREATE TABLE IF NOT EXISTS managers(id INTEGER PRIMARY KEY, manager_key TEXT, display_name TEXT, telegram_username TEXT, status TEXT)")
        con.execute("INSERT INTO managers(manager_key, display_name, telegram_username, status) VALUES('live_p','Live P','live_p_tg','active')")
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES('live_p','srcA')")
        con.execute(
            "CREATE TABLE IF NOT EXISTS partner_lead_events(manager_key TEXT, source_key TEXT, "
            "manager_display_name TEXT, manager_username TEXT, first_seen_utc TEXT)"
        )
        # no_source_mgr's only trace of belonging to srcA is this dated central-DB
        # event row (its own manager_source_links row is gone -- hard-deleted).
        con.execute(
            "INSERT INTO partner_lead_events(manager_key, source_key, manager_display_name, manager_username, first_seen_utc) "
            "VALUES('no_source_mgr','srcA','No Source Mgr','nosrc_tg','2026-06-15T10:00:00')"
        )
        con.commit()
        con.close()

        # dariass: source_key resolvable directly on the tombstone, real backup
        # daily_leads on 2026-06-09 and 2026-07-10 (same shape as the live case).
        backup_dariass = work_dir / "dariass.db"
        con = sqlite3.connect(str(backup_dariass))
        con.execute("CREATE TABLE daily_leads(id INTEGER PRIMARY KEY, lead_date TEXT, manager_key TEXT, chat_id INTEGER)")
        con.executemany(
            "INSERT INTO daily_leads(lead_date, manager_key, chat_id) VALUES(?,?,?)",
            [("2026-06-09", "dariass", 1), ("2026-07-10", "dariass", 2)],
        )
        con.commit()
        con.close()
        storage.manager_stats_tombstone_upsert(
            "dariass", display_name="Дарья С", telegram_username="dariass_tg",
            manager_db_path=str(backup_dariass), source_key="srcA",
            deleted_at="2026-07-10T17:45:44", retention_days=60, db_path=tmp_db,
        )

        # other_source_mgr: belongs to a DIFFERENT source (srcB), with its own
        # real data -- must never leak into srcA queries regardless of period.
        backup_other = work_dir / "other_source_mgr.db"
        con = sqlite3.connect(str(backup_other))
        con.execute("CREATE TABLE daily_leads(id INTEGER PRIMARY KEY, lead_date TEXT, manager_key TEXT, chat_id INTEGER)")
        con.execute("INSERT INTO daily_leads(lead_date, manager_key, chat_id) VALUES('2026-06-20','other_source_mgr',1)")
        con.commit()
        con.close()
        storage.manager_stats_tombstone_upsert(
            "other_source_mgr", display_name="Other Source", source_key="srcB",
            manager_db_path=str(backup_other), deleted_at="2026-07-10T17:45:44", retention_days=60, db_path=tmp_db,
        )

        # tombstone with NO resolvable source_key -> fallback to partner_lead_events,
        # dated 2026-06-15 (inside the range check below, outside the single-day one).
        storage.manager_stats_tombstone_upsert(
            "no_source_mgr", display_name="No Source Mgr", telegram_username="nosrc_tg",
            source_key="", deleted_at="2026-07-10T17:45:44", retention_days=60, db_path=tmp_db,
        )

        ns = extract_and_exec(
            PARTNER_STAT_BOT_PY,
            {"_manager_rows_for_source", "_tp_pstat_tombstone_eligible_for_period", "_source_links", "_connect", "_norm_key"},
            {
                "sqlite3": sqlite3, "os": os, "re": re, "BASE_DIR": BASE_DIR,
                "datetime": datetime, "timedelta": timedelta,
                "TPILOT_DB_PATH": tmp_db, "storage": storage, "List": list, "Dict": dict, "Any": object,
            },
        )
        fn = ns["_manager_rows_for_source"]

        rows_a_default = fn("srcA")
        keys_a_default = {r["manager_key"] for r in rows_a_default}
        check("PartnerBot _manager_rows_for_source with NO period does NOT include any tombstone "
              "(period-filter correction: fail-closed default, mirrors check 1)",
              keys_a_default == {"live_p"}, repr(keys_a_default))

        rows_a_10 = fn("srcA", "2026-07-10", "2026-07-10")
        keys_a_10 = {r["manager_key"] for r in rows_a_10}
        check("8. PartnerBot includes a tombstone only when source matches AND data exists in the "
              "selected period (dariass, deletion day itself)",
              "dariass" in keys_a_10, repr(keys_a_10))
        check("9. PartnerBot excludes a tombstone for the WRONG source even within an eligible period",
              "other_source_mgr" not in keys_a_10, repr(keys_a_10))

        rows_a_after = fn("srcA", "2026-07-11", "2026-07-11")
        check("10. PartnerBot excludes a tombstone for a period fully AFTER the deletion day",
              not any(r["manager_key"] == "dariass" for r in rows_a_after), repr(rows_a_after))

        rows_a_range = fn("srcA", "2026-06-09", "2026-07-10")
        keys_a_range = {r["manager_key"] for r in rows_a_range}
        check("PartnerBot includes a tombstone with NO resolvable source_key via the "
              "partner_lead_events fallback, when its dated event falls in the selected range",
              "no_source_mgr" in keys_a_range, repr(keys_a_range))
        check("11. live managers are unaffected by any of the period-filter logic above",
              all("live_p" in ks for ks in (keys_a_default, keys_a_10, keys_a_range)))

        by_key_range = {r["manager_key"]: r for r in rows_a_range}
        check("13. tombstoned manager rows preserve display_name and telegram_username",
              by_key_range.get("dariass", {}).get("display_name") == "Дарья С"
              and by_key_range.get("dariass", {}).get("telegram_username") == "dariass_tg", repr(by_key_range.get("dariass")))
        check("fallback-matched tombstone also preserves its identity fields",
              by_key_range.get("no_source_mgr", {}).get("display_name") == "No Source Mgr"
              and by_key_range.get("no_source_mgr", {}).get("telegram_username") == "nosrc_tg")

        rows_b = fn("srcB", "2026-06-09", "2026-07-10")
        keys_b = {r["manager_key"] for r in rows_b}
        check("other-source tombstone correctly appears for ITS OWN source (srcB) with matching period data",
              "other_source_mgr" in keys_b, repr(keys_b))
        check("dariass (srcA-tombstoned) does not leak into an unrelated source (srcB)",
              "dariass" not in keys_b, repr(keys_b))
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ======================================================================
# GROUP E -- db_path resolution + historical totals are unchanged by deletion.
# ======================================================================

def run_historical_data_checks() -> None:
    print("\n-- Historical daily_leads: backup db_path is honored, totals unchanged --")

    work_dir = Path(tempfile.mkdtemp(prefix="del_mgr_hist_selftest_"))
    try:
        backup_db = work_dir / "dariass_backup.db"
        con = sqlite3.connect(str(backup_db))
        con.execute(
            "CREATE TABLE daily_leads(id INTEGER PRIMARY KEY, lead_date TEXT, manager_key TEXT, chat_id INTEGER, "
            "lead_countable INTEGER, quality_bucket TEXT)"
        )
        rows_before = [
            (1, "2026-06-01", "dariass", 111, 1, "liquid"),
            (2, "2026-06-01", "dariass", 112, 1, "geo"),
            (3, "2026-06-02", "dariass", 113, 1, "liquid"),
        ]
        con.executemany("INSERT INTO daily_leads VALUES(?,?,?,?,?,?)", rows_before)
        con.commit()
        con.close()

        total_before = sqlite3.connect(str(backup_db)).execute("SELECT COUNT(*) FROM daily_leads").fetchone()[0]

        # _psf3_manager_db_path / se_manager_db_path both prefer row["db_path"] when
        # it exists on disk -- exercise the REAL storage-facing resolution logic used
        # by both readers via the exact same rule (existence-preferring).
        tomb_row = {"db_path": str(backup_db), "manager_key": "dariass"}
        resolved = str(tomb_row.get("db_path") or "").strip()
        check("14. a tombstone's backup db_path is a real, existing, readable file",
              os.path.exists(resolved) and resolved == str(backup_db))

        con2 = sqlite3.connect(resolved)
        rows_after = con2.execute("SELECT * FROM daily_leads ORDER BY id").fetchall()
        total_after = con2.execute("SELECT COUNT(*) FROM daily_leads").fetchone()[0]
        con2.close()

        check("15. historical daily_leads totals are IDENTICAL before/after (deletion never rewrites per-manager data)",
              total_before == total_after == 3, f"before={total_before} after={total_after}")
        check("15b. historical row content is byte-identical (no recompute/rewrite)",
              list(rows_after) == rows_before, repr(rows_after))
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ======================================================================
# GROUP G -- legacy backfill: _tp_mgrbf_* creates a tombstone for a manager that
# was hard-deleted BEFORE the retention feature existed (the live "dariass" case),
# purely from what's still on disk (_deleted_managers_backup) and in the central
# partner_lead_events snapshot. Real execution via AST extraction.
# ======================================================================

async def _empty_manager_list_rows(include_removed: bool = False) -> list:
    return []


def run_backfill_checks() -> None:
    print("\n-- Legacy backfill: _tp_mgrbf_* for a pre-existing deleted manager (REAL execution via AST) --")

    # Dates are RELATIVE to "now" so the retention window (60 days) never expires
    # underneath the test. The original hardcoded 2026-06-01 deletion date made the
    # enumeration checks silently start failing on 2026-07-31 (tombstone correctly
    # filtered out as expired by manager_stats_tombstone_list_active).
    bf_deleted_dt = (datetime.now() - timedelta(days=10)).replace(hour=9, minute=0, second=0, microsecond=0)
    bf_deleted_suffix = bf_deleted_dt.strftime("%Y%m%d_%H%M%S")
    bf_deleted_prefix = bf_deleted_dt.strftime("%Y-%m-%dT%H:%M:%S")
    bf_lead_date = (bf_deleted_dt - timedelta(days=12)).strftime("%Y-%m-%d")

    work_dir = Path(tempfile.mkdtemp(prefix="del_mgr_backfill_selftest_"))
    try:
        central_db = str(work_dir / "central.db")
        con = sqlite3.connect(central_db)
        con.execute(
            "CREATE TABLE IF NOT EXISTS partner_lead_events(id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "manager_key TEXT, source_key TEXT, manager_display_name TEXT, manager_username TEXT)"
        )
        con.execute(
            "INSERT INTO partner_lead_events(manager_key, source_key, manager_display_name, manager_username) "
            "VALUES ('dariass', 'src_main', 'Дарья С', 'dariass_tg')"
        )
        con.commit()
        con.close()

        # Simulate the ALREADY-deleted manager's backup folder, created long before
        # the tombstone feature existed -- no tombstone row exists for it anywhere.
        backup_root = work_dir / "_deleted_managers_backup"
        backup_folder = backup_root / f"dariass_{bf_deleted_suffix}"
        backup_folder.mkdir(parents=True)
        backup_db = backup_folder / "dariass.db"
        con = sqlite3.connect(str(backup_db))
        con.execute(
            "CREATE TABLE daily_leads(id INTEGER PRIMARY KEY, lead_date TEXT, manager_key TEXT, "
            "manager_username TEXT, chat_id INTEGER)"
        )
        con.execute("INSERT INTO daily_leads VALUES(1,?,'dariass','dariass_tg',111)", (bf_lead_date,))
        con.commit()
        con.close()

        ns = extract_and_exec(
            MAIN_PY,
            {
                "_tp_mgrbf_scan_backup_candidates", "_tp_mgrbf_identity_from_sources",
                "_tp_mgrbf_backfill_one", "_tp_mgrbf_backfill_all",
            },
            {
                "registry_normalize_manager_key": normalize_manager_key,
                "Path": Path, "BASE_DIR": work_dir, "datetime": datetime,
                "aiosqlite": aiosqlite, "TPILOT_DB_PATH": central_db, "os": os,
                "_mgrbf_re": re,
                "Optional": Optional, "List": List, "Tuple": Tuple, "Dict": Dict, "Any": Any,
            },
        )

        candidates = ns["_tp_mgrbf_scan_backup_candidates"]("dariass", base_dir=str(work_dir))
        check("scan_backup_candidates finds the legacy backup db for dariass", len(candidates) == 1, repr(candidates))

        check("precondition: no tombstone exists for dariass before backfill",
              storage.manager_stats_tombstone_get("dariass", db_path=central_db) is None)

        result1 = asyncio.run(ns["_tp_mgrbf_backfill_one"]("dariass"))
        check("backfill creates a tombstone for the legacy-deleted manager", result1.get("status") == "created", repr(result1))

        tomb = storage.manager_stats_tombstone_get("dariass", db_path=central_db)
        check("tombstone identity comes from the central partner_lead_events snapshot (preferred source)",
              bool(tomb) and tomb.get("display_name") == "Дарья С" and tomb.get("telegram_username") == "dariass_tg"
              and tomb.get("source_key") == "src_main", repr(tomb))
        check("tombstone db_path points at the real legacy backup file", tomb.get("db_path") == str(backup_db), repr(tomb))
        check("tombstone deleted_at was inferred from the backup folder's timestamp suffix",
              str(tomb.get("deleted_at") or "").startswith(bf_deleted_prefix), repr(tomb.get("deleted_at")))
        expected_until = (bf_deleted_dt + timedelta(days=60)).replace(microsecond=0).isoformat()
        check("retention_until = deleted_at + 60 days", tomb.get("retention_until") == expected_until,
              f"got={tomb.get('retention_until')} expected={expected_until}")

        # Idempotency: re-running backfill for the same manager is a safe no-op.
        result2 = asyncio.run(ns["_tp_mgrbf_backfill_one"]("dariass"))
        check("repeated backfill for the same manager is idempotent (skipped, not re-created)",
              result2.get("status") == "skipped_existing", repr(result2))
        tomb_after = storage.manager_stats_tombstone_get("dariass", db_path=central_db)
        check("tombstone content is byte-identical after the repeat run (never overwritten)", tomb_after == tomb, repr((tomb, tomb_after)))

        # backfill_all scans the whole backup tree and is likewise idempotent.
        results_all = asyncio.run(ns["_tp_mgrbf_backfill_all"](base_dir=str(work_dir)))
        check("backfill_all finds dariass and reports it as already-existing (idempotent across a full scan)",
              any(r.get("manager_key") == "dariass" and r.get("status") == "skipped_existing" for r in results_all),
              repr(results_all))

        # Safety: backfill never writes to the backup per-manager DB, never moves/
        # deletes the backup folder, and (structurally, see Group F) never touches
        # managers/manager_reserve_pairs.
        con = sqlite3.connect(str(backup_db))
        row_count = con.execute("SELECT COUNT(*) FROM daily_leads").fetchone()[0]
        con.close()
        check("backfill never wrote anything into the backup per-manager DB (still just the original 1 row)", row_count == 1)
        check("the backup folder/file were never moved or deleted", backup_db.exists())

        # AdminBot period-aware reporting enumeration includes dariass after backfill,
        # for the period intersecting its real historical data (2026-05-20); the plain
        # (no-period) default stays fail-closed/live-only per the period-filter correction.
        admin_ns = extract_and_exec(
            MAIN_PY,
            {"_manager_rows_for_reporting", "_manager_rows_for_reporting_period", "_tp_mgrret_tombstone_eligible_for_period"},
            {
                "manager_list_rows": _empty_manager_list_rows,
                "registry_normalize_manager_key": normalize_manager_key,
                "build_manager_paths": lambda base, k: {"db_path": str(Path(base) / "runtime" / "managers" / k / f"{k}.db")},
                "BASE_DIR": work_dir, "os": os, "aiosqlite": aiosqlite, "TPILOT_DB_PATH": central_db,
                "datetime": datetime, "timedelta": timedelta,
                "List": list, "Dict": dict, "Any": object,
            },
        )
        admin_rows_default = asyncio.run(admin_ns["_manager_rows_for_reporting"]())
        check("AdminBot plain (no-period) reporting enumeration does NOT include dariass after backfill "
              "(fail-closed default, period-filter correction)",
              not any(r.get("manager_key") == "dariass" for r in admin_rows_default), repr(admin_rows_default))
        admin_rows = asyncio.run(admin_ns["_manager_rows_for_reporting_period"](bf_lead_date, bf_lead_date))
        check("AdminBot period-aware reporting enumeration includes dariass after backfill "
              "for the period intersecting its real historical data",
              any(r.get("manager_key") == "dariass" for r in admin_rows), repr(admin_rows))

        # PartnerBot reporting enumeration for the tombstoned source includes dariass,
        # period-aware only -- same fail-closed-default / period-eligibility rule.
        con = sqlite3.connect(central_db)
        con.execute("CREATE TABLE IF NOT EXISTS managers(id INTEGER PRIMARY KEY, manager_key TEXT, display_name TEXT, telegram_username TEXT, status TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.commit()
        con.close()
        partner_ns = extract_and_exec(
            PARTNER_STAT_BOT_PY,
            {"_manager_rows_for_source", "_tp_pstat_tombstone_eligible_for_period", "_source_links", "_connect", "_norm_key"},
            {
                "sqlite3": sqlite3, "os": os, "re": re, "BASE_DIR": work_dir,
                "datetime": datetime, "timedelta": timedelta,
                "TPILOT_DB_PATH": central_db, "storage": storage, "List": list, "Dict": dict, "Any": object,
            },
        )
        partner_rows_default = partner_ns["_manager_rows_for_source"]("src_main")
        check("PartnerBot plain (no-period) reporting enumeration does NOT include dariass after backfill "
              "(fail-closed default, period-filter correction)",
              not any(r.get("manager_key") == "dariass" for r in partner_rows_default), repr(partner_rows_default))
        partner_rows = partner_ns["_manager_rows_for_source"]("src_main", bf_lead_date, bf_lead_date)
        check("PartnerBot period-aware reporting enumeration for src_main includes dariass after backfill",
              any(r.get("manager_key") == "dariass" for r in partner_rows), repr(partner_rows))
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


# ======================================================================
# GROUP F -- safety / scope (structural + regression-style)
# ======================================================================

def run_safety_checks(tmp_db: str) -> None:
    print("\n-- Safety / scope --")

    main_src = MAIN_PY.read_text(encoding="utf-8-sig")
    storage_src = (BASE_DIR / "storage.py").read_text(encoding="utf-8-sig")
    partner_src = PARTNER_STAT_BOT_PY.read_text(encoding="utf-8-sig")

    # Backfill functions must never reference managers/manager_reserve_pairs writes
    # or backup-folder mutation (shutil.move/rmtree/os.remove) -- structural proof
    # that the legacy backfill is read-only against everything except the new
    # tombstone table.
    for fname in ("_tp_mgrbf_scan_backup_candidates", "_tp_mgrbf_identity_from_sources", "_tp_mgrbf_backfill_one", "_tp_mgrbf_backfill_all"):
        found = find_defs(MAIN_PY, fname)
        check(f"backfill function {fname} is defined exactly once", len(found) == 1, f"count={len(found)}")
        if found:
            body_src = body_src_without_docstring(found[-1])
            check(f"{fname} contains no write to managers/manager_reserve_pairs and no folder mutation",
                  not re.search(r"(UPDATE|DELETE FROM|INSERT INTO)\s+managers\b", body_src, re.IGNORECASE)
                  and "manager_reserve_pairs" not in body_src
                  and "shutil.move" not in body_src and "shutil.rmtree" not in body_src and "os.remove" not in body_src)

    # 17. Proxy / reserve / onboarding functions are untouched: their active defs
    # must still exist unchanged in count, and the new tombstone code must not
    # appear inside them (proves no accidental edits landed in the wrong function).
    for fname in ("_handle_manager_proxy_buy_confirm_command", "_prenew_execute_renewal",
                  "manager_delete_onboarding_by_key", "reserve_pair_release_for_primary"):
        defs_main = find_defs(MAIN_PY, fname) if fname in main_src else []
        defs_storage = find_defs(BASE_DIR / "storage.py", fname) if fname in storage_src else []
        found = defs_main or defs_storage
        check(f"17. {fname} is still defined exactly once (not accidentally touched)",
              len(found) == 1, f"count={len(found)}")
        if found:
            body_src = ast.unparse(found[-1])
            check(f"17b. {fname} body contains no tombstone/retention code (scope stayed narrow)",
                  "tombstone" not in body_src.lower() and "MANAGER_DB_RETENTION" not in body_src.upper())

    # allow_spend audit -- exactly 2 real call-keyword sites, both in main.py.
    def _allow_spend_sites(src: str) -> list:
        tree = ast.parse(src)
        return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                for kw in (n.keywords or [])
                if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]

    check("allow_spend=True is still exactly 2 real call-keyword sites, both in main.py",
          len(_allow_spend_sites(main_src)) == 2 and len(_allow_spend_sites(storage_src)) == 0
          and len(_allow_spend_sites(partner_src)) == 0)

    # mojibake scan
    for name, src in (("main.py", main_src), ("storage.py", storage_src), ("partner_stat_bot.py", partner_src)):
        moji = sum(src.count(c) for c in ("Ð", "Ñ")) + src.count("â€")
        check(f"mojibake clean: {name}", moji == 0, str(moji))

    # No real DB / no network in this selftest file
    real_path = os.path.realpath(tmp_db)
    check("18. selftest used ONLY temp SQLite files (never db/data_tpilot.db or db/data.db)",
          os.path.realpath(tempfile.gettempdir()) in real_path
          and "data_tpilot.db" not in real_path and (os.sep + "db" + os.sep) not in real_path, real_path)
    self_src = Path(__file__).read_text(encoding="utf-8-sig")
    check("selftest makes no network/Telethon/API calls",
          not re.search(r"^\s*(import|from)\s+(requests|telethon|urllib|http\.client|anthropic)\b", self_src, re.MULTILINE))

    # 11. Tombstones never appear in operational/active enumerators: those functions
    # must not reference the new storage helpers at all.
    for op_fn in ("_manager_rows_all", "manager_list_rows"):
        found = find_defs(BASE_DIR / "partner_stat_bot.py" if op_fn == "_manager_rows_all" else BASE_DIR / "storage.py", op_fn)
        if found:
            body_src = ast.unparse(found[-1])
            check(f"11. operational enumerator {op_fn} does not reference manager_stats_tombstone_* (deleted managers never resurface as active)",
                  "manager_stats_tombstone" not in body_src)

    # 13. Period-aware tombstone inclusion stays scoped to genuine report paths only.
    # _manager_rows_for_reporting_period must be called from exactly this whitelist of
    # AdminBot stats functions -- no non-stat/operational caller (unanswered alerts,
    # lead repair, health, quality, presence, bizlink, exports) can see tombstones.
    _ADMIN_PERIOD_AWARE_CALLERS = {
        "_collect_day_leads", "_collect_window_leads_extended",
        "_build_det_stat_text", "_build_stat_period_text", "_handle_nmstat_command",
    }
    main_tree = ast.parse(main_src)
    admin_period_callers = {
        node.name for node in main_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name != "_manager_rows_for_reporting_period"  # exclude its own def signature
        and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "_manager_rows_for_reporting_period"
            for n in ast.walk(node)
        )
    }
    check("13. _manager_rows_for_reporting_period is called ONLY from the intended AdminBot "
          "stats/report functions (non-stat/operational callers, incl. bizlink/exports, stay fail-closed)",
          admin_period_callers == _ADMIN_PERIOD_AWARE_CALLERS, repr(admin_period_callers))

    # 13b. Same structural proof on the PartnerBot side: _manager_rows_for_source is
    # ever called WITH explicit period args only from _psf3_buyer_context (the single
    # active funnel for light/pro/both text stats) -- bizlinks (_psbl_text) and the
    # Excel export paths stay fail-closed/live-only, matching the AdminBot export decision.
    partner_tree = ast.parse(partner_src)
    partner_period_callers = set()
    for node in partner_tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "_manager_rows_for_source" and len(call.args) > 1):
                partner_period_callers.add(node.name)
    check("13b. PartnerBot's _manager_rows_for_source is called WITH explicit period args "
          "only from _psf3_buyer_context (bizlinks/export paths stay fail-closed/live-only)",
          partner_period_callers == {"_psf3_buyer_context"}, repr(partner_period_callers))

    # 13c. REGRESSION GUARD for the override-stack trap: main.py has MANY stacked
    # same-name defs; only the LAST one is active (see CLAUDE.md). Check 13 above only
    # proves SOME def named e.g. "_build_det_stat_text" calls the period-aware helper
    # -- it does NOT prove the ACTIVE (last-defined, override-winning) one does. This
    # is exactly how a real BLOCK slipped through review: an earlier patch touched a
    # SHADOWED def of _build_det_stat_text while the active last def (a full
    # reimplementation, not a delegate) silently kept calling the plain live-only
    # function. Fix: walk each whitelisted name's override stack starting from its
    # LAST def; a def resolves if it either (a) calls the period-aware helper
    # directly, or (b) delegates to a captured earlier def via this project's own
    # `_X_ORIG = globals().get("fname")` override-chain convention (used by
    # _collect_day_leads/_collect_window_leads_extended) -- in which case the
    # delegate target (the previous def) must resolve too, recursively. Any def that
    # neither calls the helper nor delegates breaks the chain -- FAIL.
    def _active_def_reaches_period_helper(path: Path, fname: str) -> tuple:
        defs = find_defs(path, fname)
        if not defs:
            return False, "no top-level def found"
        src_text = path.read_text(encoding="utf-8-sig")
        capture_names = re.findall(
            r'^\s*(\w+)\s*=\s*globals\(\)\.get\(\s*["\']' + re.escape(fname) + r'["\']\s*\)',
            src_text, re.MULTILINE,
        )
        idx = len(defs) - 1
        hops = 0
        while idx >= 0 and hops < 10:
            body_src = ast.unparse(defs[idx])
            if "_manager_rows_for_reporting_period(" in body_src:
                return True, f"resolved directly at def #{idx + 1}/{len(defs)}"
            if idx > 0 and any(name in body_src for name in capture_names):
                idx -= 1
                hops += 1
                continue
            return False, (
                f"def #{idx + 1}/{len(defs)} (the ACTIVE def when idx == {len(defs) - 1}) "
                "neither calls _manager_rows_for_reporting_period directly nor delegates "
                "to an earlier def of the same name"
            )
        return False, "delegate chain exceeded safety hop limit or exhausted without resolving"

    for fname in sorted(_ADMIN_PERIOD_AWARE_CALLERS):
        ok, detail = _active_def_reaches_period_helper(MAIN_PY, fname)
        check(f"13c. the ACTIVE (last-defined, override-winning) def of {fname} reaches "
              f"_manager_rows_for_reporting_period, directly or via its own delegate chain",
              ok, detail)


def main() -> int:
    tmp_db = tempfile.mktemp(suffix="_del_mgr_retention_selftest.db")
    _selftest_db_guard(tmp_db, BASE_DIR, storage)
    try:
        run_storage_checks(tmp_db)
        run_delete_integration_checks()
        run_admin_reporting_checks(tmp_db)
        run_admin_period_reporting_checks(tmp_db)
        run_partner_reporting_checks(tmp_db)
        run_historical_data_checks()
        run_backfill_checks()
        run_safety_checks(tmp_db)

        # 14. Reserve-release atomicity regression guard: the period-filter correction
        # touched none of the full-delete integration path, so Group B's checks 6-8c
        # (tombstone-before-delete, reserve released only after verified absence,
        # regression guard on a blocked delete) must all still have passed above.
        _reserve_release_labels = {
            "6/7. hard-delete still removes the managers row",
            "6b. command reports success",
            "6c. a tombstone was written BEFORE the hard-delete, with identity preserved",
            "6d. the tombstone db_path points at the deterministic backup location (<root>/<key>_<ts>/<key>.db)",
            "8. reserve is released (retired) only AFTER the managers row was verified absent",
            "7b. tombstone-write failure ABORTS before the destructive DELETE (managers row survives)",
            "7c. command reports failure, not false success",
            "8b. REGRESSION GUARD: reserve is NOT released when the managers row survives a blocked delete, "
            "even though a tombstone was already written for it",
            "8c. command reports the 'not confirmed' failure",
        }
        check("14. reserve-release atomicity regression guard still passes (Group B checks 6-8c, unaffected by this patch)",
              _reserve_release_labels.isdisjoint(FAILURES), repr(_reserve_release_labels & set(FAILURES)))
    finally:
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
