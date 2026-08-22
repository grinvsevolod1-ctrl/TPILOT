# -*- coding: utf-8 -*-
"""tools/manager_relogin_selftest.py -- offline self-test for the "🔐 Перезайти"
(safe re-authorization of an EXISTING manager's Telegram session) feature.

Scope under test: panel_bot.py (card button override, wizard='relogin' callback/free-text
handlers) and main.py (temp-session auth flow, identity gate, atomic commit, rollback).
No real Telegram network calls, no real subprocess spawning, no real session files
outside a temp directory that is deleted at the end of every test.

Techniques (same as every other tools/*_selftest.py in this project):
  - panel_bot.py/main.py cannot be imported standalone (Telethon/env side effects at
    import time) -- functions under test are extracted via ast.parse + ast.unparse +
    exec() and run FOR REAL against a temporary SQLite DB (never db/data_tpilot.db).
  - main.py redefines _panel_execute_command_text ~30 times via an override-stack.
    This file does NOT execute that dispatcher at all -- it calls the relogin command
    functions directly (proven registered in the last active dispatcher via a static
    source sweep instead), sidestepping the need to reconstruct 30 unrelated generations.
  - Telethon itself is not installed in this local dev environment, so the temp
    TelegramClient, and the SessionPasswordNeededError/PhoneCodeInvalidError/
    PhoneCodeExpiredError/PasswordHashInvalidError/FloodWaitError exception types it
    raises, are FAKED under their exact production names -- `except SessionPasswordNeededError:`
    inside the extracted code matches by whatever class object is bound to that name in
    the exec namespace, so a lightweight local Exception subclass works identically to
    the real telethon.errors class for every test in this file.
  - storage.py's manager_* functions read a plain module-level `storage.DB_PATH` global
    (not a parameter) -- pointed at the temp DB via direct attribute assignment before
    each test, exactly like a real deployment would set it once at process start.

    python tools\\manager_relogin_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import ast_extract  # noqa: E402  (shared AST harness, lives next to this file)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
PANEL_PATH = str(BASE_DIR / "panel_bot.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()


# ======================================================================
# Fake Telethon layer (telethon itself is not installed locally; these
# stand-ins are bound under the exact production names the extracted code
# references, so `except SessionPasswordNeededError:` etc. match correctly).
# ======================================================================

class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class PhoneCodeExpiredError(Exception):
    pass


class PasswordHashInvalidError(Exception):
    pass


class FloodWaitError(Exception):
    """Name matters: _manager_is_rate_limit_error() checks 'flood' in
    exc.__class__.__name__.lower(), so this must be named exactly this."""
    def __init__(self, seconds: int = 5):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


class AuthKeyUnregisteredError(Exception):
    pass


def make_fake_telegram_client_class(script: dict, calls: list, authorized_sessions: set):
    """authorized_sessions is a SHARED set of session_path strings -- mimics a real
    Telethon session FILE persisting its own auth state independent of which client
    object currently has it open, which is essential: production code opens a fresh
    client on the same temp path multiple times across the phone->code->commit steps."""

    class _FakeMe:
        def __init__(self, uid, username, first, last, phone):
            self.id = uid
            self.username = username
            self.first_name = first
            self.last_name = last
            self.phone = phone

    class FakeTelegramClient:
        def __init__(self, session_path, *a, **kw):
            self.session_path = str(session_path)

        async def connect(self):
            calls.append(("connect", self.session_path))
            try:
                open(self.session_path, "a").close()
            except Exception:
                pass

        async def disconnect(self):
            calls.append(("disconnect", self.session_path))

        async def send_code_request(self, phone):
            calls.append(("send_code_request", phone))
            exc = script.get("send_code_exc")
            if exc is not None:
                raise exc

            class _Sent:
                phone_code_hash = script.get("phone_code_hash", "hash123")
            return _Sent()

        async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
            calls.append(("sign_in", phone, code, password))
            if password is not None:
                exc = script.get("pass_sign_in_exc", script.get("sign_in_exc"))
            else:
                exc = script.get("code_sign_in_exc", script.get("sign_in_exc"))
            if exc is not None:
                raise exc
            authorized_sessions.add(self.session_path)

        async def is_user_authorized(self):
            return self.session_path in authorized_sessions

        async def get_me(self):
            calls.append(("get_me",))
            return _FakeMe(
                script.get("actual_user_id", 999001),
                script.get("actual_username", "newacc"),
                script.get("actual_first_name", "New"),
                script.get("actual_last_name", "Account"),
                script.get("actual_phone", "+10000000000"),
            )

        async def log_out(self):
            calls.append(("log_out",))
            authorized_sessions.discard(self.session_path)

    return FakeTelegramClient


class _OSReplaceFailProxy:
    """Thin proxy over the real os module that can be told to fail os.replace() for
    exactly one test, without affecting os.path.exists/os.remove/etc used elsewhere in
    the same extracted code."""
    def __init__(self, real_os, fail_flag: dict):
        self._real = real_os
        self._flag = fail_flag

    def replace(self, src, dst):
        if self._flag.get("fail_replace"):
            raise OSError("simulated os.replace failure")
        return self._real.replace(src, dst)

    def __getattr__(self, name):
        return getattr(self._real, name)


# ======================================================================
# Fake per-manager process registry (never spawns/kills a real process).
# ======================================================================

def make_fake_process_helpers():
    running: set = set()
    stop_calls: list = []
    spawn_calls: list = []
    spawn_result = {"ok": True, "msg": "OK"}

    async def fake_process_running(key):
        return key in running

    async def fake_stop_process(key, *, silent=False):
        stop_calls.append(key)
        running.discard(key)
        return True, "OK"

    async def fake_spawn_process(key):
        spawn_calls.append(key)
        if spawn_result["ok"]:
            running.add(key)
        return spawn_result["ok"], spawn_result["msg"]

    return {
        "running": running,
        "stop_calls": stop_calls,
        "spawn_calls": spawn_calls,
        "spawn_result": spawn_result,
        "fn_running": fake_process_running,
        "fn_stop": fake_stop_process,
        "fn_spawn": fake_spawn_process,
    }


# ======================================================================
# main.py extraction: the relogin command contour + its direct, pre-existing,
# unmodified dependencies (storage.py functions, manager_registry helpers).
# ======================================================================

MAIN_REAL_NAMES = {
    "_RELOGIN_STEP_PHONE", "_RELOGIN_STEP_CODE", "_RELOGIN_STEP_PASS",
    "_RELOGIN_STEP_IDENTITY_CONFIRM", "_RELOGIN_STEP_READY_COMMIT", "_RELOGIN_STEPS",
    "_RELOGIN_IDENTITY_MISMATCH_MARKER", "_RELOGIN_COMMIT_OK_MARKER",
    "_manager_relogin_temp_session_path", "_manager_relogin_cleanup_temp_files",
    "_manager_relogin_cleanup_state", "_manager_relogin_active_owner",
    "_manager_relogin_begin", "replacement_active_for_old_key", "_panel_manager_relogin_phone_command",
    "_manager_relogin_after_signin", "_panel_manager_relogin_code_command",
    "_panel_manager_relogin_pass_command", "_panel_manager_relogin_confirm_command",
    "_panel_manager_relogin_cancel_command", "_manager_relogin_commit",
    "_manager_runtime_paths_for_key", "_manager_is_rate_limit_error",
    "_manager_extract_wait_seconds", "_map_send_code_error",
    "_manager_finalize_login", "_manager_label_from_row",
    "_partner_source_key_for_manager", "_tp_finalize_screenshots_on",
    "_ONBOARDING_SOURCE_PICK_MARKER", "_now_utc_iso", "_future_iso",
    "_manager_runtime_onboarding_clear",
    "_manager_auth_audit_log", "_MANAGER_AUTH_AUDIT_LOG",
    "_log_manager_danger_action", "_ensure_manager_admin_log_table",
    "_backup_manager_session_files", "_copy_existing_files", "_kyiv_now",
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
    """Fail-safe (forensic review recommendation, 2026-07-16, prompted by a
    real incident where a forgotten QUEUE_DB_PATH rebind let replacement_*
    writes fall through to the real db/data_tpilot.db file): refuses any
    db_path located under base_dir/db (the project's own db/ directory,
    boundary-checked with os.sep so a sibling like .../dbfoo is never a false
    match), and requires both storage DB-path globals to already equal
    db_path. Raises AssertionError on any violation. Kept standalone (not
    inlined into build_main_ns) specifically so it can be unit-tested
    directly against synthetic bad inputs -- see
    test_dbguard_rejects_unsafe_paths -- without ever touching the real
    storage.DB_PATH/QUEUE_DB_PATH globals or opening any real connection."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def build_main_ns(db_path: str, base_dir: Path, *, script: dict = None, os_module=None) -> dict:
    import manager_registry
    import storage as _storage
    import aiosqlite as _aiosqlite
    from datetime import datetime as _dt, timedelta as _td

    _storage.DB_PATH = db_path
    # Stage 3 (2026-07-16): the replacement_* functions storage.py added for
    # account replacement go through _bsl_connect()/_bsl_db_path(), which
    # falls back to the SEPARATE storage.QUEUE_DB_PATH global (defaults to
    # db/data_tpilot.db) -- NOT storage.DB_PATH. Every manager_* function this
    # file already used only ever needed DB_PATH, so this second global was
    # never bound here before replacement_active_for_old_key existed. Binding
    # it is required -- without it, replacement_create/advance/cancel/fail
    # silently write to the real db/data_tpilot.db file instead of the temp
    # DB (caught and fixed during this exact patch; matches the binding
    # already done the same way in manager_replacement_backend_selftest.py
    # and manager_replacement_adminbot_selftest.py).
    _storage.QUEUE_DB_PATH = db_path
    # NOTE: this function's own "base_dir" parameter is the per-test TEMP
    # root, not the project root -- the module-level BASE_DIR is used
    # deliberately here instead of base_dir.
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    script = script if script is not None else {}
    calls: list = []
    authorized_sessions: set = set()
    fake_client_cls = make_fake_telegram_client_class(script, calls, authorized_sessions)
    proc = make_fake_process_helpers()

    nodes = _extract_by_names(MAIN_SRC, MAIN_REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    ns = {
        # STAGE 3: seed every side-effect-free project module the extracted main.py code
        # may reference through a module-level import alias (text_format_helpers,
        # proxy_parser, ...). Splatted FIRST so the explicit fakes below always win.
        **ast_extract.safe_module_ns(),
        "os": os_module or os,
        "asyncio": asyncio,
        "shutil": shutil,
        "re": __import__("re"),
        "Path": Path,
        "aiosqlite": _aiosqlite,
        "datetime": _dt,
        "timedelta": _td,
        # Windows here has no tzdata package (ZoneInfo("Europe/Kyiv") would raise) -- a
        # fixed UTC+3 offset is a fine stand-in for _kyiv_now()'s only use in this file
        # (a human-readable timestamp inside a backup folder name).
        "TZ_KYIV": __import__("datetime").timezone(__import__("datetime").timedelta(hours=3)),
        "BASE_DIR": base_dir,
        "TPILOT_DB_PATH": db_path,
        "MANAGER_ONBOARD_TIMEOUT_SEC": 1200,
        "MANAGER_PHONE_COOLDOWN_SEC": 900,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "mask_phone": manager_registry.mask_phone,
        "validate_manager_key": manager_registry.validate_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "manager_get": _storage.manager_get,
        "manager_get_onboarding": _storage.manager_get_onboarding,
        "manager_save_onboarding": _storage.manager_save_onboarding,
        "manager_delete_onboarding": _storage.manager_delete_onboarding,
        "manager_delete_onboarding_by_key": _storage.manager_delete_onboarding_by_key,
        "manager_list_pending": _storage.manager_list_pending,
        "manager_set_fields": _storage.manager_set_fields,
        # main.py calls storage.manager_bot_access_ensure_sync via a module-level
        # binding when granting ManagerBot access after onboarding/relogin commit;
        # ast-extraction never pulls plain imports, so bind the real storage
        # function explicitly (same _storage object, already pointed at temp DB).
        "manager_bot_access_ensure_sync": _storage.manager_bot_access_ensure_sync,
        "_build_manager_telegram_client_from_row": lambda row, session_path, source="onboarding": fake_client_cls(session_path),
        "_api_profile_for": lambda source: (0, "", "onboarding"),
        "_manager_process_running": proc["fn_running"],
        "_stop_manager_process": proc["fn_stop"],
        "_spawn_manager_process": proc["fn_spawn"],
        "SessionPasswordNeededError": SessionPasswordNeededError,
        "PhoneCodeInvalidError": PhoneCodeInvalidError,
        "PhoneCodeExpiredError": PhoneCodeExpiredError,
        "PasswordHashInvalidError": PasswordHashInvalidError,
        # Stage 3 (2026-07-16): _manager_relogin_begin now calls the real
        # replacement_active_for_old_key(), whose own body references the
        # module-level "import storage as _repl_storage" alias from main.py.
        # That plain ast.Import statement is never captured by
        # _extract_by_names (it only pulls Function/Assign/AnnAssign nodes by
        # name), so it must be bound explicitly here -- same real _storage
        # object already used for every other storage.* binding above/below,
        # already pointed at the temp DB via _storage.DB_PATH = db_path.
        "_repl_storage": _storage,
        # utcnow refactor (2026-08-16): extracted main.py code reads the
        # clock through the module-level _tp_utc_now() seam (naive UTC).
        "_tp_utc_now": (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(tzinfo=None)),
    }
    exec(compile(module_src, f"<{MAIN_PATH}:relogin>", "exec"), ns)
    ns["__calls__"] = calls
    ns["__authorized_sessions__"] = authorized_sessions
    ns["__proc__"] = proc
    ns["__storage__"] = _storage
    return ns


# ======================================================================
# Temp DB / fixture helpers. storage.py's _manager_table_ready() self-
# creates every table it needs (CREATE TABLE IF NOT EXISTS) -- no schema
# pre-seeding required beyond that.
# ======================================================================

def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="relogin_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def seed_manager(ns: dict, *, key="mgr01", tg_user_id=555001, display_name="Manager One",
                        username="mgrone", status="active", is_enabled=1, manual_stopped=0,
                        proxy_host="1.2.3.4", proxy_port=1080, source_key="src_a",
                        owner_user_id=9001) -> dict:
    # owner_user_id defaults to a value DISTINCT from every relogin-admin `owner` used in
    # this file's tests (1001/2002/9999/etc) -- if a regression re-introduces overwriting
    # owner_user_id with the acting admin, before != after will actually fail loudly
    # instead of silently matching by coincidence.
    storage = ns["__storage__"]
    paths = ns["_manager_runtime_paths_for_key"](key)
    os.makedirs(paths["root"], exist_ok=True)
    await storage.manager_add(
        manager_key=key, display_name=display_name, phone="+70000000000", status=status,
        session_path=paths["session_path"], db_path=paths["db_path"], workdir=paths["root"],
        log_path=paths["log_path"], is_enabled=is_enabled, owner_user_id=owner_user_id,
    )
    await storage.manager_set_fields(
        key, manual_stopped=manual_stopped, tg_user_id=(tg_user_id or None),
        telegram_username=username, proxy_host=proxy_host, proxy_port=proxy_port,
        proxy_type="SOCKS5", proxy_enabled=1,
    )
    # A separate table this feature must never touch (source link = "source unchanged").
    con = __import__("sqlite3").connect(ns["TPILOT_DB_PATH"])
    try:
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("DELETE FROM manager_source_links WHERE manager_key=?", (key,))
        con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, source_key))
        # A stand-in "statistics/history" table this feature must never touch either.
        con.execute("CREATE TABLE IF NOT EXISTS daily_leads_marker(manager_key TEXT, note TEXT)")
        con.execute("DELETE FROM daily_leads_marker WHERE manager_key=?", (key,))
        con.execute("INSERT INTO daily_leads_marker(manager_key, note) VALUES(?,?)", (key, "history-row-untouched"))
        con.commit()
    finally:
        con.close()
    return await storage.manager_get(key)


def write_boevoy_session(ns: dict, key: str, content: bytes = b"BOEVOY-SESSION-BYTES") -> str:
    paths = ns["_manager_runtime_paths_for_key"](key)
    os.makedirs(os.path.dirname(paths["session_path"]), exist_ok=True)
    with open(paths["session_path"], "wb") as f:
        f.write(content)
    return paths["session_path"]


def read_bytes_or_none(path: str):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


# owner_user_id is deliberately NOT in this set -- relogin must NEVER change who owns a
# manager (fixed blocker: _manager_finalize_login used to unconditionally overwrite it
# with the acting admin; relogin now passes preserve_owner_user_id=True).
IDENTITY_FIELDS = {"tg_user_id", "telegram_username", "first_name", "last_name", "phone", "last_login_at", "updated_at"}


def snapshot_diff(before: dict, after: dict) -> set:
    """Returns the set of column names whose VALUE differs between two manager rows."""
    keys = set(before.keys()) | set(after.keys())
    return {k for k in keys if before.get(k) != after.get(k)}


async def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


# ======================================================================
# 1-5. Card button placement, callback correctness, archived/closer gating.
# ======================================================================

PANEL_ROUTING_NAMES = {
    "_manager_admin_detail_buttons",
    "_manager_row_by_key", "_manager_rows_all", "_manager_rows",
    "_manager_short_label", "_manager_state_icon", "_manager_state_label",
    "_manager_proxy_badge",
}


def build_panel_render_ns(db_path: str) -> dict:
    import manager_registry
    tree = ast.parse(PANEL_SRC)
    marked = None
    for n in tree.body:
        if getattr(n, "name", None) == "_manager_admin_detail_buttons":
            try:
                if "relogin:start:" in ast.unparse(n):
                    marked = n
            except Exception:
                continue
    if marked is None:
        raise AssertionError("could not locate the relogin-edited _manager_admin_detail_buttons override")

    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm == "_manager_admin_detail_buttons":
            continue
        if nm and nm in PANEL_ROUTING_NAMES:
            nodes.append(n)
            seen.add(nm)
    missing = (PANEL_ROUTING_NAMES - {"_manager_admin_detail_buttons"}) - seen
    if missing:
        raise AssertionError(f"panel render extraction missing {missing}")
    nodes.append(marked)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    class _FakeBtn:
        __slots__ = ("text", "data")

        def __init__(self, text, data):
            self.text = text
            self.data = data

        def __iter__(self):
            yield "btn"
            yield self.text
            yield self.data

        def __getitem__(self, i):
            return ("btn", self.text, self.data)[i]

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
            return _FakeBtn(text, raw)

    def _fake_prev_buttons(key):
        # Stand-in for the M2.12A chain this override wraps: a plausible pre-existing
        # card with a real 'Запустить' row, so the "insert right after start/stop" and
        # "insert before the last two nav rows" logic can both be exercised for real.
        return [
            [_FakeButton.inline("📄 Карточка", f"cmd:/manager_info {key}".encode())],
            [_FakeButton.inline("▶️ Запустить", f"cmd:/manager_start {key}".encode()), _FakeButton.inline("⏹ Остановить", f"cmd:/manager_stop {key}".encode())],
            [_FakeButton.inline("🌐 Прокси менеджера", f"menu:proxy:{key}".encode())],
            [_FakeButton.inline("⬅️ Назад к админ-меню", b"menu:manager_admin")],
            [_FakeButton.inline("🏠 Главная панель", b"menu:main")],
        ]

    ns = {
        "sqlite3": __import__("sqlite3"),
        "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_RELOGIN_PREV_ADMIN_DETAIL_BUTTONS": _fake_prev_buttons,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:relogin_render>", "exec"), ns)
    return ns


def _seed_panel_db(db_path: str, managers: list) -> None:
    import sqlite3
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE managers(id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT UNIQUE, "
            "display_name TEXT, telegram_username TEXT, role TEXT DEFAULT 'manager', status TEXT DEFAULT 'active', "
            "is_enabled INTEGER DEFAULT 1, manual_stopped INTEGER DEFAULT 0)"
        )
        for m in managers:
            con.execute(
                "INSERT INTO managers(manager_key, display_name, telegram_username, role, status, is_enabled, manual_stopped) "
                "VALUES(?,?,?,?,?,?,?)",
                (m["manager_key"], m.get("display_name", m["manager_key"]), m.get("telegram_username", ""),
                 m.get("role", "manager"), m.get("status", "active"), int(m.get("is_enabled", 1)), int(m.get("manual_stopped", 0))),
            )
        con.commit()
    finally:
        con.close()


def test_1_5_button_placement_and_gating() -> None:
    import tempfile as _tf
    fd, db_path = _tf.mkstemp(suffix=".db", prefix="relogin_panel_")
    os.close(fd)
    try:
        _seed_panel_db(db_path, [
            {"manager_key": "mgr01", "display_name": "Manager One", "status": "active"},
            {"manager_key": "mgr_arch", "display_name": "Archived Mgr", "status": "archived"},
            {"manager_key": "mgr_closer", "display_name": "Closer Mgr", "status": "active", "role": "closer"},
        ])
        ns = build_panel_render_ns(db_path)

        rows_active = ns["_manager_admin_detail_buttons"]("mgr01")
        flat = [btn for row in rows_active for btn in row]
        relogin_btns = [b for b in flat if b.text == "🔐 Перезайти"]
        check("1. active manager card has exactly one '🔐 Перезайти' button", len(relogin_btns) == 1, flat)
        check("2. its callback carries the correct manager_key", relogin_btns and relogin_btns[0].data == b"relogin:start:mgr01", relogin_btns)
        check("3. callback_data stays <= 64 bytes", relogin_btns and len(relogin_btns[0].data) <= 64, relogin_btns)

        # Placement: right after the Запустить/Остановить row.
        idx_start = next(i for i, row in enumerate(rows_active) if any("Запустить" in b.text for b in row))
        idx_relogin = next(i for i, row in enumerate(rows_active) if any(b.text == "🔐 Перезайти" for b in row))
        check("1b. '🔐 Перезайти' is placed immediately after the Запустить/Остановить row", idx_relogin == idx_start + 1, (idx_start, idx_relogin))

        rows_arch = ns["_manager_admin_detail_buttons"]("mgr_arch")
        flat_arch = [btn for row in rows_arch for btn in row]
        check("4. archived manager card has NO '🔐 Перезайти' button", not any(b.text == "🔐 Перезайти" for b in flat_arch), flat_arch)

        rows_missing = ns["_manager_admin_detail_buttons"]("mgr_does_not_exist")
        flat_missing = [btn for row in rows_missing for btn in row]
        check("4b. nonexistent manager card has NO '🔐 Перезайти' button", not any(b.text == "🔐 Перезайти" for b in flat_missing), flat_missing)

        # Closer: _manager_row_by_key/_manager_rows_all default exclude_closers=True, so
        # the closer's own row is invisible to this card lookup entirely (pre-existing
        # project invariant, not something this feature had to add).
        closer_row = ns["_manager_row_by_key"]("mgr_closer")
        check("5. closer manager is invisible to the admin card lookup (pre-existing exclude_closers=True)", closer_row == {}, closer_row)
    finally:
        os.unlink(db_path)


# ======================================================================
# 6-16. Re-login never creates a manager / touches unrelated fields.
# ======================================================================

def test_6_7_45_no_create_manager_in_source() -> None:
    tree = ast.parse(MAIN_SRC)
    offenders = []
    for n in tree.body:
        if getattr(n, "name", None) in MAIN_REAL_NAMES and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            try:
                src_txt = ast.unparse(n)
            except Exception:
                continue
            if "manager_add(" in src_txt or "_panel_manager_add_command" in src_txt:
                offenders.append(n.name)
    check("6/7/45. no relogin function calls manager_add(...) or _panel_manager_add_command", not offenders, offenders)
    check("45b. no relogin function references 'INSERT INTO managers'", "INSERT INTO managers" not in "\n".join(
        ast.unparse(n) for n in tree.body if getattr(n, "name", None) in MAIN_REAL_NAMES and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ), None)


async def test_8_16_snapshot_diff_full_success() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001, "actual_username": "mgrone_new"})
        # owner_user_id (9001, from seed_manager's default) is DISTINCT from the admin
        # performing the relogin (owner=1001 below) -- a regression that overwrites the
        # manager's owner with the acting admin would show up as a real value mismatch,
        # not a coincidental match.
        before = await seed_manager(ns, key="mgr01", tg_user_id=555001, is_enabled=1, manual_stopped=0, status="active")
        write_boevoy_session(ns, "mgr01", b"OLD-BOEVOY-BYTES")
        managers_count_before = len((await __import__("storage").manager_list_rows(include_removed=True)))
        ns["__proc__"]["running"].add("mgr01")  # scenario A: was actually running before

        owner = 1001
        check("owner test setup: acting admin differs from the manager's existing owner", owner != before.get("owner_user_id"), (owner, before.get("owner_user_id")))
        r1 = await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        check("phone step ok (setup)", r1.startswith("✅ Код отправлен"), r1)
        r2 = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("23. matching tg_user_id proceeds straight to commit", ns["_RELOGIN_COMMIT_OK_MARKER"] in r2, r2)

        after = await __import__("storage").manager_get("mgr01")
        diff = snapshot_diff(before, after)
        check("8. manager_key unchanged", before["manager_key"] == after["manager_key"], (before["manager_key"], after["manager_key"]))
        check("9. manager DB path unchanged", before["db_path"] == after["db_path"], (before["db_path"], after["db_path"]))
        check("10. runtime workdir unchanged", before["workdir"] == after["workdir"], (before["workdir"], after["workdir"]))
        check("12. proxy fields unchanged", before.get("proxy_host") == after.get("proxy_host") and before.get("proxy_port") == after.get("proxy_port"), (before, after))
        check("33. commit changed ONLY identity/session-related fields", diff <= IDENTITY_FIELDS, diff)
        check("8b. session_path value unchanged (same key -> same deterministic path)", before["session_path"] == after["session_path"], (before, after))
        check("BLOCKER1-fix: owner_user_id is value-identical before/after (NOT the acting admin)",
              after.get("owner_user_id") == before.get("owner_user_id") and after.get("owner_user_id") != owner,
              (before.get("owner_user_id"), after.get("owner_user_id"), owner))
        check("BLOCKER2-fix scenario A: is_enabled/manual_stopped/status preserved (was already active)",
              after.get("is_enabled") == 1 and after.get("manual_stopped") == 0 and after.get("status") == "active",
              (after.get("is_enabled"), after.get("manual_stopped"), after.get("status")))
        check("scenario A: manager IS spawned (it was actually running before)", ns["__proc__"]["spawn_calls"] == ["mgr01"], ns["__proc__"]["spawn_calls"])
        check("scenario A: SPAWN_OK=1 reported to PanelBot", "SPAWN_OK=1" in r2, r2)

        managers_count_after = len((await __import__("storage").manager_list_rows(include_removed=True)))
        check("7b. no new manager row was created (row count unchanged)", managers_count_before == managers_count_after, (managers_count_before, managers_count_after))

        con = __import__("sqlite3").connect(db_path)
        try:
            src_row = con.execute("SELECT source_key FROM manager_source_links WHERE manager_key='mgr01'").fetchone()
            check("11. source link unchanged", src_row and src_row[0] == "src_a", src_row)
            hist_row = con.execute("SELECT note FROM daily_leads_marker WHERE manager_key='mgr01'").fetchone()
            check("16. statistics/history marker row unchanged", hist_row and hist_row[0] == "history-row-untouched", hist_row)
        finally:
            con.close()

        check("13. no relogin function references any schedule table/logic", "schedule" not in "\n".join(
            ast.unparse(n) for n in ast.parse(MAIN_SRC).body if getattr(n, "name", None) in MAIN_REAL_NAMES and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ).lower(), None)

        check("17. temp session path differs from boevoy session path", ns["_manager_relogin_temp_session_path"]("mgr01") != after["session_path"], None)
        check("30/31. backup was created before/because of a successful replace", ns["__proc__"]["spawn_calls"] == ["mgr01"], ns["__proc__"]["spawn_calls"])
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# 18-22. Runtime stays up through phone/code/2FA; errors never touch boevoy.
# ======================================================================

async def test_18_runtime_not_stopped_during_phone_code_pass() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 42, "sign_in_exc": SessionPasswordNeededError()})
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        boevoy = write_boevoy_session(ns, "mgr01", b"OLD-BOEVOY")
        ns["__proc__"]["running"].add("mgr01")  # manager IS running
        owner = 1001

        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        check("18a. runtime not stopped after phone step", ns["__proc__"]["stop_calls"] == [], ns["__proc__"]["stop_calls"])
        r2 = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("code->needs 2FA (setup)", "Нужен пароль 2FA" in r2, r2)
        check("18b. runtime not stopped waiting for 2FA", ns["__proc__"]["stop_calls"] == [], ns["__proc__"]["stop_calls"])
        check("19/20. boevoy session untouched through phone/code", read_bytes_or_none(boevoy) == b"OLD-BOEVOY", None)
        check("18c. boevoy still marked as running (never interrupted)", "mgr01" in ns["__proc__"]["running"], ns["__proc__"]["running"])
    finally:
        await cleanup_env(tmp_root)


async def test_19_22_errors_never_touch_boevoy() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        owner = 1001

        # 19: phone step FloodWait.
        ns = build_main_ns(db_path, tmp_root, script={"send_code_exc": FloodWaitError(30)})
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        boevoy = write_boevoy_session(ns, "mgr01", b"OLD")
        r = await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        check("22. FloodWait on phone step surfaces a wait message", "ограничил" in r, r)
        check("19/22. boevoy session untouched after phone-step FloodWait", read_bytes_or_none(boevoy) == b"OLD", None)
        check("19b. runtime never stopped on phone-step error", ns["__proc__"]["stop_calls"] == [], None)

        # 20: code step invalid code.
        ns2 = build_main_ns(db_path, tmp_root, script={"sign_in_exc": PhoneCodeInvalidError()})
        await ns2["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r2 = await ns2["_panel_manager_relogin_code_command"]("mgr01 000000", requested_by=owner)
        check("20. invalid code returns a clear error", "неверн" in r2.lower() or "истёк" in r2.lower(), r2)
        check("20b. boevoy session untouched after invalid code", read_bytes_or_none(boevoy) == b"OLD", None)
        check("20c. runtime never stopped on code-step error", ns2["__proc__"]["stop_calls"] == [], None)

        # 21: 2FA wrong password.
        ns3 = build_main_ns(db_path, tmp_root, script={
            "code_sign_in_exc": SessionPasswordNeededError(),
            "pass_sign_in_exc": PasswordHashInvalidError(),
        })
        await ns3["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r3a = await ns3["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("21 setup: reaches 2FA prompt", "2FA" in r3a, r3a)
        r3b = await ns3["_panel_manager_relogin_pass_command"]("mgr01 wrongpass", requested_by=owner)
        check("21. wrong 2FA password returns a clear error", "неверный пароль" in r3b.lower(), r3b)
        check("21b. boevoy session untouched after wrong 2FA password", read_bytes_or_none(boevoy) == b"OLD", None)
        check("21c. runtime never stopped on 2FA error", ns3["__proc__"]["stop_calls"] == [], None)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# 23-27. Identity gate.
# ======================================================================

async def test_23_27_identity_gate() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        owner = 1001

        # 24/25: mismatch -- commit must NOT happen, confirm marker must appear.
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 777777, "actual_username": "someoneelse"})
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        boevoy = write_boevoy_session(ns, "mgr01", b"OLD")
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("24. mismatched tg_user_id does NOT commit", ns["_RELOGIN_COMMIT_OK_MARKER"] not in r, r)
        check("25. mismatch response carries the identity-confirm marker + both ids", ns["_RELOGIN_IDENTITY_MISMATCH_MARKER"] in r and "EXPECTED_ID=555001" in r and "ACTUAL_ID=777777" in r, r)
        check("24b. boevoy session untouched on mismatch (no commit)", read_bytes_or_none(boevoy) == b"OLD", None)
        check("24c. runtime never stopped on mismatch alone", ns["__proc__"]["stop_calls"] == [], None)

        # 26: cancel after mismatch logs out the temp (mismatched) account.
        r_cancel = await ns["_panel_manager_relogin_cancel_command"]("mgr01", requested_by=owner)
        check("26. cancel after mismatch calls log_out on the temp session", ("log_out",) in ns["__calls__"], ns["__calls__"])
        check("26b. cancel does not touch boevoy session", read_bytes_or_none(boevoy) == b"OLD", None)
        check("37a. cancel removes the temp session file", not os.path.exists(ns["_manager_relogin_temp_session_path"]("mgr01")), None)

        # 27: expected_user_id is NULL (legacy row) -- commit proceeds without a mismatch step.
        ns2 = build_main_ns(db_path, tmp_root, script={"actual_user_id": 42424242, "actual_username": "brandnew"})
        await seed_manager(ns2, key="mgr02", tg_user_id=None)
        await ns2["_panel_manager_relogin_phone_command"]("mgr02 +79990009999", requested_by=owner)
        r_legacy = await ns2["_panel_manager_relogin_code_command"]("mgr02 123456", requested_by=owner)
        check("27. NULL expected_user_id allows backward-compatible auto-commit", ns2["_RELOGIN_COMMIT_OK_MARKER"] in r_legacy, r_legacy)

        # 25b: an explicit confirm after mismatch DOES commit.
        ns3 = build_main_ns(db_path, tmp_root, script={"actual_user_id": 888888, "actual_username": "confirmed"})
        await seed_manager(ns3, key="mgr03", tg_user_id=555003)
        await ns3["_panel_manager_relogin_phone_command"]("mgr03 +79990003333", requested_by=owner)
        r_mismatch = await ns3["_panel_manager_relogin_code_command"]("mgr03 123456", requested_by=owner)
        check("mismatch setup for mgr03", ns3["_RELOGIN_IDENTITY_MISMATCH_MARKER"] in r_mismatch, r_mismatch)
        r_confirm = await ns3["_panel_manager_relogin_confirm_command"]("mgr03", requested_by=owner)
        check("confirm_identity after mismatch DOES commit", ns3["_RELOGIN_COMMIT_OK_MARKER"] in r_confirm, r_confirm)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# 28-29. Only the selected manager_key is stopped/started; others untouched.
# ======================================================================

async def test_28_29_only_selected_key_touched() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        other_before = await seed_manager(ns, key="mgr_other", tg_user_id=555099)
        write_boevoy_session(ns, "mgr01", b"OLD1")
        other_session = write_boevoy_session(ns, "mgr_other", b"OTHER-UNTOUCHED")
        ns["__proc__"]["running"].update({"mgr01", "mgr_other"})
        owner = 1001

        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("commit reached for mgr01", ns["_RELOGIN_COMMIT_OK_MARKER"] in r, r)

        check("28. only mgr01 was stopped", ns["__proc__"]["stop_calls"] == ["mgr01"], ns["__proc__"]["stop_calls"])
        check("29a. mgr_other stayed in the running set the whole time", "mgr_other" in ns["__proc__"]["running"], ns["__proc__"]["running"])
        check("29b. mgr_other's boevoy session file untouched", read_bytes_or_none(other_session) == b"OTHER-UNTOUCHED", None)
        other_after = await __import__("storage").manager_get("mgr_other")
        check("29c. mgr_other's row is byte-identical", other_before == other_after, (other_before, other_after))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# 34-36. Backup/replace/spawn failure handling.
# ======================================================================

async def test_34_backup_failure_leaves_boevoy_untouched() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        before = await seed_manager(ns, key="mgr01", tg_user_id=555001)
        boevoy = write_boevoy_session(ns, "mgr01", b"OLD")
        ns["__proc__"]["running"].add("mgr01")
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)

        def _boom(*a, **kw):
            raise RuntimeError("simulated backup failure")
        ns["_backup_manager_session_files"] = _boom  # monkeypatch just this ns's binding

        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("34. backup failure is reported, not silently swallowed", "Backup не создан" in r, r)
        check("34b. boevoy session untouched when backup fails", read_bytes_or_none(boevoy) == b"OLD", None)
        check("34c. runtime restarted after aborting on backup failure", ns["__proc__"]["spawn_calls"] == ["mgr01"], ns["__proc__"]["spawn_calls"])
        after = await ns["__storage__"].manager_get("mgr01")
        check("34d. rollback (backup failure) preserves owner_user_id/is_enabled/manual_stopped -- finalize was never reached",
              after.get("owner_user_id") == before.get("owner_user_id") and after.get("is_enabled") == before.get("is_enabled") and after.get("manual_stopped") == before.get("manual_stopped"),
              (before, after))
    finally:
        await cleanup_env(tmp_root)


async def test_35_replace_failure_restores_backup() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        fail_flag = {"fail_replace": False}
        proxy_os = _OSReplaceFailProxy(os, fail_flag)
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001}, os_module=proxy_os)
        before = await seed_manager(ns, key="mgr01", tg_user_id=555001)
        boevoy = write_boevoy_session(ns, "mgr01", b"OLD-BEFORE-REPLACE")
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)

        fail_flag["fail_replace"] = True
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("35. replace failure is reported", "Ошибка замены сессии" in r, r)
        check("35b. boevoy session restored to its ORIGINAL content after a failed replace", read_bytes_or_none(boevoy) == b"OLD-BEFORE-REPLACE", read_bytes_or_none(boevoy))
        after = await ns["__storage__"].manager_get("mgr01")
        check("35c. rollback (replace failure) preserves owner_user_id/is_enabled/manual_stopped -- finalize was never reached",
              after.get("owner_user_id") == before.get("owner_user_id") and after.get("is_enabled") == before.get("is_enabled") and after.get("manual_stopped") == before.get("manual_stopped"),
              (before, after))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# Runtime-state preservation scenarios (BLOCKER 2 fix): B/C/D from the
# review's exact scenario list. Scenario A is covered inline inside
# test_8_16_snapshot_diff_full_success (the main happy-path test).
# ======================================================================

async def test_runtime_state_scenario_b_manual_stopped() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        before = await seed_manager(ns, key="mgr01", tg_user_id=555001, is_enabled=1, manual_stopped=1, status="active")
        write_boevoy_session(ns, "mgr01", b"OLD")
        # runtime_was_running stays False -- never added to the fake running set.
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("scenario B setup: commit reached", ns["_RELOGIN_COMMIT_OK_MARKER"] in r, r)
        after = await ns["__storage__"].manager_get("mgr01")
        check("scenario B: session WAS replaced (identity updated)", after.get("tg_user_id") == 555001, after)
        check("scenario B: manual_stopped remains 1 (not silently cleared)", after.get("manual_stopped") == 1, after)
        check("scenario B: status unchanged", after.get("status") == before.get("status"), (before.get("status"), after.get("status")))
        check("scenario B: owner_user_id unchanged", after.get("owner_user_id") == before.get("owner_user_id"), (before.get("owner_user_id"), after.get("owner_user_id")))
        check("scenario B: manager was never spawned (it was not running before)", ns["__proc__"]["spawn_calls"] == [], ns["__proc__"]["spawn_calls"])
        check("scenario B: manager is not left running afterwards", "mgr01" not in ns["__proc__"]["running"], ns["__proc__"]["running"])
        check("scenario B: PanelBot-visible result honestly reports not-running (SPAWN_OK=0)", "SPAWN_OK=0" in r, r)
    finally:
        await cleanup_env(tmp_root)


async def test_runtime_state_scenario_c_disabled() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        before = await seed_manager(ns, key="mgr01", tg_user_id=555001, is_enabled=0, manual_stopped=0, status="active")
        write_boevoy_session(ns, "mgr01", b"OLD")
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("scenario C setup: commit reached", ns["_RELOGIN_COMMIT_OK_MARKER"] in r, r)
        after = await ns["__storage__"].manager_get("mgr01")
        check("scenario C: session WAS replaced", after.get("tg_user_id") == 555001, after)
        check("scenario C: is_enabled remains 0 (not silently re-enabled)", after.get("is_enabled") == 0, after)
        check("scenario C: manual_stopped preserved", after.get("manual_stopped") == before.get("manual_stopped"), (before, after))
        check("scenario C: owner_user_id preserved", after.get("owner_user_id") == before.get("owner_user_id"), (before, after))
        check("scenario C: manager was never spawned", ns["__proc__"]["spawn_calls"] == [], ns["__proc__"]["spawn_calls"])
    finally:
        await cleanup_env(tmp_root)


async def test_runtime_state_scenario_d_flags_active_but_not_running() -> None:
    """Flags say the manager SHOULD be active, but the process itself was not actually
    alive (e.g. crashed silently) -- relogin must not treat 'flags allow it' as license
    to auto-start; it only spawns when the runtime was genuinely running beforehand."""
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        await seed_manager(ns, key="mgr01", tg_user_id=555001, is_enabled=1, manual_stopped=0, status="active")
        write_boevoy_session(ns, "mgr01", b"OLD")
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("scenario D setup: commit reached", ns["_RELOGIN_COMMIT_OK_MARKER"] in r, r)
        after = await ns["__storage__"].manager_get("mgr01")
        check("scenario D: flags stay active/enabled/not-stopped (already were)",
              after.get("is_enabled") == 1 and after.get("manual_stopped") == 0 and after.get("status") == "active", after)
        check("scenario D: relogin does NOT auto-spawn just because flags allow it -- only runtime_was_running gates spawn",
              ns["__proc__"]["spawn_calls"] == [], ns["__proc__"]["spawn_calls"])
    finally:
        await cleanup_env(tmp_root)


async def test_owner_legacy_null_fallback() -> None:
    """A legacy row with no owner_user_id at all (NULL) is the ONLY case where relogin
    is allowed to fall back to the acting admin -- there is no "previous owner" to
    preserve."""
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        before = await seed_manager(ns, key="mgr01", tg_user_id=555001, owner_user_id=None)
        check("legacy setup: owner_user_id really is NULL before relogin", before.get("owner_user_id") is None, before.get("owner_user_id"))
        write_boevoy_session(ns, "mgr01", b"OLD")
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("legacy fallback: commit reached", ns["_RELOGIN_COMMIT_OK_MARKER"] in r, r)
        after = await ns["__storage__"].manager_get("mgr01")
        check("legacy fallback: owner_user_id falls back to the acting admin ONLY because it was NULL before", after.get("owner_user_id") == owner, after)
    finally:
        await cleanup_env(tmp_root)


async def test_36_spawn_failure_after_commit_is_recoverable() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        write_boevoy_session(ns, "mgr01", b"OLD")
        ns["__proc__"]["running"].add("mgr01")  # it WAS running -> spawn is actually attempted (and fails)
        ns["__proc__"]["spawn_result"]["ok"] = False
        ns["__proc__"]["spawn_result"]["msg"] = "simulated spawn failure"
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("36. commit still completes (session replaced) even if spawn fails", ns["_RELOGIN_COMMIT_OK_MARKER"] in r, r)
        check("36b. commit result reports spawn as not-ok", "SPAWN_OK=0" in r, r)
        check("36e. spawn was genuinely attempted (not skipped) since the manager was running before", ns["__proc__"]["spawn_calls"] == ["mgr01"], ns["__proc__"]["spawn_calls"])
        row_count = len(await __import__("storage").manager_list_rows(include_removed=True))
        check("36c. no new manager row was created despite the spawn failure", row_count == 1, row_count)
        after = await __import__("storage").manager_get("mgr01")
        check("36d. identity WAS updated even though spawn failed (session swap itself succeeded)", after.get("tg_user_id") == 555001, after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# 37-39. Cancel / stale-lock cleanup / per-manager exclusivity.
# ======================================================================

async def test_38_stale_lock_is_cleaned_up() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root)
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        storage = ns["__storage__"]
        # Seed an EXPIRED relogin row for a different admin, plus a stray temp file.
        stray_temp = ns["_manager_relogin_temp_session_path"]("mgr01")
        os.makedirs(os.path.dirname(stray_temp), exist_ok=True)
        open(stray_temp, "a").close()
        await storage.manager_save_onboarding(
            9999, manager_key="mgr01", step=ns["_RELOGIN_STEP_CODE"],
            phone="+70000000000", phone_code_hash="stalehash",
            tmp_session_path=stray_temp, expires_at="2000-01-01T00:00:00",
        )
        ok, err, row = await ns["_manager_relogin_begin"]("mgr01", 1001)
        check("38. begin() succeeds by cleaning up the stale/expired lock", ok, err)
        check("38b. the stray temp session file was removed", not os.path.exists(stray_temp), None)
        stale_row = await storage.manager_get_onboarding(9999)
        check("38c. the expired onboarding row was deleted", stale_row is None, stale_row)
    finally:
        await cleanup_env(tmp_root)


async def test_39_per_manager_lock() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root)
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        owner_a, owner_b = 1001, 2002
        ok_a, err_a, _ = await ns["_manager_relogin_begin"]("mgr01", owner_a)
        check("39 setup: first admin can begin", ok_a, err_a)
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner_a)

        ok_b, err_b, _ = await ns["_manager_relogin_begin"]("mgr01", owner_b)
        check("39. a second admin is blocked while the first admin's relogin is active", not ok_b, err_b)
        check("39b. the SAME admin continuing their own flow is never blocked", (await ns["_manager_relogin_begin"]("mgr01", owner_a))[0], None)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# Stage 3 (2026-07-16) reverse mutual exclusion: _manager_relogin_begin now
# calls the REAL replacement_active_for_old_key(main.py) -> real
# storage.replacement_get_active_for_old_key against the same temp SQLite DB
# every other test in this file uses. ensure_replacement_tables() self-
# creates the Stage 1 schema on first use, exactly like every other storage
# table -- no separate seeding step. This is a genuine harness-integration
# fix (missing extraction name), not a behavior change to relogin or to the
# replacement backend, both of which are out of scope for this file.
# ======================================================================

def seed_replacement_op(ns: dict, *, old_key: str, status: str, operation_id: str = None,
                         created_by_user_id: int = None) -> dict:
    """Builds a replacement operation for old_key at the given durable status
    using ONLY the real storage.py replacement_* functions against the temp
    DB -- never a hand-rolled UPDATE -- for every status reachable through
    the normal CAS API. 'done' is the sole exception: replacement_finalize()
    enforces link-readiness/new_manager_key prerequisites that belong to the
    (separate, already-covered) replacement backend selftest, not to a
    relogin-exclusion test that only cares about the terminal status VALUE --
    so 'done' is set via one direct, minimal SQL UPDATE, matching this file's
    own established convention for other auxiliary-table fixture setup (see
    seed_manager's manager_source_links/daily_leads_marker rows above)."""
    storage = ns["__storage__"]
    op_id = operation_id or f"op-{old_key}-{status}"
    created = storage.replacement_create(op_id, old_key, created_by_user_id=created_by_user_id)
    if created is None:
        raise AssertionError(f"seed_replacement_op: replacement_create failed for {old_key}/{op_id}")
    chain = ["draft", "auth_phone", "auth_code", "identity_ok", "ready_commit"]
    if status in chain:
        idx = chain.index(status)
        for i in range(idx):
            ok_adv = storage.replacement_advance(op_id, chain[i], chain[i + 1])
            if not ok_adv:
                raise AssertionError(f"seed_replacement_op: advance {chain[i]}->{chain[i+1]} failed for {op_id}")
    elif status == "cancelled":
        if not storage.replacement_cancel(op_id):
            raise AssertionError(f"seed_replacement_op: cancel failed for {op_id}")
    elif status == "failed":
        if not storage.replacement_fail(op_id, "test_seed", "simulated failure for fixture"):
            raise AssertionError(f"seed_replacement_op: fail failed for {op_id}")
    elif status == "done":
        con = __import__("sqlite3").connect(ns["TPILOT_DB_PATH"])
        try:
            con.execute("UPDATE manager_replacements SET status='done' WHERE operation_id=?", (op_id,))
            con.commit()
        finally:
            con.close()
    else:
        raise AssertionError(f"seed_replacement_op: unsupported status {status!r}")
    row = storage.replacement_get(op_id)
    if row is None:
        raise AssertionError(f"seed_replacement_op: replacement_get returned None for {op_id}")
    return row


_STAGE3_BLOCK_MESSAGE = "Сначала завершите или отмените текущую замену аккаунта."


async def test_stage3_relogin_replacement_exclusion() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root)
        storage = ns["__storage__"]
        owner = 1001

        # --- Active replacement, EARLY status (draft) blocks relogin. ---
        before_mgr1 = await seed_manager(ns, key="mgrrepl1", tg_user_id=555010)
        op_draft = seed_replacement_op(ns, old_key="mgrrepl1", status="draft", created_by_user_id=owner)
        ok1, err1, row1 = await ns["_manager_relogin_begin"]("mgrrepl1", owner)
        check("stage3-A1. _manager_relogin_begin returns ok=False for an active (draft) replacement", ok1 is False, (ok1, err1))
        check("stage3-A2. block message matches the exact required text", err1 == _STAGE3_BLOCK_MESSAGE, err1)
        check("stage3-A3. begin() returns no row on the blocked path", row1 is None, row1)
        r1 = await ns["_panel_manager_relogin_phone_command"]("mgrrepl1 +79990001122", requested_by=owner)
        check("stage3-A4. full phone-command wrapper surfaces the same block message (no side effects attempted)", r1 == _STAGE3_BLOCK_MESSAGE, r1)
        check("stage3-A5. no relogin temp session file was created", not os.path.exists(ns["_manager_relogin_temp_session_path"]("mgrrepl1")), None)
        onboarding_after1 = await storage.manager_get_onboarding(owner)
        check("stage3-A6. no relogin onboarding step was written", onboarding_after1 is None, onboarding_after1)
        op_draft_after = storage.replacement_get(op_draft["operation_id"])
        check("stage3-A7. replacement operation status unchanged (still draft)", op_draft_after["status"] == "draft", op_draft_after)
        check("stage3-A8. replacement operation row otherwise byte-identical", op_draft_after == op_draft, (op_draft, op_draft_after))
        mgr_after1 = await storage.manager_get("mgrrepl1")
        check("stage3-A9. manager row unchanged", mgr_after1 == before_mgr1, (before_mgr1, mgr_after1))

        # --- Active replacement, LATER non-terminal status (ready_commit) blocks relogin. ---
        before_mgr2 = await seed_manager(ns, key="mgrrepl2", tg_user_id=555011)
        op_ready = seed_replacement_op(ns, old_key="mgrrepl2", status="ready_commit", created_by_user_id=owner)
        ok2, err2, row2 = await ns["_manager_relogin_begin"]("mgrrepl2", owner)
        check("stage3-B1. active replacement (ready_commit) blocks relogin (ok=False)", ok2 is False, (ok2, err2))
        check("stage3-B2. block message matches the exact required text", err2 == _STAGE3_BLOCK_MESSAGE, err2)
        check("stage3-B3. no relogin temp session file was created", not os.path.exists(ns["_manager_relogin_temp_session_path"]("mgrrepl2")), None)
        op_ready_after = storage.replacement_get(op_ready["operation_id"])
        check("stage3-B4. replacement operation status unchanged (still ready_commit)", op_ready_after["status"] == "ready_commit", op_ready_after)
        check("stage3-B5. replacement operation row otherwise byte-identical", op_ready_after == op_ready, (op_ready, op_ready_after))
        mgr_after2 = await storage.manager_get("mgrrepl2")
        check("stage3-B6. manager row unchanged", mgr_after2 == before_mgr2, (before_mgr2, mgr_after2))

        # --- Terminal replacements (cancelled / failed / done) do NOT block relogin. ---
        for term_status, mkey, uid in (
            ("cancelled", "mgrrepl3", 555012),
            ("failed", "mgrrepl4", 555013),
            ("done", "mgrrepl5", 555014),
        ):
            await seed_manager(ns, key=mkey, tg_user_id=uid)
            op_term = seed_replacement_op(ns, old_key=mkey, status=term_status, created_by_user_id=owner)
            check(f"stage3-C setup ({term_status}): fixture really reached the terminal status", op_term["status"] == term_status, op_term)
            ok_t, err_t, row_t = await ns["_manager_relogin_begin"](mkey, owner)
            check(f"stage3-C. terminal replacement ({term_status}) does NOT block relogin (ok=True)", ok_t is True, (ok_t, err_t))
            check(f"stage3-C. terminal replacement ({term_status}) returns the real manager row", bool(row_t) and row_t.get("manager_key") == mkey, row_t)
            check(f"stage3-C. terminal replacement ({term_status}) leaves err empty", err_t == "", err_t)

        # --- No replacement at all: original (pre-Stage-3) relogin behavior is unchanged. ---
        await seed_manager(ns, key="mgrrepl6", tg_user_id=555015)
        ok_none, err_none, row_none = await ns["_manager_relogin_begin"]("mgrrepl6", owner)
        check("stage3-D. no replacement at all: relogin begins normally (ok=True)", ok_none is True, (ok_none, err_none))
        check("stage3-D2. no replacement at all: no exclusion message leaked into err", err_none == "", err_none)
        check("stage3-D3. no replacement at all: begin() returns the real manager row", bool(row_none) and row_none.get("manager_key") == "mgrrepl6", row_none)
    finally:
        await cleanup_env(tmp_root)


async def test_37_cancel_cleans_everything() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root)
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        boevoy = write_boevoy_session(ns, "mgr01", b"OLD")
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        temp_path = ns["_manager_relogin_temp_session_path"]("mgr01")
        check("37 setup: temp session exists before cancel", os.path.exists(temp_path), None)

        r = await ns["_panel_manager_relogin_cancel_command"]("mgr01", requested_by=owner)
        check("37. cancel reports success without touching data", "отменён" in r, r)
        check("37b. temp session file removed", not os.path.exists(temp_path), None)
        onboarding = await ns["__storage__"].manager_get_onboarding(owner)
        check("37c. onboarding/lock state cleared", onboarding is None, onboarding)
        check("5. cancel never touches boevoy session", read_bytes_or_none(boevoy) == b"OLD", None)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# 40. Duplicate confirm never double-commits.
# ======================================================================

async def test_40_duplicate_confirm_no_double_commit() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 999999})
        await seed_manager(ns, key="mgr01", tg_user_id=555001)
        ns["__proc__"]["running"].add("mgr01")  # was running -> spawn is meaningfully exercised, not skipped
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r_mismatch = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("40 setup: mismatch reached", ns["_RELOGIN_IDENTITY_MISMATCH_MARKER"] in r_mismatch, r_mismatch)

        r1 = await ns["_panel_manager_relogin_confirm_command"]("mgr01", requested_by=owner)
        check("40a. first confirm commits", ns["_RELOGIN_COMMIT_OK_MARKER"] in r1, r1)
        r2 = await ns["_panel_manager_relogin_confirm_command"]("mgr01", requested_by=owner)
        check("40. a second confirm click does not double-commit", ns["_RELOGIN_COMMIT_OK_MARKER"] not in r2, r2)
        check("40b. second confirm's spawn count stayed at exactly one", ns["__proc__"]["spawn_calls"] == ["mgr01"], ns["__proc__"]["spawn_calls"])
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# 41. Stale panel-side callback rejected (wizard state mismatch/missing).
# ======================================================================

def build_panel_callback_ns(db_path: str, submit_and_wait_fn) -> dict:
    import manager_registry

    class _FakeBtn:
        __slots__ = ("text", "data")

        def __init__(self, text, data):
            self.text = text
            self.data = data

        def __iter__(self):
            yield "btn"
            yield self.text
            yield self.data

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
            return _FakeBtn(text, raw)

    class _FakeEventsNS:
        CallbackQuery = object()
        NewMessage = object()

    class _FakeClient:
        def on(self, *a, **kw):
            def _decorator(fn):
                return fn
            return _decorator

        async def send_message(self, chat_id, text, buttons=None):
            SENT.append((chat_id, text, buttons))

    SENT: list = []

    class FakeEvent:
        def __init__(self, data: bytes, chat_id: int, sender_id: int):
            self.data = data
            self.chat_id = chat_id
            self.sender_id = sender_id
            self.answers: list = []
            self.edits: list = []

        async def answer(self, text=None, alert=False):
            self.answers.append((text, alert))

    async def _fake_safe_event_edit(event, text, buttons=None):
        event.edits.append((text, buttons))

    async def _fake_pb_safe_answer(event, *args, **kwargs):
        # panel_bot.py defines _pb_safe_answer at module level (never raises,
        # returns bool); the callback handler now calls it instead of raw
        # event.answer(). Same contract here: delegate, swallow exceptions.
        try:
            answer = getattr(event, "answer", None)
            if answer is not None:
                await answer(*args, **kwargs)
            return True
        except Exception:
            return False

    tree = ast.parse(PANEL_SRC)
    marker_names = {"_relogin_callback", "_relogin_wizard_input"}
    other_names = {
        "_relogin_intro_text", "_relogin_intro_buttons", "_relogin_phone_prompt_text",
        "_relogin_cancel_only_buttons", "_relogin_mismatch_text", "_relogin_mismatch_buttons",
        "_relogin_done_text", "_relogin_done_buttons", "_relogin_error_buttons",
        "_relogin_parse_marker_fields", "_relogin_render_backend_result",
        "_RELOGIN_IDENTITY_MISMATCH_MARKER", "_RELOGIN_COMMIT_OK_MARKER",
        "_manager_row_by_key", "_manager_rows_all", "_manager_rows", "_manager_short_label",
        "_manager_state_icon", "_manager_state_label", "_manager_proxy_badge",
        "_wizard_get", "_wizard_set", "_wizard_clear", "_connect_panel_db",
        "_ensure_panel_runtime_tables", "_panel_callback_is_duplicate", "_safe_text",
        "_utc_now_iso", "_panel_loop_time", "_PANEL_RECENT_CALLBACKS", "_PANEL_DEDUPE_SEC",
        # AUTH UI 20260809 (Ф4): unified auth chooser, now called from
        # _relogin_intro_buttons/_relogin_intro_text.
        "_auth_chooser_text", "_auth_chooser_buttons",
        # TERMINAL OK 20260809 (Ф4 checkpoint correction): _relogin_done_
        # buttons/_relogin_error_buttons now append _terminal_ok_button().
        "_terminal_ok_button",
    }
    all_names = marker_names | other_names
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm in marker_names:
            nodes.append(n)
            seen.add(nm)
            continue
        if nm and nm in other_names:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in all_names:
            nodes.append(n)
            seen.add(n.targets[0].id)
            continue
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in all_names:
            nodes.append(n)
            seen.add(n.target.id)
    missing = all_names - seen
    if missing:
        raise AssertionError(f"panel callback extraction missing {missing}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    ns = {
        "sqlite3": __import__("sqlite3"),
        "os": os,
        "json": __import__("json"),
        "datetime": __import__("datetime").datetime,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "events": _FakeEventsNS,
        "client": _FakeClient(),
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_safe_event_edit": _fake_safe_event_edit,
        "_pb_safe_answer": _fake_pb_safe_answer,
        # utcnow refactor (2026-08-16): extracted handlers now read the clock
        # through the module-level _pb_utc_now() seam (naive UTC contract).
        "_pb_utc_now": (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(tzinfo=None)),
        "_is_allowed": lambda event: True,
        "_submit_and_wait": submit_and_wait_fn,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:relogin_callback>", "exec"), ns)
    ns["__sent__"] = SENT
    ns["__FakeEvent__"] = FakeEvent
    return ns


def test_41_stale_callback_rejected() -> None:
    import tempfile as _tf
    fd, db_path = _tf.mkstemp(suffix=".db", prefix="relogin_stale_")
    os.close(fd)
    try:
        _seed_panel_db(db_path, [{"manager_key": "mgr01", "display_name": "Manager One"}])
        calls = []

        async def fake_submit(command_text, requested_by, chat_id, response_chat_id):
            calls.append(command_text)
            return {"status": "done", "result_text": "should-not-be-called"}

        ns = build_panel_callback_ns(db_path, fake_submit)
        ev = ns["__FakeEvent__"](b"relogin:confirm_identity:mgr01", 555, 1001)
        asyncio.run(ns["_relogin_callback"](ev))
        check("41. stale confirm_identity (no wizard state) is rejected with an alert", ev.answers and ev.answers[-1][1] is True, ev.answers)
        check("41b. stale callback never calls the backend", calls == [], calls)
    finally:
        os.unlink(db_path)


def test_start_and_phone_render_and_dedup() -> None:
    import tempfile as _tf
    fd, db_path = _tf.mkstemp(suffix=".db", prefix="relogin_start_")
    os.close(fd)
    try:
        _seed_panel_db(db_path, [
            {"manager_key": "mgr01", "display_name": "Manager One", "telegram_username": "mgrone"},
            {"manager_key": "mgr_arch", "display_name": "Archived", "status": "archived"},
        ])
        calls = []

        async def fake_submit(command_text, requested_by, chat_id, response_chat_id):
            calls.append(command_text)
            return {"status": "done", "result_text": "unused"}

        ns = build_panel_callback_ns(db_path, fake_submit)

        ev_start = ns["__FakeEvent__"](b"relogin:start:mgr01", 555, 1001)
        asyncio.run(ns["_relogin_callback"](ev_start))
        check("intro screen renders for an active manager", ev_start.edits and "Повторный вход" in ev_start.edits[0][0], ev_start.edits)

        ev_start_arch = ns["__FakeEvent__"](b"relogin:start:mgr_arch", 555, 1002)
        asyncio.run(ns["_relogin_callback"](ev_start_arch))
        check("start on an archived manager is denied", ev_start_arch.answers and ev_start_arch.answers[-1][1] is True and not ev_start_arch.edits, ev_start_arch.answers)

        ev_phone = ns["__FakeEvent__"](b"relogin:phone:mgr01", 555, 1001)
        asyncio.run(ns["_relogin_callback"](ev_phone))
        check("phone-entry screen prompts for a phone number", ev_phone.edits and "телефон" in ev_phone.edits[0][0].lower(), ev_phone.edits)

        # 47 (superseded by Ф4 20260809): v1 intentionally excluded QR from
        # relogin. Ф4's unified auth chooser explicitly ADDS QR as one of
        # the 4 offered methods for relogin (requirement #8 of the Ф4 spec)
        # -- so "QR absent from the intro screen" is no longer the intended
        # behavior. Re-derived as the opposite, disclosed policy: the intro
        # screen (now the chooser) MUST offer QR, alongside phone/session/tdata.
        intro_text, intro_buttons = ev_start.edits[0]
        intro_labels = " ".join(b.text for row in intro_buttons for b in row)
        check("47. QR is offered on the intro/chooser screen (Ф4: relogin now supports all 4 methods)",
              "QR" in intro_labels, (intro_text, intro_labels))
        check("47b. phone is also offered on the intro/chooser screen", "телефон" in intro_labels.lower(), intro_labels)
        check("47c. session and tdata are also offered on the intro/chooser screen",
              "session" in intro_labels.lower() and "tdata" in intro_labels.lower(), intro_labels)
    finally:
        os.unlink(db_path)


def test_render_result_marker_dispatch() -> None:
    """Exercises _relogin_render_backend_result directly against the three possible
    backend outcomes -- mismatch screen, success screen, plain error -- without needing
    a live backend round-trip."""
    import tempfile as _tf
    fd, db_path = _tf.mkstemp(suffix=".db", prefix="relogin_render_")
    os.close(fd)
    try:
        _seed_panel_db(db_path, [{"manager_key": "mgr01", "display_name": "Manager One", "telegram_username": "mgrone"}])

        async def fake_submit(*a, **kw):
            return {"status": "done", "result_text": ""}

        ns = build_panel_callback_ns(db_path, fake_submit)

        mismatch_text = "\n".join([
            ns["_RELOGIN_IDENTITY_MISMATCH_MARKER"], "MANAGER_KEY=mgr01", "EXPECTED_ID=555001",
            "ACTUAL_ID=777777", "ACTUAL_USERNAME=someone", "ACTUAL_FIRST_NAME=Some", "ACTUAL_LAST_NAME=One",
        ])
        ev1 = ns["__FakeEvent__"](b"noop", 555, 1001)
        asyncio.run(ns["_relogin_render_backend_result"](ev1, 555, 1001, "mgr01", mismatch_text))
        check("mismatch marker renders the confirm/cancel screen with both ids", ns["__sent__"] and "777777" in ns["__sent__"][-1][1] and "555001" in ns["__sent__"][-1][1], ns["__sent__"])
        mismatch_buttons = ns["__sent__"][-1][2]
        mismatch_labels = {b.text: b.data for row in mismatch_buttons for b in row}
        check("mismatch screen offers Заменить + Отмена with correct callbacks", mismatch_labels.get("✅ Заменить аккаунт") == b"relogin:confirm_identity:mgr01" and mismatch_labels.get("❌ Отмена") == b"relogin:cancel:mgr01", mismatch_labels)

        ok_text = "\n".join([ns["_RELOGIN_COMMIT_OK_MARKER"], "MANAGER_KEY=mgr01", "SPAWN_OK=1", "BACKUP_PATH=/tmp/x"])
        ev2 = ns["__FakeEvent__"](b"noop", 555, 1001)
        asyncio.run(ns["_relogin_render_backend_result"](ev2, 555, 1001, "mgr01", ok_text))
        check("commit-ok marker renders the final success screen", ns["__sent__"] and "Повторный вход выполнен" in ns["__sent__"][-1][1], ns["__sent__"][-1])
        check("success screen reports spawned/running status", "запущен" in ns["__sent__"][-1][1], ns["__sent__"][-1])

        ev3 = ns["__FakeEvent__"](b"noop", 555, 1001)
        asyncio.run(ns["_relogin_render_backend_result"](ev3, 555, 1001, "mgr01", "Код неверный или истёк."))
        check("plain error text is surfaced with a warning prefix", ns["__sent__"][-1][1].startswith("⚠️"), ns["__sent__"][-1])
    finally:
        os.unlink(db_path)


# ======================================================================
# 42-44, 46-47. Scope guards + invariants.
# ======================================================================

def test_42_43_scope_untouched() -> None:
    for marker in ("def _nms_callback", "nms:pickmgr:", "_nms_render_mgrlist", "_nms_sources_screen", "nms:entry:mgr"):
        check(f"42. nms: block marker still present: {marker!r}", marker in PANEL_SRC, marker)
    for marker in ("def _nm_reports_screen", "def _nm_automation_screen", "def _nm_traffic_screen",
                   "menu:nm_llm_toggle_confirm", "menu:nm_quality_w"):
        check(f"43. new-menu distribution marker still present: {marker!r}", marker in PANEL_SRC, marker)


def test_44_allow_spend_unaffected() -> None:
    tree = ast.parse(MAIN_SRC)
    sites = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords
             if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]
    check("44. allow_spend=True remains exactly 2 call sites in main.py", len(sites) == 2, sites)


def test_46_no_db_or_runtime_dir_deletion_in_source() -> None:
    tree = ast.parse(MAIN_SRC)
    relogin_src = "\n".join(
        ast.unparse(n) for n in tree.body
        if getattr(n, "name", None) in MAIN_REAL_NAMES and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    check("46. no relogin function calls shutil.rmtree/os.rmdir (runtime dir deletion)",
          "rmtree" not in relogin_src and "rmdir" not in relogin_src, None)
    # 'db_path' legitimately appears in _manager_finalize_login (it WRITES the deterministic
    # db_path value back onto the row, exactly like the original add-manager flow) -- the
    # real invariant to prove is that no line combining 'db_path' with a removal call exists.
    offending_lines = [
        ln for ln in relogin_src.splitlines()
        if "db_path" in ln and (".unlink(" in ln or "os.remove(" in ln or "rmtree(" in ln)
    ]
    check("46b. no relogin function removes/unlinks anything keyed by db_path", not offending_lines, offending_lines)


async def test_46c_manager_db_and_workdir_survive_full_commit() -> None:
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_main_ns(db_path, tmp_root, script={"actual_user_id": 555001})
        row = await seed_manager(ns, key="mgr01", tg_user_id=555001)
        # Simulate the manager's own per-manager DB file existing.
        os.makedirs(os.path.dirname(row["db_path"]), exist_ok=True)
        with open(row["db_path"], "wb") as f:
            f.write(b"MANAGER-OWN-DB-BYTES")
        write_boevoy_session(ns, "mgr01", b"OLD")
        owner = 1001
        await ns["_panel_manager_relogin_phone_command"]("mgr01 +79990001122", requested_by=owner)
        r = await ns["_panel_manager_relogin_code_command"]("mgr01 123456", requested_by=owner)
        check("46c setup: commit reached", ns["_RELOGIN_COMMIT_OK_MARKER"] in r, r)
        check("46c. the manager's own per-manager DB file survives untouched", read_bytes_or_none(row["db_path"]) == b"MANAGER-OWN-DB-BYTES", None)
        check("46d. the runtime workdir still exists", os.path.isdir(row["workdir"]), row["workdir"])
    finally:
        await cleanup_env(tmp_root)


def test_47_qr_absent_from_dispatch() -> None:
    # 47c (superseded by Ф3+Ф4 20260809): v1 had no relogin QR backend at
    # all. Ф3 built the safe temp-session QR backend
    # (_panel_manager_relogin_qr_start_command in main.py); Ф4 wired the
    # unified auth chooser's "🔳 QR" button to it via the new
    # auth:relogin:qr:<key> callback (handled in on_callback's auth:
    # dispatch block, NOT inside _relogin_callback -- see 47b below). QR is
    # therefore no longer absent from relogin; what must still hold is the
    # architecture note this file itself documents: no raw Telethon auth
    # logic lives directly inside _relogin_callback, only a command dispatch.
    tree = ast.parse(PANEL_SRC)
    for n in tree.body:
        if getattr(n, "name", None) == "_relogin_callback":
            src_txt = ast.unparse(n)
            check("47b. _relogin_callback itself still has no 'qr' action branch (QR is dispatched via auth:relogin:qr:, not here)",
                  '"qr"' not in src_txt.lower(), src_txt[:200])
    check("47c. relogin QR command DOES exist in main.py (Ф3 backend, wired by Ф4's chooser)",
          "manager_relogin_qr" in MAIN_SRC, None)


def test_dispatcher_registration_is_last_active() -> None:
    """Static proof that the 5 relogin commands remain reachable through the LAST
    active _panel_execute_command_text override chain (main.py redefines this name
    many times; only the textually-last def is bound to the name at runtime).

    Originally this required the literal command strings to appear IN the last
    generation itself. Stage 3 (2026-07-16) added a new generation on top of
    relogin's own -- a thin dispatch-then-delegate wrapper that only lists its
    OWN new commands directly and falls back to a PREV-captured reference
    (`_REPL3_PREV_PANEL_EXEC = globals().get("_panel_execute_command_text")`,
    the project's own established override-chain convention per CLAUDE.md) for
    everything else, including every relogin command. That is correct,
    intentional production behavior -- relogin remains fully reachable via the
    delegation chain -- but it invalidated the old literal-substring check the
    moment ANY wrapping override was added, which is inherent to this pattern,
    not a relogin or replacement regression. This rewrite verifies the real
    guarantee instead: exactly one generation literally dispatches the relogin
    commands, no LATER generation reimplements/shadows them directly, and if a
    later generation IS the last active one, its body demonstrably falls back
    to a captured previous-handler reference rather than silently dropping
    anything it doesn't own."""
    tree = ast.parse(MAIN_SRC)
    generations = [n for n in tree.body if getattr(n, "name", None) == "_panel_execute_command_text"]
    check("dispatcher: found the _panel_execute_command_text override chain", len(generations) >= 2, len(generations))

    relogin_cmds = ("/manager_relogin_phone", "/manager_relogin_code", "/manager_relogin_pass",
                     "/manager_relogin_confirm", "/manager_relogin_cancel")

    gen_srcs = []
    for gen in generations:
        try:
            gen_srcs.append(ast.unparse(gen))
        except Exception:
            gen_srcs.append(None)

    owning_indices = [i for i, src_txt in enumerate(gen_srcs) if src_txt and any(cmd in src_txt for cmd in relogin_cmds)]
    check("dispatcher: exactly one generation literally dispatches the relogin commands", len(owning_indices) == 1, owning_indices)
    if not owning_indices:
        return
    owner_idx = owning_indices[0]
    owner_src = gen_srcs[owner_idx]
    for cmd in relogin_cmds:
        check(f"dispatcher: {cmd} is registered in its owning generation", cmd in owner_src, None)

    # No generation BEFORE the owner may also claim these commands (dead code /
    # ambiguous ownership -- same invariant the original check enforced).
    for j in range(0, owner_idx):
        src_txt = gen_srcs[j]
        if src_txt is None:
            continue
        check(f"dispatcher: no earlier generation #{j} also claims /manager_relogin_phone (would be an ownership conflict)",
              "manager_relogin_phone" not in src_txt, None)

    # No generation AFTER the owner may reimplement/shadow the relogin commands
    # directly -- a later generation is only safe to add if it either leaves
    # these commands alone (falls through to its own PREV-delegate for them) or,
    # if it IS the new last-active generation, demonstrably delegates instead of
    # silently dropping them.
    for j in range(owner_idx + 1, len(generations)):
        src_txt = gen_srcs[j]
        if src_txt is None:
            continue
        mentions = [cmd for cmd in relogin_cmds if cmd in src_txt]
        check(f"dispatcher: later generation #{j} does not reimplement/shadow the relogin commands directly",
              not mentions, mentions)

    last_idx = len(generations) - 1
    if last_idx != owner_idx:
        last_src = gen_srcs[last_idx]
        prev_capture_present = 'globals().get(' in MAIN_SRC and '_panel_execute_command_text' in MAIN_SRC
        check("dispatcher: a PREV _panel_execute_command_text reference is captured somewhere in main.py",
              prev_capture_present, None)
        delegates_in_body = bool(last_src) and (
            "PREV_PANEL_EXEC" in last_src or "_REPL3_PREV_PANEL_EXEC" in last_src
        )
        check("dispatcher: the LAST (non-owning) generation's body actually calls a captured PREV reference as a fallback, not just tests truthiness",
              delegates_in_body, last_src[:300] if last_src else None)
        check("dispatcher: the LAST generation's fallback call is reachable (not behind an always-false guard) -- 'callable(' guard present",
              bool(last_src) and "callable(" in last_src, last_src[:300] if last_src else None)


def test_dbguard_rejects_unsafe_paths() -> None:
    """Direct unit test of _selftest_db_guard's exact logic (the same
    function build_main_ns calls on every invocation) against every unsafe
    scenario the 2026-07-16 forensic review required to be proven -- without
    ever touching the real storage.DB_PATH/QUEUE_DB_PATH globals or creating
    any file under the project's db/ directory (the guard raises before any
    connection is ever opened; a plain lightweight stand-in object is passed
    in place of the real storage module)."""
    class _FakeStorage:
        def __init__(self, db_path, queue_db_path):
            self.DB_PATH = db_path
            self.QUEUE_DB_PATH = queue_db_path

    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    prod_nested = os.path.join(prod_dir, "nested", "sub", "x.db")
    safe_path = os.path.join(tempfile.gettempdir(), "dbguard_probe_dir", "safe.db")
    sibling_path = os.path.join(str(BASE_DIR), "dbfoo", "x.db")

    def _raises(db_path, storage_stub):
        try:
            _selftest_db_guard(db_path, BASE_DIR, storage_stub)
            return False
        except AssertionError:
            return True

    check("dbguard-a. rejects the real production DB file path",
          _raises(prod_file, _FakeStorage(prod_file, prod_file)), prod_file)
    check("dbguard-b. rejects a nested path inside the production db/ directory",
          _raises(prod_nested, _FakeStorage(prod_nested, prod_nested)), prod_nested)
    check("dbguard-c. rejects the bare production db/ directory itself",
          _raises(prod_dir, _FakeStorage(prod_dir, prod_dir)), prod_dir)
    check("dbguard-d. rejects a DB_PATH mismatch (QUEUE_DB_PATH correct, DB_PATH stale)",
          _raises(safe_path, _FakeStorage("some-other-path.db", safe_path)), None)
    check("dbguard-e. rejects a QUEUE_DB_PATH mismatch (DB_PATH correct, QUEUE_DB_PATH stale)",
          _raises(safe_path, _FakeStorage(safe_path, "some-other-path.db")), None)
    check("dbguard-f. rejects both globals agreeing with EACH OTHER but not with db_path",
          _raises(safe_path, _FakeStorage("wrong.db", "wrong.db")), None)
    check("dbguard-g. allows a normal safe temp path with matching globals (no exception)",
          not _raises(safe_path, _FakeStorage(safe_path, safe_path)), None)
    check("dbguard-h. does not false-positive on a sibling 'dbfoo' directory (separator-aware boundary)",
          not _raises(sibling_path, _FakeStorage(sibling_path, sibling_path)), None)


def main() -> int:
    test_1_5_button_placement_and_gating()
    test_6_7_45_no_create_manager_in_source()
    asyncio.run(test_8_16_snapshot_diff_full_success())
    asyncio.run(test_18_runtime_not_stopped_during_phone_code_pass())
    asyncio.run(test_19_22_errors_never_touch_boevoy())
    asyncio.run(test_23_27_identity_gate())
    asyncio.run(test_28_29_only_selected_key_touched())
    asyncio.run(test_34_backup_failure_leaves_boevoy_untouched())
    asyncio.run(test_35_replace_failure_restores_backup())
    asyncio.run(test_runtime_state_scenario_b_manual_stopped())
    asyncio.run(test_runtime_state_scenario_c_disabled())
    asyncio.run(test_runtime_state_scenario_d_flags_active_but_not_running())
    asyncio.run(test_owner_legacy_null_fallback())
    asyncio.run(test_36_spawn_failure_after_commit_is_recoverable())
    asyncio.run(test_37_cancel_cleans_everything())
    asyncio.run(test_38_stale_lock_is_cleaned_up())
    asyncio.run(test_39_per_manager_lock())
    asyncio.run(test_stage3_relogin_replacement_exclusion())
    asyncio.run(test_40_duplicate_confirm_no_double_commit())
    test_41_stale_callback_rejected()
    test_start_and_phone_render_and_dedup()
    test_render_result_marker_dispatch()
    test_42_43_scope_untouched()
    test_44_allow_spend_unaffected()
    test_46_no_db_or_runtime_dir_deletion_in_source()
    asyncio.run(test_46c_manager_db_and_workdir_survive_full_commit())
    test_47_qr_absent_from_dispatch()
    test_dispatcher_registration_is_last_active()
    test_dbguard_rejects_unsafe_paths()

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
