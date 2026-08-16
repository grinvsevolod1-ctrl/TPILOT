# -*- coding: utf-8 -*-
"""tools/qr_auth_context_selftest.py -- offline selftest for the
"TPILOT AUTH SAFETY 20260809" patch (plan Ф3, D-QR): QR login for
relogin/replacement of an EXISTING manager must open the Telethon QR client
on a TEMPORARY session path (never the live/boevoy .session), must be
fail-closed against any temp==live path collision, must hand off to the
EXISTING, already-safe commit mechanisms (_manager_relogin_commit /
_replacement_after_signin) rather than inventing a new one, and the three
QR contexts (add/relogin/replace) must never cross-trigger each other's
commit path.

Pure/offline, same technique as every other tools/*_selftest.py in this
project: main.py cannot be imported standalone (Telethon/env side effects
at import time), so the functions under test and their direct, pre-existing,
UNMODIFIED dependencies are extracted via ast.parse + ast.unparse + exec()
and run for real against a temp SQLite DB (never db/data_tpilot.db) and a
temp runtime dir (never runtime/managers/). Telethon itself is faked under
its exact production names (SessionPasswordNeededError), same as
tools/manager_replacement_backend_selftest.py.

Scope boundary (deliberate, matches the plan's "reuse existing commit
mechanisms, do not build a new one"): _manager_relogin_commit and
_replacement_after_signin are the SAFETY-CRITICAL commit/rollback engines
for relogin and replacement respectively -- both are already covered by
their own dedicated selftests elsewhere (manager_relogin_selftest.py /
manager_replacement_backend_selftest.py / manager_replacement_commit_
selftest.py) and are NOT re-verified here. Here they are counting fakes --
this file's job is ROUTING and CONTEXT ISOLATION: that the right temp path
is used, that a collision is refused before any client is opened, and that
each context calls the ONE correct existing commit function, exactly once,
and never the other one.

Covers plan section "Q1-Q11" plus the required mutation proofs:
  Q1  ADD: target session path == the manager's normal final session path
      (_manager_runtime_paths_for_key -- unchanged, proven via source read,
      not behavioral extraction: _panel_manager_qr_start_command's own
      target-path line was never touched by this patch).
  Q2  RELOGIN: target path == _manager_relogin_temp_session_path(key),
      target != live path.
  Q3  REPLACE: target path == replacement_send_qr_start's temp_session_path,
      target != live (permanent) path.
  Q4  Forced temp==live collision (relogin) -> guard refuses, NO client ever
      opened.
  Q4b Forced temp==live collision (replace) -> guard refuses, NO client ever
      opened.
  Q5  QR cancel (relogin) -> _manager_qr_session_drop only; commit fns never
      called; nothing on disk touched.
  Q6  QR timeout (replace) -> commit fns never called; operation status
      stays at auth_phone (never advanced further).
  Q7  QR successful relogin -> _manager_relogin_commit called exactly once,
      with the right (key, owner, phone) args.
  Q8  QR successful replacement -> _replacement_after_signin called exactly
      once, with the right (operation_id, client) args.
  Q9  context="add" never calls _manager_relogin_commit or
      _replacement_after_signin.
  Q10 context="relogin" never calls _replacement_after_signin.
  Q11 context="replace" never calls _manager_relogin_commit.
  MUT-1: strip the collision-guard check out of _manager_relogin_qr_start's
      own AST -> with a forced temp==live collision, the client factory
      that was never called under Q4 IS now called (proves Q4 is
      load-bearing, not a tautology).
  MUT-2: strip the `context == "relogin"` dispatch branch out of
      _manager_qr_wait_task's own AST -> a relogin-context QR success now
      falls through to the ADD path and calls _manager_finalize_login
      instead of _manager_relogin_commit (proves Q9/Q10's isolation is
      load-bearing).
  MUT-3: same collision-guard strip as MUT-1, applied to
      _manager_replace_qr_start -> proves Q4b is load-bearing too.

Never: real Telegram network, real proxy/provider network, real process
spawn/stop, production DB/runtime/session/log access.

    python tools\\qr_auth_context_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()


# ======================================================================
# Fake Telethon layer (telethon is not installed locally).
# ======================================================================

class SessionPasswordNeededError(Exception):
    pass


class PasswordHashInvalidError(Exception):
    pass


# ======================================================================
# main.py extraction surface.
# ======================================================================

# The same replacement-backend dependency set proven safe/working by
# tools/manager_replacement_backend_selftest.py's STAGE2_REAL_NAMES,
# MINUS _replacement_after_signin (faked here -- see module docstring),
# PLUS the new QR routing/context functions this patch adds.
REPL_BACKEND_NAMES = {
    "_REPLACE_STEP_PHONE", "_REPLACE_STEP_CODE", "_REPLACE_STEP_PASS", "_REPLACE_STEPS",
    "_REPLACE_TEMP_SESSION_SUFFIX", "_REPLACE_NEXT_STEP_BY_STATUS",
    "MANAGER_ONBOARD_TIMEOUT_SEC", "MANAGER_PHONE_COOLDOWN_SEC",
    "_RELOGIN_STEP_PHONE", "_RELOGIN_STEP_CODE", "_RELOGIN_STEP_PASS",
    "_RELOGIN_STEP_IDENTITY_CONFIRM", "_RELOGIN_STEP_READY_COMMIT", "_RELOGIN_STEPS",
    "_MANAGER_AUTH_AUDIT_LOG",
    "_replacement_next_step_for_status", "_replacement_result",
    "_replacement_temp_session_path", "_replacement_permanent_session_path",
    "_replacement_cleanup_temp_files",
    "_replacement_relogin_active", "replacement_active_for_old_key",
    "_replacement_onboarding_proxy_fields", "_replacement_resolve_durable_proxy",
    "replacement_start", "replacement_reserve_key", "replacement_describe_proxy",
    "_manager_auth_audit_log",
    "_resolve_manager_telethon_proxy", "_build_telethon_proxy_from_row",
    "_manager_auth_proxy_mode", "_proxy_type_norm", "_proxy_port_int", "_manager_proxy_enabled",
    "_future_iso", "_now_utc_iso", "_partner_source_key_for_manager",
    "replacement_send_qr_start",
    "_RELOGIN_COMMIT_OK_MARKER",
}

QR_NAMES = REPL_BACKEND_NAMES | {
    "_manager_qr_guard_no_collision",
    "_manager_qr_session_drop",
    "_manager_qr_temp_cleanup_for_context",
    "_manager_relogin_cleanup_temp_files",
    "_manager_runtime_paths_for_key",
    "_manager_relogin_temp_session_path",
    "_manager_relogin_qr_start",
    "_manager_replace_qr_start",
    "_manager_qr_wait_task",
    "_panel_manager_qr_cancel_command",
    "_qr_expires_iso",
    "_iso_to_local_hhmm",
    "_ONBOARDING_SOURCE_PICK_MARKER",
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


def _extract_specific_overload(src: str, name: str, unique_marker: str) -> ast.AST:
    """main.py's stacked-override convention means several functions share the
    SAME name at module scope (only the LAST one is active at runtime -- see
    CLAUDE.md section 4). _extract_by_names() grabs every same-named def and
    lets Python's own last-wins exec semantics decide, which is correct when
    the LAST one really is what's under test -- but _panel_manager_pass_
    command has 3 stacked defs, and the actual QR/2FA context-dispatch logic
    (what this section tests) lives in the MIDDLE one; the last one is an
    unrelated TPAG proxy-guard wrapper that delegates to the middle one via
    _TPAG_V2_ORIG_PASS, and pulling that wrapper's own dependency chain in
    would be scope creep unrelated to the bug this section regression-guards.
    Selects the SPECIFIC def whose unparsed source contains unique_marker,
    raising if zero or more than one match (fails loudly on drift instead of
    silently picking the wrong overload again)."""
    tree = ast.parse(src)
    matches = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == name and unique_marker in ast.unparse(n)]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one '{name}' overload containing {unique_marker!r}, found {len(matches)}")
    return matches[0]


def _selftest_db_guard(db_path: str, base_dir, storage_mod) -> None:
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


# ======================================================================
# Fake QR/Telegram client layer.
# ======================================================================

class FakeMe:
    def __init__(self, *, id=700001, username="newacc", first_name="New", last_name="Acc", phone="79990000001"):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.phone = phone


class FakeQRLogin:
    """script controls .wait()'s outcome: 'ok' (default) returns a FakeMe,
    'timeout' raises asyncio.TimeoutError, '2fa' raises
    SessionPasswordNeededError, 'error' raises a generic Exception,
    'cancel' raises asyncio.CancelledError."""

    def __init__(self, script=None):
        self.script = script or {}
        self.url = "tg://login?token=FAKE"
        self.expires = datetime.now(timezone.utc) + timedelta(minutes=2)

    async def wait(self, timeout=None):
        mode = self.script.get("wait", "ok")
        if mode == "hang":
            # Never resolves -- lets a test inspect _MANAGER_QR_SESSIONS at any
            # later point without racing the background wait-task's own
            # eventual self-removal via _manager_qr_session_drop (every other
            # mode -- success, timeout, 2fa, error, cancel -- ends by calling
            # that, popping the entry the moment the event loop gets a chance
            # to run this task to completion).
            await asyncio.Event().wait()
        if mode == "timeout":
            raise asyncio.TimeoutError()
        if mode == "2fa":
            raise SessionPasswordNeededError()
        if mode == "cancel":
            raise asyncio.CancelledError()
        if mode == "error":
            raise RuntimeError("simulated QR wait error")
        return self.script.get("me") or FakeMe()

    async def recreate(self):
        self.expires = datetime.now(timezone.utc) + timedelta(minutes=2)


class FakeQRClient:
    def __init__(self, session_path, *, script=None, calls=None, tag=""):
        self.session_path = str(session_path)
        self.script = script or {}
        self.calls = calls if calls is not None else []
        self.tag = tag
        self.disconnected = False
        self.qr = None

    async def connect(self):
        self.calls.append((f"connect:{self.tag}", self.session_path))

    async def qr_login(self):
        self.calls.append((f"qr_login:{self.tag}", self.session_path))
        self.qr = FakeQRLogin(self.script)
        return self.qr

    async def sign_in(self, *, password=None):
        self.calls.append((f"sign_in:{self.tag}", self.session_path))
        mode = self.script.get("pass_sign_in", "ok")
        if mode == "wrong":
            raise PasswordHashInvalidError()
        if mode == "error":
            raise RuntimeError("simulated 2FA sign_in error")

    async def get_me(self):
        self.calls.append((f"get_me:{self.tag}", self.session_path))
        return self.script.get("me") or FakeMe()

    async def is_user_authorized(self):
        self.calls.append((f"is_user_authorized:{self.tag}", self.session_path))
        return bool(self.script.get("authorized", True))

    async def disconnect(self):
        self.disconnected = True
        self.calls.append((f"disconnect:{self.tag}", self.session_path))


def make_relogin_client_factory(script, calls):
    def factory(row_dict, session_path, *, source=""):
        calls.append(("build_relogin_client", session_path, source))
        return FakeQRClient(session_path, script=script, calls=calls, tag="relogin")
    return factory


def make_replace_client_factory(script, calls):
    def factory(session_path, proxy_config):
        calls.append(("build_replace_client", session_path))
        return FakeQRClient(session_path, script=script, calls=calls, tag="replace")
    return factory


# ======================================================================
# Namespace builder.
# ======================================================================

def build_qr_ns(db_path: str, base_dir: Path, *, script: dict = None, exclude_names: set = None,
                 extra_ns: dict = None) -> dict:
    import manager_registry
    import storage as _storage

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    import aiosqlite as _aiosqlite

    script = script if script is not None else {}
    calls: list = []
    relogin_commit_calls: list = []
    replace_signin_calls: list = []
    finalize_login_calls: list = []
    notify_calls: list = []

    async def fake_manager_relogin_commit(key, owner_user_id, *, phone_hint=""):
        relogin_commit_calls.append((key, owner_user_id, phone_hint))
        outcome = script.get("relogin_commit_outcome", "ok")
        if outcome == "ok":
            return "✅ RELOGIN_COMMIT_OK: session заменена."
        return "⚠️ RELOGIN_COMMIT_FAILED: simulated failure."

    async def fake_replacement_after_signin(operation_id, temp_client, **kw):
        replace_signin_calls.append((operation_id, temp_client))
        outcome = script.get("replace_signin_outcome", "ok")
        if outcome == "ok":
            return {"ok": True, "message": "Личность подтверждена."}
        return {"ok": False, "message": "simulated identity failure"}

    async def fake_manager_finalize_login(owner_user_id, manager_key, phone, me):
        finalize_login_calls.append((owner_user_id, manager_key, phone, me))
        return "✅ Менеджер вошёл."

    async def fake_tpag_insert_notification(title, body, *, kind=None):
        notify_calls.append((title, body, kind))

    async def fake_manager_save_onboarding(*a, **kw):
        return None

    def fake_manager_runtime_onboarding_set(*a, **kw):
        return None

    names = set(QR_NAMES)
    if exclude_names:
        names -= set(exclude_names)
    nodes = _extract_by_names(MAIN_SRC, names)
    if not (exclude_names and "_panel_manager_pass_command" in exclude_names):
        nodes = nodes + [_extract_specific_overload(
            MAIN_SRC, "_panel_manager_pass_command", "_MANAGER_QR_SESSIONS.get(owner)",
        )]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    tmp_audit_log = Path(db_path).parent / "manager_auth_audit.log"

    ns = {
        "os": os,
        "asyncio": asyncio,
        "aiosqlite": _aiosqlite,
        "Path": Path,
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        "TZ_KYIV": ZoneInfo("Europe/Kyiv"),
        "BASE_DIR": base_dir,
        "TPILOT_DB_PATH": db_path,
        "Optional": None, "Dict": dict, "Any": object, "Tuple": tuple,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "validate_manager_key": manager_registry.validate_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "ensure_manager_dirs": manager_registry.ensure_manager_dirs,
        "manager_get": _storage.manager_get,
        "manager_add": _storage.manager_add,
        "manager_set_fields": _storage.manager_set_fields,
        "manager_save_onboarding": fake_manager_save_onboarding,
        "_manager_runtime_onboarding_set": fake_manager_runtime_onboarding_set,
        "_repl_storage": _storage,
        "_MANAGER_AUTH_AUDIT_LOG": tmp_audit_log,
        "SessionPasswordNeededError": SessionPasswordNeededError,
        "PasswordHashInvalidError": PasswordHashInvalidError,
        "_api_profile_for": lambda source: (0, "", "onboarding"),
        "_build_manager_telegram_client_from_row": make_relogin_client_factory(script, calls),
        "_replacement_build_client": make_replace_client_factory(script, calls),
        "_manager_relogin_commit": fake_manager_relogin_commit,
        "_replacement_after_signin": fake_replacement_after_signin,
        "_manager_finalize_login": fake_manager_finalize_login,
        "_tpag_insert_notification": fake_tpag_insert_notification,
        "_MANAGER_QR_SESSIONS": {},
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(module_src, f"<{MAIN_PATH}:qr>", "exec"), ns)
    ns["__calls__"] = calls
    ns["__relogin_commit_calls__"] = relogin_commit_calls
    ns["__replace_signin_calls__"] = replace_signin_calls
    ns["__finalize_login_calls__"] = finalize_login_calls
    ns["__notify_calls__"] = notify_calls
    ns["__storage__"] = _storage
    return ns


# ======================================================================
# CLEAN-6/CLEAN-7: minimal, purpose-built namespaces around the REAL
# _manager_relogin_commit / _repl4_promote_session -- proving the specific
# claim that os.replace() already physically removes the temp file on a
# successful commit/promote (no NEW cleanup code was added for the success
# path; this only verifies the existing, unmodified behavior actually holds).
# _backup_manager_session_files is FAKED here (not extracted for real) --
# its real body writes under the module-level BASE_DIR / "_manager_session_
# backups" (the REAL project tree, not parameterized by a base_dir argument
# at all), which would be unsafe to invoke for real from a test.
# ======================================================================

def build_relogin_commit_ns(db_path: str, base_dir: Path) -> dict:
    import manager_registry
    import storage as _storage

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    finalize_calls = []
    backup_dir = base_dir / "_fake_backups"

    async def fake_backup_manager_session_files(key, action):
        dest = backup_dir / key / action
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "BACKUP_INFO.txt").write_text("fake backup\n", encoding="utf-8")
        return str(dest)

    async def fake_manager_process_running(key):
        return False

    async def fake_stop_manager_process(key, *, silent=False):
        return True, "OK"

    async def fake_spawn_manager_process(key):
        return True, "OK"

    async def fake_log_manager_danger_action(action, manager_key, requested_by, **kw):
        return None

    async def fake_manager_finalize_login(owner_user_id, manager_key, phone, me, **kw):
        finalize_calls.append((owner_user_id, manager_key, phone, me, kw))
        return "✅ Менеджер активирован и запущен."

    names = {
        "_manager_relogin_commit", "_manager_relogin_cleanup_state",
        "_manager_relogin_cleanup_temp_files", "_manager_relogin_temp_session_path",
        "_manager_runtime_paths_for_key", "_manager_auth_audit_log", "_MANAGER_AUTH_AUDIT_LOG",
        "_RELOGIN_COMMIT_OK_MARKER",
    }
    nodes = _extract_by_names(MAIN_SRC, names)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    calls = []
    client_factory = make_relogin_client_factory({"pass_sign_in": "ok"}, calls)

    ns = {
        "os": os, "asyncio": asyncio, "Path": Path, "shutil": __import__("shutil"),
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "Optional": None, "Dict": dict, "Any": object,
        "BASE_DIR": base_dir, "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "manager_get": _storage.manager_get,
        "manager_add": _storage.manager_add,
        "manager_set_fields": _storage.manager_set_fields,
        "manager_delete_onboarding": _storage.manager_delete_onboarding,
        "_MANAGER_AUTH_AUDIT_LOG": base_dir / "manager_auth_audit.log",
        "_build_manager_telegram_client_from_row": client_factory,
        "_backup_manager_session_files": fake_backup_manager_session_files,
        "_manager_process_running": fake_manager_process_running,
        "_stop_manager_process": fake_stop_manager_process,
        "_spawn_manager_process": fake_spawn_manager_process,
        "_log_manager_danger_action": fake_log_manager_danger_action,
        "_manager_finalize_login": fake_manager_finalize_login,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:relogin_commit>", "exec"), ns)
    ns["__storage__"] = _storage
    ns["__client_calls__"] = calls
    ns["__finalize_calls__"] = finalize_calls
    return ns


def build_promote_ns(db_path: str, base_dir: Path, *, script: dict = None) -> dict:
    import manager_registry
    import storage as _storage

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    names = {
        "_repl4_promote_session", "_repl4_result", "_repl4_progress", "_replacement_result",
        "_replacement_permanent_session_path", "_replacement_temp_session_path",
        "_REPLACE_TEMP_SESSION_SUFFIX",
        "_replacement_resolve_durable_proxy", "_manager_auth_proxy_mode",
        "_resolve_manager_telethon_proxy", "_build_telethon_proxy_from_row",
        "_proxy_type_norm", "_proxy_port_int", "_manager_proxy_enabled",
    }
    nodes = _extract_by_names(MAIN_SRC, names)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    calls = []
    client_factory = make_replace_client_factory(script or {"pass_sign_in": "ok"}, calls)

    ns = {
        "os": os, "asyncio": asyncio, "Path": Path,
        "Optional": None, "Dict": dict, "Any": object,
        "BASE_DIR": base_dir, "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "validate_manager_key": manager_registry.validate_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "manager_get_onboarding": _storage.manager_get_onboarding,
        "_replacement_build_client": client_factory,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:promote>", "exec"), ns)
    ns["__storage__"] = _storage
    ns["__client_calls__"] = calls
    return ns


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="qr_auth_context_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def seed_manager(ns: dict, *, key="mgrqr", tg_user_id=555101, display_name="ТестМенеджер",
                        username="mgrqr_u", status="active", is_enabled=1) -> dict:
    storage_mod = ns["__storage__"]
    paths = ns["build_manager_paths"](str(ns["BASE_DIR"]), key)
    os.makedirs(paths["root"], exist_ok=True)
    await storage_mod.manager_add(
        manager_key=key, display_name=display_name, phone="+70000000001", status=status,
        session_path=paths["session_path"], db_path=paths["db_path"], workdir=paths["root"],
        log_path=paths["log_path"], is_enabled=is_enabled,
    )
    await storage_mod.manager_set_fields(key, tg_user_id=(tg_user_id or None), telegram_username=username)
    # _partner_source_key_for_manager (pulled in via REPL_BACKEND_NAMES, called
    # from replacement_start) reads this table -- create it defensively so
    # that call resolves a real value instead of silently swallowing an
    # OperationalError, matching manager_replacement_backend_selftest.py's
    # own seed_old_manager convention.
    import sqlite3 as _sqlite3
    con = _sqlite3.connect(ns["TPILOT_DB_PATH"])
    try:
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("DELETE FROM manager_source_links WHERE manager_key=?", (key,))
        con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, "src1"))
        con.commit()
    finally:
        con.close()
    return await storage_mod.manager_get(key)


async def seed_draft_replacement_op(ns: dict, *, old_key: str, created_by=999, new_key="mgrqrnew"):
    op = await ns["replacement_start"](old_key, created_by, db_path=ns["TPILOT_DB_PATH"])
    assert op.get("ok"), op
    op_id = str(op.get("operation_id"))
    reserved = await ns["replacement_reserve_key"](op_id, new_key, db_path=ns["TPILOT_DB_PATH"], base_dir=ns["BASE_DIR"])
    assert reserved.get("ok"), reserved
    return op_id


def main() -> int:
    async def run():
        # ==================================================================
        # Q1: ADD -- target path is the manager's normal final session path.
        # _panel_manager_qr_start_command was NOT behaviorally modified by
        # this patch (only a nearby comment block changed) -- proven here as
        # a static source check rather than full behavioral extraction
        # (that function pulls in the entire onboarding subsystem, out of
        # scope for a routing test).
        import inspect
        start_cmd_src = None
        tree = ast.parse(MAIN_SRC)
        for n in tree.body:
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_panel_manager_qr_start_command":
                start_cmd_src = ast.unparse(n)
                break
        check("Q1. _panel_manager_qr_start_command found", start_cmd_src is not None)
        check("Q1. ADD still opens the QR client on paths['session_path'] (the normal final "
              "session path, via _manager_runtime_paths_for_key) -- unchanged by this patch",
              start_cmd_src is not None and 'paths = _manager_runtime_paths_for_key(manager_key)' in start_cmd_src
              and '_build_manager_session_client(manager_key, paths[\'session_path\'])' in start_cmd_src)
        check("Q1. ADD path never calls the new collision guard (onboarding has no live "
              "session yet -- guard is relogin/replace-only)",
              start_cmd_src is not None and '_manager_qr_guard_no_collision' not in start_cmd_src)

        # ==================================================================
        # Q2: RELOGIN -- target == temp path, target != live path.
        # ==================================================================
        tmp_root, db_path = make_temp_env()
        ns = build_qr_ns(db_path, tmp_root)
        row2 = await seed_manager(ns, key="mgrq2", tg_user_id=555201, username="q2_u")
        live_path_2 = ns["_manager_runtime_paths_for_key"]("mgrq2")["session_path"]
        temp_path_2 = ns["_manager_relogin_temp_session_path"]("mgrq2")
        check("Q2. relogin temp path != live path (precondition)", temp_path_2 != live_path_2)
        r2 = await ns["_manager_relogin_qr_start"](12345, "mgrq2")
        check("Q2. relogin QR start succeeded", "QR_URL=" in r2, detail=r2)
        entry2 = ns["_MANAGER_QR_SESSIONS"].get(12345)
        check("Q2. session entry recorded context='relogin'", entry2 is not None and entry2.get("context") == "relogin")
        check("Q2. client was opened on the TEMP path, not the live path",
              any(c[0] == "build_relogin_client" and c[1] == temp_path_2 for c in ns["__calls__"]),
              detail=str(ns["__calls__"]))
        check("Q2. client was NEVER opened on the live path", not any(c[1] == live_path_2 for c in ns["__calls__"] if c[0] == "build_relogin_client"))

        # ==================================================================
        # Q3: REPLACE -- target == replacement_send_qr_start's temp path,
        # target != permanent (future-live) path.
        # ==================================================================
        tmp_root3, db_path3 = make_temp_env()
        ns3 = build_qr_ns(db_path3, tmp_root3)
        row3 = await seed_manager(ns3, key="mgrq3old", tg_user_id=555301, username="q3old_u")
        op_id_3 = await seed_draft_replacement_op(ns3, old_key="mgrq3old", new_key="mgrq3new")
        live_path_3 = ns3["_replacement_permanent_session_path"]("mgrq3new")
        r3 = await ns3["_manager_replace_qr_start"](22345, op_id_3, "New Display", "mgrq3new", {})
        check("Q3. replace QR start succeeded", "QR_URL=" in r3, detail=r3)
        entry3 = ns3["_MANAGER_QR_SESSIONS"].get(22345)
        check("Q3. session entry recorded context='replace'", entry3 is not None and entry3.get("context") == "replace")
        opened_path_3 = None
        for c in ns3["__calls__"]:
            if c[0] == "build_replace_client":
                opened_path_3 = c[1]
        check("Q3. client was opened on a temp path, not the permanent path",
              opened_path_3 is not None and opened_path_3 != live_path_3, detail=str(ns3["__calls__"]))
        check("Q3. temp path carries the replacement suffix (never the bare permanent session file)",
              opened_path_3 is not None and opened_path_3.endswith(".replace.session"))

        # ==================================================================
        # Q4: forced temp==live collision (relogin) -> guard refuses, client
        # NEVER opened.
        # ==================================================================
        tmp_root4, db_path4 = make_temp_env()
        ns4 = build_qr_ns(
            db_path4, tmp_root4,
            exclude_names={"_manager_relogin_temp_session_path"},
            extra_ns={"_manager_relogin_temp_session_path": lambda key: ns4_live_holder["path"]},
        )
        # ns4 must exist before the lambda captures it by closure; use a
        # holder dict populated right after seeding instead of a forward ref.
        row4 = await seed_manager(ns4, key="mgrq4", tg_user_id=555401, username="q4_u")
        ns4_live_holder = {"path": ns4["_manager_runtime_paths_for_key"]("mgrq4")["session_path"]}
        r4 = await ns4["_manager_relogin_qr_start"](32345, "mgrq4")
        check("Q4. forced collision refused with the internal-error guard message",
              "временная и боевая сессия совпадают" in r4, detail=r4)
        check("Q4. NO client was ever opened when the guard refused", len(ns4["__calls__"]) == 0, detail=str(ns4["__calls__"]))
        check("Q4. no session entry was stored", 32345 not in ns4["_MANAGER_QR_SESSIONS"])

        # ==================================================================
        # Q4b: forced temp==live collision (replace) -> guard refuses,
        # client NEVER opened.
        # ==================================================================
        tmp_root4b, db_path4b = make_temp_env()

        async def fake_replacement_send_qr_start_collide(operation_id, new_display_name, new_manager_key,
                                                           proxy_config, *, proxy_source=""):
            perm = ns4b["_replacement_permanent_session_path"](new_manager_key)
            return {"ok": True, "new_manager_key": new_manager_key, "temp_session_path": perm}

        ns4b = build_qr_ns(
            db_path4b, tmp_root4b,
            exclude_names={"replacement_send_qr_start"},
            extra_ns={"replacement_send_qr_start": fake_replacement_send_qr_start_collide},
        )
        row4b = await seed_manager(ns4b, key="mgrq4bold", tg_user_id=555451, username="q4bold_u")
        op_id_4b = await seed_draft_replacement_op(ns4b, old_key="mgrq4bold", new_key="mgrq4bnew")
        r4b = await ns4b["_manager_replace_qr_start"](32355, op_id_4b, "New", "mgrq4bnew", {})
        check("Q4b. forced collision (replace) refused with the internal-error guard message",
              "временная и боевая сессия совпадают" in r4b, detail=r4b)
        check("Q4b. NO client was ever opened when the guard refused", len(ns4b["__calls__"]) == 0, detail=str(ns4b["__calls__"]))
        check("Q4b. no session entry was stored", 32355 not in ns4b["_MANAGER_QR_SESSIONS"])

        # ==================================================================
        # Q5: QR cancel (relogin) -> session drop only, commit fns never
        # called, nothing on disk touched.
        # ==================================================================
        tmp_root5, db_path5 = make_temp_env()
        ns5 = build_qr_ns(db_path5, tmp_root5)
        await seed_manager(ns5, key="mgrq5", tg_user_id=555501, username="q5_u")
        live_path_5 = ns5["_manager_runtime_paths_for_key"]("mgrq5")["session_path"]
        os.makedirs(os.path.dirname(live_path_5), exist_ok=True)
        with open(live_path_5, "wb") as fh:
            fh.write(b"LIVE-Q5-BYTES")
        await ns5["_manager_relogin_qr_start"](42345, "mgrq5")
        entry5 = ns5["_MANAGER_QR_SESSIONS"].get(42345)
        client5 = entry5.get("client")
        # Simulate the admin pressing "Cancel": the real cancel command calls
        # _manager_qr_session_drop directly (same primitive used by every
        # other cancel path in this file already).
        await ns5["_manager_qr_session_drop"](42345)
        check("Q5. cancel: entry removed from _MANAGER_QR_SESSIONS", 42345 not in ns5["_MANAGER_QR_SESSIONS"])
        check("Q5. cancel: temp client was disconnected", client5.disconnected is True)
        check("Q5. cancel: relogin commit was NEVER called", len(ns5["__relogin_commit_calls__"]) == 0)
        with open(live_path_5, "rb") as fh:
            after5 = fh.read()
        check("Q5. cancel: live session bytes UNCHANGED", after5 == b"LIVE-Q5-BYTES", detail=str(after5))

        # ==================================================================
        # Q6: QR timeout (replace) -> commit fns never called, operation
        # status stays at auth_phone (never advanced further).
        # ==================================================================
        tmp_root6, db_path6 = make_temp_env()
        ns6 = build_qr_ns(db_path6, tmp_root6, script={"wait": "timeout"})
        await seed_manager(ns6, key="mgrq6old", tg_user_id=555601, username="q6old_u")
        op_id_6 = await seed_draft_replacement_op(ns6, old_key="mgrq6old", new_key="mgrq6new")
        await ns6["_manager_replace_qr_start"](52345, op_id_6, "New", "mgrq6new", {})
        entry6 = ns6["_MANAGER_QR_SESSIONS"].get(52345)
        task6 = entry6["task"]
        await task6
        check("Q6. timeout: replacement_after_signin was NEVER called", len(ns6["__replace_signin_calls__"]) == 0)
        check("Q6. timeout: entry removed from _MANAGER_QR_SESSIONS", 52345 not in ns6["_MANAGER_QR_SESSIONS"])
        op_row_6 = ns6["_repl_storage"].replacement_get(op_id_6, db_path=db_path6)
        check("Q6. timeout: operation status still 'auth_phone' (never advanced to identity_ok/committed)",
              str(op_row_6.get("status")) == "auth_phone", detail=str(op_row_6))

        # ==================================================================
        # Q7: successful relogin -> _manager_relogin_commit called EXACTLY
        # once, with (key, owner, phone_hint=phone).
        # ==================================================================
        tmp_root7, db_path7 = make_temp_env()
        me7 = FakeMe(id=700701, username="q7_real", phone="380971112233")
        ns7 = build_qr_ns(db_path7, tmp_root7, script={"wait": "ok", "me": me7})
        await seed_manager(ns7, key="mgrq7", tg_user_id=555701, username="q7_u")
        await ns7["_manager_relogin_qr_start"](62345, "mgrq7")
        entry7 = ns7["_MANAGER_QR_SESSIONS"].get(62345)
        await entry7["task"]
        check("Q7. relogin commit called EXACTLY once", len(ns7["__relogin_commit_calls__"]) == 1,
              detail=str(ns7["__relogin_commit_calls__"]))
        if ns7["__relogin_commit_calls__"]:
            k7, owner7, phone7 = ns7["__relogin_commit_calls__"][0]
            check("Q7. relogin commit called with the correct manager_key", k7 == "mgrq7", detail=k7)
            check("Q7. relogin commit called with the correct owner_user_id", owner7 == 62345, detail=owner7)
            check("Q7. relogin commit called with the phone from the QR-authorized session",
                  phone7 == "380971112233", detail=phone7)
        check("Q7. replacement_after_signin was NEVER called on a relogin success", len(ns7["__replace_signin_calls__"]) == 0)
        check("Q7. _manager_finalize_login (ADD path) was NEVER called on a relogin success", len(ns7["__finalize_login_calls__"]) == 0)

        # ==================================================================
        # Q8: successful replacement -> _replacement_after_signin called
        # EXACTLY once, with (operation_id, client).
        # ==================================================================
        tmp_root8, db_path8 = make_temp_env()
        ns8 = build_qr_ns(db_path8, tmp_root8, script={"wait": "ok"})
        await seed_manager(ns8, key="mgrq8old", tg_user_id=555801, username="q8old_u")
        op_id_8 = await seed_draft_replacement_op(ns8, old_key="mgrq8old", new_key="mgrq8new")
        await ns8["_manager_replace_qr_start"](72345, op_id_8, "New", "mgrq8new", {})
        entry8 = ns8["_MANAGER_QR_SESSIONS"].get(72345)
        await entry8["task"]
        check("Q8. replacement_after_signin called EXACTLY once", len(ns8["__replace_signin_calls__"]) == 1,
              detail=str(ns8["__replace_signin_calls__"]))
        if ns8["__replace_signin_calls__"]:
            op_arg_8, client_arg_8 = ns8["__replace_signin_calls__"][0]
            check("Q8. replacement_after_signin called with the correct operation_id", op_arg_8 == op_id_8, detail=op_arg_8)
            check("Q8. replacement_after_signin called with the SAME client object that was opened",
                  isinstance(client_arg_8, FakeQRClient) and client_arg_8.tag == "replace")
        check("Q8. relogin_commit was NEVER called on a replace success", len(ns8["__relogin_commit_calls__"]) == 0)
        check("Q8. _manager_finalize_login (ADD path) was NEVER called on a replace success", len(ns8["__finalize_login_calls__"]) == 0)

        # ==================================================================
        # Q9/Q11: context="add" behavior -- exercised via the real, unmodified
        # add branch of _manager_qr_wait_task directly (no live onboarding
        # subsystem needed: an "add" entry with no "context" key at all is
        # the documented default/back-compat path).
        # ==================================================================
        tmp_root9, db_path9 = make_temp_env()
        me9 = FakeMe(id=700901, username="q9_real")
        ns9 = build_qr_ns(db_path9, tmp_root9, script={"wait": "ok", "me": me9})
        await seed_manager(ns9, key="mgrq9", tg_user_id=555901, username="q9_u")
        client9 = FakeQRClient(ns9["_manager_runtime_paths_for_key"]("mgrq9")["session_path"],
                                script={"wait": "ok", "me": me9}, tag="add")
        await client9.connect()
        qr9 = await client9.qr_login()
        ns9["_MANAGER_QR_SESSIONS"][82345] = {
            "manager_key": "mgrq9", "client": client9, "qr_login": qr9, "task": None,
            "expires_at_dt": qr9.expires, "needs_2fa": False, "session_path": client9.session_path,
            "phone": "", "phone_code_hash": "",
            # no "context" key -- exercises the documented add/back-compat default.
        }
        await ns9["_manager_qr_wait_task"](82345, "mgrq9")
        check("Q9. context='add' (default) called _manager_finalize_login exactly once", len(ns9["__finalize_login_calls__"]) == 1)
        check("Q9. context='add' NEVER called _manager_relogin_commit", len(ns9["__relogin_commit_calls__"]) == 0)
        check("Q11. context='add' NEVER called _replacement_after_signin", len(ns9["__replace_signin_calls__"]) == 0)

        # Q10 was already proven positively by Q7 above (relogin calls only
        # relogin_commit) -- add the negative direction explicitly here too
        # for a self-contained assertion list.
        check("Q10. context='relogin' (Q7 scenario) NEVER called _replacement_after_signin",
              len(ns7["__replace_signin_calls__"]) == 0)
        check("Q11. context='replace' (Q8 scenario) NEVER called _manager_relogin_commit",
              len(ns8["__relogin_commit_calls__"]) == 0)

        # ==================================================================
        # MUT-1: strip the collision-guard check out of
        # _manager_relogin_qr_start -> the SAME forced collision that Q4
        # proved refuses the client now goes through and OPENS one.
        # ==================================================================
        class _GuardStripper(ast.NodeTransformer):
            def __init__(self):
                self.hit = False

            def visit_If(self, node):
                self.generic_visit(node)
                if not self.hit and "ok_guard" in ast.unparse(node.test):
                    self.hit = True
                    return None  # delete the whole `if not ok_guard: ... return guard_err` block
                return node

        tree_mut1 = ast.parse(MAIN_SRC)
        target_fn = None
        for n in tree_mut1.body:
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_manager_relogin_qr_start":
                target_fn = n
                break
        assert target_fn is not None
        stripper = _GuardStripper()
        mutant_fn = stripper.visit(target_fn)
        assert stripper.hit, "MUT-1 anchor (ok_guard) not found"
        ast.fix_missing_locations(mutant_fn)

        tmp_rootm1, db_pathm1 = make_temp_env()
        nsm1 = build_qr_ns(
            db_pathm1, tmp_rootm1,
            exclude_names={"_manager_relogin_temp_session_path", "_manager_relogin_qr_start"},
            extra_ns={"_manager_relogin_temp_session_path": lambda key: m1_live_holder["path"]},
        )
        # Inject the mutant function (guard stripped) into the SAME namespace
        # its real dependencies were already exec'd into.
        exec(compile(ast.Module(body=[mutant_fn], type_ignores=[]), f"<{MAIN_PATH}:qr-mutant>", "exec"), nsm1)
        await seed_manager(nsm1, key="mgrm1", tg_user_id=556001, username="m1_u")
        m1_live_holder = {"path": nsm1["_manager_runtime_paths_for_key"]("mgrm1")["session_path"]}
        r_m1 = await nsm1["_manager_relogin_qr_start"](92345, "mgrm1")
        check("MUT-1. with the collision guard stripped, the SAME forced collision that Q4 refused "
              "now OPENS a client on the live path (proves Q4's guard is load-bearing, not a "
              "tautology)",
              "QR_URL=" in r_m1 and any(
                  c[0] == "build_relogin_client" and c[1] == m1_live_holder["path"] for c in nsm1["__calls__"]
              ),
              detail=(r_m1, nsm1["__calls__"]))

        # ==================================================================
        # MUT-2: strip the `context == "relogin"` dispatch branch out of
        # _manager_qr_wait_task -> a relogin-context success now falls
        # through to the ADD path (_manager_finalize_login) instead of
        # _manager_relogin_commit.
        # ==================================================================
        class _DispatchBranchStripper(ast.NodeTransformer):
            def __init__(self):
                self.hit = False

            def visit_If(self, node):
                self.generic_visit(node)
                if not self.hit and ast.unparse(node.test) == ast.unparse(ast.parse('context == "relogin"', mode="eval").body):
                    self.hit = True
                    return None
                return node

        tree_mut2 = ast.parse(MAIN_SRC)
        target_fn2 = None
        for n in tree_mut2.body:
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_manager_qr_wait_task":
                target_fn2 = n
                break
        assert target_fn2 is not None
        stripper2 = _DispatchBranchStripper()
        mutant_fn2 = stripper2.visit(target_fn2)
        assert stripper2.hit, "MUT-2 anchor (context == 'relogin') not found"
        ast.fix_missing_locations(mutant_fn2)

        tmp_rootm2, db_pathm2 = make_temp_env()
        me_m2 = FakeMe(id=700111, username="mut2_real")
        nsm2 = build_qr_ns(
            db_pathm2, tmp_rootm2, script={"wait": "ok", "me": me_m2},
            exclude_names={"_manager_qr_wait_task"},
        )
        exec(compile(ast.Module(body=[mutant_fn2], type_ignores=[]), f"<{MAIN_PATH}:qr-mutant2>", "exec"), nsm2)
        await seed_manager(nsm2, key="mgrm2", tg_user_id=556101, username="m2_u")
        await nsm2["_manager_relogin_qr_start"](102345, "mgrm2")
        entrym2 = nsm2["_MANAGER_QR_SESSIONS"].get(102345)
        await entrym2["task"]
        check("MUT-2. with the relogin dispatch branch stripped, a relogin-context QR success "
              "WRONGLY calls _manager_finalize_login (the ADD path) instead of "
              "_manager_relogin_commit (proves Q9/Q10's context isolation is load-bearing)",
              len(nsm2["__finalize_login_calls__"]) == 1 and len(nsm2["__relogin_commit_calls__"]) == 0,
              detail=(nsm2["__finalize_login_calls__"], nsm2["__relogin_commit_calls__"]))

        # ==================================================================
        # MUT-3: same collision-guard strip as MUT-1, applied to
        # _manager_replace_qr_start -> proves Q4b is load-bearing too.
        # ==================================================================
        tree_mut3 = ast.parse(MAIN_SRC)
        target_fn3 = None
        for n in tree_mut3.body:
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_manager_replace_qr_start":
                target_fn3 = n
                break
        assert target_fn3 is not None
        stripper3 = _GuardStripper()
        mutant_fn3 = stripper3.visit(target_fn3)
        assert stripper3.hit, "MUT-3 anchor (ok_guard) not found"
        ast.fix_missing_locations(mutant_fn3)

        tmp_rootm3, db_pathm3 = make_temp_env()

        async def fake_replacement_send_qr_start_collide_m3(operation_id, new_display_name, new_manager_key,
                                                              proxy_config, *, proxy_source=""):
            perm = nsm3["_replacement_permanent_session_path"](new_manager_key)
            return {"ok": True, "new_manager_key": new_manager_key, "temp_session_path": perm}

        nsm3 = build_qr_ns(
            db_pathm3, tmp_rootm3,
            exclude_names={"replacement_send_qr_start", "_manager_replace_qr_start"},
            extra_ns={"replacement_send_qr_start": fake_replacement_send_qr_start_collide_m3},
        )
        exec(compile(ast.Module(body=[mutant_fn3], type_ignores=[]), f"<{MAIN_PATH}:qr-mutant3>", "exec"), nsm3)
        await seed_manager(nsm3, key="mgrm3old", tg_user_id=556201, username="m3old_u")
        op_id_m3 = await seed_draft_replacement_op(nsm3, old_key="mgrm3old", new_key="mgrm3new")
        r_m3 = await nsm3["_manager_replace_qr_start"](112345, op_id_m3, "New", "mgrm3new", {})
        check("MUT-3. with the collision guard stripped, the SAME forced collision that Q4b "
              "refused now OPENS a client on the permanent path (proves Q4b's guard is "
              "load-bearing for the replace context too)",
              "QR_URL=" in r_m3 and any(c[0] == "build_replace_client" for c in nsm3["__calls__"]),
              detail=(r_m3, nsm3["__calls__"]))

        # ==================================================================
        # STORAGE-SAFETY: independent proof for the storage.py transition
        # edge this patch added (_REPLACEMENT_TRANSITIONS["auth_phone"] +=
        # "identity_ok"). Unlike every test above (which fakes
        # _replacement_after_signin at the routing boundary), THIS section
        # extracts the REAL _replacement_after_signin and its real identity-
        # conflict-guard dependencies, to directly prove:
        #   A. replacement_send_qr_start genuinely lands the operation in
        #      'auth_phone' via the SAME draft->auth_phone CAS the phone flow
        #      uses (not an artificially chosen status).
        #   B/C. the auth_phone->identity_ok transition is reachable ONLY
        #      through _replacement_after_signin, which unconditionally
        #      requires a LIVE, Telegram-confirmed is_user_authorized()==True
        #      + a real get_me() result BEFORE it ever calls
        #      replacement_advance(..., "identity_ok", ...) -- proven by
        #      calling it with an UNAUTHORIZED client and asserting the
        #      operation status is NOT advanced.
        #   D. a successful, authorized call DOES advance to identity_ok and
        #      persists the real Telegram identity.
        # Honest scope note (not glossed over): storage.replacement_advance()
        # itself is a pure CAS with no identity check of its own -- it trusts
        # its caller. The actual safety invariant is structural: grep-proven
        # (main.py has exactly ONE call site that ever targets "identity_ok",
        # inside _replacement_after_signin) plus this behavioral proof that
        # that one call site is unconditionally gated on live authorization.
        # ==================================================================
        import inspect as _inspect

        identity_to_calls = [ln for ln in MAIN_SRC.splitlines() if '"identity_ok"' in ln and "replacement_advance" not in ln]
        advance_to_identity_ok_sites = 0
        _tree_scan = ast.parse(MAIN_SRC)
        for _n in ast.walk(_tree_scan):
            if isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute) and _n.func.attr == "replacement_advance":
                try:
                    args_src = [ast.unparse(a) for a in _n.args]
                except Exception:
                    args_src = []
                if len(args_src) >= 3 and args_src[2] == "'identity_ok'":
                    advance_to_identity_ok_sites += 1
        # TPILOT AUTH SAFETY 20260809 (Ф4): a SECOND legitimate caller was
        # added -- replacement_confirm_tdimport_install (Session/TData
        # entry method) -- alongside the original _replacement_after_signin
        # (QR/phone). Both are independently audited: the QR/phone one via
        # this file's own STORAGE-B/C proofs above, the new one via
        # tools/replacement_tdimport_selftest.py's Q1-Q5 (same identity-
        # conflict guards, same fail-closed-without-live-authorization
        # property, live session never touched). The invariant this check
        # protects -- "no UNAUDITED call path reaches identity_ok" -- still
        # holds; only the expected COUNT of audited paths changed from 1 to
        # 2, verified by name below so a THIRD, unaudited site would still
        # be caught.
        check("STORAGE-A. exactly TWO call sites in main.py ever target replacement_advance(..., "
              "'identity_ok', ...) -- both audited (QR/phone's _replacement_after_signin here, "
              "Session/TData's replacement_confirm_tdimport_install in tools/replacement_tdimport_"
              "selftest.py); no THIRD, unaudited call path exists",
              advance_to_identity_ok_sites == 2, detail=advance_to_identity_ok_sites)
        _tree_scan2 = ast.parse(MAIN_SRC)
        _identity_ok_callers = set()
        for _n in ast.walk(_tree_scan2):
            if isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for _inner in ast.walk(_n):
                    if (isinstance(_inner, ast.Call) and isinstance(_inner.func, ast.Attribute)
                            and _inner.func.attr == "replacement_advance"):
                        try:
                            _args_src = [ast.unparse(a) for a in _inner.args]
                        except Exception:
                            _args_src = []
                        if len(_args_src) >= 3 and _args_src[2] == "'identity_ok'":
                            _identity_ok_callers.add(_n.name)
        check("STORAGE-A2. the two call sites are EXACTLY the two audited functions by name "
              "(_replacement_after_signin, replacement_confirm_tdimport_install) -- not some other, "
              "unaudited function",
              _identity_ok_callers == {"_replacement_after_signin", "replacement_confirm_tdimport_install"},
              detail=_identity_ok_callers)

        SIGNIN_NAMES = REPL_BACKEND_NAMES | {
            "_replacement_after_signin", "_replacement_tg_user_conflict",
            "_replacement_username_conflict", "_replacement_tgid_reserved_by_other",
            "_replacement_fail",
        }

        class FakeSigninClient:
            def __init__(self, *, authorized, me=None):
                self._authorized = authorized
                self._me = me
                self.disconnected = False

            async def is_user_authorized(self):
                return self._authorized

            async def get_me(self):
                return self._me

            async def disconnect(self):
                self.disconnected = True

        def build_signin_ns(db_path, base_dir):
            import manager_registry as _mr
            import storage as _storage
            import aiosqlite as _aiosqlite2

            _storage.DB_PATH = db_path
            _storage.QUEUE_DB_PATH = db_path
            _selftest_db_guard(db_path, BASE_DIR, _storage)
            nodes = _extract_by_names(MAIN_SRC, SIGNIN_NAMES)
            module_src = "\n\n".join(ast.unparse(n) for n in nodes)
            ns = {
                "os": os, "asyncio": asyncio, "aiosqlite": _aiosqlite2, "Path": Path,
                "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
                "Optional": None, "Dict": dict, "Any": object,
                "BASE_DIR": base_dir, "TPILOT_DB_PATH": db_path,
                "registry_normalize_manager_key": _mr.normalize_manager_key,
                "validate_manager_key": _mr.validate_manager_key,
                "build_manager_paths": _mr.build_manager_paths,
                "ensure_manager_dirs": _mr.ensure_manager_dirs,
                "manager_get": _storage.manager_get,
                "manager_add": _storage.manager_add,
                "manager_set_fields": _storage.manager_set_fields,
                "manager_delete_onboarding": _storage.manager_delete_onboarding,
                "_repl_storage": _storage,
                "_MANAGER_AUTH_AUDIT_LOG": Path(db_path).parent / "manager_auth_audit.log",
            }
            exec(compile(module_src, f"<{MAIN_PATH}:signin>", "exec"), ns)
            ns["__storage__"] = _storage
            return ns

        tmp_rootS, db_pathS = make_temp_env()
        nsS = build_signin_ns(db_pathS, tmp_rootS)
        await seed_manager(nsS, key="mgrsold", tg_user_id=557001, username="sold_u")
        op_id_S = await seed_draft_replacement_op(nsS, old_key="mgrsold", new_key="mgrsnew")
        reserved_S = await nsS["replacement_send_qr_start"](op_id_S, "New QR Display", "mgrsnew", {})
        check("STORAGE-A2. replacement_send_qr_start lands the operation in 'auth_phone' via the "
              "durable CAS (same status the phone flow's send_phone_code also produces)",
              reserved_S.get("ok") is True, detail=reserved_S)
        op_row_S = nsS["_repl_storage"].replacement_get(op_id_S, db_path=db_pathS)
        check("STORAGE-A3. operation status in DB is genuinely 'auth_phone' after QR reserve "
              "(not an artificially-chosen status)", str(op_row_S.get("status")) == "auth_phone", detail=op_row_S)

        # STORAGE-B: unauthorized client -> transition MUST be refused, status
        # MUST stay at 'auth_phone'.
        unauth_client = FakeSigninClient(authorized=False)
        result_B = await nsS["_replacement_after_signin"](op_id_S, unauth_client, db_path=db_pathS)
        check("STORAGE-B1. _replacement_after_signin refuses an UNAUTHORIZED client (ok=False)",
              result_B.get("ok") is False, detail=result_B)
        op_row_S2 = nsS["_repl_storage"].replacement_get(op_id_S, db_path=db_pathS)
        check("STORAGE-B2. after the refusal, operation status is STILL 'auth_phone' -- "
              "auth_phone->identity_ok was NOT reached without a confirmed live authorization "
              "(the exact mutation/regression proof required: no bypass of live QR-auth "
              "confirmation)",
              str(op_row_S2.get("status")) == "auth_phone", detail=op_row_S2)
        check("STORAGE-B3. the unauthorized client was still disconnected (no leaked connection)",
              unauth_client.disconnected is True)

        # STORAGE-C: authorized client with a real identity -> transition DOES
        # succeed and persists the real Telegram identity.
        me_S = FakeMe(id=558001, username="s_real_new", first_name="Real", last_name="New")
        auth_client = FakeSigninClient(authorized=True, me=me_S)
        result_C = await nsS["_replacement_after_signin"](op_id_S, auth_client, db_path=db_pathS)
        check("STORAGE-C1. _replacement_after_signin ACCEPTS a genuinely authorized client (ok=True)",
              result_C.get("ok") is True, detail=result_C)
        op_row_S3 = nsS["_repl_storage"].replacement_get(op_id_S, db_path=db_pathS)
        check("STORAGE-C2. operation status advanced to 'identity_ok' (the audited edge fired "
              "exactly when live authorization was genuinely confirmed)",
              str(op_row_S3.get("status")) == "identity_ok", detail=op_row_S3)
        check("STORAGE-C3. the real Telegram tg_user_id was persisted", int(op_row_S3.get("new_tg_user_id") or 0) == 558001,
              detail=op_row_S3)
        check("STORAGE-C4. the real Telegram username was persisted", str(op_row_S3.get("new_username") or "") == "s_real_new",
              detail=op_row_S3)

        # STORAGE-D (idempotent-retry guard, R4): a SECOND call on the now-
        # identity_ok operation must short-circuit (no second CAS attempt,
        # no crash) -- proves the auth_phone-sourced path converges onto the
        # SAME idempotency guard the phone flow already relies on.
        result_D = await nsS["_replacement_after_signin"](op_id_S, FakeSigninClient(authorized=True, me=me_S), db_path=db_pathS)
        check("STORAGE-D. a second call after identity_ok short-circuits cleanly (idempotent, "
              "no duplicate CAS attempt)", result_D.get("ok") is True and result_D.get("code") == "identity_verified",
              detail=result_D)

        # ==================================================================
        # 2FA-CONTEXT: regression coverage for a real bug found during the F3
        # independent checkpoint review (2026-08-09) -- _panel_manager_pass_
        # command (the actual /manager_pass 2FA-password handler) reads
        # _MANAGER_QR_SESSIONS[owner]['needs_2fa'] but, before this fix,
        # NEVER read 'context' -- it unconditionally called
        # _manager_finalize_login (the ADD path) once the 2FA password was
        # accepted, regardless of whether the QR session was opened for
        # relogin/replace. That would have mis-finalized an EXISTING
        # (relogin) or reserved-but-uncommitted (replace) manager_key,
        # bypassing _manager_relogin_commit/_replacement_after_signin
        # entirely -- exactly the kind of split-brain state (new identity in
        # the DB, old/wrong session file on disk) the plan's rollback-
        # consistency requirement forbids. Fixed by mirroring
        # _manager_qr_wait_task's own context dispatch inside
        # _panel_manager_pass_command, immediately after a successful
        # sign_in(password=...).
        # ==================================================================
        tmp_root2fa, db_path2fa = make_temp_env()

        # 2FA-ADD: context missing/"add" (default/back-compat) -- 2FA success
        # still finalizes via _manager_finalize_login, unchanged.
        me_2fa_add = FakeMe(id=700201, username="tfa_add_real")
        ns2fa_add = build_qr_ns(db_path2fa, tmp_root2fa, script={"pass_sign_in": "ok", "me": me_2fa_add})
        await seed_manager(ns2fa_add, key="mgr2faadd", tg_user_id=559001, username="tfaadd_u")
        client_2fa_add = FakeQRClient("dummy", script={"pass_sign_in": "ok", "me": me_2fa_add}, tag="2fa")
        ns2fa_add["_MANAGER_QR_SESSIONS"][92001] = {
            "manager_key": "mgr2faadd", "client": client_2fa_add, "needs_2fa": True,
            "phone": "", "qr_login": None, "task": None, "expires_at_dt": None,
            # no "context" key -- exercises the documented add/back-compat default.
        }
        r_2fa_add = await ns2fa_add["_panel_manager_pass_command"]("Password1", requested_by=92001)
        check("2FA-ADD. context='add' (default) 2FA success calls _manager_finalize_login exactly once",
              len(ns2fa_add["__finalize_login_calls__"]) == 1, detail=r_2fa_add)
        check("2FA-ADD. context='add' 2FA success NEVER calls _manager_relogin_commit",
              len(ns2fa_add["__relogin_commit_calls__"]) == 0)
        check("2FA-ADD. context='add' 2FA success NEVER calls _replacement_after_signin",
              len(ns2fa_add["__replace_signin_calls__"]) == 0)

        # 2FA-RELOGIN: THE bug scenario -- must call _manager_relogin_commit,
        # never _manager_finalize_login.
        tmp_root2fb, db_path2fb = make_temp_env()
        ns2fa_relogin = build_qr_ns(db_path2fb, tmp_root2fb, script={"pass_sign_in": "ok"})
        await seed_manager(ns2fa_relogin, key="mgr2farel", tg_user_id=559101, username="tfarel_u")
        client_2fa_rel = FakeQRClient("dummy", script={"pass_sign_in": "ok"}, tag="2fa")
        ns2fa_relogin["_MANAGER_QR_SESSIONS"][92002] = {
            "manager_key": "mgr2farel", "client": client_2fa_rel, "needs_2fa": True,
            "context": "relogin", "phone": "", "qr_login": None, "task": None, "expires_at_dt": None,
        }
        r_2fa_rel = await ns2fa_relogin["_panel_manager_pass_command"]("Password1", requested_by=92002)
        check("2FA-RELOGIN. context='relogin' 2FA success calls _manager_relogin_commit exactly once "
              "(THE bug this section regression-guards: before the fix this called "
              "_manager_finalize_login instead)",
              len(ns2fa_relogin["__relogin_commit_calls__"]) == 1, detail=r_2fa_rel)
        check("2FA-RELOGIN. context='relogin' 2FA success NEVER calls _manager_finalize_login (the ADD path)",
              len(ns2fa_relogin["__finalize_login_calls__"]) == 0)
        check("2FA-RELOGIN. context='relogin' 2FA success NEVER calls _replacement_after_signin",
              len(ns2fa_relogin["__replace_signin_calls__"]) == 0)
        if ns2fa_relogin["__relogin_commit_calls__"]:
            k_rel2fa, owner_rel2fa, _phone_rel2fa = ns2fa_relogin["__relogin_commit_calls__"][0]
            check("2FA-RELOGIN. _manager_relogin_commit called with the correct manager_key",
                  k_rel2fa == "mgr2farel", detail=k_rel2fa)
            check("2FA-RELOGIN. _manager_relogin_commit called with the correct owner_user_id",
                  owner_rel2fa == 92002, detail=owner_rel2fa)

        # 2FA-REPLACE: must call _replacement_after_signin, never
        # _manager_finalize_login.
        tmp_root2fc, db_path2fc = make_temp_env()
        ns2fa_replace = build_qr_ns(db_path2fc, tmp_root2fc, script={"pass_sign_in": "ok"})
        await seed_manager(ns2fa_replace, key="mgr2fareplold", tg_user_id=559201, username="tfarepl_u")
        op_id_2fa = await seed_draft_replacement_op(ns2fa_replace, old_key="mgr2fareplold", new_key="mgr2farepl")
        client_2fa_repl = FakeQRClient("dummy", script={"pass_sign_in": "ok"}, tag="2fa")
        ns2fa_replace["_MANAGER_QR_SESSIONS"][92003] = {
            "manager_key": "mgr2farepl", "client": client_2fa_repl, "needs_2fa": True,
            "context": "replace", "operation_id": op_id_2fa, "phone": "", "qr_login": None,
            "task": None, "expires_at_dt": None,
        }
        r_2fa_repl = await ns2fa_replace["_panel_manager_pass_command"]("Password1", requested_by=92003)
        check("2FA-REPLACE. context='replace' 2FA success calls _replacement_after_signin exactly once "
              "(same bug class: before the fix this called _manager_finalize_login instead)",
              len(ns2fa_replace["__replace_signin_calls__"]) == 1, detail=r_2fa_repl)
        check("2FA-REPLACE. context='replace' 2FA success NEVER calls _manager_finalize_login (the ADD path)",
              len(ns2fa_replace["__finalize_login_calls__"]) == 0)
        check("2FA-REPLACE. context='replace' 2FA success NEVER calls _manager_relogin_commit",
              len(ns2fa_replace["__relogin_commit_calls__"]) == 0)
        if ns2fa_replace["__replace_signin_calls__"]:
            op_arg_2fa, client_arg_2fa = ns2fa_replace["__replace_signin_calls__"][0]
            check("2FA-REPLACE. _replacement_after_signin called with the correct operation_id",
                  op_arg_2fa == op_id_2fa, detail=op_arg_2fa)

        # 2FA-WRONG-PASSWORD: wrong password -> no commit function called for
        # ANY context, entry NOT dropped (retry must remain possible).
        tmp_root2fd, db_path2fd = make_temp_env()
        ns2fa_wrong = build_qr_ns(db_path2fd, tmp_root2fd, script={"pass_sign_in": "wrong"})
        await seed_manager(ns2fa_wrong, key="mgr2fawrong", tg_user_id=559301, username="tfawrong_u")
        client_2fa_wrong = FakeQRClient("dummy", script={"pass_sign_in": "wrong"}, tag="2fa")
        ns2fa_wrong["_MANAGER_QR_SESSIONS"][92004] = {
            "manager_key": "mgr2fawrong", "client": client_2fa_wrong, "needs_2fa": True,
            "context": "relogin", "phone": "", "qr_login": None, "task": None, "expires_at_dt": None,
        }
        r_2fa_wrong = await ns2fa_wrong["_panel_manager_pass_command"]("WrongPass", requested_by=92004)
        check("2FA-WRONG-PASSWORD. wrong 2FA password refused, no commit function called",
              len(ns2fa_wrong["__relogin_commit_calls__"]) == 0 and len(ns2fa_wrong["__finalize_login_calls__"]) == 0,
              detail=r_2fa_wrong)
        check("2FA-WRONG-PASSWORD. QR session entry NOT dropped (admin can retry the password)",
              92004 in ns2fa_wrong["_MANAGER_QR_SESSIONS"])

        # MUT-4: strip the context dispatch out of _panel_manager_pass_command
        # -> a relogin-context 2FA success WRONGLY falls through to
        # _manager_finalize_login again, reproducing the exact bug this
        # section regression-guards.
        class _PassCommandDispatchStripper(ast.NodeTransformer):
            def __init__(self):
                self.hit = 0

            def visit_If(self, node):
                self.generic_visit(node)
                src = ast.unparse(node.test)
                anchors = (ast.unparse(ast.parse('context == "relogin"', mode="eval").body),
                           ast.unparse(ast.parse('context == "replace"', mode="eval").body))
                if src in anchors:
                    self.hit += 1
                    return None
                return node

        target_fn4 = _extract_specific_overload(MAIN_SRC, "_panel_manager_pass_command", "_MANAGER_QR_SESSIONS.get(owner)")
        stripper4 = _PassCommandDispatchStripper()
        mutant_fn4 = stripper4.visit(target_fn4)
        assert stripper4.hit == 2, f"MUT-4 anchors not found (hit={stripper4.hit})"
        ast.fix_missing_locations(mutant_fn4)

        tmp_rootm4, db_pathm4 = make_temp_env()
        nsm4 = build_qr_ns(db_pathm4, tmp_rootm4, script={"pass_sign_in": "ok"}, exclude_names={"_panel_manager_pass_command"})
        exec(compile(ast.Module(body=[mutant_fn4], type_ignores=[]), f"<{MAIN_PATH}:qr-mutant4>", "exec"), nsm4)
        await seed_manager(nsm4, key="mgrm4", tg_user_id=559401, username="m4_u")
        client_m4 = FakeQRClient("dummy", script={"pass_sign_in": "ok"}, tag="2fa")
        nsm4["_MANAGER_QR_SESSIONS"][92005] = {
            "manager_key": "mgrm4", "client": client_m4, "needs_2fa": True,
            "context": "relogin", "phone": "", "qr_login": None, "task": None, "expires_at_dt": None,
        }
        await nsm4["_panel_manager_pass_command"]("Password1", requested_by=92005)
        check("MUT-4. with the context dispatch stripped from _panel_manager_pass_command, a "
              "relogin-context 2FA success WRONGLY calls _manager_finalize_login instead of "
              "_manager_relogin_commit (proves 2FA-RELOGIN/2FA-REPLACE's context dispatch is "
              "load-bearing, reproducing the exact real bug this section guards against)",
              len(nsm4["__finalize_login_calls__"]) == 1 and len(nsm4["__relogin_commit_calls__"]) == 0,
              detail=(nsm4["__finalize_login_calls__"], nsm4["__relogin_commit_calls__"]))

        # ==================================================================
        # CONCURRENCY: _MANAGER_QR_SESSIONS is keyed by owner_user_id only.
        # Scenario A (checkpoint requirement) -- the SAME admin starts a
        # relogin QR for manager A, then (without cancelling) a replacement
        # QR for manager B -- must NOT leak the first session (orphaned task/
        # connected client) and must NOT let the second start's success
        # dispatch into the FIRST session's context. Both
        # _manager_relogin_qr_start/_manager_replace_qr_start already call
        # _manager_qr_session_drop(owner) before creating their own entry
        # (same pattern the pre-existing ADD flow's _panel_manager_qr_start_
        # command uses) -- this proves that call is actually load-bearing
        # for the cross-manager case, not just same-context re-starts.
        # ==================================================================
        tmp_rootCC, db_pathCC = make_temp_env()
        nsCC = build_qr_ns(db_pathCC, tmp_rootCC, script={"wait": "timeout"})
        await seed_manager(nsCC, key="mgrcca", tg_user_id=560001, username="cca_u")
        await seed_manager(nsCC, key="mgrccbold", tg_user_id=560002, username="ccbold_u")
        op_id_CC = await seed_draft_replacement_op(nsCC, old_key="mgrccbold", new_key="mgrccbnew")

        await nsCC["_manager_relogin_qr_start"](132345, "mgrcca")
        entry_first = nsCC["_MANAGER_QR_SESSIONS"].get(132345)
        client_first = entry_first.get("client")
        task_first = entry_first.get("task")
        check("CONCURRENCY-A1. first QR session (relogin mgrcca) recorded", entry_first is not None
              and entry_first.get("context") == "relogin")

        # SAME owner starts a SECOND QR flow (replace mgrccbnew) without
        # cancelling the first.
        await nsCC["_manager_replace_qr_start"](132345, op_id_CC, "New", "mgrccbnew", {})
        entry_second = nsCC["_MANAGER_QR_SESSIONS"].get(132345)
        check("CONCURRENCY-A2. the dict now holds the SECOND session (replace mgrccbnew), not the first",
              entry_second is not None and entry_second.get("context") == "replace"
              and entry_second.get("manager_key") == "mgrccbnew")
        check("CONCURRENCY-A3. the FIRST session's client was disconnected (no leaked connection) "
              "when the second QR flow started", client_first.disconnected is True)
        check("CONCURRENCY-A4. the FIRST session's background wait-task was cancelled, not left "
              "orphaned", task_first.cancelled() or task_first.done())

        # Let the second (now-current) session's wait resolve (timeout, so no
        # commit function fires) and confirm it is scoped to ITS OWN context
        # only -- the earlier relogin context never fires.
        entry_second_task = entry_second.get("task")
        if entry_second_task is not None:
            await entry_second_task
        check("CONCURRENCY-A5. after the switch, NEITHER commit function was ever called for the "
              "orphaned first (relogin) session", len(nsCC["__relogin_commit_calls__"]) == 0)

        # Scenario B: two DIFFERENT owners -- structurally independent dict
        # keys, proven by starting both and checking neither entry disturbs
        # the other.
        tmp_rootCD, db_pathCD = make_temp_env()
        nsCD = build_qr_ns(db_pathCD, tmp_rootCD, script={"wait": "hang"})
        await seed_manager(nsCD, key="mgrcdx", tg_user_id=560101, username="cdx_u")
        await seed_manager(nsCD, key="mgrcdy", tg_user_id=560102, username="cdy_u")
        await nsCD["_manager_relogin_qr_start"](142345, "mgrcdx")
        await nsCD["_manager_relogin_qr_start"](242345, "mgrcdy")
        eX = nsCD["_MANAGER_QR_SESSIONS"].get(142345)
        eY = nsCD["_MANAGER_QR_SESSIONS"].get(242345)
        check("CONCURRENCY-B1. two different owners each have their OWN independent session entry",
              eX is not None and eY is not None and eX is not eY)
        check("CONCURRENCY-B2. owner X's entry still references manager mgrcdx (untouched by Y's start)",
              eX.get("manager_key") == "mgrcdx")
        check("CONCURRENCY-B3. owner Y's entry references manager mgrcdy", eY.get("manager_key") == "mgrcdy")
        check("CONCURRENCY-B4. owner X's client was NOT disconnected by owner Y's unrelated QR start",
              eX.get("client").disconnected is False)

        # ==================================================================
        # CLEAN-*: regression coverage for a second real gap found during the
        # F3 independent checkpoint (2026-08-09, second pass) -- the SHARED
        # _panel_manager_qr_cancel_command (and _manager_qr_wait_task's own
        # timeout/error branches) called _manager_qr_session_drop (drops the
        # dict entry, disconnects the client) but never deleted the
        # relogin/replace TEMP session file, unlike the phone-based relogin/
        # replacement cancel commands which already do this for the
        # identical purpose. Fixed by routing all three call sites through
        # the new _manager_qr_temp_cleanup_for_context, which reuses the
        # SAME existing helpers (_manager_relogin_cleanup_temp_files /
        # _replacement_cleanup_temp_files) -- no new cleanup mechanism.
        # Deliberately does NOT call Telegram log_out() (local file/client
        # cleanup only, per explicit scope decision -- see the function's
        # own docstring in main.py).
        # ==================================================================

        # CLEAN-1: relogin cancel -> temp session file removed, live session
        # bytes unchanged.
        tmp_rootC1, db_pathC1 = make_temp_env()
        nsC1 = build_qr_ns(db_pathC1, tmp_rootC1)
        await seed_manager(nsC1, key="mgrc1", tg_user_id=561001, username="c1_u")
        live_pathC1 = nsC1["_manager_runtime_paths_for_key"]("mgrc1")["session_path"]
        os.makedirs(os.path.dirname(live_pathC1), exist_ok=True)
        with open(live_pathC1, "wb") as fh:
            fh.write(b"LIVE-C1-BYTES")
        temp_pathC1 = nsC1["_manager_relogin_temp_session_path"]("mgrc1")
        with open(temp_pathC1, "wb") as fh:
            fh.write(b"TEMP-C1-BYTES")
        await nsC1["_manager_relogin_qr_start"](171001, "mgrc1")
        await nsC1["_panel_manager_qr_cancel_command"]("", requested_by=171001)
        check("CLEAN-1. relogin cancel: temp session file removed", not os.path.exists(temp_pathC1))
        with open(live_pathC1, "rb") as fh:
            after_c1 = fh.read()
        check("CLEAN-1. relogin cancel: live session bytes UNCHANGED", after_c1 == b"LIVE-C1-BYTES", detail=after_c1)

        # CLEAN-2: replace cancel -> replacement temp file removed, OLD
        # manager's live session bytes unchanged.
        tmp_rootC2, db_pathC2 = make_temp_env()
        nsC2 = build_qr_ns(db_pathC2, tmp_rootC2)
        await seed_manager(nsC2, key="mgrc2old", tg_user_id=561002, username="c2old_u")
        old_live_pathC2 = nsC2["_manager_runtime_paths_for_key"]("mgrc2old")["session_path"]
        with open(old_live_pathC2, "wb") as fh:
            fh.write(b"LIVE-C2-OLD-BYTES")
        op_id_C2 = await seed_draft_replacement_op(nsC2, old_key="mgrc2old", new_key="mgrc2new")
        r_c2 = await nsC2["_manager_replace_qr_start"](172002, op_id_C2, "New", "mgrc2new", {})
        temp_pathC2 = nsC2["_replacement_temp_session_path"]("mgrc2new")
        with open(temp_pathC2, "wb") as fh:
            fh.write(b"TEMP-C2-BYTES")
        check("CLEAN-2 setup: replace QR start succeeded", "QR_URL=" in r_c2, detail=r_c2)
        await nsC2["_panel_manager_qr_cancel_command"]("", requested_by=172002)
        check("CLEAN-2. replace cancel: replacement temp file removed", not os.path.exists(temp_pathC2))
        with open(old_live_pathC2, "rb") as fh:
            after_c2 = fh.read()
        check("CLEAN-2. replace cancel: OLD manager's live session bytes UNCHANGED",
              after_c2 == b"LIVE-C2-OLD-BYTES", detail=after_c2)

        # CLEAN-3: relogin timeout -> temp file removed (via
        # _manager_qr_wait_task's own TimeoutError branch, not the cancel
        # command).
        tmp_rootC3, db_pathC3 = make_temp_env()
        nsC3 = build_qr_ns(db_pathC3, tmp_rootC3, script={"wait": "timeout"})
        await seed_manager(nsC3, key="mgrc3", tg_user_id=561003, username="c3_u")
        temp_pathC3 = nsC3["_manager_relogin_temp_session_path"]("mgrc3")
        with open(temp_pathC3, "wb") as fh:
            fh.write(b"TEMP-C3-BYTES")
        await nsC3["_manager_relogin_qr_start"](173003, "mgrc3")
        entryC3 = nsC3["_MANAGER_QR_SESSIONS"].get(173003)
        await entryC3["task"]
        check("CLEAN-3. relogin timeout: temp session file removed", not os.path.exists(temp_pathC3))

        # CLEAN-4: replace timeout -> temp file removed.
        tmp_rootC4, db_pathC4 = make_temp_env()
        nsC4 = build_qr_ns(db_pathC4, tmp_rootC4, script={"wait": "timeout"})
        await seed_manager(nsC4, key="mgrc4old", tg_user_id=561004, username="c4old_u")
        op_id_C4 = await seed_draft_replacement_op(nsC4, old_key="mgrc4old", new_key="mgrc4new")
        await nsC4["_manager_replace_qr_start"](174004, op_id_C4, "New", "mgrc4new", {})
        temp_pathC4 = nsC4["_replacement_temp_session_path"]("mgrc4new")
        with open(temp_pathC4, "wb") as fh:
            fh.write(b"TEMP-C4-BYTES")
        entryC4 = nsC4["_MANAGER_QR_SESSIONS"].get(174004)
        await entryC4["task"]
        check("CLEAN-4. replace timeout: temp session file removed", not os.path.exists(temp_pathC4))

        # CLEAN-5: 2FA wrong password, THEN a final cancel -> temp cleanup
        # still happens (the wrong-password attempt itself must NOT drop/
        # clean the session -- proven by 2FA-WRONG-PASSWORD already -- but
        # the SUBSEQUENT cancel must).
        tmp_rootC5, db_pathC5 = make_temp_env()
        nsC5 = build_qr_ns(db_pathC5, tmp_rootC5, script={"pass_sign_in": "wrong"})
        await seed_manager(nsC5, key="mgrc5", tg_user_id=561005, username="c5_u")
        temp_pathC5 = nsC5["_manager_relogin_temp_session_path"]("mgrc5")
        with open(temp_pathC5, "wb") as fh:
            fh.write(b"TEMP-C5-BYTES")
        client_c5 = FakeQRClient(temp_pathC5, script={"pass_sign_in": "wrong"}, tag="2fa")
        nsC5["_MANAGER_QR_SESSIONS"][175005] = {
            "manager_key": "mgrc5", "client": client_c5, "needs_2fa": True,
            "context": "relogin", "phone": "", "qr_login": None, "task": None, "expires_at_dt": None,
        }
        await nsC5["_panel_manager_pass_command"]("WrongPass", requested_by=175005)
        check("CLEAN-5 setup: temp file still present after a wrong-password attempt (session not dropped)",
              os.path.exists(temp_pathC5))
        await nsC5["_panel_manager_qr_cancel_command"]("", requested_by=175005)
        check("CLEAN-5. final cancel after 2FA wrong-password: temp session file removed",
              not os.path.exists(temp_pathC5))

        # CLEAN-6: successful relogin commit -> os.replace() already leaves
        # NO orphan temp file (proven against the REAL, unmodified
        # _manager_relogin_commit -- not the fake used everywhere else in
        # this file). No new deletion code was added for this path.
        tmp_rootC6, db_pathC6 = make_temp_env()
        nsC6 = build_relogin_commit_ns(db_pathC6, tmp_rootC6)
        await nsC6["__storage__"].manager_add(
            manager_key="mgrc6", display_name="C6", phone="+70000000006", status="active",
            session_path=str(nsC6["_manager_runtime_paths_for_key"]("mgrc6")["session_path"]),
            db_path=str(nsC6["_manager_runtime_paths_for_key"]("mgrc6")["db_path"]),
            workdir=str(nsC6["_manager_runtime_paths_for_key"]("mgrc6")["root"]),
            log_path=str(nsC6["_manager_runtime_paths_for_key"]("mgrc6")["log_path"]), is_enabled=1,
        )
        os.makedirs(nsC6["_manager_runtime_paths_for_key"]("mgrc6")["root"], exist_ok=True)
        live_pathC6 = nsC6["_manager_runtime_paths_for_key"]("mgrc6")["session_path"]
        with open(live_pathC6, "wb") as fh:
            fh.write(b"OLD-LIVE-C6")
        temp_pathC6 = nsC6["_manager_relogin_temp_session_path"]("mgrc6")
        with open(temp_pathC6, "wb") as fh:
            fh.write(b"NEW-AUTHORIZED-C6")
        nsC6["__client_calls__"].clear()
        # The fake client factory's is_user_authorized() defaults to True
        # (mirrors QR having already succeeded before commit is ever called).
        r_c6 = await nsC6["_manager_relogin_commit"]("mgrc6", 6, phone_hint="+70000000006")
        check("CLEAN-6. relogin commit reaches the real OK marker", nsC6["_RELOGIN_COMMIT_OK_MARKER"] in r_c6, detail=r_c6)
        check("CLEAN-6. successful relogin commit leaves NO orphan temp file (os.replace already "
              "moved it)", not os.path.exists(temp_pathC6), detail=(r_c6, os.path.exists(temp_pathC6)))
        check("CLEAN-6. the live path now holds the NEW (promoted) content", os.path.exists(live_pathC6)
              and open(live_pathC6, "rb").read() == b"NEW-AUTHORIZED-C6")

        # CLEAN-7: successful replacement promote -> os.replace() already
        # leaves NO orphan temp file (real _repl4_promote_session).
        tmp_rootC7, db_pathC7 = make_temp_env()
        nsC7 = build_promote_ns(db_pathC7, tmp_rootC7)
        new_key_c7 = "mgrc7new"
        paths_c7 = nsC7["build_manager_paths"](str(tmp_rootC7), new_key_c7)
        os.makedirs(paths_c7["root"], exist_ok=True)
        temp_pathC7 = nsC7["_replacement_temp_session_path"](new_key_c7, base_dir=tmp_rootC7)
        perm_pathC7 = nsC7["_replacement_permanent_session_path"](new_key_c7, base_dir=tmp_rootC7)
        with open(temp_pathC7, "wb") as fh:
            fh.write(b"NEW-AUTHORIZED-C7")
        op_row_c7 = {
            "operation_id": "op-c7", "new_tg_user_id": 700777, "created_by_user_id": 0,
            "proxy_mode": "direct", "proxy_confirmed": 1,
        }
        old_row_c7 = {"session_path": ""}
        me_c7 = FakeMe(id=700777)
        nsC7["__client_calls__"].clear()
        result_c7 = await nsC7["_repl4_promote_session"](op_row_c7, old_row_c7, new_key_c7, tmp_rootC7,
                                                          client_factory=lambda p, cfg: FakeQRClient(p, script={"me": me_c7}, tag="promote"))
        check("CLEAN-7. promote returns None (success -- no error result)", result_c7 is None, detail=result_c7)
        check("CLEAN-7. successful promote leaves NO orphan temp file", not os.path.exists(temp_pathC7))
        check("CLEAN-7. the permanent path now holds the promoted content",
              os.path.exists(perm_pathC7) and open(perm_pathC7, "rb").read() == b"NEW-AUTHORIZED-C7")

        # CLEAN-8: cleaning up one manager/context's temp leaves an UNRELATED
        # manager/context's own temp file untouched.
        tmp_rootC8, db_pathC8 = make_temp_env()
        nsC8 = build_qr_ns(db_pathC8, tmp_rootC8)
        await seed_manager(nsC8, key="mgrc8a", tg_user_id=561008, username="c8a_u")
        await seed_manager(nsC8, key="mgrc8bold", tg_user_id=561009, username="c8bold_u")
        op_id_C8 = await seed_draft_replacement_op(nsC8, old_key="mgrc8bold", new_key="mgrc8bnew")
        # replacement_send_qr_start (called inside _manager_replace_qr_start)
        # is what creates mgrc8bnew's runtime directory via ensure_manager_
        # dirs -- must run before a temp file can be written under it.
        await nsC8["_manager_replace_qr_start"](179009, op_id_C8, "New", "mgrc8bnew", {})
        temp_pathC8a = nsC8["_manager_relogin_temp_session_path"]("mgrc8a")
        temp_pathC8b = nsC8["_replacement_temp_session_path"]("mgrc8bnew")
        with open(temp_pathC8a, "wb") as fh:
            fh.write(b"TEMP-C8A")
        with open(temp_pathC8b, "wb") as fh:
            fh.write(b"TEMP-C8B")
        await nsC8["_manager_relogin_qr_start"](178008, "mgrc8a")
        await nsC8["_panel_manager_qr_cancel_command"]("", requested_by=178008)
        check("CLEAN-8. cleaning up mgrc8a's relogin temp removed ONLY its own file",
              not os.path.exists(temp_pathC8a))
        check("CLEAN-8. the UNRELATED mgrc8bnew replace temp file is UNTOUCHED",
              os.path.exists(temp_pathC8b) and open(temp_pathC8b, "rb").read() == b"TEMP-C8B")

        # CLEAN-9: structural collision safety -- even if
        # _manager_qr_temp_cleanup_for_context is called for a manager_key
        # that IS live/active, it can never delete the live session (the
        # temp helpers compute their OWN suffixed path internally; there is
        # no code path by which they could target the bare "<key>.session"
        # file). Proven directly against the real cleanup dispatcher, not by
        # trying to trick it into a collision that cannot exist.
        tmp_rootC9, db_pathC9 = make_temp_env()
        nsC9 = build_qr_ns(db_pathC9, tmp_rootC9)
        await seed_manager(nsC9, key="mgrc9", tg_user_id=561010, username="c9_u")
        live_pathC9 = nsC9["_manager_runtime_paths_for_key"]("mgrc9")["session_path"]
        with open(live_pathC9, "wb") as fh:
            fh.write(b"LIVE-C9-BYTES")
        nsC9["_manager_qr_temp_cleanup_for_context"]("relogin", "mgrc9")
        nsC9["_manager_qr_temp_cleanup_for_context"]("replace", "mgrc9")
        with open(live_pathC9, "rb") as fh:
            after_c9 = fh.read()
        check("CLEAN-9. calling the cleanup dispatcher for a LIVE manager_key (both contexts) "
              "never touches its live session file", after_c9 == b"LIVE-C9-BYTES", detail=after_c9)

        # MUT-5: strip the temp-cleanup call out of
        # _panel_manager_qr_cancel_command's relogin branch -> CLEAN-1's
        # property goes RED (temp file survives the cancel), proving the fix
        # is load-bearing.
        class _CleanupCallStripper(ast.NodeTransformer):
            def __init__(self):
                self.hit = 0

            def visit_Expr(self, node):
                self.generic_visit(node)
                if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "_manager_qr_temp_cleanup_for_context"):
                    self.hit += 1
                    return None
                return node

        target_fn5 = None
        for _n in ast.parse(MAIN_SRC).body:
            if isinstance(_n, ast.AsyncFunctionDef) and _n.name == "_panel_manager_qr_cancel_command":
                target_fn5 = _n
                break
        assert target_fn5 is not None
        stripper5 = _CleanupCallStripper()
        mutant_fn5 = stripper5.visit(target_fn5)
        assert stripper5.hit >= 1, f"MUT-5 anchor not found (hit={stripper5.hit})"
        ast.fix_missing_locations(mutant_fn5)

        tmp_rootM5, db_pathM5 = make_temp_env()
        nsM5 = build_qr_ns(db_pathM5, tmp_rootM5, exclude_names={"_panel_manager_qr_cancel_command"})
        exec(compile(ast.Module(body=[mutant_fn5], type_ignores=[]), f"<{MAIN_PATH}:qr-mutant5>", "exec"), nsM5)
        await seed_manager(nsM5, key="mgrm5", tg_user_id=561011, username="m5_u")
        temp_pathM5 = nsM5["_manager_relogin_temp_session_path"]("mgrm5")
        with open(temp_pathM5, "wb") as fh:
            fh.write(b"TEMP-M5")
        await nsM5["_manager_relogin_qr_start"](181001, "mgrm5")
        await nsM5["_panel_manager_qr_cancel_command"]("", requested_by=181001)
        check("MUT-5. with the temp-cleanup call stripped from the cancel command, CLEAN-1's "
              "property goes RED -- the temp file SURVIVES the cancel (proves the fix is "
              "load-bearing, not a tautology)",
              os.path.exists(temp_pathM5), detail=os.path.exists(temp_pathM5))

    asyncio.run(run())

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL QR AUTH CONTEXT SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
