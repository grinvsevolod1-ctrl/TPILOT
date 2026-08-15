# -*- coding: utf-8 -*-
"""tools/onboarding_source_pick_selftest.py -- offline self-test for the
"new-manager onboarding: source pick / screenshots ON / schedule inheritance
status / proxy PIN reveal one-line format" patch (main.py, panel_bot.py).

Business rules under test:
* Every successful login/re-login path (phone-code, 2FA password, QR, QR+2FA)
  converges in main.py's _manager_finalize_login, which now:
    - forces this manager's screenshot config to ON/instant (targeted UPSERT,
      never touches other managers, never fails the login on write error);
    - appends a source-pick prompt marker to the result text UNLESS the
      manager is already linked to a traffic source.
* A failed auth attempt (bad code/password) never reaches _manager_finalize_login
  at all -- it returns its own error text directly.
* panel_bot.py detects the marker in the synchronous wizard result text (code/
  pass/qr steps) and renders source-pick buttons instead of the generic
  back-to-panel row; the QR path (async) carries the same marker through a
  panel_notifications row with kind="manager_source_pick", parsed back into
  buttons by the notification loop.
* /source link (main.py _handle_source_command) now appends a schedule
  status line (inherited vs not-configured) without hardcoding 08:00-17:00
  and without creating any manager schedule rows; the existing closer filter
  is untouched.
* The proxy PIN-reveal flow (panel_bot.py) now renders one copyable monospace
  row "host:port:login:password" instead of a password-only line; every other
  proxy display in the project still masks the password.

Techniques (matching the project's own tools/*_selftest.py conventions):
* storage.py IS importable -> real helpers exercised against throwaway
  temporary SQLite files (never the real project DB);
* main.py / panel_bot.py are NOT importable (Telethon/env side effects at
  import time) -> the relevant functions are AST-extracted (last top-level def
  wins, matching the project's override-stack convention) and exec'd into a
  namespace seeded with fakes for their Telethon/env/log boundaries -- same
  technique as tools/deleted_manager_stats_retention_selftest.py and
  tools/reserve_release_selftest.py. Everything DB-related runs for real (real
  aiosqlite/sqlite3) against temp files. Telethon's Button is faked (FakeButton)
  rather than imported, so this file never imports telethon.

Pure/offline: no network, no Telegram, no production DB, no external APIs.

    python3.12 tools\\onboarding_source_pick_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite
import storage
from manager_registry import normalize_manager_key

MAIN_PY = BASE_DIR / "main.py"
PANEL_BOT_PY = BASE_DIR / "panel_bot.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def find_defs(path: Path, name: str) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return [n for n in tree.body if getattr(n, "name", None) == name]


def extract_and_exec(path: Path, names: set, extra_ns: dict) -> dict:
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


class FakeButton:
    def __init__(self, text, data):
        self.text = str(text)
        self.data = data if isinstance(data, bytes) else str(data).encode()

    @classmethod
    def inline(cls, text, data=b""):
        return cls(text, data)


def button_callbacks(rows) -> list:
    return [btn.data.decode("utf-8", "ignore") for row in rows for btn in row]


_COMMON_TYPE_NS = {"Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple}


# ======================================================================
# GROUP A -- main.py _manager_finalize_login: screenshots ON/instant, source-
# pick prompt gating, failed-login isolation (REAL execution via AST).
# ======================================================================

class _FakeMe:
    def __init__(self, username="mgr_tg", uid=555, first_name="New", last_name=""):
        self.username = username
        self.id = uid
        self.first_name = first_name
        self.last_name = last_name


def _screenshot_row_full(db_path: str, key: str) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM manager_screenshot_config WHERE manager_key=?", (key,)).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _seed_screenshot_row(db_path: str, key: str, *, require: int, mode: str, reminder_enabled: int, reminder_time: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS manager_screenshot_config("
            "manager_key TEXT PRIMARY KEY, require_screenshots INTEGER NOT NULL DEFAULT 0, "
            "mode TEXT NOT NULL DEFAULT 'off', reminder_enabled INTEGER NOT NULL DEFAULT 1, "
            "reminder_time TEXT NOT NULL DEFAULT '16:50', last_reminder_date TEXT NOT NULL DEFAULT '', "
            "updated_at TEXT NOT NULL DEFAULT '')"
        )
        con.execute(
            "INSERT OR REPLACE INTO manager_screenshot_config"
            "(manager_key, require_screenshots, mode, reminder_enabled, reminder_time, updated_at) VALUES(?,?,?,?,?,?)",
            (key, require, mode, reminder_enabled, reminder_time, "2026-01-01T00:00:00"),
        )
        con.commit()
    finally:
        con.close()


def _seed_source_link(db_path: str, manager_key: str, source_key: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS manager_source_links("
            "manager_key TEXT PRIMARY KEY, source_key TEXT, created_at TEXT, updated_at TEXT)"
        )
        con.execute(
            "INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at) VALUES(?,?,?,?)",
            (manager_key, source_key, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        con.commit()
    finally:
        con.close()


def run_finalize_login_checks(tmp_db: str) -> None:
    print("\n-- main.py: _manager_finalize_login (REAL execution via AST) --")

    # Pre-create the (empty) manager_source_links table so _partner_source_key_for_manager's
    # real query has something to hit before _seed_source_link() adds the first row below --
    # purely test hygiene (avoids a harmless-but-noisy "no such table" print from that
    # function's own error logging); behavior is identical either way (returns "").
    con = sqlite3.connect(tmp_db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS manager_source_links("
        "manager_key TEXT PRIMARY KEY, source_key TEXT, created_at TEXT, updated_at TEXT)"
    )
    con.commit()
    con.close()

    state = {"managers": {}, "spawn_ok": True}

    async def _fake_manager_get(k):
        return dict(state["managers"].get(k, {}))

    async def _fake_manager_set_fields(key, **fields):
        state["managers"].setdefault(key, {"manager_key": key})
        state["managers"][key].update(fields)

    async def _fake_spawn_manager_process(key):
        return (state["spawn_ok"], "" if state["spawn_ok"] else "spawn failed")

    def _fake_manager_label_from_row(row):
        return f"label:{(row or {}).get('manager_key', '')}"

    def _fake_manager_runtime_paths_for_key(key):
        return {"session_path": f"/s/{key}", "db_path": f"/d/{key}", "root": f"/r/{key}", "log_path": f"/l/{key}"}

    def _fake_manager_runtime_onboarding_clear(uid):
        pass

    async def _fake_manager_delete_onboarding(uid):
        pass

    def _fake_now_utc_iso():
        return "2026-07-11T00:00:00"

    ns = extract_and_exec(
        MAIN_PY,
        {"_manager_finalize_login", "_tp_finalize_screenshots_on", "_partner_source_key_for_manager"},
        {
            **_COMMON_TYPE_NS,
            "registry_normalize_manager_key": normalize_manager_key,
            "_manager_runtime_paths_for_key": _fake_manager_runtime_paths_for_key,
            "manager_get": _fake_manager_get,
            "manager_set_fields": _fake_manager_set_fields,
            "_now_utc_iso": _fake_now_utc_iso,
            "_manager_runtime_onboarding_clear": _fake_manager_runtime_onboarding_clear,
            "manager_delete_onboarding": _fake_manager_delete_onboarding,
            "_spawn_manager_process": _fake_spawn_manager_process,
            "_manager_label_from_row": _fake_manager_label_from_row,
            "_ONBOARDING_SOURCE_PICK_MARKER": "В какой источник определить менеджера?",
            "aiosqlite": aiosqlite,
            "TPILOT_DB_PATH": tmp_db,
        },
    )
    finalize = ns["_manager_finalize_login"]

    # 1. successful finalize sets screenshots ON/instant for a brand-new manager.
    result1 = asyncio.run(finalize(1, "newmgr", "+10000000000", _FakeMe()))
    row1 = _screenshot_row_full(tmp_db, "newmgr")
    check("1. successful finalize sets screenshots ON/instant for the manager",
          int(row1.get("require_screenshots") or 0) == 1 and row1.get("mode") == "instant", repr(row1))
    check("finalize result reports success", "Менеджер активирован и запущен: newmgr" in result1, result1)
    check("4. source-pick marker appears after successful login for an unlinked manager",
          "В какой источник определить менеджера?" in result1, result1)

    # 2. re-login with an existing screenshot row set OFF becomes ON/instant, but
    # unrelated fields (reminder_enabled/reminder_time) are preserved.
    _seed_screenshot_row(tmp_db, "existingmgr", require=0, mode="off", reminder_enabled=0, reminder_time="10:00")
    asyncio.run(finalize(1, "existingmgr", "+10000000001", _FakeMe(username="existing_tg", uid=556)))
    row2 = _screenshot_row_full(tmp_db, "existingmgr")
    check("2. re-login with an existing screenshot row set OFF becomes ON/instant",
          int(row2.get("require_screenshots") or 0) == 1 and row2.get("mode") == "instant", repr(row2))
    check("2b. unrelated fields (reminder_enabled/reminder_time) are preserved on re-login",
          int(row2.get("reminder_enabled") or 0) == 0 and row2.get("reminder_time") == "10:00", repr(row2))

    # 3. unrelated managers are not modified by another manager's finalize.
    _seed_screenshot_row(tmp_db, "untouched_mgr", require=0, mode="off", reminder_enabled=1, reminder_time="16:50")
    asyncio.run(finalize(1, "newmgr", "+10000000000", _FakeMe()))  # re-run for a DIFFERENT, already-processed manager
    row3 = _screenshot_row_full(tmp_db, "untouched_mgr")
    check("3. unrelated managers are not modified by another manager's finalize",
          int(row3.get("require_screenshots") or 0) == 0 and row3.get("mode") == "off", repr(row3))

    # 5. an already-linked manager does not get a duplicate source-pick prompt,
    # but screenshots are still forced ON for that re-login.
    _seed_source_link(tmp_db, "linkedmgr", "srcA")
    result_linked = asyncio.run(finalize(1, "linkedmgr", "+10000000002", _FakeMe(username="linked_tg", uid=557)))
    check("5. an already-linked manager does not get a duplicate source-pick prompt",
          "В какой источник определить менеджера?" not in result_linked, result_linked)
    row_linked = _screenshot_row_full(tmp_db, "linkedmgr")
    check("screenshots are still forced ON/instant on an already-linked re-login",
          int(row_linked.get("require_screenshots") or 0) == 1 and row_linked.get("mode") == "instant", repr(row_linked))

    # Screenshot-config write failure must never fail the login (best-effort only).
    state_fail_ns = extract_and_exec(
        MAIN_PY,
        {"_manager_finalize_login", "_partner_source_key_for_manager"},
        {
            **_COMMON_TYPE_NS,
            "registry_normalize_manager_key": normalize_manager_key,
            "_manager_runtime_paths_for_key": _fake_manager_runtime_paths_for_key,
            "manager_get": _fake_manager_get,
            "manager_set_fields": _fake_manager_set_fields,
            "_now_utc_iso": _fake_now_utc_iso,
            "_manager_runtime_onboarding_clear": _fake_manager_runtime_onboarding_clear,
            "manager_delete_onboarding": _fake_manager_delete_onboarding,
            "_spawn_manager_process": _fake_spawn_manager_process,
            "_manager_label_from_row": _fake_manager_label_from_row,
            "_ONBOARDING_SOURCE_PICK_MARKER": "В какой источник определить менеджера?",
            "aiosqlite": aiosqlite,
            "TPILOT_DB_PATH": tmp_db,
            "_tp_finalize_screenshots_on": _raise_always,
        },
    )
    result_fail = asyncio.run(state_fail_ns["_manager_finalize_login"](1, "failmgr", "+10000000003", _FakeMe(username="fail_tg", uid=558)))
    check("screenshot-config write failure never fails the login (still reports success)",
          "Менеджер активирован и запущен: failmgr" in result_fail, result_fail)


async def _raise_always(*a, **kw):
    raise RuntimeError("simulated screenshot config write failure")


def run_failed_login_never_finalizes_checks() -> None:
    print("\n-- Structural: failed-login branches never call _manager_finalize_login --")
    for fname in ("_panel_manager_code_command", "_panel_manager_pass_command", "_manager_qr_wait_task"):
        defs = find_defs(MAIN_PY, fname)
        check(f"{fname} has at least one top-level def", len(defs) >= 1, fname)
        if not defs:
            continue
        node = defs[-1]
        bad_hops = []
        for n in ast.walk(node):
            if isinstance(n, ast.ExceptHandler):
                for call in ast.walk(n):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                            and call.func.id == "_manager_finalize_login"):
                        bad_hops.append(fname)
        check(f"6. {fname}'s exception/error branches never call _manager_finalize_login "
              "(a failed login never reaches the source-pick/screenshot logic)",
              not bad_hops, repr(bad_hops))


# ======================================================================
# GROUP B -- panel_bot.py: source-pick buttons, sync wizard rendering, QR
# notification-loop rendering (REAL execution via AST + FakeButton).
# ======================================================================

def run_source_pick_buttons_checks() -> None:
    print("\n-- panel_bot.py: _source_pick_buttons / notification buttons (REAL execution via AST) --")

    # TPILOT SOURCE PICK CALLBACK SIZE FIX 20260719 (Part H): traffic_sources'
    # real rows now carry an "id" (SQLite rowid alias, see _traffic_sources_
    # rows) and _source_pick_buttons resolves manager_id via a fresh
    # _manager_row_by_key lookup -- both fakes below mirror that contract.
    def _fake_traffic_sources_rows():
        return [
            {"id": 1, "source_key": "seo", "name": "SEO", "status": "active"},
            {"id": 2, "source_key": "vk", "name": "VK", "status": "active"},
            {"id": 3, "source_key": "old", "name": "Old", "status": "disabled"},
        ]

    def _fake_manager_row_by_key(key):
        return {"newmgr": {"id": 42, "manager_key": "newmgr"},
                "qrmgr": {"id": 77, "manager_key": "qrmgr"}}.get(str(key or "").strip().lower(), {})

    def _fake_back_to_panel_buttons():
        return [[FakeButton.inline("Назад", b"panel:back")]]

    ns = extract_and_exec(
        PANEL_BOT_PY,
        {"_source_pick_buttons", "_manager_source_pick_notification_buttons"},
        {
            **_COMMON_TYPE_NS,
            "normalize_manager_key": normalize_manager_key,
            "_traffic_sources_rows": _fake_traffic_sources_rows,
            "_manager_row_by_key": _fake_manager_row_by_key,
            "_back_to_panel_buttons": _fake_back_to_panel_buttons,
            "Button": FakeButton,
            "re": re,
        },
    )
    fn = ns["_source_pick_buttons"]
    rows = fn("newmgr")
    callbacks = button_callbacks(rows)

    check("9. source buttons use the numeric 'srcpick:<source_id>:<manager_id>' callback "
          "(never the raw source_key/manager_key, which have no enforced length cap and "
          "could otherwise push callback_data past Telegram's 64-byte limit)",
          "srcpick:1:42" in callbacks and "srcpick:2:42" in callbacks, repr(callbacks))
    check("only ACTIVE sources are offered (a disabled source is excluded)",
          "srcpick:3:42" not in callbacks, repr(callbacks))
    check("10. a skip button exists with a safe callback to menu/main",
          "menu:main" in callbacks, repr(callbacks))

    fn_notif = ns["_manager_source_pick_notification_buttons"]
    notif_rows = fn_notif("Успешно залогинен. В какой источник...\nmanager_key:qrmgr")
    notif_callbacks = button_callbacks(notif_rows)
    check("7. QR async notification (kind=manager_source_pick) renders source buttons, "
          "manager_key parsed safely from the notification body",
          "srcpick:1:77" in notif_callbacks, repr(notif_callbacks))

    notif_rows_bad = fn_notif("no manager key line here")
    check("notification buttons fall back to back-to-panel on an unparseable body (never crashes)",
          button_callbacks(notif_rows_bad) == button_callbacks(_fake_back_to_panel_buttons()), repr(notif_rows_bad))


def run_sync_wizard_structural_checks() -> None:
    print("\n-- Structural: sync wizard (code/pass/qr) renders source-pick buttons on marker --")
    defs = find_defs(PANEL_BOT_PY, "panel_wizard_input")
    check("panel_wizard_input is defined exactly once", len(defs) == 1, f"count={len(defs)}")
    if not defs:
        return
    body_src = ast.unparse(defs[-1])
    render_calls = body_src.count("_send_text_result_with_source_pick(")
    check("8. sync wizard renders source-pick buttons via _send_text_result_with_source_pick "
          "at exactly 3 call sites (code/pass/qr steps)",
          render_calls == 3, f"count={render_calls}")
    guard_calls = body_src.count("_ONBOARDING_SOURCE_PICK_MARKER in text")
    check("each source-pick render is gated on the marker actually being present in the result text",
          guard_calls == 3, f"count={guard_calls}")


def run_notification_loop_wiring_check() -> None:
    print("\n-- Structural: _panel_notification_loop routes kind=manager_source_pick --")
    defs = find_defs(PANEL_BOT_PY, "_panel_notification_loop")
    check("_panel_notification_loop is defined exactly once", len(defs) == 1, f"count={len(defs)}")
    if not defs:
        return
    body_src = ast.unparse(defs[-1])
    check("_panel_notification_loop dispatches kind=='manager_source_pick' to "
          "_manager_source_pick_notification_buttons",
          "manager_source_pick" in body_src and "_manager_source_pick_notification_buttons(" in body_src,
          body_src[:300])
    # Existing partner/proxy notification branches must be untouched (still present).
    # ast.unparse normalizes string literals to single quotes, so match on the bare
    # kind text rather than a specific quote style.
    for existing_kind, existing_fn in (
        ("partner_request", "_partner_request_notification_buttons"),
        ("proxy_renew_warning", "_prenew_notification_buttons"),
        ("proxy_autorenew_failed", "_prenew_autorenew_failed_buttons"),
    ):
        check(f"existing notification branch kind=={existing_kind!r} -> {existing_fn} is unchanged",
              existing_kind in body_src and f"{existing_fn}(" in body_src)


def run_marker_sync_check() -> None:
    print("\n-- Safety: onboarding marker stays byte-identical between main.py and panel_bot.py --")
    main_src = MAIN_PY.read_text(encoding="utf-8-sig")
    panel_src = PANEL_BOT_PY.read_text(encoding="utf-8-sig")
    m1 = re.search(r'_ONBOARDING_SOURCE_PICK_MARKER\s*=\s*"([^"]*)"', main_src)
    m2 = re.search(r'_ONBOARDING_SOURCE_PICK_MARKER\s*=\s*"([^"]*)"', panel_src)
    check("_ONBOARDING_SOURCE_PICK_MARKER is defined in both main.py and panel_bot.py",
          bool(m1) and bool(m2), (bool(m1), bool(m2)))
    if m1 and m2:
        check("the marker literal is byte-identical in both files (cross-process detection depends on this)",
              m1.group(1) == m2.group(1), (m1.group(1), m2.group(1)))


# ======================================================================
# GROUP C -- main.py /source link: schedule status line, closer filter intact.
# ======================================================================

def run_source_link_schedule_status_checks(tmp_db: str) -> None:
    print("\n-- main.py: /source link schedule status line (REAL execution via AST) --")

    async def _fake_manager_get(k):
        return {"manager_key": k, "status": "active"}

    ns = extract_and_exec(
        MAIN_PY,
        {"_handle_source_command", "_structure_key", "_ensure_sources_groups_tables", "_source_get", "_manager_key_exists"},
        {
            **_COMMON_TYPE_NS,
            "registry_normalize_manager_key": normalize_manager_key,
            "re": re, "os": os, "aiosqlite": aiosqlite, "TPILOT_DB_PATH": tmp_db,
            "manager_get": _fake_manager_get,
            "_now_utc_iso": lambda: "2026-07-11T00:00:00",
        },
    )
    fn = ns["_handle_source_command"]

    con = sqlite3.connect(tmp_db)
    con.execute(
        "CREATE TABLE IF NOT EXISTS traffic_sources(source_key TEXT PRIMARY KEY, name TEXT, "
        "description TEXT, status TEXT, created_at TEXT, updated_at TEXT)"
    )
    con.execute("INSERT INTO traffic_sources(source_key, name, status, created_at, updated_at) VALUES('seo','SEO','active','x','x')")
    con.execute(
        "CREATE TABLE IF NOT EXISTS manager_source_links("
        "manager_key TEXT PRIMARY KEY, source_key TEXT, created_at TEXT, updated_at TEXT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS managers(manager_key TEXT PRIMARY KEY, role TEXT)")
    con.execute("INSERT INTO managers(manager_key, role) VALUES('closermgr','closer')")
    con.commit()
    con.close()

    result_no_sched = asyncio.run(fn("link seo mgra", user_id=1))
    check("11. /source link response includes a schedule status line when the source has NO "
          "configured schedule, and does not hardcode 08:00-17:00",
          "не настроен график" in result_no_sched and "08:00" not in result_no_sched and "17:00" not in result_no_sched,
          result_no_sched)

    storage.source_work_schedule_set(
        "seo", mon=1, tue=1, wed=1, thu=1, fri=1, sat=0, sun=0, enabled=1,
        updated_by_user_id=1, db_path=tmp_db,
    )
    result_with_sched = asyncio.run(fn("link seo mgrb", user_id=1))
    check("11b. /source link response confirms inheritance when the source HAS a configured/enabled schedule",
          "наследует график источника" in result_with_sched, result_with_sched)

    result_closer = asyncio.run(fn("link seo closermgr", user_id=1))
    check("12. closer filter still works: a manager with role=closer is refused a source link",
          result_closer.strip() == "Клоузеры не привязываются к источникам.", repr(result_closer))

    # No manager schedule rows were ever created by /source link -- inheritance is
    # read-only, per the owner decision (existing manager_effective_is_working
    # naturally reads source_work_schedule; nothing is duplicated per-manager).
    con = sqlite3.connect(tmp_db)
    try:
        cur = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='manager_work_schedule_days'"
        )
        table_exists = cur.fetchone()[0] > 0
        rowcount = 0
        if table_exists:
            rowcount = con.execute("SELECT COUNT(*) FROM manager_work_schedule_days").fetchone()[0]
    finally:
        con.close()
    check("/source link never creates manager_work_schedule_days rows (inheritance stays read-only)",
          rowcount == 0, f"table_exists={table_exists} rowcount={rowcount}")


# ======================================================================
# GROUP D -- panel_bot.py proxy PIN reveal: one-line host:port:login:password;
# every other proxy display still masks the password.
# ======================================================================

def run_proxy_reveal_checks(tmp_db: str) -> None:
    print("\n-- panel_bot.py: proxy PIN reveal one-line format (REAL execution via AST) --")

    ns = extract_and_exec(
        PANEL_BOT_PY,
        {"_ppool_reveal_creds_once", "_connect_panel_db"},
        {**_COMMON_TYPE_NS, "sqlite3": sqlite3, "os": os, "TPILOT_DB_PATH": tmp_db},
    )
    fn = ns["_ppool_reveal_creds_once"]

    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE IF NOT EXISTS proxy_leases(id INTEGER PRIMARY KEY, host TEXT, port INTEGER, login TEXT, password TEXT)")
    con.execute("INSERT INTO proxy_leases(id, host, port, login, password) VALUES(1,'89.248.68.227',62913,'VADQiD83','4fk5w3Ns')")
    con.execute("INSERT INTO proxy_leases(id, host, port, login, password) VALUES(2,'1.2.3.4',1080,'','')")
    con.commit()
    con.close()

    creds = fn(1)
    check("13. proxy PIN reveal helper returns host/port/login/password for a lease with a password",
          creds == {"host": "89.248.68.227", "port": "62913", "login": "VADQiD83", "password": "4fk5w3Ns"}, repr(creds))
    row = ":".join(str(v) for v in (creds["host"], creds["port"], creds["login"], creds["password"]))
    check("13b. the assembled row matches the required host:port:login:password format",
          row == "89.248.68.227:62913:VADQiD83:4fk5w3Ns", row)

    check("a lease with no password returns None (nothing to reveal)", fn(2) is None, repr(fn(2)))
    check("a nonexistent lease id returns None", fn(999) is None, repr(fn(999)))


def run_proxy_mask_unaffected_checks() -> None:
    print("\n-- Safety: non-PIN proxy displays still mask the password --")
    defs = find_defs(PANEL_BOT_PY, "_proxy_detail_text")
    check("_proxy_detail_text has at least one def (uses the LAST/active one below, "
          "per this project's override-stack convention)", len(defs) >= 1, f"count={len(defs)}")
    if defs:
        body_src = ast.unparse(defs[-1])
        check("14. _proxy_detail_text (non-PIN proxy card) still masks the password (**** placeholder) "
              "and never calls the PIN-reveal helper",
              "****" in body_src and "_ppool_reveal_creds_once" not in body_src, body_src[:200])

    defs_main = find_defs(MAIN_PY, "_manager_proxy_info_text")
    check("_manager_proxy_info_text has at least one def", len(defs_main) >= 1, f"count={len(defs_main)}")
    if defs_main:
        active_body = ast.unparse(defs_main[-1])
        check("14b. main.py's ACTIVE _manager_proxy_info_text still masks the password "
              "and never calls the PIN-reveal helper",
              ("_mask_secret" in active_body or "****" in active_body)
              and "_ppool_reveal_creds_once" not in active_body, active_body[:300])


# ======================================================================
# GROUP E -- safety / scope (structural + regression-style)
# ======================================================================

def run_safety_checks(tmp_db: str) -> None:
    print("\n-- Safety / scope --")
    main_src = MAIN_PY.read_text(encoding="utf-8-sig")
    panel_src = PANEL_BOT_PY.read_text(encoding="utf-8-sig")
    storage_src = (BASE_DIR / "storage.py").read_text(encoding="utf-8-sig")

    def _allow_spend_sites(src: str) -> list:
        tree = ast.parse(src)
        return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                for kw in (n.keywords or [])
                if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]

    check("15. allow_spend=True is still exactly 2 real call-keyword sites, both in main.py",
          len(_allow_spend_sites(main_src)) == 2 and len(_allow_spend_sites(panel_src)) == 0
          and len(_allow_spend_sites(storage_src)) == 0)

    for name, src in (("main.py", main_src), ("panel_bot.py", panel_src)):
        moji = sum(src.count(c) for c in ("Ð", "Ñ")) + src.count("â€")
        check(f"mojibake clean: {name}", moji == 0, str(moji))

    real_path = os.path.realpath(tmp_db)
    check("16. selftest used ONLY temp SQLite files (never db/data_tpilot.db or db/data.db)",
          os.path.realpath(tempfile.gettempdir()) in real_path
          and "data_tpilot.db" not in real_path and (os.sep + "db" + os.sep) not in real_path, real_path)

    self_src = Path(__file__).read_text(encoding="utf-8-sig")
    check("17. selftest makes no network/Telethon/API calls (no requests/telethon/urllib/anthropic imports)",
          not re.search(r"^\s*(import|from)\s+(requests|telethon|urllib|http\.client|anthropic)\b", self_src, re.MULTILINE))

    # Proxy purchase/renewal logic itself must be untouched by this patch.
    for fname in ("_handle_manager_proxy_buy_confirm_command", "_prenew_execute_renewal"):
        found = find_defs(MAIN_PY, fname)
        check(f"{fname} is still defined exactly once (proxy buy/renewal untouched)", len(found) == 1, f"count={len(found)}")
        if found:
            body_src = ast.unparse(found[-1])
            check(f"{fname} contains no onboarding-source-pick code (scope stayed narrow)",
                  "_ONBOARDING_SOURCE_PICK_MARKER" not in body_src and "_source_pick_buttons" not in body_src)


def main() -> int:
    tmp_db = tempfile.mktemp(suffix="_onboarding_source_pick_selftest.db")
    try:
        run_finalize_login_checks(tmp_db)
        run_failed_login_never_finalizes_checks()
        run_source_pick_buttons_checks()
        run_sync_wizard_structural_checks()
        run_notification_loop_wiring_check()
        run_marker_sync_check()
        run_source_link_schedule_status_checks(tmp_db)
        run_proxy_reveal_checks(tmp_db)
        run_proxy_mask_unaffected_checks()
        run_safety_checks(tmp_db)
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
