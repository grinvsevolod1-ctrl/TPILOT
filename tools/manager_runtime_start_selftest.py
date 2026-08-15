# -*- coding: utf-8 -*-
"""tools/manager_runtime_start_selftest.py -- offline self-test for the
"TPILOT RUNTIME AUTH SESSION-REUSE FIX 20260718" patch (plan principle A):
main.py's manager-runtime startup sequence must reuse an already-authorized
on-disk Telethon session WITHOUT ever calling client.start(phone=None) --
real Telethon's own client.start() raises "No phone number or bot token
provided." (telethon/client/auth.py) before it ever attempts its own
session-reuse path, so a manager with a fully authorized session but an
empty PHONE (e.g. created via replacement, whose phone was never persisted)
previously could never start even though nothing about its session actually
needed re-authorization.

Technique / why this file does NOT extract-and-exec the whole main():
  main.py's async def main() (the manager-runtime process entrypoint,
  currently defined -- as ONE base definition at line ~5061, its ONLY
  definition; unlike some other names in this file "main" itself is not
  re-overridden anywhere below it, later "async def main()" occurrences in
  main.py belong to unrelated wrapper patches for OTHER features that
  delegate back to this same base via a `_X_ORIG_MAIN = globals().get("main")`
  handoff, e.g. the downtime-catchup patch) is a ~130-line function that also
  does DB init, six-plus background client.loop.create_task(...) spawns, and
  finally blocks forever on client.run_until_disconnected() -- none of which
  this offline test can or should exercise. Extracting/exec'ing main() whole
  is impractical and unsafe (no real Telethon client, no real event loop
  meant to block forever).

  Investigation confirmed the session-reuse control flow is NOT already
  factored into its own smaller, independently-callable helper -- it lives
  inline inside main() (main.py lines ~5082-5163). Per the task's own
  documented fallback (option a): this file extracts, for REAL, via
  ast.parse + ast.unparse + exec() (same technique as every other
  tools/*_selftest.py in this project), only the one genuinely small,
  independently-testable helper that carries the actual pass/fail DECISION
  logic under test -- `_m212a_fail_startup` (writes a start_status.json
  reason_class then raises SystemExit(1)). `_now_utc_iso` is extracted
  alongside it (trivial, pure, a direct dependency).

  `_m212a_write_start_status` -- the OTHER direct dependency of
  `_m212a_fail_startup` -- is deliberately NOT extracted: the real
  implementation resolves its own write path relative to main.py's own file
  location (BASE_DIR/runtime/managers/<key>/start_status.json), i.e. the
  real, protected project runtime/ directory. A fake is bound under the
  exact production name instead (same technique already used project-wide
  for primitives an offline test cannot safely exercise for real, e.g.
  _spawn_manager_process in manager_replacement_commit_selftest.py) -- it
  only appends (manager_key, phase, kwargs) tuples to an in-memory list,
  never touching any filesystem path.

  The session-reuse CONTROL FLOW itself (connect -> is_user_authorized ->
  [phone-gated client.start] -> write "connected" -> get_me -> [identity
  mismatch guard] -> profile sync) is hand-mirrored, verbatim, as this
  file's own `harness_session_reuse_flow` async function -- a deliberate,
  documented copy of main.py lines ~5082-5163, calling the REAL, exec'd
  `_m212a_fail_startup` for every pass/fail decision. This mirror is NOT
  automatically kept in sync with main.py -- a future edit to that control
  flow needs a matching update here (the same tradeoff manager_replacement_
  commit_selftest.py's own docstring documents for its own extraction
  choices). `get_manager_row_from_db_sync` (manager_registry.py, plain sync
  sqlite read) and `manager_sync_telegram_profile_in_db` (storage.py, real
  async helper) are used for REAL against a temp SQLite DB -- never faked.

Never: real Telegram network, real filesystem writes outside a temp dir,
production DB/runtime/session/log access.

    python tools\\manager_runtime_start_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import shutil
import sys
import tempfile
import tokenize
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

REAL_NAMES = {"_m212a_fail_startup", "_now_utc_iso"}


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
    """Mandatory production-path DB guard, same convention as every other
    tools/*_selftest.py in this project: refuses any db_path under
    base_dir/db, and requires both storage DB-path globals to already equal
    db_path."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def build_main_ns(db_path: str, *, write_status_calls: list) -> dict:
    import storage as _storage
    from datetime import datetime as _dt

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    nodes = _extract_by_names(MAIN_SRC, REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    def fake_write_start_status(manager_key, phase, **kwargs):
        write_status_calls.append((manager_key, phase, dict(kwargs)))

    ns = {
        "datetime": _dt,
        "MANAGER_RUNTIME_KEY": "",
        "CONTROLLER_MODE": False,
        "_m212a_write_start_status": fake_write_start_status,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:startup>", "exec"), ns)
    ns["__storage__"] = _storage
    return ns


# ======================================================================
# Fake Telethon client -- mirrors real Telethon's own client.start()
# semantics for the ONE behavior under test: raising "No phone number or
# bot token provided." (ValueError) when called with a falsy phone, BEFORE
# any session-reuse attempt of its own.
# ======================================================================

class FakeTelegramClient:
    def __init__(self, *, initially_authorized: bool, start_ok: bool = True, get_me_id: int = 900001,
                 get_me_username: str = "tguser", get_me_first: str = "First", get_me_last: str = "Last"):
        self._authorized = initially_authorized
        self._start_ok = start_ok
        self._get_me_id = get_me_id
        self._get_me_username = get_me_username
        self._get_me_first = get_me_first
        self._get_me_last = get_me_last
        self.connect_calls = 0
        self.start_calls: list = []   # each entry is the `phone` kwarg passed
        self.get_me_calls = 0
        self.disconnect_calls = 0

    async def connect(self):
        self.connect_calls += 1

    async def disconnect(self):
        self.disconnect_calls += 1

    async def is_user_authorized(self):
        return self._authorized

    async def start(self, phone=None):
        self.start_calls.append(phone)
        if not phone:
            # Real Telethon (telethon/client/auth.py _start): raises before
            # ever attempting its own connect/is_user_authorized/get_me
            # reuse path when neither phone nor bot_token is given.
            raise ValueError("No phone number or bot token provided.")
        if not self._start_ok:
            raise RuntimeError("simulated client.start() failure")
        self._authorized = True

    async def get_me(self):
        self.get_me_calls += 1
        if not self._authorized:
            return None

        class _Me:
            pass

        me = _Me()
        me.id = self._get_me_id
        me.username = self._get_me_username
        me.first_name = self._get_me_first
        me.last_name = self._get_me_last
        return me


# ======================================================================
# Hand-mirrored session-reuse control flow -- see module docstring.
# Mirrors main.py's async def main(), lines ~5082-5163.
# ======================================================================

async def harness_session_reuse_flow(
    ns: dict, client, *, phone: str, manager_runtime_key: str, controller_mode: bool,
    tpilot_db_path: str, get_manager_row_from_db_sync, manager_sync_telegram_profile_in_db,
    sync_calls: list,
) -> dict:
    ns["MANAGER_RUNTIME_KEY"] = manager_runtime_key
    ns["CONTROLLER_MODE"] = controller_mode

    await client.connect()
    try:
        _runtime_already_authorized = bool(await client.is_user_authorized())
    except Exception:
        _runtime_already_authorized = False

    if not _runtime_already_authorized:
        # HNV2 D3 20260807 mirror -- see main.py's own comment at the
        # equivalent site for the full rationale. A headless worker
        # (manager_runtime_key set, controller_mode False) must NEVER reach
        # client.start() -- it stops here, unconditionally, before the
        # phone check.
        if manager_runtime_key and not controller_mode:
            try:
                await client.disconnect()
            except Exception:
                pass
            ns["_m212a_fail_startup"](
                "session_unauthorized",
                "Session not authorized. Automatic login from a worker process is forbidden -- "
                "login via the Panel: relogin / QR / device login.",
            )
        if not phone:
            ns["_m212a_fail_startup"]("session_unauthorized", "Session not authorized, no phone number set -- login required.")
        try:
            await client.start(phone=phone)
        except Exception as e:
            ns["_m212a_fail_startup"]("session_unauthorized", f"Login failed: {e!r}")

    if manager_runtime_key and not controller_mode:
        ns["_m212a_write_start_status"](manager_runtime_key, "connected", last_connected_at=ns["_now_utc_iso"]())

    me = await client.get_me()
    if me is None:
        if manager_runtime_key and not controller_mode:
            ns["_m212a_fail_startup"]("session_unauthorized", "get_me() returned empty after successful login.")
        return {"me": None}

    if manager_runtime_key:
        try:
            row0 = get_manager_row_from_db_sync(tpilot_db_path, manager_runtime_key) or {}
            stored_tgid = int(row0.get("tg_user_id") or 0)
            live_tgid = int(getattr(me, "id", 0) or 0)
            if stored_tgid and live_tgid and stored_tgid != live_tgid:
                ns["_m212a_fail_startup"](
                    "telegram_identity_mismatch",
                    f"Telegram id mismatch: registry has {stored_tgid}, session has {live_tgid}.",
                )
            await manager_sync_telegram_profile_in_db(
                tpilot_db_path, manager_runtime_key,
                tg_user_id=live_tgid or None, telegram_username=getattr(me, "username", None) or "",
                first_name=(getattr(me, "first_name", None) or ""), last_name=(getattr(me, "last_name", None) or ""),
                phone=phone, status="active", last_login_at=ns["_now_utc_iso"](), last_error="",
            )
            sync_calls.append((manager_runtime_key, live_tgid))
        except SystemExit:
            raise
        except Exception:
            pass

    return {"me": me}


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="runtime_start_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


async def run_case(*, manager_key: str, phone: str, client: FakeTelegramClient,
                    seed_tg_user_id: int = 0, controller_mode: bool = False):
    """One full scenario: fresh temp DB + main_ns, optionally seeds a
    managers row with a durable tg_user_id, drives harness_session_reuse_flow,
    and returns everything a check() needs. Never leaves the temp dir behind
    (cleaned up by the caller)."""
    import manager_registry

    tmp_root, db_path = make_temp_env()
    write_status_calls: list = []
    main_ns = build_main_ns(db_path, write_status_calls=write_status_calls)
    storage_mod = main_ns["__storage__"]

    if manager_key:
        await storage_mod.manager_add(
            manager_key=manager_key, display_name=manager_key, phone="", status="active",
            session_path="", db_path=db_path, workdir="", log_path="",
        )
        if seed_tg_user_id:
            await storage_mod.manager_set_fields(manager_key, tg_user_id=seed_tg_user_id)

    sync_calls: list = []
    system_exit: SystemExit = None
    result = None
    try:
        result = await harness_session_reuse_flow(
            main_ns, client, phone=phone, manager_runtime_key=manager_key, controller_mode=controller_mode,
            tpilot_db_path=db_path, get_manager_row_from_db_sync=manager_registry.get_manager_row_from_db_sync,
            manager_sync_telegram_profile_in_db=storage_mod.manager_sync_telegram_profile_in_db,
            sync_calls=sync_calls,
        )
    except SystemExit as e:
        system_exit = e

    row_after = await storage_mod.manager_get(manager_key) if manager_key else None

    return {
        "tmp_root": tmp_root, "db_path": db_path, "write_status_calls": write_status_calls,
        "sync_calls": sync_calls, "system_exit": system_exit, "result": result, "row_after": row_after,
    }


def _last_exited_reason_class(write_status_calls: list) -> str:
    for mk, phase, kwargs in reversed(write_status_calls):
        if phase == "exited":
            return str(kwargs.get("reason_class") or "")
    return ""


# ======================================================================
# Scenario 1: authorized session + empty phone -- must skip client.start()
# entirely and reach get_me().
# ======================================================================

async def test_1_authorized_empty_phone():
    print("\n-- Scenario 1: authorized session + empty phone --")
    client = FakeTelegramClient(initially_authorized=True)
    ctx = await run_case(manager_key="mgr1", phone="", client=client)
    try:
        check("1a. no SystemExit raised", ctx["system_exit"] is None, ctx["system_exit"])
        check("1b. client.start() was never called (already-authorized session skips it entirely)",
              client.start_calls == [], client.start_calls)
        check("1c. get_me() was reached and returned a real identity", ctx["result"] and ctx["result"].get("me") is not None, ctx["result"])
        check("1d. 'connected' start_status was written", any(p == "connected" for _, p, _ in ctx["write_status_calls"]), ctx["write_status_calls"])
    finally:
        await cleanup_env(ctx["tmp_root"])


# ======================================================================
# Scenario 2: unauthorized session + empty phone -- must fail with
# session_unauthorized WITHOUT ever calling client.start(phone=None).
# ======================================================================

async def test_2_unauthorized_empty_phone():
    print("\n-- Scenario 2: unauthorized session + empty phone (THE BUG BEING FIXED) --")
    client = FakeTelegramClient(initially_authorized=False)
    ctx = await run_case(manager_key="mgr2", phone="", client=client)
    try:
        check("2a. SystemExit(1) raised", ctx["system_exit"] is not None and ctx["system_exit"].code == 1, ctx["system_exit"])
        check("2b. client.start() was never called at all (not even with phone=None)",
              client.start_calls == [], client.start_calls)
        check("2c. reason_class == session_unauthorized", _last_exited_reason_class(ctx["write_status_calls"]) == "session_unauthorized",
              ctx["write_status_calls"])
        check("2d. get_me() was never reached", client.get_me_calls == 0, client.get_me_calls)
    finally:
        await cleanup_env(ctx["tmp_root"])


# Regression-lock: prove the FakeTelegramClient itself genuinely reproduces
# real Telethon's ValueError semantics when start(phone=None) IS called, so
# scenario 2's "never called" assertion is meaningful and not vacuously true.
async def test_2b_fake_client_start_none_raises():
    print("\n-- Scenario 2b: fake client fidelity check (not itself part of main.py's flow) --")
    client = FakeTelegramClient(initially_authorized=False)
    raised = False
    try:
        await client.start(phone=None)
    except ValueError as e:
        raised = "No phone number or bot token provided." in str(e)
    check("2b. FakeTelegramClient.start(phone=None) raises the real Telethon ValueError (proves scenario 2's guard is real)", raised, None)


# ======================================================================
# Scenario 3: HNV2 D3 20260807 -- unauthorized session + non-empty phone,
# WORKER MODE (manager_runtime_key set, controller_mode False). A headless
# worker must NEVER call client.start() -- not even with a valid phone --
# it must stop immediately with session_unauthorized and route the
# operator to the Panel's controlled relogin/QR/device-login flows. This
# scenario used to expect client.start() to succeed (the pre-D3
# behavior); rewritten per the approved plan's D3 fix.
# ======================================================================

async def test_3_unauthorized_nonempty_phone_worker_mode():
    print("\n-- Scenario 3: unauthorized session + non-empty phone, WORKER MODE (D3) --")
    client = FakeTelegramClient(initially_authorized=False)
    ctx = await run_case(manager_key="mgr3", phone="+79990000000", client=client, controller_mode=False)
    try:
        check("3a. SystemExit(1) raised (worker refuses to log in interactively)", ctx["system_exit"] is not None and ctx["system_exit"].code == 1, ctx["system_exit"])
        check("3b. client.start() was NEVER called, even though a valid phone was available", client.start_calls == [], client.start_calls)
        check("3c. reason_class == session_unauthorized", _last_exited_reason_class(ctx["write_status_calls"]) == "session_unauthorized",
              ctx["write_status_calls"])
        check("3d. client.disconnect() was called before bailing out", client.disconnect_calls == 1, client.disconnect_calls)
        check("3e. get_me() was never reached", client.get_me_calls == 0, client.get_me_calls)
        check("3f. profile sync never ran", ctx["sync_calls"] == [], ctx["sync_calls"])
    finally:
        await cleanup_env(ctx["tmp_root"])


# ======================================================================
# Scenario 3b: HNV2 D3 20260807 -- the SAME unauthorized-session +
# non-empty-phone case, but CONTROLLER_MODE=True (a console-attached
# process with an operator present). The historical interactive
# client.start(phone=...) path must remain fully intact here -- D3 only
# gates the headless worker path, never the controller's.
# ======================================================================

async def test_3b_unauthorized_nonempty_phone_controller_mode():
    print("\n-- Scenario 3b: unauthorized session + non-empty phone, CONTROLLER_MODE (D3 unaffected) --")
    client = FakeTelegramClient(initially_authorized=False)
    ctx = await run_case(manager_key="mgr3b", phone="+79990000000", client=client, controller_mode=True)
    try:
        check("3b-a. no SystemExit raised", ctx["system_exit"] is None, ctx["system_exit"])
        check("3b-b. client.start() was called exactly once, with the real phone (controller path preserved)", client.start_calls == ["+79990000000"], client.start_calls)
        check("3b-c. client.disconnect() was NOT called (the D3 worker-only bailout never fires in controller mode)", client.disconnect_calls == 0, client.disconnect_calls)
        check("3b-d. get_me() was reached and succeeded", ctx["result"] and ctx["result"].get("me") is not None, ctx["result"])
    finally:
        await cleanup_env(ctx["tmp_root"])


# ======================================================================
# Scenario 4: identity mismatch -- live get_me().id differs from the
# registry's durable tg_user_id -- must fail loudly, never silently
# overwrite the registry.
# ======================================================================

async def test_4_identity_mismatch():
    print("\n-- Scenario 4: Telegram identity mismatch --")
    client = FakeTelegramClient(initially_authorized=True, get_me_id=999999)
    ctx = await run_case(manager_key="mgr4", phone="", client=client, seed_tg_user_id=555001)
    try:
        check("4a. SystemExit(1) raised", ctx["system_exit"] is not None and ctx["system_exit"].code == 1, ctx["system_exit"])
        check("4b. reason_class == telegram_identity_mismatch", _last_exited_reason_class(ctx["write_status_calls"]) == "telegram_identity_mismatch",
              ctx["write_status_calls"])
        check("4c. profile sync was NEVER called (mismatch short-circuits before it)", ctx["sync_calls"] == [], ctx["sync_calls"])
        check("4d. registry tg_user_id was NOT silently overwritten", ctx["row_after"] and int(ctx["row_after"].get("tg_user_id") or 0) == 555001,
              ctx["row_after"])
    finally:
        await cleanup_env(ctx["tmp_root"])


async def test_4b_matching_identity_no_mismatch():
    print("\n-- Scenario 4b: matching identity is NOT flagged as a mismatch (control case) --")
    client = FakeTelegramClient(initially_authorized=True, get_me_id=555001)
    ctx = await run_case(manager_key="mgr4b", phone="", client=client, seed_tg_user_id=555001)
    try:
        check("4b-1. no SystemExit raised when live id matches the registry", ctx["system_exit"] is None, ctx["system_exit"])
        check("4b-2. profile sync ran normally", ctx["sync_calls"] == [("mgr4b", 555001)], ctx["sync_calls"])
    finally:
        await cleanup_env(ctx["tmp_root"])


async def test_4c_first_login_no_stored_tgid():
    print("\n-- Scenario 4c: first-ever login (no stored tg_user_id yet) is never flagged as a mismatch --")
    client = FakeTelegramClient(initially_authorized=True, get_me_id=123456)
    ctx = await run_case(manager_key="mgr4c", phone="", client=client, seed_tg_user_id=0)
    try:
        check("4c-1. no SystemExit raised (stored_tgid=0 never compared)", ctx["system_exit"] is None, ctx["system_exit"])
        check("4c-2. tg_user_id now written for the first time", ctx["row_after"] and int(ctx["row_after"].get("tg_user_id") or 0) == 123456,
              ctx["row_after"])
    finally:
        await cleanup_env(ctx["tmp_root"])


# ======================================================================
# Static / regression-lock checks.
# ======================================================================

def test_static_no_override_stacking():
    print("\n-- Static: no override stacking on the extracted names --")
    tree = ast.parse(MAIN_SRC)
    from collections import Counter
    defs = Counter(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for name in ("_m212a_fail_startup", "_now_utc_iso", "_m212a_write_start_status"):
        check(f"static. {name} defined exactly once at module top level", defs.get(name, 0) == 1, defs.get(name, 0))


def test_static_dbguard():
    print("\n-- Static: production-path DB guard --")
    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    safe_path = os.path.join(tempfile.gettempdir(), "dbguard_runtime_start_probe", "safe.db")

    class _FakeStorage:
        def __init__(self, db_path, queue_db_path):
            self.DB_PATH = db_path
            self.QUEUE_DB_PATH = queue_db_path

    def _raises(db_path, storage_stub):
        try:
            _selftest_db_guard(db_path, BASE_DIR, storage_stub)
            return False
        except AssertionError:
            return True

    check("dbguard-1. rejects the real production DB file path", _raises(prod_file, _FakeStorage(prod_file, prod_file)), prod_file)
    check("dbguard-2. rejects a DB_PATH mismatch", _raises(safe_path, _FakeStorage("other.db", safe_path)), None)
    check("dbguard-3. allows a normal safe temp path", not _raises(safe_path, _FakeStorage(safe_path, safe_path)), None)


# ======================================================================
# Static tripwire: a genuine, independent source-inspection check that the
# real fix is actually present in main.py -- NOT just that
# harness_session_reuse_flow (the hand-mirrored copy above, per this file's
# own docstring a deliberate manual duplicate of main.py's control flow)
# still passes. The hand-mirror can never notice a future REVERT of the
# real fix in main.py, because it does not read main.py at all once
# written; this check re-reads main.py's actual source text every run.
#
# "Active main()" here follows the same investigation this file's own
# docstring already documents: main.py has many module-level "async def
# main()" *textual* occurrences (this project's usual stacked-override
# convention is "grep -n 'def main', take the last"), but every one of
# them past the ONE base definition is a thin delegate-only wrapper for an
# unrelated feature -- each captures its predecessor via
# `_X_ORIG_MAIN = globals().get("main")` (or an equivalently-named capture)
# and calls it through, never re-implementing or removing the session-reuse
# control flow itself. That base definition is identified STRUCTURALLY
# below, not by a hardcoded line number (so it survives line drift from
# unrelated future patches): it is the only "async def main()" in the
# module that directly calls client.run_until_disconnected() -- every
# delegate-only wrapper hands off instead of blocking on the client loop
# itself. Verified by inspection against both the current main.py and the
# pre-fix backup: exactly one hit in each.
# ======================================================================

def _tripwire_strip_comments(src: str) -> str:
    """Blanks out `# ...` comment text (keeping the line's own code and
    newline exactly as-is, byte-for-byte, via tokenize's own (row, col)
    positions) so a comment merely DESCRIBING the old buggy pattern (as the
    fix's own explanatory comment does, verbatim, right above the real fix)
    is never mistaken for the pattern still being live code. Falls back to
    the original text if tokenization fails for any reason (never hides a
    real failure behind a parse error)."""
    lines = src.splitlines(keepends=True)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                line = lines[row - 1]
                nl = ""
                if line.endswith("\r\n"):
                    nl = "\r\n"
                elif line.endswith("\n"):
                    nl = "\n"
                lines[row - 1] = line[:col] + nl
    except Exception:
        return src
    return "".join(lines)


def _tripwire_locate_base_main(src: str) -> str:
    """Returns the source text of the one module-level `async def main()`
    definition in `src` that contains the real runtime startup control
    flow, identified structurally by the one signal no delegate-only
    wrapper override shares: a direct call to client.run_until_disconnected()."""
    tree = ast.parse(src)
    hits = []
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            seg = ast.get_source_segment(src, node) or ""
            if "run_until_disconnected(" in seg:
                hits.append(seg)
    if len(hits) != 1:
        raise AssertionError(
            f"expected exactly one async def main() containing run_until_disconnected(), found {len(hits)}"
        )
    return hits[0]


# Exact pre-fix buggy line, quoted verbatim from
# main.py.bak_replfix_20260718_163215 line 5054 (the backup taken
# immediately before the TPILOT RUNTIME AUTH SESSION-REUSE FIX 20260718
# patch was applied).
_TRIPWIRE_OLD_BUGGY_PATTERN = "await client.start(phone=PHONE if PHONE else None)"


def _tripwire_assertions(main_src: str) -> dict:
    """The 3 required tripwire assertions plus the identity-mismatch guard
    signal, each independently evaluable against any main() source text --
    current main.py or a pre-fix backup alike. Evaluated against a
    comment-stripped copy of the source (see _tripwire_strip_comments) so
    that comment TEXT describing the old bug (main.py's fix comment quotes
    the old buggy line verbatim as documentation) can never masquerade as
    the pattern still being live, executable code."""
    code_only = _tripwire_strip_comments(main_src)
    connect_idx = code_only.find("client.connect(")
    start_idx = code_only.find("client.start(")
    return {
        "has_is_user_authorized": "is_user_authorized(" in code_only,
        "connect_before_start": connect_idx != -1 and start_idx != -1 and connect_idx < start_idx,
        "no_old_buggy_pattern": _TRIPWIRE_OLD_BUGGY_PATTERN not in code_only,
        "has_identity_mismatch_guard": (
            "stored_tgid != live_tgid" in code_only and "telegram_identity_mismatch" in code_only
        ),
    }


def test_static_source_tripwire():
    print("\n-- Static: source-inspection tripwire (independent of the hand-mirrored harness) --")

    current_main_src = _tripwire_locate_base_main(MAIN_SRC)
    current = _tripwire_assertions(current_main_src)
    check("tripwire-1. active main() source calls is_user_authorized( "
          "(proves the authorized-session-reuse pre-check exists)",
          current["has_is_user_authorized"], current)
    check("tripwire-2. active main() source calls client.connect( before any client.start( "
          "(proves connect-then-check ordering, not start-first)",
          current["connect_before_start"], current)
    check("tripwire-3. active main() source does NOT contain the old buggy pattern "
          f"{_TRIPWIRE_OLD_BUGGY_PATTERN!r}",
          current["no_old_buggy_pattern"], current)
    check("tripwire-5. active main() source contains an identity-mismatch guard "
          "(stored_tgid != live_tgid guarding telegram_identity_mismatch)",
          current["has_identity_mismatch_guard"], current)

    # Requirement 4 -- discriminating-power proof: the SAME tripwire logic
    # run against the pre-fix backup must fail at least one of tripwire-1/2/3
    # (proving these checks are not trivially always-true).
    backup_path = BASE_DIR / "main.py.bak_replfix_20260718_163215"
    try:
        backup_src = backup_path.read_text(encoding="utf-8-sig")
        backup_main_src = _tripwire_locate_base_main(backup_src)
        backup = _tripwire_assertions(backup_main_src)
        backup_three_pass = (
            backup["has_is_user_authorized"],
            backup["connect_before_start"],
            backup["no_old_buggy_pattern"],
        )
        check("tripwire-4. the SAME tripwire logic run against the pre-fix backup "
              f"({backup_path.name}) correctly FAILS at least one of assertions 1-3 "
              "(proves real discriminating power, not a vacuously-passing check)",
              not all(backup_three_pass), backup)
    except FileNotFoundError:
        check(f"tripwire-4. pre-fix backup file exists at {backup_path}", False, "file not found")


def main() -> int:
    asyncio.run(test_1_authorized_empty_phone())
    asyncio.run(test_2_unauthorized_empty_phone())
    asyncio.run(test_2b_fake_client_start_none_raises())
    asyncio.run(test_3_unauthorized_nonempty_phone_worker_mode())
    asyncio.run(test_3b_unauthorized_nonempty_phone_controller_mode())
    asyncio.run(test_4_identity_mismatch())
    asyncio.run(test_4b_matching_identity_no_mismatch())
    asyncio.run(test_4c_first_login_no_stored_tgid())
    test_static_no_override_stacking()
    test_static_dbguard()
    test_static_source_tripwire()

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
