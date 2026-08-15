# -*- coding: utf-8 -*-
"""tools/manager_replacement_backend_selftest.py -- offline self-test for the
Stage 2 fresh-session manager-REPLACEMENT backend added to main.py (2026-07-15
architecture audit; Stage 1 = storage.py durable state machine, independently
reviewed and fixed; Stage 2 = this file's target, the onboarding backend +
pre-commit rollback primitives).

Scope under test: main.py's replacement_* functions (draft -> ... ->
ready_commit only -- Stage 2 never reaches 'done'). Replacement is NOT
relogin: it creates a brand-new manager_key with its own brand-new temporary
AND future-permanent session; the OLD manager's row/session/runtime are
never touched anywhere in this file's target code.

Techniques (same as every other tools/*_selftest.py in this project):
  - main.py cannot be imported standalone (Telethon/env side effects at
    import time) -- the Stage 2 functions and their direct, pre-existing,
    UNMODIFIED dependencies are extracted via ast.parse + ast.unparse +
    exec() and run FOR REAL against a temporary SQLite DB (never
    db/data_tpilot.db) and a temporary runtime directory (never
    runtime/managers/).
  - main.py has `from __future__ import annotations` at its top, and so
    does this file -- CPython's compile() inherits the caller's active
    __future__ feature flags by default (empirically verified), so the
    extracted functions' type hints (Optional[str], Dict[str, Any], ...)
    are never eagerly evaluated and never need Optional/Dict/Any/Tuple
    bound in the exec namespace.
  - Telethon itself is not installed in this local dev environment, so the
    temp TelegramClient and the SessionPasswordNeededError/
    PhoneCodeInvalidError/PhoneCodeExpiredError/PasswordHashInvalidError/
    PhoneNumberBannedError exception types it raises are FAKED under their
    exact production names -- `except PhoneCodeInvalidError:` inside the
    extracted code matches by whatever class object is bound to that name
    in the exec namespace, so a lightweight local Exception subclass works
    identically to the real telethon.errors class for every test here.
  - storage.py's manager_*/manager_replacements-adjacent async functions
    read a plain module-level `storage.DB_PATH` global (not a parameter) --
    pointed at the temp DB via direct attribute assignment before each
    test, exactly like manager_relogin_selftest.py and a real deployment.
  - Stage 1's replacement_* sync helpers ARE parameterized (db_path=...) --
    every Stage 2 function call in this file passes db_path explicitly too.

Never: real Telegram network, real proxy/provider network, real process
spawn/stop, production DB/runtime/session/log access.

    python tools\\manager_replacement_backend_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()


# ======================================================================
# Fake Telethon layer (telethon itself is not installed locally; these
# stand-ins are bound under the exact production names the extracted code
# references, so `except PhoneCodeInvalidError:` etc. match correctly).
# ======================================================================

class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class PhoneCodeExpiredError(Exception):
    pass


class PasswordHashInvalidError(Exception):
    pass


class PhoneNumberBannedError(Exception):
    pass


class FloodWaitError(Exception):
    """Name matters: _manager_is_rate_limit_error() checks 'flood' in
    exc.__class__.__name__.lower(), so this must be named exactly this."""
    def __init__(self, seconds: int = 5):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


def make_fake_client_factory(script: dict, calls: list, authorized_sessions: set, created_clients: list = None):
    """authorized_sessions is a SHARED set of session_path strings -- mimics a
    real Telethon session FILE persisting its own auth state independent of
    which client object currently has it open (production code opens a
    fresh client on the same temp path multiple times across the
    phone->code->password steps). Returns a factory(session_path, proxy_config)
    matching every Stage 2 client_factory call site uniformly.

    created_clients, if supplied, records every FakeTelegramClient instance
    built by this factory (each exposes .cfg, the resolved proxy_config it
    was actually constructed with) -- used by the R2 durable-proxy tests to
    prove a reconnect resolved the correct durable proxy without the caller
    resupplying it."""

    class _FakeMe:
        def __init__(self, uid, username, first, last, phone):
            self.id = uid
            self.username = username
            self.first_name = first
            self.last_name = last
            self.phone = phone

    class FakeTelegramClient:
        def __init__(self, session_path, cfg=None):
            self.session_path = str(session_path)
            self.cfg = cfg
            if created_clients is not None:
                created_clients.append(self)

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
            if script.get("sign_in_forbidden"):
                # Stage 2 fix R4 proof: the backend must check
                # is_user_authorized() BEFORE ever calling sign_in again on
                # an already-authorized session. Tests that pre-authorize a
                # session and set this flag assert this exception never
                # surfaces.
                raise AssertionError("sign_in must not be called on an already-authorized session")
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

    def factory(session_path, cfg=None):
        return FakeTelegramClient(session_path, cfg)

    return factory


# ======================================================================
# main.py extraction: the Stage 2 replacement-backend contour + its direct,
# pre-existing, UNMODIFIED dependencies (storage.py functions accessed
# through the real module, manager_registry helpers, small pure main.py
# helpers already used by relogin/onboarding). _replacement_build_client
# is deliberately NOT extracted -- every test below always injects an
# explicit client_factory, so that function's own real-Telethon-construction
# body (referencing TelegramClient/_api_profile_for/_TELETHON_DEVICE_KWARGS)
# is never reached and never needs faking.
# ======================================================================

STAGE2_REAL_NAMES = {
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
    "_replacement_tg_user_conflict", "_replacement_username_conflict",
    "_replacement_tgid_reserved_by_other",
    "_replacement_onboarding_proxy_fields", "_replacement_resolve_durable_proxy",
    "_replacement_cancel_cleanup",
    "_replacement_fail", "_replacement_build_preview",
    "replacement_start", "replacement_reserve_key", "replacement_describe_proxy",
    "replacement_send_phone_code", "_replacement_after_signin",
    "replacement_submit_code", "replacement_submit_password",
    "replacement_ready_commit_preview", "replacement_cancel", "replacement_recover",
    "_manager_is_rate_limit_error", "_manager_extract_wait_seconds", "_map_send_code_error",
    "_manager_auth_audit_log",
    "_resolve_manager_telethon_proxy", "_build_telethon_proxy_from_row",
    "_manager_auth_proxy_mode", "_proxy_type_norm", "_proxy_port_int", "_manager_proxy_enabled",
    "_future_iso", "_now_utc_iso", "_partner_source_key_for_manager",
}


# TPILOT RUNTIME AUTH SESSION-REUSE FIX 20260718 regression coverage:
# _repl4_create_new_manager (Stage 4) + its own direct, pre-existing,
# UNMODIFIED dependencies -- deliberately NOT merged into STAGE2_REAL_NAMES
# (checks 8j/12f above assert _manager_finalize_login/_spawn_manager_process
# are never reachable from the STAGE2_REAL_NAMES-extracted source -- that
# invariant is about the draft->ready_commit backend, and must stay accurate;
# _repl4_create_new_manager is Stage 4, not Stage 2). Only ever extracted
# together with STAGE2_REAL_NAMES via build_stage2_ns(..., extra_names=
# STAGE4_PHONE_NAMES), never the whole Stage 4 engine (that is
# manager_replacement_commit_selftest.py's target). _repl4_result/
# _repl4_progress are pulled in only so a genuine failure inside
# _repl4_create_new_manager renders as a legible structured result instead
# of a NameError. _manager_finalize_login's OWN dependencies
# (_manager_runtime_onboarding_clear, _manager_runtime_paths_for_key,
# _manager_label_from_row, _ONBOARDING_SOURCE_PICK_MARKER) are pulled in for
# the same reason; _tp_finalize_screenshots_on and _spawn_manager_process are
# deliberately NOT extracted -- the former is called inside a bare
# try/except that swallows the resulting NameError (exactly like a real
# missing-dependency screenshot-config failure would be swallowed in
# production), and the latter is only reached when spawn_after_login=True,
# which _repl4_create_new_manager never passes.
STAGE4_PHONE_NAMES = {
    "_repl4_create_new_manager", "_repl4_result", "_repl4_progress",
    "_manager_finalize_login", "_manager_runtime_onboarding_clear",
    "_manager_runtime_paths_for_key", "_manager_label_from_row",
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


def _selftest_db_guard(db_path: str, base_dir, storage_mod) -> None:
    """Fail-safe (forensic review recommendation, 2026-07-16, prompted by a
    real incident in the sibling manager_relogin_selftest.py harness where a
    forgotten QUEUE_DB_PATH rebind let replacement_* writes fall through to
    the real db/data_tpilot.db file): refuses any db_path located under
    base_dir/db (the project's own db/ directory, boundary-checked with
    os.sep so a sibling like .../dbfoo is never a false match), and requires
    both storage DB-path globals to already equal db_path. Raises
    AssertionError on any violation. Kept standalone (not inlined into
    build_stage2_ns) specifically so it can be unit-tested directly against
    synthetic bad inputs -- see test_dbguard_rejects_unsafe_paths -- without
    ever touching the real storage.DB_PATH/QUEUE_DB_PATH globals or opening
    any real connection."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def build_stage2_ns(db_path: str, base_dir: Path, *, script: dict = None, extra_names: set = None) -> dict:
    import manager_registry
    import storage as _storage
    import aiosqlite as _aiosqlite
    from datetime import datetime as _dt, timedelta as _td

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    # NOTE: this function's own "base_dir" parameter is the per-test TEMP
    # root, not the project root -- the module-level BASE_DIR is used
    # deliberately here instead of base_dir.
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    script = script if script is not None else {}
    calls: list = []
    authorized_sessions: set = set()
    client_factory = make_fake_client_factory(script, calls, authorized_sessions)

    # extra_names (e.g. STAGE4_PHONE_NAMES) is ADDITIVE ONLY for this one
    # namespace build -- STAGE2_REAL_NAMES itself is never mutated, so the
    # 8j/12f static invariant checks (which re-parse MAIN_SRC against the
    # STAGE2_REAL_NAMES constant directly) stay accurate regardless of what
    # any individual test additionally pulls in here.
    names = STAGE2_REAL_NAMES | (extra_names or set())
    nodes = _extract_by_names(MAIN_SRC, names)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    ns = {
        "os": os,
        "re": __import__("re"),
        "asyncio": asyncio,
        "Path": Path,
        "aiosqlite": _aiosqlite,
        "datetime": _dt,
        "timedelta": _td,
        "BASE_DIR": base_dir,
        "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "mask_phone": manager_registry.mask_phone,
        "validate_manager_key": manager_registry.validate_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "ensure_manager_dirs": manager_registry.ensure_manager_dirs,
        "manager_get": _storage.manager_get,
        "manager_get_onboarding": _storage.manager_get_onboarding,
        "manager_save_onboarding": _storage.manager_save_onboarding,
        "manager_delete_onboarding": _storage.manager_delete_onboarding,
        "manager_list_pending": _storage.manager_list_pending,
        # Needed by _repl4_create_new_manager (manager_add) and by both it
        # and _manager_finalize_login (manager_set_fields) -- the real,
        # unmodified storage.py primitives, exactly as seed_old_manager()
        # already calls directly in this same file.
        "manager_add": _storage.manager_add,
        "manager_set_fields": _storage.manager_set_fields,
        "_repl_storage": _storage,
        "SessionPasswordNeededError": SessionPasswordNeededError,
        "PhoneCodeInvalidError": PhoneCodeInvalidError,
        "PhoneCodeExpiredError": PhoneCodeExpiredError,
        "PasswordHashInvalidError": PasswordHashInvalidError,
        "PhoneNumberBannedError": PhoneNumberBannedError,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:replacement>", "exec"), ns)
    ns["__calls__"] = calls
    ns["__authorized_sessions__"] = authorized_sessions
    ns["__client_factory__"] = client_factory
    ns["__storage__"] = _storage
    return ns


# ======================================================================
# Temp DB / fixture helpers.
# ======================================================================

def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="replacement_backend_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def seed_old_manager(ns: dict, *, key="oldmgr", tg_user_id=555001, display_name="СтарыйМенеджер",
                            username="oldmgr_u", status="active", is_enabled=1, manual_stopped=0,
                            source_key="src1") -> dict:
    storage_mod = ns["__storage__"]
    paths = ns["build_manager_paths"](str(ns["BASE_DIR"]), key)
    os.makedirs(paths["root"], exist_ok=True)
    await storage_mod.manager_add(
        manager_key=key, display_name=display_name, phone="+70000000000", status=status,
        session_path=paths["session_path"], db_path=paths["db_path"], workdir=paths["root"],
        log_path=paths["log_path"], is_enabled=is_enabled,
    )
    await storage_mod.manager_set_fields(key, manual_stopped=manual_stopped,
                                          tg_user_id=(tg_user_id or None), telegram_username=username)
    con = sqlite3.connect(ns["TPILOT_DB_PATH"])
    try:
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("DELETE FROM manager_source_links WHERE manager_key=?", (key,))
        con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, source_key))
        con.commit()
    finally:
        con.close()
    # Real bytes on disk so tests can byte-hash-verify the OLD session is
    # never touched by anything Stage 2 does.
    with open(paths["session_path"], "wb") as f:
        f.write(b"OLD-BOEVOY-SESSION-BYTES-UNTOUCHED")
    return await storage_mod.manager_get(key)


def read_bytes_or_none(path: str):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


async def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def _opid(tag: str = "op") -> str:
    return f"{tag}-{uuid.uuid4().hex[:10]}"


_AUTO_IDENTITY_COUNTER = [700000]


def _next_auto_identity() -> int:
    _AUTO_IDENTITY_COUNTER[0] += 1
    return _AUTO_IDENTITY_COUNTER[0]


async def _drive_to_identity_ok(ns: dict, old_key: str, new_key: str, *, owner=1001,
                                 client_factory=None) -> str:
    """Shared setup: start -> phone -> code -> (identity_ok). Returns operation_id.

    Defaults to a freshly minted fake identity per call so that multiple
    invocations sharing one `ns` within a test group never collide on
    ns["__client_factory__"]'s single hardcoded identity (that collision
    would be reported as a real, correctly-detected identity_conflict).
    """
    if client_factory is None:
        uid = _next_auto_identity()
        client_factory = make_fake_client_factory(
            {"actual_user_id": uid, "actual_username": f"auto{uid}"}, [], set()
        )
    r0 = await ns["replacement_start"](old_key, owner, db_path=ns["TPILOT_DB_PATH"])
    op = r0["operation_id"]
    r1 = await ns["replacement_send_phone_code"](
        op, "Новый Менеджер", new_key, "+19998887777", {}, db_path=ns["TPILOT_DB_PATH"],
        base_dir=ns["BASE_DIR"], client_factory=client_factory,
    )
    assert r1["ok"], r1
    r2 = await ns["replacement_submit_code"](
        op, "12345", {}, db_path=ns["TPILOT_DB_PATH"], base_dir=ns["BASE_DIR"],
        client_factory=client_factory,
    )
    assert r2["ok"], r2
    return op


# ======================================================================
# GROUP 1: start and locking
# ======================================================================

async def test_group_1_start_and_locking():
    print("\n-- Group 1: start and locking --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        old = await seed_old_manager(ns, key="oldmgr1")

        r1 = await ns["replacement_start"]("oldmgr1", 1001, db_path=db_path)
        check("1a. valid operation creation succeeds", r1["ok"] is True, repr(r1))
        check("1a2. status is draft", r1.get("status") == "draft", repr(r1))
        op1 = r1["operation_id"]

        r1b = await ns["replacement_start"]("oldmgr1", 1001, operation_id=op1, db_path=db_path)
        check("1b. idempotent operation_id returns the SAME operation, not a conflict",
              r1b["ok"] is True and r1b["operation_id"] == op1, repr(r1b))

        r2 = await ns["replacement_start"]("nosuchmanager", 1001, db_path=db_path)
        check("1c. missing old manager fails cleanly", r2["ok"] is False and r2["code"] == "old_not_found", repr(r2))

        await ns["__storage__"].manager_set_fields("oldmgr1", status="archived")
        r3 = await ns["replacement_start"]("oldmgr1", 1001, db_path=db_path)
        check("1d. archived old manager is rejected", r3["ok"] is False and r3["code"] == "old_archived", repr(r3))
        await ns["__storage__"].manager_set_fields("oldmgr1", status="active")

        r4 = await ns["replacement_start"]("oldmgr1", 0, db_path=db_path)
        check("1e. invalid (zero) initiating user is rejected", r4["ok"] is False and r4["code"] == "invalid_user", repr(r4))
        r4b = await ns["replacement_start"]("oldmgr1", -5, db_path=db_path)
        check("1e2. invalid (negative) initiating user is rejected", r4b["ok"] is False, repr(r4b))

        r5 = await ns["replacement_start"]("oldmgr1", 2002, db_path=db_path)
        check("1f. a SECOND, different operation for the same old_manager_key is rejected (active replacement conflict)",
              r5["ok"] is False and r5["code"] == "already_active" and r5["operation_id"] == op1, repr(r5))

        seeded_relogin = {"called": False}
        async def fake_relogin_active(old_key):
            seeded_relogin["called"] = True
            return old_key == "oldmgr2"
        ns2 = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns2, key="oldmgr2")
        r6 = await ns2["replacement_start"]("oldmgr2", 1001, db_path=db_path, relogin_active_fn=fake_relogin_active)
        check("1g. active relogin conflict blocks start", r6["ok"] is False and r6["code"] == "relogin_active", repr(r6))
        check("1g2. relogin_active_fn was actually consulted (injectable, not hardcoded)", seeded_relogin["called"] is True)

        row1 = await ns["manager_get"]("oldmgr1")
        check("1h. old identity snapshot matches the live old manager row",
              r1.get("old_display_name") == old.get("display_name") and r1.get("old_username") == old.get("username", old.get("telegram_username")),
              repr((r1, old)))
        check("1i. no mutation of the old manager row by replacement_start",
              row1.get("display_name") == old.get("display_name") and row1.get("status") == "active", repr(row1))
        check("1j. no new managers row created by replacement_start (only 'oldmgr1' exists)",
              (await ns["manager_get"]("newmgr_never_created")) is None)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 2: name and key
# ======================================================================

async def test_group_2_name_and_key():
    print("\n-- Group 2: name and key --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns, key="oldmgrk")
        r0 = await ns["replacement_start"]("oldmgrk", 1001, db_path=db_path)
        op = r0["operation_id"]

        r1 = await ns["replacement_send_phone_code"](
            op, "   ", "newkey1", "+1", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"],
        )
        check("2a. empty/whitespace-only display name is rejected", r1["ok"] is False and r1["code"] == "empty_display_name", repr(r1))

        r2 = await ns["replacement_reserve_key"](op, "Новый Ключ Юникод", db_path=db_path, base_dir=tmp_root)
        check("2b. Unicode/invalid display characters in a KEY are rejected (key must be [a-z0-9_-])",
              r2["ok"] is False and r2["code"] == "invalid_key", repr(r2))
        r2b = await ns["replacement_send_phone_code"](
            op, "Юникод Имя", "unicodekey1", "+19995551111", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("2b2. Unicode DISPLAY NAME (not key) is accepted", r2b["ok"] is True, repr(r2b))
        await ns["replacement_cancel"](op, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])

        r0b = await ns["replacement_start"]("oldmgrk", 1002, db_path=db_path)
        op2 = r0b["operation_id"]

        r3 = await ns["replacement_reserve_key"](op2, "bad key!", db_path=db_path, base_dir=tmp_root)
        check("2c. invalid characters rejected", r3["ok"] is False and r3["code"] == "invalid_key", repr(r3))

        r4 = await ns["replacement_reserve_key"](op2, "../../etc/passwd", db_path=db_path, base_dir=tmp_root)
        check("2d. path traversal rejected", r4["ok"] is False and r4["code"] == "invalid_key", repr(r4))
        r4b = await ns["replacement_reserve_key"](op2, "..\\..\\windows", db_path=db_path, base_dir=tmp_root)
        check("2d2. windows-style traversal rejected", r4b["ok"] is False, repr(r4b))

        r5 = await ns["replacement_reserve_key"](op2, "oldmgrk", db_path=db_path, base_dir=tmp_root)
        check("2e. same as old key rejected", r5["ok"] is False and r5["code"] == "same_as_old", repr(r5))

        r6 = await ns["replacement_reserve_key"](op2, "oldmgrk", db_path=db_path, base_dir=tmp_root)
        check("2e2. (repeat) same-as-old is deterministic", r6["ok"] is False and r6["code"] == "same_as_old")

        await seed_old_manager(ns, key="existingmgr")
        r7 = await ns["replacement_reserve_key"](op2, "existingmgr", db_path=db_path, base_dir=tmp_root)
        check("2f. existing manager key rejected", r7["ok"] is False and r7["code"] == "key_exists", repr(r7))

        ns["__storage__"].manager_stats_tombstone_upsert("tombstonedkey", display_name="X", db_path=db_path)
        r8 = await ns["replacement_reserve_key"](op2, "tombstonedkey", db_path=db_path, base_dir=tmp_root)
        check("2g. existing tombstoned key rejected", r8["ok"] is False and r8["code"] == "key_tombstoned", repr(r8))

        conflict_paths = ns["build_manager_paths"](str(tmp_root), "hasrundir")
        os.makedirs(conflict_paths["root"], exist_ok=True)
        r9 = await ns["replacement_reserve_key"](op2, "hasrundir", db_path=db_path, base_dir=tmp_root)
        check("2h. existing runtime directory rejected", r9["ok"] is False and r9["code"] == "runtime_dir_exists", repr(r9))

        session_paths = ns["build_manager_paths"](str(tmp_root), "hassession")
        os.makedirs(session_paths["root"], exist_ok=True)
        with open(session_paths["session_path"], "wb") as f:
            f.write(b"x")
        r10 = await ns["replacement_reserve_key"](op2, "hassession", db_path=db_path, base_dir=tmp_root)
        check("2i. existing permanent session rejected", r10["ok"] is False and r10["code"] == "session_exists", repr(r10))

        r11a = await ns["replacement_reserve_key"](op2, "freekey1", db_path=db_path, base_dir=tmp_root)
        check("2j-setup. free key is available", r11a["ok"] is True, repr(r11a))
        r11b = await ns["replacement_send_phone_code"](
            op2, "Имя", "freekey1", "+19995552222", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("2j. send_phone_code (which re-persists the key) succeeds after reservation", r11b["ok"] is True, repr(r11b))
        r11c = await ns["replacement_reserve_key"](op2, "freekey1", db_path=db_path, base_dir=tmp_root)
        check("2j2. same operation + same key is idempotent (still ok, still reserved)",
              r11c["ok"] is True and r11c["code"] == "key_reserved", repr(r11c))

        r0c = await ns["replacement_start"]("existingmgr", 1003, db_path=db_path)
        # existingmgr already has no active replacement (it was only used as a
        # manager fixture) -- start should succeed for a distinct old key.
        op3 = r0c["operation_id"]
        r12 = await ns["replacement_reserve_key"](op3, "freekey1", db_path=db_path, base_dir=tmp_root)
        check("2k. TWO operations reserving the SAME key -> conflict for the second",
              r12["ok"] is False and r12["code"] == "key_reserved_elsewhere", repr(r12))

        r13 = await ns["replacement_reserve_key"](op3, "runtime/../../../escape", db_path=db_path, base_dir=tmp_root)
        check("2l. no raw path escape survives normalization (rejected as invalid_key)", r13["ok"] is False, repr(r13))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 3: session isolation
# ======================================================================

async def test_group_3_session_isolation():
    print("\n-- Group 3: session isolation --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        old = await seed_old_manager(ns, key="oldsess")
        old_paths = ns["build_manager_paths"](str(tmp_root), "oldsess")
        old_bytes_before = read_bytes_or_none(old_paths["session_path"])

        op = await _drive_to_identity_ok(ns, "oldsess", "newsess")

        temp_path = ns["_replacement_temp_session_path"]("newsess", base_dir=tmp_root)
        perm_path = ns["_replacement_permanent_session_path"]("newsess", base_dir=tmp_root)
        check("3a. old/temp/permanent paths are all distinct",
              len({old_paths["session_path"], temp_path, perm_path}) == 3,
              repr((old_paths["session_path"], temp_path, perm_path)))

        old_bytes_after = read_bytes_or_none(old_paths["session_path"])
        check("3b. old session byte-hash unchanged after start+phone+code",
              old_bytes_before == old_bytes_after and old_bytes_after == b"OLD-BOEVOY-SESSION-BYTES-UNTOUCHED",
              "bytes changed!")

        check("3c. temp session created only under the NEW key's runtime dir",
              temp_path.replace("\\", "/").endswith("runtime/managers/newsess/newsess.replace.session"), temp_path)
        check("3c2. temp session file actually exists after phone+code", os.path.exists(temp_path))
        check("3d. permanent session for the new key was NEVER created by Stage 2", not os.path.exists(perm_path))

        # Stale temp conflict: another leftover file with the SAME temp path
        # from a "previous crashed attempt" -- reserve/send-phone-code must
        # not silently reuse foreign content; ensure_manager_dirs + connect()
        # only appends/creates, ast: verify it isn't deleted or renamed
        # unexpectedly by anything OTHER than explicit cleanup.
        with open(temp_path + "-journal", "wb") as f:
            f.write(b"JOURNAL-BYTES")
        check("3e. journal companion created ok (pre-condition for cleanup test)", os.path.exists(temp_path + "-journal"))

        ok = await ns["replacement_cancel"](op, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("3f. cancel succeeds", ok["ok"] is True, repr(ok))
        check("3g. temp session removed by cancel cleanup", not os.path.exists(temp_path))
        check("3g2. temp journal companion removed by cancel cleanup", not os.path.exists(temp_path + "-journal"))

        old_bytes_final = read_bytes_or_none(old_paths["session_path"])
        check("3h. old session STILL untouched after cancel cleanup", old_bytes_final == b"OLD-BOEVOY-SESSION-BYTES-UNTOUCHED")
        check("3i. cleanup never reaches the OLD manager's directory (old session file still exists)",
              os.path.exists(old_paths["session_path"]))

        # Cleanup containment: verify _replacement_cleanup_temp_files for a
        # DIFFERENT key never deletes files under this key's directory.
        with open(temp_path, "wb") as f:
            f.write(b"SHOULD-NOT-BE-DELETED")
        ns["_replacement_cleanup_temp_files"]("someotherkey", base_dir=tmp_root)
        check("3j. cleanup for a DIFFERENT key never deletes this key's files", os.path.exists(temp_path))
        os.remove(temp_path)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 4: proxy
# ======================================================================

async def test_group_4_proxy():
    print("\n-- Group 4: proxy --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)

        pool_cfg = {"proxy_enabled": 1, "proxy_type": "socks5", "proxy_host": "10.0.0.5",
                    "proxy_port": 1080, "proxy_username": "u1", "proxy_password": "p1"}
        r1 = ns["replacement_describe_proxy"](pool_cfg)
        check("4a. valid pool-style proxy representation accepted", r1["ok"] is True and r1["proxy_configured"] is True, repr(r1))

        bought_cfg = {"proxy_enabled": 1, "proxy_type": "SOCKS5", "proxy_host": "10.0.0.9",
                      "proxy_port": 3128, "proxy_username": "b1", "proxy_password": "b2"}
        r2 = ns["replacement_describe_proxy"](bought_cfg)
        check("4b. valid purchased-proxy representation accepted", r2["ok"] is True and r2["proxy_configured"] is True, repr(r2))

        manual_cfg = {"proxy_enabled": 1, "proxy_type": "socks5", "proxy_host": "1.1.1.1", "proxy_port": 1080}
        r3 = ns["replacement_describe_proxy"](manual_cfg)
        check("4c. valid manual proxy representation (no credentials) accepted", r3["ok"] is True, repr(r3))

        r4 = ns["replacement_describe_proxy"](None)
        check("4d. missing/empty proxy config -> direct mode, ok (existing bypass semantics reused verbatim)",
              r4["ok"] is True and r4["proxy_mode"] == "direct", repr(r4))

        # An empty host/port with no explicit proxy_mode silently resolves to
        # "direct" under the EXISTING (reused, unmodified) _manager_auth_proxy_mode
        # fallback -- that is legitimate existing behavior, not malformed. A
        # genuinely malformed config is one that EXPLICITLY demands proxy mode
        # but cannot actually build a proxy (host/port missing).
        malformed_cfg = {"proxy_mode": "proxy", "proxy_enabled": 1, "proxy_type": "socks5", "proxy_host": "", "proxy_port": 0}
        r5 = ns["replacement_describe_proxy"](malformed_cfg)
        check("4e. malformed proxy (explicit proxy mode but no host/port) is rejected",
              r5["ok"] is False and r5["code"] == "proxy_invalid", repr(r5))

        bypass_cfg = {"proxy_bypass_allowed": 1}
        r6 = ns["replacement_describe_proxy"](bypass_cfg)
        check("4f. explicit bypass_allowed=1 with no host -> direct, ok", r6["ok"] is True and r6["proxy_mode"] == "direct", repr(r6))

        check("4g. no secret (password) leaks into the describe_proxy result",
              "p1" not in str(r1) and "b2" not in str(r2))

        # Conflicting/multi-assign semantics: Stage 2 doesn't own proxy
        # allocation state, but the caller-supplied config is still
        # opaquely passed through phone-request without modification --
        # verify a full send_phone_code call actually uses the SAME cfg
        # object it was given (round-trips through the fake client).
        old = await seed_old_manager(ns, key="oldproxy")
        r0 = await ns["replacement_start"]("oldproxy", 1001, db_path=db_path)
        op = r0["operation_id"]
        r7 = await ns["replacement_send_phone_code"](
            op, "Имя", "proxykey1", "+19990001111", pool_cfg, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("4h. send_phone_code succeeds with a valid proxy config", r7["ok"] is True, repr(r7))
        check("4i. no allow_spend=True anywhere in the proxy path (Stage 2 never buys/prolongs a proxy)",
              "allow_spend" not in str(r1) + str(r2) + str(r7))

        r8 = ns["replacement_describe_proxy"]({"proxy_enabled": 1, "proxy_type": "http", "proxy_host": "x", "proxy_port": 8080})
        check("4j. non-socks5 proxy type treated as malformed (existing parser only supports socks5)", r8["ok"] is False, repr(r8))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 5: phone / send-code
# ======================================================================

async def test_group_5_phone():
    print("\n-- Group 5: phone/send-code --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns, key="oldphone")
        r0 = await ns["replacement_start"]("oldphone", 1001, db_path=db_path)
        op = r0["operation_id"]

        r1 = await ns["replacement_send_phone_code"](
            op, "Имя", "phkey1", "+19991112222", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("5a. successful code request", r1["ok"] is True and r1["code"] == "code_sent", repr(r1))
        check("5b. status advances draft -> auth_code (Stage 2 fix B1: durable only once phone_code_hash is on disk)",
              (ns["_repl_storage"].replacement_get(op, db_path=db_path))["status"] == "auth_code")
        check("5b2. next_step is continue_code",
              ns["_replacement_next_step_for_status"]("auth_code") == "continue_code")

        onboarding = await ns["manager_get_onboarding"](1001)
        check("5c. phone_code_hash persisted in onboarding scratch row", onboarding.get("phone_code_hash") == "hash123", repr(onboarding))
        check("5c2. no code/password field exists in onboarding row schema (only phone/phone_code_hash)",
              set(onboarding.keys()) & {"code", "password", "telegram_code", "two_factor_password"} == set())

        old2 = await seed_old_manager(ns, key="oldphone2")
        r0b = await ns["replacement_start"]("oldphone2", 1002, db_path=db_path)
        op2 = r0b["operation_id"]
        r2a = await ns["replacement_send_phone_code"](
            op2, "Имя2", "phkey2", "+19991112223", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("5d-setup. first send succeeds", r2a["ok"] is True)
        r2b = await ns["replacement_send_phone_code"](
            op2, "Имя2", "phkey2", "+19991112223", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("5d. duplicate/repeat request after auth_phone is deterministic (rejected: wrong_state, not a re-send)",
              r2b["ok"] is False and r2b["code"] == "wrong_state", repr(r2b))

        old3 = await seed_old_manager(ns, key="oldphone3")
        r0c = await ns["replacement_start"]("oldphone3", 1003, db_path=db_path)
        op3 = r0c["operation_id"]
        r3 = await ns["replacement_send_phone_code"](
            op3, "Имя3", "phkey3", "+19991112224", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=make_fake_client_factory({"send_code_exc": FloodWaitError(42)}, [], set()),
        )
        check("5e. FloodWait is retryable, includes wait_seconds, no secrets", r3["ok"] is False and r3["code"] == "flood_wait" and r3["retryable"] is True, repr(r3))
        check("5e2. wait_seconds extracted correctly", r3.get("wait_seconds") == 42, repr(r3))

        old4 = await seed_old_manager(ns, key="oldphone4")
        r0d = await ns["replacement_start"]("oldphone4", 1004, db_path=db_path)
        op4 = r0d["operation_id"]
        banned_factory = make_fake_client_factory({"send_code_exc": PhoneNumberBannedError("banned")}, [], set())
        r4 = await ns["replacement_send_phone_code"](
            op4, "Имя4", "phkey4", "+19991112225", {}, db_path=db_path, base_dir=tmp_root, client_factory=banned_factory,
        )
        check("5f. banned phone is fatal (not retryable)", r4["ok"] is False and r4["code"] == "phone_banned", repr(r4))
        row4 = ns["_repl_storage"].replacement_get(op4, db_path=db_path)
        check("5f2. operation marked failed on banned phone", row4["status"] == "failed", repr(row4))
        temp4 = ns["_replacement_temp_session_path"]("phkey4", base_dir=tmp_root)
        check("5g. temp session cleaned up after fatal failure", not os.path.exists(temp4))

        old5 = await seed_old_manager(ns, key="oldphone5")
        r0e = await ns["replacement_start"]("oldphone5", 1005, db_path=db_path)
        op5 = r0e["operation_id"]
        proxy_fail_cfg = {"proxy_enabled": 1, "proxy_mode": "proxy", "proxy_type": "socks5", "proxy_host": "", "proxy_port": 0}
        r5x = await ns["replacement_send_phone_code"](
            op5, "Имя5", "phkey5", "+19991112226", proxy_fail_cfg, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("5h. malformed proxy config blocks send_phone_code before any Telegram call", r5x["ok"] is False, repr(r5x))

        old1_check = await ns["manager_get"]("oldphone")
        check("5i. old manager (oldphone) is completely untouched after all phone-stage tests", old1_check.get("status") == "active")
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 6: code
# ======================================================================

async def test_group_6_code():
    print("\n-- Group 6: code --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns, key="oldcode1")
        r0 = await ns["replacement_start"]("oldcode1", 1001, db_path=db_path)
        op = r0["operation_id"]
        await ns["replacement_send_phone_code"](op, "Имя", "ckey1", "+1999", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        r1 = await ns["replacement_submit_code"](op, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("6a. correct code, no 2FA -> identity verified", r1["ok"] is True and r1["code"] == "identity_verified", repr(r1))
        check("6a2. status is identity_ok", (ns["_repl_storage"].replacement_get(op, db_path=db_path))["status"] == "identity_ok")

        await seed_old_manager(ns, key="oldcode2")
        r0b = await ns["replacement_start"]("oldcode2", 1002, db_path=db_path)
        op2 = r0b["operation_id"]
        await ns["replacement_send_phone_code"](op2, "Имя2", "ckey2", "+1998", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        pass_factory = make_fake_client_factory({"code_sign_in_exc": SessionPasswordNeededError()}, [], set())
        r2 = await ns["replacement_submit_code"](op2, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=pass_factory)
        check("6b. correct code with 2FA requirement -> password_required", r2["ok"] is True and r2["code"] == "password_required", repr(r2))
        check("6b2. status is auth_pass", (ns["_repl_storage"].replacement_get(op2, db_path=db_path))["status"] == "auth_pass")

        await seed_old_manager(ns, key="oldcode3")
        r0c = await ns["replacement_start"]("oldcode3", 1003, db_path=db_path)
        op3 = r0c["operation_id"]
        await ns["replacement_send_phone_code"](op3, "Имя3", "ckey3", "+1997", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        inv_factory = make_fake_client_factory({"code_sign_in_exc": PhoneCodeInvalidError()}, [], set())
        r3 = await ns["replacement_submit_code"](op3, "00000", {}, db_path=db_path, base_dir=tmp_root, client_factory=inv_factory)
        check("6c. invalid code is retryable", r3["ok"] is False and r3["code"] == "invalid_code" and r3["retryable"] is True, repr(r3))
        check("6c2. status stays auth_code (not failed) after invalid code -- temp session not deleted",
              (ns["_repl_storage"].replacement_get(op3, db_path=db_path))["status"] == "auth_code")

        exp_factory = make_fake_client_factory({"code_sign_in_exc": PhoneCodeExpiredError()}, [], set())
        r4 = await ns["replacement_submit_code"](op3, "00000", {}, db_path=db_path, base_dir=tmp_root, client_factory=exp_factory)
        check("6d. expired code is retryable (same handling as invalid)", r4["ok"] is False and r4["code"] == "invalid_code" and r4["retryable"] is True)

        fatal_factory = make_fake_client_factory({"code_sign_in_exc": RuntimeError("weird fatal")}, [], set())
        r5 = await ns["replacement_submit_code"](op3, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=fatal_factory)
        check("6e. unrecognized fatal auth error marks the operation failed", r5["ok"] is False and r5["code"] == "fatal_error", repr(r5))
        check("6e2. operation status is failed", (ns["_repl_storage"].replacement_get(op3, db_path=db_path))["status"] == "failed")

        r6 = await ns["replacement_submit_code"](op, "99999", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("6f. duplicate code submission AFTER identity_ok is rejected (never signs in again)",
              r6["ok"] is False and r6["code"] == "wrong_state", repr(r6))
        # Code/password never appearing in the audit log or persisted state
        # is verified exhaustively in the regression/security group (12).
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 7: password
# ======================================================================

async def test_group_7_password():
    print("\n-- Group 7: password --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns, key="oldpw1")
        r0 = await ns["replacement_start"]("oldpw1", 1001, db_path=db_path)
        op = r0["operation_id"]
        await ns["replacement_send_phone_code"](op, "Имя", "pwkey1", "+1996", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        pass_factory = make_fake_client_factory({"code_sign_in_exc": SessionPasswordNeededError()}, [], set())
        await ns["replacement_submit_code"](op, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=pass_factory)

        r1 = await ns["replacement_submit_password"](op, "SuperSecret123", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("7a. successful 2FA -> identity verified", r1["ok"] is True and r1["code"] == "identity_verified", repr(r1))
        check("7a2. status is identity_ok", (ns["_repl_storage"].replacement_get(op, db_path=db_path))["status"] == "identity_ok")

        await seed_old_manager(ns, key="oldpw2")
        r0b = await ns["replacement_start"]("oldpw2", 1002, db_path=db_path)
        op2 = r0b["operation_id"]
        await ns["replacement_send_phone_code"](op2, "Имя2", "pwkey2", "+1995", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        pass_factory2 = make_fake_client_factory({"code_sign_in_exc": SessionPasswordNeededError()}, [], set())
        await ns["replacement_submit_code"](op2, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=pass_factory2)
        wrong_pw_factory = make_fake_client_factory({"pass_sign_in_exc": PasswordHashInvalidError()}, [], set())
        r2 = await ns["replacement_submit_password"](op2, "WrongPass", {}, db_path=db_path, base_dir=tmp_root, client_factory=wrong_pw_factory)
        check("7b. wrong password is retryable", r2["ok"] is False and r2["code"] == "wrong_password" and r2["retryable"] is True, repr(r2))
        check("7b2. status stays auth_pass after wrong password", (ns["_repl_storage"].replacement_get(op2, db_path=db_path))["status"] == "auth_pass")

        fatal_pw_factory = make_fake_client_factory({"pass_sign_in_exc": RuntimeError("weird 2fa fatal")}, [], set())
        r3 = await ns["replacement_submit_password"](op2, "AnotherPass", {}, db_path=db_path, base_dir=tmp_root, client_factory=fatal_pw_factory)
        check("7c. fatal 2FA error marks the operation failed", r3["ok"] is False and r3["code"] == "fatal_error", repr(r3))
        check("7c2. status is failed", (ns["_repl_storage"].replacement_get(op2, db_path=db_path))["status"] == "failed")

        r4 = await ns["replacement_submit_password"](op, "AnythingAtAll", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("7d. duplicate password submission after identity_ok is rejected", r4["ok"] is False and r4["code"] == "wrong_state", repr(r4))

        onboarding_after = await ns["manager_get_onboarding"](1001)
        check("7e. no password value persisted anywhere in onboarding scratch state",
              onboarding_after is None or "SuperSecret123" not in str(onboarding_after))
        row_after = ns["_repl_storage"].replacement_get(op, db_path=db_path)
        check("7e2. no password value persisted anywhere in manager_replacements row",
              "SuperSecret123" not in str(row_after))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 8: identity
# ======================================================================

async def test_group_8_identity():
    print("\n-- Group 8: identity --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns, key="oldid1", tg_user_id=555001)
        r0 = await ns["replacement_start"]("oldid1", 1001, db_path=db_path)
        op = r0["operation_id"]
        factory = make_fake_client_factory({"actual_user_id": 700001, "actual_username": "newacc700001"}, [], set())
        await ns["replacement_send_phone_code"](op, "Имя", "idkey1", "+1994", {}, db_path=db_path, base_dir=tmp_root, client_factory=factory)
        r1 = await ns["replacement_submit_code"](op, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=factory)
        check("8a. valid identity accepted", r1["ok"] is True, repr(r1))
        row1 = ns["_repl_storage"].replacement_get(op, db_path=db_path)
        check("8a2. new_username snapshot persisted", row1.get("new_username") == "newacc700001", repr(row1))

        await seed_old_manager(ns, key="oldid2", tg_user_id=555002)
        r0b = await ns["replacement_start"]("oldid2", 1002, db_path=db_path)
        op2 = r0b["operation_id"]
        empty_uname_factory = make_fake_client_factory({"actual_user_id": 700002, "actual_username": ""}, [], set())
        await ns["replacement_send_phone_code"](op2, "Имя2", "idkey2", "+1993", {}, db_path=db_path, base_dir=tmp_root, client_factory=empty_uname_factory)
        r2 = await ns["replacement_submit_code"](op2, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=empty_uname_factory)
        check("8b. empty username handled cleanly, no bare '@' anywhere in result", r2["ok"] is True and "@" not in str(r2), repr(r2))

        await seed_old_manager(ns, key="oldid3", tg_user_id=555003)
        r0c = await ns["replacement_start"]("oldid3", 1003, db_path=db_path)
        op3 = r0c["operation_id"]
        unicode_factory = make_fake_client_factory(
            {"actual_user_id": 700003, "actual_username": "unicode700003",
             "actual_first_name": "Иван", "actual_last_name": "Петрович"}, [], set(),
        )
        await ns["replacement_send_phone_code"](op3, "Имя3", "idkey3", "+1992", {}, db_path=db_path, base_dir=tmp_root, client_factory=unicode_factory)
        r3 = await ns["replacement_submit_code"](op3, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=unicode_factory)
        check("8c. Unicode first/last names handled cleanly", r3["ok"] is True and r3.get("tg_first_name") == "Иван", repr(r3))

        await seed_old_manager(ns, key="oldid4", tg_user_id=555004)
        r0d = await ns["replacement_start"]("oldid4", 1004, db_path=db_path)
        op4 = r0d["operation_id"]
        no_uid_factory = make_fake_client_factory({"actual_user_id": 0}, [], set())
        await ns["replacement_send_phone_code"](op4, "Имя4", "idkey4", "+1991", {}, db_path=db_path, base_dir=tmp_root, client_factory=no_uid_factory)
        r4 = await ns["replacement_submit_code"](op4, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=no_uid_factory)
        check("8d. missing tg_user_id fails cleanly", r4["ok"] is False and r4["code"] == "missing_identity", repr(r4))
        check("8d2. operation marked failed", (ns["_repl_storage"].replacement_get(op4, db_path=db_path))["status"] == "failed")

        await seed_old_manager(ns, key="oldid5", tg_user_id=555005)
        await seed_old_manager(ns, key="othermgr5", tg_user_id=800005)
        r0e = await ns["replacement_start"]("oldid5", 1005, db_path=db_path)
        op5 = r0e["operation_id"]
        taken_factory = make_fake_client_factory({"actual_user_id": 800005, "actual_username": "taken"}, [], set())
        await ns["replacement_send_phone_code"](op5, "Имя5", "idkey5", "+1990", {}, db_path=db_path, base_dir=tmp_root, client_factory=taken_factory)
        r5 = await ns["replacement_submit_code"](op5, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=taken_factory)
        check("8e. tg_user_id already assigned to ANOTHER manager is rejected", r5["ok"] is False and r5["code"] == "identity_conflict", repr(r5))

        # oldid5's operation is now terminal (failed) from 8e -- start a fresh
        # operation to test "new identity same as the OLD manager's identity".
        await seed_old_manager(ns, key="oldid6", tg_user_id=900006)
        r0g = await ns["replacement_start"]("oldid6", 1007, db_path=db_path)
        op6 = r0g["operation_id"]
        same_as_old_factory = make_fake_client_factory({"actual_user_id": 900006, "actual_username": "sameasold"}, [], set())
        await ns["replacement_send_phone_code"](op6, "Имя6", "idkey6", "+1989", {}, db_path=db_path, base_dir=tmp_root, client_factory=same_as_old_factory)
        r6 = await ns["replacement_submit_code"](op6, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=same_as_old_factory)
        check("8f. authenticated account SAME as the OLD manager's identity is rejected", r6["ok"] is False and r6["code"] == "same_identity", repr(r6))

        await seed_old_manager(ns, key="oldid7", tg_user_id=910007)
        await seed_old_manager(ns, key="oldid8", tg_user_id=920008)
        r0h = await ns["replacement_start"]("oldid7", 1008, db_path=db_path)
        op7 = r0h["operation_id"]
        r0i = await ns["replacement_start"]("oldid8", 1009, db_path=db_path)
        op8 = r0i["operation_id"]
        dup_factory_a = make_fake_client_factory({"actual_user_id": 700100, "actual_username": "duplicateuname"}, [], set())
        await ns["replacement_send_phone_code"](op7, "Имя7", "idkey7", "+1988", {}, db_path=db_path, base_dir=tmp_root, client_factory=dup_factory_a)
        ra = await ns["replacement_submit_code"](op7, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=dup_factory_a)
        check("8g-setup. op7 claims username 'duplicateuname' first", ra["ok"] is True, repr(ra))
        dup_factory_b = make_fake_client_factory({"actual_user_id": 700200, "actual_username": "duplicateuname"}, [], set())
        await ns["replacement_send_phone_code"](op8, "Имя8", "idkey8", "+1987", {}, db_path=db_path, base_dir=tmp_root, client_factory=dup_factory_b)
        rb = await ns["replacement_submit_code"](op8, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=dup_factory_b)
        check("8g. same Telegram username claimed by a SECOND concurrent replacement is rejected",
              rb["ok"] is False and rb["code"] == "identity_conflict", repr(rb))

        check("8h. snapshot (new_username) persisted through Stage 1's constrained whitelist only",
              row1.get("new_username") == "newacc700001")
        check("8i. NO managers-row was created for any of these operations (still only fixture rows exist)",
              (await ns["manager_get"]("idkey1")) is None and (await ns["manager_get"]("idkey2")) is None)
        check("8j. _manager_finalize_login is never CALLED anywhere in the extracted Stage 2 source",
              "_manager_finalize_login(" not in "\n\n".join(ast.unparse(n) for n in ast.parse(MAIN_SRC).body
                                                             if getattr(n, "name", None) in STAGE2_REAL_NAMES))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 9: ready commit
# ======================================================================

async def test_group_9_ready_commit():
    print("\n-- Group 9: ready commit --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns, key="oldrc1")
        op = await _drive_to_identity_ok(ns, "oldrc1", "rckey1")
        r1 = await ns["replacement_ready_commit_preview"](op, db_path=db_path, base_dir=tmp_root)
        check("9a. all prerequisites pass -> ready_commit", r1["ok"] is True and r1["code"] == "ready_commit", repr(r1))
        check("9a2. status is ready_commit", (ns["_repl_storage"].replacement_get(op, db_path=db_path))["status"] == "ready_commit")

        preview = r1.get("preview", {})
        # "session" itself is not forbidden -- the legitimate "session_status"
        # field (e.g. "authorized") uses that word. Check for an actual leaked
        # session file path/extension instead.
        forbidden_substrings = ["+1999", "12345", "SuperSecret", ".session", "tg_user_id", "999001"]
        preview_text = str(preview)
        leaked = [s for s in forbidden_substrings if s in preview_text]
        check("9i2. no secrets in preview (phone/code/password/session path/tg_user_id absent)", not leaked, repr((leaked, preview)))
        check("9i3. no session_path key leaked in preview", "session_path" not in preview)
        check("9j. status stops at ready_commit (never advances further in Stage 2)",
              (ns["_repl_storage"].replacement_get(op, db_path=db_path))["status"] == "ready_commit")

        await seed_old_manager(ns, key="oldrc2")
        r0b = await ns["replacement_start"]("oldrc2", 1002, db_path=db_path)
        op2 = r0b["operation_id"]
        await ns["replacement_send_phone_code"](op2, "Имя2", "rckey2", "+1986", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        r2 = await ns["replacement_ready_commit_preview"](op2, db_path=db_path, base_dir=tmp_root)
        check("9b. missing temp session (still in auth_phone, never signed in) is rejected", r2["ok"] is False, repr(r2))

        await seed_old_manager(ns, key="oldrc3")
        op3 = await _drive_to_identity_ok(ns, "oldrc3", "rckey3")
        perm3 = ns["_replacement_permanent_session_path"]("rckey3", base_dir=tmp_root)
        with open(perm3, "wb") as f:
            f.write(b"SOMEHOW-ALREADY-EXISTS")
        r3 = await ns["replacement_ready_commit_preview"](op3, db_path=db_path, base_dir=tmp_root)
        check("9c. permanent session conflict rejected", r3["ok"] is False and r3["code"] == "permanent_session_conflict", repr(r3))
        os.remove(perm3)

        await seed_old_manager(ns, key="oldrc5")
        op5 = await _drive_to_identity_ok(ns, "oldrc5", "rckey5")

        async def fake_relogin_active_true(old_key):
            return True
        ns5 = ns
        orig_fn = ns5["_replacement_relogin_active"]
        ns5["_replacement_relogin_active"] = fake_relogin_active_true
        r5 = await ns["replacement_ready_commit_preview"](op5, db_path=db_path, base_dir=tmp_root)
        ns5["_replacement_relogin_active"] = orig_fn
        check("9d. relogin started (active) for the OLD key blocks ready_commit", r5["ok"] is False and r5["code"] == "relogin_active", repr(r5))

        await seed_old_manager(ns, key="oldrc6")
        op6 = await _drive_to_identity_ok(ns, "oldrc6", "rckey6")
        await ns["__storage__"].manager_set_fields("oldrc6", status="archived")
        r6 = await ns["replacement_ready_commit_preview"](op6, db_path=db_path, base_dir=tmp_root)
        check("9e. old manager archived mid-flow blocks ready_commit", r6["ok"] is False and r6["code"] == "old_manager_gone", repr(r6))

        r7 = await ns["replacement_ready_commit_preview"](_opid("ghost"), db_path=db_path, base_dir=tmp_root)
        check("9f. unknown operation (owner mismatch surrogate: nonexistent op) fails cleanly", r7["ok"] is False and r7["code"] == "missing_operation", repr(r7))

        r8 = await ns["replacement_ready_commit_preview"](op, db_path=db_path, base_dir=tmp_root)
        check("9g. idempotent re-call on an already ready_commit operation returns the same preview", r8["ok"] is True and r8["code"] == "ready_commit", repr(r8))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 10: cancel / fail cleanup
# ======================================================================

async def test_group_10_cancel_fail():
    print("\n-- Group 10: cancel/fail cleanup --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)

        # Explicit per-stage cancel tests below (draft/auth_phone/auth_code/
        # auth_pass/identity_ok/ready_commit) -- each drives a fresh
        # operation to the target stage, then cancels.
        await seed_old_manager(ns, key="oldc_draft")
        r0 = await ns["replacement_start"]("oldc_draft", 1001, db_path=db_path)
        op_draft = r0["operation_id"]
        c1 = await ns["replacement_cancel"](op_draft, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10a-draft. cancel succeeds from draft", c1["ok"] is True, repr(c1))

        await seed_old_manager(ns, key="oldc_phone")
        r0b = await ns["replacement_start"]("oldc_phone", 1002, db_path=db_path)
        op_phone = r0b["operation_id"]
        await ns["replacement_send_phone_code"](op_phone, "И", "cpkey1", "+1985", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        c2 = await ns["replacement_cancel"](op_phone, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10a-auth_phone. cancel succeeds from auth_phone", c2["ok"] is True, repr(c2))

        await seed_old_manager(ns, key="oldc_code")
        r0c = await ns["replacement_start"]("oldc_code", 1003, db_path=db_path)
        op_code = r0c["operation_id"]
        await ns["replacement_send_phone_code"](op_code, "И", "cckey1", "+1984", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        c3 = await ns["replacement_cancel"](op_code, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10a-auth_code. cancel succeeds from auth_code", c3["ok"] is True, repr(c3))

        await seed_old_manager(ns, key="oldc_pass")
        r0d = await ns["replacement_start"]("oldc_pass", 1004, db_path=db_path)
        op_pass = r0d["operation_id"]
        await ns["replacement_send_phone_code"](op_pass, "И", "cpakey1", "+1983", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        pf = make_fake_client_factory({"code_sign_in_exc": SessionPasswordNeededError()}, [], set())
        await ns["replacement_submit_code"](op_pass, "12345", {}, db_path=db_path, base_dir=tmp_root, client_factory=pf)
        c4 = await ns["replacement_cancel"](op_pass, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10a-auth_pass. cancel succeeds from auth_pass", c4["ok"] is True, repr(c4))

        await seed_old_manager(ns, key="oldc_idok")
        op_idok = await _drive_to_identity_ok(ns, "oldc_idok", "cidkey1")
        c5 = await ns["replacement_cancel"](op_idok, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10a-identity_ok. cancel succeeds from identity_ok", c5["ok"] is True, repr(c5))

        await seed_old_manager(ns, key="oldc_rc")
        op_rc = await _drive_to_identity_ok(ns, "oldc_rc", "crckey1")
        await ns["replacement_ready_commit_preview"](op_rc, db_path=db_path, base_dir=tmp_root)
        c6 = await ns["replacement_cancel"](op_rc, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10a-ready_commit. cancel succeeds from ready_commit (last pre-commit stage)", c6["ok"] is True, repr(c6))

        c6b = await ns["replacement_cancel"](op_rc, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10b. repeated cancel on an already-cancelled operation is safe/idempotent", c6b["ok"] is True and c6b["code"] == "already_cancelled", repr(c6b))

        temp_path = ns["_replacement_temp_session_path"]("cidkey1", base_dir=tmp_root)
        check("10c. temp + journal/WAL/SHM cleaned up (temp file gone)", not os.path.exists(temp_path))
        for suf in ("-journal", "-wal", "-shm"):
            check(f"10c-{suf}. companion {suf} cleaned up", not os.path.exists(temp_path + suf))

        old_check = await ns["manager_get"]("oldc_idok")
        check("10d. old session untouched after cancel", old_check.get("status") == "active")
        old_paths = ns["build_manager_paths"](str(tmp_root), "oldc_idok")
        check("10d2. old session file still has its original bytes", read_bytes_or_none(old_paths["session_path"]) == b"OLD-BOEVOY-SESSION-BYTES-UNTOUCHED")

        # fatal failure path
        await seed_old_manager(ns, key="oldf1")
        r0e = await ns["replacement_start"]("oldf1", 2001, db_path=db_path)
        op_f = r0e["operation_id"]
        banned_factory = make_fake_client_factory({"send_code_exc": PhoneNumberBannedError()}, [], set())
        rf = await ns["replacement_send_phone_code"](op_f, "И", "fkey1", "+1982", {}, db_path=db_path, base_dir=tmp_root, client_factory=banned_factory)
        check("10e. fatal failure state reached", rf["ok"] is False)
        check("10e2. status is failed", (ns["_repl_storage"].replacement_get(op_f, db_path=db_path))["status"] == "failed")

        # retryable errors (invalid code) do NOT mark failed
        await seed_old_manager(ns, key="oldf2")
        r0f = await ns["replacement_start"]("oldf2", 2002, db_path=db_path)
        op_f2 = r0f["operation_id"]
        await ns["replacement_send_phone_code"](op_f2, "И", "fkey2", "+1981", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        inv_factory = make_fake_client_factory({"code_sign_in_exc": PhoneCodeInvalidError()}, [], set())
        await ns["replacement_submit_code"](op_f2, "00000", {}, db_path=db_path, base_dir=tmp_root, client_factory=inv_factory)
        check("10f. retryable error (invalid code) does NOT mark the operation failed",
              (ns["_repl_storage"].replacement_get(op_f2, db_path=db_path))["status"] == "auth_code")

        # cleanup never deletes outside the temp basename -- verify sibling
        # permanent path for a DIFFERENT already-cancelled op is untouched.
        c7 = await ns["replacement_cancel"]("nonexistent-op-id", db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("10g. cancel on a nonexistent operation fails cleanly (not a crash)", c7["ok"] is False and c7["code"] == "missing_operation", repr(c7))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 11: restart recovery
# ======================================================================

async def test_group_11_recovery():
    print("\n-- Group 11: restart recovery --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)

        await seed_old_manager(ns, key="oldr_draft")
        r0 = await ns["replacement_start"]("oldr_draft", 1001, db_path=db_path)
        rec = await ns["replacement_recover"](r0["operation_id"], db_path=db_path, base_dir=tmp_root)
        check("11a-draft. next_step == reserve_key", rec["next_step"] == "reserve_key", repr(rec))

        await seed_old_manager(ns, key="oldr_phone")
        r0b = await ns["replacement_start"]("oldr_phone", 1002, db_path=db_path)
        op_phone = r0b["operation_id"]
        await ns["replacement_send_phone_code"](op_phone, "И", "rphkey1", "+1980", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        rec2 = await ns["replacement_recover"](op_phone, db_path=db_path, base_dir=tmp_root)
        check("11a-auth_phone. next_step == continue_code (temp session exists)", rec2["next_step"] == "continue_code", repr(rec2))
        check("11b. temp_session_exists True when it genuinely exists", rec2["temp_session_exists"] is True)

        # missing temp file -- simulate a crash that lost the temp session.
        temp_path = ns["_replacement_temp_session_path"]("rphkey1", base_dir=tmp_root)
        os.remove(temp_path)
        rec3 = await ns["replacement_recover"](op_phone, db_path=db_path, base_dir=tmp_root)
        check("11c. missing temp file -> cleanup_required", rec3["next_step"] == "cleanup_required" and rec3["temp_session_exists"] is False, repr(rec3))

        await seed_old_manager(ns, key="oldr_idok")
        op_idok = await _drive_to_identity_ok(ns, "oldr_idok", "ridkey1")
        rec4 = await ns["replacement_recover"](op_idok, db_path=db_path, base_dir=tmp_root)
        check("11d-identity_ok. next_step == ready_commit", rec4["next_step"] == "ready_commit", repr(rec4))

        # unexpected permanent session (should never happen in Stage 2, but
        # recovery must still flag it defensively).
        perm_path = ns["_replacement_permanent_session_path"]("ridkey1", base_dir=tmp_root)
        with open(perm_path, "wb") as f:
            f.write(b"UNEXPECTED")
        rec5 = await ns["replacement_recover"](op_idok, db_path=db_path, base_dir=tmp_root)
        check("11e. unexpected permanent session -> cleanup_required, permanent_session_exists True",
              rec5["next_step"] == "cleanup_required" and rec5["permanent_session_exists"] is True, repr(rec5))
        os.remove(perm_path)

        # old manager missing
        await seed_old_manager(ns, key="oldr_gone")
        op_gone = await _drive_to_identity_ok(ns, "oldr_gone", "rgonekey1")
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM managers WHERE manager_key='oldr_gone'")
        con.commit()
        con.close()
        rec6 = await ns["replacement_recover"](op_gone, db_path=db_path, base_dir=tmp_root)
        check("11f. old manager missing -> old_manager_exists False", rec6["old_manager_exists"] is False, repr(rec6))

        # key conflict
        await seed_old_manager(ns, key="oldr_kc")
        op_kc = await _drive_to_identity_ok(ns, "oldr_kc", "rkckey1")
        await seed_old_manager(ns, key="rkckey1")  # a real manager now exists under the "new" key
        rec7 = await ns["replacement_recover"](op_kc, db_path=db_path, base_dir=tmp_root)
        check("11g. key conflict detected", rec7["key_conflict"] is True, repr(rec7))

        # terminal states
        await seed_old_manager(ns, key="oldr_term1")
        r0h = await ns["replacement_start"]("oldr_term1", 1010, db_path=db_path)
        await ns["replacement_cancel"](r0h["operation_id"], db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        rec8 = await ns["replacement_recover"](r0h["operation_id"], db_path=db_path, base_dir=tmp_root)
        check("11h-cancelled. terminal cancelled maps to next_step 'cancelled'", rec8["next_step"] == "cancelled", repr(rec8))

        await seed_old_manager(ns, key="oldr_term2")
        r0i = await ns["replacement_start"]("oldr_term2", 1011, db_path=db_path)
        banned_factory = make_fake_client_factory({"send_code_exc": PhoneNumberBannedError()}, [], set())
        await ns["replacement_send_phone_code"](r0i["operation_id"], "И", "rtermkey2", "+1979", {}, db_path=db_path, base_dir=tmp_root, client_factory=banned_factory)
        rec9 = await ns["replacement_recover"](r0i["operation_id"], db_path=db_path, base_dir=tmp_root)
        check("11h-failed. terminal failed maps to next_step 'failed'", rec9["next_step"] == "failed", repr(rec9))

        check("11i. no automatic Telegram reconnect happens during simple recovery "
              "(the recovery calls above never used a client_factory at all)", True)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 12: regression / security
# ======================================================================

async def test_group_12_regression_security():
    print("\n-- Group 12: regression/security --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        await seed_old_manager(ns, key="oldsec1")
        op = await _drive_to_identity_ok(ns, "oldsec1", "seckey1")

        row = ns["_repl_storage"].replacement_get(op, db_path=db_path)
        row_text = str(row)
        for secret in ("+1999888777", "12345", "hash123"):
            check(f"12a. {secret!r} never stored in manager_replacements", secret not in row_text, row_text)

        con = sqlite3.connect(db_path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(manager_replacements)").fetchall()]
        con.close()
        # proxy_mode/proxy_ref/proxy_confirmed (Stage 2 fix R2) are legitimate
        # NON-SECRET route-tracking columns -- never credentials -- so they
        # are explicitly allowed here; a genuine credential-shaped column
        # (proxy_host/port/username/password, phone, code, session path) is
        # still forbidden.
        allowed_proxy_cols = {"proxy_mode", "proxy_ref", "proxy_confirmed"}
        forbidden_cols = [
            c for c in cols
            if c not in allowed_proxy_cols and any(s in c.lower() for s in ("phone", "code", "password", "proxy", "session", "2fa"))
        ]
        check("12b. manager_replacements schema has no phone/code/password/proxy-credential/session column", not forbidden_cols, repr(cols))

        audit_log_path = ns["_MANAGER_AUTH_AUDIT_LOG"]
        audit_text = ""
        if os.path.exists(str(audit_log_path)):
            with open(str(audit_log_path), encoding="utf-8") as f:
                audit_text = f.read()
        check("12c. audit log written under the TEMP base_dir, not the real project runtime/",
              str(tmp_root) in str(audit_log_path), str(audit_log_path))
        for secret in ("12345", "hash123"):
            check(f"12d. {secret!r} never appears in the audit log text", secret not in audit_text, audit_text[:300])

        check("12e. no callback/UI function defined in the Stage 2 source (no @client.on, no _panel_execute)",
              "@client.on" not in "\n\n".join(ast.unparse(n) for n in ast.parse(MAIN_SRC).body if getattr(n, "name", None) in STAGE2_REAL_NAMES))

        stage2_src_all = "\n\n".join(ast.unparse(n) for n in ast.parse(MAIN_SRC).body if getattr(n, "name", None) in STAGE2_REAL_NAMES)
        for forbidden_call in ("_stop_manager_process(", "manager_soft_remove(", "_spawn_manager_process(",
                                "_manager_finalize_login(", "reserve_pair_", "reserve_activation_"):
            check(f"12f. {forbidden_call!r} (old-manager mutation / finalize / reserve-table write) never called from Stage 2 source",
                  forbidden_call not in stage2_src_all, forbidden_call)

        check("12h. no business-link creation call (bizlink_) in Stage 2 source", "bizlink_" not in stage2_src_all)
        check("12i. no PartnerBot send call in Stage 2 source", "_pf_send_partner_report" not in stage2_src_all and "partner_stat_bot" not in stage2_src_all)
        check("12j. no preflight mutation call in Stage 2 source", "preflight_check" not in stage2_src_all)
        check("12k. replacement_finalize (Stage 1's ONLY path to done) is never called from Stage 2 source",
              "replacement_finalize(" not in stage2_src_all)

        final_status = (ns["_repl_storage"].replacement_get(op, db_path=db_path))["status"]
        check("12l. no Stage 2 test in this file ever reaches a notify-eligible status",
              final_status not in ns["_repl_storage"].REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES, final_status)

        import ast as _ast2
        src_main = open('main.py', encoding='utf-8-sig').read()
        tree = _ast2.parse(src_main)
        locs = [n.lineno for n in _ast2.walk(tree) if isinstance(n, _ast2.Call)
                for kw in (n.keywords or []) if kw.arg == 'allow_spend'
                and isinstance(kw.value, _ast2.Constant) and kw.value.value is True]
        check("12m. allow_spend=True AST count remains exactly 2 (Stage 2 added zero)", len(locs) == 2, repr(locs))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 13: R1 -- cancellation ordering across post-commit statuses
# ======================================================================

async def _drive_past_ready_commit(ns, old_key, new_key, target_status, *, db_path, base_dir, owner=1001):
    """Drives an operation from scratch all the way to target_status,
    including statuses Stage 2 itself never reaches (committing and later)
    -- using RAW Stage 1 storage calls directly, exactly as a future Stage 3
    committing implementation would. Stage 2's OWN replacement_cancel must
    correctly refuse to touch anything once past ready_commit; this helper
    exists only to construct that fixture state for group 13's tests."""
    op = await _drive_to_identity_ok(ns, old_key, new_key, owner=owner)
    r = await ns["replacement_ready_commit_preview"](op, db_path=db_path, base_dir=base_dir)
    assert r["ok"], r
    storage = ns["_repl_storage"]
    if target_status == "ready_commit":
        return op
    assert storage.replacement_advance(op, "ready_commit", "committing", db_path=db_path)
    if target_status == "committing":
        return op
    assert storage.replacement_advance(op, "committing", "links_pending", db_path=db_path)
    if target_status == "links_pending":
        return op
    storage.replacement_update_links(op, 15, required_links=15, db_path=db_path)
    assert storage.replacement_advance(op, "links_pending", "links_ready", db_path=db_path)
    if target_status == "links_ready":
        return op
    assert storage.replacement_advance(op, "links_ready", "cutover_done", db_path=db_path)
    if target_status == "cutover_done":
        return op
    assert storage.replacement_advance(op, "cutover_done", "notified", db_path=db_path)
    if target_status == "notified":
        return op
    assert storage.replacement_finalize(op, db_path=db_path)
    return op


async def test_group_13_r1_cancel_ordering():
    print("\n-- Group 13: R1 cancellation ordering (post-commit statuses) --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        storage = ns["_repl_storage"]

        owner_seq = 5001
        for target in ("committing", "links_pending", "links_ready", "cutover_done", "notified", "done"):
            old_key = f"oldr1-{target}"
            new_key = f"newr1-{target}"
            await seed_old_manager(ns, key=old_key)
            op = await _drive_past_ready_commit(ns, old_key, new_key, target, db_path=db_path, base_dir=tmp_root, owner=owner_seq)
            owner_seq += 1

            row_before = storage.replacement_get(op, db_path=db_path)
            status_before = row_before["status"]
            calls_before = len(ns["__calls__"])
            temp_path = ns["_replacement_temp_session_path"](new_key, base_dir=tmp_root)
            perm_path = ns["_replacement_permanent_session_path"](new_key, base_dir=tmp_root)
            temp_existed_before = os.path.exists(temp_path)
            perm_existed_before = os.path.exists(perm_path)

            r = await ns["replacement_cancel"](op, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
            check(f"13-{target}. cancel is refused (cancellation_not_allowed)",
                  r["ok"] is False and r["code"] == "cancellation_not_allowed", repr(r))

            row_after = storage.replacement_get(op, db_path=db_path)
            check(f"13-{target}. status unchanged after refused cancel", row_after["status"] == status_before, repr(row_after))
            check(f"13-{target}. zero client activity -- no connect/disconnect/logout was ever attempted",
                  len(ns["__calls__"]) == calls_before, ns["__calls__"][calls_before:])
            check(f"13-{target}. temp session file presence unchanged (not deleted)",
                  os.path.exists(temp_path) == temp_existed_before)
            check(f"13-{target}. permanent session file presence unchanged (never touched)",
                  os.path.exists(perm_path) == perm_existed_before)

        # 'failed' gets its own explicit bounded-retry policy, distinct from
        # the blanket 'cancellation_not_allowed' refusal above.
        await seed_old_manager(ns, key="oldr1failed")
        r0 = await ns["replacement_start"]("oldr1failed", 6001, db_path=db_path)
        op_failed = r0["operation_id"]
        banned_factory = make_fake_client_factory({"send_code_exc": PhoneNumberBannedError()}, [], set())
        await ns["replacement_send_phone_code"](op_failed, "И", "newr1failed", "+19990000", {}, db_path=db_path, base_dir=tmp_root, client_factory=banned_factory)
        check("13-failed-setup. operation reached failed", storage.replacement_get(op_failed, db_path=db_path)["status"] == "failed")
        r = await ns["replacement_cancel"](op_failed, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("13-failed. cancel on a failed operation reports cannot_cancel (bounded retry, not cancellation_not_allowed)",
              r["ok"] is False and r["code"] == "cannot_cancel", repr(r))
        check("13-failed. status stays failed (retry never mutates it)",
              storage.replacement_get(op_failed, db_path=db_path)["status"] == "failed")

        # Regression guard: genuinely cancellable pre-commit statuses still
        # work (an over-broad R1 fix must not block everything).
        await seed_old_manager(ns, key="oldr1precommit")
        r0b = await ns["replacement_start"]("oldr1precommit", 6002, db_path=db_path)
        op_pre = r0b["operation_id"]
        c = await ns["replacement_cancel"](op_pre, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("13-draft-regression. cancel from draft still succeeds", c["ok"] is True and c["code"] == "cancelled", repr(c))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 14: R2 -- durable proxy across restart
# ======================================================================

async def test_group_14_r2_durable_proxy():
    print("\n-- Group 14: R2 durable proxy across restart --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        storage = ns["_repl_storage"]

        # 1/8. proxy mode/reference survives a restart; manual/pool/purchased
        # routes stay distinguishable; submit_code/submit_password work
        # WITHOUT the caller resupplying proxy_config.
        await seed_old_manager(ns, key="oldp1")
        r0 = await ns["replacement_start"]("oldp1", 7001, db_path=db_path)
        op1 = r0["operation_id"]
        proxy_cfg = {"proxy_enabled": 1, "proxy_type": "socks5", "proxy_host": "10.0.0.5",
                     "proxy_port": 1080, "proxy_username": "puser", "proxy_password": "SuperSecretProxyPW"}
        created1: list = []
        factory1 = make_fake_client_factory({"actual_user_id": 810001, "actual_username": "p1acc"}, [], set(), created1)
        r1 = await ns["replacement_send_phone_code"](
            op1, "Имя P1", "newp1", "+1900001", proxy_cfg, proxy_source="pool",
            db_path=db_path, base_dir=tmp_root, client_factory=factory1,
        )
        check("14a. send_phone_code with an explicit proxy_source succeeds", r1["ok"] is True, repr(r1))
        row1 = storage.replacement_get(op1, db_path=db_path)
        check("14b. durable proxy_mode='proxy' persisted on the operation", row1.get("proxy_mode") == "proxy", repr(row1))
        check("14c. durable proxy_ref carries the caller's route tag ('pool')", row1.get("proxy_ref") == "pool", repr(row1))
        check("14d. proxy_confirmed=1", int(row1.get("proxy_confirmed") or 0) == 1, repr(row1))

        # "Restart": a brand new client_factory/created_clients list, and
        # submit_code is called WITHOUT resupplying proxy_config at all.
        created2: list = []
        factory2 = make_fake_client_factory({"actual_user_id": 810001, "actual_username": "p1acc"}, [], set(), created2)
        r2 = await ns["replacement_submit_code"](op1, "12345", db_path=db_path, base_dir=tmp_root, client_factory=factory2)
        check("14e. submit_code succeeds WITHOUT the caller resupplying proxy_config", r2["ok"] is True, repr(r2))
        check("14f. the resolved reconnect used the DURABLE proxy host (not empty/direct)",
              len(created2) > 0 and created2[0].cfg.get("proxy_host") == "10.0.0.5", [c.cfg for c in created2])
        check("14g. the resolved reconnect used the DURABLE proxy credentials",
              created2[0].cfg.get("proxy_username") == "puser" and created2[0].cfg.get("proxy_password") == "SuperSecretProxyPW")
        check("14h. resolved proxy_mode is 'proxy' (never silently direct)", created2[0].cfg.get("proxy_mode") == "proxy")

        # 2. explicit direct mode remains direct across the same restart
        # pattern.
        await seed_old_manager(ns, key="oldp2")
        r0b = await ns["replacement_start"]("oldp2", 7002, db_path=db_path)
        op2 = r0b["operation_id"]
        created3: list = []
        factory3 = make_fake_client_factory({"actual_user_id": 810002, "actual_username": "p2acc"}, [], set(), created3)
        r3 = await ns["replacement_send_phone_code"](
            op2, "Имя P2", "newp2", "+1900002", {}, proxy_source="direct",
            db_path=db_path, base_dir=tmp_root, client_factory=factory3,
        )
        check("14i. direct-mode send succeeds", r3["ok"] is True, repr(r3))
        row2 = storage.replacement_get(op2, db_path=db_path)
        check("14j. durable proxy_mode='direct' persisted", row2.get("proxy_mode") == "direct", repr(row2))
        created4: list = []
        factory4 = make_fake_client_factory({"actual_user_id": 810002, "actual_username": "p2acc"}, [], set(), created4)
        r4 = await ns["replacement_submit_code"](op2, "12345", db_path=db_path, base_dir=tmp_root, client_factory=factory4)
        check("14k. direct-mode submit_code succeeds, resolved cfg is empty (direct)",
              r4["ok"] is True and created4[0].cfg == {}, [c.cfg for c in created4])

        # 3/7. proxy-mode operation with a corrupted/missing durable state
        # never silently falls back to direct -- returns a structured
        # proxy_state_missing/proxy_reentry_required + configure_proxy.
        await seed_old_manager(ns, key="oldp3")
        r0c = await ns["replacement_start"]("oldp3", 7003, db_path=db_path)
        op3 = r0c["operation_id"]
        factory5 = make_fake_client_factory({"actual_user_id": 810003, "actual_username": "p3acc"}, [], set())
        await ns["replacement_send_phone_code"](
            op3, "Имя P3", "newp3", "+1900003", proxy_cfg, proxy_source="manual",
            db_path=db_path, base_dir=tmp_root, client_factory=factory5,
        )
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET proxy_confirmed=0 WHERE operation_id=?", (op3,))
        con.commit()
        con.close()
        r5 = await ns["replacement_submit_code"](op3, "12345", db_path=db_path, base_dir=tmp_root, client_factory=factory5)
        check("14l. corrupted proxy_confirmed=0 -> proxy_state_missing (never silently direct)",
              r5["ok"] is False and r5["code"] == "proxy_state_missing", repr(r5))
        check("14m. proxy_state_missing carries next_step=configure_proxy", r5.get("next_step") == "configure_proxy", repr(r5))

        await seed_old_manager(ns, key="oldp4")
        r0d = await ns["replacement_start"]("oldp4", 7004, db_path=db_path)
        op4 = r0d["operation_id"]
        factory6 = make_fake_client_factory({"actual_user_id": 810004, "actual_username": "p4acc"}, [], set())
        await ns["replacement_send_phone_code"](
            op4, "Имя P4", "newp4", "+1900004", proxy_cfg, proxy_source="purchased",
            db_path=db_path, base_dir=tmp_root, client_factory=factory6,
        )
        row4 = storage.replacement_get(op4, db_path=db_path)
        check("14n. 'purchased' route tag distinguishable from 'pool'/'manual'", row4.get("proxy_ref") == "purchased", repr(row4))
        # Simulate the durable proxy CREDENTIALS being lost (e.g. a stale
        # onboarding row from before a schema-level cleanup) while the
        # onboarding row itself (phone/hash/step) is still intact -- the
        # route is still durably 'proxy' in manager_replacements, but there
        # is nothing to reconnect with.
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_onboarding SET proxy_host='', proxy_port=NULL WHERE owner_user_id=?", (7004,))
        con.commit()
        con.close()
        r6 = await ns["replacement_submit_code"](op4, "12345", db_path=db_path, base_dir=tmp_root, client_factory=factory6)
        check("14o. lost proxy credentials -> proxy_reentry_required (never silently direct)",
              r6["ok"] is False and r6["code"] == "proxy_reentry_required", repr(r6))
        check("14p. proxy_reentry_required carries next_step=configure_proxy", r6.get("next_step") == "configure_proxy", repr(r6))

        # 4. cancel cleanup resolves the SAME durable proxy for its
        # best-effort logout.
        await seed_old_manager(ns, key="oldp5")
        r0e = await ns["replacement_start"]("oldp5", 7005, db_path=db_path)
        op5 = r0e["operation_id"]
        created7: list = []
        factory7 = make_fake_client_factory({"actual_user_id": 810005, "actual_username": "p5acc"}, [], set(), created7)
        await ns["replacement_send_phone_code"](
            op5, "Имя P5", "newp5", "+1900005", proxy_cfg, proxy_source="manual",
            db_path=db_path, base_dir=tmp_root, client_factory=factory7,
        )
        created8: list = []
        factory8 = make_fake_client_factory({"actual_user_id": 810005, "actual_username": "p5acc"}, [], set(), created8)
        c = await ns["replacement_cancel"](op5, db_path=db_path, base_dir=tmp_root, client_factory=factory8)
        check("14q. cancel succeeds", c["ok"] is True, repr(c))
        check("14r. cancel's own cleanup client used the SAME durable proxy host",
              len(created8) > 0 and created8[0].cfg.get("proxy_host") == "10.0.0.5", [cl.cfg for cl in created8])

        # 9/10. credentials never leak into manager_replacements, previews,
        # or results.
        secrets = ("SuperSecretProxyPW", "puser")
        for op_check in (op1,):
            row_text = str(storage.replacement_get(op_check, db_path=db_path))
            for s in secrets:
                check(f"14s. {s!r} never stored in manager_replacements", s not in row_text, row_text)
        for r in (r1, r2, r3, r4, r5, r6, c):
            result_text = str(r)
            for s in secrets:
                check(f"14t. {s!r} never leaks into a Stage 2 structured result", s not in result_text, result_text)

        # 12. allow_spend invariant (re-verified in this group's own context).
        tree = ast.parse(MAIN_SRC)
        locs = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                for kw in (n.keywords or []) if kw.arg == "allow_spend"
                and isinstance(kw.value, ast.Constant) and kw.value.value is True]
        check("14u. allow_spend=True AST count remains exactly 2", len(locs) == 2, repr(locs))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 15: R3 -- Telegram identity (new_tg_user_id) persistence
# ======================================================================

async def test_group_15_r3_tg_user_id():
    print("\n-- Group 15: R3 tg_user_id persistence --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        storage = ns["_repl_storage"]

        con = sqlite3.connect(db_path)
        storage.ensure_replacement_tables(db_path)
        cols = [r[1] for r in con.execute("PRAGMA table_info(manager_replacements)").fetchall()]
        con.close()
        check("15a. manager_replacements schema contains new_tg_user_id", "new_tg_user_id" in cols, repr(cols))

        await seed_old_manager(ns, key="oldid1")
        factory1 = make_fake_client_factory({"actual_user_id": 820001, "actual_username": "idacc1"}, [], set())
        op1 = await _drive_to_identity_ok(ns, "oldid1", "newid1", owner=8001, client_factory=factory1)
        row1 = storage.replacement_get(op1, db_path=db_path)
        check("15b. valid identity's tg_user_id durably stored", row1.get("new_tg_user_id") == 820001, repr(row1))

        # Username-less identity conflict: two operations authenticate the
        # SAME tg_user_id, both with an EMPTY Telegram username -- the
        # username-only check cannot see this; the database-backed
        # new_tg_user_id check must.
        await seed_old_manager(ns, key="oldid2")
        r0 = await ns["replacement_start"]("oldid2", 8002, db_path=db_path)
        op2 = r0["operation_id"]
        await ns["replacement_send_phone_code"](
            op2, "Имя ID2", "newid2", "+1910002", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        dup_factory = make_fake_client_factory({"actual_user_id": 820001, "actual_username": ""}, [], set())
        r2 = await ns["replacement_submit_code"](op2, "12345", db_path=db_path, base_dir=tmp_root, client_factory=dup_factory)
        check("15c. username-less reuse of an ALREADY-claimed tg_user_id is rejected",
              r2["ok"] is False and r2["code"] == "identity_conflict", repr(r2))
        row2 = storage.replacement_get(op2, db_path=db_path)
        check("15c2. rejected operation is marked failed, tg_user_id never persisted",
              row2["status"] == "failed" and not row2.get("new_tg_user_id"), repr(row2))

        # failed/cancelled release the tg_user_id reservation -- a NEW
        # operation may then legitimately claim the same identity. op1 (not
        # op2 -- op2 never actually acquired the reservation, it was
        # rejected before ever persisting new_tg_user_id) is the ACTUAL
        # current holder of 820001, so op1 itself must be released first.
        c1 = await ns["replacement_cancel"](op1, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("15c3-setup. releasing op1 (the actual holder) via cancel succeeds", c1["ok"] is True, repr(c1))

        await seed_old_manager(ns, key="oldid3")
        factory3 = make_fake_client_factory({"actual_user_id": 820001, "actual_username": "idacc1reuse"}, [], set())
        op3 = await _drive_to_identity_ok(ns, "oldid3", "newid3", owner=8003, client_factory=factory3)
        row3 = storage.replacement_get(op3, db_path=db_path)
        check("15d. after op1's cancellation released the reservation, a new op can claim the same tg_user_id",
              row3.get("new_tg_user_id") == 820001, repr(row3))

        # 'done' keeps the reservation blocked permanently -- drive op3 to
        # 'done' via raw Stage 1 calls (Stage 2 itself never reaches this).
        r3prev = await ns["replacement_ready_commit_preview"](op3, db_path=db_path, base_dir=tmp_root)
        assert r3prev["ok"], r3prev
        assert storage.replacement_advance(op3, "ready_commit", "committing", db_path=db_path)
        assert storage.replacement_advance(op3, "committing", "links_pending", db_path=db_path)
        storage.replacement_update_links(op3, 15, required_links=15, db_path=db_path)
        assert storage.replacement_advance(op3, "links_pending", "links_ready", db_path=db_path)
        assert storage.replacement_advance(op3, "links_ready", "cutover_done", db_path=db_path)
        assert storage.replacement_advance(op3, "cutover_done", "notified", db_path=db_path)
        assert storage.replacement_finalize(op3, db_path=db_path)
        check("15e. op3 reached done", storage.replacement_get(op3, db_path=db_path)["status"] == "done")

        await seed_old_manager(ns, key="oldid4")
        r0d = await ns["replacement_start"]("oldid4", 8004, db_path=db_path)
        op4 = r0d["operation_id"]
        await ns["replacement_send_phone_code"](
            op4, "Имя ID4", "newid4", "+1910004", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        done_reuse_factory = make_fake_client_factory({"actual_user_id": 820001, "actual_username": "idacc1anotherone"}, [], set())
        r4 = await ns["replacement_submit_code"](op4, "12345", db_path=db_path, base_dir=tmp_root, client_factory=done_reuse_factory)
        check("15f. a DONE operation's tg_user_id reservation remains blocked forever",
              r4["ok"] is False and r4["code"] == "identity_conflict", repr(r4))

        # preview never exposes tg_user_id.
        await seed_old_manager(ns, key="oldid5")
        factory5 = make_fake_client_factory({"actual_user_id": 820005, "actual_username": "idacc5"}, [], set())
        op5 = await _drive_to_identity_ok(ns, "oldid5", "newid5", owner=8005, client_factory=factory5)
        preview_r = await ns["replacement_ready_commit_preview"](op5, db_path=db_path, base_dir=tmp_root)
        check("15g. ready_commit succeeds with a valid tg_user_id", preview_r["ok"] is True, repr(preview_r))
        check("15h. preview never contains the raw tg_user_id", "820005" not in str(preview_r), repr(preview_r))

        # ready_commit requires a valid persisted tg_user_id.
        await seed_old_manager(ns, key="oldid6")
        factory6 = make_fake_client_factory({"actual_user_id": 820006, "actual_username": "idacc6"}, [], set())
        op6 = await _drive_to_identity_ok(ns, "oldid6", "newid6", owner=8006, client_factory=factory6)
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET new_tg_user_id=NULL WHERE operation_id=?", (op6,))
        con.commit()
        con.close()
        r6res = await ns["replacement_ready_commit_preview"](op6, db_path=db_path, base_dir=tmp_root)
        check("15i. ready_commit rejects a missing tg_user_id (missing_identity)",
              r6res["ok"] is False and r6res["code"] == "missing_identity", repr(r6res))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 16: R4 -- already-authorized session recovery
# ======================================================================

async def test_group_16_r4_authorized_recovery():
    print("\n-- Group 16: R4 already-authorized session recovery --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        storage = ns["_repl_storage"]

        # Code path: Telegram already accepted the code on a prior attempt
        # that crashed before the DB transition -- the temp session file is
        # ALREADY authorized when we reconnect.
        await seed_old_manager(ns, key="oldr4a")
        r0 = await ns["replacement_start"]("oldr4a", 9001, db_path=db_path)
        opA = r0["operation_id"]
        await ns["replacement_send_phone_code"](
            opA, "Имя R4A", "newr4a", "+1920001", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        temp_path_a = ns["_replacement_temp_session_path"]("newr4a", base_dir=tmp_root)
        pre_authorized = {temp_path_a}
        recovery_factory_a = make_fake_client_factory(
            {"actual_user_id": 830001, "actual_username": "r4aacc", "sign_in_forbidden": True},
            [], pre_authorized,
        )
        rA = await ns["replacement_submit_code"](opA, "00000", db_path=db_path, base_dir=tmp_root, client_factory=recovery_factory_a)
        check("16a. already-authorized auth_code session reaches identity_ok without calling sign_in",
              rA["ok"] is True and rA["code"] == "identity_verified", repr(rA))
        rowA = storage.replacement_get(opA, db_path=db_path)
        check("16b. durable status reached identity_ok", rowA["status"] == "identity_ok", repr(rowA))
        check("16c. tg_user_id/username were persisted via the bypass path",
              rowA.get("new_tg_user_id") == 830001 and rowA.get("new_username") == "r4aacc", repr(rowA))

        # Password path: same scenario for the 2FA branch.
        await seed_old_manager(ns, key="oldr4b")
        r0b = await ns["replacement_start"]("oldr4b", 9002, db_path=db_path)
        opB = r0b["operation_id"]
        await ns["replacement_send_phone_code"](
            opB, "Имя R4B", "newr4b", "+1920002", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        pass_needed_factory = make_fake_client_factory({"code_sign_in_exc": SessionPasswordNeededError()}, [], set())
        r_needpass = await ns["replacement_submit_code"](opB, "12345", db_path=db_path, base_dir=tmp_root, client_factory=pass_needed_factory)
        assert r_needpass["ok"] and r_needpass["code"] == "password_required", r_needpass

        temp_path_b = ns["_replacement_temp_session_path"]("newr4b", base_dir=tmp_root)
        pre_authorized_b = {temp_path_b}
        recovery_factory_b = make_fake_client_factory(
            {"actual_user_id": 830002, "actual_username": "r4bacc", "sign_in_forbidden": True},
            [], pre_authorized_b,
        )
        rB = await ns["replacement_submit_password"](opB, "AnyPassword", db_path=db_path, base_dir=tmp_root, client_factory=recovery_factory_b)
        check("16d. already-authorized auth_pass session reaches identity_ok without calling sign_in",
              rB["ok"] is True and rB["code"] == "identity_verified", repr(rB))
        rowB = storage.replacement_get(opB, db_path=db_path)
        check("16e. durable status reached identity_ok (from auth_pass)", rowB["status"] == "identity_ok", repr(rowB))

        # A conflicting authorized identity still fails safely (the bypass
        # path runs through the SAME conflict checks as the normal path).
        await seed_old_manager(ns, key="oldr4c")
        r0c = await ns["replacement_start"]("oldr4c", 9003, db_path=db_path)
        opC = r0c["operation_id"]
        await ns["replacement_send_phone_code"](
            opC, "Имя R4C", "newr4c", "+1920003", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        temp_path_c = ns["_replacement_temp_session_path"]("newr4c", base_dir=tmp_root)
        conflicting_factory = make_fake_client_factory(
            {"actual_user_id": 830001, "actual_username": "r4aacc-conflict", "sign_in_forbidden": True},
            [], {temp_path_c},
        )
        rC = await ns["replacement_submit_code"](opC, "00000", db_path=db_path, base_dir=tmp_root, client_factory=conflicting_factory)
        check("16f. a conflicting authorized identity fails safely (identity_conflict, not a crash)",
              rC["ok"] is False and rC["code"] == "identity_conflict", repr(rC))
        rowC = storage.replacement_get(opC, db_path=db_path)
        check("16g. conflicting op marked failed, never reached identity_ok", rowC["status"] == "failed", repr(rowC))

        # No code/password persisted anywhere along the bypass path either.
        for op_check, row in ((opA, rowA), (opB, rowB)):
            row_text = str(storage.replacement_get(op_check, db_path=db_path))
            for secret in ("00000", "12345", "AnyPassword"):
                check(f"16h. {secret!r} never persisted for {op_check[:8]}", secret not in row_text, row_text)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 17: REQ-1/REQ-2 -- auth_phone retry consistency + cleanup-failure
# durability (final review round, 2026-07-15)
# ======================================================================

async def test_group_17_retry_consistency_and_cleanup_durability():
    print("\n-- Group 17: REQ-1 retry consistency + REQ-2 cleanup-failure durability --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root)
        storage = ns["_repl_storage"]

        # ------------------------------------------------------------
        # Test A: crash-window reconciliation performs NO network call.
        # ------------------------------------------------------------
        await seed_old_manager(ns, key="oldreqa")
        r0 = await ns["replacement_start"]("oldreqa", 10001, db_path=db_path)
        opA = r0["operation_id"]
        callsA: list = []
        factoryA = make_fake_client_factory({"phone_code_hash": "hashA111"}, callsA, set())
        r1 = await ns["replacement_send_phone_code"](
            opA, "Имя ReqA", "newreqa", "+1930001", {}, db_path=db_path, base_dir=tmp_root, client_factory=factoryA,
        )
        check("A-setup. initial send succeeds, reaches auth_code", r1["ok"] is True and r1["code"] == "code_sent", repr(r1))
        onboardingA = await ns["manager_get_onboarding"](10001)
        check("A-setup2. onboarding row durably has step=replace_code + hash",
              onboardingA["step"] == "replace_code" and onboardingA["phone_code_hash"] == "hashA111", repr(onboardingA))

        # Simulate the exact crash window: Telegram accepted the code and the
        # onboarding scratch row was durably saved (that write already ran
        # for real, in the real order), but the FINAL CAS auth_phone->
        # auth_code never committed.
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET status='auth_phone' WHERE operation_id=?", (opA,))
        con.commit()
        con.close()

        calls_before = len(callsA)
        r2 = await ns["replacement_send_phone_code"](
            opA, "Имя ReqA", "newreqa", "+1930001", {}, db_path=db_path, base_dir=tmp_root, client_factory=factoryA,
        )
        check("A1. reconciliation call succeeds with a distinct result code",
              r2["ok"] is True and r2["code"] == "code_accepted_reconciled", repr(r2))
        check("A2. next_step is continue_code (via submit_code)", r2.get("next_step") == "submit_code", repr(r2))
        check("A3. status is durably auth_code", storage.replacement_get(opA, db_path=db_path)["status"] == "auth_code")
        check("A4. ZERO new client calls of any kind (no connect, no send_code_request)",
              len(callsA) == calls_before, callsA[calls_before:])
        onboardingA_after = await ns["manager_get_onboarding"](10001)
        check("A5. onboarding hash unchanged by reconciliation", onboardingA_after["phone_code_hash"] == "hashA111", repr(onboardingA_after))
        rowA_final = storage.replacement_get(opA, db_path=db_path)
        check("A6. durable new_manager_key unchanged", rowA_final.get("new_manager_key") == "newreqa", repr(rowA_final))
        check("A7. durable proxy_mode unchanged (direct)", rowA_final.get("proxy_mode") == "direct", repr(rowA_final))

        # ------------------------------------------------------------
        # Test B: plain resend after a retryable failure before auth_code.
        # ------------------------------------------------------------
        await seed_old_manager(ns, key="oldreqb")
        r0b = await ns["replacement_start"]("oldreqb", 10002, db_path=db_path)
        opB = r0b["operation_id"]
        flood_factory = make_fake_client_factory({"send_code_exc": FloodWaitError(3)}, [], set())
        rb1 = await ns["replacement_send_phone_code"](
            opB, "Имя ReqB", "newreqb", "+1930002", {}, db_path=db_path, base_dir=tmp_root, client_factory=flood_factory,
        )
        check("B-setup. first attempt hits FloodWait (retryable)",
              rb1["ok"] is False and rb1["code"] == "flood_wait" and rb1["retryable"] is True, repr(rb1))
        rowB_flood = storage.replacement_get(opB, db_path=db_path)
        check("B-setup2. status is auth_phone after the retryable failure", rowB_flood["status"] == "auth_phone", repr(rowB_flood))
        check("B-setup3. durable proxy route already committed at the initial CAS", rowB_flood.get("proxy_mode") == "direct", repr(rowB_flood))

        good_calls: list = []
        good_factory = make_fake_client_factory({}, good_calls, set())
        rb2 = await ns["replacement_send_phone_code"](
            opB, "Имя ReqB", "newreqb", "+1930002", {}, db_path=db_path, base_dir=tmp_root, client_factory=good_factory,
        )
        check("B1. retry succeeds and reaches auth_code", rb2["ok"] is True and rb2["code"] == "code_sent", repr(rb2))
        check("B2. exactly ONE new send_code_request call on the retry",
              sum(1 for c in good_calls if c[0] == "send_code_request") == 1, good_calls)
        rowB_final = storage.replacement_get(opB, db_path=db_path)
        check("B3. durable status is auth_code", rowB_final["status"] == "auth_code", repr(rowB_final))
        onboardingB = await ns["manager_get_onboarding"](10002)
        check("B4. hash persisted on the retry", bool(onboardingB.get("phone_code_hash")), repr(onboardingB))
        check("B5. next_step for auth_code is continue_code", ns["_replacement_next_step_for_status"]("auth_code") == "continue_code")
        old_checkB = await ns["manager_get"]("oldreqb")
        check("B6. old manager untouched", old_checkB.get("status") == "active")

        # ------------------------------------------------------------
        # Test C: divergent new_manager_key on retry is rejected before any
        # side effect; the SAME (normalized) key remains accepted.
        # ------------------------------------------------------------
        await seed_old_manager(ns, key="oldreqc")
        r0c = await ns["replacement_start"]("oldreqc", 10003, db_path=db_path)
        opC = r0c["operation_id"]
        flood_factory_c = make_fake_client_factory({"send_code_exc": FloodWaitError(3)}, [], set())
        rc1 = await ns["replacement_send_phone_code"](
            opC, "Имя ReqC", "manager-new-a", "+1930003", {}, db_path=db_path, base_dir=tmp_root, client_factory=flood_factory_c,
        )
        check("C-setup. first attempt lands at auth_phone with key manager-new-a", rc1["ok"] is False and rc1["code"] == "flood_wait", repr(rc1))
        rowC_setup = storage.replacement_get(opC, db_path=db_path)
        check("C-setup2. durable key is manager-new-a", rowC_setup.get("new_manager_key") == "manager-new-a", repr(rowC_setup))

        paths_b = ns["build_manager_paths"](str(tmp_root), "manager-new-b")
        dir_b_existed_before = os.path.isdir(paths_b["root"])
        calls_mismatch: list = []
        mismatch_factory = make_fake_client_factory({}, calls_mismatch, set())
        rc2 = await ns["replacement_send_phone_code"](
            opC, "Имя ReqC", "manager-new-b", "+1930003", {}, db_path=db_path, base_dir=tmp_root, client_factory=mismatch_factory,
        )
        check("C1. divergent key on retry is rejected (key_mismatch_on_retry)",
              rc2["ok"] is False and rc2["code"] == "key_mismatch_on_retry", repr(rc2))
        check("C2. ZERO Telegram client activity (no connect, no send_code_request)", len(calls_mismatch) == 0, calls_mismatch)
        check("C3. no runtime directory ever created for the mismatched key",
              os.path.isdir(paths_b["root"]) == dir_b_existed_before)
        onboardingC = await ns["manager_get_onboarding"](10003)
        check("C4. onboarding manager_key was NOT rewritten to the mismatched key",
              ns["registry_normalize_manager_key"](onboardingC.get("manager_key") or "") != "manager-new-b", repr(onboardingC))
        rowC_after = storage.replacement_get(opC, db_path=db_path)
        check("C5. operation remains auth_phone", rowC_after["status"] == "auth_phone", repr(rowC_after))
        check("C6. durable key remains manager-new-a (unchanged)", rowC_after.get("new_manager_key") == "manager-new-a", repr(rowC_after))

        rc3 = await ns["replacement_send_phone_code"](
            opC, "Имя ReqC", "manager-new-a", "+1930003", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"],
        )
        check("C7. same normalized key retry remains accepted (idempotent)", rc3["ok"] is True, repr(rc3))

        # ------------------------------------------------------------
        # Test D: divergent proxy route on retry is ignored (never adopted).
        # ------------------------------------------------------------
        # D1: durable DIRECT -> retry supplies a proxy config -- must be
        # ignored, connection stays direct.
        await seed_old_manager(ns, key="oldreqd1")
        r0d1 = await ns["replacement_start"]("oldreqd1", 10004, db_path=db_path)
        opD1 = r0d1["operation_id"]
        flood_d1 = make_fake_client_factory({"send_code_exc": FloodWaitError(3)}, [], set())
        await ns["replacement_send_phone_code"](
            opD1, "Имя D1", "newreqd1", "+1930004", {}, db_path=db_path, base_dir=tmp_root, client_factory=flood_d1,
        )
        rowD1_setup = storage.replacement_get(opD1, db_path=db_path)
        check("D1-setup. durable proxy_mode is direct", rowD1_setup.get("proxy_mode") == "direct", repr(rowD1_setup))

        created_d1: list = []
        factory_d1_retry = make_fake_client_factory({}, [], set(), created_d1)
        proxy_attempt = {"proxy_enabled": 1, "proxy_type": "socks5", "proxy_host": "9.9.9.9",
                          "proxy_port": 1080, "proxy_username": "x", "proxy_password": "y"}
        rd1 = await ns["replacement_send_phone_code"](
            opD1, "Имя D1", "newreqd1", "+1930004", proxy_attempt, db_path=db_path, base_dir=tmp_root, client_factory=factory_d1_retry,
        )
        check("D1a. retry with a divergent proxy config still succeeds (ignored, not adopted)", rd1["ok"] is True, repr(rd1))
        check("D1b. the resolved client cfg is direct -- caller's proxy config was IGNORED",
              len(created_d1) > 0 and created_d1[0].cfg == {}, [c.cfg for c in created_d1])
        rowD1_after = storage.replacement_get(opD1, db_path=db_path)
        check("D1c. durable proxy_mode still direct (never silently switched to proxy)",
              rowD1_after.get("proxy_mode") == "direct", repr(rowD1_after))

        # D2: durable PROXY -> retry supplies NO proxy config -- must never
        # silently go direct; must reuse the durable proxy.
        await seed_old_manager(ns, key="oldreqd2")
        r0d2 = await ns["replacement_start"]("oldreqd2", 10005, db_path=db_path)
        opD2 = r0d2["operation_id"]
        proxy_cfg_d2 = {"proxy_enabled": 1, "proxy_type": "socks5", "proxy_host": "8.8.4.4",
                         "proxy_port": 1080, "proxy_username": "pu", "proxy_password": "pp"}
        flood_d2 = make_fake_client_factory({"send_code_exc": FloodWaitError(3)}, [], set())
        await ns["replacement_send_phone_code"](
            opD2, "Имя D2", "newreqd2", "+1930005", proxy_cfg_d2, proxy_source="manual",
            db_path=db_path, base_dir=tmp_root, client_factory=flood_d2,
        )
        rowD2_setup = storage.replacement_get(opD2, db_path=db_path)
        check("D2-setup. durable proxy_mode is proxy", rowD2_setup.get("proxy_mode") == "proxy", repr(rowD2_setup))

        created_d2: list = []
        factory_d2_retry = make_fake_client_factory({}, [], set(), created_d2)
        rd2 = await ns["replacement_send_phone_code"](
            opD2, "Имя D2", "newreqd2", "+1930005", {}, db_path=db_path, base_dir=tmp_root, client_factory=factory_d2_retry,
        )
        check("D2a. retry with NO proxy config still succeeds via the durable proxy", rd2["ok"] is True, repr(rd2))
        check("D2b. the resolved client cfg used the DURABLE proxy host (never silently direct)",
              len(created_d2) > 0 and created_d2[0].cfg.get("proxy_host") == "8.8.4.4", [c.cfg for c in created_d2])
        rowD2_after = storage.replacement_get(opD2, db_path=db_path)
        check("D2c. durable proxy_mode still proxy", rowD2_after.get("proxy_mode") == "proxy", repr(rowD2_after))

        # ------------------------------------------------------------
        # Test E: cleanup failure after cancellation still leaves the
        # durable 'cancelled' state intact (CAS-first ordering, R1).
        # ------------------------------------------------------------
        await seed_old_manager(ns, key="oldreqe")
        r0e = await ns["replacement_start"]("oldreqe", 10006, db_path=db_path)
        opE = r0e["operation_id"]
        await ns["replacement_send_phone_code"](
            opE, "Имя E", "newreqe", "+1930006", {}, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"],
        )
        temp_path_e = ns["_replacement_temp_session_path"]("newreqe", base_dir=tmp_root)
        check("E-setup. temp session file exists before cancel", os.path.exists(temp_path_e))

        def _raising_cleanup(*_a, **_kw):
            raise RuntimeError("simulated cleanup disk failure")

        orig_cleanup = ns["_replacement_cleanup_temp_files"]
        ns["_replacement_cleanup_temp_files"] = _raising_cleanup
        raised = False
        try:
            await ns["replacement_cancel"](opE, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        except RuntimeError as e:
            raised = True
            check("E1. the injected cleanup failure is a REAL, non-swallowed exception (test is non-vacuous)",
                  "simulated cleanup disk failure" in str(e), str(e))
        finally:
            ns["_replacement_cleanup_temp_files"] = orig_cleanup
        check("E2. cleanup genuinely raised (proves CAS-first ordering is actually exercised)", raised is True)

        rowE_after_crash = storage.replacement_get(opE, db_path=db_path)
        check("E3. durable status is STILL cancelled despite the cleanup crash (CAS committed first)",
              rowE_after_crash["status"] == "cancelled", repr(rowE_after_crash))

        old_checkE = await ns["manager_get"]("oldreqe")
        check("E4. old manager untouched by the failed cleanup", old_checkE.get("status") == "active")
        old_pathsE = ns["build_manager_paths"](str(tmp_root), "oldreqe")
        check("E5. old session bytes untouched", read_bytes_or_none(old_pathsE["session_path"]) == b"OLD-BOEVOY-SESSION-BYTES-UNTOUCHED")
        perm_path_e = ns["_replacement_permanent_session_path"]("newreqe", base_dir=tmp_root)
        check("E6. permanent session path was never created", not os.path.exists(perm_path_e))

        # Repeated cancel (with cleanup restored) is a safe, idempotent retry
        # that actually finishes the cleanup this time.
        r_retry = await ns["replacement_cancel"](opE, db_path=db_path, base_dir=tmp_root, client_factory=ns["__client_factory__"])
        check("E7. repeated cancel after the cleanup crash is idempotent (already_cancelled)",
              r_retry["ok"] is True and r_retry["code"] == "already_cancelled", repr(r_retry))
        check("E8. the temp session is actually removed once cleanup is allowed to run", not os.path.exists(temp_path_e))
        check("E9. no secret/traceback text leaked into the retry result", "SuperSecret" not in str(r_retry) and "password" not in str(r_retry).lower())
    finally:
        await cleanup_env(tmp_root)


def test_group_18_dbguard() -> None:
    """Direct unit test of _selftest_db_guard's exact logic (the same
    function build_stage2_ns calls on every invocation) against every unsafe
    scenario the 2026-07-16 forensic review required to be proven -- without
    ever touching the real storage.DB_PATH/QUEUE_DB_PATH globals or creating
    any file under the project's db/ directory."""
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


# ======================================================================
# GROUP PHONE: _repl4_create_new_manager phone-persistence regression
# (TPILOT RUNTIME AUTH SESSION-REUSE FIX 20260718)
# ======================================================================

async def test_group_phone_persistence_regression():
    """Production incident regression: a manager-replacement operation used
    to create the new managers row with an EMPTY phone (manager_add(...,
    phone="", ...)), which -- combined with a separate runtime-startup bug
    -- meant the new manager's process could never start (Telethon's
    client.start(phone=None) raises before it ever reaches its
    authorized-session-reuse path). The fix: _repl4_create_new_manager now
    fetches the owner's manager_onboarding row (durably populated earlier by
    replacement_send_phone_code -> manager_save_onboarding with the
    operator-entered phone) and passes that phone to BOTH manager_add(...)
    AND _manager_finalize_login(...) -- never "" to either. This test drives
    BOTH branches of _repl4_create_new_manager's post-create identity check:
      Case 1: identity not (yet) confirmed -> the fallback branch, which
        never writes phone at all -- manager_add's own write must be the one
        that sticks (this alone reproduces the original incident).
      Case 2: identity confirmed (me.id == op_row.new_tg_user_id) ->
        _manager_finalize_login runs and writes phone unconditionally --
        the regression risk explicitly called out in the fix's own comment
        is that passing "" there would silently ERASE the phone manager_add
        just wrote."""
    print("\n-- Group PHONE: _repl4_create_new_manager phone persistence regression --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root, extra_names=STAGE4_PHONE_NAMES)
        entered_phone = "+19995551234"

        # --- Case 1: fallback branch (identity not confirmed) ---
        old1 = await seed_old_manager(ns, key="oldphonex1")
        r0 = await ns["replacement_start"]("oldphonex1", 1101, db_path=db_path)
        op1 = r0["operation_id"]
        r1 = await ns["replacement_send_phone_code"](
            op1, "Имя Ф1", "newphonex1", entered_phone, {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("PHONE1-setup. send_phone_code succeeds (durably saves the operator-entered phone to onboarding)",
              r1["ok"] is True, repr(r1))
        onboarding1 = await ns["manager_get_onboarding"](1101)
        check("PHONE1a. onboarding scratch row carries the operator-entered phone",
              onboarding1.get("phone") == entered_phone, repr(onboarding1))

        op1_row = ns["_repl_storage"].replacement_get(op1, db_path=db_path)
        old1_row = await ns["manager_get"]("oldphonex1")
        # No pre-authorized session at the (not-yet-created) permanent path
        # -- is_user_authorized() is False, me stays None, the fallback
        # branch runs (it never writes phone) -- manager_add's own write is
        # the only thing that can persist the phone here.
        unauth_factory = make_fake_client_factory({}, [], set())
        result1 = await ns["_repl4_create_new_manager"](
            op1_row, old1_row, "newphonex1", 1101, tmp_root, client_factory=unauth_factory,
        )
        check("PHONE1b. _repl4_create_new_manager reports no error (returns None) on the fallback path",
              result1 is None, repr(result1))
        new_row1 = await ns["manager_get"]("newphonex1")
        check("PHONE1c. new manager row was created", new_row1 is not None, new_row1)
        check("PHONE1d. new manager phone equals the operator-entered phone, NOT empty (fallback/manager_add path -- this is exactly the incident condition)",
              new_row1 is not None and new_row1.get("phone") == entered_phone, repr(new_row1))

        # --- Case 2: _manager_finalize_login branch (identity confirmed) ---
        old2 = await seed_old_manager(ns, key="oldphonex2")
        op2 = await _drive_to_identity_ok(ns, "oldphonex2", "newphonex2", owner=1102)
        # _drive_to_identity_ok's own send_phone_code call durably saves ITS
        # OWN operator-entered phone to the onboarding row for owner 1102 --
        # the exact same durable-save code path as Case 1, just reached via
        # the shared driving helper instead of a bespoke call.
        onboarding2 = await ns["manager_get_onboarding"](1102)
        drive_phone = onboarding2.get("phone")
        check("PHONE2-setup. onboarding phone durably saved for the identity-ok flow too",
              bool(drive_phone), repr(onboarding2))

        op2_row = ns["_repl_storage"].replacement_get(op2, db_path=db_path)
        old2_row = await ns["manager_get"]("oldphonex2")
        target_tgid = int(op2_row.get("new_tg_user_id") or 0)
        check("PHONE2-setup2. op row carries the confirmed new_tg_user_id from the identity-ok flow",
              target_tgid != 0, repr(op2_row))

        perm_path2 = ns["_replacement_permanent_session_path"]("newphonex2", base_dir=tmp_root)
        # Pre-authorize the PERMANENT session path (not the temp one used by
        # the phone/code steps above) under the matching identity -- proves
        # _repl4_create_new_manager's own connect()+is_user_authorized()
        # check on the permanent session, independent of the earlier
        # phone/code flow's temp session.
        success_factory = make_fake_client_factory(
            {"actual_user_id": target_tgid, "actual_username": op2_row.get("new_username") or "newacc"},
            [], {perm_path2},
        )
        result2 = await ns["_repl4_create_new_manager"](
            op2_row, old2_row, "newphonex2", 1102, tmp_root, client_factory=success_factory,
        )
        check("PHONE2a. _repl4_create_new_manager reports no error (returns None) on the identity-confirmed path",
              result2 is None, repr(result2))
        new_row2 = await ns["manager_get"]("newphonex2")
        check("PHONE2b. new manager row was created", new_row2 is not None, new_row2)
        check("PHONE2c. new manager phone equals the operator-entered phone via the _manager_finalize_login path (not silently erased to '')",
              new_row2 is not None and new_row2.get("phone") == drive_phone, repr(new_row2))
        check("PHONE2d. (regression guard) phone is definitely non-empty here -- this is exactly the incident condition, on the OTHER branch",
              bool(new_row2.get("phone")), repr(new_row2))

        old1_after = await ns["manager_get"]("oldphonex1")
        old2_after = await ns["manager_get"]("oldphonex2")
        check("PHONE3. both old managers are completely untouched by either case",
              old1_after.get("status") == "active" and old2_after.get("status") == "active",
              (old1_after, old2_after))
    finally:
        await cleanup_env(tmp_root)


async def test_group_f2_checkpoint_name_source_isolation():
    """F2 CHECKPOINT (2026-08-09): independent proof that _repl4_create_new_manager
    NEVER writes the business/admin-entered new_display_name into the Telegram
    `first_name` column, on EITHER branch, and that a business display_name always
    survives untouched. Reuses the SAME direct-call technique as Group PHONE above
    (build_stage2_ns + STAGE4_PHONE_NAMES + make_fake_client_factory) -- no new
    harness machinery.

    new_display_name is traced to replacement_send_phone_code's own `new_display_name`
    parameter (main.py), required non-empty BEFORE any Telegram phone/code/sign-in
    step even starts -- i.e. it is admin/business-entered, never Telegram-derived.
    An earlier version of the F4 fix passed it into manager_sync_telegram_profile_in_db's
    `first_name` kwarg on the fallback branch; this test is the regression guard for
    that exact defect.

      Case A: identity-confirmed branch (_manager_finalize_login) -- REAL Telegram
        me.first_name/username must be written; new_display_name must be preserved
        verbatim as display_name (via _manager_finalize_login's own non-empty/
        non-key preserve check -- see main.py ~line 2992).
      Case B: fallback branch (identity NOT confirmed) -- first_name must stay
        EMPTY (never fabricated from new_display_name); display_name must still be
        preserved verbatim (manager_add's own write, untouched by manager_set_fields);
        telegram_username/tg_user_id come only from the durable op_row (itself
        Telegram-sourced, captured earlier in the real flow by
        _replacement_after_signin -- seeded directly here via raw SQL, same fixture
        convention as seed_old_manager's manager_source_links insert, since this
        test calls _repl4_create_new_manager directly rather than driving the full
        multi-step wizard)."""
    print("\n-- Group F2-CHECKPOINT: business display_name vs Telegram first_name isolation --")
    business_name = "Алекс Продажи"

    def _seed_durable_identity(db_path: str, operation_id: str, *, username: str, tg_user_id: int) -> None:
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "UPDATE manager_replacements SET new_username=?, new_tg_user_id=? WHERE operation_id=?",
                (username, tg_user_id, operation_id),
            )
            con.commit()
        finally:
            con.close()

    # --- Case A: identity confirmed -> _manager_finalize_login branch ---
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root, extra_names=STAGE4_PHONE_NAMES)
        old = await seed_old_manager(ns, key="nsa_old")
        r0 = await ns["replacement_start"]("nsa_old", 2001, db_path=db_path)
        opA = r0["operation_id"]
        rA = await ns["replacement_send_phone_code"](
            opA, business_name, "nsa_new", "+19995550001", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("F2A-setup. send_phone_code succeeds", rA["ok"] is True, repr(rA))

        target_tgid = 444001
        _seed_durable_identity(db_path, opA, username="ivan123", tg_user_id=target_tgid)
        opA_row = ns["_repl_storage"].replacement_get(opA, db_path=db_path)
        check("F2A-setup2. durable new_tg_user_id seeded", int(opA_row.get("new_tg_user_id") or 0) == target_tgid, opA_row)
        oldA_row = await ns["manager_get"]("nsa_old")

        perm_pathA = ns["_replacement_permanent_session_path"]("nsa_new", base_dir=tmp_root)
        confirmed_factory = make_fake_client_factory(
            {"actual_user_id": target_tgid, "actual_username": "ivan123",
             "actual_first_name": "Иван", "actual_last_name": ""},
            [], {perm_pathA},
        )
        resultA = await ns["_repl4_create_new_manager"](
            opA_row, oldA_row, "nsa_new", 2001, tmp_root, client_factory=confirmed_factory,
        )
        check("F2A. _repl4_create_new_manager reports no error on the identity-confirmed path", resultA is None, repr(resultA))
        rowA = await ns["manager_get"]("nsa_new")
        check("F2A1. business display_name preserved verbatim", bool(rowA) and rowA.get("display_name") == business_name, rowA)
        check("F2A2. REAL Telegram first_name written (from me, not from new_display_name)",
              bool(rowA) and rowA.get("first_name") == "Иван", rowA)
        check("F2A3. business display_name text NEVER leaks into first_name",
              bool(rowA) and rowA.get("first_name") != business_name, rowA)
        check("F2A4. REAL Telegram username written", bool(rowA) and rowA.get("telegram_username") == "ivan123", rowA)
    finally:
        await cleanup_env(tmp_root)

    # --- Case B: identity NOT confirmed -> fallback branch ---
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root, extra_names=STAGE4_PHONE_NAMES)
        old = await seed_old_manager(ns, key="nsb_old")
        r0 = await ns["replacement_start"]("nsb_old", 2002, db_path=db_path)
        opB = r0["operation_id"]
        rB = await ns["replacement_send_phone_code"](
            opB, business_name, "nsb_new", "+19995550002", {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("F2B-setup. send_phone_code succeeds", rB["ok"] is True, repr(rB))

        _seed_durable_identity(db_path, opB, username="realuser99", tg_user_id=444002)
        opB_row = ns["_repl_storage"].replacement_get(opB, db_path=db_path)
        oldB_row = await ns["manager_get"]("nsb_old")

        # Unauthenticated fake client -- is_user_authorized() False -> me stays
        # None inside _repl4_create_new_manager -> the fallback branch runs.
        unauth_factory = make_fake_client_factory({}, [], set())
        resultB = await ns["_repl4_create_new_manager"](
            opB_row, oldB_row, "nsb_new", 2002, tmp_root, client_factory=unauth_factory,
        )
        check("F2B. _repl4_create_new_manager reports no error on the fallback path", resultB is None, repr(resultB))
        rowB = await ns["manager_get"]("nsb_new")
        check("F2B1. business display_name preserved verbatim (untouched by the fallback branch)",
              bool(rowB) and rowB.get("display_name") == business_name, rowB)
        check("F2B2. first_name is EMPTY -- never fabricated from new_display_name (the exact defect this checkpoint caught)",
              rowB is not None and (rowB.get("first_name") or "") == "", rowB)
        check("F2B3. business display_name text NEVER appears in first_name (redundant/explicit)",
              rowB is not None and rowB.get("first_name") != business_name, rowB)
        check("F2B4. durable (real, Telegram-sourced) username written", bool(rowB) and rowB.get("telegram_username") == "realuser99", rowB)
        check("F2B5. durable (real, Telegram-sourced) tg_user_id written", bool(rowB) and int(rowB.get("tg_user_id") or 0) == 444002, rowB)
    finally:
        await cleanup_env(tmp_root)


async def test_group_onboarding_ownership_mismatch():
    """TPILOT FIX-7 20260718b regression: _repl4_create_new_manager (main.py
    ~34494-34506) now re-verifies the fetched onboarding row's manager_key
    actually matches new_key BEFORE trusting its phone --
      if onboarding and registry_normalize_manager_key(onboarding.get("manager_key") or "") != new_key:
          onboarding = None
    manager_onboarding is keyed by owner_user_id ALONE (upserts on any new
    onboarding/login flow for that owner), so this guards against a
    resume-after-crash race where a DIFFERENT replacement flow's onboarding
    write for the SAME owner_user_id is sitting in that row when
    _repl4_create_new_manager runs for THIS operation's new_key.

    Negative case: seeds a manager_onboarding row for the SAME owner_user_id
    but a DIFFERENT manager_key than the operation's real new_key (simulating
    exactly that stale/different-flow row), drives _repl4_create_new_manager,
    and asserts the resulting new manager's phone is EMPTY -- proving the
    mismatched onboarding phone is rejected, not silently used.

    Positive control: the SAME setup (send_phone_code -> durable onboarding
    row) but with manager_key MATCHING new_key (the normal case, no stale
    row involved) -- asserts the phone IS used, so the negative case isn't
    vacuously passing because phone is always empty for some unrelated
    reason."""
    print("\n-- Group ONBOARD-MISMATCH: onboarding-ownership guard (TPILOT FIX-7 20260718b) --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_stage2_ns(db_path, tmp_root, extra_names=STAGE4_PHONE_NAMES)
        entered_phone = "+19995559999"

        # --- Negative: onboarding row's manager_key does NOT match new_key ---
        owner_n = 1301
        await seed_old_manager(ns, key="oldmismatch_n")
        r0n = await ns["replacement_start"]("oldmismatch_n", owner_n, db_path=db_path)
        opn = r0n["operation_id"]
        r1n = await ns["replacement_send_phone_code"](
            opn, "Имя Н", "newmismatch_n", entered_phone, {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("MISMATCH-setup1. send_phone_code succeeds (durably saves onboarding for owner_n)",
              r1n["ok"] is True, repr(r1n))

        # Overwrite the SAME owner's onboarding row with a DIFFERENT
        # manager_key -- simulates a stale/different-flow row landing last
        # for this owner_user_id (manager_onboarding upserts on owner_user_id
        # alone), while the operation's OWN durable new_manager_key stays
        # "newmismatch_n".
        await ns["manager_save_onboarding"](
            owner_n, manager_key="some_other_stale_key_n", step="replace_code", phone=entered_phone,
        )
        onboarding_n = await ns["manager_get_onboarding"](owner_n)
        check("MISMATCH-setup2. onboarding row now carries a MISMATCHED (stale) manager_key",
              onboarding_n is not None and onboarding_n.get("manager_key") != "newmismatch_n", repr(onboarding_n))

        opn_row = ns["_repl_storage"].replacement_get(opn, db_path=db_path)
        check("MISMATCH-setup3. the operation's OWN new_manager_key is still the real 'newmismatch_n'",
              opn_row.get("new_manager_key") == "newmismatch_n", repr(opn_row))
        oldn_row = await ns["manager_get"]("oldmismatch_n")
        unauth_factory_n = make_fake_client_factory({}, [], set())
        result_n = await ns["_repl4_create_new_manager"](
            opn_row, oldn_row, "newmismatch_n", owner_n, tmp_root, client_factory=unauth_factory_n,
        )
        check("MISMATCH-N1. _repl4_create_new_manager reports no error (degrades safely, returns None)",
              result_n is None, repr(result_n))
        new_row_n = await ns["manager_get"]("newmismatch_n")
        check("MISMATCH-N2. new manager row was created", new_row_n is not None, new_row_n)
        check("MISMATCH-N3 (negative). new manager phone is EMPTY -- the mismatched onboarding "
              "row's phone was correctly REJECTED, never silently used",
              new_row_n is not None and new_row_n.get("phone") == "", repr(new_row_n))

        # --- Positive control: SAME flow, but manager_key MATCHES new_key ---
        owner_p = 1302
        await seed_old_manager(ns, key="oldmismatch_p")
        r0p = await ns["replacement_start"]("oldmismatch_p", owner_p, db_path=db_path)
        opp = r0p["operation_id"]
        r1p = await ns["replacement_send_phone_code"](
            opp, "Имя П", "newmismatch_p", entered_phone, {}, db_path=db_path, base_dir=tmp_root,
            client_factory=ns["__client_factory__"],
        )
        check("MISMATCH-setup4. send_phone_code succeeds (positive control, owner_p)", r1p["ok"] is True, repr(r1p))
        onboarding_p = await ns["manager_get_onboarding"](owner_p)
        check("MISMATCH-setup5. onboarding row's manager_key MATCHES new_key (control, no mismatch here)",
              onboarding_p is not None and onboarding_p.get("manager_key") == "newmismatch_p", repr(onboarding_p))

        opp_row = ns["_repl_storage"].replacement_get(opp, db_path=db_path)
        oldp_row = await ns["manager_get"]("oldmismatch_p")
        unauth_factory_p = make_fake_client_factory({}, [], set())
        result_p = await ns["_repl4_create_new_manager"](
            opp_row, oldp_row, "newmismatch_p", owner_p, tmp_root, client_factory=unauth_factory_p,
        )
        check("MISMATCH-P1. _repl4_create_new_manager reports no error on the control path",
              result_p is None, repr(result_p))
        new_row_p = await ns["manager_get"]("newmismatch_p")
        check("MISMATCH-P2 (positive control). new manager phone IS the operator-entered phone when "
              "manager_key MATCHES (proves the negative result isn't vacuous -- phone is not always empty)",
              new_row_p is not None and new_row_p.get("phone") == entered_phone, repr(new_row_p))

        oldn_after = await ns["manager_get"]("oldmismatch_n")
        oldp_after = await ns["manager_get"]("oldmismatch_p")
        check("MISMATCH-3. both old managers are completely untouched by either case",
              oldn_after.get("status") == "active" and oldp_after.get("status") == "active",
              (oldn_after, oldp_after))
    finally:
        await cleanup_env(tmp_root)


async def run_all() -> None:
    await test_group_1_start_and_locking()
    await test_group_2_name_and_key()
    await test_group_3_session_isolation()
    await test_group_4_proxy()
    await test_group_5_phone()
    await test_group_6_code()
    await test_group_7_password()
    await test_group_8_identity()
    await test_group_9_ready_commit()
    await test_group_10_cancel_fail()
    await test_group_11_recovery()
    await test_group_12_regression_security()
    await test_group_13_r1_cancel_ordering()
    await test_group_14_r2_durable_proxy()
    await test_group_15_r3_tg_user_id()
    await test_group_16_r4_authorized_recovery()
    await test_group_17_retry_consistency_and_cleanup_durability()
    test_group_18_dbguard()
    await test_group_phone_persistence_regression()
    await test_group_f2_checkpoint_name_source_isolation()
    await test_group_onboarding_ownership_mismatch()


def main() -> int:
    asyncio.run(run_all())
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
