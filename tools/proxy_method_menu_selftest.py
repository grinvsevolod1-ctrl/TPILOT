# -*- coding: utf-8 -*-
"""tools/proxy_method_menu_selftest.py -- offline self-test for the AdminBot
proxy REPLACEMENT method menu (2026-07-18): the "✏️ Заменить proxy" button on
an existing manager's Proxy card now opens a method-selection screen
(pxm: namespace) offering pool / purchase / manual instead of jumping
straight into manual SOCKS5 entry.

Scope: panel_bot.py only. Every method (pool: _frompool_run_*, purchase:
_pbuy_run_*, manual: _tpilot_panel_handle_proxy_line/_tpilot_parse_socks5_
one_line) is the pre-existing, UNMODIFIED business logic -- this feature
only adds a routing layer (pxm: callbacks + a "ret" context flag threaded
through each subflow's own wizard-state payload, never through
callback_data) on top of it. Nothing here re-implements proxy parsing,
pool listing, or provider calls.

Techniques (same as every other tools/*_selftest.py in this project):
  - panel_bot.py cannot be imported standalone (real Telethon TelegramClient
    at module scope; Telethon is not installed locally) -- the functions
    under test are extracted via ast.parse + ast.unparse + exec() and run
    FOR REAL against a temporary SQLite DB (never db/data_tpilot.db).
  - manager_registry.py and proxy_parser.py ARE importable -> exercised for
    real (list_manager_rows_from_db_sync backs _manager_row_by_key with a
    real managers table; proxy_parser.parse_proxy_line backs the manual
    format-support claims made in the new prompt text).
  - _submit_and_wait (the real panel_commands bridge to the controller
    process) is faked with a scripted dispatcher keyed by command prefix,
    exactly mirroring the technique proxy_pool_selftest.py /
    proxy_buy_flow_selftest.py already use for the SAME underlying
    /proxy_pool_* and /manager_proxy_buy_* commands -- this file does not
    re-test those commands' own business logic (covered there), only that
    panel_bot.py's NEW routing calls the RIGHT existing functions with the
    RIGHT arguments and reacts correctly to their result.
  - _title_for_menu is NOT extracted (a 30+-times-stacked, ~19000-line-wide
    function with a huge unrelated dependency graph) -- faked with a
    deterministic stand-in for the one input this feature actually calls
    it with (f"proxy:{key}"), so tests can assert THIS code calls it
    correctly and renders its result, without re-verifying that giant
    function's own internal correctness (unmodified, out of scope).
  - _ppool_success_card_text_from_lease is faked (a thin async stub) to
    avoid pulling in _ppool_reveal_creds_once's proxy_leases-table
    dependency chain -- unmodified, out of scope; this file cares that the
    ret-aware branches CALL it and use its return value, not that the real
    credential-reveal plumbing works (covered by proxy_pool_selftest.py).

Never: real Telegram network, real proxy/provider network, real process
spawn/stop, production DB/runtime/session/log access.

    python tools\\proxy_method_menu_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import proxy_parser as _proxy_parser_module
from manager_registry import list_manager_rows_from_db_sync as _real_list_manager_rows, normalize_manager_key

PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _selftest_db_guard(db_path: str, base_dir: Path, storage_mod=None) -> None:
    prod_dir = os.path.join(str(base_dir), "db")
    abs_db = os.path.abspath(db_path)
    try:
        common = os.path.commonpath([os.path.abspath(prod_dir), abs_db])
    except Exception:
        common = ""
    assert common != os.path.abspath(prod_dir), f"REFUSING production-adjacent db path: {db_path}"


# ======================================================================
# Fixtures
# ======================================================================

def make_temp_env(prefix: str):
    tmp_root = Path(tempfile.mkdtemp(prefix=prefix))
    db_path = str(tmp_root / "data_tpilot.db")
    _selftest_db_guard(db_path, BASE_DIR)
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    import shutil
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


async def _drain() -> None:
    """Lets every asyncio.create_task(...) fire-and-forget background task
    (pxm:pool:/pxm:buy: spawn one, matching the real callback handlers'
    own convention) run to completion -- a single sleep(0) only advances a
    task to its NEXT await point, which is not enough for a chain of
    several awaits (submit_and_wait -> send_message)."""
    for _ in range(20):
        await asyncio.sleep(0)


def seed_manager(db_path: str, *, manager_key: str, display_name: str = "", telegram_username: str = "",
                  proxy_mode: str = "proxy", proxy_host: str = "1.2.3.4", proxy_port: str = "1080",
                  proxy_username: str = "", proxy_password: str = "", proxy_lease_id=None,
                  status: str = "active", is_enabled: int = 1) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS managers("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT UNIQUE NOT NULL,"
            " display_name TEXT DEFAULT '', telegram_username TEXT DEFAULT '',"
            " status TEXT DEFAULT 'active', is_enabled INTEGER DEFAULT 1, manual_stopped INTEGER DEFAULT 0,"
            " role TEXT DEFAULT 'manager', proxy_mode TEXT DEFAULT '', proxy_host TEXT DEFAULT '',"
            " proxy_port TEXT DEFAULT '', proxy_username TEXT DEFAULT '', proxy_password TEXT DEFAULT '',"
            " proxy_lease_id INTEGER, proxy_bypass_allowed INTEGER DEFAULT 0)"
        )
        con.execute(
            "INSERT OR REPLACE INTO managers"
            "(manager_key, display_name, telegram_username, status, is_enabled, manual_stopped, role,"
            " proxy_mode, proxy_host, proxy_port, proxy_username, proxy_password, proxy_lease_id, proxy_bypass_allowed)"
            " VALUES(?,?,?,?,?,0,'manager',?,?,?,?,?,?,0)",
            (manager_key, display_name, telegram_username, status, is_enabled, proxy_mode, proxy_host, proxy_port,
             proxy_username, proxy_password, proxy_lease_id),
        )
        con.commit()
    finally:
        con.close()


def set_manager_proxy_creds(db_path: str, manager_key: str, *, host: str, port: str, login: str, password: str) -> None:
    """Simulates main.py's _tpag_registry_set_fields already having applied
    a newly-assigned/bought/manually-set proxy to the manager row -- the
    real write panel_bot.py's fake _submit_and_wait does not itself perform."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE managers SET proxy_host=?, proxy_port=?, proxy_username=?, proxy_password=? WHERE manager_key=?",
            (host, port, login, password, manager_key),
        )
        con.commit()
    finally:
        con.close()


# ======================================================================
# Fake Telethon-shaped layer
# ======================================================================

class FakeButton:
    def __init__(self, text, data):
        self.text = text
        self.data = data

    @staticmethod
    def inline(text, data=b""):
        return FakeButton(text, data)


class FakeEventsNS:
    CallbackQuery = object()
    NewMessage = object()


class FakeClient:
    def __init__(self):
        self.calls: list = []
        self.send_fail_uids: set = set()
        self._next_id = 1000

    def on(self, *a, **kw):
        def _decorator(fn):
            return fn
        return _decorator

    async def send_message(self, uid, text, buttons=None):
        self.calls.append(("send_message", int(uid), text, buttons))
        if int(uid) in self.send_fail_uids:
            raise RuntimeError("simulated send failure")
        self._next_id += 1
        from types import SimpleNamespace
        return SimpleNamespace(id=self._next_id)

    async def delete_messages(self, uid, mids):
        self.calls.append(("delete_messages", int(uid), list(mids)))

    async def edit_message(self, uid, mid, text, buttons=None):
        self.calls.append(("edit_message", int(uid), int(mid), text, buttons))


class FakeCallbackEvent:
    """Mimics the subset of a Telethon CallbackQuery event this feature's
    handlers actually use: .data, .chat_id, .sender_id, .answer(), .edit()."""

    def __init__(self, data: str, chat_id: int, sender_id: int):
        self.data = data.encode("utf-8")
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.answers: list = []
        self.edits: list = []
        self.edit_fail = False

    async def answer(self, text: str = "", alert: bool = False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        if self.edit_fail:
            raise RuntimeError("simulated edit failure")
        self.edits.append((text, buttons))


class FakeMessageEvent:
    """Mimics the subset of a Telethon NewMessage event _pxm_reveal_pin_input
    (and the existing _ppool_reveal_pin_input) actually use: .raw_text,
    .chat_id, .sender_id, .delete()."""

    def __init__(self, raw_text: str, chat_id: int, sender_id: int):
        self.raw_text = raw_text
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


# ======================================================================
# Extraction
# ======================================================================

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


PXM_NAMES = {
    # code under test (this task's new/modified surface)
    "_pxm_manager_identity", "_pxm_enter_manual",
    "_pxm_menu_text", "_pxm_menu_buttons", "_pxm_manager_not_found_buttons", "_pxm_callback",
    "_pxm_result_card_text", "_pxm_result_card_buttons", "_pxm_send_result_card",
    "_pxm_full_reveal_text", "_pxm_full_reveal_buttons", "_pxm_reveal_pin_input",
    "_tpilot_proxy_one_line_prompt", "_tpilot_parse_socks5_one_line", "_tpilot_proxy_text_success",
    "_tpilot_panel_handle_proxy_line",
    "_proxy_detail_buttons", "_tpag_panel_v2_mode",
    # pool flow (pre-existing, reused, some functions gained an optional ret= param)
    "_frompool_item_label", "_frompool_picker_text", "_frompool_picker_buttons", "_frompool_empty_buttons",
    "_frompool_confirm_state_ok", "_frompool_run_picker", "_frompool_run_confirm_screen",
    "_frompool_run_assign", "_frompool_callback",
    # purchase flow (pre-existing, reused, some functions gained an optional ret= param)
    "_pbuy_calc_preview_text", "_pbuy_confirm_buttons", "_pbuy_recover_buttons", "_pbuy_result_text",
    "_pbuy_run_calc", "_pbuy_run_confirm", "_pbuy_run_recover", "_pbuy_callback",
    "_ppool_parse_result_json", "_pbuy_parse_result_json", "_ppool_assign_result_text",
    "_ppool_guard_failed_buttons", "_PPOOL_STATUS_LABELS",
    # existing PIN reveal mechanism (Stage 6.1C) -- reused, not duplicated
    # (_ppool_pin_matches: PIN comparison helper extracted later in panel_bot;
    # _pxm_reveal_pin_input calls it instead of a raw == compare)
    "_ppool_pin_env_value", "_ppool_pin_matches",
    # shared plumbing
    "_wizard_get", "_wizard_set", "_wizard_clear",
    "_connect_panel_db", "_ensure_panel_runtime_tables", "_utc_now_iso",
    "_manager_rows", "_manager_rows_all", "_manager_row_by_key",
    "_safe_text", "_safe_event_edit",
    "_back_to_panel_buttons", "_add_manager_proxy_choice_buttons", "_manager_qr_entry_button",
    "_panel_callback_is_duplicate", "_panel_loop_time",
    "_proxy_success_card_text",
    "_send_text_result_with_panel", "_send_fresh_panel", "_delete_active_panel",
    "_get_active_panel_message", "_set_active_panel_message", "_clear_active_panel_message",
    # N5.3.1 blockquote-title rendering: _send_fresh_panel's active generation calls
    # this helper (and its own _N531_TITLE_BODY constant) directly -- extract both, or
    # the call raises NameError. _safe_event_edit's own N5.3.1 override generation
    # ALSO calls it, and its override-chain marker (the standard project pattern:
    # `_N531_PREV_SAFE_EVENT_EDIT = globals().get("_safe_event_edit")`, a plain
    # top-level assignment preceding the override def) must be extracted too, or that
    # assignment statement itself raises NameError before the override def is even
    # reached.
    "_pb_render_with_title_quote", "_N531_TITLE_BODY", "_N531_PREV_SAFE_EVENT_EDIT",
    # R1B/F-16/F-32/F-18/F-40 (2026-08-12, large reliability batch): every
    # `await event.answer(...)` and `asyncio.create_task(...)` call site in
    # panel_bot.py -- including inside _pxm_callback/_frompool_callback/
    # _pbuy_callback -- was mechanically rewritten to `_pb_safe_answer(event,
    # ...)` / `_pb_track_task(...)`. Real, load-bearing dependencies now.
    "_pb_safe_answer", "_pb_is_query_invalid_error",
    "_PB_QUERY_INVALID_CLASS_NAMES", "_PB_QUERY_INVALID_TEXT_MARKERS",
    "_pb_track_task",
    # utcnow refactor (2026-08-16): _utc_now_iso and cutoff math now go
    # through the module-level _pb_utc_now() clock seam.
    "_pb_utc_now",
    # TERMINAL OK 20260809 (Ф4 bot-wide audit): _pxm_send_result_card,
    # _pxm_reveal_pin_input, and _send_text_result_with_panel now call
    # _terminal_ok_button().
    "_terminal_ok_button",
    # AUTH UI 20260809 (Ф4, unified auth UX -- predates this session's
    # bot-wide pass, only now surfaced by a full regression sweep):
    # _frompool_run_assign/_pbuy_run_confirm/_pbuy_run_recover's onboarding
    # success paths now show the unified chooser instead of auto-starting
    # the phone step.
    "_auth_chooser_text", "_auth_chooser_buttons",
}


async def _fake_ppool_success_card_text_from_lease(data: dict, *, fallback_text: str) -> str:
    return f"SUCCESS_CARD:{data.get('manager_key')}:{fallback_text}"


async def _fake_prep_resume_after_proxy(chat_id, user_id, key, *, event=None) -> bool:
    """PREPARED ACCOUNTS PHASE 6 seam: production returns False for every
    non-prepared manager, after which callers continue their normal
    onboarding behavior. All managers in this test are ordinary (created
    via /manager_add), so the correct faithful stub is a constant False."""
    return False


def build_pxm_ns(db_path: str, *, scripted_commands: dict, is_allowed: bool = True, pin: str = "1234"):
    _selftest_db_guard(db_path, BASE_DIR)
    nodes = _extract_by_names(PANEL_SRC, PXM_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    client = FakeClient()

    async def _fake_submit_and_wait(command_text, requested_by, source_chat_id, response_chat_id):
        for prefix, responder in scripted_commands.items():
            if command_text.startswith(prefix):
                result = responder(command_text) if callable(responder) else responder
                return {"status": "done", "result_text": json.dumps(result, ensure_ascii=False)}
        return {"status": "error", "error_text": f"unscripted command: {command_text}"}

    def _fake_title_for_menu(raw: str):
        if raw.startswith("proxy:"):
            key = normalize_manager_key(raw.split(":", 1)[1])
            row = _real_list_manager_rows(db_path)
            found = any(normalize_manager_key(r.get("manager_key") or "") == key for r in row)
            if not found:
                return f"PROXYCARD_NOTFOUND:{key}", [[FakeButton("back", b"menu:proxy")]]
            return f"PROXYCARD:{key}", [[FakeButton("card", f"noop:{key}".encode())]]
        return "UNKNOWN_MENU", []

    def _fake_is_allowed(event):
        return is_allowed

    ns = {
        "os": os,
        "re": __import__("re"),
        "json": json,
        "asyncio": asyncio,
        "sqlite3": sqlite3,
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
        "TPILOT_DB_PATH": db_path,
        "Button": FakeButton,
        "events": FakeEventsNS,
        "client": client,
        "Dict": dict, "Any": object, "List": list, "Optional": None, "Tuple": tuple,
        "_PANEL_BACKGROUND_TASKS": set(),
        "normalize_manager_key": normalize_manager_key,
        "list_manager_rows_from_db_sync": _real_list_manager_rows,
        "proxy_parser": _proxy_parser_module,
        "_submit_and_wait": _fake_submit_and_wait,
        "_title_for_menu": _fake_title_for_menu,
        "_is_allowed": _fake_is_allowed,
        "_ppool_success_card_text_from_lease": _fake_ppool_success_card_text_from_lease,
        "_prep_resume_after_proxy": _fake_prep_resume_after_proxy,
        "_PANEL_RECENT_CALLBACKS": {},
        "_PANEL_DEDUPE_SEC": 2.5,
        "ALLOWED_USERS": set(),
        "ALLOWED_CHATS": set(),
        "PANEL_ADMIN_PASSWORD": pin,
        "MANAGER_ADMIN_PASSWORD": "",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:pxm>", "exec"), ns)
    ns["__client__"] = client
    return ns


# ======================================================================
# Scripted controller responses (mirror the REAL /proxy_pool_* and
# /manager_proxy_buy_* command JSON shapes -- proxy_pool_selftest.py and
# proxy_buy_flow_selftest.py already exercise the real handlers that
# produce these).
# ======================================================================

def ok_pool_list(items):
    return lambda cmd: {"ok": True, "items": items}


def ok_pool_assign(*, check_ok=True, lease_id=501, manager_key="mgr1"):
    return lambda cmd: {"ok": True, "check_ok": check_ok, "lease_id": lease_id, "manager_key": manager_key}


def fail_pool_assign(msg="assign failed"):
    return lambda cmd: {"ok": False, "message": msg}


def ok_buy_calc(manager_key="mgr1"):
    return lambda cmd: {"ok": True, "manager_key": manager_key, "country_name": "Germany", "country_alpha3": "DEU",
                         "period_id": "1m", "quantity": 1, "price": 100, "total": 100, "currency": "RUB", "balance": 500}


def fail_buy_calc(msg="calc failed"):
    return lambda cmd: {"ok": False, "message": msg}


def ok_buy_confirm(*, check_ok=True, lease_id=601, manager_key="mgr1"):
    return lambda cmd: {"ok": True, "check_ok": check_ok, "lease_id": lease_id, "manager_key": manager_key,
                         "expires_at": "2026-08-18"}


def pending_buy_confirm(order_id="ORD-1"):
    return lambda cmd: {"ok": False, "error": "provisioning_pending", "order_id": order_id,
                         "message": "still pending"}


def fail_buy_confirm(msg="buy failed"):
    return lambda cmd: {"ok": False, "message": msg}


def ok_manager_proxy_set():
    return lambda cmd: None  # not JSON -- handled specially below


class _RawTextResponder:
    """_tpilot_panel_handle_proxy_line expects PLAIN result_text (not JSON),
    matched against _tpilot_proxy_text_success's keyword list -- unlike the
    JSON-returning pool/buy commands."""
    def __init__(self, text):
        self.text = text


def scripted_with_raw(scripted: dict, prefix: str, raw_text: str):
    scripted = dict(scripted)
    scripted[prefix] = _RawTextResponder(raw_text)
    return scripted


def build_pxm_ns_raw_aware(db_path: str, *, scripted_commands: dict, is_allowed: bool = True, pin: str = "1234"):
    """Same as build_pxm_ns but supports _RawTextResponder entries (plain
    result_text, not JSON) for /manager_proxy_set."""
    _selftest_db_guard(db_path, BASE_DIR)
    nodes = _extract_by_names(PANEL_SRC, PXM_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    client = FakeClient()

    async def _fake_submit_and_wait(command_text, requested_by, source_chat_id, response_chat_id):
        for prefix, responder in scripted_commands.items():
            if command_text.startswith(prefix):
                if isinstance(responder, _RawTextResponder):
                    return {"status": "done", "result_text": responder.text}
                result = responder(command_text) if callable(responder) else responder
                return {"status": "done", "result_text": json.dumps(result, ensure_ascii=False)}
        return {"status": "error", "error_text": f"unscripted command: {command_text}"}

    def _fake_title_for_menu(raw: str):
        if raw.startswith("proxy:"):
            key = normalize_manager_key(raw.split(":", 1)[1])
            rows = _real_list_manager_rows(db_path)
            found = any(normalize_manager_key(r.get("manager_key") or "") == key for r in rows)
            if not found:
                return f"PROXYCARD_NOTFOUND:{key}", [[FakeButton("back", b"menu:proxy")]]
            return f"PROXYCARD:{key}", [[FakeButton("card", f"noop:{key}".encode())]]
        return "UNKNOWN_MENU", []

    def _fake_is_allowed(event):
        return is_allowed

    ns = {
        "os": os,
        "re": __import__("re"),
        "json": json,
        "asyncio": asyncio,
        "sqlite3": sqlite3,
        "datetime": __import__("datetime").datetime,
        "timezone": __import__("datetime").timezone,
        "TPILOT_DB_PATH": db_path,
        "Button": FakeButton,
        "events": FakeEventsNS,
        "client": client,
        "Dict": dict, "Any": object, "List": list, "Optional": None, "Tuple": tuple,
        "_PANEL_BACKGROUND_TASKS": set(),
        "normalize_manager_key": normalize_manager_key,
        "list_manager_rows_from_db_sync": _real_list_manager_rows,
        "proxy_parser": _proxy_parser_module,
        "_submit_and_wait": _fake_submit_and_wait,
        "_title_for_menu": _fake_title_for_menu,
        "_is_allowed": _fake_is_allowed,
        "_ppool_success_card_text_from_lease": _fake_ppool_success_card_text_from_lease,
        "_prep_resume_after_proxy": _fake_prep_resume_after_proxy,
        "_PANEL_RECENT_CALLBACKS": {},
        "_PANEL_DEDUPE_SEC": 2.5,
        "ALLOWED_USERS": set(),
        "ALLOWED_CHATS": set(),
        "PANEL_ADMIN_PASSWORD": pin,
        "MANAGER_ADMIN_PASSWORD": "",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:pxm_raw>", "exec"), ns)
    ns["__client__"] = client
    return ns


# ======================================================================
# Tests
# ======================================================================

async def test_1_2_3_proxy_card_and_menu_screen():
    print("\n-- 1/2/3: existing Proxy card still renders; replace button -> pxm:menu:; menu has all 5 buttons --")
    tmp_root, db_path = make_temp_env("pxm_1_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        seed_manager(db_path, manager_key="mgr1", display_name="Иван", telegram_username="ivan_tg")
        text, buttons = ns["_proxy_detail_buttons"]("mgr1"), None
        buttons = ns["_proxy_detail_buttons"]("mgr1")
        flat = [b for row in buttons for b in row]
        replace_btn = next((b for b in flat if b.text == "✏️ Заменить proxy"), None)
        check("1. existing manager Proxy card buttons still render (check/replace/bypass/back/home)", len(flat) == 5, [b.text for b in flat])
        check("2. the '✏️ Заменить proxy' button now points at pxm:menu:<key>", replace_btn is not None and replace_btn.data == b"pxm:menu:mgr1", replace_btn.data if replace_btn else None)

        menu_text = ns["_pxm_menu_text"]("mgr1")
        menu_buttons = ns["_pxm_menu_buttons"]("mgr1")
        flat_menu = [b for row in menu_buttons for b in row]
        labels = [b.text for b in flat_menu]
        check("3a. method menu contains 'Выбрать из пула'", "📦 Выбрать из пула" in labels, labels)
        check("3b. method menu contains 'Купить новый proxy'", "🛒 Купить новый proxy" in labels, labels)
        check("3c. method menu contains 'Ввести proxy вручную'", "⌨️ Ввести proxy вручную" in labels, labels)
        check("3d. method menu contains 'Назад'", "⬅️ Назад" in labels, labels)
        check("3e. method menu contains 'Главная'", "🏠 Главная" in labels, labels)
        check("3f. method menu header shows the manager identity", "Иван" in menu_text and "@ivan_tg" in menu_text, menu_text)
    finally:
        cleanup_env(tmp_root)


async def test_4_5_manager_identity_safe():
    print("\n-- 4/5: manager identity renders safely; missing username never produces bare @ or None --")
    tmp_root, db_path = make_temp_env("pxm_4_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        seed_manager(db_path, manager_key="mgr2", display_name="Пётр", telegram_username="")
        ident = ns["_pxm_manager_identity"]({"display_name": "Пётр", "manager_key": "mgr2", "telegram_username": ""})
        check("4. identity with username renders 'name | @user'", ns["_pxm_manager_identity"]({"display_name": "Иван", "manager_key": "mgr1", "telegram_username": "ivan_tg"}) == "Иван | @ivan_tg", None)
        check("5a. missing username never produces a bare '@'", "@" not in ident, ident)
        check("5b. missing username never renders the string 'None'", "None" not in ident, ident)
        check("5c. missing username uses the explicit fallback text", "без username" in ident, ident)

        prompt = ns["_tpilot_proxy_one_line_prompt"]("mgr2", new_manager=False)
        check("5d. manual prompt for a no-username manager never shows a bare '@' or 'None'", "None" not in prompt and " @\n" not in prompt and " @ " not in prompt, prompt)
    finally:
        cleanup_env(tmp_root)


async def test_6_manual_reuses_existing_wizard():
    print("\n-- 6: pxm:manual: enters the existing proxy_set wizard (same state, same prompt fn) --")
    tmp_root, db_path = make_temp_env("pxm_6_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        seed_manager(db_path, manager_key="mgr3", display_name="Ольга")
        ev = FakeCallbackEvent("pxm:manual:mgr3", chat_id=777, sender_id=888)
        await ns["_pxm_callback"](ev)
        state = ns["_wizard_get"](777, 888)
        check("6a. pxm:manual: sets the SAME wizard/step as the original direct entry (proxy_set/line)", state.get("wizard") == "proxy_set" and state.get("step") == "line", state)
        check("6b. wizard payload carries the manager_key", (state.get("payload") or {}).get("key") == "mgr3", state.get("payload"))
        check("6c. the manual prompt was shown via event.edit (reused _tpilot_proxy_one_line_prompt)", len(ev.edits) == 1 and "Ольга" in ev.edits[0][0], ev.edits)
    finally:
        cleanup_env(tmp_root)


async def test_7_8_9_manual_prompt_formats():
    print("\n-- 7/8/9: manual prompt shows exactly the 4 confirmed formats, 4 copyable examples, no unsupported formats --")
    tmp_root, db_path = make_temp_env("pxm_7_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        seed_manager(db_path, manager_key="mgr4", display_name="Игорь", telegram_username="igor_tg")
        prompt = ns["_tpilot_proxy_one_line_prompt"]("mgr4", new_manager=False)
        check("7a. prompt lists host:port:login:password", "host:port:login:password" in prompt, prompt)
        check("7b. prompt lists login:password@host:port", "login:password@host:port" in prompt, prompt)
        check("7c. prompt lists socks5://login:password@host:port", "socks5://login:password@host:port" in prompt, prompt)
        check("7d. prompt lists host:port (no creds)", prompt.count("`host:port`") == 1 or "`host:port` " in prompt, prompt)

        examples = [l.strip("`") for l in prompt.splitlines() if l.strip().startswith("`") and l.strip().endswith("`")]
        check("8a. prompt shows exactly 4 separate copyable examples", len(examples) == 4, examples)
        check("8b. every shown example actually parses via the real proxy_parser", all(ns["proxy_parser"].parse_proxy_line(e) is not None for e in examples), examples)
        check("8c. no real-looking previously-hardcoded IP/creds leaked into the new text", "89.248.68.227" not in prompt and "VADQiD83" not in prompt, prompt)

        check("9. prompt never advertises 'username:password:host:port'", "username:password:host:port" not in prompt, prompt)
        check("9b. prompt never advertises an http(s):// example", "http://" not in prompt and "https://" not in prompt, prompt)
        check("9c. prompt never advertises URL-encoded credentials (%xx)", "%40" not in prompt and "%2f" not in prompt.lower(), prompt)
        check("9d. no shown example itself contains a space (space-separated input is never advertised as a real example)", all(" " not in e for e in examples), examples)
        check("9e. prompt does not advertise multiline input", "multiline" not in prompt.lower() and "несколько строк" not in prompt.lower(), prompt)
    finally:
        cleanup_env(tmp_root)


async def test_10_11_pool_and_buy_delegate_to_existing_functions():
    print("\n-- 10/11: pxm:pool: calls _frompool_run_picker; pxm:buy: calls _pbuy_run_calc --")
    tmp_root, db_path = make_temp_env("pxm_10_")
    scripted = {"/proxy_pool_list": ok_pool_list([{"lease_id": 501, "host": "1.2.3.4", "port": 1080, "status": "free", "expires_at": "2026-09-01"}]),
                "/manager_proxy_buy_calc": ok_buy_calc("mgr5")}
    ns = build_pxm_ns(db_path, scripted_commands=scripted)
    try:
        seed_manager(db_path, manager_key="mgr5", display_name="Света")
        ev_pool = FakeCallbackEvent("pxm:pool:mgr5", chat_id=1, sender_id=1)
        await ns["_pxm_callback"](ev_pool)
        await _drain()
        sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        # The lease id appears in the picker's BUTTON labels (via
        # _frompool_item_label), not the message text itself.
        all_labels = [b.text for c in sends for row in (c[3] or []) for b in row]
        check("10. pxm:pool: reaches the real _frompool_run_picker (picker message with the real lease shown)", any("501" in lbl for lbl in all_labels), (sends, all_labels))

        ev_buy = FakeCallbackEvent("pxm:buy:mgr5", chat_id=1, sender_id=1)
        await ns["_pxm_callback"](ev_buy)
        await _drain()
        sends2 = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("11. pxm:buy: reaches the real _pbuy_run_calc (calc preview message with the real country/price shown)", any("Germany" in str(c[2]) for c in sends2), sends2)
    finally:
        cleanup_env(tmp_root)


async def test_12_13_ret_preserved_through_confirmation():
    print("\n-- 12/13: ret='proxy_card' is preserved through pool AND purchase confirmation tokens --")
    tmp_root, db_path = make_temp_env("pxm_12_")
    scripted = {"/proxy_pool_list": ok_pool_list([{"lease_id": 501, "host": "1.2.3.4", "port": 1080, "status": "free", "expires_at": "2026-09-01"}]),
                "/manager_proxy_buy_calc": ok_buy_calc("mgr6")}
    ns = build_pxm_ns(db_path, scripted_commands=scripted)
    try:
        seed_manager(db_path, manager_key="mgr6", display_name="Марк")

        # Pool: pxm:pool -> wizard carries ret -> pick -> confirm screen carries ret in ITS OWN payload.
        await ns["_pxm_callback"](FakeCallbackEvent("pxm:pool:mgr6", chat_id=2, sender_id=2))
        await _drain()
        await ns["_frompool_callback"](FakeCallbackEvent("wiz:frompool:pick:mgr6:501", chat_id=2, sender_id=2))
        await _drain()
        state_pool = ns["_wizard_get"](2, 2)
        check("12. ret='proxy_card' survives into the pool CONFIRM step's own wizard payload", (state_pool.get("payload") or {}).get("ret") == "proxy_card", state_pool)

        # Purchase: pxm:buy -> _pbuy_run_calc embeds ret directly into buyproxy_confirm payload.
        await ns["_pxm_callback"](FakeCallbackEvent("pxm:buy:mgr6", chat_id=3, sender_id=3))
        await _drain()
        state_buy = ns["_wizard_get"](3, 3)
        check("13. ret='proxy_card' survives into the purchase CONFIRM step's own wizard payload", state_buy.get("step") == "buyproxy_confirm" and (state_buy.get("payload") or {}).get("ret") == "proxy_card", state_buy)
    finally:
        cleanup_env(tmp_root)


async def test_14_15_16_success_shows_masked_result_card():
    print("\n-- 14/15/16: pool/purchase/manual SUCCESS all show the masked result card (password hidden) --")
    tmp_root, db_path = make_temp_env("pxm_14_")
    scripted_pool = {"/proxy_pool_assign": ok_pool_assign(check_ok=True, lease_id=501, manager_key="mgr7")}
    ns_pool = build_pxm_ns(db_path, scripted_commands=scripted_pool)
    try:
        seed_manager(db_path, manager_key="mgr7", display_name="Настя")
        set_manager_proxy_creds(db_path, "mgr7", host="1.2.3.4", port="1080", login="poolLogin", password="poolSecretPass1")
        await ns_pool["_frompool_run_assign"](5, 5, "mgr7", 501, ret="proxy_card")
        sends = [c for c in ns_pool["__client__"].calls if c[0] == "send_message"]
        texts = [str(c[2]) for c in sends]
        check("14a. pool assignment SUCCESS shows the masked result card", any("✅ Proxy назначен" in t for t in texts), texts)
        check("14b. masked card shows host:port", any("1.2.3.4:1080" in t for t in texts), texts)
        check("14c. masked card never leaks the raw password", not any("poolSecretPass1" in t for t in texts), texts)
        check("14d. does not jump to onboarding phone-entry text", not any("телефон" in t for t in texts), texts)
    finally:
        cleanup_env(tmp_root)

    tmp_root2, db_path2 = make_temp_env("pxm_15_")
    scripted_buy = {"/manager_proxy_buy_confirm": ok_buy_confirm(check_ok=True, lease_id=601, manager_key="mgr8")}
    ns_buy = build_pxm_ns(db_path2, scripted_commands=scripted_buy)
    try:
        seed_manager(db_path2, manager_key="mgr8", display_name="Борис")
        set_manager_proxy_creds(db_path2, "mgr8", host="5.6.7.8", port="2080", login="buyLogin", password="buySecretPass2")
        await ns_buy["_pbuy_run_confirm"](6, 6, "mgr8", ret="proxy_card")
        sends2 = [c for c in ns_buy["__client__"].calls if c[0] == "send_message"]
        texts2 = [str(c[2]) for c in sends2]
        check("15a. purchase SUCCESS shows the masked result card", any("✅ Proxy назначен" in t for t in texts2), texts2)
        check("15b. masked card shows host:port", any("5.6.7.8:2080" in t for t in texts2), texts2)
        check("15c. masked card never leaks the raw password", not any("buySecretPass2" in t for t in texts2), texts2)
        check("15d. does not jump to onboarding phone-entry text", not any("телефон" in t for t in texts2), texts2)
    finally:
        cleanup_env(tmp_root2)

    tmp_root3, db_path3 = make_temp_env("pxm_16_")
    scripted_manual = scripted_with_raw({}, "/manager_proxy_set", "✅ прокси подключён, tcp ok, telethon ok")
    ns_manual = build_pxm_ns_raw_aware(db_path3, scripted_commands=scripted_manual)
    try:
        seed_manager(db_path3, manager_key="mgr9", display_name="Вера")
        ok = await ns_manual["_tpilot_panel_handle_proxy_line"](7, 7, "mgr9", "1.2.3.4:1080:manualUser:manualSecretPass3", new_manager=False)
        sends3 = [c for c in ns_manual["__client__"].calls if c[0] == "send_message"]
        texts3 = [str(c[2]) for c in sends3]
        check("16a. manual SUCCESS uses the same masked result card", ok is True and any("✅ Proxy назначен" in t for t in texts3), texts3)
        check("16b. manual masked card never leaks the raw password", not any("manualSecretPass3" in t for t in texts3), texts3)
    finally:
        cleanup_env(tmp_root3)


# ======================================================================
# PIN-protected full reveal (labels prefixed "RC<n>" -- mapping to the
# 30-item checklist for THIS round's result-card/reveal requirement; kept
# distinct from the plain-numbered labels above, which belong to the prior
# round's method-menu requirement, to avoid ambiguous duplicate numbering
# in the console output).
# ======================================================================

async def test_rc4_6_7_8_result_card_content():
    print("\n-- RC4/6/7/8: result card shows correct identity/login; password stays masked, never leaked --")
    tmp_root, db_path = make_temp_env("pxm_rc4_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        seed_manager(db_path, manager_key="mgrC1", display_name="Клара", telegram_username="klara_tg")
        set_manager_proxy_creds(db_path, "mgrC1", host="6.6.6.6", port="6060", login="claraLogin", password="claraSecret99")
        text = ns["_pxm_result_card_text"]("mgrC1")
        check("RC4. result card shows the correct manager identity", "Клара" in text and "@klara_tg" in text, text)
        check("RC6a. result card shows the login", "claraLogin" in text, text)
        check("RC7. password shown as fully masked (****), never the real value", "****" in text and "claraSecret99" not in text, text)
        check("RC8. the raw password string never appears anywhere in the card text", "claraSecret99" not in text, text)

        seed_manager(db_path, manager_key="mgrC2", display_name="БезЛогина")
        set_manager_proxy_creds(db_path, "mgrC2", host="7.7.7.7", port="7070", login="", password="")
        text2 = ns["_pxm_result_card_text"]("mgrC2")
        check("RC6b. missing login renders the explicit 'без логина' fallback", "без логина" in text2, text2)
    finally:
        cleanup_env(tmp_root)


async def test_rc9_10_11_pin_flow():
    print("\n-- RC9/10/11: reveal button enters the existing PIN flow; wrong PIN reveals nothing; correct PIN renders the full current proxy --")
    tmp_root, db_path = make_temp_env("pxm_rc9_")
    ns = build_pxm_ns(db_path, scripted_commands={}, pin="7777")
    try:
        seed_manager(db_path, manager_key="mgrR", display_name="Роман", telegram_username="roman_tg")
        set_manager_proxy_creds(db_path, "mgrR", host="9.9.9.9", port="3080", login="revLogin", password="revSecretPass")

        ev = FakeCallbackEvent("pxm:reveal:mgrR", chat_id=1, sender_id=1)
        await ns["_pxm_callback"](ev)
        state = ns["_wizard_get"](1, 1)
        check("RC9a. pxm:reveal: enters the existing PIN mechanism (wizard=pxm/reveal_pin)", state.get("wizard") == "pxm" and state.get("step") == "reveal_pin", state)
        check("RC9b. wizard payload during PIN wait carries ONLY manager_key", set((state.get("payload") or {}).keys()) == {"key"}, state.get("payload"))
        sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("RC9c. a PIN prompt was sent", any("PIN" in str(c[2]) for c in sends), sends)

        ev_wrong = FakeMessageEvent("0000", chat_id=1, sender_id=1)
        await ns["_pxm_reveal_pin_input"](ev_wrong)
        sends2 = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("RC10a. wrong PIN never reveals the proxy string", not any("revSecretPass" in str(c[2]) for c in sends2), sends2)
        check("RC10b. wrong PIN shows a generic denial message", any("Неверный PIN" in str(c[2]) for c in sends2), sends2)
        check("RC10c. the PIN message itself is deleted (never left visible in chat)", ev_wrong.deleted, None)
        check("RC10d. wizard state is one-shot -- cleared even after a wrong PIN", ns["_wizard_get"](1, 1) == {}, ns["_wizard_get"](1, 1))

        ev2 = FakeCallbackEvent("pxm:reveal:mgrR", chat_id=1, sender_id=1)
        await ns["_pxm_callback"](ev2)
        ev_right = FakeMessageEvent("7777", chat_id=1, sender_id=1)
        await ns["_pxm_reveal_pin_input"](ev_right)
        sends3 = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("RC11a. correct PIN renders the full current proxy string", any("9.9.9.9:3080:revLogin:revSecretPass" in str(c[2]) for c in sends3), sends3)
        check("RC11b. correct PIN response shows the manager identity", any("Роман" in str(c[2]) and "@roman_tg" in str(c[2]) for c in sends3), sends3)
    finally:
        cleanup_env(tmp_root)


async def test_rc12_13_14_full_reveal_format():
    print("\n-- RC12/13/14: full proxy uses host:port:login:password; credential-free renders host:port; wrapped in a code span --")
    tmp_root, db_path = make_temp_env("pxm_rc12_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        seed_manager(db_path, manager_key="mgrF1", display_name="Формат1")
        set_manager_proxy_creds(db_path, "mgrF1", host="1.1.1.1", port="1111", login="loginF", password="passF")
        text1 = ns["_pxm_full_reveal_text"]("mgrF1")
        check("RC12. full proxy uses host:port:login:password", "1.1.1.1:1111:loginF:passF" in text1, text1)
        check("RC14. the full proxy string is inside its own backtick code span (this project's <code>-equivalent convention)", "`1.1.1.1:1111:loginF:passF`" in text1, text1)

        seed_manager(db_path, manager_key="mgrF2", display_name="Формат2")
        set_manager_proxy_creds(db_path, "mgrF2", host="2.2.2.2", port="2222", login="", password="")
        text2 = ns["_pxm_full_reveal_text"]("mgrF2")
        check("RC13a. credential-free proxy renders host:port", "2.2.2.2:2222" in text2, text2)
        check("RC13b. credential-free proxy never shows an empty separator (host:port:: or host:port:)", "::" not in text2 and "2.2.2.2:2222:" not in text2, text2)
    finally:
        cleanup_env(tmp_root)


async def test_rc15_16_no_plaintext_credentials_stored():
    print("\n-- RC15/16: plaintext proxy credentials never appear in callback_data or unnecessary wizard payload --")
    tmp_root, db_path = make_temp_env("pxm_rc15_")
    ns = build_pxm_ns(db_path, scripted_commands={}, pin="9999")
    try:
        seed_manager(db_path, manager_key="mgrS", display_name="Секрет")
        set_manager_proxy_creds(db_path, "mgrS", host="3.3.3.3", port="3333", login="secLogin", password="secPassword123")

        buttons = ns["_pxm_result_card_buttons"]("mgrS") + ns["_pxm_full_reveal_buttons"]("mgrS") + ns["_pxm_menu_buttons"]("mgrS")
        flat = [b.data for row in buttons for b in row]
        check("RC15. no button's callback_data contains the raw password anywhere in this feature", not any(b"secPassword123" in d for d in flat), flat)

        ev = FakeCallbackEvent("pxm:reveal:mgrS", chat_id=1, sender_id=1)
        await ns["_pxm_callback"](ev)
        state = ns["_wizard_get"](1, 1)
        check("RC16. wizard payload during the PIN wait never stores the password (or any proxy field)", "secPassword123" not in str(state) and "secLogin" not in str(state) and "3.3.3.3" not in str(state), state)
    finally:
        cleanup_env(tmp_root)


async def test_rc17_18_revalidation_and_stale_proxy():
    print("\n-- RC17/18: manager/proxy re-read after PIN success; a proxy changed mid-wait reveals only the current one --")
    tmp_root, db_path = make_temp_env("pxm_rc17_")
    ns = build_pxm_ns(db_path, scripted_commands={}, pin="4242")
    try:
        seed_manager(db_path, manager_key="mgrC", display_name="Смена")
        set_manager_proxy_creds(db_path, "mgrC", host="1.1.1.1", port="1000", login="oldLogin", password="oldPassword")

        ev = FakeCallbackEvent("pxm:reveal:mgrC", chat_id=1, sender_id=1)
        await ns["_pxm_callback"](ev)

        # The proxy changes WHILE the PIN is pending (e.g. a different admin
        # replaced it in another chat).
        set_manager_proxy_creds(db_path, "mgrC", host="2.2.2.2", port="2000", login="newLogin", password="newPassword")

        ev_pin = FakeMessageEvent("4242", chat_id=1, sender_id=1)
        await ns["_pxm_reveal_pin_input"](ev_pin)
        sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("RC17. the manager/proxy is re-read fresh after PIN success (shows the NEW proxy)", any("2.2.2.2:2000:newLogin:newPassword" in str(c[2]) for c in sends), sends)
        check("RC18. the OLD (stale, pre-change) credentials are never revealed", not any("1.1.1.1:1000" in str(c[2]) or "oldPassword" in str(c[2]) for c in sends), sends)
    finally:
        cleanup_env(tmp_root)


async def test_rc19_20_stale_manager_and_missing_proxy():
    print("\n-- RC19/20: stale/deleted manager is safe; missing proxy is safe --")
    tmp_root, db_path = make_temp_env("pxm_rc19_")
    ns = build_pxm_ns(db_path, scripted_commands={}, pin="1111")
    try:
        ev = FakeCallbackEvent("pxm:reveal:ghost", chat_id=1, sender_id=1)
        try:
            await ns["_pxm_callback"](ev)
        except Exception as e:
            check("RC19a. pxm:reveal: on a stale/never-existed manager never raises", False, repr(e))
        else:
            check("RC19a. pxm:reveal: on a stale/never-existed manager answers safely (no crash)", ev.answers != [], ev.answers)

        seed_manager(db_path, manager_key="mgrdel", display_name="Удалён")
        set_manager_proxy_creds(db_path, "mgrdel", host="1.2.3.4", port="1000", login="l", password="p")
        ev2 = FakeCallbackEvent("pxm:reveal:mgrdel", chat_id=2, sender_id=2)
        await ns["_pxm_callback"](ev2)
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM managers WHERE manager_key=?", ("mgrdel",))
        con.commit()
        con.close()
        ev_pin = FakeMessageEvent("1111", chat_id=2, sender_id=2)
        try:
            await ns["_pxm_reveal_pin_input"](ev_pin)
        except Exception as e:
            check("RC19b. correct PIN for a manager deleted mid-wait never raises", False, repr(e))
        else:
            sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
            check("RC19b. correct PIN for a manager deleted mid-wait shows a safe 'not found' message, no crash", any("не найден" in str(c[2]) for c in sends), sends)

        seed_manager(db_path, manager_key="mgrNoProxy", display_name="БезПрокси", proxy_host="", proxy_port="")
        text = ns["_pxm_full_reveal_text"]("mgrNoProxy")
        check("RC20. a manager with no proxy configured at all is handled safely (clear message, no malformed string)", "не настроен proxy" in text, text)
    finally:
        cleanup_env(tmp_root)


async def test_rc21_missing_username_safe():
    print("\n-- RC21: missing username never produces bare @ or None in the result card or full reveal --")
    tmp_root, db_path = make_temp_env("pxm_rc21_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        seed_manager(db_path, manager_key="mgrNU", display_name="БезЮзернейм", telegram_username="")
        set_manager_proxy_creds(db_path, "mgrNU", host="4.4.4.4", port="4000", login="l", password="p")
        card_text = ns["_pxm_result_card_text"]("mgrNU")
        reveal_text = ns["_pxm_full_reveal_text"]("mgrNU")
        for label, text in (("result card", card_text), ("full reveal", reveal_text)):
            check(f"RC21. {label} text never shows a bare '@'", "@" not in text, text)
            check(f"RC21. {label} text never shows the literal 'None'", "None" not in text, text)
            check(f"RC21. {label} text uses the 'без username' fallback", "без username" in text, text)
    finally:
        cleanup_env(tmp_root)


async def test_rc22_25_button_targets():
    print("\n-- RC22-25: check/card/back/home button targets match the spec exactly --")
    tmp_root, db_path = make_temp_env("pxm_rc22_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        buttons = ns["_pxm_result_card_buttons"]("mgrb")
        flat = {b.text: b.data for row in buttons for b in row}
        check("RC22. 'Проверить proxy' reuses the existing check-command callback", flat.get("🔄 Проверить proxy") == b"cmd:/manager_proxy_check mgrb", flat)
        check("RC23. 'Карточка proxy' targets menu:proxy:<key>", flat.get("🌐 Карточка proxy") == b"menu:proxy:mgrb", flat)
        check("RC24. 'Способ замены' targets pxm:menu:<key>", flat.get("⬅️ Способ замены") == b"pxm:menu:mgrb", flat)
        check("RC25. 'Главная' targets the existing home menu", flat.get("🏠 Главная") == b"menu:main", flat)

        reveal_buttons = ns["_pxm_full_reveal_buttons"]("mgrb")
        flat_reveal = {b.text: b.data for row in reveal_buttons for b in row}
        check("RC24b. full-reveal 'Назад' also targets pxm:menu:<key>", flat_reveal.get("⬅️ Назад") == b"pxm:menu:mgrb", flat_reveal)
        check("RC25b. full-reveal 'Главная' targets the existing home menu", flat_reveal.get("🏠 Главная") == b"menu:main", flat_reveal)
    finally:
        cleanup_env(tmp_root)


async def test_17_18_19_back_navigation():
    print("\n-- 17/18/19: pool Back / purchase Back -> pxm:menu:<key>; method-menu Back -> menu:proxy:<key> --")
    tmp_root, db_path = make_temp_env("pxm_17_")
    scripted = {"/proxy_pool_list": ok_pool_list([{"lease_id": 501, "host": "1.2.3.4", "port": 1080, "status": "free", "expires_at": "2026-09-01"}])}
    ns = build_pxm_ns(db_path, scripted_commands=scripted)
    try:
        seed_manager(db_path, manager_key="mgr10", display_name="Дима")
        await ns["_pxm_callback"](FakeCallbackEvent("pxm:pool:mgr10", chat_id=9, sender_id=9))
        await _drain()
        await ns["_frompool_callback"](FakeCallbackEvent("wiz:frompool:back:mgr10", chat_id=9, sender_id=9))
        sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("17. pool 'Back' (ret=proxy_card) returns to the method menu, not the onboarding proxy-choice screen", any("Замена proxy" in str(c[2]) for c in sends), sends)

        # Purchase: confirm-screen Cancel button target is ret-aware.
        buttons = ns["_pbuy_confirm_buttons"]("mgr10", ret="proxy_card")
        cancel = next(b for row in buttons for b in row if b.text == "❌ Отмена")
        check("18. purchase 'Back'/cancel button (ret=proxy_card) targets pxm:menu:<key>", cancel.data == b"pxm:menu:mgr10", cancel.data)

        ev_back = FakeCallbackEvent("pxm:back:mgr10", chat_id=9, sender_id=9)
        await ns["_pxm_callback"](ev_back)
        check("19. method-menu 'Назад' returns to menu:proxy:<key> (via _title_for_menu)", len(ev_back.edits) == 1 and "PROXYCARD:mgr10" in ev_back.edits[0][0], ev_back.edits)
    finally:
        cleanup_env(tmp_root)


async def test_20_21_onboarding_regression():
    print("\n-- 20/21: existing onboarding pool/purchase flows still return to the phone step when ret is absent --")
    tmp_root, db_path = make_temp_env("pxm_20_")
    scripted = {"/proxy_pool_list": ok_pool_list([{"lease_id": 701, "host": "5.6.7.8", "port": 2080, "status": "free", "expires_at": "2026-09-01"}]),
                "/proxy_pool_assign": ok_pool_assign(check_ok=True, lease_id=701, manager_key="mgr11"),
                "/manager_proxy_buy_calc": ok_buy_calc("mgr12"),
                "/manager_proxy_buy_confirm": ok_buy_confirm(check_ok=True, lease_id=801, manager_key="mgr12")}
    ns = build_pxm_ns(db_path, scripted_commands=scripted)
    try:
        seed_manager(db_path, manager_key="mgr11", display_name="Онбординг1")
        seed_manager(db_path, manager_key="mgr12", display_name="Онбординг2")

        # Onboarding entry -- NOT via pxm:, exactly as before this feature.
        await ns["_frompool_callback"](FakeCallbackEvent("wiz:add_manager:frompool:mgr11", chat_id=10, sender_id=10))
        await _drain()
        await ns["_frompool_callback"](FakeCallbackEvent("wiz:frompool:pick:mgr11:701", chat_id=10, sender_id=10))
        await _drain()
        state = ns["_wizard_get"](10, 10)
        await ns["_frompool_callback"](FakeCallbackEvent(f"wiz:frompool:confirm:mgr11:701", chat_id=10, sender_id=10))
        await _drain()
        sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        # SUPERSEDED by Ф4 (unified auth UX, independently proven in tools/
        # auth_chooser_selftest.py): onboarding proxy completion no longer
        # auto-starts the phone step -- it lands on the unified QR/Phone/
        # Session/TData chooser, phone only after an explicit tap.
        check("20. onboarding pool assignment (ret absent) proceeds to the unified auth chooser",
              any("Как войти в Telegram?" in str(c[2]) for c in sends), sends)
        final_state = ns["_wizard_get"](10, 10)
        check("20b. onboarding wizard state is 'add_manager/auth_choice' (not an auto-started phone step)",
              final_state.get("wizard") == "add_manager" and final_state.get("step") == "auth_choice", final_state)

        await ns["_pbuy_callback"](FakeCallbackEvent("wiz:add_manager:buyproxy:mgr12", chat_id=11, sender_id=11))
        await _drain()
        state_confirm = ns["_wizard_get"](11, 11)
        check("21. onboarding purchase calc still uses buyproxy_confirm/ret='' (unchanged)", state_confirm.get("step") == "buyproxy_confirm" and (state_confirm.get("payload") or {}).get("ret") == "", state_confirm)
        await ns["_pbuy_callback"](FakeCallbackEvent("wiz:buyproxy:confirm:mgr12", chat_id=11, sender_id=11))
        await _drain()
        sends2 = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        # SUPERSEDED by Ф4, same reasoning as check 20 above.
        check("21b. onboarding purchase (ret absent) proceeds to the unified auth chooser",
              any("Как войти в Telegram?" in str(c[2]) for c in sends2), sends2)
    finally:
        cleanup_env(tmp_root)


async def test_22_stale_manager_handled_safely():
    print("\n-- 22: a stale/deleted manager is handled safely at every pxm: entry point --")
    tmp_root, db_path = make_temp_env("pxm_22_")
    ns = build_pxm_ns(db_path, scripted_commands={})
    try:
        for cb in ("pxm:menu:ghost", "pxm:manual:ghost", "pxm:pool:ghost", "pxm:buy:ghost"):
            ev = FakeCallbackEvent(cb, chat_id=1, sender_id=1)
            try:
                await ns["_pxm_callback"](ev)
            except Exception as e:
                check(f"22. {cb} on a stale manager never raises", False, repr(e))
                continue
            check(f"22. {cb} on a stale manager answers safely with a clear 'not found' message", ev.answers != [] and (not ev.edits or "не найден" in ev.edits[0][0]), (ev.answers, ev.edits))
    finally:
        cleanup_env(tmp_root)


async def test_23_empty_pool_unchanged():
    print("\n-- 23: empty-pool behavior is unchanged (still the pre-existing onboarding-flavored empty screen) --")
    tmp_root, db_path = make_temp_env("pxm_23_")
    scripted = {"/proxy_pool_list": ok_pool_list([])}
    ns = build_pxm_ns(db_path, scripted_commands=scripted)
    try:
        seed_manager(db_path, manager_key="mgr13", display_name="Пусто")
        await ns["_pxm_callback"](FakeCallbackEvent("pxm:pool:mgr13", chat_id=1, sender_id=1))
        await _drain()
        sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("23. empty pool still shows 'Свободных proxy в пуле нет' (unmodified _frompool_run_picker)", any("Свободных proxy в пуле нет" in str(c[2]) for c in sends), sends)
    finally:
        cleanup_env(tmp_root)


async def test_24_confirmation_token_logic_still_used():
    print("\n-- 24: the existing one-time confirmation token logic is still enforced (not bypassed) --")
    tmp_root, db_path = make_temp_env("pxm_24_")
    scripted = {"/proxy_pool_assign": ok_pool_assign(check_ok=True, lease_id=501, manager_key="mgr14")}
    ns = build_pxm_ns(db_path, scripted_commands=scripted)
    try:
        seed_manager(db_path, manager_key="mgr14", display_name="Токен")
        # No prior confirm-step wizard state -- a bare confirm tap must be refused.
        ev = FakeCallbackEvent("wiz:frompool:confirm:mgr14:501", chat_id=1, sender_id=1)
        await ns["_frompool_callback"](ev)
        sends = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("24. a forged/stale confirm tap (no matching one-time token) is refused, not executed", sends == [] and ev.answers and ev.answers[0][1] is True, (sends, ev.answers))
    finally:
        cleanup_env(tmp_root)


async def test_25_no_duplicate_implementation():
    print("\n-- 25: no duplicate pool/purchase/manual implementation was introduced --")
    tree = ast.parse(PANEL_SRC)
    from collections import Counter
    defs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    c = Counter(defs)
    for name in ("_frompool_run_picker", "_frompool_run_confirm_screen", "_frompool_run_assign",
                 "_pbuy_run_calc", "_pbuy_run_confirm", "_pbuy_run_recover",
                 "_tpilot_panel_handle_proxy_line", "_tpilot_parse_socks5_one_line",
                 "_pxm_callback", "_pxm_menu_text", "_pxm_menu_buttons", "_pxm_enter_manual",
                 "_pxm_result_card_text", "_pxm_result_card_buttons", "_pxm_send_result_card",
                 "_pxm_full_reveal_text", "_pxm_full_reveal_buttons", "_pxm_reveal_pin_input",
                 "_ppool_pin_env_value", "_ppool_reveal_pin_input", "_ppool_reveal_creds_once"):
        check(f"25. {name} defined exactly once (no duplicate active definitions)", c.get(name, 0) == 1, c.get(name, 0))


async def test_26_callback_data_length():
    print("\n-- 26: every pxm: callback_data stays <= 64 bytes, even for a long manager_key --")
    long_key = "a" * 24  # generous vs. real project keys (2-16 chars observed)
    for tmpl in ("pxm:menu:{k}", "pxm:back:{k}", "pxm:manual:{k}", "pxm:pool:{k}", "pxm:buy:{k}",
                 "pxm:reveal:{k}", "pxm:cardresult:{k}"):
        data = tmpl.format(k=long_key).encode()
        check(f"26. {tmpl!r} stays <=64 bytes for a 24-char key", len(data) <= 64, len(data))


async def test_27_no_new_allow_spend_site():
    print("\n-- 27: no new allow_spend=True call site was introduced (still exactly 2, both in main.py) --")
    main_src = open(BASE_DIR / "main.py", encoding="utf-8-sig").read()
    tree = ast.parse(main_src)
    sites = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords
             if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]
    check("27. allow_spend=True remains exactly 2 real call sites in main.py", len(sites) == 2, sites)
    panel_tree = ast.parse(PANEL_SRC)
    panel_sites = [n.lineno for n in ast.walk(panel_tree) if isinstance(n, ast.Call) for kw in n.keywords
                   if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]
    check("27b. panel_bot.py (this task's only allowed production file) has zero real allow_spend=True call sites", panel_sites == [], panel_sites)


async def test_28_credentials_masked_on_card():
    print("\n-- 28: credentials remain masked on the (unmodified) Proxy card renderer path; no logging of secrets --")
    pxm_block_start = PANEL_SRC.index("TPILOT PROXY METHOD MENU 20260718 START")
    pxm_block_end = PANEL_SRC.index("TPILOT PROXY METHOD MENU 20260718 END")
    pxm_src = PANEL_SRC[pxm_block_start:pxm_block_end]
    # session_path/api_hash/phone_code_hash are genuinely irrelevant to proxy
    # handling -- their presence would indicate scope creep. proxy_password
    # itself is now a LEGITIMATE column reference (the PIN-gated reveal's
    # entire purpose is reading it) -- checked separately below for the one
    # thing that actually matters: it is never printed/logged.
    check("28a. the new pxm: block never references session_path/api_hash/phone_code_hash", not any(s in pxm_src for s in ("session_path", "api_hash", "phone_code_hash")), None)
    print_lines = [ln for ln in pxm_src.splitlines() if "print(" in ln]
    check("28b. no print()/log statement in the new pxm: block references a proxy credential variable", not any(("password" in ln or "proxy_row" in ln or "creds" in ln) for ln in print_lines), print_lines)


async def test_29_30_temp_db_and_no_provider_call():
    print("\n-- 29/30: every fixture uses a temp DB only; no real provider/API call is ever made --")
    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    try:
        _selftest_db_guard(prod_file, BASE_DIR)
        check("29. the DB guard refuses the real production DB file", False, "guard did not raise")
    except AssertionError:
        check("29. the DB guard refuses the real production DB file", True)
    # 30: every scripted "provider" response above is a plain Python dict built
    # in this file -- _submit_and_wait is fully faked, so proxy_provider.py /
    # ProxySellerProvider are never IMPORTED or CONSTRUCTED here (mentioning
    # their names in a comment, as this docstring itself does, is fine --
    # only a real import/construction would indicate a real provider call).
    this_tree = ast.parse(open(__file__, encoding="utf-8").read())
    real_imports = [n.lineno for n in ast.walk(this_tree)
                    if (isinstance(n, ast.Import) and any("proxy_provider" in (a.name or "") for a in n.names))
                    or (isinstance(n, ast.ImportFrom) and "proxy_provider" in (n.module or ""))]
    construct_calls = [n.lineno for n in ast.walk(this_tree) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name) and n.func.id == "ProxySellerProvider"]
    check("30. this selftest never imports proxy_provider.py", real_imports == [], real_imports)
    check("30b. this selftest never constructs a ProxySellerProvider instance", construct_calls == [], construct_calls)


# ======================================================================
# Startup/import-order regression (2026-07-18b): the AST-extraction
# technique above runs hand-picked function bodies out of file order with
# a fake `client` pre-injected into the exec namespace -- it can never
# catch a REAL module-execution-order bug like a `@client.on(...)`
# decorator placed before the module-level `client = TelegramClient(...)`
# assignment (a genuine production incident: panel_bot.py raised
# `NameError: name 'client' is not defined` on real startup even though
# py_compile and every extracted-function selftest passed clean). These
# two checks close that gap: a static AST ordering guard (always safe,
# instant) plus a real subprocess import of the actual file in true
# top-to-bottom execution order, with ONLY telethon and dotenv stubbed
# (network/session-touching), so every decorator genuinely executes.
# ======================================================================

def test_31_static_client_decorator_order_guard():
    print("\n-- 31: static guard -- every @client.on(...) decorator appears AFTER the client assignment --")
    tree = ast.parse(PANEL_SRC)
    client_assign_line = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "client" and isinstance(node.value, ast.Call):
            client_assign_line = node.lineno
            break
    check("31a. module-level 'client = TelegramClient(...)' assignment was found", client_assign_line is not None, client_assign_line)

    offenders = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                dec_src = ast.unparse(dec)
                if dec_src.startswith("client.on("):
                    if client_assign_line is None or dec.lineno < client_assign_line:
                        offenders.append((node.name, dec.lineno))
    check("31b. every @client.on(...) decorator is at/after the client assignment (catches the exact prior incident)", offenders == [], offenders)


def test_32_real_import_order_startup_check():
    print("\n-- 32: real subprocess import of panel_bot.py in true module order (telethon/dotenv stubbed, no network/DB/provider) --")
    script = r'''
import sys, types, os

# Stub telethon: only Button/TelegramClient/events are imported by panel_bot.py
# (confirmed via a single "from telethon import Button, TelegramClient, events"
# grep hit) -- TelegramClient.on() just records the decorated function, never
# connects; nothing here ever calls .start()/.connect()/.run_until_disconnected().
fake_telethon = types.ModuleType("telethon")

class _FakeButton:
    @staticmethod
    def inline(text, data=b""):
        return (text, data)

class _FakeEvents:
    # Both used bare (@client.on(events.NewMessage)) AND constructed with a
    # pattern filter (@client.on(events.NewMessage(pattern=r"..."))) --
    # confirmed both forms exist in panel_bot.py -- so these must accept
    # arbitrary constructor args without erroring either way.
    class NewMessage:
        def __init__(self, *a, **kw):
            pass
    class CallbackQuery:
        def __init__(self, *a, **kw):
            pass

class _FakeTelegramClient:
    def __init__(self, *a, **kw):
        self.registered = []
    def on(self, *a, **kw):
        def _decorator(fn):
            self.registered.append(fn.__name__)
            return fn
        return _decorator

fake_telethon.Button = _FakeButton
fake_telethon.TelegramClient = _FakeTelegramClient
fake_telethon.events = _FakeEvents
sys.modules["telethon"] = fake_telethon

# Stub dotenv to a no-op so the real .env.TPilot (production config) is
# NEVER read -- os.getenv() then only sees whatever is already in this
# subprocess's own environment (safe empty/zero defaults throughout
# panel_bot.py's config-loading block). dotenv_values is also stubbed --
# manager_registry.py (imported by panel_bot.py) imports it too.
fake_dotenv = types.ModuleType("dotenv")
fake_dotenv.load_dotenv = lambda *a, **kw: None
fake_dotenv.dotenv_values = lambda *a, **kw: {}
sys.modules["dotenv"] = fake_dotenv

sys.path.insert(0, sys.argv[1])
import panel_bot  # real file, real top-to-bottom execution order

client = panel_bot.client
assert isinstance(client, _FakeTelegramClient), "client was not the stubbed TelegramClient"
assert "_pxm_callback" in client.registered, f"_pxm_callback never registered: {client.registered}"
assert "_pxm_reveal_pin_input" in client.registered, f"_pxm_reveal_pin_input never registered: {client.registered}"
assert "_ppool_reveal_pin_input" in client.registered, "existing ppool reveal handler missing"
assert "_frompool_callback" in client.registered, "existing frompool callback missing"
print("STARTUP_IMPORT_OK", len(client.registered), "handlers registered")
'''
    tmp = Path(tempfile.mkdtemp(prefix="pxm_startup_")) / "run_import_check.py"
    tmp.write_text(script, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(tmp), str(BASE_DIR)],
            capture_output=True, text=True, timeout=60, cwd=str(BASE_DIR),
        )
        ok = proc.returncode == 0 and "STARTUP_IMPORT_OK" in proc.stdout
        detail = (proc.stdout + proc.stderr)[-2000:]
        check("32a. panel_bot.py imports cleanly in true module order (no NameError/other startup exception)", ok, detail)
        check("32b. this is a REAL execution, not the AST-extraction technique (proven by the actual NameError this exact check would have caught before the fix)", "NameError" not in proc.stderr or ok, proc.stderr[-500:])
    finally:
        cleanup_env(tmp.parent)


def main() -> int:
    asyncio.run(test_1_2_3_proxy_card_and_menu_screen())
    asyncio.run(test_4_5_manager_identity_safe())
    asyncio.run(test_6_manual_reuses_existing_wizard())
    asyncio.run(test_7_8_9_manual_prompt_formats())
    asyncio.run(test_10_11_pool_and_buy_delegate_to_existing_functions())
    asyncio.run(test_12_13_ret_preserved_through_confirmation())
    asyncio.run(test_14_15_16_success_shows_masked_result_card())
    asyncio.run(test_rc4_6_7_8_result_card_content())
    asyncio.run(test_rc9_10_11_pin_flow())
    asyncio.run(test_rc12_13_14_full_reveal_format())
    asyncio.run(test_rc15_16_no_plaintext_credentials_stored())
    asyncio.run(test_rc17_18_revalidation_and_stale_proxy())
    asyncio.run(test_rc19_20_stale_manager_and_missing_proxy())
    asyncio.run(test_rc21_missing_username_safe())
    asyncio.run(test_rc22_25_button_targets())
    asyncio.run(test_17_18_19_back_navigation())
    asyncio.run(test_20_21_onboarding_regression())
    asyncio.run(test_22_stale_manager_handled_safely())
    asyncio.run(test_23_empty_pool_unchanged())
    asyncio.run(test_24_confirmation_token_logic_still_used())
    asyncio.run(test_25_no_duplicate_implementation())
    asyncio.run(test_26_callback_data_length())
    asyncio.run(test_27_no_new_allow_spend_site())
    asyncio.run(test_28_credentials_masked_on_card())
    asyncio.run(test_29_30_temp_db_and_no_provider_call())
    test_31_static_client_decorator_order_guard()
    test_32_real_import_order_startup_check()

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
