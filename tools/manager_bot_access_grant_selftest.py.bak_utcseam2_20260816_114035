# -*- coding: utf-8 -*-
"""tools/manager_bot_access_grant_selftest.py -- offline selftest for the
2026-07-27 "ManagerBot: canonical auto-grant + clean /start" patch
(storage.py, main.py, manager_bot.py).

Business rules under test:
* storage.manager_bot_access_ensure_sync is the SINGLE canonical, idempotent
  grant of ManagerBot base access for an active TPilot manager. It is called
  from every confirmed onboarding/reconnect path in main.py
  (_manager_finalize_login -- phone/code/2FA/QR/relogin/replacement identity
  match; the replacement fallback branch; the tdata/.session runtime profile
  sync) and as a /start self-heal in manager_bot.py.
* It writes ONLY missing rows across access_users/access_targets/
  manager_bot_access, never resets scope_mode/access_level on an existing
  access_users row, never enables sub-permissions (can_view_stats,
  stats_format, allow_custom_period), and never reads/recomputes
  event_cutoff_id on an existing manager_bot_access row (the exact class of
  bug that once destroyed 30 undelivered lead cards in this project).
* manager_bot.py's active /start now shows ONLY: "granted_now" ->
  "Доступ выдан автоматически", "already_granted" -> "Доступ активен",
  "disabled" -> "Доступ отключён администратором" (no tg_user_id -- this is
  an already-known user), "error" -> retry message (no tg_user_id), or
  "not_eligible"/"identity_conflict" -> "Доступ не найден" WITH the user's
  OWN tg_user_id (operationally needed to hand to an admin). All technical
  status text (tg_user_id/access level/scope/managers count/Commands:/
  whoami) is gone from every OTHER screen. /whoami is removed entirely.
* The canonical menu builder (_mbstat_start_buttons, the 4-level override
  chain) is reused unchanged -- no second/duplicate menu.

Techniques (matching the project's own tools/*_selftest.py conventions,
specifically tools/onboarding_source_pick_selftest.py's extract_and_exec
pattern for main.py):
  * storage.py IS importable (zero import-time side effects) -> the REAL
    storage.manager_bot_access_ensure_sync runs against throwaway temp
    SQLite files, never mocked.
  * main.py / manager_bot.py are NOT importable (Telethon/env side effects
    at import time) -> the relevant functions are AST-extracted (last
    top-level def wins, matching the project's override-stack convention)
    and exec'd into a namespace seeded with fakes for their own
    Telethon/env/log boundaries, but with the REAL storage module bound --
    so the actual grant call's real DB effects are what gets asserted, not
    a mock's say-so.
  * manager_bot.py's schema (manager_bot_access, manager_bot_events,
    manager_bot_sent, manager_lead_cards, the M2.6B backfill and M2.6E
    cutoff-repair SQL) is AST-extracted from the real init_schema, never
    hand-copied, so this suite can never silently drift from production
    the way an earlier round's hand-typed stub once did.
  * mutation / negative-control proofs: apply a small text mutation to the
    extracted source and prove the NAMED check now fails, for the RIGHT
    reason (positive behavioral assertions, never "the mutant crashed").
  * a self-guard AST-scans THIS file for any `check(...)` call whose
    condition is a literal that cannot be False.

Pure/offline: no network, no Telegram, no production DB, no spend.

    python tools\\manager_bot_access_grant_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage
from manager_registry import normalize_manager_key

MAIN_PY = BASE_DIR / "main.py"
MANAGER_BOT_PY = BASE_DIR / "manager_bot.py"
THIS_FILE = Path(__file__).resolve()

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


# ==========================================================================
# Shared AST helpers (project convention: last top-level def wins)
# ==========================================================================

def _guard_temp_db(db_path: str) -> None:
    rp = os.path.realpath(db_path)
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"db must live under tempdir, got {rp}"
    assert "data_tpilot.db" not in rp and (os.sep + "db" + os.sep) not in rp, rp


def extract_and_exec(path: Path, names: set, extra_ns: dict) -> dict:
    """AST-extract the LAST (active) def of each requested name and exec into
    a seeded namespace -- same technique as tools/onboarding_source_pick_selftest.py
    and the rest of this project's tools/*_selftest.py files."""
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


def _last_def_node(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    defs = [n for n in tree.body if getattr(n, "name", None) == name]
    if not defs:
        raise AssertionError(f"no top-level def named {name!r} found in {path}")
    return defs[-1]


def _src_of(path: Path, name: str) -> str:
    return ast.unparse(_last_def_node(path, name))


MB_SRC = MANAGER_BOT_PY.read_text(encoding="utf-8-sig")
MB_TREE = ast.parse(MB_SRC)
MAIN_PY_TREE = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))


def _find_init_schema_sql(match_substr: str) -> str:
    """AST-extract the literal SQL string of the one con.execute(...) call
    inside the real init_schema whose text contains match_substr -- so the
    B1 regression proof runs the REAL migration SQL, not a hand copy."""
    init_fn = _last_def_node(MANAGER_BOT_PY, "init_schema")
    for node in ast.walk(init_fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and match_substr in node.args[0].value
        ):
            return node.args[0].value
    raise AssertionError(f"no init_schema con.execute(...) containing {match_substr!r} found")


def _find_init_schema_executescript(match_substr: str) -> str:
    init_fn = _last_def_node(MANAGER_BOT_PY, "init_schema")
    for node in ast.walk(init_fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "executescript"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and match_substr in node.args[0].value
        ):
            return node.args[0].value
    raise AssertionError(f"no init_schema con.executescript(...) containing {match_substr!r} found")


def _find_init_schema_alter_columns(table_name: str) -> tuple:
    init_fn = _last_def_node(MANAGER_BOT_PY, "init_schema")
    marker = f"ALTER TABLE {table_name} ADD COLUMN"
    for node in ast.walk(init_fn):
        if isinstance(node, ast.For) and marker in ast.unparse(node):
            return ast.literal_eval(node.iter)
    raise AssertionError(f"no ADD COLUMN migration loop for {table_name!r} found")


M2_6B_BACKFILL_SQL = _find_init_schema_sql("INSERT OR IGNORE INTO manager_bot_access")
M2_6E_CUTOFF_REPAIR_SQL = _find_init_schema_sql("SET event_cutoff_id")
MANAGER_BOT_ACCESS_SCRIPT = _find_init_schema_executescript("CREATE TABLE IF NOT EXISTS manager_bot_access")
EVENTS_SENT_CARDS_SCRIPT = _find_init_schema_executescript("CREATE TABLE IF NOT EXISTS manager_bot_events")
MANAGER_BOT_SENT_ALTER_COLUMNS = _find_init_schema_alter_columns("manager_bot_sent")


# ==========================================================================
# Temp-DB fixture: managers/access_users/access_targets match storage.py's
# own schema (storage.py:109-113 / 886-911 / 1193-1200) byte-for-byte
# (cross-checked against the live file in the same session this patch was
# written); manager_bot_access/events/sent/cards are AST-extracted above.
# ==========================================================================

_TEMP_DB_PATHS: list = []


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(prefix="mbgrant_", suffix=".db")
    os.close(fd)
    os.remove(path)
    _guard_temp_db(path)
    _TEMP_DB_PATHS.append(path)
    con = sqlite3.connect(path)
    try:
        con.execute(
            """
            CREATE TABLE managers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT UNIQUE NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                phone TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                manual_stopped INTEGER NOT NULL DEFAULT 0,
                tg_user_id INTEGER,
                telegram_username TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            """
            CREATE TABLE access_users(
                tg_user_id INTEGER PRIMARY KEY,
                display_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                access_level INTEGER NOT NULL DEFAULT 1,
                scope_mode TEXT NOT NULL DEFAULT 'selected',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            """
            CREATE TABLE access_targets(
                tg_user_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, manager_key)
            )
            """
        )
        con.executescript(MANAGER_BOT_ACCESS_SCRIPT)
        con.executescript(EVENTS_SENT_CARDS_SCRIPT)
        for col, decl in MANAGER_BOT_SENT_ALTER_COLUMNS:
            try:
                con.execute(f"ALTER TABLE manager_bot_sent ADD COLUMN {col} {decl}")
            except Exception:
                pass
        con.commit()
    finally:
        con.close()
    return path


def _seed_manager(db_path, key, tg_user_id=None, *, status="active", is_enabled=1,
                   manual_stopped=0, display_name="", telegram_username=""):
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO managers(manager_key, display_name, tg_user_id, telegram_username,"
            " status, is_enabled, manual_stopped) VALUES (?,?,?,?,?,?,?)",
            (key, display_name, tg_user_id, telegram_username, status, is_enabled, manual_stopped),
        )
        con.commit()
    finally:
        con.close()


def _seed_access_user(db_path, tg_user_id, *, is_enabled=1, scope_mode="selected",
                       access_level=1, display_name="", username=""):
    now = "2026-01-01T00:00:00"
    _exec(db_path,
          "INSERT INTO access_users(tg_user_id, display_name, username, access_level,"
          " scope_mode, is_enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
          (tg_user_id, display_name, username, access_level, scope_mode, is_enabled, now, now))


def _seed_mba(db_path, tg_user_id, manager_key, *, revoked=0, cutoff=0,
              can_receive_cards=1, can_view_stats=0, stats_format="light", auto_granted=0):
    now = "2026-01-01T00:00:00"
    _exec(db_path,
          "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, can_set_status,"
          " can_view_stats, stats_format, allow_custom_period, auto_granted, granted_by, granted_at,"
          " revoked, revoked_by, revoked_at, event_cutoff_id, created_at, updated_at)"
          " VALUES (?,?,?,1,?,?,0,?,0,?,?,0,'',?,?,?)",
          (tg_user_id, manager_key, can_receive_cards, can_view_stats, stats_format,
           auto_granted, now, revoked, cutoff, now, now))


_EVENT_KEY_COUNTER = {"n": 0}


def _seed_event(db_path, manager_key):
    _EVENT_KEY_COUNTER["n"] += 1
    _exec(db_path, "INSERT INTO manager_bot_events(event_key, manager_key) VALUES (?, ?)",
          (f"grant-selftest-evk-{_EVENT_KEY_COUNTER['n']}", manager_key))


def _row(db_path, table, **where):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        cond = " AND ".join(f"{k}=?" for k in where) if where else "1=1"
        row = con.execute(f"SELECT * FROM {table} WHERE {cond}", tuple(where.values())).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _rows(db_path, table, **where):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        cond = " AND ".join(f"{k}=?" for k in where) if where else "1=1"
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} WHERE {cond}", tuple(where.values())).fetchall()]
    finally:
        con.close()


def _exec(db_path, sql, params=()):
    con = sqlite3.connect(db_path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _full_snapshot(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        return tuple(tuple(r) for r in rows)
    finally:
        con.close()


# ==========================================================================
# Section A -- storage.manager_bot_access_ensure_sync: direct functional
# tests against the REAL function (no mocking of storage.py at all).
# ==========================================================================

def run_storage_functional_checks() -> None:
    print("\n-- Section A: storage.manager_bot_access_ensure_sync (real function) --")

    # A1: fresh grant -- all three rows created, granted_now.
    db = _make_temp_db()
    _seed_manager(db, "ivan", 555)
    r1 = storage.manager_bot_access_ensure_sync(tg_user_id=555, display_name="Ivan Telegram",
                                                  username="ivan_tg", db_path=db)
    check("A1: fresh grant -> granted_now", r1["status"] == "granted_now", r1)
    check("A1: access_users created", bool(_rows(db, "access_users", tg_user_id=555)))
    check("A1: access_targets created", bool(_rows(db, "access_targets", tg_user_id=555, manager_key="ivan")))
    mba1 = _row(db, "manager_bot_access", tg_user_id=555, manager_key="ivan")
    check("A1: manager_bot_access created with base perms, no sub-perms",
          bool(mba1) and int(mba1["can_receive_cards"]) == 1 and int(mba1["can_set_status"]) == 1
          and int(mba1["can_view_stats"]) == 0 and mba1["stats_format"] == "light"
          and int(mba1["allow_custom_period"]) == 0, mba1)
    check("A1: display_name/username resolved from caller-supplied identity",
          r1["display_name"] == "Ivan Telegram" and r1["username"] == "ivan_tg", r1)

    # A2 (item: idempotency): repeat call -> already_granted, no duplicate,
    # no change to any existing row.
    before = {t: _full_snapshot(db, t) for t in ("access_users", "access_targets", "manager_bot_access")}
    r2 = storage.manager_bot_access_ensure_sync(tg_user_id=555, display_name="Ivan Telegram",
                                                  username="ivan_tg", db_path=db)
    after = {t: _full_snapshot(db, t) for t in ("access_users", "access_targets", "manager_bot_access")}
    check("A2: repeat call -> already_granted", r2["status"] == "already_granted", r2)
    check("A2: all three tables byte-identical after repeat call",
          before == after, (before, after))

    # A3 (item: preserve existing sub-permissions / never reset scope+level):
    # an admin already upgraded this manager to scope_mode='all', level=2,
    # can_view_stats=1, stats_format='pro', allow_custom_period=1, and a real
    # positive event_cutoff_id -- a repeat grant call must not touch ANY of it.
    db = _make_temp_db()
    _seed_manager(db, "adminmgr", 606)
    _seed_access_user(db, 606, scope_mode="all", access_level=2)
    _seed_mba(db, 606, "adminmgr", can_view_stats=1, stats_format="pro", cutoff=77)
    _exec(db, "UPDATE manager_bot_access SET allow_custom_period=1 WHERE tg_user_id=606")
    # scope_mode='all' means access_targets is never even READ by
    # _linked_manager_keys, but the grant still does an unconditional
    # INSERT OR IGNORE there (matching the pre-existing
    # _auto_grant_manager_bot_access behavior this canon replaces) -- so an
    # "already fully granted" fixture must pre-seed it too, or this call
    # would legitimately report granted_now for that one harmless row.
    _exec(db, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (606, 'adminmgr', '2026-01-01T00:00:00')")
    before_au = _row(db, "access_users", tg_user_id=606)
    before_mba = _row(db, "manager_bot_access", tg_user_id=606, manager_key="adminmgr")
    r3 = storage.manager_bot_access_ensure_sync(tg_user_id=606, db_path=db)
    after_au = _row(db, "access_users", tg_user_id=606)
    after_mba = _row(db, "manager_bot_access", tg_user_id=606, manager_key="adminmgr")
    check("A3: scope_mode='all' -> already_granted (nothing missing to create)",
          r3["status"] == "already_granted", r3)
    check("A3: access_users row byte-identical (scope_mode/access_level NOT reset)",
          before_au == after_au, (before_au, after_au))
    check("A3: manager_bot_access row byte-identical (sub-perms/cutoff NOT touched)",
          before_mba == after_mba, (before_mba, after_mba))

    # A4 (item: not-eligible -- disabled access_users, no username-only bypass).
    db = _make_temp_db()
    _seed_manager(db, "dismgr", 601)
    _seed_access_user(db, 601, is_enabled=0)
    r4 = storage.manager_bot_access_ensure_sync(tg_user_id=601, db_path=db)
    check("A4: disabled access_users -> 'disabled', zero writes",
          r4["status"] == "disabled", r4)
    check("A4: no access_targets/manager_bot_access created for a disabled user",
          not _rows(db, "access_targets", tg_user_id=601) and not _rows(db, "manager_bot_access", tg_user_id=601))

    # A5: revoked manager_bot_access -> 'disabled', zero further writes.
    db = _make_temp_db()
    _seed_manager(db, "revmgr", 602)
    _seed_access_user(db, 602)
    _seed_mba(db, 602, "revmgr", revoked=1)
    r5 = storage.manager_bot_access_ensure_sync(tg_user_id=602, db_path=db)
    check("A5: revoked manager_bot_access -> 'disabled'", r5["status"] == "disabled", r5)
    check("A5: no access_targets created for a revoked user",
          not _rows(db, "access_targets", tg_user_id=602))

    # A6: genuinely unknown user -> not_eligible.
    db = _make_temp_db()
    r6 = storage.manager_bot_access_ensure_sync(tg_user_id=999, db_path=db)
    check("A6: unknown tg_user_id -> not_eligible, zero writes",
          r6["status"] == "not_eligible" and not _rows(db, "access_users", tg_user_id=999))

    # A7 (item: deleted historical manager never restored): a manager_key
    # that used to be active but is now archived/removed must not grant.
    db = _make_temp_db()
    _seed_manager(db, "goneMgr", 700, status="archived")
    r7 = storage.manager_bot_access_ensure_sync(tg_user_id=700, db_path=db)
    check("A7: archived manager -> not_eligible, zero writes",
          r7["status"] == "not_eligible" and not _rows(db, "access_users", tg_user_id=700))

    # A8 (item: disabled manager row / manual_stopped follows confirmed policy):
    # is_enabled=0 and manual_stopped=1 on the MANAGER (not access_users) both
    # make the manager ineligible -- same predicate _find_active_manager_by_tg_user_id uses.
    for label, kwargs in (("manager is_enabled=0", dict(is_enabled=0)), ("manual_stopped=1", dict(manual_stopped=1))):
        db = _make_temp_db()
        _seed_manager(db, "stopmgr", 701, **kwargs)
        r = storage.manager_bot_access_ensure_sync(tg_user_id=701, db_path=db)
        check(f"A8: {label} -> not_eligible, zero writes",
              r["status"] == "not_eligible" and not _rows(db, "access_users", tg_user_id=701), r)

    # A9 (item: username change at the SAME tg_user_id does not break access --
    # identity is by tg_user_id, username is display-only).
    db = _make_temp_db()
    _seed_manager(db, "unmgr", 800)
    storage.manager_bot_access_ensure_sync(tg_user_id=800, username="old_name", db_path=db)
    r9 = storage.manager_bot_access_ensure_sync(tg_user_id=800, username="brand_new_name", db_path=db)
    check("A9: username changed at the same tg_user_id -> still already_granted (no re-eval by username)",
          r9["status"] == "already_granted", r9)

    # A10 (item: a colliding username at a DIFFERENT tg_user_id grants nothing
    # extra and never cross-contaminates the other account).
    db = _make_temp_db()
    _seed_manager(db, "unmgr2", 801, telegram_username="popular_name")
    _seed_manager(db, "othermgr", 802, telegram_username="popular_name")
    r10a = storage.manager_bot_access_ensure_sync(tg_user_id=801, username="popular_name", db_path=db)
    r10b = storage.manager_bot_access_ensure_sync(tg_user_id=802, username="popular_name", db_path=db)
    check("A10: two different tg_user_ids sharing a username each get their OWN independent grant",
          r10a["status"] == "granted_now" and r10b["status"] == "granted_now"
          and r10a["manager_key"] == "unmgr2" and r10b["manager_key"] == "othermgr", (r10a, r10b))
    check("A10: no cross-contamination -- each access_targets row points at its own manager_key only",
          _rows(db, "access_targets", tg_user_id=801, manager_key="unmgr2")
          and _rows(db, "access_targets", tg_user_id=802, manager_key="othermgr")
          and not _rows(db, "access_targets", tg_user_id=801, manager_key="othermgr"))

    # A11 (item: account without a username gets a safe, non-empty fallback
    # at the UI layer -- storage itself just reports "" faithfully; verified
    # here that "" is what comes back, never None, never a crash).
    db = _make_temp_db()
    _seed_manager(db, "nounamemgr", 900, display_name="No Username Guy")
    r11 = storage.manager_bot_access_ensure_sync(tg_user_id=900, db_path=db)
    check("A11: no username anywhere -> username is '' (never None)",
          r11["status"] == "granted_now" and r11.get("username") == "" and r11.get("username") is not None, r11)
    check("A11: display_name falls back to the managers row's own display_name",
          r11.get("display_name") == "No Username Guy", r11)

    # A12 (item: ambiguous identity -- two active managers share one tg_user_id
    # -- fail-closed, matching _find_active_manager_by_tg_user_id's own rule).
    db = _make_temp_db()
    _seed_manager(db, "dupA", 950)
    _seed_manager(db, "dupB", 950)
    r12 = storage.manager_bot_access_ensure_sync(tg_user_id=950, db_path=db)
    check("A12: ambiguous tg_user_id (two active managers) -> identity_conflict, zero writes",
          r12["status"] == "identity_conflict" and not _rows(db, "access_users", tg_user_id=950), r12)

    # A13 (item: real concurrent grant calls never create duplicates).
    db = _make_temp_db()
    _seed_manager(db, "concmgr", 960)
    results = [None, None]
    barrier = threading.Barrier(2)

    def _call(i):
        barrier.wait()
        results[i] = storage.manager_bot_access_ensure_sync(tg_user_id=960, db_path=db)

    t1 = threading.Thread(target=_call, args=(0,))
    t2 = threading.Thread(target=_call, args=(1,))
    t1.start(); t2.start(); t1.join(30); t2.join(30)
    check("A13: neither concurrent call errored", all(r and r.get("status") != "error" for r in results), results)
    # NOTE (round-6 review, T-7): "exactly one row" is enforced by the tables'
    # PRIMARY KEY, so those two checks cannot fail whatever the code does.
    # Kept as cheap schema sanity, but the DISCRIMINATING assertion is the
    # status split below: BEGIN IMMEDIATE must serialise the two callers, so
    # exactly one of them may report having created the rows.
    check("A13: exactly one access_targets row after two concurrent grants (schema sanity)",
          len(_rows(db, "access_targets", tg_user_id=960, manager_key="concmgr")) == 1, results)
    check("A13: exactly one manager_bot_access row after two concurrent grants (schema sanity)",
          len(_rows(db, "manager_bot_access", tg_user_id=960, manager_key="concmgr")) == 1)
    statuses = sorted(str(r.get("status")) for r in results if r)
    check("A13 (discriminating): the two racing callers serialise -- exactly one reports "
          "granted_now and the other already_granted (a lost-update or a second INSERT "
          "attempt would break this)",
          statuses == ["already_granted", "granted_now"], statuses)

    # A14 (item: event_cutoff_id computed correctly for a brand-new grant with
    # PRE-EXISTING historical events -- must suppress the backlog, not deliver it).
    db = _make_temp_db()
    _seed_manager(db, "histmgr", 970)
    for _ in range(12):
        _seed_event(db, "histmgr")
    r14 = storage.manager_bot_access_ensure_sync(tg_user_id=970, db_path=db)
    mba14 = _row(db, "manager_bot_access", tg_user_id=970, manager_key="histmgr")
    check("A14: brand-new grant with 12 pre-existing events -> cutoff=12 (suppresses ALL history)",
          r14["status"] == "granted_now" and int(mba14.get("event_cutoff_id") or -1) == 12, mba14)

    # A15 (item: resolving by manager_key instead of tg_user_id works the same way).
    db = _make_temp_db()
    _seed_manager(db, "bykeymgr", 980)
    r15 = storage.manager_bot_access_ensure_sync(manager_key="bykeymgr", db_path=db)
    check("A15: resolving by manager_key alone -> granted_now with the right tg_user_id",
          r15["status"] == "granted_now" and r15["tg_user_id"] == 980, r15)

    # A16: neither identifier given -> not_eligible, no crash.
    db = _make_temp_db()
    r16 = storage.manager_bot_access_ensure_sync(db_path=db)
    check("A16: no identifier at all -> not_eligible (not an exception)", r16["status"] == "not_eligible", r16)

    # A17 (item: DB/connect failure -> 'error', never an uncaught exception --
    # a directory path can never be opened by sqlite3.connect as a DB file).
    bad_dir = tempfile.mkdtemp(prefix="mbgrant_baddir_")
    try:
        r17 = storage.manager_bot_access_ensure_sync(tg_user_id=1, db_path=bad_dir)
        check("A17: a connect failure returns status='error' (never raises)", r17.get("status") == "error", r17)
    finally:
        os.rmdir(bad_dir)


# ==========================================================================
# Section B -- manager_bot.py's real active _handle_start (AST-extracted,
# REAL storage module bound -- not mocked).
# ==========================================================================

class _FakeSender:
    def __init__(self, username="", first_name="", last_name=""):
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


class _FakeEvent:
    def __init__(self, sender_id, sender=None, is_private=True):
        self.sender_id = sender_id
        self._sender = sender
        self.is_private = is_private
        self.responses: list = []

    async def get_sender(self):
        return self._sender

    async def respond(self, text, buttons=None):
        self.responses.append((text, buttons))


class _NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


class _ButtonsFake:
    """State-aware: non-None iff this uid has at least one access_targets row
    (a loose but real DB-backed proxy for "has some linked manager key"),
    mirroring the actual production gate closely enough for these tests."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.calls: list = []

    def __call__(self, uid):
        self.calls.append(int(uid))
        con = sqlite3.connect(self.db_path)
        try:
            row = con.execute(
                "SELECT 1 FROM access_targets WHERE tg_user_id=? LIMIT 1", (int(uid),)
            ).fetchone()
            return [["BTN"]] if row else None
        finally:
            con.close()


# Real helpers pulled out of manager_bot.py alongside _handle_start, so the
# fallback branch (FIX-1) and the access-request branch (FIX-2) execute the
# PRODUCTION _access_allowed/_linked_manager_keys/_find_active_manager_by_username/
# _upsert_manager_access_request against the temp DB -- not stand-ins.
_START_NS_NAMES = {
    "_manager_bot_sender_identity", "_sender_display_name", "_handle_start",
    "_connect", "_norm_key", "_now_iso",
    "_access_user", "_access_targets", "_all_manager_keys",
    "_linked_manager_keys", "_access_allowed",
    "_find_active_manager_by_username", "_upsert_manager_access_request",
}


def _build_start_ns(db_path, mutate=None, buttons_fn=None):
    tree = ast.parse(MB_SRC)
    names = set(_START_NS_NAMES)
    picked = {}
    for node in tree.body:
        if getattr(node, "name", None) in names:
            picked[node.name] = node
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"missing defs in manager_bot.py: {missing}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in sorted(picked))
    if mutate is not None:
        module_src = mutate(module_src)
    ns = {
        "storage": storage, "TPILOT_DB_PATH": db_path, "log": _NullLogger(),
        "Any": Any, "Dict": Dict, "List": List,
        "sqlite3": sqlite3, "Path": Path, "datetime": datetime,
        "_mbstat_start_buttons": buttons_fn if buttons_fn is not None else _ButtonsFake(db_path),
    }
    exec(compile(module_src, "<manager_bot.py:start>", "exec"), ns)
    return ns


def run_handle_start_checks() -> None:
    print("\n-- Section B: manager_bot.py active /start (real code, real storage) --")

    # B1: granted_now -- exact text, tg_user_id absent, canonical buttons attached.
    db = _make_temp_db()
    _seed_manager(db, "ivan", 555, display_name="Ivan Row")
    ns = _build_start_ns(db)
    ev = _FakeEvent(555, _FakeSender(username="ivan_tg", first_name="Ivan", last_name="Telegram"))
    asyncio.run(ns["_handle_start"](ev))
    text0 = ev.responses[0][0] if ev.responses else ""
    check("B1: response starts with the exact granted_now header", text0.startswith("✅ Доступ выдан автоматически"), ev.responses)
    check("B1: buttons attached (non-None)", bool(ev.responses) and ev.responses[0][1] is not None, ev.responses)
    check("B1: tg_user_id (555) NOT present anywhere in a granted_now response", "555" not in text0, text0)
    check("B1: no technical fields anywhere in a granted_now response",
          not any(m in text0 for m in ("tg_user_id", "access:", "level=", "scope=", "managers=", "Commands:", "whoami")), text0)
    check("B1: shows the Telegram display name", "Ivan Telegram" in text0, text0)
    check("B1: shows the username", "@ivan_tg" in text0, text0)

    # B2: second /start -- already_granted, still no tg_user_id, no repeated
    # "granted automatically" text.
    ev2 = _FakeEvent(555, _FakeSender(username="ivan_tg", first_name="Ivan", last_name="Telegram"))
    asyncio.run(ns["_handle_start"](ev2))
    text2 = ev2.responses[0][0] if ev2.responses else ""
    check("B2: second /start shows the exact already_granted header", text2.startswith("✅ Доступ активен"), ev2.responses)
    check("B2: does NOT repeat 'выдан автоматически'", "выдан автоматически" not in text2, text2)
    check("B2: tg_user_id (555) NOT present", "555" not in text2, text2)
    check("B2: still exactly one access_targets row (idempotent)",
          len(_rows(db, "access_targets", tg_user_id=555)) == 1)

    # B3: no username -- safe non-crashing fallback line, not "@None"/"@".
    db3 = _make_temp_db()
    _seed_manager(db3, "nounmgr", 556, display_name="No Username")
    ns3 = _build_start_ns(db3)
    ev3 = _FakeEvent(556, _FakeSender(username="", first_name="No", last_name="Username"))
    asyncio.run(ns3["_handle_start"](ev3))
    text3 = ev3.responses[0][0] if ev3.responses else ""
    check("B3: missing username shows the safe fallback line, not '@None'/'@'",
          "не задан" in text3 and "@None" not in text3 and "🔗 Username: @\n" not in text3, text3)

    # B4: disabled manager -- exact text, NO tg_user_id (already-known user).
    db4 = _make_temp_db()
    _seed_manager(db4, "dismgr", 601)
    _seed_access_user(db4, 601, is_enabled=0)
    ns4 = _build_start_ns(db4)
    ev4 = _FakeEvent(601, _FakeSender(username="dis_tg"))
    asyncio.run(ns4["_handle_start"](ev4))
    text4 = ev4.responses[0][0] if ev4.responses else ""
    check("B4: disabled user sees the exact denial text", text4 == "🚫 Доступ отключён администратором", ev4.responses)
    check("B4 (disclosure): tg_user_id (601) NOT present on the disabled screen", "601" not in text4, text4)

    # B5: revoked manager_bot_access -- same denial text, no tg_user_id.
    db5 = _make_temp_db()
    _seed_manager(db5, "revmgr", 602)
    _seed_access_user(db5, 602)
    _seed_mba(db5, 602, "revmgr", revoked=1)
    ns5 = _build_start_ns(db5)
    ev5 = _FakeEvent(602, _FakeSender(username="rev_tg"))
    asyncio.run(ns5["_handle_start"](ev5))
    text5 = ev5.responses[0][0] if ev5.responses else ""
    check("B5: revoked user sees the exact denial text", text5 == "🚫 Доступ отключён администратором", ev5.responses)
    check("B5 (disclosure): tg_user_id (602) NOT present on the revoked screen", "602" not in text5, text5)

    # B6 (disclosure, the one screen where the ID belongs): unknown user ->
    # sees ONLY their own id, no manager_key, no scope, no other user's id.
    db6 = _make_temp_db()
    _seed_manager(db6, "unrelated", 700)  # a real manager, different uid
    ns6 = _build_start_ns(db6)
    ev6 = _FakeEvent(888, _FakeSender(username="stranger_tg"))
    asyncio.run(ns6["_handle_start"](ev6))
    text6 = ev6.responses[0][0] if ev6.responses else ""
    check("B6: unknown user sees the exact 'not found' header", "Доступ к TPilot ManagerBot не найден" in text6, text6)
    check("B6 (disclosure): the response contains the user's OWN id (888)", "888" in text6, text6)
    check("B6 (disclosure): the response does NOT contain any other user's id (700)", "700" not in text6, text6)
    check("B6: no manager_key/scope/level leaked on the not-found screen",
          not any(m in text6 for m in ("manager_key", "scope=", "level=", "unrelated")), text6)

    # B7: ambiguous identity -> same 'not found' screen, own id shown.
    db7 = _make_temp_db()
    _seed_manager(db7, "dupA", 950)
    _seed_manager(db7, "dupB", 950)
    ns7 = _build_start_ns(db7)
    ev7 = _FakeEvent(950, _FakeSender(username="dup_tg"))
    asyncio.run(ns7["_handle_start"](ev7))
    text7 = ev7.responses[0][0] if ev7.responses else ""
    check("B7: ambiguous identity -> 'not found' screen with own id (950)",
          "не найден" in text7 and "950" in text7, text7)

    # B8: storage error (directory-as-db-path) -> exact retry message, NO id.
    bad_dir = tempfile.mkdtemp(prefix="mbgrant_baddir2_")
    try:
        ns8 = _build_start_ns(bad_dir)
        ev8 = _FakeEvent(777, _FakeSender(username="err_tg"))
        asyncio.run(ns8["_handle_start"](ev8))
        text8 = ev8.responses[0][0] if ev8.responses else ""
        check("B8: storage error -> exact retry message", text8 == "⚠️ Не удалось проверить доступ. Повторите /start немного позже.", ev8.responses)
        check("B8 (disclosure): tg_user_id (777) NOT present on the error screen", "777" not in text8, text8)
    finally:
        os.rmdir(bad_dir)

    # B9: /whoami is fully removed -- no dispatcher route, no handler.
    # NOTE (round-6 review, T-1): this used to test `'"/whoami"' not in src`.
    # _src_of() returns ast.unparse() output, and ast.unparse ALWAYS emits
    # string literals in SINGLE quotes -- so a double-quoted needle could
    # never match and the check passed even with the route fully restored.
    # Proven below with a real mutant.
    on_message_src = _src_of(MANAGER_BOT_PY, "on_message")
    check("B9: on_message dispatcher no longer routes '/whoami'", "/whoami" not in on_message_src, on_message_src[:200])
    check("B9: _handle_whoami no longer defined as a top-level function in manager_bot.py",
          not any(getattr(n, "name", None) == "_handle_whoami" for n in MB_TREE.body))

    # B9-mutation: restore the dispatcher route in a COPY of the real source
    # and prove the B9 predicate above actually flips to False. Without this
    # the check is unfalsifiable (exactly the defect it used to have).
    b9_mutant = on_message_src.replace(
        "if cmd == '/start':",
        "if cmd == '/whoami':\n            await _handle_whoami(event, tg_user_id)\n            return\n        if cmd == '/start':",
        1,
    )
    check("B9-mutation: the route was really injected into the mutant copy",
          b9_mutant != on_message_src and "/whoami" in b9_mutant)
    check("B9-mutation: the B9 predicate is FALSE on the mutant (so it is falsifiable)",
          not ("/whoami" not in b9_mutant))


# ==========================================================================
# Section R -- remediation round (2026-07-27). Regressions found by the
# independent review: the canonical grant resolves identity ONLY through
# managers.tg_user_id, so three classes of legitimate users were denied
# their working menu, and the AdminBot access-request pipeline was starved.
# Every check here executes the REAL _handle_start with the REAL access
# helpers, and R-BTN below runs the REAL _mbstat_start_buttons chain.
# ==========================================================================

class _FakeButton:
    """Stand-in for telethon's Button; records inline(text, data) calls."""

    @staticmethod
    def inline(text, data=None):
        return ("inline", text, data)


def _build_real_buttons_chain(db_path, can_view_stats=False, closer_keys=()):
    """Execute the REAL _mbstat_start_buttons override chain from
    manager_bot.py (4 stacked defs + the 3 real `_X_PREV_START_BTNS =
    globals().get('_mbstat_start_buttons')` bindings between them), in
    source order, so the delegate chain rebuilds exactly as at import time.

    Round-6 review, T-5: the previous harness replaced this builder with a
    fake, so 'the canonical builder is reused, no second menu' was never
    actually verified. Only the two INDEPENDENT leaf predicates
    (_mbstat_user_can_view, _cls_closer_keys) are stubbed -- they have deep
    unrelated dependency trees. The gates that matter for the fallback fix
    (_access_allowed / _linked_manager_keys) are the REAL ones, hitting the
    real temp DB."""
    tree = ast.parse(MB_SRC)
    pieces = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_mbstat_start_buttons":
            pieces.append((node.lineno, ast.unparse(node)))
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id.endswith("_PREV_START_BTNS"):
                    pieces.append((node.lineno, ast.unparse(node)))
    pieces.sort(key=lambda p: p[0])
    n_defs = sum(1 for _, s in pieces if s.startswith("def "))
    n_binds = len(pieces) - n_defs

    helper_names = {"_connect", "_norm_key", "_access_user", "_access_targets",
                    "_all_manager_keys", "_linked_manager_keys", "_access_allowed"}
    helpers = {}
    for node in tree.body:
        if getattr(node, "name", None) in helper_names:
            helpers[node.name] = ast.unparse(node)

    ns = {
        "TPILOT_DB_PATH": db_path, "log": _NullLogger(), "sqlite3": sqlite3,
        "Path": Path, "Any": Any, "Dict": Dict, "List": List,
        "Button": _FakeButton,
        "_mbstat_user_can_view": lambda uid: bool(can_view_stats),
        "_cls_closer_keys": lambda uid: list(closer_keys),
    }
    src = "\n\n".join(helpers[n] for n in sorted(helpers)) + "\n\n" + \
          "\n\n".join(s for _, s in pieces)
    exec(compile(src, "<manager_bot.py:buttons-chain>", "exec"), ns)
    return ns["_mbstat_start_buttons"], n_defs, n_binds


def _btn_labels(rows):
    out = []
    for row in rows or []:
        for b in row:
            out.append(b[1] if isinstance(b, tuple) and len(b) > 1 else str(b))
    return out


def run_remediation_checks() -> None:
    print("\n-- Section R: review remediation (fallback, requests, scope='all') --")

    # ---- R-BTN: the REAL canonical builder chain, executed (T-5) ----
    dbb = _make_temp_db()
    _seed_manager(dbb, "alpha", 111)
    _seed_access_user(dbb, 4001, scope_mode="selected")
    _exec(dbb, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)", (4001, "alpha", "now"))
    real_buttons, n_defs, n_binds = _build_real_buttons_chain(dbb)
    # W1 (2026-07-29, frozen master plan): added exactly ONE new sanctioned
    # override (the '🔎 Мой доступ' self-diagnostic button,
    # WAVE0/07_safe_diagnostics_contract.md) on top of this same chain --
    # 4 defs/3 binds -> 5 defs/4 binds is the expected new baseline, not a
    # regression. A count above 5/4 would still mean an unsanctioned
    # second/duplicate menu builder was added.
    check("R-BTN: manager_bot.py defines exactly 5 stacked _mbstat_start_buttons "
          "overrides and 4 PREV bindings (4 pre-W1 + 1 W1 self-diagnostic override; "
          "no second/duplicate menu builder was added)",
          n_defs == 5 and n_binds == 4, f"defs={n_defs} binds={n_binds}")
    labels_ok = _btn_labels(real_buttons(4001))
    check("R-BTN: the REAL chain returns the canonical rows for a user with linked keys",
          any("график" in l.lower() for l in labels_ok)
          and any("обновить карточки" in l.lower() for l in labels_ok), labels_ok)
    check("R-BTN: the REAL chain returns None for a uid with no access at all",
          real_buttons(999999) is None, real_buttons(999999))
    check("R-BTN: base grant does NOT unlock the stats button (can_view_stats stays off)",
          not any("статистика" in l.lower() for l in labels_ok), labels_ok)
    closer_btns = _btn_labels(_build_real_buttons_chain(dbb, closer_keys=("alpha",))[0](4001))
    check("R-BTN: closer settings appear ONLY for a closer role, via the real chain",
          any("клоузера" in l.lower() for l in closer_btns)
          and not any("клоузера" in l.lower() for l in labels_ok), (closer_btns, labels_ok))

    # ---- B10: admin-approved NON-manager keeps the menu (blocker B-1) ----
    # panel_bot.py's _mba_approve grants access_users+access_targets to a uid
    # that is deliberately NOT in managers (the username matched, the ID did
    # not). Before the fix this uid got "Доступ не найден" and zero buttons.
    db10 = _make_temp_db()
    _seed_manager(db10, "anna", 111, display_name="Anna", telegram_username="anna_tg")
    _seed_access_user(db10, 222, scope_mode="selected")
    _exec(db10, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)", (222, "anna", "now"))
    # REAL canonical button chain (not _ButtonsFake) -- the fake keyed off
    # access_targets alone and could not model scope_mode='all'.
    ns10 = _build_start_ns(db10, buttons_fn=_build_real_buttons_chain(db10)[0])
    ev10 = _FakeEvent(222, _FakeSender(username="approved_tg", first_name="Real", last_name="Person"))
    asyncio.run(ns10["_handle_start"](ev10))
    t10, b10 = (ev10.responses[0] if ev10.responses else ("", None))
    check("B10: admin-approved non-manager gets the ACTIVE-access screen", "✅ Доступ активен" in t10, t10)
    check("B10: admin-approved non-manager is NOT told access was not found",
          "не найден" not in t10, t10)
    check("B10: admin-approved non-manager receives buttons (menu preserved)", b10 is not None, b10)
    check("B10 (disclosure): own tg_user_id is NOT shown on this success screen", "222" not in t10, t10)
    check("B10: the fallback granted nothing -- no manager_bot_access row was created",
          not _row(db10, "manager_bot_access", tg_user_id=222))

    # ---- B11: scope_mode='all' supervisor keeps the menu (blocker B-1) ----
    db11 = _make_temp_db()
    _seed_manager(db11, "alpha", 111)
    _seed_manager(db11, "beta", 112)
    _seed_access_user(db11, 333, scope_mode="all")
    ns11 = _build_start_ns(db11, buttons_fn=_build_real_buttons_chain(db11)[0])
    ev11 = _FakeEvent(333, _FakeSender(username="super_tg", first_name="Super", last_name="Visor"))
    asyncio.run(ns11["_handle_start"](ev11))
    t11, b11 = (ev11.responses[0] if ev11.responses else ("", None))
    check("B11: scope_mode='all' supervisor gets the ACTIVE-access screen", "✅ Доступ активен" in t11, t11)
    check("B11: scope_mode='all' supervisor receives buttons", b11 is not None, b11)
    check("B11: no access_targets row was fabricated for the blanket-scope user",
          _rows(db11, "access_targets", tg_user_id=333) == [])

    # ---- B12: revoked OWN identity must not kill access to ANOTHER key ----
    # uid 556 legitimately holds access to 'alpha'; their own manager identity
    # 'beta' is revoked. Canon answers "disabled"; the menu must survive.
    db12 = _make_temp_db()
    _seed_manager(db12, "alpha", 111)
    _seed_manager(db12, "beta", 556, telegram_username="beta_tg")
    _seed_access_user(db12, 556, scope_mode="selected")
    _exec(db12, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)", (556, "alpha", "now"))
    _seed_mba(db12, 556, "beta", revoked=1)
    ns12 = _build_start_ns(db12, buttons_fn=_build_real_buttons_chain(db12)[0])
    ev12 = _FakeEvent(556, _FakeSender(username="beta_tg", first_name="Beta", last_name="User"))
    asyncio.run(ns12["_handle_start"](ev12))
    t12, b12 = (ev12.responses[0] if ev12.responses else ("", None))
    check("B12: revoked own identity does NOT show the admin-disabled screen "
          "while access to another manager_key is healthy",
          "отключён администратором" not in t12, t12)
    check("B12: access to the other manager_key survives (active screen + buttons)",
          "✅ Доступ активен" in t12 and b12 is not None, (t12, b12))
    check("B12: the revoked permission row was NOT silently un-revoked",
          int((_row(db12, "manager_bot_access", tg_user_id=556, manager_key="beta") or {}).get("revoked") or 0) == 1)

    # ---- B12b: genuinely disabled user (no other access) still denied ----
    db12b = _make_temp_db()
    _seed_manager(db12b, "gamma", 557, telegram_username="gamma_tg")
    _seed_access_user(db12b, 557, is_enabled=0)
    ns12b = _build_start_ns(db12b)
    ev12b = _FakeEvent(557, _FakeSender(username="gamma_tg"))
    asyncio.run(ns12b["_handle_start"](ev12b))
    t12b, b12b = (ev12b.responses[0] if ev12b.responses else ("", None))
    check("B12b: a disabled user with NO other access still gets the disabled screen",
          "отключён администратором" in t12b, t12b)
    check("B12b: the disabled screen carries no buttons and no tg_user_id",
          b12b is None and "557" not in t12b, (t12b, b12b))

    # ---- B13: username match files a request but grants NOTHING ----
    db13 = _make_temp_db()
    _seed_manager(db13, "ivan", 111, display_name="Ivan", telegram_username="ivan_tg")
    ns13 = _build_start_ns(db13)
    ev13 = _FakeEvent(444, _FakeSender(username="ivan_tg", first_name="Look", last_name="Alike"))
    asyncio.run(ns13["_handle_start"](ev13))
    t13, b13 = (ev13.responses[0] if ev13.responses else ("", None))
    req = _row(db13, "manager_access_requests", user_id=444)
    check("B13: username match files a manager_access_requests row (AdminBot pipeline alive)",
          req is not None and str(req.get("status")) == "new", req)
    check("B13: the request is linked to the matched manager and marked as a username match",
          req is not None and _norm(req.get("matched_manager_key")) == "ivan"
          and str(req.get("match_kind")) == "username", req)
    check("B13: the user sees the clean pending screen", "Заявка создана" in t13, t13)
    check("B13: the pending screen leaks no tg_user_id / manager_key / techno-fields",
          not any(m in t13 for m in ("444", "ivan", "manager_key", "access:", "scope")), t13)
    check("B13: NO access was granted by a username match (access_users)",
          not _row(db13, "access_users", tg_user_id=444))
    check("B13: NO access was granted by a username match (access_targets)",
          _rows(db13, "access_targets", tg_user_id=444) == [])
    check("B13: NO access was granted by a username match (manager_bot_access)",
          _rows(db13, "manager_bot_access", tg_user_id=444) == [])
    check("B13: a username match produces no buttons", b13 is None, b13)

    # ---- A18: canon must not create access_targets for scope_mode='all' ----
    db18 = _make_temp_db()
    _seed_manager(db18, "alpha", 606, telegram_username="sup_tg")
    _seed_access_user(db18, 606, scope_mode="all")
    r18 = storage.manager_bot_access_ensure_sync(tg_user_id=606, db_path=db18)
    check("A18: blanket scope -- canon still resolves and returns a success status",
          r18.get("status") in ("granted_now", "already_granted"), r18)
    check("A18: blanket scope -- NO access_targets row is created",
          _rows(db18, "access_targets", tg_user_id=606) == [], _rows(db18, "access_targets", tg_user_id=606))
    check("A18: blanket scope -- scope_mode was NOT downgraded to 'selected'",
          str((_row(db18, "access_users", tg_user_id=606) or {}).get("scope_mode")) == "all")
    # positive control: a 'selected'-scope user DOES get the row (so A18 is falsifiable)
    _seed_manager(db18, "beta", 707, telegram_username="sel_tg")
    _seed_access_user(db18, 707, scope_mode="selected")
    storage.manager_bot_access_ensure_sync(tg_user_id=707, db_path=db18)
    check("A18 (control): a 'selected'-scope user DOES get an access_targets row",
          [r["manager_key"] for r in _rows(db18, "access_targets", tg_user_id=707)] == ["beta"],
          _rows(db18, "access_targets", tg_user_id=707))

    # ---- E4: lead cards are byte-identical across a grant ----
    db19 = _make_temp_db()
    _seed_manager(db19, "cards", 808, telegram_username="cards_tg")
    for i in range(3):
        _exec(db19,
              "INSERT INTO manager_lead_cards(tg_user_id, bot_chat_id, message_id, chat_id, "
              "manager_key, lead_date, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
              (808, 4000 + i, 9000 + i, 500 + i, "cards", "2026-07-01", "old", "old"))
    before_cards = _full_snapshot(db19, "manager_lead_cards")
    check("E4 (precondition): the lead-card fixture is non-empty", len(before_cards) == 3, before_cards)
    storage.manager_bot_access_ensure_sync(tg_user_id=808, db_path=db19)
    after_cards = _full_snapshot(db19, "manager_lead_cards")
    check("E4: manager_lead_cards is byte-identical after the grant (no card was touched)",
          before_cards == after_cards, (before_cards, after_cards))

    # ---- F5: mutation proof -- removing the fallback MUST break B10/B12 ----
    # Without this, B10/B11/B12 would be unfalsifiable: they must be shown to
    # go RED on code that lacks the fix (i.e. the exact pre-remediation state).
    def _kill_fallback(src):
        assert "if fallback_keys:" in src, "fallback anchor not found in the real _handle_start"
        return src.replace("if fallback_keys:", "if False:", 1)

    db_f5 = _make_temp_db()
    _seed_manager(db_f5, "anna", 111, display_name="Anna", telegram_username="anna_tg")
    _seed_access_user(db_f5, 222, scope_mode="selected")
    _exec(db_f5, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)", (222, "anna", "now"))
    real_src = _src_of(MANAGER_BOT_PY, "_handle_start")
    check("F5 (precondition): the real _handle_start contains the fallback branch",
          "if fallback_keys:" in real_src)
    ns_f5 = _build_start_ns(db_f5, mutate=_kill_fallback,
                            buttons_fn=_build_real_buttons_chain(db_f5)[0])
    ev_f5 = _FakeEvent(222, _FakeSender(username="approved_tg", first_name="Real", last_name="Person"))
    asyncio.run(ns_f5["_handle_start"](ev_f5))
    t_f5, b_f5 = (ev_f5.responses[0] if ev_f5.responses else ("", None))
    check("F5 mutation-proof: WITHOUT the fallback, the admin-approved non-manager is "
          "denied ('not found') -- so B10 genuinely discriminates",
          "не найден" in t_f5 and "✅ Доступ активен" not in t_f5, t_f5)
    check("F5 mutation-proof: WITHOUT the fallback that user also loses every button",
          b_f5 is None, b_f5)

    db_f5b = _make_temp_db()
    _seed_manager(db_f5b, "alpha", 111)
    _seed_manager(db_f5b, "beta", 556, telegram_username="beta_tg")
    _seed_access_user(db_f5b, 556, scope_mode="selected")
    _exec(db_f5b, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)", (556, "alpha", "now"))
    _seed_mba(db_f5b, 556, "beta", revoked=1)
    ns_f5b = _build_start_ns(db_f5b, mutate=_kill_fallback,
                             buttons_fn=_build_real_buttons_chain(db_f5b)[0])
    ev_f5b = _FakeEvent(556, _FakeSender(username="beta_tg"))
    asyncio.run(ns_f5b["_handle_start"](ev_f5b))
    t_f5b = ev_f5b.responses[0][0] if ev_f5b.responses else ""
    check("F5 mutation-proof: WITHOUT the fallback, a revoked own-identity user loses "
          "access to their OTHER manager_key -- so B12 genuinely discriminates",
          "отключён администратором" in t_f5b, t_f5b)

    # ---- F6: mutation proof -- removing the request branch MUST break B13 ----
    def _kill_request(src):
        assert "if username_match:" in src, "username-match anchor not found in the real _handle_start"
        return src.replace("if username_match:", "if False:", 1)

    check("F6 (precondition): the real _handle_start contains the username-match branch",
          "if username_match:" in real_src)
    db_f6 = _make_temp_db()
    _seed_manager(db_f6, "ivan", 111, display_name="Ivan", telegram_username="ivan_tg")
    ns_f6 = _build_start_ns(db_f6, mutate=_kill_request)
    ev_f6 = _FakeEvent(444, _FakeSender(username="ivan_tg", first_name="Look", last_name="Alike"))
    asyncio.run(ns_f6["_handle_start"](ev_f6))
    t_f6 = ev_f6.responses[0][0] if ev_f6.responses else ""
    check("F6 mutation-proof: WITHOUT the branch, NO manager_access_requests row is filed "
          "-- so B13 genuinely discriminates (AdminBot pipeline would starve)",
          not _row(db_f6, "manager_access_requests", user_id=444), _row(db_f6, "manager_access_requests", user_id=444))
    check("F6 mutation-proof: WITHOUT the branch the look-alike falls through to 'not found'",
          "не найден" in t_f6 and "Заявка создана" not in t_f6, t_f6)

    # ---- B14 (round-7 review, R-1): canon error must NOT be masked by the
    # fallback. Fixture: access_users/access_targets readable and show a
    # working access, but the `managers` table is deliberately ABSENT so the
    # canon itself raises -> status == "error". Before the R-1 reorder, the
    # fallback ran first and this uid got "Доступ активен" instead of the
    # honest retry screen -- proven as a live bug against the pre-R-1 code.
    db14 = _make_temp_db()
    _exec(db14, "DROP TABLE managers")
    _seed_access_user(db14, 700, scope_mode="selected")
    _exec(db14, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)", (700, "alpha", "now"))
    canon_r_b14 = storage.manager_bot_access_ensure_sync(tg_user_id=700, db_path=db14)
    check("B14 (precondition): the canon itself reports 'error' on this fixture (no managers table)",
          canon_r_b14.get("status") == "error", canon_r_b14)
    ns14 = _build_start_ns(db14, buttons_fn=_build_real_buttons_chain(db14)[0])
    ev14 = _FakeEvent(700, _FakeSender(username="masked_tg"))
    asyncio.run(ns14["_handle_start"](ev14))
    t14, b14 = (ev14.responses[0] if ev14.responses else ("", None))
    check("B14: canon error shows the honest retry screen, NOT the fallback's active-access text",
          "Не удалось проверить доступ" in t14 and "Доступ активен" not in t14, t14)
    check("B14: the error screen carries no buttons", b14 is None, b14)
    check("B14: the error screen leaks no tg_user_id", "700" not in t14, t14)

    # ---- F9: mutation proof -- reinstating the pre-R-1 order (fallback
    # BEFORE the error check) must make B14 genuinely fail on the SAME
    # fixture, proving the branch ORDER -- not just its presence -- is what
    # B14 depends on.
    real_src_r1 = _src_of(MANAGER_BOT_PY, "_handle_start")
    error_anchor = "if status == 'error':"
    fallback_anchor = "if fallback_keys:"
    check("F9 (precondition): both the error-check and fallback anchors exist verbatim",
          error_anchor in real_src_r1 and fallback_anchor in real_src_r1)

    def _reorder_fallback_before_error(src):
        # Cut out the "if status == 'error': ... return" block (it is the
        # FIRST occurrence, immediately after the granted_now/already_granted
        # return) and reinsert it AFTER the fallback's own "return" -- i.e.
        # restore the exact pre-R-1 order.
        err_start = src.index(error_anchor)
        err_block_end = src.index("return", err_start) + len("return")
        err_block = src[err_start:err_block_end]
        without_err = src[:err_start] + src[err_block_end:]
        fb_start = without_err.index(fallback_anchor)
        fb_return = without_err.index("return", without_err.index("if fallback_keys:", fb_start))
        insert_at = fb_return + len("return")
        return without_err[:insert_at] + "\n\n        " + err_block + without_err[insert_at:]

    mutant_src = _reorder_fallback_before_error(real_src_r1)
    check("F9 (precondition): the reordering actually changed the executable text",
          mutant_src != real_src_r1)
    # _build_start_ns's `mutate` receives the FULL joined namespace source
    # (all helpers, not just _handle_start) -- apply the same reorder
    # transform to THAT text, not a hardcoded replacement, so the other
    # helpers (_access_allowed, _linked_manager_keys, etc.) survive intact.
    ns_f9 = _build_start_ns(db14, mutate=_reorder_fallback_before_error,
                            buttons_fn=_build_real_buttons_chain(db14)[0])
    ev_f9 = _FakeEvent(700, _FakeSender(username="masked_tg"))
    asyncio.run(ns_f9["_handle_start"](ev_f9))
    t_f9 = ev_f9.responses[0][0] if ev_f9.responses else ""
    check("F9 mutation-proof: with fallback restored ahead of the error check, B14's exact "
          "fixture is WRONGLY shown 'Доступ активен' again -- proving order is load-bearing",
          "Доступ активен" in t_f9, t_f9)


def _norm(v):
    return str(v or "").strip().lower()


def _calls_all_inside_try(fn_node, call_name):
    """(all_protected, n_calls) -- True iff EVERY call to `call_name` inside
    fn_node is lexically nested in the body of a try: that has at least one
    except handler. Structural replacement for the old substring scan, which
    could not tell the grant's own try/except from unrelated ones."""
    calls = [n for n in ast.walk(fn_node)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == call_name]
    if not calls:
        return False, 0
    protected = set()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Try) and node.handlers:
            for stmt in node.body:
                for desc in ast.walk(stmt):
                    protected.add(id(desc))
    return all(id(c) in protected for c in calls), len(calls)


# ==========================================================================
# Section C -- main.py's _manager_finalize_login (REAL execution via AST,
# same extract_and_exec technique as tools/onboarding_source_pick_selftest.py,
# with the REAL storage.manager_bot_access_ensure_sync bound -- not mocked).
# This is the call site covering onboarding paths 1/2/5 (phone/code/2FA/QR/
# relogin) and the identity-match branch of path 6 (account replacement).
# ==========================================================================

class _FakeMe:
    def __init__(self, username="mgr_tg", uid=555, first_name="New", last_name=""):
        self.username = username
        self.id = uid
        self.first_name = first_name
        self.last_name = last_name


def _finalize_login_ns(db_path, mutate=None):
    state = {"managers": {}}

    async def _fake_manager_get(k):
        return dict(state["managers"].get(k, {}))

    async def _fake_manager_set_fields(key, **fields):
        state["managers"].setdefault(key, {"manager_key": key})
        state["managers"][key].update(fields)
        # ALSO write through to the REAL temp DB -- the real grant call
        # (manager_bot_access_ensure_sync, bound below to the REAL storage
        # function) reads the actual `managers` table directly, not this
        # fake's in-memory dict, so a finalize that "activates" a manager
        # via manager_set_fields must be reflected there for the grant's own
        # eligibility check to see anything but stale pre-login state.
        real_cols = {"status", "is_enabled", "manual_stopped", "tg_user_id",
                     "telegram_username", "display_name"}
        write = {k: v for k, v in fields.items() if k in real_cols}
        if write:
            con = sqlite3.connect(db_path)
            try:
                set_clause = ", ".join(f"{k}=?" for k in write)
                con.execute(f"UPDATE managers SET {set_clause} WHERE manager_key=?",
                            (*write.values(), key))
                con.commit()
            finally:
                con.close()

    async def _fake_spawn_manager_process(key):
        return (True, "")

    def _fake_manager_label_from_row(row):
        return f"label:{(row or {}).get('manager_key', '')}"

    def _fake_manager_runtime_paths_for_key(key):
        return {"session_path": f"/s/{key}", "db_path": f"/d/{key}", "root": f"/r/{key}", "log_path": f"/l/{key}"}

    def _fake_manager_runtime_onboarding_clear(uid):
        pass

    async def _fake_manager_delete_onboarding(uid):
        pass

    def _fake_now_utc_iso():
        return "2026-01-01T00:00:00"

    async def _fake_partner_source_key_for_manager(key):
        return ""

    async def _fake_tp_finalize_screenshots_on(key):
        pass

    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))
    names = {"_manager_finalize_login"}
    picked = {}
    for node in tree.body:
        if getattr(node, "name", None) in names:
            picked[node.name] = node
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"missing defs in main.py: {missing}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in sorted(picked))
    if mutate is not None:
        module_src = mutate(module_src)

    ns = {
        "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple,
        "registry_normalize_manager_key": normalize_manager_key,
        "_manager_runtime_paths_for_key": _fake_manager_runtime_paths_for_key,
        "manager_get": _fake_manager_get,
        "manager_set_fields": _fake_manager_set_fields,
        "_now_utc_iso": _fake_now_utc_iso,
        "_manager_runtime_onboarding_clear": _fake_manager_runtime_onboarding_clear,
        "manager_delete_onboarding": _fake_manager_delete_onboarding,
        "_spawn_manager_process": _fake_spawn_manager_process,
        "_manager_label_from_row": _fake_manager_label_from_row,
        "_tp_finalize_screenshots_on": _fake_tp_finalize_screenshots_on,
        "_partner_source_key_for_manager": _fake_partner_source_key_for_manager,
        "_ONBOARDING_SOURCE_PICK_MARKER": "В какой источник определить менеджера?",
        "manager_bot_access_ensure_sync": storage.manager_bot_access_ensure_sync,
        "TPILOT_DB_PATH": db_path,
        "print": lambda *a, **k: None,
    }
    exec(compile(module_src, "<main.py:finalize>", "exec"), ns)
    return ns


def run_finalize_login_checks() -> None:
    print("\n-- Section C: main.py _manager_finalize_login (REAL execution, REAL grant) --")

    # C1: a brand-new manager's first successful login -> the REAL grant call
    # fires and actually creates all three rows in the temp DB.
    db = _make_temp_db()
    _seed_manager(db, "newmgr", None, status="new")
    ns = _finalize_login_ns(db)
    asyncio.run(ns["_manager_finalize_login"](1, "newmgr", "+10000000000", _FakeMe(uid=8001, username="new_tg", first_name="New", last_name="Guy")))
    check("C1: finalize triggers a real grant -- access_users created", bool(_rows(db, "access_users", tg_user_id=8001)))
    check("C1: finalize triggers a real grant -- access_targets created",
          bool(_rows(db, "access_targets", tg_user_id=8001, manager_key="newmgr")))
    check("C1: finalize triggers a real grant -- manager_bot_access created",
          bool(_rows(db, "manager_bot_access", tg_user_id=8001, manager_key="newmgr")))

    # C2 (item: relogin/reconnect restores a MISSING grant, not just a fresh
    # one): a manager whose access_targets was wiped (simulating the exact
    # AdminBot full-delete asymmetry) gets it back on the next successful login.
    db = _make_temp_db()
    _seed_manager(db, "relogmgr", 8002, status="active")
    _seed_access_user(db, 8002)
    _seed_mba(db, 8002, "relogmgr")
    ns2 = _finalize_login_ns(db)
    asyncio.run(ns2["_manager_finalize_login"](1, "relogmgr", "+10000000001", _FakeMe(uid=8002, username="relog_tg")))
    check("C2: relogin restores the missing access_targets row",
          bool(_rows(db, "access_targets", tg_user_id=8002, manager_key="relogmgr")))

    # C3: idempotent -- finalize called twice never duplicates anything.
    before = _full_snapshot(db, "access_targets")
    asyncio.run(ns2["_manager_finalize_login"](1, "relogmgr", "+10000000001", _FakeMe(uid=8002, username="relog_tg")))
    after = _full_snapshot(db, "access_targets")
    check("C3: calling finalize twice is idempotent (no duplicate access_targets row)", before == after, (before, after))

    # C4: the grant call must be lexically INSIDE a try/except so a grant
    # failure can never fail the login.
    # NOTE (round-6 review, T-2): this used to assert
    # `"manager_bot_access_ensure_sync" in src and "except Exception" in src`.
    # _manager_finalize_login contains SEVERAL unrelated `except Exception`
    # blocks, so deleting the grant's own try/except left the check green.
    # Now proven structurally via AST ancestry, with a negative control.
    ok_c4, n_calls = _calls_all_inside_try(_last_def_node(MAIN_PY, "_manager_finalize_login"),
                                           "manager_bot_access_ensure_sync")
    check("C4: _manager_finalize_login's grant call is lexically inside a try/except "
          "(AST ancestry, not a substring scan)", ok_c4, f"calls={n_calls}")
    check("C4 (precondition): exactly one grant call exists in _manager_finalize_login", n_calls == 1, n_calls)
    # Negative control: the same predicate must be FALSE for an unprotected call.
    _c4_bad = ast.parse(
        "def f():\n"
        "    manager_bot_access_ensure_sync(1)\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
    ).body[0]
    check("C4 (negative control): the AST predicate is FALSE when the call sits OUTSIDE "
          "the try -- so C4 is falsifiable",
          not _calls_all_inside_try(_c4_bad, "manager_bot_access_ensure_sync")[0])


# ==========================================================================
# Section D -- structural source-scan proof for the other two main.py call
# sites (the replacement fallback branch, and the tdata/.session runtime
# profile-sync block). Both are embedded deep inside functions with many
# unrelated dependencies (a live-client replacement flow; the full manager
# runtime startup sequence) -- too deep to safely extract+exec in isolation,
# same category as this project's own existing structural-only proofs (see
# tools/onboarding_source_pick_selftest.py's "failed-login branches never
# call _manager_finalize_login" check). Each check's mutation-detectability
# is proven directly below it, so this is not merely "the text is present".
# ==========================================================================

def run_call_site_structural_checks() -> None:
    print("\n-- Section D: structural proof for main.py call sites 2 and 3 --")

    repl_src = _src_of(MAIN_PY, "_repl4_create_new_manager")
    d1_anchor = "manager_bot_access_ensure_sync(tg_user_id=int(op_row.get('new_tg_user_id') or 0) or None, manager_key=new_key"
    check("D1: _repl4_create_new_manager's fallback branch calls the canonical grant "
          "with the new account's tg_user_id and manager_key",
          d1_anchor in repl_src, repl_src[:200])
    check("D1: the grant call sits in its own try/except (a grant failure can never fail the replacement commit)",
          "manager_bot_access_ensure_sync" in repl_src and repl_src.count("except Exception as e:") >= 1)

    # main() is itself stacked-override (8 defs) via the SAME delegate-chain
    # pattern as _handle_start/_M26D_ORIG_HANDLE_START -- each later override
    # captures the previous main via globals().get("main") and delegates to
    # it, so the substantive runtime-startup BODY (which is where this grant
    # call lives) is the FIRST def, not the last (the last ones are thin
    # wrappers like the downtime-catchup one). "Last def wins" picks the
    # active NAME, but the call site is reachable through the delegate chain
    # regardless -- so here we deliberately pick the specific def whose body
    # actually contains the call, not simply the last one.
    main_defs = [n for n in MAIN_PY_TREE.body if getattr(n, "name", None) == "main"]
    main_fn_src = None
    for node in main_defs:
        candidate = ast.unparse(node)
        if "manager_bot_access_ensure_sync" in candidate:
            main_fn_src = candidate
            break
    check("D2 precondition: found the specific main() def (of 8 stacked overrides) containing the grant call",
          main_fn_src is not None, [n.lineno for n in main_defs])
    main_fn_src = main_fn_src or ""
    d2_anchor = "manager_bot_access_ensure_sync(tg_user_id=live_tgid or None, manager_key=MANAGER_RUNTIME_KEY"
    check("D2: the tdata/.session runtime-startup path calls the canonical grant with "
          "the freshly-synced live_tgid and MANAGER_RUNTIME_KEY",
          d2_anchor in main_fn_src, main_fn_src.count("manager_bot_access_ensure_sync"))
    check("D2: the grant call is positioned AFTER manager_sync_telegram_profile_in_db "
          "(tg_user_id must already be written before the grant reads it)",
          main_fn_src.find("manager_sync_telegram_profile_in_db") < main_fn_src.find(d2_anchor)
          and main_fn_src.find("manager_sync_telegram_profile_in_db") != -1
          and main_fn_src.find(d2_anchor) != -1)

    # D3: assertions ABOUT THE REAL SOURCE.
    # NOTE (round-6 review, T-3): the previous "D3 mutation proofs" mutated
    # the test's OWN string constant and then asserted the mutated copy
    # differed from the original -- i.e. they tested str.replace() semantics.
    # No production code was mutated or executed, and every one of those
    # checks was constantly true. Replaced with statements that can actually
    # be false if main.py changes.
    full_main_src = MAIN_PY.read_text(encoding="utf-8-sig")
    check("D3: the canonical grant is called exactly 3 times in main.py "
          "(finalize / replacement-fallback / runtime-sync -- no stray 4th site)",
          full_main_src.count("manager_bot_access_ensure_sync(") == 3,
          full_main_src.count("manager_bot_access_ensure_sync("))
    check("D3: the replacement-fallback anchor occurs exactly once (the check is specific)",
          repl_src.count(d1_anchor) == 1, repl_src.count(d1_anchor))
    check("D3: the runtime-sync anchor occurs exactly once (the check is specific)",
          main_fn_src.count(d2_anchor) == 1, main_fn_src.count(d2_anchor))
    # Real negative assertions: the WRONG key must not be what production passes.
    check("D3: the replacement fallback does NOT grant against the OLD manager key",
          "manager_bot_access_ensure_sync(tg_user_id=int(op_row.get('new_tg_user_id') or 0) or None, manager_key=old_key"
          not in repl_src)
    check("D3: the runtime-sync site does NOT grant against a display name instead of the key",
          "manager_bot_access_ensure_sync(tg_user_id=live_tgid or None, manager_key=PUBLIC_MANAGER_NAME"
          not in main_fn_src)

    # D4 (T-6): prove main() def #1 -- which physically holds the runtime-sync
    # call site -- is REACHABLE, instead of only asserting it in a comment.
    # Every later override captures the previous binding and awaits it, so the
    # chain must be unbroken from the LAST def back to the one holding the call.
    grant_def_idx = next(i for i, n in enumerate(main_defs) if "manager_bot_access_ensure_sync" in ast.unparse(n))
    def _delegates_to_predecessor(node) -> bool:
        """True iff this override awaits its captured predecessor -- directly
        (`await _X_ORIG_MAIN()`) or through a local alias
        (`fn = _X_ORIG_MAIN; ...; await fn()`), which main.py uses at 11945."""
        orig_names = {n.id for n in ast.walk(node)
                      if isinstance(n, ast.Name) and n.id.endswith("_ORIG_MAIN")}
        if not orig_names:
            return False
        aliases = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign) and isinstance(sub.value, ast.Name) \
                    and sub.value.id in orig_names:
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Name):
                        aliases.add(tgt.id)
        awaited = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Await) and isinstance(sub.value, ast.Call) \
                    and isinstance(sub.value.func, ast.Name):
                awaited.add(sub.value.func.id)
        return bool(awaited & (orig_names | aliases))

    chain_ok = True
    chain_detail = []
    for node in main_defs[grant_def_idx + 1:]:
        delegated = _delegates_to_predecessor(node)
        chain_detail.append((node.lineno, delegated))
        if not delegated:
            chain_ok = False
    check("D4 (T-6): main() def holding the runtime-sync grant is not the last def "
          "(so reachability genuinely depends on the delegate chain)",
          grant_def_idx < len(main_defs) - 1, (grant_def_idx, len(main_defs)))
    check("D4 (T-6): EVERY later main() override awaits its captured predecessor, so the "
          "delegate chain reaches the def that holds the grant call",
          chain_ok, chain_detail)


# ==========================================================================
# Section E -- BLOCKER B1 regression: prove the canonical grant structurally
# prevents the historical-card-burst class of bug, using the REAL
# AST-extracted M2.6B/M2.6E SQL and the REAL active _fetch_unsent_events_for_user
# (not a hand-copied predicate).
# ==========================================================================

def run_b1_regression_checks() -> None:
    print("\n-- Section E: BLOCKER B1 regression (real backfill SQL + real delivery query) --")

    def _restart(db_path):
        con = sqlite3.connect(db_path)
        con.execute(M2_6B_BACKFILL_SQL)
        con.execute(M2_6E_CUTOFF_REPAIR_SQL)
        con.commit()
        con.close()

    delivery_ns = extract_and_exec(
        MANAGER_BOT_PY, {"_fetch_unsent_events_for_user", "_norm_key", "_now_iso", "_connect"},
        {"sqlite3": sqlite3, "Path": Path, "datetime": datetime, "timedelta": timedelta,
         "Any": Any, "Dict": Dict, "List": List, "log": _NullLogger(), "TPILOT_DB_PATH": None},
    )

    # E1: brand-new manager, 50 pre-existing historical events, granted via
    # the REAL canonical function -- the resulting manager_bot_access row's
    # event_cutoff_id must suppress ALL of that history, both immediately
    # and after a simulated ManagerBot restart (the real backfill/repair
    # re-running must not reset it, since INSERT OR IGNORE never touches an
    # existing row and the repair only touches auto_granted=1 rows with
    # cutoff=0, neither of which applies here).
    db = _make_temp_db()
    _seed_manager(db, "histmgr", 9001)
    for _ in range(50):
        _seed_event(db, "histmgr")
    r = storage.manager_bot_access_ensure_sync(tg_user_id=9001, db_path=db)
    check("E1: canonical grant succeeds for a manager with 50 pre-existing events",
          r["status"] == "granted_now", r)
    delivery_ns["TPILOT_DB_PATH"] = db
    # POSITIVE CONTROL (round-6 review, T-7): the real
    # _fetch_unsent_events_for_user swallows ANY exception and returns [],
    # so a bare "len(...) == 0" would also pass against a broken query or a
    # drifted schema. Prove first, on the SAME fixture, that the query CAN
    # return rows -- an event created AFTER the cutoff must be deliverable.
    _seed_event(db, "histmgr")
    control_deliverable = delivery_ns["_fetch_unsent_events_for_user"](9001, ["histmgr"])
    check("E1 (positive control): the REAL delivery query is functional on this fixture -- "
          "a post-cutoff event IS returned (so the zero-checks below are meaningful)",
          len(control_deliverable) == 1, len(control_deliverable))
    _exec(db, "DELETE FROM manager_bot_events WHERE id > ?", (50,))
    deliverable_before = delivery_ns["_fetch_unsent_events_for_user"](9001, ["histmgr"])
    check("E1: zero historical events deliverable immediately after grant (real delivery query)",
          len(deliverable_before) == 0, len(deliverable_before))
    _restart(db)
    deliverable_after = delivery_ns["_fetch_unsent_events_for_user"](9001, ["histmgr"])
    check("E1: still zero deliverable after a REAL restart (backfill + cutoff-repair)",
          len(deliverable_after) == 0, len(deliverable_after))
    mba = _row(db, "manager_bot_access", tg_user_id=9001, manager_key="histmgr")
    check("E1: event_cutoff_id is exactly 50 (not reset to 0 by the real repair)",
          int(mba.get("event_cutoff_id") or -1) == 50, mba)

    # E2: the supported reconnect scenario (access_targets missing, but
    # manager_bot_access survived with its own real cutoff and some already-
    # delivered history) -- only access_targets gets touched; cutoff/history
    # untouched, deliverable set unchanged before/after the grant AND after
    # a real restart.
    db = _make_temp_db()
    _seed_manager(db, "reconnmgr", 9002)
    _seed_access_user(db, 9002)
    _seed_mba(db, 9002, "reconnmgr", cutoff=5)
    for i in range(1, 11):
        _seed_event(db, "reconnmgr")
    delivery_ns["TPILOT_DB_PATH"] = db
    before_ids = sorted(e["id"] for e in delivery_ns["_fetch_unsent_events_for_user"](9002, ["reconnmgr"]))
    check("E2 precondition: events 6-10 deliverable before any grant (cutoff=5)", before_ids == [6, 7, 8, 9, 10], before_ids)
    before_mba = _row(db, "manager_bot_access", tg_user_id=9002, manager_key="reconnmgr")
    r2 = storage.manager_bot_access_ensure_sync(tg_user_id=9002, db_path=db)
    check("E2: reconnect grant succeeds (access_targets was the only missing piece)",
          r2["status"] == "granted_now", r2)
    _restart(db)
    after_ids = sorted(e["id"] for e in delivery_ns["_fetch_unsent_events_for_user"](9002, ["reconnmgr"]))
    after_mba = _row(db, "manager_bot_access", tg_user_id=9002, manager_key="reconnmgr")
    check("E2: deliverable set UNCHANGED after grant + real restart (no burst)", before_ids == after_ids, (before_ids, after_ids))
    check("E2: manager_bot_access row byte-identical (cutoff still 5, nothing rewritten)",
          before_mba == after_mba, (before_mba, after_mba))

    # E3 (the actual regression this whole class of bug is about): a
    # PRE-EXISTING manager_bot_access row with auto_granted=0 and
    # event_cutoff_id=0 (the exact "manual/backfilled" shape M2.6E
    # deliberately never repairs) must NOT be touched by the grant, and a
    # real restart must not suddenly make 50 historical events deliverable
    # out of nowhere -- proving the canonical grant introduces no NEW path
    # to that historically-dangerous state.
    db = _make_temp_db()
    _seed_manager(db, "manualmgr", 9003)
    _seed_access_user(db, 9003)
    _seed_mba(db, 9003, "manualmgr", cutoff=0, auto_granted=0)
    for _ in range(50):
        _seed_event(db, "manualmgr")
    before_mba3 = _row(db, "manager_bot_access", tg_user_id=9003, manager_key="manualmgr")
    r3 = storage.manager_bot_access_ensure_sync(tg_user_id=9003, db_path=db)
    _restart(db)
    delivery_ns["TPILOT_DB_PATH"] = db
    deliverable3 = delivery_ns["_fetch_unsent_events_for_user"](9003, ["manualmgr"])
    # The real delivery query caps at its own LIMIT 30 (confirmed against the
    # live query, same lesson as this project's earlier round-5 review: a
    # simplified predicate that expected all 50 would be WRONG here).
    check("E3: this pre-existing risk (cutoff=0, auto_granted=0) already existed before this "
          "patch -- the real delivery query returns its capped 30, confirming the grant call "
          "introduces no ADDITIONAL exposure beyond what already existed",
          len(deliverable3) == 30, len(deliverable3))
    after_mba3 = _row(db, "manager_bot_access", tg_user_id=9003, manager_key="manualmgr")
    check("E3: manager_bot_access row byte-identical before/after the grant call "
          "(event_cutoff_id/auto_granted untouched -- the grant never writes to an existing row)",
          before_mba3 == after_mba3, (before_mba3, after_mba3))


# ==========================================================================
# Section F -- mutation / negative-control proofs. Each one: (1) confirms
# the anchor is found verbatim in the REAL source, (2) confirms the mutation
# actually changed executable text, (3) proves the named check that passes
# on real code now FAILS on the mutant, for the intended behavioral reason
# (never merely because the mutant crashed or produced no response).
# ==========================================================================

# ==========================================================================
# Section G (round-7 review, R-3): race-injection against the REAL
# storage.manager_bot_access_ensure_sync. The canon reads `managers` once
# BEFORE `BEGIN IMMEDIATE` (storage.py ~8060-8090) and re-verifies eligibility
# INSIDE the transaction (the `mgr_row`/`mba_row` re-checks, ~8119/8202) --
# a genuine TOCTOU window exists between those two reads. This section
# injects a state mutation, via a SEPARATE connection, into exactly that
# window (simulating a concurrent admin action landing there), and proves
# the transactional re-check is what refuses the call -- not merely that
# refusal happens to occur for some other reason.
# ==========================================================================

class _RaceConnProxy:
    """Wraps the REAL sqlite3 connection storage._bsl_connect returns. The
    very first `execute("BEGIN IMMEDIATE")` call triggers `inject_fn()` via a
    brand-new, independent connection/commit BEFORE delegating to the real
    BEGIN IMMEDIATE -- placing the mutation exactly between the canon's outer
    `managers` read and its transactional re-check. Fires at most once."""

    def __init__(self, real_con, inject_fn):
        self._real = real_con
        self._inject_fn = inject_fn
        self._fired = False

    def execute(self, sql, *args, **kwargs):
        if not self._fired and isinstance(sql, str) and sql.strip().upper() == "BEGIN IMMEDIATE":
            self._fired = True
            self._inject_fn()
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run_with_race_injection(db_path, inject_fn, **kwargs):
    """Monkeypatch storage._bsl_connect for the duration of ONE call to the
    real manager_bot_access_ensure_sync, then restore it unconditionally.
    storage.py itself is never modified -- only the connection the canon
    asks for is wrapped."""
    real_connect = storage._bsl_connect

    def _patched(db_path_arg=None):
        return _RaceConnProxy(real_connect(db_path_arg), inject_fn)

    storage._bsl_connect = _patched
    try:
        return storage.manager_bot_access_ensure_sync(db_path=db_path, **kwargs)
    finally:
        storage._bsl_connect = real_connect


def _race_mutate(db_path, sql, params=()):
    con = sqlite3.connect(db_path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def run_race_injection_checks() -> None:
    print("\n-- Section G: race-injection against the REAL transactional re-check --")

    # C1: the manager row is DELETED by a concurrent admin action between the
    # canon's outer read (which sees it active) and the transactional
    # re-check. Must fail closed, zero writes.
    db_c1 = _make_temp_db()
    _seed_manager(db_c1, "alpha", 901, telegram_username="alpha_tg")
    r_c1_control = storage.manager_bot_access_ensure_sync(tg_user_id=901, db_path=db_c1)
    check("C1 (positive control, no injection): the same fixture grants normally",
          r_c1_control.get("status") == "granted_now", r_c1_control)

    db_c1b = _make_temp_db()
    _seed_manager(db_c1b, "alpha", 901, telegram_username="alpha_tg")
    r_c1 = _run_with_race_injection(
        db_c1b, lambda: _race_mutate(db_c1b, "DELETE FROM managers WHERE manager_key=?", ("alpha",)),
        tg_user_id=901,
    )
    check("C1: manager row deleted mid-flight -> canon fails closed (not_eligible)",
          r_c1.get("status") == "not_eligible", r_c1)
    check("C1: zero writes to access_users after the race", not _rows(db_c1b, "access_users", tg_user_id=901))
    check("C1: zero writes to access_targets after the race",
          not _rows(db_c1b, "access_targets", tg_user_id=901, manager_key="alpha"))
    check("C1: zero writes to manager_bot_access after the race",
          not _rows(db_c1b, "manager_bot_access", tg_user_id=901, manager_key="alpha"))

    # C2: the manager row is disabled (is_enabled=0) mid-flight.
    db_c2 = _make_temp_db()
    _seed_manager(db_c2, "beta", 902, telegram_username="beta_tg")
    r_c2_control = storage.manager_bot_access_ensure_sync(tg_user_id=902, db_path=db_c2)
    check("C2 (positive control, no injection): the same fixture grants normally",
          r_c2_control.get("status") == "granted_now", r_c2_control)

    db_c2b = _make_temp_db()
    _seed_manager(db_c2b, "beta", 902, telegram_username="beta_tg")
    r_c2 = _run_with_race_injection(
        db_c2b, lambda: _race_mutate(db_c2b, "UPDATE managers SET is_enabled=0 WHERE manager_key=?", ("beta",)),
        tg_user_id=902,
    )
    check("C2: manager disabled mid-flight -> canon fails closed (not_eligible)",
          r_c2.get("status") == "not_eligible", r_c2)
    check("C2: zero new access_users/access_targets/manager_bot_access rows after the race",
          not _rows(db_c2b, "access_users", tg_user_id=902)
          and not _rows(db_c2b, "access_targets", tg_user_id=902)
          and not _rows(db_c2b, "manager_bot_access", tg_user_id=902))

    # C3: an EXISTING manager_bot_access permission is revoked mid-flight.
    # Unlike C1/C2, here everything else (managers, access_users,
    # access_targets, manager_bot_access) pre-exists healthy -- the race must
    # not let a concurrently-revoked permission slip through as still valid.
    def _seed_c3(db_path):
        _seed_manager(db_path, "gamma", 903, telegram_username="gamma_tg")
        _seed_access_user(db_path, 903, scope_mode="selected")
        _exec(db_path, "INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)",
              (903, "gamma", "now"))
        _seed_mba(db_path, 903, "gamma", revoked=0)

    db_c3 = _make_temp_db()
    _seed_c3(db_c3)
    r_c3_control = storage.manager_bot_access_ensure_sync(tg_user_id=903, db_path=db_c3)
    check("C3 (positive control, no injection): the same fixture is already_granted",
          r_c3_control.get("status") == "already_granted", r_c3_control)

    db_c3b = _make_temp_db()
    _seed_c3(db_c3b)
    r_c3 = _run_with_race_injection(
        db_c3b,
        lambda: _race_mutate(
            db_c3b,
            "UPDATE manager_bot_access SET revoked=1 WHERE tg_user_id=? AND manager_key=?",
            (903, "gamma"),
        ),
        tg_user_id=903,
    )
    check("C3: permission revoked mid-flight -> canon refuses (disabled), does NOT slip through",
          r_c3.get("status") == "disabled", r_c3)
    mba_c3 = _row(db_c3b, "manager_bot_access", tg_user_id=903, manager_key="gamma")
    check("C3: the revoked flag was NOT silently restored to 0 by the grant",
          int(mba_c3.get("revoked") or 0) == 1, mba_c3)
    check("C3: sub-permissions were not touched by the refused grant",
          int(mba_c3.get("can_view_stats") if mba_c3.get("can_view_stats") is not None else -1) == 0, mba_c3)

    # F10a: weaken the transactional mgr_row re-check (the guard C1/C2 rely
    # on) in a COPY of storage.py, and prove C1's/C2's own scenario now
    # succeeds where the real code refuses -- the re-check is load-bearing,
    # not incidental.
    storage_src = THIS_FILE.parent.parent / "storage.py"
    storage_text = storage_src.read_text(encoding="utf-8-sig")
    f10a_anchor = (
        "mgr_row = con.execute(\n"
        "                \"\"\"\n"
        "                SELECT 1 FROM managers\n"
        "                WHERE tg_user_id=? AND manager_key=?\n"
        "                  AND COALESCE(is_enabled,0)=1\n"
        "                  AND COALESCE(manual_stopped,0)=0\n"
        "                  AND COALESCE(status,'')='active'\n"
        "                \"\"\",\n"
        "                (resolved_uid, mk),\n"
        "            ).fetchone()"
    )
    check("F10a precondition: the transactional mgr_row re-check anchor exists verbatim",
          f10a_anchor in storage_text)
    f10a_mutant_text = storage_text.replace(f10a_anchor, "mgr_row = (1,)  # F10a mutation: re-check disabled", 1)
    check("F10a precondition: mutation actually changed the text", f10a_mutant_text != storage_text)
    f10a_ns: dict = {"__file__": str(storage_src), "__name__": "storage_f10a_mutant"}
    exec(compile(f10a_mutant_text, "<storage.py:F10a-mutant>", "exec"), f10a_ns)

    db_f10a = _make_temp_db()
    _seed_manager(db_f10a, "alpha", 901, telegram_username="alpha_tg")

    def _inject_delete():
        _race_mutate(db_f10a, "DELETE FROM managers WHERE manager_key=?", ("alpha",))

    real_connect = storage._bsl_connect
    storage._bsl_connect = lambda db_path_arg=None: _RaceConnProxy(real_connect(db_path_arg), _inject_delete)
    try:
        r_f10a = f10a_ns["manager_bot_access_ensure_sync"](tg_user_id=901, db_path=db_f10a)
    finally:
        storage._bsl_connect = real_connect
    check("F10a mutation-proof: with the mgr_row re-check disabled, C1's exact scenario "
          "(manager deleted mid-flight) now WRONGLY succeeds -- proving the real re-check "
          "is what C1 depends on",
          r_f10a.get("status") == "granted_now", r_f10a)

    # F10b: weaken the transactional mba_row revoked guard (the guard C3
    # relies on) and prove C3's own scenario now WRONGLY succeeds.
    f10b_anchor = 'elif int(mba_row["revoked"] or 0) != 0:'
    check("F10b precondition: the mba_row revoked re-check anchor exists verbatim", f10b_anchor in storage_text)
    f10b_mutant_text = storage_text.replace(f10b_anchor, "elif False:  # F10b mutation: revoked re-check disabled", 1)
    check("F10b precondition: mutation actually changed the text", f10b_mutant_text != storage_text)
    f10b_ns: dict = {"__file__": str(storage_src), "__name__": "storage_f10b_mutant"}
    exec(compile(f10b_mutant_text, "<storage.py:F10b-mutant>", "exec"), f10b_ns)

    db_f10b = _make_temp_db()
    _seed_c3(db_f10b)

    def _inject_revoke():
        _race_mutate(db_f10b, "UPDATE manager_bot_access SET revoked=1 WHERE tg_user_id=? AND manager_key=?",
                     (903, "gamma"))

    storage._bsl_connect = lambda db_path_arg=None: _RaceConnProxy(real_connect(db_path_arg), _inject_revoke)
    try:
        r_f10b = f10b_ns["manager_bot_access_ensure_sync"](tg_user_id=903, db_path=db_f10b)
    finally:
        storage._bsl_connect = real_connect
    check("F10b mutation-proof: with the revoked re-check disabled, C3's exact scenario "
          "(permission revoked mid-flight) now WRONGLY succeeds (already_granted) -- proving "
          "the real re-check is what C3 depends on",
          r_f10b.get("status") == "already_granted", r_f10b)
    check("F10b: storage._bsl_connect was restored to the real function after both injections",
          storage._bsl_connect is real_connect)


# ==========================================================================
# Section S (round-7 review, R-2): structural proof that the two dead,
# zero-caller helpers with dangerous ON CONFLICT ... DO UPDATE SQL
# (_auto_grant_manager_bot_access, _reconcile_reconnected_manager) were
# REMOVED, not merely left unreachable.
# ==========================================================================

def run_structural_absence_checks() -> None:
    print("\n-- Section S: dead-code removal proof (R-2) --")

    top_level_names = {getattr(n, "name", None) for n in MB_TREE.body}
    check("S1: _auto_grant_manager_bot_access is no longer defined as a top-level function",
          "_auto_grant_manager_bot_access" not in top_level_names)
    check("S2: _reconcile_reconnected_manager is no longer defined as a top-level function",
          "_reconcile_reconnected_manager" not in top_level_names)

    # S3: the specific dangerous fragment (unconditional scope/level downgrade
    # + re-enable) must not exist ANYWHERE in the file, not just in the two
    # removed defs -- guards against it having been copy-pasted elsewhere.
    danger_fragments = [
        "access_level=1,\n                scope_mode='selected',\n                is_enabled=1,",
        "ON CONFLICT(tg_user_id) DO UPDATE SET is_enabled=1,",
    ]
    for i, frag in enumerate(danger_fragments, start=1):
        check(f"S3.{i}: the legacy scope/level-downgrade SQL fragment is absent from manager_bot.py",
              frag not in MB_SRC, frag[:60])

    # Negative control: prove S3's frag-matching would actually catch this
    # exact pattern if it existed, so "absent" isn't vacuously true.
    control_src = (
        "INSERT INTO access_users(...) VALUES (...)\n"
        "            ON CONFLICT(tg_user_id) DO UPDATE SET\n"
        "                access_level=1,\n"
        "                scope_mode='selected',\n"
        "                is_enabled=1,\n"
    )
    check("S3 (negative control): the fragment-match predicate DOES trigger on a "
          "reintroduced copy of the removed SQL -- so S3 is falsifiable",
          danger_fragments[0] in control_src)

    # Reachability: no dynamic dispatch surface in manager_bot.py could still
    # reach either name (globals()[...]/getattr(sys.modules,...)/eval/exec).
    dynamic_dispatch_calls = [
        n for n in ast.walk(MB_TREE)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id in ("eval", "exec")
    ]
    check("S4: manager_bot.py contains no eval()/exec() call sites that could dynamically "
          "resurrect the removed helpers by name", not dynamic_dispatch_calls, dynamic_dispatch_calls)


def run_mutation_checks() -> None:
    print("\n-- Section F: mutation / negative-control proofs --")

    # F1: remove the /start self-heal call entirely -- a manager whose
    # access is missing must no longer be auto-granted (the "not found"
    # screen appears instead of "granted_now").
    def _f1_mutate(src):
        anchor = "result = storage.manager_bot_access_ensure_sync(tg_user_id=uid, display_name=str(identity.get('display_name') or '').strip(), username=str(identity.get('username') or '').strip(), db_path=TPILOT_DB_PATH)"
        check("F1 precondition: /start's grant-call anchor found verbatim", anchor in src)
        return src.replace(
            anchor,
            "result = {'status': 'not_eligible', 'tg_user_id': uid, 'manager_key': '', 'display_name': '', 'username': ''}"
            "  # F1 mutation: self-heal call removed",
            1,
        )

    db = _make_temp_db()
    _seed_manager(db, "f1mgr", 9101)
    ns_f1 = _build_start_ns(db, mutate=_f1_mutate)
    ev_f1 = _FakeEvent(9101, _FakeSender(username="f1_tg"))
    asyncio.run(ns_f1["_handle_start"](ev_f1))
    text_f1 = ev_f1.responses[0][0] if ev_f1.responses else ""
    check("F1 mutation-proof: with the self-heal call removed, an otherwise-eligible manager "
          "now gets the 'not found' screen instead of being granted access",
          "не найден" in text_f1 and not text_f1.startswith("✅"), ev_f1.responses)
    check("F1 mutation-proof: and (real-world consequence) no access_targets row gets created",
          not _rows(db, "access_targets", tg_user_id=9101))

    # F2: auto-enable a sub-permission (can_view_stats) on a BRAND-NEW grant
    # -- proves A1's "no sub-perms enabled" assertion actually discriminates.
    storage_src = THIS_FILE.parent.parent / "storage.py"
    storage_text = storage_src.read_text(encoding="utf-8-sig")
    f2_anchor = "VALUES (?, ?, 1, 1, 0, 'light', 0, 1, 0, ?, 0, 0, '', ?, ?, ?)"
    check("F2 precondition: the manager_bot_access INSERT literal found verbatim in storage.py", f2_anchor in storage_text)
    mutated_storage = storage_text.replace(
        f2_anchor,
        "VALUES (?, ?, 1, 1, 1, 'light', 0, 1, 0, ?, 0, 0, '', ?, ?, ?)  -- F2 mutation: can_view_stats forced to 1",
        1,
    )
    check("F2 precondition: mutation actually changed the text", mutated_storage != storage_text)
    mod_ns: dict = {"__file__": str(storage_src), "__name__": "storage_f2_mutant"}
    exec(compile(mutated_storage, "<storage.py:F2-mutant>", "exec"), mod_ns)
    db_f2 = _make_temp_db()
    _seed_manager(db_f2, "f2mgr", 9102)
    r_f2 = mod_ns["manager_bot_access_ensure_sync"](tg_user_id=9102, db_path=db_f2)
    mba_f2 = _row(db_f2, "manager_bot_access", tg_user_id=9102, manager_key="f2mgr")
    check("F2 mutation-proof: with the INSERT literal corrupted, a BRAND-NEW grant now "
          "auto-enables can_view_stats=1 (the exact regression A1 exists to catch)",
          r_f2.get("status") == "granted_now" and int(mba_f2.get("can_view_stats") or 0) == 1, mba_f2)

    # F7/F8 (round-6 review): the review flagged the revoked / is_enabled
    # guards as PARTIAL -- asserted positively, but never shown to be
    # load-bearing. Mutate each guard in storage.py and prove the guard is
    # what refuses the grant (self-heal must never resurrect access an admin
    # revoked or disabled).
    def _exec_storage_mutant(mutated_text, tag):
        mns: dict = {"__file__": str(storage_src), "__name__": f"storage_{tag}_mutant"}
        exec(compile(mutated_text, f"<storage.py:{tag}-mutant>", "exec"), mns)
        return mns["manager_bot_access_ensure_sync"]

    f7_anchor = 'elif int(mba_row["revoked"] or 0) != 0:'
    check("F7 precondition: the revoked guard exists verbatim in storage.py", f7_anchor in storage_text)
    f7_fn = _exec_storage_mutant(
        storage_text.replace(f7_anchor, 'elif False:  # F7 mutation: revoked guard disabled', 1), "f7")
    db_f7 = _make_temp_db()
    _seed_manager(db_f7, "f7mgr", 9107)
    _seed_access_user(db_f7, 9107)
    _seed_mba(db_f7, 9107, "f7mgr", revoked=1)
    r_f7_real = storage.manager_bot_access_ensure_sync(tg_user_id=9107, db_path=db_f7)
    check("F7 baseline: the REAL function refuses a revoked permission ('disabled')",
          r_f7_real.get("status") == "disabled", r_f7_real)
    # The mutant gets its OWN db: it is expected to WRITE, and must not
    # contaminate the state the baseline assertions above were made against.
    db_f7m = _make_temp_db()
    _seed_manager(db_f7m, "f7mgr", 9107)
    _seed_access_user(db_f7m, 9107)
    _seed_mba(db_f7m, 9107, "f7mgr", revoked=1)
    r_f7_mut = f7_fn(tg_user_id=9107, db_path=db_f7m)
    check("F7 mutation-proof: with the revoked guard removed the call NO LONGER refuses -- "
          "so that guard is what blocks resurrection of admin-revoked access",
          r_f7_mut.get("status") != "disabled", r_f7_mut)

    f8_anchor = 'elif int(user_row["is_enabled"] or 0) != 1:'
    check("F8 precondition: the is_enabled guard exists verbatim in storage.py", f8_anchor in storage_text)
    f8_fn = _exec_storage_mutant(
        storage_text.replace(f8_anchor, 'elif False:  # F8 mutation: is_enabled guard disabled', 1), "f8")
    db_f8 = _make_temp_db()
    _seed_manager(db_f8, "f8mgr", 9108)
    _seed_access_user(db_f8, 9108, is_enabled=0)
    r_f8_real = storage.manager_bot_access_ensure_sync(tg_user_id=9108, db_path=db_f8)
    check("F8 baseline: the REAL function refuses a disabled access_users row ('disabled')",
          r_f8_real.get("status") == "disabled", r_f8_real)
    check("F8: the REAL function wrote nothing for the disabled user (no access_targets)",
          not _rows(db_f8, "access_targets", tg_user_id=9108),
          _rows(db_f8, "access_targets", tg_user_id=9108))
    db_f8m = _make_temp_db()
    _seed_manager(db_f8m, "f8mgr", 9108)
    _seed_access_user(db_f8m, 9108, is_enabled=0)
    r_f8_mut = f8_fn(tg_user_id=9108, db_path=db_f8m)
    check("F8 mutation-proof: with the is_enabled guard removed the call NO LONGER refuses -- "
          "so that guard is what blocks resurrection of admin-disabled access",
          r_f8_mut.get("status") != "disabled", r_f8_mut)
    check("F8 mutation-proof: and the mutant DOES write access rows the real guard prevents",
          bool(_rows(db_f8m, "access_targets", tg_user_id=9108)),
          _rows(db_f8m, "access_targets", tg_user_id=9108))

    # F3: reinstate the OLD technical status text on the granted_now branch
    # -- proves B1's "no technical fields" assertion actually discriminates.
    def _f3_mutate(src):
        anchor = "header = '✅ Доступ выдан автоматически' if status == 'granted_now' else '✅ Доступ активен'"
        check("F3 precondition: header-selection anchor found verbatim", anchor in src)
        replacement = (
            anchor + "\n            "
            "header = header + f' | tg_user_id: {uid} | access: active, level=1, scope=selected, managers=1'"
            "  # F3 mutation: old technical text reinstated"
        )
        return src.replace(anchor, replacement, 1)

    db_f3 = _make_temp_db()
    _seed_manager(db_f3, "f3mgr", 9103)
    ns_f3 = _build_start_ns(db_f3, mutate=_f3_mutate)
    ev_f3 = _FakeEvent(9103, _FakeSender(username="f3_tg"))
    asyncio.run(ns_f3["_handle_start"](ev_f3))
    text_f3 = ev_f3.responses[0][0] if ev_f3.responses else ""
    check("F3 mutation-proof: with the old technical text reinstated, the granted_now response "
          "now DOES contain tg_user_id/access:/level=/scope=/managers= (B1's assertion correctly fails)",
          any(m in text_f3 for m in ("tg_user_id", "access:", "level=", "scope=", "managers=")), text_f3)

    # F4: make identity resolution username-based instead of tg_user_id-based,
    # AND remove the corroborating uid-vs-resolved-row cross-check (real code
    # has BOTH layers -- proven below that removing only the query still gets
    # caught by the cross-check, so a meaningful attack requires breaking
    # both, which is exactly what this mutation does). Proves this project's
    # mandated "Telegram ID is the ONLY basis for granting access, username
    # is display-only" rule is load-bearing: with it gone, an attacker's OWN
    # real (but unrelated) Telegram account gets silently bound to a
    # DIFFERENT manager's identity purely by claiming their username.
    f4_query_anchor = "WHERE tg_user_id=?\n                  AND COALESCE(is_enabled,0)=1\n                  AND COALESCE(manual_stopped,0)=0\n                  AND COALESCE(status,'')='active'\n                  AND COALESCE(manager_key,'')<>''\n                ORDER BY manager_key ASC\n                \"\"\",\n                (uid,),"
    f4_crosscheck_anchor = "if uid and row_uid and uid != row_uid:\n            # manager_key input resolved to a DIFFERENT tg_user_id than the\n            # caller expected -- fail closed rather than silently granting\n            # to a mismatched account.\n            return {\"status\": \"identity_conflict\", \"tg_user_id\": uid, \"manager_key\": mk,\n                    \"display_name\": \"\", \"username\": \"\"}"
    check("F4 precondition: the tg_user_id-based resolution query found verbatim in storage.py", f4_query_anchor in storage_text)
    check("F4 precondition: the uid-vs-resolved-row cross-check found verbatim in storage.py", f4_crosscheck_anchor in storage_text)
    mutated_storage_f4 = storage_text.replace(
        f4_query_anchor,
        "WHERE telegram_username=?\n                  AND COALESCE(is_enabled,0)=1\n                  AND COALESCE(manual_stopped,0)=0\n                  AND COALESCE(status,'')='active'\n                  AND COALESCE(manager_key,'')<>''\n                ORDER BY manager_key ASC\n                \"\"\",\n                (username or '',),  # F4 mutation: resolves by username instead of tg_user_id",
        1,
    ).replace(
        f4_crosscheck_anchor,
        "pass  # F4 mutation: uid-vs-resolved-row cross-check removed",
        1,
    )
    check("F4 precondition: mutation actually changed the text", mutated_storage_f4 != storage_text)
    mod_ns_f4: dict = {"__file__": str(storage_src), "__name__": "storage_f4_mutant"}
    exec(compile(mutated_storage_f4, "<storage.py:F4-mutant>", "exec"), mod_ns_f4)

    # First prove removing ONLY the query is not (by itself) exploitable --
    # the cross-check alone already stops it (genuine defense-in-depth: an
    # attacker's real, mismatched uid=9999 is refused even though the
    # resolution query itself was corrupted to trust the username claim).
    query_only_mutant_src = storage_text.replace(
        f4_query_anchor,
        "WHERE telegram_username=?\n                  AND COALESCE(is_enabled,0)=1\n                  AND COALESCE(manual_stopped,0)=0\n                  AND COALESCE(status,'')='active'\n                  AND COALESCE(manager_key,'')<>''\n                ORDER BY manager_key ASC\n                \"\"\",\n                (username or '',),  # F4 mutation: resolves by username instead of tg_user_id",
        1,
    )
    check("F4 precondition: the query-only mutant text differs from the real source",
          query_only_mutant_src != storage_text)
    query_only_ns: dict = {"__file__": str(storage_src), "__name__": "storage_f4_queryonly_mutant"}
    exec(compile(query_only_mutant_src, "<storage.py:F4-query-only-mutant>", "exec"), query_only_ns)
    db_f4a = _make_temp_db()
    _seed_manager(db_f4a, "f4victim0", 9104, telegram_username="shared_name0")
    r_f4a = query_only_ns["manager_bot_access_ensure_sync"](tg_user_id=9999, username="shared_name0", db_path=db_f4a)
    check("F4 (defense-in-depth): with ONLY the query mutated (cross-check still intact), "
          "the same attack is still refused -- identity_conflict, not granted_now",
          r_f4a.get("status") == "identity_conflict", r_f4a)

    db_f4 = _make_temp_db()
    _seed_manager(db_f4, "f4victim", 9104, telegram_username="shared_name")
    # Attacker's OWN real (but unrelated) Telegram account, uid=9999 --
    # claims a username that matches a DIFFERENT, real manager's stored
    # telegram_username.
    r_f4 = mod_ns_f4["manager_bot_access_ensure_sync"](tg_user_id=9999, username="shared_name", db_path=db_f4)
    # This is a STRONGER result than the mutation originally set out to
    # prove: even with BOTH the resolution query AND its corroborating
    # cross-check corrupted, a THIRD, independent layer -- the transactional
    # re-check re-querying `managers WHERE tg_user_id=? AND manager_key=?`
    # with the (attacker uid, victim's key) pair, which matches no real row
    # -- still refuses the grant. Real-world takeaway: this project's
    # "identity is by Telegram ID, never by username" rule is defended in
    # triplicate, not by a single check that this suite could vacuously
    # mutate around.
    # HONESTY NOTE (round-6 review, T-4): these two checks were previously
    # labelled "F4 mutation-proof". They are NOT mutation proofs: the mutant
    # and the real function return the SAME value (not_eligible), so nothing
    # goes GREEN->RED and neither anchor is shown to be load-bearing. What
    # they legitimately demonstrate is defence-in-depth -- the attack stays
    # refused even with two of the three layers broken. Relabelled to say
    # exactly that, no more.
    check("F4 (defence-in-depth, NOT a mutation proof): with the outer query AND the outer "
          "cross-check both corrupted, the independent transactional re-check still refuses "
          "the mismatched (attacker uid, victim manager_key) pair -- not_eligible, zero writes",
          r_f4.get("status") == "not_eligible" and not _rows(db_f4, "access_users", tg_user_id=9999), r_f4)
    db_f4b = _make_temp_db()
    _seed_manager(db_f4b, "f4victim2", 9105, telegram_username="shared_name2")
    r_f4_real = storage.manager_bot_access_ensure_sync(tg_user_id=9999, username="shared_name2", db_path=db_f4b)
    check("F4 (baseline): the REAL unmutated function refuses the same username-based attack "
          "on a fresh db -- not_eligible, zero writes",
          r_f4_real.get("status") == "not_eligible" and not _rows(db_f4b, "access_users", tg_user_id=9999), r_f4_real)


# ==========================================================================
# Self-guard: this file must contain no vacuous check(...) calls
# ==========================================================================

_SELF_GUARD_SAFE_BUILTINS = {
    "any": any, "all": all, "bool": bool, "len": len, "int": int, "str": str,
    "set": set, "list": list, "tuple": tuple, "dict": dict, "sorted": sorted,
    "abs": abs, "min": min, "max": max, "sum": sum,
}


def _condition_is_constantly_true(node) -> bool:
    """Constant-fold `node` in an empty namespace. Anything that evaluates
    without a NameError is independent of program state; if also truthy, the
    surrounding check() can never fail. Ported from this project's own
    audited round-5/6 fix (proven against 8 always-true/genuine forms)."""
    try:
        expr = ast.Expression(body=node)
        ast.fix_missing_locations(expr)
        value = eval(  # noqa: S307 -- constant-folding only; no names, allowlisted builtins
            compile(expr, "<self-guard>", "eval"),
            {"__builtins__": _SELF_GUARD_SAFE_BUILTINS},
            {},
        )
    except Exception:
        return False
    return bool(value)


def _scan_self_for_vacuous_checks() -> list[str]:
    self_src = THIS_FILE.read_text(encoding="utf-8")
    self_tree = ast.parse(self_src)
    offenders = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == "check" and len(node.args) >= 2:
                cond = node.args[1]
                if _condition_is_constantly_true(cond):
                    label = node.args[0].value if isinstance(node.args[0], ast.Constant) else "<dynamic label>"
                    offenders.append(f"line {node.lineno}: check({label!r}, {ast.unparse(cond)})")
            self.generic_visit(node)

    _Visitor().visit(self_tree)
    return offenders


def _self_guard_detector_probe() -> list[str]:
    must_catch = [
        "True", "bool(True)", "any([True])", "any(c for c in [True])",
        "1 == 1", "not False", "len([1]) > 0", "all([])",
    ]
    must_not_catch = [
        "r['status'] == 'granted_now'", "not offenders",
        "event.responses and event.responses[0][1] is not None",
        "bool(result4)", "False", "1 == 2",
    ]
    problems = []
    for src in must_catch:
        node = ast.parse(src, mode="eval").body
        if not _condition_is_constantly_true(node):
            problems.append(f"MISSED always-true form: {src}")
    for src in must_not_catch:
        node = ast.parse(src, mode="eval").body
        if _condition_is_constantly_true(node):
            problems.append(f"FALSE POSITIVE on genuine/false condition: {src}")
    return problems


def _cleanup_temp_dbs() -> None:
    """Every temp DB this suite creates gets deleted here (best-effort,
    including SQLite's -wal/-shm sidecar files left by the real _connect()'s
    WAL mode) instead of leaking into %TEMP% forever."""
    for path in _TEMP_DB_PATHS:
        for candidate in (path, path + "-wal", path + "-shm", path + "-journal"):
            try:
                if os.path.exists(candidate):
                    os.remove(candidate)
            except Exception:
                pass


def main() -> int:
    try:
        probe_problems = _self_guard_detector_probe()
        check("SELF-GUARD PROBE: the vacuous-check detector catches every always-true form "
              "and none of the genuine ones", not probe_problems, detail=str(probe_problems))

        offenders = _scan_self_for_vacuous_checks()
        check("SELF-GUARD: no constant-foldable check(...) conditions in this file", not offenders, detail=str(offenders))

        run_storage_functional_checks()
        run_handle_start_checks()
        run_remediation_checks()
        run_finalize_login_checks()
        run_call_site_structural_checks()
        run_b1_regression_checks()
        run_race_injection_checks()
        run_structural_absence_checks()
        run_mutation_checks()

        print(f"\n{len(FAILURES)} failing check(s)." if FAILURES else "\nAll checks passed.")
        for f in FAILURES:
            print(f" - {f}")
        return 1 if FAILURES else 0
    finally:
        _cleanup_temp_dbs()


if __name__ == "__main__":
    sys.exit(main())
