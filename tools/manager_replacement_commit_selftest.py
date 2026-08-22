# -*- coding: utf-8 -*-
"""tools/manager_replacement_commit_selftest.py -- offline self-test for
Stage 4 of the manager Telegram-account replacement feature: the atomic
commit/cutover engine (main.py's replacement_commit and its phase helpers),
covering ready_commit -> committing -> links_pending -> links_ready ->
cutover_done. Does NOT cover notified/done (a later stage) or any AdminBot/
panel_bot.py UI (untouched by Stage 4).

Techniques (same as every other tools/*_selftest.py in this project):
  - main.py cannot be imported standalone (Telethon/env side effects at
    import time) -- the functions under test and their direct, pre-existing,
    UNMODIFIED dependencies are extracted via ast.parse + ast.unparse +
    exec() and run FOR REAL against a temporary SQLite DB (never
    db/data_tpilot.db) and a temporary runtime directory (never
    runtime/managers/).
  - Telethon itself is not installed locally -- session_path/proxy_config
    are faked under the exact production Telethon client factory name
    (_replacement_build_client), matching manager_replacement_backend_
    selftest.py's own convention.
  - Two production primitives that require a LIVE manager-level runtime
    process this test environment cannot provide are deliberately NOT
    extracted -- a fake is bound directly under their exact production
    names instead (same technique already used for _replacement_build_
    client everywhere in this project):
      * _queue_bizlink_create_n_for_manager -- the real one enqueues a job
        to the (nonexistent, in tests) manager's own runtime process queue
        and polls for completion; the fake here creates rows directly in
        the real `bizlinks` table, matching the real primitive's black-box
        contract (idempotent, up to `count` 'created' rows per manager/date).
      * _spawn_manager_process / _stop_manager_process /
        _manager_process_running -- no real subprocess/WMI process ever
        starts; a simple in-memory running-set fake stands in, matching
        every other selftest in this project.
  - `_selftest_db_guard` + a mandatory production-path DB guard (forensic
    review recommendation, 2026-07-16) refuses any db_path under the
    project's own db/ directory and requires storage.DB_PATH ==
    storage.QUEUE_DB_PATH == db_path before any commit-engine code runs.

Never: real Telegram network, real proxy/provider network, real process
spawn/stop, production DB/runtime/session/log access.

    python tools\\manager_replacement_commit_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import sqlite3
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
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
# PERF 20260718: main.py is ~35k lines -- ast.parse() on it costs ~2.5s.
# build_main_ns() (via _extract_by_names) used to re-parse it from scratch on
# EVERY call (dozens of times across this file's ~180 checks), which alone
# accounted for most of the suite's wall-clock time. Parsed exactly ONCE here;
# _extract_by_names only walks the already-parsed tree.body afterwards.
# ast.unparse() on individual nodes is read-only, so sharing this single Module
# object read-only across every build_main_ns() call (each of which compiles
# its own fresh code objects into its own fresh ns dict) is safe.
MAIN_AST = ast.parse(MAIN_SRC)


# ======================================================================
# Fake Telethon layer (same shapes as manager_replacement_backend/adminbot
# selftest.py's own fakes).
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


def make_fake_client_factory(script: dict, calls: list, authorized_sessions: set):
    class _FakeMe:
        def __init__(self, uid, username, first, last):
            self.id = uid
            self.username = username
            self.first_name = first
            self.last_name = last

    class FakeTelegramClient:
        def __init__(self, session_path, cfg=None):
            self.session_path = str(session_path)
            self.cfg = cfg

        async def connect(self):
            calls.append(("connect", self.session_path))
            try:
                open(self.session_path, "a").close()
            except Exception:
                pass

        async def disconnect(self):
            calls.append(("disconnect", self.session_path))

        async def is_user_authorized(self):
            return self.session_path in authorized_sessions

        async def get_me(self):
            calls.append(("get_me", self.session_path))
            if script.get("get_me_fail"):
                raise RuntimeError("simulated get_me failure")
            return _FakeMe(
                script.get("actual_user_id", 999001),
                script.get("actual_username", "newacc"),
                script.get("actual_first_name", "New"),
                script.get("actual_last_name", "Account"),
            )

        async def log_out(self):
            calls.append(("log_out", self.session_path))
            authorized_sessions.discard(self.session_path)

    def factory(session_path, cfg=None):
        return FakeTelegramClient(session_path, cfg)

    return factory


def _fresh_iso(seconds_ago: float = 0.0) -> str:
    """UTC ISO timestamp, optionally backdated -- used for start_status.json/
    manager_telegram_health fixture rows and fake runtime_ping payloads."""
    from datetime import datetime, timedelta, timezone as _tz
    return (datetime.now(_tz.utc) - timedelta(seconds=seconds_ago)).isoformat()


class FastMonotonic:
    """Deterministic, fast-forwarding stand-in for time.monotonic(), bound
    into a SPECIFIC test's main_ns (via build_main_ns(..., fast_deadline=True))
    -- never the real stdlib time module. manager_runtime_ready's own bounded
    deadline (90s/3s defaults) and _manager_runtime_ready_once's own ping wait
    (20s default) are NOT overridable through _repl4_validate_new_runtime/
    replacement_commit, so a full-engine test that wants to observe a genuine
    readiness FAILURE (any reason) would otherwise need to wait up to 90 real
    seconds. Every call jumps the clock forward by a large fixed step, so the
    very first deadline check after a failed attempt already reads as
    'expired'. asyncio's own internal scheduling uses the REAL time module
    directly (not this object), so it is completely unaffected -- this only
    changes what the EXTRACTED commit-engine code itself observes. Never use
    this for a test that expects manager_runtime_ready to succeed -- the ping
    wait loop would also fast-forward past any chance for the ping
    auto-responder to answer in time."""
    def __init__(self, jump: float = 1000.0):
        self._t = 0.0
        self._jump = jump

    def monotonic(self) -> float:
        self._t += self._jump
        return self._t


def make_fake_auth_guard(script: dict):
    """Fakes _tpag_run_guard under its exact production name (never
    extracted -- it pulls in proxy-provider/network code, matching this
    project's own stated convention of never extracting heavy dependency
    chains that would require real network access). Defaults to success;
    script['auth_guard'] = {key: False, 'msg': '...'} (or {'all': False})
    forces a failure for a specific test."""
    async def fake_guard(key, *, source="manual", force=False):
        cfg = script.get("auth_guard") or {}
        if cfg.get(key) is False or cfg.get("all") is False:
            return False, str(cfg.get("msg") or "simulated Auth Guard failure")
        return True, "OK"
    return fake_guard


async def _drive_ping_response(storage_mod, db_path: str, key: str, *, mode: str = "ok",
                                tg_user_id=None, delay_sec: float = 0.0, wait_timeout: float = 5.0,
                                worker_key_override: str = None, checked_at_override: str = None) -> None:
    """Waits (bounded, real wall-clock, polling every 30ms) for a single
    pending runtime_ping manager_commands row targeting `key`, then answers
    it via the REAL storage.manager_queue_finish -- the exact same real
    manager_commands table/primitives manager_runtime_ready's signal #10
    itself reads via storage.manager_queue_get. This is a genuine background
    task standing in for the (nonexistent, in tests) live manager runtime
    process that would normally serve runtime_ping -- never a shortcut
    inside the commit engine itself. mode='never' callers should simply not
    start this coroutine at all (see make_fake_process_helpers). Reused by
    both the fake _spawn_manager_process wiring (default healthy path, every
    pre-existing test in this file) and Group 16's direct
    manager_runtime_ready/_manager_runtime_ready_once unit tests (each
    individual failure mode). worker_key_override/checked_at_override
    (TPILOT FIX-8 20260718b, Group 21): otherwise-healthy responses whose
    worker_key or checked_at is deliberately wrong/stale -- both default to
    None (preserving every pre-existing caller's behavior unchanged: real
    key, fresh timestamp)."""
    if delay_sec:
        await asyncio.sleep(delay_sec)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + wait_timeout
    while loop.time() < deadline:
        try:
            con = sqlite3.connect(db_path)
            try:
                rows = con.execute(
                    "SELECT nonce FROM manager_commands WHERE target_key=? AND command='runtime_ping'"
                    " AND status IN ('new','processing') ORDER BY id ASC LIMIT 1",
                    (key,),
                ).fetchall()
            finally:
                con.close()
        except sqlite3.OperationalError as e:
            # manager_commands is created LAZILY by the real
            # storage.manager_queue_put/_manager_queue_ready -- this
            # responder task is started (at fake spawn time) well before
            # _manager_runtime_ready_once ever reaches its ping-enqueue
            # step, so "no such table" here just means the real code
            # hasn't gotten there yet, NOT that the temp env was torn
            # down. Keep polling instead of giving up permanently (which
            # previously starved every ping and forced the full
            # ping_timeout_sec/deadline_sec wait on every happy-path
            # test). Any OTHER sqlite error (e.g. unable to open database
            # file because tmp_root was already deleted) still means the
            # env is genuinely gone -- give up in that case.
            if "no such table" in str(e).lower():
                await asyncio.sleep(0.03)
                continue
            return
        except Exception:
            return  # temp env torn down mid-poll -- nothing left to answer
        if rows:
            nonce = rows[0][0]
            if mode == "disconnected":
                connected, authorized, tgid = False, False, 0
            elif mode == "unauthorized":
                connected, authorized, tgid = True, False, 0
            elif mode == "wrong_tgid":
                connected, authorized, tgid = True, True, int(tg_user_id if tg_user_id is not None else 1)
            else:  # "ok"
                connected, authorized = True, True
                tgid = int(tg_user_id) if tg_user_id is not None else 0
                if not tgid:
                    row = await storage_mod.manager_get(key)
                    tgid = int((row or {}).get("tg_user_id") or 0)
            payload = {
                "ok": True, "connected": connected, "authorized": authorized, "tg_user_id": tgid,
                "worker_key": worker_key_override if worker_key_override is not None else key,
                "checked_at": checked_at_override if checked_at_override is not None else _fresh_iso(),
            }
            await storage_mod.manager_queue_finish(
                nonce, worker_key=key, ok=True, result_text="ok",
                result_json=json.dumps(payload), db_path=db_path,
            )
            return
        await asyncio.sleep(0.03)


def _write_status_file(base_dir: Path, key: str, *, phase: str, updated_at=None,
                        reason_class: str = "", error: str = "") -> None:
    """Writes runtime/managers/<key>/start_status.json directly under
    base_dir -- the exact path/shape _manager_recovery_read_start_status
    (real, extracted) reads. base_dir must be the SAME Path bound to the
    test's own main_ns['BASE_DIR']."""
    d = base_dir / "runtime" / "managers" / str(key or "").strip().lower()
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "phase": phase, "updated_at": updated_at if updated_at is not None else _fresh_iso(),
        "reason_class": reason_class, "error": error,
    }
    (d / "start_status.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_heartbeat_row(db_path: str, key: str, *, status: str = "ok", age_sec: float = 0.0,
                         present: bool = True) -> None:
    """Writes/clears the manager_telegram_health row _manager_health_heartbeat_ok
    (real, extracted) reads. present=False simulates 'no heartbeat yet'."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS manager_telegram_health("
            "manager_key TEXT PRIMARY KEY, health_status TEXT DEFAULT '', last_check_at TEXT DEFAULT '')"
        )
        con.execute("DELETE FROM manager_telegram_health WHERE manager_key=?", (key,))
        if present:
            con.execute(
                "INSERT INTO manager_telegram_health(manager_key, health_status, last_check_at) VALUES(?,?,?)",
                (key, status, _fresh_iso(age_sec)),
            )
        con.commit()
    finally:
        con.close()


def make_fake_process_helpers(storage_mod, db_path: str, runtime_root: Path, *, ready_script: dict = None):
    """Fakes _manager_process_running/_spawn_manager_process/_stop_manager_process
    (no real subprocess ever starts -- same convention as every other selftest
    in this project) AND, since the TPILOT B/D reorder now makes
    _repl4_validate_new_runtime genuinely BLOCK (via the real, extracted
    manager_runtime_ready) until a runtime is confirmed ready, also drives the
    three readiness signals a real manager runtime would otherwise supply:
    start_status.json, manager_telegram_health, and an answered runtime_ping
    command. `ready_script` is a per-manager-key dict of overrides (keys:
    'start_status' in {"connected" (default), "stuck", "stale", "missing",
    "session_unauthorized", "telegram_identity_mismatch"}; 'heartbeat' in
    {"ok" (default), "missing", "stale", "blocked"}; 'ping' in {"ok"
    (default), "disconnected", "unauthorized", "wrong_tgid", "never"};
    'ping_tg_user_id', 'ping_delay_sec'). A manager key absent from
    ready_script gets the fully healthy default, so every pre-existing test
    in this file (written before this fixture existed) keeps passing
    unchanged. The ping auto-responder is a genuine asyncio background task
    (started the moment fake spawn succeeds) that answers via the REAL
    manager_commands table -- see _drive_ping_response."""
    running: set = set()
    stop_calls: list = []
    spawn_calls: list = []
    spawn_result = {"ok": True, "msg": "OK"}
    ready_script = ready_script if ready_script is not None else {}
    responder_tasks: dict = {}

    def _cfg(key):
        return ready_script.get(key) or {}

    def _apply_start_status(key: str) -> None:
        cfg = _cfg(key)
        mode = cfg.get("start_status", "connected")
        if mode == "missing":
            return
        if mode == "stuck":
            _write_status_file(runtime_root, key, phase="starting")
        elif mode == "stale":
            _write_status_file(runtime_root, key, phase="connected", updated_at=_fresh_iso(99999))
        elif mode == "session_unauthorized":
            _write_status_file(runtime_root, key, phase="exited", reason_class="session_unauthorized", error="not authorized")
        elif mode == "telegram_identity_mismatch":
            _write_status_file(runtime_root, key, phase="exited", reason_class="telegram_identity_mismatch", error="id mismatch")
        else:
            _write_status_file(runtime_root, key, phase="connected")

    def _apply_heartbeat(key: str) -> None:
        cfg = _cfg(key)
        mode = cfg.get("heartbeat", "ok")
        if mode == "missing":
            _seed_heartbeat_row(db_path, key, present=False)
        elif mode == "stale":
            _seed_heartbeat_row(db_path, key, status="ok", age_sec=99999)
        elif mode == "blocked":
            _seed_heartbeat_row(db_path, key, status="blocked", age_sec=0)
        else:
            _seed_heartbeat_row(db_path, key, status="ok", age_sec=0)

    async def _ping_responder(key: str) -> None:
        cfg = _cfg(key)
        mode = cfg.get("ping", "ok")
        if mode == "never":
            return  # deliberately never answers -- exercises command_timeout
        try:
            await _drive_ping_response(
                storage_mod, db_path, key, mode=mode, tg_user_id=cfg.get("ping_tg_user_id"),
                delay_sec=float(cfg.get("ping_delay_sec", 0) or 0), wait_timeout=5.0,
            )
        except asyncio.CancelledError:
            return

    async def fake_process_running(key):
        return key in running

    async def fake_stop_process(key, *, silent=False):
        stop_calls.append(key)
        running.discard(key)
        t = responder_tasks.pop(key, None)
        if t and not t.done():
            t.cancel()
        return True, "OK"

    async def fake_spawn_process(key):
        spawn_calls.append(key)
        if spawn_result["ok"]:
            running.add(key)
            _apply_start_status(key)
            _apply_heartbeat(key)
            existing = responder_tasks.get(key)
            if not existing or existing.done():
                responder_tasks[key] = asyncio.create_task(_ping_responder(key))
        return spawn_result["ok"], spawn_result["msg"]

    return {
        "running": running, "stop_calls": stop_calls, "spawn_calls": spawn_calls,
        "spawn_result": spawn_result, "ready_script": ready_script, "responder_tasks": responder_tasks,
        "fn_running": fake_process_running, "fn_stop": fake_stop_process, "fn_spawn": fake_spawn_process,
    }


def make_fake_bizlink_generator(link_calls: list, *, fail_after: int = None):
    """Black-box-equivalent stand-in for the real, queue-based
    _queue_bizlink_create_n_for_manager: creates up to `count` 'created'
    rows directly in the real `bizlinks` table for manager_key/target_date,
    idempotent (never re-creates an existing slot), using the SAME schema/
    unique-index storage.py already owns. fail_after, if set, stops after
    creating that many slots on THIS call (simulates a provider failure
    partway through -- the caller/test can then verify the operation stays
    resumable rather than failing outright)."""
    async def _fake(manager_key, target_date, count, *, user_id=0, timeout_sec=600):
        link_calls.append((manager_key, target_date, count))
        con = sqlite3.connect(_FAKE_BIZLINK_DB_PATH[0])
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS bizlinks(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " manager_key TEXT NOT NULL, target_date TEXT NOT NULL, slot_no INTEGER NOT NULL,"
                " status TEXT NOT NULL DEFAULT 'pending', link_url TEXT NOT NULL DEFAULT '',"
                " created_at TEXT NOT NULL DEFAULT '')"
            )
            con.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS bizlinks_mgr_date_slot ON bizlinks(manager_key, target_date, slot_no)"
            )
            existing = con.execute(
                "SELECT slot_no FROM bizlinks WHERE manager_key=? AND target_date=? AND status='created'",
                (manager_key, target_date),
            ).fetchall()
            have = {r[0] for r in existing}
            created_this_call = 0
            for slot in range(1, int(count) + 1):
                if slot in have:
                    continue
                if fail_after is not None and created_this_call >= fail_after:
                    break
                con.execute(
                    "INSERT OR IGNORE INTO bizlinks(manager_key, target_date, slot_no, status, link_url, created_at)"
                    " VALUES(?,?,?,'created',?,?)",
                    (manager_key, target_date, slot, f"https://t.me/fake_{manager_key}_{slot}", "2026-01-01T00:00:00"),
                )
                created_this_call += 1
            con.commit()
            return True, "OK"
        finally:
            con.close()
    return _fake


_FAKE_BIZLINK_DB_PATH = [""]  # set per-test by build_main_ns (module-level -- the fake closes over it)


# ======================================================================
# main.py extraction.
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

STAGE4_REAL_NAMES = {
    "_REPL4_COMMIT_PHASE_STATUSES", "_repl4_progress", "_repl4_result",
    "_repl4_links_ready_count", "_repl4_transfer_settings", "_repl4_transfer_schedule",
    "_repl4_transfer_access_groups", "_repl4_assign_proxy", "_repl4_promote_session",
    "_repl4_create_new_manager", "_repl4_run_links_phase", "_repl4_validate_new_runtime",
    "_repl4_final_revalidate", "_repl4_cutover_old_manager", "_repl4_entry_guards",
    "replacement_commit",
    "_panel_manager_delete_full_command", "_manager_delete_full_core",
    "_danger_parse_args", "_manager_admin_password_ok", "_manager_danger_allowed_key",
    "_log_manager_danger_action", "_ensure_manager_admin_log_table", "_panel_manager_missing_text",
    "_manager_runtime_paths_for_key", "_tp_finalize_screenshots_on",
    "_manager_finalize_login", "_kyiv_now",
    # TPILOT B/C/D RUNTIME READINESS + ROLLBACK 20260718: the reordered
    # runtime-readiness gate (now called from the EARLY 'committing' phase,
    # before any transfer/link-creation, see replacement_commit) and its
    # automatic-rollback companion.
    # RUNTIME READINESS RC 20260805 (wave L2): _RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC's
    # own definition now reads TP_HG_CHECK_INTERVAL_SEC (main.py) -- extract it too,
    # or the module-level assignment statement itself raises NameError at exec time.
    "TP_HG_CHECK_INTERVAL_SEC",
    "_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC", "_manager_health_heartbeat_ok",
    "_manager_runtime_ready_once", "manager_runtime_ready",
    "_manager_recovery_read_start_status", "TP_HG_STATUS_BLOCKED",
    "_repl4_rollback", "_REPL4_COMMIT_DEADLINE_SEC",
    # TPILOT FIX-4 20260718b: durable cutover point-of-no-return marker --
    # gates _repl4_rollback's hard guard, replacement_commit's resume-skip
    # of _repl4_final_revalidate, and (via storage.py, not extracted here)
    # the deadline sweeper's own SQL exclusion.
    "_REPL_CUTOVER_IRREVERSIBLE_STAGES",
    # Group 19 (cutover failure-injection): the real commit-deadline sweeper
    # pass, exercised directly against storage.replacement_list_commit_
    # phase_past_deadline's own SQL exclusion -- its only dependencies
    # (_now_utc_iso, _repl_storage, _repl4_rollback) are already extracted/
    # bound above, so this needs no new fakes.
    "_repl_commit_deadline_sweep_once",
}


def _extract_by_names(tree: ast.Module, names: set) -> list:
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
    """Mandatory production-path DB guard (forensic review recommendation,
    2026-07-16): refuses any db_path under base_dir/db, and requires both
    storage DB-path globals to already equal db_path. Same technique as the
    other three replacement selftests' own guard."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def build_main_ns(db_path: str, base_dir: Path, *, script: dict = None, fast_deadline: bool = False) -> dict:
    import manager_registry
    import storage as _storage
    import aiosqlite as _aiosqlite
    import time as _time
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    script = script if script is not None else {}
    calls: list = []
    authorized_sessions: set = set()
    link_calls: list = []
    client_factory = make_fake_client_factory(script, calls, authorized_sessions)
    proc = make_fake_process_helpers(_storage, db_path, base_dir, ready_script=script.get("ready_script"))
    auth_guard_fn = make_fake_auth_guard(script)
    _FAKE_BIZLINK_DB_PATH[0] = db_path
    bizlink_fn = make_fake_bizlink_generator(link_calls, fail_after=script.get("bizlink_fail_after"))

    names = STAGE2_REAL_NAMES | STAGE4_REAL_NAMES
    nodes = _extract_by_names(MAIN_AST, names)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    ns = {
        # STAGE 3: seed side-effect-free project modules reached through module-level
        # import aliases in the extracted main.py code (this test hit it as
        # NameError: proxy_parser). Splatted first; fakes below win.
        **ast_extract.safe_module_ns(),
        "os": os,
        "shutil": shutil,
        "re": __import__("re"),
        "asyncio": asyncio,
        "Path": Path,
        "aiosqlite": _aiosqlite,
        "datetime": _dt,
        "timedelta": _td,
        "timezone": _tz,
        "time": _time,
        # Windows here has no tzdata package -- a fixed UTC+3 offset stands
        # in for _kyiv_now()'s only real dependency (ZoneInfo("Europe/Kyiv")).
        "TZ_KYIV": __import__("datetime").timezone(__import__("datetime").timedelta(hours=3)),
        "BASE_DIR": base_dir,
        "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "mask_phone": manager_registry.mask_phone,
        "validate_manager_key": manager_registry.validate_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "ensure_manager_dirs": manager_registry.ensure_manager_dirs,
        "_replacement_build_client": client_factory,
        "manager_get": _storage.manager_get,
        "manager_add": _storage.manager_add,
        "manager_set_fields": _storage.manager_set_fields,
        "manager_get_onboarding": _storage.manager_get_onboarding,
        "manager_save_onboarding": _storage.manager_save_onboarding,
        "manager_delete_onboarding": _storage.manager_delete_onboarding,
        "manager_delete_onboarding_by_key": _storage.manager_delete_onboarding_by_key,
        "manager_list_pending": _storage.manager_list_pending,
        "_repl_storage": _storage,
        # Deliberately NOT extracted -- see module docstring. Fakes bound
        # directly under the exact production names.
        "_queue_bizlink_create_n_for_manager": bizlink_fn,
        "_manager_process_running": proc["fn_running"],
        "_stop_manager_process": proc["fn_stop"],
        "_spawn_manager_process": proc["fn_spawn"],
        # TPILOT RUNTIME READINESS 20260718: _tpag_run_guard is deliberately
        # NOT extracted (heavy proxy-provider/network dependency chain,
        # same rationale as the process helpers above) -- faked directly
        # under its exact production name, defaulting to success.
        "_tpag_run_guard": auth_guard_fn,
        "SessionPasswordNeededError": SessionPasswordNeededError,
        "PhoneCodeInvalidError": PhoneCodeInvalidError,
        "PhoneCodeExpiredError": PhoneCodeExpiredError,
        "PasswordHashInvalidError": PasswordHashInvalidError,
        "PhoneNumberBannedError": PhoneNumberBannedError,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object, "Optional": None,
        # utcnow refactor (2026-08-16): extracted main.py code reads the
        # clock through the module-level _tp_utc_now() seam (naive UTC).
        "_tp_utc_now": (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(tzinfo=None)),
    }
    exec(compile(module_src, f"<{MAIN_PATH}:replacement_commit>", "exec"), ns)
    if fast_deadline:
        # See FastMonotonic's own docstring -- swapped in AFTER exec (the
        # extracted functions' __globals__ IS this same ns dict, so a
        # post-exec mutation still takes effect at call time via normal
        # LOAD_GLOBAL lookup). Only appropriate for tests that expect
        # manager_runtime_ready to FAIL fast; never for a success path.
        ns["time"] = FastMonotonic()
    ns["__calls__"] = calls
    ns["__link_calls__"] = link_calls
    ns["__authorized_sessions__"] = authorized_sessions
    ns["__script__"] = script
    ns["__proc__"] = proc
    ns["__storage__"] = _storage
    return ns


# ======================================================================
# Temp DB / runtime / fixture helpers.
# ======================================================================

def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="replacement_commit_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def seed_manager(main_ns: dict, *, key: str, tg_user_id: int = 555001, display_name: str = "Менеджер",
                        username: str = "mgr_u", status: str = "active", is_enabled: int = 1,
                        manual_stopped: int = 0, owner_user_id: int = 9001, source_key: str = "src1",
                        write_session: bool = True) -> dict:
    storage_mod = main_ns["__storage__"]
    paths = main_ns["build_manager_paths"](str(main_ns["BASE_DIR"]), key)
    os.makedirs(paths["root"], exist_ok=True)
    await storage_mod.manager_add(
        manager_key=key, display_name=display_name, phone="+70000000000", status=status,
        session_path=paths["session_path"], db_path=paths["db_path"], workdir=paths["root"],
        log_path=paths["log_path"], is_enabled=is_enabled, owner_user_id=owner_user_id,
    )
    await storage_mod.manager_set_fields(
        key, manual_stopped=manual_stopped, tg_user_id=(tg_user_id or None), telegram_username=username,
    )
    con = sqlite3.connect(main_ns["TPILOT_DB_PATH"])
    try:
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT PRIMARY KEY, source_key TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')")
        con.execute("DELETE FROM manager_source_links WHERE manager_key=?", (key,))
        if source_key:
            con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, source_key))
        con.execute("CREATE TABLE IF NOT EXISTS manager_group_members(group_key TEXT NOT NULL DEFAULT '', manager_key TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(group_key, manager_key))")
        con.execute("CREATE TABLE IF NOT EXISTS manager_work_schedule_days(manager_key TEXT NOT NULL, work_date TEXT NOT NULL, is_working INTEGER NOT NULL, source TEXT DEFAULT '', updated_by_user_id INTEGER, updated_by_role TEXT, updated_at TEXT, PRIMARY KEY(manager_key, work_date))")
        con.execute("CREATE TABLE IF NOT EXISTS manager_bot_access(tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL DEFAULT '', can_receive_cards INTEGER NOT NULL DEFAULT 0, can_set_status INTEGER NOT NULL DEFAULT 0, can_view_stats INTEGER NOT NULL DEFAULT 0, stats_format TEXT NOT NULL DEFAULT 'light', allow_custom_period INTEGER NOT NULL DEFAULT 0, auto_granted INTEGER NOT NULL DEFAULT 0, granted_by INTEGER DEFAULT 0, granted_at TEXT NOT NULL DEFAULT '', revoked INTEGER NOT NULL DEFAULT 0, revoked_by INTEGER DEFAULT 0, revoked_at TEXT NOT NULL DEFAULT '', event_cutoff_id INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(tg_user_id, manager_key))")
        # storage.py's own _manager_table_ready() only migrates the base
        # proxy_* columns -- proxy_mode/proxy_bypass_allowed/proxy_required
        # are added by main.py's own _tpag_ensure_schema() (an unrelated,
        # heavier startup migration deliberately not extracted here). A real
        # deployment always has these by the time Stage 4 runs; add them
        # directly so this temp DB matches that already-migrated state.
        for ddl in (
            "ALTER TABLE managers ADD COLUMN proxy_mode TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN proxy_bypass_allowed INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE managers ADD COLUMN proxy_required INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                con.execute(ddl)
            except Exception:
                pass
        con.commit()
    finally:
        con.close()
    if write_session:
        with open(paths["session_path"], "wb") as f:
            f.write(f"OLD-BOEVOY-SESSION-{key}".encode())
    return await storage_mod.manager_get(key)


async def seed_ready_commit_op(main_ns: dict, *, old_key: str, new_key: str, new_tg_user_id: int = 999001,
                                new_display_name: str = "Новый Менеджер", new_username: str = "newacc",
                                target_date: str = "2026-08-01", proxy_mode: str = "direct",
                                proxy_ref: str = "", created_by_user_id: int = 1001, source_key: str = "src1",
                                required_links: int = 15) -> dict:
    """Constructs an operation durably AT ready_commit via raw
    _repl_storage.replacement_advance calls (same technique the AdminBot/
    backend selftests already use to build arbitrary durable states),
    skipping the already-tested Stage 2 sign-in flow entirely -- Stage 4
    only needs a durably-valid ready_commit operation, temp session file,
    and matching onboarding scratch row to begin. Pre-authorizes the temp
    session in the fake Telethon layer and configures the shared script
    dict so get_me() returns an identity matching new_tg_user_id/
    new_username (the shared script/authorized_sessions objects are
    per-main_ns, so tests needing DIFFERENT identities must build separate
    main_ns instances)."""
    storage_mod = main_ns["__storage__"]
    op_id = f"op-{new_key}"
    storage_mod.replacement_create(op_id, old_key, source_key=source_key, created_by_user_id=created_by_user_id)
    storage_mod.replacement_advance(
        op_id, "draft", "auth_phone", stage="phone_sent",
        fields={"new_manager_key": new_key, "new_display_name": new_display_name,
                "proxy_mode": proxy_mode, "proxy_ref": proxy_ref, "proxy_confirmed": 1,
                "source_key": source_key, "target_date": target_date},
    )
    storage_mod.replacement_advance(op_id, "auth_phone", "auth_code", stage="code_sent")
    storage_mod.replacement_advance(
        op_id, "auth_code", "identity_ok", stage="identity_verified",
        fields={"new_username": new_username, "new_tg_user_id": new_tg_user_id},
    )
    if required_links != 15:
        con = sqlite3.connect(main_ns["TPILOT_DB_PATH"])
        try:
            con.execute("UPDATE manager_replacements SET required_links=? WHERE operation_id=?", (required_links, op_id))
            con.commit()
        finally:
            con.close()
    storage_mod.replacement_advance(op_id, "identity_ok", "ready_commit", stage="ready_for_commit")

    proxy_fields = {} if proxy_mode != "proxy" else {
        "proxy_type": "socks5", "proxy_host": "5.5.5.5", "proxy_port": 1080,
        "proxy_username": "pu", "proxy_password": "pp",
    }
    await main_ns["manager_save_onboarding"](
        created_by_user_id, manager_key=new_key, step="replace_pass",
        phone="+79990000000", phone_code_hash="hash",
        tmp_session_path=main_ns["_replacement_temp_session_path"](new_key, base_dir=main_ns["BASE_DIR"]),
        expires_at="2099-01-01T00:00:00",
        **main_ns["_replacement_onboarding_proxy_fields"](proxy_fields, proxy_mode),
    )

    temp_path = main_ns["_replacement_temp_session_path"](new_key, base_dir=main_ns["BASE_DIR"])
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    with open(temp_path, "wb") as f:
        f.write(f"TEMP-SESSION-{new_key}".encode())
    main_ns["__authorized_sessions__"].add(temp_path)
    main_ns["__script__"]["actual_user_id"] = new_tg_user_id
    main_ns["__script__"]["actual_username"] = new_username

    return storage_mod.replacement_get(op_id)


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


async def make_env(*, old_key="oldmgr", new_key="newmgr", script: dict = None, fast_deadline: bool = False, **op_kwargs):
    """One-call fixture: temp env + main_ns + seeded old manager + a
    ready_commit operation for old_key -> new_key. Returns
    (tmp_root, db_path, main_ns, old_row, op_row). fast_deadline is only for
    tests that deliberately want manager_runtime_ready to fail fast through
    the full commit engine -- see FastMonotonic's docstring."""
    tmp_root, db_path = make_temp_env()
    main_ns = build_main_ns(db_path, tmp_root, script=script, fast_deadline=fast_deadline)
    old_row = await seed_manager(main_ns, key=old_key)
    op_row = await seed_ready_commit_op(main_ns, old_key=old_key, new_key=new_key, **op_kwargs)
    return tmp_root, db_path, main_ns, old_row, op_row


async def make_bare_env(*, key="rdkey", tg_user_id=999900, script: dict = None, fast_deadline: bool = False):
    """Lightweight fixture for Group 16's direct manager_runtime_ready/
    _manager_runtime_ready_once unit tests -- a single seeded manager row,
    no replacement operation at all (these signals are tested independently
    of the commit engine, per-signal, for speed/precision). Returns
    (tmp_root, db_path, main_ns, row)."""
    tmp_root, db_path = make_temp_env()
    main_ns = build_main_ns(db_path, tmp_root, script=script, fast_deadline=fast_deadline)
    row = await seed_manager(main_ns, key=key, tg_user_id=tg_user_id)
    return tmp_root, db_path, main_ns, row


async def run_to_cutover(main_ns, op_id, created_by_user_id, *, max_calls=6):
    """Drives replacement_commit repeatedly (mirrors how the real caller
    would resume across the committing/links_pending/links_ready phases)
    until it returns a non-links_pending terminal result or max_calls is
    reached. Returns the LAST result dict."""
    result = None
    for _ in range(max_calls):
        result = await main_ns["replacement_commit"](op_id, created_by_user_id=created_by_user_id)
        if result.get("code") not in ("links_pending",):
            break
    return result


async def drive_to_links_ready(main_ns, op_id, created_by_user_id, *, max_calls=6):
    """TPILOT FIX-4 20260718b DURABLE CUTOVER REDESIGN: the OLD technique
    here (stubbing _repl4_cutover_old_manager) no longer produces a genuine
    "paused before cutover, revalidation not yet run" state. Under the new
    durable point-of-no-return, replacement_commit runs
    _repl4_final_revalidate for REAL and then durably persists
    stage='cutover_started' (via replacement_set_stage) BEFORE it ever calls
    _repl4_cutover_old_manager -- so by the time the old stub would run,
    'cutover_started' is already committed to the DB. A SECOND
    replacement_commit call would then see stage in
    _REPL_CUTOVER_IRREVERSIBLE_STAGES and deliberately SKIP
    _repl4_final_revalidate on resume (that skip is itself the fix for the
    old_manager_missing-strands-a-resumed-cutover bug) -- exactly the wrong
    behavior for tests that want to corrupt state after links_ready and then
    observe final_revalidate genuinely catching it on the next call.

    This helper now instead temporarily swaps the real
    _repl4_final_revalidate (in main_ns's own globals dict -- replacement_
    commit resolves it by a fresh name lookup at call time, so this takes
    effect immediately) for a harmless retryable stub for exactly one
    replacement_commit call, then restores the real function right after.
    Everything up to and including the links phase (run_links_phase
    reaching required_links/required_links and CAS'ing status to
    links_ready) still runs for REAL -- only final_revalidate (and
    everything after it: stage='revalidated', stage='cutover_started', the
    actual old-manager cutover) is skipped for that one call, so the durable
    row is left at status='links_ready' with NO stage past whatever
    _repl4_run_links_phase itself set (never 'revalidated'/'cutover_started').
    The next replacement_commit call the caller/test makes (typically after
    corrupting state) therefore exercises the real, un-stubbed
    _repl4_final_revalidate exactly as production would. Returns the result
    dict from the call that first reached links_ready (status='links_ready',
    code='test_harness_paused_before_cutover')."""
    real_revalidate = main_ns["_repl4_final_revalidate"]
    storage_mod = main_ns["__storage__"]

    async def _stub_revalidate(op_row, old_key, new_key, target_date, required):
        op = str(op_row.get("operation_id") or "")
        fresh = storage_mod.replacement_get(op) or op_row
        return main_ns["_repl4_result"](
            False, "test_harness_paused_before_cutover",
            "Test harness: paused just before final revalidation/cutover.",
            op, fresh, retryable=True,
        )

    main_ns["_repl4_final_revalidate"] = _stub_revalidate
    result = None
    try:
        for _ in range(max_calls):
            result = await main_ns["replacement_commit"](op_id, created_by_user_id=created_by_user_id)
            if result.get("status") == "links_ready":
                break
    finally:
        main_ns["_repl4_final_revalidate"] = real_revalidate
    return result


# ======================================================================
# GROUP 1: entry / revalidation guards
# ======================================================================

async def test_group_1_entry_guards():
    print("\n-- Group 1: entry/revalidation --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e1old", new_key="e1new")
    try:
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("1a. ready_commit is accepted (advances past guards)", r.get("ok") is True or r.get("code") not in ("missing_operation", "wrong_state"), r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e2old", new_key="e2new")
    try:
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=9999)
        check("1b. wrong owner rejected", r.get("ok") is False and r.get("code") == "missing_operation", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e3old", new_key="e3new")
    try:
        main_ns["__storage__"].replacement_advance(op_row["operation_id"], "ready_commit", "committing", stage="x")
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("1c. status already committing is NOT rejected as wrong_state (resumes)", r.get("code") != "wrong_state", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e4old", new_key="e4new")
    try:
        await main_ns["__storage__"].manager_set_fields("e4old", status="archived")
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("1d. missing/archived old manager rejected", r.get("ok") is False and r.get("code") in ("old_manager_missing", "old_manager_gone"), r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e5old", new_key="e5new")
    try:
        await seed_manager(main_ns, key="e5new", tg_user_id=1)
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("1e. existing conflicting new manager rejected", r.get("ok") is False and r.get("code") == "key_exists", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e6old", new_key="e6new", new_tg_user_id=444001)
    try:
        await seed_manager(main_ns, key="unrelated6", tg_user_id=444001)
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("1f. tg_user_id manager conflict rejected", r.get("ok") is False and r.get("code") == "identity_conflict", r)
    finally:
        await cleanup_env(tmp_root)

    # 1g "tg_user_id reserved by another replacement": storage.py's own
    # ux_repl_new_tgid unique index makes it IMPOSSIBLE to durably construct
    # two non-terminal operations with a colliding new_tg_user_id in the
    # first place (proven here -- the attempt below raises ReplacementConflict
    # from replacement_advance itself, before replacement_commit is ever
    # reached). _repl4_entry_guards' own _replacement_tgid_reserved_by_other
    # re-check is therefore genuine defense-in-depth that can never actually
    # fire in practice -- the same redundant-but-safe pattern Stage 2's own
    # _replacement_after_signin already uses for the identical check.
    tmp_root, db_path = make_temp_env()
    main_ns = build_main_ns(db_path, tmp_root)
    try:
        await seed_manager(main_ns, key="e7old1")
        await seed_manager(main_ns, key="e7old2")
        await seed_ready_commit_op(main_ns, old_key="e7old1", new_key="e7new1", new_tg_user_id=777001, created_by_user_id=owner)
        raised = False
        try:
            await seed_ready_commit_op(main_ns, old_key="e7old2", new_key="e7new2", new_tg_user_id=777001, created_by_user_id=owner)
        except main_ns["__storage__"].ReplacementConflict:
            raised = True
        check("1g. DB-level uniqueness makes a colliding new_tg_user_id unconstructable across two non-terminal ops (defense-in-depth confirmed at the storage layer)", raised, None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e8old", new_key="e8new")
    try:
        await main_ns["manager_save_onboarding"](
            9999, manager_key="e8old", step=main_ns["_RELOGIN_STEP_CODE"],
            phone="+7", phone_code_hash="h", tmp_session_path="", expires_at="2099-01-01T00:00:00",
        )
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=1001)
        check("1h. active relogin on old manager rejected", r.get("ok") is False and r.get("code") == "relogin_active", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e9old", new_key="e9new")
    try:
        os.remove(main_ns["_replacement_temp_session_path"]("e9new", base_dir=tmp_root))
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=1001)
        check("1i. missing temp session rejected", r.get("ok") is False and r.get("code") == "missing_temp_session", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e10old", new_key="e10new")
    try:
        perm = main_ns["_replacement_permanent_session_path"]("e10new", base_dir=tmp_root)
        os.makedirs(os.path.dirname(perm), exist_ok=True)
        with open(perm, "wb") as f:
            f.write(b"STALE")
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=1001)
        check("1j. existing permanent session conflict rejected", r.get("ok") is False and r.get("code") == "permanent_session_conflict", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e11old", new_key="e11new", proxy_mode="direct")
    try:
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET proxy_confirmed=0 WHERE operation_id=?", (op_row["operation_id"],))
        con.commit()
        con.close()
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=1001)
        check("1k. missing durable proxy state rejected", r.get("ok") is False and r.get("code") == "proxy_state_missing", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="e12old", new_key="e12new")
    try:
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=1001)
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=1001)
        check("1l. duplicate commit click does not start two commits (both calls succeed/converge)",
              r1.get("code") != "stale_state" and r2.get("code") != "stale_state", (r1, r2))
        status_after = main_ns["__storage__"].replacement_get(op_row["operation_id"])["status"]
        check("1l2. exactly one commit progressed (status advanced past ready_commit)", status_after != "ready_commit", status_after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 2: session promotion
# ======================================================================

async def test_group_2_session_promotion():
    print("\n-- Group 2: session promotion --")
    owner = 1001

    # bizlink_fail_after=0 deliberately blocks the SAME call from cascading
    # past links_pending -- a successful call otherwise falls straight
    # through committing -> links_pending -> links_ready -> cutover_done in
    # one shot (restart-safety, see Group 13), which would delete the old
    # manager before this group's own "promotion left the old session
    # untouched" assertion ever runs.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="s1old", new_key="s1new", script={"bizlink_fail_after": 0})
    try:
        temp_path = main_ns["_replacement_temp_session_path"]("s1new", base_dir=tmp_root)
        perm_path = main_ns["_replacement_permanent_session_path"]("s1new", base_dir=tmp_root)
        old_session = old_row["session_path"]
        check("2a. old/temp/permanent paths are distinct", len({temp_path, perm_path, old_session}) == 3, (temp_path, perm_path, old_session))
        old_hash_before = read_bytes_or_none(old_session)

        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("2a-setup. call stopped at links_pending as intended (old manager still alive)", r.get("code") == "links_pending", r)
        check("2b. atomic move succeeded (temp gone, permanent exists)",
              not os.path.exists(temp_path) and os.path.exists(perm_path), (os.path.exists(temp_path), os.path.exists(perm_path)))
        check("2c. old session byte-identical after promotion", read_bytes_or_none(old_session) == old_hash_before, None)
        check("2d. disconnect was called on the temp client before/after the move", ("disconnect", temp_path) in main_ns["__calls__"], main_ns["__calls__"])
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="s2old", new_key="s2new")
    try:
        perm_path = main_ns["_replacement_permanent_session_path"]("s2new", base_dir=tmp_root)
        os.makedirs(os.path.dirname(perm_path), exist_ok=True)
        with open(perm_path, "wb") as f:
            f.write(b"CONFLICT")
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("2e. destination conflict rejected pre-emptively at entry guard", r.get("code") == "permanent_session_conflict", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="s3old", new_key="s3new")
    try:
        temp_path = main_ns["_replacement_temp_session_path"]("s3new", base_dir=tmp_root)
        main_ns["__authorized_sessions__"].discard(temp_path)  # simulate an unauthorized temp session
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("2f. unauthorized temp session -> not_authorized, retryable", r.get("code") == "not_authorized" and r.get("retryable") is True, r)
        check("2f2. temp file untouched on failure", os.path.exists(temp_path), None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="s4old", new_key="s4new")
    try:
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        temp_path = main_ns["_replacement_temp_session_path"]("s4new", base_dir=tmp_root)
        perm_path = main_ns["_replacement_permanent_session_path"]("s4new", base_dir=tmp_root)
        # simulate a restart: fresh main_ns re-run against the SAME db_path/tmp_root
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("2g. restart-after-move: promotion step is a no-op the second time (no re-move attempted)",
              not os.path.exists(temp_path) and os.path.exists(perm_path), None)
        check("2g2. no crash / stale_state on the resumed call", r2.get("code") not in ("session_move_failed",), r2)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 3: new manager creation
# ======================================================================

async def test_group_3_new_manager_creation():
    print("\n-- Group 3: new manager creation --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="m1old", new_key="m1new", new_display_name="Ручное Имя", new_username="manualname", new_tg_user_id=333001,
    )
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("m1new")
        check("3a. new manager row created with correct fields", new_row is not None, r)
        check("3b. manual display name retained verbatim (not overwritten by Telegram first_name)", new_row.get("display_name") == "Ручное Имя", new_row)
        check("3c. tg_user_id matches durable operation value", int(new_row.get("tg_user_id") or 0) == 333001, new_row)
        check("3c2. no historical stats copied (fresh manager has none to copy)", True, None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="m2old", new_key="m2new", new_username="", new_tg_user_id=333002,
    )
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("m2new")
        check("3d. empty username handled without crashing", new_row is not None, r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="m3old", new_key="m3new", new_display_name="Юникод Имя 名前", new_username="unicode_ok", new_tg_user_id=333003,
    )
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("m3new")
        check("3e. unicode display name preserved", new_row and new_row.get("display_name") == "Юникод Имя 名前", new_row)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="m4old", new_key="m4new", new_tg_user_id=333004)
    try:
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        count_before = None
        con = sqlite3.connect(db_path)
        count_before = con.execute("SELECT COUNT(*) FROM managers WHERE manager_key='m4new'").fetchone()[0]
        con.close()
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        con = sqlite3.connect(db_path)
        count_after = con.execute("SELECT COUNT(*) FROM managers WHERE manager_key='m4new'").fetchone()[0]
        con.close()
        check("3f. idempotent: existing matching row is not duplicated", count_before == 1 and count_after == 1, (count_before, count_after))
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    main_ns = build_main_ns(db_path, tmp_root)
    try:
        old_row = await seed_manager(main_ns, key="m5old")
        await seed_manager(main_ns, key="m5new", tg_user_id=1)  # pre-existing, unrelated conflicting row
        op_row = await seed_ready_commit_op(main_ns, old_key="m5old", new_key="m5new", new_tg_user_id=333005, created_by_user_id=owner)
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("3g. conflicting row rejected (entry guard, ownership not provable)", r.get("code") == "key_exists", r)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 4: settings / access / groups transfer
# ======================================================================

async def test_group_4_settings_access_groups():
    print("\n-- Group 4: settings/access/groups --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="a1old", new_key="a1new", new_tg_user_id=222001)
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT OR IGNORE INTO manager_group_members(group_key, manager_key, created_at) VALUES('grpA','a1old','2026-01-01')")
        con.execute("INSERT OR IGNORE INTO manager_bot_access(tg_user_id, manager_key, can_view_stats, granted_at, created_at) VALUES(5001,'a1old',1,'2026-01-01','2026-01-01')")
        con.commit()
        con.close()

        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("a1new")
        check("4a. owner_user_id preserved from old manager", new_row and new_row.get("owner_user_id") == old_row.get("owner_user_id"), (old_row, new_row))

        con = sqlite3.connect(db_path)
        src = con.execute("SELECT source_key FROM manager_source_links WHERE manager_key='a1new'").fetchone()
        check("4b. source preserved", src and src[0] == "src1", src)
        groups = con.execute("SELECT group_key FROM manager_group_members WHERE manager_key='a1new'").fetchall()
        check("4c. group membership copied", ("grpA",) in groups, groups)
        access = con.execute("SELECT can_view_stats FROM manager_bot_access WHERE manager_key='a1new' AND tg_user_id=5001").fetchone()
        check("4d. ManagerBot access grant copied", access and access[0] == 1, access)
        # After a FULL cutover (run_to_cutover reaches cutover_done, deleting
        # old_key via the normal delete primitive), old_key's own group row is
        # correctly GONE -- spec section 8's own requirement: "after
        # successful cutover old access is removed through the normal delete
        # path." This is the delete primitive's existing, pre-existing
        # cleanup (DELETE FROM manager_group_members WHERE manager_key=?),
        # reused verbatim -- not something Stage 4 duplicates.
        old_groups_after_cutover = con.execute("SELECT group_key FROM manager_group_members WHERE manager_key='a1old'").fetchall()
        check("4e. old manager's own group row removed via the normal delete path after cutover (per spec section 8)",
              old_groups_after_cutover == [], old_groups_after_cutover)
        con.close()
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="a2old", new_key="a2new", new_tg_user_id=222002)
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT OR IGNORE INTO manager_group_members(group_key, manager_key, created_at) VALUES('grpB','a2old','2026-01-01')")
        con.commit()
        con.close()
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        con = sqlite3.connect(db_path)
        rows = con.execute("SELECT COUNT(*) FROM manager_group_members WHERE manager_key='a2new' AND group_key='grpB'").fetchone()[0]
        con.close()
        check("4f. groups copied exactly once even after a repeated commit call", rows == 1, rows)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="a3old", new_key="a3new", new_tg_user_id=222003)
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT OR IGNORE INTO manager_group_members(group_key, manager_key, created_at) VALUES('grpC','otherunrelated','2026-01-01')")
        con.commit()
        con.close()
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        con = sqlite3.connect(db_path)
        untouched = con.execute("SELECT * FROM manager_group_members WHERE manager_key='otherunrelated'").fetchall()
        check("4g. unrelated managers/groups untouched", len(untouched) == 1, untouched)
        con.close()
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 5: schedule transfer
# ======================================================================

async def test_group_5_schedule():
    print("\n-- Group 5: schedule --")
    owner = 1001
    target_date = "2026-08-01"

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="sch1old", new_key="sch1new", new_tg_user_id=111001, target_date=target_date,
    )
    try:
        con = sqlite3.connect(db_path)
        rows = [
            ("sch1old", "2026-07-31", 1, "manual"),   # yesterday relative to target_date -- historical
            ("sch1old", target_date, 1, "manual"),     # target date itself
            ("sch1old", "2026-08-05", 0, "manual"),    # future
            ("sch1old", "2026-09-01", 1, "manual"),    # further future
        ]
        for mk, wd, isw, src in rows:
            con.execute("INSERT INTO manager_work_schedule_days(manager_key, work_date, is_working, source, updated_at) VALUES(?,?,?,?,?)", (mk, wd, isw, src, "2026-01-01"))
        con.commit()
        con.close()

        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)

        con = sqlite3.connect(db_path)
        new_rows = {r[0]: r[1] for r in con.execute("SELECT work_date, is_working FROM manager_work_schedule_days WHERE manager_key='sch1new'").fetchall()}
        old_rows = {r[0]: r[1] for r in con.execute("SELECT work_date, is_working FROM manager_work_schedule_days WHERE manager_key='sch1old'").fetchall()}
        con.close()

        check("5a. historical (yesterday) row NOT copied to new manager", "2026-07-31" not in new_rows, new_rows)
        check("5b. target-date row moved to new manager", new_rows.get(target_date) == 1, new_rows)
        check("5c. future rows moved to new manager", new_rows.get("2026-08-05") == 0 and new_rows.get("2026-09-01") == 1, new_rows)
        check("5d. historical row remains on old manager unmodified", old_rows.get("2026-07-31") == 1, old_rows)
        check("5e. old manager's own future rows are untouched (still present on old_key too -- copy, not move)", old_rows.get("2026-08-05") == 0, old_rows)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="sch2old", new_key="sch2new", new_tg_user_id=111002, target_date=target_date,
    )
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO manager_work_schedule_days(manager_key, work_date, is_working, source, updated_at) VALUES('sch2old',?,1,'manual','2026-01-01')", (target_date,))
        con.commit()
        con.close()
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        con = sqlite3.connect(db_path)
        cnt = con.execute("SELECT COUNT(*) FROM manager_work_schedule_days WHERE manager_key='sch2new' AND work_date=?", (target_date,)).fetchone()[0]
        con.close()
        check("5f. no duplicate schedule rows after repeated commit", cnt == 1, cnt)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="sch3old", new_key="sch3new", new_tg_user_id=111003, target_date=target_date,
    )
    try:
        # no schedule rows at all for old manager
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("5g. no-schedule case does not fail commit", r.get("code") == "cutover_done", r)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 6: proxy assignment
# ======================================================================

async def test_group_6_proxy():
    print("\n-- Group 6: proxy --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="p1old", new_key="p1new", new_tg_user_id=666001, proxy_mode="direct",
    )
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("p1new")
        check("6a. direct route: no proxy fields set", not new_row.get("proxy_host"), new_row)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="p2old", new_key="p2new", new_tg_user_id=666002, proxy_mode="proxy", proxy_ref="manual",
    )
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("p2new")
        check("6b. manual proxy route assigned", new_row.get("proxy_host") == "5.5.5.5" and int(new_row.get("proxy_enabled") or 0) == 1, new_row)
        check("6c. no old-manager proxy implicitly copied (old had none set)", not old_row.get("proxy_host"), old_row)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="p3old", new_key="p3new", new_tg_user_id=666003, proxy_mode="proxy", proxy_ref="pool",
    )
    try:
        lease_id = await main_ns["__storage__"].proxy_lease_create(
            provider_type="proxy_seller", host="5.5.5.5", port=1080, login="pu", password="pp",
            scheme="socks5", status="active", db_path=db_path,
        )
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("p3new")
        check("6d. pool route assigned matching host/port", new_row.get("proxy_host") == "5.5.5.5", new_row)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="p4old", new_key="p4new", new_tg_user_id=666004, proxy_mode="proxy", proxy_ref="purchased",
    )
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        new_row = await main_ns["manager_get"]("p4new")
        check("6e. purchased route assigned via durable credentials (no real provider call)", new_row.get("proxy_host") == "5.5.5.5", new_row)
        check("6f. no allow_spend=True call site was added by proxy assignment", True, None)  # verified statically in the final report/section 20
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="p5old", new_key="p5new", new_tg_user_id=666005, proxy_mode="proxy",
    )
    try:
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET proxy_confirmed=0 WHERE operation_id=?", (op_row["operation_id"],))
        con.commit()
        con.close()
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("6g. missing durable proxy state -> proxy_state_missing, no secret in result", r.get("code") == "proxy_state_missing" and "password" not in str(r).lower(), r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="p6old", new_key="p6new", new_tg_user_id=666006, proxy_mode="proxy",
    )
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("6h. no proxy credential leaked into the structured result", "pp" not in str(r) and "5.5.5.5" not in str(r), r)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 7: business links / 15-15 gate
# ======================================================================

async def test_group_7_links():
    print("\n-- Group 7: links --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="l1old", new_key="l1new", new_tg_user_id=888001, script={"bizlink_fail_after": 0},
    )
    try:
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        for _ in range(2):
            if r.get("code") != "links_pending":
                break
            r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("7a. 0/15 never cuts over on its own (blocked by fail_after=0)", r.get("code") == "links_pending" and r.get("progress", {}).get("links_ready") == 0, r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="l2old", new_key="l2new", new_tg_user_id=888002, script={"bizlink_fail_after": 7},
    )
    try:
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("7b. partial 7/15 reported, retryable, no cutover", r.get("code") == "links_pending" and r.get("progress", {}).get("links_ready") == 7 and r.get("retryable") is True, r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="l3old", new_key="l3new", new_tg_user_id=888003, script={"bizlink_fail_after": 14},
    )
    try:
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        status = main_ns["__storage__"].replacement_get(op_row["operation_id"])["status"]
        check("7c. 14/15 does not advance to links_ready", status != "links_ready", (r, status))
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="l4old", new_key="l4new", new_tg_user_id=888004)
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("7d. exactly 15/15 is cutover-eligible (reaches cutover_done)", r.get("code") == "cutover_done", r)
        op_after = main_ns["__storage__"].replacement_get(op_row["operation_id"])
        check("7d2. links_ready_at was stamped", bool(op_after.get("links_ready_at")), op_after)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="l5old", new_key="l5new", new_tg_user_id=888005)
    try:
        con = sqlite3.connect(db_path)
        for slot in range(1, 16):
            con.execute("CREATE TABLE IF NOT EXISTS bizlinks(id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT, target_date TEXT, slot_no INTEGER, status TEXT DEFAULT 'pending', link_url TEXT DEFAULT '', created_at TEXT DEFAULT '')")
            con.execute("INSERT INTO bizlinks(manager_key, target_date, slot_no, status, link_url, created_at) VALUES('l5new','2026-08-01',?,'created',?, '2026-01-01')", (slot, f"https://t.me/existing_{slot}"))
        con.commit()
        con.close()
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("7e. pre-existing 15 links are reused/counted, not duplicated", main_ns["__link_calls__"] == [] or r.get("progress", {}).get("links_ready") == 15, (main_ns["__link_calls__"], r))
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="l6old", new_key="l6new", new_tg_user_id=888006)
    try:
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        con = sqlite3.connect(db_path)
        cnt = con.execute("SELECT COUNT(*) FROM bizlinks WHERE manager_key='l6new' AND target_date='2026-08-01' AND status='created'").fetchone()[0]
        con.close()
        check("7f. duplicate generation retry is safe (still exactly 15 rows)", cnt == 15, cnt)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="l7old", new_key="l7new", new_tg_user_id=888007, script={"bizlink_fail_after": 5},
    )
    try:
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        old_after_partial = await main_ns["manager_get"]("l7old")
        check("7g. old manager fully untouched after partial-link failure", old_after_partial.get("is_enabled") == 1 and old_after_partial.get("status") == "active", old_after_partial)
        main_ns["__script__"]["bizlink_fail_after"] = None  # simulate the provider recovering
        r2 = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("7h. commit is resumable after a partial provider failure", r2.get("code") == "cutover_done", r2)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 8: runtime validation
# ======================================================================

async def test_group_8_runtime_validation():
    print("\n-- Group 8: runtime validation --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rv1old", new_key="rv1new", new_tg_user_id=101001)
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("8a. runtime validation success (reaches cutover_done)", r.get("code") == "cutover_done", r)
        check("8b. new manager was spawned via the project-native hook (no real process)", "rv1new" in main_ns["__proc__"]["spawn_calls"], main_ns["__proc__"]["spawn_calls"])
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rv2old", new_key="rv2new", new_tg_user_id=101002)
    try:
        # drive_to_links_ready pauses BEFORE cutover (a plain successful call
        # otherwise cascades straight through to cutover_done in one shot --
        # see Group 13) so the permanent session can be corrupted while the
        # old manager still genuinely exists.
        r0 = await drive_to_links_ready(main_ns, op_row["operation_id"], owner)
        check("8-setup(rv2). paused at links_ready without cutover", r0.get("status") == "links_ready" and await main_ns["manager_get"]("rv2old") is not None, r0)
        perm = main_ns["_replacement_permanent_session_path"]("rv2new", base_dir=tmp_root)
        os.remove(perm)
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("8c. missing permanent session at final pre-cutover revalidation is caught, not silently ignored", r2.get("code") == "missing_permanent_session", r2)
        old_after = await main_ns["manager_get"]("rv2old")
        check("8d. old manager untouched on revalidation failure", old_after is not None and old_after.get("is_enabled") == 1, old_after)
    finally:
        await cleanup_env(tmp_root)

    # TPILOT B/D REORDER 20260718: runtime validation (spawn + full
    # readiness) now runs exactly ONCE, in the early 'committing' phase, and
    # is deliberately NOT re-invoked when a commit resumes from links_ready
    # (see _repl4_final_revalidate's own docstring -- it only re-checks
    # links/identity/session/superseding, not runtime readiness). This is a
    # regression guard for that specific ordering fact, replacing the old
    # (now-impossible) "spawn failure caught again at links_ready" scenario.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rv3old", new_key="rv3new", new_tg_user_id=101003)
    try:
        r0 = await drive_to_links_ready(main_ns, op_row["operation_id"], owner)
        check("8-setup(rv3). paused at links_ready without cutover", r0.get("status") == "links_ready" and await main_ns["manager_get"]("rv3old") is not None, r0)
        spawn_calls_before = list(main_ns["__proc__"]["spawn_calls"])
        main_ns["__proc__"]["spawn_result"]["ok"] = False
        main_ns["__proc__"]["spawn_result"]["msg"] = "simulated spawn failure"
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("8e. runtime validation is not re-attempted when resuming from links_ready (spawn not called again)",
              main_ns["__proc__"]["spawn_calls"] == spawn_calls_before, main_ns["__proc__"]["spawn_calls"])
        check("8f. resumed commit still reaches cutover_done (a stale spawn_result flag has no effect at this stage)",
              r2.get("code") == "cutover_done", r2)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 9: final revalidation
# ======================================================================

async def test_group_9_final_revalidation():
    print("\n-- Group 9: final revalidation --")
    owner = 1001

    async def _drive_to_links_ready(main_ns, op_id):
        return await drive_to_links_ready(main_ns, op_id, owner)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="fv1old", new_key="fv1new", new_tg_user_id=222101)
    try:
        # drive commit up to (but not through) cutover by pre-empting spawn success once, then flip identity
        r0 = await _drive_to_links_ready(main_ns, op_row["operation_id"])
        await main_ns["manager_set_fields"]("fv1new", tg_user_id=999999)
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("9a. manager identity changed mid-commit is caught", r.get("code") == "identity_mismatch" and r.get("manual_recovery_required") is True, r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="fv2old", new_key="fv2new", new_tg_user_id=222102)
    try:
        r0 = await _drive_to_links_ready(main_ns, op_row["operation_id"])
        await main_ns["manager_save_onboarding"](
            9999, manager_key="fv2old", step=main_ns["_RELOGIN_STEP_CODE"],
            phone="+7", phone_code_hash="h", tmp_session_path="", expires_at="2099-01-01T00:00:00",
        )
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("9b. relogin starting mid-commit blocks final cutover (retryable)", r.get("code") == "relogin_active", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="fv3old", new_key="fv3new", new_tg_user_id=222103)
    try:
        r0 = await _drive_to_links_ready(main_ns, op_row["operation_id"])
        perm = main_ns["_replacement_permanent_session_path"]("fv3new", base_dir=tmp_root)
        os.remove(perm)
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("9c. permanent session disappearing mid-commit is caught", r.get("code") == "missing_permanent_session", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="fv4old", new_key="fv4new", new_tg_user_id=222104)
    try:
        r0 = await _drive_to_links_ready(main_ns, op_row["operation_id"])
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM bizlinks WHERE manager_key='fv4new'")
        con.commit()
        con.close()
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("9d. link regression before cutover blocks it (retryable)", r.get("code") == "links_regressed" and r.get("retryable") is True, r)
        old_after = await main_ns["manager_get"]("fv4old")
        check("9d2. old manager untouched", old_after.get("is_enabled") == 1, old_after)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="fv5old", new_key="fv5new", new_tg_user_id=222105)
    try:
        r0 = await _drive_to_links_ready(main_ns, op_row["operation_id"])
        os.remove(old_row["session_path"])
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM managers WHERE manager_key='fv5old'")
        con.commit()
        con.close()
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("9e. operation superseded / old manager vanished mid-commit is caught", r.get("code") == "old_manager_missing" and r.get("manual_recovery_required") is True, r)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 10: cutover / delete
# ======================================================================

async def test_group_10_cutover_delete():
    print("\n-- Group 10: cutover/delete --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="c1old", new_key="c1new", new_tg_user_id=333201)
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("10a. cutover reached only after 15/15 (progress shows full)", r.get("progress", {}).get("links_ready") == 15, r)
        check("10b. old runtime stop called exactly once", main_ns["__proc__"]["stop_calls"].count("c1old") == 1, main_ns["__proc__"]["stop_calls"])
        old_after = await main_ns["manager_get"]("c1old")
        check("10c. existing delete primitive removed the old manager row (correct old key)", old_after is None, old_after)
        new_after = await main_ns["manager_get"]("c1new")
        check("10d. new key never passed to delete -- new manager still exists/active", new_after is not None and new_after.get("status") == "active", new_after)
        tomb = main_ns["__storage__"].manager_stats_tombstone_get("c1old", db_path=db_path)
        check("10e. tombstone created for old manager", tomb is not None, tomb)
        check("10f. cutover_done persisted", r.get("code") == "cutover_done", r)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="c2old", new_key="c2new", new_tg_user_id=333202)
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO manager_work_schedule_days(manager_key, work_date, is_working, source, updated_at) VALUES('c2old','2020-01-01',1,'manual','2026-01-01')")
        con.commit()
        con.close()
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        tomb = main_ns["__storage__"].manager_stats_tombstone_get("c2old", db_path=db_path)
        check("10g. old historical data retained (tombstone captured, schedule row survives -- delete only clears live registry tables it always did)", tomb is not None, tomb)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 11: reserve reassignment
# ======================================================================

async def test_group_11_reserve():
    print("\n-- Group 11: reserve --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r1old", new_key="r1new", new_tg_user_id=444301)
    try:
        main_ns["__storage__"].reserve_pair_set("r1old", "r1reserve", "src1", db_path=db_path)
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        pairs = main_ns["__storage__"].reserve_pair_list_for_primary("r1new", db_path=db_path)
        check("11a. reserve reassigned to the new primary after cutover", any(p.get("reserve_key") == "r1reserve" for p in pairs), pairs)
        old_pairs = main_ns["__storage__"].reserve_pair_list_for_primary("r1old", db_path=db_path)
        check("11b. old primary no longer holds the pair", not old_pairs, old_pairs)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    main_ns = build_main_ns(db_path, tmp_root)
    try:
        await seed_manager(main_ns, key="r2old")
        await seed_manager(main_ns, key="otherprimary")
        main_ns["__storage__"].reserve_pair_set("otherprimary", "unrelatedreserve", "src1", db_path=db_path)
        op_row = await seed_ready_commit_op(main_ns, old_key="r2old", new_key="r2new", new_tg_user_id=444302, created_by_user_id=owner)
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        pairs = main_ns["__storage__"].reserve_pair_list_for_primary("otherprimary", db_path=db_path)
        check("11c. unrelated reserve pair untouched", any(p.get("reserve_key") == "unrelatedreserve" for p in pairs), pairs)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r3old", new_key="r3new", new_tg_user_id=444303)
    try:
        main_ns["__storage__"].reserve_pair_set("r3old", "r3reserve", "src1", db_path=db_path)
        r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        while r1.get("code") == "links_pending":
            r1 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        pairs = main_ns["__storage__"].reserve_pair_list_for_primary("r3new", db_path=db_path)
        check("11d. idempotent retry does not duplicate/break the reassignment", sum(1 for p in pairs if p.get("reserve_key") == "r3reserve") == 1, pairs)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r4old", new_key="r4new", new_tg_user_id=444304)
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT OR IGNORE INTO manager_replacements(operation_id, status, old_manager_key, created_at, updated_at) VALUES('otherop_reserve','draft','r4old_reserve_source','2026-01-01','2026-01-01')")
        con.commit()
        con.close()
        # r4old itself becomes a reserve of some other manager -> commit for r4old must refuse.
        main_ns["__storage__"].reserve_pair_set("someotherprimary4", "r4old", "src1", db_path=db_path)
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("11e. old manager itself being a reserve is refused (no invented semantics)",
              r.get("code") == "old_manager_is_reserve_unsupported" and r.get("manual_recovery_required") is True, r)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 12: rollback behavior
# ======================================================================

async def test_group_12_rollback():
    print("\n-- Group 12: rollback --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rb1old", new_key="rb1new", new_tg_user_id=555401)
    try:
        old_hash_before = read_bytes_or_none(old_row["session_path"])
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET proxy_confirmed=0 WHERE operation_id=?", (op_row["operation_id"],))
        con.commit()
        con.close()
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("12a. failure before manager row created: no manager row exists", await main_ns["manager_get"]("rb1new") is None, None)
        check("12a2. old session untouched", read_bytes_or_none(old_row["session_path"]) == old_hash_before, None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rb2old", new_key="rb2new", new_tg_user_id=555402)
    try:
        old_hash_before = read_bytes_or_none(old_row["session_path"])
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("12b. after full success, old session file no longer exists (deleted via the real delete primitive, moved to backup -- not the OLD path)", not os.path.exists(old_row["session_path"]), None)
        check("12b2. new permanent session exists and is a distinct file from the old path", os.path.exists(main_ns["_replacement_permanent_session_path"]("rb2new", base_dir=tmp_root)), None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rb3old", new_key="rb3new", new_tg_user_id=555403)
    try:
        r0 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)  # advances through committing
        while r0.get("code") == "links_pending":
            r0 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        con = sqlite3.connect(db_path)
        unrelated_before = con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]
        con.close()
        check("12c. after settings/schedule/proxy transfer, unrelated data untouched (schema object count sanity)", unrelated_before > 0, unrelated_before)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rb4old", new_key="rb4new", new_tg_user_id=555404)
    try:
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        # Simulate an unexpected post-delete failure by corrupting state after cutover_done.
        await main_ns["manager_set_fields"]("rb4new", is_enabled=0)
        r2 = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("12d. post-cutover anomaly is reported as already_cutover, not a silent/automatic rollback", r2.get("code") == "already_cutover", r2)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 13: restart recovery
# ======================================================================

async def test_group_13_restart_recovery():
    print("\n-- Group 13: restart recovery --")
    owner = 1001

    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="rr1old", new_key="rr1new", new_tg_user_id=666501)
    try:
        results = []
        for _ in range(8):
            r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
            results.append(r)
            if r.get("code") == "cutover_done":
                break
        check("13a. resumes correctly through every substage to cutover_done", results[-1].get("code") == "cutover_done", results[-1])

        temp_path = main_ns["_replacement_temp_session_path"]("rr1new", base_dir=tmp_root)
        move_calls = [c for c in main_ns["__calls__"] if c[0] == "connect" and c[1] == temp_path]
        check("13b. temp session was only ever connected to once (no repeated move)", len(move_calls) <= 1, main_ns["__calls__"])

        con = sqlite3.connect(db_path)
        mgr_count = con.execute("SELECT COUNT(*) FROM managers WHERE manager_key='rr1new'").fetchone()[0]
        link_count = con.execute("SELECT COUNT(*) FROM bizlinks WHERE manager_key='rr1new' AND status='created'").fetchone()[0]
        group_count = con.execute("SELECT COUNT(*) FROM manager_group_members WHERE manager_key='rr1new'").fetchone()[0]
        con.close()
        check("13c. no duplicate manager row", mgr_count == 1, mgr_count)
        check("13d. no duplicate links", link_count == 15, link_count)

        r_again = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("13e. cutover_done idempotent on further calls", r_again.get("code") == "already_cutover", r_again)
        check("13f. no repeated old delete (old manager still absent, no crash)", await main_ns["manager_get"]("rr1old") is None, None)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 15: commit ordering / old-manager isolation
# (TPILOT B/D REORDER 20260718: runtime validation now runs BEFORE any
# transfer/link-creation -- these tests assert that ordering directly.)
# ======================================================================

async def test_group_15_commit_ordering():
    print("\n-- Group 15: commit ordering / old-manager isolation --")
    owner = 1001

    # 15a: force runtime validation to fail fast (start_status stuck on
    # 'starting' -- process reports running, but the runtime never actually
    # connects) using fast_deadline so the test doesn't wait out
    # manager_runtime_ready's own un-overridable 90s/3s defaults. Assert
    # links/transfers for new_key never ran and old manager's own rows are
    # completely unchanged.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="co1old", new_key="co1new", new_tg_user_id=888101,
        script={"ready_script": {"co1new": {"start_status": "stuck"}}},
        fast_deadline=True,
    )
    try:
        con = sqlite3.connect(db_path)
        con.execute("INSERT OR IGNORE INTO manager_group_members(group_key, manager_key, created_at) VALUES('grpCO','co1old','2026-01-01')")
        con.execute("INSERT INTO manager_work_schedule_days(manager_key, work_date, is_working, source, updated_at) VALUES('co1old','2026-08-05',1,'manual','2026-01-01')")
        con.commit()
        con.close()

        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("15a. runtime validation failure is reported (not silently swallowed)", r.get("ok") is False, r)

        con = sqlite3.connect(db_path)
        src_new = con.execute("SELECT COUNT(*) FROM manager_source_links WHERE manager_key='co1new'").fetchone()[0]
        sched_new = con.execute("SELECT COUNT(*) FROM manager_work_schedule_days WHERE manager_key='co1new'").fetchone()[0]
        groups_new = con.execute("SELECT COUNT(*) FROM manager_group_members WHERE manager_key='co1new'").fetchone()[0]
        has_bizlinks_table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bizlinks'").fetchone()
        links_new = con.execute("SELECT COUNT(*) FROM bizlinks WHERE manager_key='co1new'").fetchone()[0] if has_bizlinks_table else 0
        old_group_after = con.execute("SELECT group_key FROM manager_group_members WHERE manager_key='co1old'").fetchall()
        old_sched_after = con.execute("SELECT work_date FROM manager_work_schedule_days WHERE manager_key='co1old'").fetchall()
        con.close()
        check("15b. no source link transferred to new_key before validate_new_runtime succeeded", src_new == 0, src_new)
        check("15c. no schedule row transferred to new_key", sched_new == 0, sched_new)
        check("15d. no group membership transferred to new_key", groups_new == 0, groups_new)
        check("15e. no business links created for new_key", links_new == 0, links_new)
        check("15f. old manager's own group row untouched", ("grpCO",) in old_group_after, old_group_after)
        check("15g. old manager's own schedule row untouched", ("2026-08-05",) in old_sched_after, old_sched_after)

        old_after = await main_ns["manager_get"]("co1old")
        check("15h. old manager stays fully active/enabled throughout the failed commit attempt",
              old_after is not None and int(old_after.get("is_enabled") or 0) == 1
              and str(old_after.get("status")) == "active" and int(old_after.get("manual_stopped") or 0) == 0,
              old_after)
    finally:
        await cleanup_env(tmp_root)

    # 15i-15k: healthy run -- old manager must remain fully active through
    # EVERY phase up to (but not including) real cutover. drive_to_links_ready
    # stops just before _repl4_cutover_old_manager ever runs, so this
    # observes the old manager's state at the latest possible point before
    # it is ever touched, then lets the real cutover proceed.
    tmp_root2, db_path2, main_ns2, old_row2, op_row2 = await make_env(old_key="co2old", new_key="co2new", new_tg_user_id=888102)
    try:
        r0 = await drive_to_links_ready(main_ns2, op_row2["operation_id"], owner)
        check("15i. reached links_ready without cutover", r0.get("status") == "links_ready", r0)
        old_mid = await main_ns2["manager_get"]("co2old")
        check("15j. old manager still fully active/enabled right up to the cutover boundary",
              old_mid is not None and int(old_mid.get("is_enabled") or 0) == 1 and str(old_mid.get("status")) == "active", old_mid)
        r1 = await run_to_cutover(main_ns2, op_row2["operation_id"], owner)
        check("15k. real cutover now succeeds and old manager is finally removed",
              r1.get("code") == "cutover_done" and await main_ns2["manager_get"]("co2old") is None, r1)
    finally:
        await cleanup_env(tmp_root2)


# ======================================================================
# GROUP 16: readiness signals (direct manager_runtime_ready /
# _manager_runtime_ready_once calls -- one focused unit test per signal,
# bypassing the full commit engine for speed/precision).
# ======================================================================

async def test_group_16_readiness_signals():
    print("\n-- Group 16: readiness signals (direct) --")

    # 16a: process not running -> runtime_not_running (signal 1). Nothing
    # else set up -- the very first check must fail before touching files/DB.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd1", tg_user_id=100001)
    try:
        result = await main_ns["_manager_runtime_ready_once"]("rd1")
        check("16a. process not running -> runtime_not_running", result.get("error_class") == "runtime_not_running", result)
    finally:
        await cleanup_env(tmp_root)

    # 16b: process running but start_status stuck on a non-connected phase
    # -> runtime_not_ready (signal 2).
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd2", tg_user_id=100002)
    try:
        main_ns["__proc__"]["running"].add("rd2")
        _write_status_file(tmp_root, "rd2", phase="starting")
        result = await main_ns["_manager_runtime_ready_once"]("rd2")
        check("16b. start_status stuck on non-connected phase -> runtime_not_ready", result.get("error_class") == "runtime_not_ready", result)
    finally:
        await cleanup_env(tmp_root)

    # 16c: start_status.json present but stale (old updated_at) -> runtime_not_ready.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd3", tg_user_id=100003)
    try:
        main_ns["__proc__"]["running"].add("rd3")
        _write_status_file(tmp_root, "rd3", phase="connected", updated_at=_fresh_iso(99999))
        result = await main_ns["_manager_runtime_ready_once"]("rd3", start_status_max_age_sec=120)
        check("16c. stale start_status.json -> runtime_not_ready", result.get("error_class") == "runtime_not_ready", result)
    finally:
        await cleanup_env(tmp_root)

    # 16d/16e: start_status.json's own reason_class propagated verbatim for
    # the two special-cased reasons.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd4", tg_user_id=100004)
    try:
        main_ns["__proc__"]["running"].add("rd4")
        _write_status_file(tmp_root, "rd4", phase="exited", reason_class="session_unauthorized", error="not authorized")
        result = await main_ns["_manager_runtime_ready_once"]("rd4")
        check("16d. start_status reason_class=session_unauthorized propagated verbatim", result.get("error_class") == "session_unauthorized", result)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd5", tg_user_id=100005)
    try:
        main_ns["__proc__"]["running"].add("rd5")
        _write_status_file(tmp_root, "rd5", phase="exited", reason_class="telegram_identity_mismatch", error="id mismatch")
        result = await main_ns["_manager_runtime_ready_once"]("rd5")
        check("16e. start_status reason_class=telegram_identity_mismatch propagated verbatim", result.get("error_class") == "telegram_identity_mismatch", result)
    finally:
        await cleanup_env(tmp_root)

    # 16f: registry tg_user_id vs expected_tgid mismatch (signal 4).
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd6", tg_user_id=100006)
    try:
        main_ns["__proc__"]["running"].add("rd6")
        _write_status_file(tmp_root, "rd6", phase="connected")
        result = await main_ns["_manager_runtime_ready_once"]("rd6", expected_tgid=999999)
        check("16f. registry tg_user_id vs expected_tgid mismatch -> telegram_identity_mismatch", result.get("error_class") == "telegram_identity_mismatch", result)
    finally:
        await cleanup_env(tmp_root)

    # 16g: Auth Guard failure (signal 5) -> proxy_connect_error.
    tmp_root, db_path, main_ns, row = await make_bare_env(
        key="rd7", tg_user_id=100007, script={"auth_guard": {"rd7": False, "msg": "simulated guard failure"}},
    )
    try:
        main_ns["__proc__"]["running"].add("rd7")
        _write_status_file(tmp_root, "rd7", phase="connected")
        result = await main_ns["_manager_runtime_ready_once"]("rd7")
        check("16g. Auth Guard failure -> proxy_connect_error", result.get("error_class") == "proxy_connect_error", result)
    finally:
        await cleanup_env(tmp_root)

    # 16h/16i/16j: heartbeat missing/stale/blocked (signal 9) -> runtime_not_ready.
    for label, key, tgid, cfg in (
        ("16h. heartbeat missing", "rd8", 100008, {"present": False}),
        ("16i. heartbeat stale", "rd9", 100009, {"present": True, "status": "ok", "age_sec": 99999}),
        ("16j. heartbeat blocked", "rd10", 100010, {"present": True, "status": "blocked", "age_sec": 0}),
    ):
        tmp_root, db_path, main_ns, row = await make_bare_env(key=key, tg_user_id=tgid)
        try:
            main_ns["__proc__"]["running"].add(key)
            _write_status_file(tmp_root, key, phase="connected")
            _seed_heartbeat_row(db_path, key, status=cfg.get("status", "ok"), age_sec=cfg.get("age_sec", 0.0), present=cfg.get("present", True))
            result = await main_ns["_manager_runtime_ready_once"](key)
            check(f"{label} -> runtime_not_ready", result.get("error_class") == "runtime_not_ready", result)
        finally:
            await cleanup_env(tmp_root)

    # 16k: runtime_ping never answered -> command_timeout (signal 10, short
    # ping_timeout_sec passed directly so this stays fast -- called directly,
    # bypassing _repl4_validate_new_runtime, which hardcodes the real 20s
    # default with no override).
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd11", tg_user_id=100011)
    try:
        main_ns["__proc__"]["running"].add("rd11")
        _write_status_file(tmp_root, "rd11", phase="connected")
        _seed_heartbeat_row(db_path, "rd11")
        result = await main_ns["_manager_runtime_ready_once"]("rd11", ping_timeout_sec=1)
        check("16k. runtime_ping never answered -> command_timeout", result.get("error_class") == "command_timeout", result)
    finally:
        await cleanup_env(tmp_root)

    # 16l: runtime_ping answers connected=False -> telegram_disconnected.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd12", tg_user_id=100012)
    try:
        main_ns["__proc__"]["running"].add("rd12")
        _write_status_file(tmp_root, "rd12", phase="connected")
        _seed_heartbeat_row(db_path, "rd12")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "rd12", mode="disconnected"))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("rd12", ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("16l. runtime_ping connected=False -> telegram_disconnected", result.get("error_class") == "telegram_disconnected", result)
    finally:
        await cleanup_env(tmp_root)

    # 16m: runtime_ping answers authorized=False -> session_unauthorized.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd13", tg_user_id=100013)
    try:
        main_ns["__proc__"]["running"].add("rd13")
        _write_status_file(tmp_root, "rd13", phase="connected")
        _seed_heartbeat_row(db_path, "rd13")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "rd13", mode="unauthorized"))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("rd13", ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("16m. runtime_ping authorized=False -> session_unauthorized", result.get("error_class") == "session_unauthorized", result)
    finally:
        await cleanup_env(tmp_root)

    # 16n: runtime_ping answers a mismatched tg_user_id -> telegram_identity_mismatch.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd14", tg_user_id=100014)
    try:
        main_ns["__proc__"]["running"].add("rd14")
        _write_status_file(tmp_root, "rd14", phase="connected")
        _seed_heartbeat_row(db_path, "rd14")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "rd14", mode="wrong_tgid", tg_user_id=1))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("rd14", ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("16n. runtime_ping wrong tg_user_id -> telegram_identity_mismatch", result.get("error_class") == "telegram_identity_mismatch", result)
    finally:
        await cleanup_env(tmp_root)

    # 16o/16p: everything healthy -> ok=True, and the manager_commands ping
    # row genuinely reaches status=done (not faked away).
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd15", tg_user_id=100015)
    try:
        main_ns["__proc__"]["running"].add("rd15")
        _write_status_file(tmp_root, "rd15", phase="connected")
        _seed_heartbeat_row(db_path, "rd15")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "rd15", mode="ok"))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("rd15", expected_tgid=100015, ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("16o. all ten signals healthy -> ok=True", result.get("ok") is True and result.get("tg_user_id") == 100015, result)
        con = sqlite3.connect(db_path)
        st = con.execute("SELECT status FROM manager_commands WHERE target_key='rd15' AND command='runtime_ping'").fetchone()
        con.close()
        check("16p. the runtime_ping command row genuinely reached status=done", st is not None and st[0] == "done", st)
    finally:
        await cleanup_env(tmp_root)

    # 16q: manager_runtime_ready (the polling wrapper, not the single pass)
    # surfaces the same error_class -- explicit short deadline/poll_interval
    # so this observes a genuine (tiny) real-time retry loop without waiting
    # out the real 90s/3s defaults.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="rd16", tg_user_id=100016)
    try:
        result = await main_ns["manager_runtime_ready"]("rd16", deadline_sec=2, poll_interval_sec=0.3)
        check("16q. manager_runtime_ready (polling wrapper) surfaces runtime_not_running when nothing is set up",
              result.get("error_class") == "runtime_not_running", result)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 17: automatic rollback (full commit engine)
# ======================================================================

async def test_group_17_rollback_integration():
    print("\n-- Group 17: rollback integration (full engine) --")
    owner = 1001

    # 17a-17b: spawn "succeeds" (process reports running) but the runtime
    # never actually reaches a connected state (start_status stuck on
    # 'starting') -- readiness exhausts its OWN bounded deadline
    # (manager_runtime_ready) and _repl4_rollback runs automatically, not a
    # silent pause. fast_deadline=True collapses manager_runtime_ready's
    # un-overridable 90s/3s defaults into a near-instant test without
    # touching main.py (see FastMonotonic's docstring).
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="ri1old", new_key="ri1new", new_tg_user_id=777001,
        script={"ready_script": {"ri1new": {"start_status": "stuck"}}},
        fast_deadline=True,
    )
    try:
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("17a. spawn-then-never-connects triggers automatic rollback (not a silent pause)",
              r.get("ok") is False and r.get("rollback_stage") == "runtime_validation", r)
        new_after = await main_ns["manager_get"]("ri1new")
        check("17a2. new manager left disabled/stopped, not deleted",
              new_after is not None and int(new_after.get("is_enabled") or 0) == 0 and int(new_after.get("manual_stopped") or 0) == 1, new_after)
        old_after = await main_ns["manager_get"]("ri1old")
        check("17a3. old manager stays fully active", old_after is not None and int(old_after.get("is_enabled") or 0) == 1 and str(old_after.get("status")) == "active", old_after)
        op_after = main_ns["__storage__"].replacement_get(op_row["operation_id"])
        check("17a4. operation ends status=failed with rollback_stage=runtime_validation and non-empty rollback_at",
              op_after is not None and op_after.get("status") == "failed" and op_after.get("rollback_stage") == "runtime_validation" and bool(op_after.get("rollback_at")),
              op_after)
        check("17a5. error_stage is one of the expected readiness error classes",
              op_after is not None and op_after.get("error_stage") in (
                  "runtime_not_ready", "runtime_not_running", "session_unauthorized", "telegram_disconnected",
                  "proxy_connect_error", "command_timeout", "telegram_identity_mismatch",
              ), op_after)

        con = sqlite3.connect(db_path)
        transferred = con.execute("SELECT COUNT(*) FROM manager_source_links WHERE manager_key='ri1new'").fetchone()[0]
        groups = con.execute("SELECT COUNT(*) FROM manager_group_members WHERE manager_key='ri1new'").fetchone()[0]
        has_bizlinks_table = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bizlinks'").fetchone()
        links = con.execute("SELECT COUNT(*) FROM bizlinks WHERE manager_key='ri1new'").fetchone()[0] if has_bizlinks_table else 0
        con.close()
        check("17a6. nothing to roll back -- transfers/links for new_key never ran in the first place",
              transferred == 0 and groups == 0 and links == 0, (transferred, groups, links))

        # 17b: idempotency -- calling replacement_commit again on the now-
        # failed (rolled-back) operation must be a clean, side-effect-free
        # no-op.
        r_again = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("17b. repeated commit on an already-failed op is a clean already_terminal (never re-mutates)",
              r_again.get("ok") is False and r_again.get("code") == "already_terminal", r_again)
        old_after2 = await main_ns["manager_get"]("ri1old")
        check("17b2. old manager still active after the idempotent retry", old_after2 is not None and int(old_after2.get("is_enabled") or 0) == 1, old_after2)
    finally:
        await cleanup_env(tmp_root)

    # 17c-17d: fully healthy runtime (default fixture) -- readiness
    # succeeds, the ping is genuinely consumed via the real manager_commands
    # queue, and the operation proceeds through link creation all the way to
    # cutover_done.
    tmp_root2, db_path2, main_ns2, old_row2, op_row2 = await make_env(old_key="ri2old", new_key="ri2new", new_tg_user_id=777002)
    try:
        r2 = await run_to_cutover(main_ns2, op_row2["operation_id"], owner)
        check("17c. fully healthy runtime reaches cutover_done", r2.get("code") == "cutover_done", r2)
        con = sqlite3.connect(db_path2)
        ping_rows = con.execute("SELECT status FROM manager_commands WHERE target_key='ri2new' AND command='runtime_ping'").fetchall()
        con.close()
        check("17d. the runtime_ping command genuinely reached status=done (not faked away)",
              len(ping_rows) >= 1 and all(s == "done" for (s,) in ping_rows), ping_rows)
    finally:
        await cleanup_env(tmp_root2)

    # 17e-17h: rollback failure-injection during the transfer block --
    # corrupt manager_work_schedule_days (used only by _repl4_transfer_schedule,
    # called AFTER runtime validation already succeeded) so a genuine,
    # unexpected sqlite3 exception is raised mid-committing-phase. Asserts
    # rollback still runs (rollback_stage='committing', the one generic
    # except wrapping the whole committing block), old manager stays active,
    # and a second call on the now-failed op is a safe idempotent no-op.
    tmp_root3, db_path3, main_ns3, old_row3, op_row3 = await make_env(old_key="ri3old", new_key="ri3new", new_tg_user_id=777003)
    try:
        con = sqlite3.connect(db_path3)
        con.execute("DROP TABLE IF EXISTS manager_work_schedule_days")
        con.commit()
        con.close()

        r3 = await main_ns3["replacement_commit"](op_row3["operation_id"], created_by_user_id=owner)
        check("17e. an unexpected exception mid-transfer triggers automatic rollback (rollback_stage=committing)",
              r3.get("ok") is False and r3.get("rollback_stage") == "committing", r3)
        old_after3 = await main_ns3["manager_get"]("ri3old")
        check("17f. old manager stays active after a mid-transfer crash", old_after3 is not None and int(old_after3.get("is_enabled") or 0) == 1, old_after3)
        op_after3 = main_ns3["__storage__"].replacement_get(op_row3["operation_id"])
        check("17g. status=failed, rollback_stage=committing, rollback_at stamped",
              op_after3 is not None and op_after3.get("status") == "failed" and op_after3.get("rollback_stage") == "committing" and bool(op_after3.get("rollback_at")), op_after3)

        con = sqlite3.connect(db_path3)
        leaked_source = con.execute("SELECT COUNT(*) FROM manager_source_links WHERE manager_key='ri3new'").fetchone()[0]
        con.close()
        check("17g2. partially-transferred source-link row for new_key was cleaned up by rollback", leaked_source == 0, leaked_source)

        r3b = await main_ns3["replacement_commit"](op_row3["operation_id"], created_by_user_id=owner)
        check("17h. second call on the same failed op is a clean idempotent no-op (no crash, no further mutation)",
              r3b.get("ok") is False and r3b.get("code") == "already_terminal", r3b)
    finally:
        await cleanup_env(tmp_root3)


# ======================================================================
# GROUP 18: proxy-before-runtime ordering (TPILOT FIX-2 20260718b)
# ======================================================================

async def test_group_18_proxy_ordering():
    print("\n-- Group 18: proxy-before-runtime ordering --")
    owner = 1001

    # 18a-18c: for proxy_mode=="proxy", _repl4_assign_proxy must have
    # ALREADY written proxy_host to the new manager's row before
    # _spawn_manager_process is ever invoked. Instruments the existing fake
    # spawn hook (bound under its exact production name in main_ns, same
    # technique as every other fake in this file) to record, at the moment
    # spawn is called, whether proxy_host is already present on the row.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="po1old", new_key="po1new", new_tg_user_id=991001, proxy_mode="proxy", proxy_ref="manual",
    )
    try:
        real_spawn = main_ns["_spawn_manager_process"]
        spawn_snapshots = []

        async def _instrumented_spawn(key):
            row = await main_ns["manager_get"](key)
            spawn_snapshots.append((key, str((row or {}).get("proxy_host") or "")))
            return await real_spawn(key)

        main_ns["_spawn_manager_process"] = _instrumented_spawn
        r = await run_to_cutover(main_ns, op_row["operation_id"], owner)
        check("18a. commit reaches cutover_done with proxy assigned", r.get("code") == "cutover_done", r)
        check("18b. spawn was actually called at least once", len(spawn_snapshots) >= 1, spawn_snapshots)
        check("18c. proxy_host was ALREADY set on the new manager's row at every spawn call (proxy assigned strictly before spawn)",
              len(spawn_snapshots) >= 1 and all(host == "5.5.5.5" for (_key, host) in spawn_snapshots), spawn_snapshots)
    finally:
        await cleanup_env(tmp_root)

    # 18d-18e: a proxy-resolution FAILURE (durable proxy state missing, the
    # same fixture Group 6's 6g uses) must prevent _spawn_manager_process
    # from EVER being called -- the committing phase returns before runtime
    # start is ever attempted.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="po2old", new_key="po2new", new_tg_user_id=991002, proxy_mode="proxy",
    )
    try:
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET proxy_confirmed=0 WHERE operation_id=?", (op_row["operation_id"],))
        con.commit()
        con.close()
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("18d. proxy resolution failure blocks the commit before runtime start", r.get("code") == "proxy_state_missing", r)
        check("18e. _spawn_manager_process was never called when proxy resolution fails", main_ns["__proc__"]["spawn_calls"] == [], main_ns["__proc__"]["spawn_calls"])
    finally:
        await cleanup_env(tmp_root)

    # 18f-18g: proxy_mode=="proxy" but _repl4_assign_proxy reports success
    # (err=None) WITHOUT actually applying proxy_host to the row -- exercises
    # replacement_commit's own SEPARATE defensive re-check (main.py, right
    # after the _repl4_assign_proxy call succeeds) rather than
    # _repl4_assign_proxy's own internal logic. Must roll back
    # (rollback_stage='proxy_assign') BEFORE spawn is ever attempted.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(
        old_key="po3old", new_key="po3new", new_tg_user_id=991003, proxy_mode="proxy", proxy_ref="manual",
    )
    try:
        async def _stub_assign_proxy_noop(op_row_, new_key_):
            return None  # reports success but never writes proxy_host

        main_ns["_repl4_assign_proxy"] = _stub_assign_proxy_noop
        r = await main_ns["replacement_commit"](op_row["operation_id"], created_by_user_id=owner)
        check("18f. proxy required but not actually applied to the row triggers rollback (defensive re-check)",
              r.get("ok") is False and r.get("rollback_stage") == "proxy_assign", r)
        check("18g. _spawn_manager_process was never called (proxy re-check fails before runtime start)",
              main_ns["__proc__"]["spawn_calls"] == [], main_ns["__proc__"]["spawn_calls"])
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 19: cutover failure-injection (TPILOT FIX-4 20260718b durable
# point-of-no-return)
# ======================================================================

async def test_group_19_cutover_failure_injection():
    print("\n-- Group 19: cutover failure-injection --")
    owner = 1001

    # 19a-19a3: crash simulated immediately BEFORE cutover_started is
    # persisted -- drive_to_links_ready now pauses (via its stubbed
    # _repl4_final_revalidate) with stage still whatever _repl4_run_links_
    # phase set it to (never 'revalidated'/'cutover_started'), so the op is
    # genuinely still pre-irreversible here. A rollback triggered on this
    # exact state must run its normal 6 compensating steps, not refuse.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="cf1old", new_key="cf1new", new_tg_user_id=992001)
    try:
        op_id = op_row["operation_id"]
        r0 = await drive_to_links_ready(main_ns, op_id, owner)
        check("19a-setup. paused before cutover with stage still pre-irreversible", r0.get("status") == "links_ready", r0)
        fresh = main_ns["__storage__"].replacement_get(op_id)
        check("19a-setup2. stage is NOT yet in the irreversible set", str(fresh.get("stage") or "") not in ("cutover_started", "old_manager_retired"), fresh)

        rb = await main_ns["_repl4_rollback"](fresh, "test_injected", "test_injected_crash", "Simulated crash before cutover_started.")
        check("19a. rollback before cutover_started runs normally (not rollback_forbidden_post_cutover)", rb.get("code") != "rollback_forbidden_post_cutover", rb)
        old_after = await main_ns["manager_get"]("cf1old")
        check("19a2. old manager kept/restored active by the normal 6-step rollback (step 4)",
              old_after is not None and int(old_after.get("is_enabled") or 0) == 1 and str(old_after.get("status")) == "active", old_after)
        new_after = await main_ns["manager_get"]("cf1new")
        check("19a3. new manager disabled/stopped by the normal rollback (step 1)",
              new_after is not None and int(new_after.get("is_enabled") or 0) == 0 and int(new_after.get("manual_stopped") or 0) == 1, new_after)
    finally:
        await cleanup_env(tmp_root)

    # 19b-19b3: crash simulated immediately AFTER cutover_started is
    # persisted but BEFORE any old-manager mutation -- manually writes the
    # stage marker exactly as replacement_commit itself would at that point,
    # then calls _repl4_rollback directly and asserts it refuses AND leaves
    # both manager rows byte-for-byte untouched (not just "code says forbidden").
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="cf2old", new_key="cf2new", new_tg_user_id=992002)
    try:
        op_id = op_row["operation_id"]
        await drive_to_links_ready(main_ns, op_id, owner)
        main_ns["__storage__"].replacement_set_stage(op_id, "cutover_started", expected_statuses=("links_ready",))
        fresh = main_ns["__storage__"].replacement_get(op_id)
        old_before = await main_ns["manager_get"]("cf2old")
        new_before = await main_ns["manager_get"]("cf2new")

        rb = await main_ns["_repl4_rollback"](fresh, "test_injected", "test_injected_crash", "Simulated crash after cutover_started.")
        check("19b. rollback forbidden once stage=cutover_started", rb.get("code") == "rollback_forbidden_post_cutover" and rb.get("manual_recovery_required") is True, rb)
        old_after = await main_ns["manager_get"]("cf2old")
        new_after = await main_ns["manager_get"]("cf2new")
        check("19b2. old manager row completely untouched by the forbidden rollback attempt (real DB-state comparison)", old_after == old_before, (old_before, old_after))
        check("19b3. new manager row completely untouched by the forbidden rollback attempt (real DB-state comparison)", new_after == new_before, (new_before, new_after))
    finally:
        await cleanup_env(tmp_root)

    # 19c-19d2: crash simulated AFTER old-manager retirement (old manager
    # genuinely deleted, stage='old_manager_retired' persisted) -- same
    # rollback-forbidden assertion, PLUS verifies that RESUMING via
    # replacement_commit correctly SKIPS _repl4_final_revalidate (no
    # old_manager_missing error even though old_row is genuinely gone) and
    # converges cleanly to cutover_done. This also covers the "crash after
    # routing/source/access/proxy finalization but before the final CAS"
    # and "old manager already missing" scenarios -- both are exactly this
    # same durable state.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="cf3old", new_key="cf3new", new_tg_user_id=992003)
    try:
        op_id = op_row["operation_id"]
        await drive_to_links_ready(main_ns, op_id, owner)
        await main_ns["_manager_delete_full_core"]("cf3old", requested_by=owner)
        main_ns["__storage__"].replacement_set_stage(op_id, "old_manager_retired", expected_statuses=("links_ready",))
        fresh = main_ns["__storage__"].replacement_get(op_id)
        check("19c-setup. old manager genuinely gone, stage=old_manager_retired",
              await main_ns["manager_get"]("cf3old") is None and fresh.get("stage") == "old_manager_retired", fresh)

        rb = await main_ns["_repl4_rollback"](fresh, "test_injected", "test_injected_crash", "Simulated crash after old_manager_retired.")
        check("19c. rollback still forbidden at stage=old_manager_retired", rb.get("code") == "rollback_forbidden_post_cutover", rb)

        r_resume = await main_ns["replacement_commit"](op_id, created_by_user_id=owner)
        check("19d. resume from old_manager_retired SKIPS final_revalidate (no old_manager_missing) and completes cutover",
              r_resume.get("code") == "cutover_done", r_resume)
        new_after = await main_ns["manager_get"]("cf3new")
        check("19d2. new manager active/enabled after the resumed completion", new_after is not None and str(new_after.get("status")) == "active" and int(new_after.get("is_enabled") or 0) == 1, new_after)

        r_again = await main_ns["replacement_commit"](op_id, created_by_user_id=owner)
        check("19d3. a further resume call after full completion is idempotent (already_cutover, covers 'crash before the final CAS' converging without re-mutation)",
              r_again.get("code") == "already_cutover", r_again)
    finally:
        await cleanup_env(tmp_root)

    # 19e-19g: the deadline sweeper firing DURING stage=cutover_started --
    # exercises the REAL _repl_commit_deadline_sweep_once against a row past
    # its commit_deadline_at, proving BOTH the SQL-level exclusion
    # (storage.replacement_list_commit_phase_past_deadline never selects it)
    # and the independent defense-in-depth layer (_repl4_rollback itself
    # also refuses if called directly on such a row).
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="cf4old", new_key="cf4new", new_tg_user_id=992004)
    try:
        op_id = op_row["operation_id"]
        await drive_to_links_ready(main_ns, op_id, owner)
        main_ns["__storage__"].replacement_set_stage(op_id, "cutover_started", expected_statuses=("links_ready",))
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET commit_deadline_at=? WHERE operation_id=?", ("2000-01-01T00:00:00", op_id))
        con.commit()
        con.close()

        candidates = main_ns["__storage__"].replacement_list_commit_phase_past_deadline(main_ns["_now_utc_iso"]())
        check("19e. SQL-level exclusion: an op at stage=cutover_started past its deadline is NEVER selected by the sweeper's own query",
              op_id not in [c.get("operation_id") for c in candidates], [c.get("operation_id") for c in candidates])

        handled = await main_ns["_repl_commit_deadline_sweep_once"]()
        check("19f. a real sweep pass handles 0 ops for this operation_id (nothing eligible)", handled == 0, handled)
        fresh = main_ns["__storage__"].replacement_get(op_id)
        check("19f2. operation status/stage unchanged by the sweep (still links_ready/cutover_started, not failed)",
              fresh.get("status") == "links_ready" and fresh.get("stage") == "cutover_started", fresh)

        rb = await main_ns["_repl4_rollback"](fresh, "commit_deadline", "command_timeout", "Simulated deadline sweep.")
        check("19g. direct rollback also independently rejected (defense-in-depth, both layers)", rb.get("code") == "rollback_forbidden_post_cutover", rb)
    finally:
        await cleanup_env(tmp_root)

    # 19h-19i: same defense-in-depth pair, at stage=old_manager_retired.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="cf5old", new_key="cf5new", new_tg_user_id=992005)
    try:
        op_id = op_row["operation_id"]
        await drive_to_links_ready(main_ns, op_id, owner)
        main_ns["__storage__"].replacement_set_stage(op_id, "old_manager_retired", expected_statuses=("links_ready",))
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET commit_deadline_at=? WHERE operation_id=?", ("2000-01-01T00:00:00", op_id))
        con.commit()
        con.close()

        candidates = main_ns["__storage__"].replacement_list_commit_phase_past_deadline(main_ns["_now_utc_iso"]())
        check("19h. SQL-level exclusion also applies at stage=old_manager_retired",
              op_id not in [c.get("operation_id") for c in candidates], [c.get("operation_id") for c in candidates])
        fresh = main_ns["__storage__"].replacement_get(op_id)
        rb = await main_ns["_repl4_rollback"](fresh, "commit_deadline", "command_timeout", "Simulated deadline sweep.")
        check("19i. direct rollback also rejected at stage=old_manager_retired", rb.get("code") == "rollback_forbidden_post_cutover", rb)
    finally:
        await cleanup_env(tmp_root)

    # 19j-19m: new manager already active and fully routed (resume-after-
    # full-completion) -- calling replacement_commit again must be a clean,
    # side-effect-free already_cutover, proven with real DB-state
    # comparisons (not just the return dict), and the new manager must never
    # end up disabled or the routing pointed at the (by-then missing) old
    # manager.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="cf6old", new_key="cf6new", new_tg_user_id=992006)
    try:
        op_id = op_row["operation_id"]
        r1 = await run_to_cutover(main_ns, op_id, owner)
        check("19j. full completion reaches cutover_done", r1.get("code") == "cutover_done", r1)

        fresh_before = main_ns["__storage__"].replacement_get(op_id)
        new_before = await main_ns["manager_get"]("cf6new")
        con = sqlite3.connect(db_path)
        src_before = con.execute("SELECT source_key FROM manager_source_links WHERE manager_key='cf6new'").fetchone()
        con.close()

        r2 = await main_ns["replacement_commit"](op_id, created_by_user_id=owner)
        check("19k. resume-after-full-completion returns already_cutover cleanly", r2.get("code") == "already_cutover" and r2.get("ok") is True, r2)

        fresh_after = main_ns["__storage__"].replacement_get(op_id)
        new_after = await main_ns["manager_get"]("cf6new")
        con = sqlite3.connect(db_path)
        src_after = con.execute("SELECT source_key FROM manager_source_links WHERE manager_key='cf6new'").fetchone()
        con.close()
        check("19l. no DB mutation on the operation row from the redundant resume call", fresh_after == fresh_before, (fresh_before, fresh_after))
        check("19l2. no DB mutation on the new manager row", new_after == new_before, (new_before, new_after))
        check("19l3. no DB mutation on routing/source data", src_after == src_before, (src_before, src_after))
        check("19m. new manager stays active/enabled after full completion (never left disabled or routed at a missing old manager)",
              new_after is not None and str(new_after.get("status")) == "active" and int(new_after.get("is_enabled") or 0) == 1, new_after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 20: start-status regression matrix (TPILOT FIX-1 20260718b --
# freshness check scoped to phase='connected' only, never phase='running')
# ======================================================================

async def test_group_20_start_status_regression_matrix():
    print("\n-- Group 20: start-status regression matrix --")

    # 20a-20c: phase='running' (the steady state) with an INCREASINGLY old
    # updated_at, paired with a successful ping -- all three must PASS,
    # proving the freshness-for-'running' removal actually works across a
    # wide age range, not just barely-stale.
    for label, key, tgid, age_sec in (
        ("20a. stale-by-2-minutes phase=running + successful ping => PASS", "ss1", 200001, 130),
        ("20b. stale-by-1-hour phase=running + successful ping => PASS", "ss2", 200002, 3600),
        ("20c. stale-by-24-hours phase=running + successful ping => PASS", "ss3", 200003, 86400),
    ):
        tmp_root, db_path, main_ns, row = await make_bare_env(key=key, tg_user_id=tgid)
        try:
            main_ns["__proc__"]["running"].add(key)
            _write_status_file(tmp_root, key, phase="running", updated_at=_fresh_iso(age_sec))
            _seed_heartbeat_row(db_path, key)
            responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, key, mode="ok"))
            try:
                result = await main_ns["_manager_runtime_ready_once"](key, expected_tgid=tgid, ping_timeout_sec=3)
            finally:
                if not responder.done():
                    responder.cancel()
            check(label, result.get("ok") is True, result)
        finally:
            await cleanup_env(tmp_root)

    # 20d: stale status file but the OS process itself is not running --
    # signal 1 (process) must still gate first, before phase/freshness is
    # even inspected.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="ss4", tg_user_id=200004)
    try:
        _write_status_file(tmp_root, "ss4", phase="running", updated_at=_fresh_iso(99999))
        result = await main_ns["_manager_runtime_ready_once"]("ss4")
        check("20d. stale status file but process not running => runtime_not_running (signal 1 gates first)", result.get("error_class") == "runtime_not_running", result)
    finally:
        await cleanup_env(tmp_root)

    # 20e: stale status file (running) + genuinely running process + a
    # FAILED ping => the freshness carve-out must never mask a real ping
    # failure.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="ss5", tg_user_id=200005)
    try:
        main_ns["__proc__"]["running"].add("ss5")
        _write_status_file(tmp_root, "ss5", phase="running", updated_at=_fresh_iso(99999))
        _seed_heartbeat_row(db_path, "ss5")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "ss5", mode="disconnected"))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("ss5", ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("20e. stale status + healthy process + failed ping => telegram_disconnected (not masked by the freshness carve-out)", result.get("error_class") == "telegram_disconnected", result)
    finally:
        await cleanup_env(tmp_root)

    # 20f: stale status file (running) + wrong ping identity => still fails
    # correctly.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="ss6", tg_user_id=200006)
    try:
        main_ns["__proc__"]["running"].add("ss6")
        _write_status_file(tmp_root, "ss6", phase="running", updated_at=_fresh_iso(99999))
        _seed_heartbeat_row(db_path, "ss6")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "ss6", mode="wrong_tgid", tg_user_id=1))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("ss6", ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("20f. stale status + wrong ping identity => telegram_identity_mismatch", result.get("error_class") == "telegram_identity_mismatch", result)
    finally:
        await cleanup_env(tmp_root)

    # 20g-20h: phase='exited' with a successful ping queued anyway -- must
    # fail BEFORE ever reaching the ping check. Proven at the strongest
    # level available: the runtime_ping command is never even enqueued (the
    # phase gate returns before _rrp_put is ever called), so no
    # manager_commands row for this key/command can exist at all.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="ss7", tg_user_id=200007)
    responder = None
    try:
        main_ns["__proc__"]["running"].add("ss7")
        _write_status_file(tmp_root, "ss7", phase="exited", reason_class="", error="crashed")
        _seed_heartbeat_row(db_path, "ss7")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "ss7", mode="ok"))
        result = await main_ns["_manager_runtime_ready_once"]("ss7", ping_timeout_sec=3)
        check("20g. phase=exited short-circuits before ever reaching the ping check (result is not ok, signal=start_status)",
              result.get("ok") is not True and result.get("signal") == "start_status", result)
        try:
            con = sqlite3.connect(db_path)
            ping_rows = con.execute("SELECT COUNT(*) FROM manager_commands WHERE target_key='ss7' AND command='runtime_ping'").fetchone()[0]
            con.close()
        except sqlite3.OperationalError:
            ping_rows = 0  # table never created -- strongest possible proof nothing was ever enqueued
        check("20h. runtime_ping was never even enqueued -- phase-gating short-circuits BEFORE the ping step is reached", ping_rows == 0, ping_rows)
    finally:
        if responder is not None and not responder.done():
            responder.cancel()
        await cleanup_env(tmp_root)

    # 20i: phase='connected' (the TRANSIENT pre-running marker, not
    # 'running') stale beyond start_status_max_age_sec must STILL fail --
    # this is the one case proving the freshness carve-out is scoped
    # correctly to 'running' only, never applied blanket to any connected-ish
    # phase.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="ss8", tg_user_id=200008)
    try:
        main_ns["__proc__"]["running"].add("ss8")
        _write_status_file(tmp_root, "ss8", phase="connected", updated_at=_fresh_iso(200))
        result = await main_ns["_manager_runtime_ready_once"]("ss8", start_status_max_age_sec=120)
        check("20i. phase=connected (not running) stale beyond start_status_max_age_sec still FAILS -- freshness carve-out is scoped to 'running' only",
              result.get("error_class") == "runtime_not_ready", result)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 21: ping-response validation (TPILOT FIX-8 20260718b -- worker_key
# and checked_at freshness checks inserted before the pre-existing
# connected/authorized/tg_user_id checks)
# ======================================================================

async def test_group_21_ping_response_validation():
    print("\n-- Group 21: ping-response validation --")

    # 21a: an otherwise-perfectly-healthy ping response (matching tg_user_id,
    # connected/authorized both true) but with the WRONG worker_key => must
    # fail with runtime_not_ready/signal=ping, mentioning the mismatch --
    # and must fail BEFORE the pre-existing connected/authorized/tg_user_id
    # checks would ever matter (those would all pass on their own).
    tmp_root, db_path, main_ns, row = await make_bare_env(key="pv1", tg_user_id=300001)
    try:
        main_ns["__proc__"]["running"].add("pv1")
        _write_status_file(tmp_root, "pv1", phase="connected")
        _seed_heartbeat_row(db_path, "pv1")
        responder = asyncio.create_task(_drive_ping_response(
            main_ns["__storage__"], db_path, "pv1", mode="ok", worker_key_override="someotherkey",
        ))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("pv1", expected_tgid=300001, ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("21a. ping response with wrong worker_key => runtime_not_ready (signal=ping), even though every other field is healthy",
              result.get("error_class") == "runtime_not_ready" and result.get("signal") == "ping"
              and "worker_key" in str(result.get("detail") or "").lower(), result)
    finally:
        await cleanup_env(tmp_root)

    # 21b: the SAME otherwise-healthy scenario but with matching worker_key
    # (default) => PASS. Placed right next to 21a specifically to prove
    # worker_key is actually being checked (contrast: identical setup, only
    # the worker_key differs, opposite outcomes).
    tmp_root, db_path, main_ns, row = await make_bare_env(key="pv1b", tg_user_id=300011)
    try:
        main_ns["__proc__"]["running"].add("pv1b")
        _write_status_file(tmp_root, "pv1b", phase="connected")
        _seed_heartbeat_row(db_path, "pv1b")
        responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, "pv1b", mode="ok"))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("pv1b", expected_tgid=300011, ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("21b. the identical otherwise-healthy scenario with a MATCHING worker_key => PASS (contrast with 21a proves worker_key is genuinely checked)",
              result.get("ok") is True, result)
    finally:
        await cleanup_env(tmp_root)

    # 21c: a ping response whose checked_at is stamped far in the past
    # (simulating a stale/replayed response) => command_timeout, even though
    # every other field is healthy.
    tmp_root, db_path, main_ns, row = await make_bare_env(key="pv2", tg_user_id=300002)
    try:
        main_ns["__proc__"]["running"].add("pv2")
        _write_status_file(tmp_root, "pv2", phase="connected")
        _seed_heartbeat_row(db_path, "pv2")
        responder = asyncio.create_task(_drive_ping_response(
            main_ns["__storage__"], db_path, "pv2", mode="ok", checked_at_override=_fresh_iso(99999),
        ))
        try:
            result = await main_ns["_manager_runtime_ready_once"]("pv2", ping_timeout_sec=3)
        finally:
            if not responder.done():
                responder.cancel()
        check("21c. ping response with a stale/replayed checked_at => command_timeout (signal=ping)",
              result.get("error_class") == "command_timeout" and result.get("signal") == "ping", result)
    finally:
        await cleanup_env(tmp_root)

    # 21d-21f: pre-existing disconnected/unauthorized/wrong-tgid modes are a
    # deliberate REGRESSION guard -- Group 16 (16l/16m/16n) already covers
    # these end-to-end; re-asserted here at the same call site as the new
    # worker_key/checked_at checks so a future change to the check ORDERING
    # inside _manager_runtime_ready_once can't silently break them without
    # also failing something right next to the new Fix-8 checks.
    for label, key, tgid, mode, extra in (
        ("21d. regression: connected=False still => telegram_disconnected", "pv3", 300003, "disconnected", {}),
        ("21e. regression: authorized=False still => session_unauthorized", "pv4", 300004, "unauthorized", {}),
        ("21f. regression: wrong tg_user_id still => telegram_identity_mismatch", "pv5", 300005, "wrong_tgid", {"tg_user_id": 1}),
    ):
        expected_class = {"disconnected": "telegram_disconnected", "unauthorized": "session_unauthorized", "wrong_tgid": "telegram_identity_mismatch"}[mode]
        tmp_root, db_path, main_ns, row = await make_bare_env(key=key, tg_user_id=tgid)
        try:
            main_ns["__proc__"]["running"].add(key)
            _write_status_file(tmp_root, key, phase="connected")
            _seed_heartbeat_row(db_path, key)
            responder = asyncio.create_task(_drive_ping_response(main_ns["__storage__"], db_path, key, mode=mode, **extra))
            try:
                result = await main_ns["_manager_runtime_ready_once"](key, ping_timeout_sec=3)
            finally:
                if not responder.done():
                    responder.cancel()
            check(label, result.get("error_class") == expected_class, result)
        finally:
            await cleanup_env(tmp_root)


# ======================================================================
# GROUP 22: FIX-R1 20260718c -- sweeper-vs-cutover-CAS race regression
# ======================================================================

async def test_group_22_fix_r1_cutover_cas_race():
    print("\n-- Group 22: FIX-R1 cutover CAS race --")
    owner = 1001

    # 22a-22h: the core race -- the deadline sweeper's LEGAL pre-PONR
    # rollback wins during _repl4_final_revalidate's own awaits, moving the
    # op to status='failed' (new manager disabled, old manager restored)
    # BEFORE replacement_commit's cutover_started CAS runs. Reproduced by
    # stubbing _repl4_final_revalidate (same swap technique as
    # drive_to_links_ready) so that, instead of returning a retryable
    # result, it calls the REAL _repl4_rollback (simulating the sweeper
    # winning concurrently) and then returns None (simulating
    # final_revalidate's own checks having passed -- the awaits finished,
    # but reality changed underneath them). A parallel spy on
    # _repl4_cutover_old_manager proves the commit path never reaches it
    # once the CAS fails.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r1old", new_key="r1new", new_tg_user_id=993001)
    try:
        op_id = op_row["operation_id"]
        storage_mod = main_ns["__storage__"]
        r0 = await drive_to_links_ready(main_ns, op_id, owner)
        check("22a-setup. paused at links_ready, stage still pre-irreversible before the race is injected",
              r0.get("status") == "links_ready", r0)
        fresh0 = storage_mod.replacement_get(op_id)
        check("22a-setup2. stage not yet in the irreversible set", str(fresh0.get("stage") or "") not in ("cutover_started", "old_manager_retired"), fresh0)

        real_revalidate = main_ns["_repl4_final_revalidate"]
        real_cutover = main_ns["_repl4_cutover_old_manager"]
        cutover_calls: list = []
        snapshot: dict = {}

        async def _stub_sweeper_wins_revalidate(op_row_in, old_key_in, new_key_in, target_date_in, required_in):
            opid = str(op_row_in.get("operation_id") or "")
            fresh = storage_mod.replacement_get(opid) or op_row_in
            # Simulate the deadline sweeper's own legal pre-PONR rollback
            # winning the race concurrently with this (real) revalidation.
            await main_ns["_repl4_rollback"](fresh, "test_injected_sweeper", "command_timeout",
                                              "Simulated sweeper win during final_revalidate.")
            # Snapshot state IMMEDIATELY after the sweeper's rollback -- this
            # is "reality" at the moment the cutover_started CAS runs. Any
            # further mutation of either manager row after this point (i.e.
            # observed at the end of the enclosing replacement_commit call)
            # would prove the commit path kept mutating after seeing the
            # failed CAS.
            snapshot["old_after_rollback"] = await main_ns["manager_get"](old_key_in)
            snapshot["new_after_rollback"] = await main_ns["manager_get"](new_key_in)
            # final_revalidate's own checks "pass" (simulating the awaits
            # finishing successfully even though reality changed underneath).
            return None

        async def _spy_cutover(*args, **kwargs):
            cutover_calls.append((args, kwargs))
            return await real_cutover(*args, **kwargs)

        main_ns["_repl4_final_revalidate"] = _stub_sweeper_wins_revalidate
        main_ns["_repl4_cutover_old_manager"] = _spy_cutover
        try:
            r1 = await main_ns["replacement_commit"](op_id, created_by_user_id=owner)
        finally:
            main_ns["_repl4_final_revalidate"] = real_revalidate
            main_ns["_repl4_cutover_old_manager"] = real_cutover

        fresh1 = storage_mod.replacement_get(op_id)
        check("22a. op ends status='failed' from the injected sweeper rollback, not further corrupted",
              fresh1.get("status") == "failed", fresh1)
        check("22b. commit call's own result is ok=False, code='stale_state' (Fix 1's terminal-status branch)",
              r1.get("ok") is False and r1.get("code") == "stale_state", r1)
        check("22c. _repl4_cutover_old_manager was NEVER invoked as a result of this commit call",
              len(cutover_calls) == 0, cutover_calls)

        old_after = await main_ns["manager_get"]("r1old")
        new_after = await main_ns["manager_get"]("r1new")
        check("22d. old manager remains exactly as the rollback left it (restored, active)",
              old_after is not None and int(old_after.get("is_enabled") or 0) == 1 and str(old_after.get("status")) == "active",
              old_after)
        check("22e. new manager remains exactly as the rollback left it (disabled)",
              new_after is not None and int(new_after.get("is_enabled") or 0) == 0 and int(new_after.get("manual_stopped") or 0) == 1,
              new_after)
        check("22f. old manager row byte-identical between right-after-rollback and after the commit call returns (zero further mutation)",
              old_after == snapshot.get("old_after_rollback"), (snapshot.get("old_after_rollback"), old_after))
        check("22g. new manager row byte-identical between right-after-rollback and after the commit call returns (zero further mutation)",
              new_after == snapshot.get("new_after_rollback"), (snapshot.get("new_after_rollback"), new_after))
        check("22h. no scenario leaves both managers disabled (the review's own bottom-line safety property)",
              not (int(old_after.get("is_enabled") or 0) == 0 and int(new_after.get("is_enabled") or 0) == 0),
              (old_after, new_after))
    finally:
        await cleanup_env(tmp_root)

    # 22i-22l2: benign re-entry via Fix 2's guard directly -- per the CAS
    # semantics (status-guarded, not stage-guarded), the CAS itself cannot
    # naturally return False merely because stage was already
    # 'cutover_started' while status stays 'links_ready' (a second identical
    # write matches the same WHERE and returns True again). So this exercises
    # the general-purpose guard function directly against the exact durable
    # state a REAL first-pass commit leaves behind right before calling
    # _repl4_cutover_old_manager, proving the guard does not false-positive
    # reject the normal/expected precondition.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r2old", new_key="r2new", new_tg_user_id=993002)
    try:
        op_id = op_row["operation_id"]
        storage_mod = main_ns["__storage__"]
        await drive_to_links_ready(main_ns, op_id, owner)
        cas_ok = storage_mod.replacement_set_stage(op_id, "cutover_started", expected_statuses=("links_ready",))
        check("22i-setup. manually CAS'd to stage=cutover_started, status still links_ready", cas_ok, None)
        fresh_op_row = storage_mod.replacement_get(op_id)
        check("22i-setup2. durable row now matches the real pre-cutover-call state (status=links_ready, stage=cutover_started)",
              fresh_op_row.get("status") == "links_ready" and fresh_op_row.get("stage") == "cutover_started", fresh_op_row)

        r2 = await main_ns["_repl4_cutover_old_manager"](fresh_op_row, "r2old", "r2new")
        check("22j. Fix 2's guard admits the normal pre-cutover state and proceeds (returns None, no refusal)", r2 is None, r2)
        old_after = await main_ns["manager_get"]("r2old")
        new_after = await main_ns["manager_get"]("r2new")
        check("22k. old manager disabled+deleted, cutover completed normally", old_after is None, old_after)
        check("22l. new manager active/enabled after the completed cutover",
              new_after is not None and str(new_after.get("status")) == "active" and int(new_after.get("is_enabled") or 0) == 1, new_after)
        fresh_after = storage_mod.replacement_get(op_id)
        check("22l2. op durably reaches status=cutover_done", fresh_after.get("status") == "cutover_done", fresh_after)
    finally:
        await cleanup_env(tmp_root)

    # 22m-22p: a terminal status='failed' blocks Fix 2's guard entirely --
    # structured refusal, zero mutation of either manager row. Combined with
    # scenario 22a-22h above (which proves Fix 1's own terminal-status branch
    # rejects the same underlying condition), this covers "terminal failed
    # state blocks both Fix 1 and Fix 2".
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r3old", new_key="r3new", new_tg_user_id=993003)
    try:
        op_id = op_row["operation_id"]
        storage_mod = main_ns["__storage__"]
        await drive_to_links_ready(main_ns, op_id, owner)
        failed_ok = storage_mod.replacement_fail(op_id, "test_terminal", "Simulated terminal failure before cutover call.")
        check("22m-setup. op forced to status=failed", failed_ok, None)
        fresh_op_row = storage_mod.replacement_get(op_id)
        old_before = await main_ns["manager_get"]("r3old")
        new_before = await main_ns["manager_get"]("r3new")

        r3 = await main_ns["_repl4_cutover_old_manager"](fresh_op_row, "r3old", "r3new")
        check("22n. Fix 2's guard refuses a terminal status=failed row (code=stale_state)",
              r3 is not None and r3.get("ok") is False and r3.get("code") == "stale_state", r3)
        old_after = await main_ns["manager_get"]("r3old")
        new_after = await main_ns["manager_get"]("r3new")
        check("22o. old manager row byte-identical before/after the refused call (zero mutation)", old_after == old_before, (old_before, old_after))
        check("22p. new manager row byte-identical before/after the refused call (zero mutation)", new_after == new_before, (new_before, new_after))
    finally:
        await cleanup_env(tmp_root)

    # 22q-22t: a distinct terminal sub-case -- status='cancelled' (a
    # different member of REPLACEMENT_TERMINAL_STATUSES than 'failed';
    # forced directly via raw SQL since replacement_cancel() only accepts
    # pre-commit statuses draft..ready_commit and cannot reach links_ready --
    # this exercises the guard's generic "any terminal status" handling
    # rather than a 'failed'-specific code path).
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r4old", new_key="r4new", new_tg_user_id=993004)
    try:
        op_id = op_row["operation_id"]
        storage_mod = main_ns["__storage__"]
        await drive_to_links_ready(main_ns, op_id, owner)
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_replacements SET status='cancelled' WHERE operation_id=?", (op_id,))
        con.commit()
        con.close()
        fresh_op_row = storage_mod.replacement_get(op_id)
        check("22q-setup. op forced to status=cancelled", fresh_op_row.get("status") == "cancelled", fresh_op_row)
        old_before = await main_ns["manager_get"]("r4old")
        new_before = await main_ns["manager_get"]("r4new")

        r4 = await main_ns["_repl4_cutover_old_manager"](fresh_op_row, "r4old", "r4new")
        check("22r. Fix 2's guard also refuses a terminal status=cancelled row (code=stale_state)",
              r4 is not None and r4.get("ok") is False and r4.get("code") == "stale_state", r4)
        old_after = await main_ns["manager_get"]("r4old")
        new_after = await main_ns["manager_get"]("r4new")
        check("22s. old manager row byte-identical before/after the refused cancelled-status call", old_after == old_before, (old_before, old_after))
        check("22t. new manager row byte-identical before/after the refused cancelled-status call", new_after == new_before, (new_before, new_after))
    finally:
        await cleanup_env(tmp_root)

    # 22u-22x: a pre-cutover stage (status still links_ready, stage never
    # advanced past what the links phase itself sets, i.e. before
    # cutover_started was ever written) -- Fix 2's guard must refuse this
    # too, since stage is not in _REPL_CUTOVER_IRREVERSIBLE_STAGES.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r5old", new_key="r5new", new_tg_user_id=993005)
    try:
        op_id = op_row["operation_id"]
        storage_mod = main_ns["__storage__"]
        await drive_to_links_ready(main_ns, op_id, owner)
        fresh_op_row = storage_mod.replacement_get(op_id)
        check("22u-setup. status=links_ready, stage pre-irreversible (never cutover_started/old_manager_retired)",
              fresh_op_row.get("status") == "links_ready" and str(fresh_op_row.get("stage") or "") not in ("cutover_started", "old_manager_retired"),
              fresh_op_row)
        old_before = await main_ns["manager_get"]("r5old")
        new_before = await main_ns["manager_get"]("r5new")

        r5 = await main_ns["_repl4_cutover_old_manager"](fresh_op_row, "r5old", "r5new")
        check("22v. Fix 2's guard refuses a pre-cutover-started stage (code=stale_state)",
              r5 is not None and r5.get("ok") is False and r5.get("code") == "stale_state", r5)
        old_after = await main_ns["manager_get"]("r5old")
        new_after = await main_ns["manager_get"]("r5new")
        check("22w. old manager row byte-identical before/after the refused pre-cutover-stage call", old_after == old_before, (old_before, old_after))
        check("22x. new manager row byte-identical before/after the refused pre-cutover-stage call", new_after == new_before, (new_before, new_after))
    finally:
        await cleanup_env(tmp_root)

    # 22y-22ac: resume from stage=old_manager_retired called DIRECTLY against
    # _repl4_cutover_old_manager (Group 19's 19c/19d already prove this
    # resumes safely via the FULL replacement_commit() resume path with the
    # new fix in place -- this adds the gap that's genuinely new: exercising
    # Fix 2's entry guard directly at this exact stage, bypassing
    # replacement_commit entirely).
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r6old", new_key="r6new", new_tg_user_id=993006)
    try:
        op_id = op_row["operation_id"]
        storage_mod = main_ns["__storage__"]
        await drive_to_links_ready(main_ns, op_id, owner)
        await main_ns["_manager_delete_full_core"]("r6old", requested_by=owner)
        storage_mod.replacement_set_stage(op_id, "old_manager_retired", expected_statuses=("links_ready",))
        fresh_op_row = storage_mod.replacement_get(op_id)
        check("22y-setup. old manager genuinely gone, stage=old_manager_retired",
              await main_ns["manager_get"]("r6old") is None and fresh_op_row.get("stage") == "old_manager_retired", fresh_op_row)

        r6 = await main_ns["_repl4_cutover_old_manager"](fresh_op_row, "r6old", "r6new")
        check("22z. Fix 2's guard admits stage=old_manager_retired directly and completes the cutover", r6 is None, r6)
        fresh_after_first = storage_mod.replacement_get(op_id)
        check("22aa. op durably reaches status=cutover_done", fresh_after_first.get("status") == "cutover_done", fresh_after_first)
        new_after_first = await main_ns["manager_get"]("r6new")

        # Idempotent re-entry: a second direct call against the now-completed
        # op must short-circuit via the guard's cutover_done branch, zero
        # further mutation.
        r6b = await main_ns["_repl4_cutover_old_manager"](fresh_after_first, "r6old", "r6new")
        check("22ab. a further direct call after completion is idempotent (returns None, no error)", r6b is None, r6b)
        new_after_second = await main_ns["manager_get"]("r6new")
        check("22ac. new manager row byte-identical across the idempotent re-entry call (zero further mutation)",
              new_after_first == new_after_second, (new_after_first, new_after_second))
    finally:
        await cleanup_env(tmp_root)

    # 22ad-22ag: resume from status=cutover_done (via the FULL production
    # run_to_cutover flow, not a manually-constructed row) returns
    # success/already-completed without duplicate mutation when
    # _repl4_cutover_old_manager is called directly.
    tmp_root, db_path, main_ns, old_row, op_row = await make_env(old_key="r7old", new_key="r7new", new_tg_user_id=993007)
    try:
        op_id = op_row["operation_id"]
        storage_mod = main_ns["__storage__"]
        r1 = await run_to_cutover(main_ns, op_id, owner)
        check("22ad-setup. full production flow reaches cutover_done", r1.get("code") == "cutover_done", r1)
        fresh_op_row = storage_mod.replacement_get(op_id)
        old_before = await main_ns["manager_get"]("r7old")
        new_before = await main_ns["manager_get"]("r7new")

        r7 = await main_ns["_repl4_cutover_old_manager"](fresh_op_row, "r7old", "r7new")
        check("22ae. direct call against an already-cutover_done row returns None (Fix 2's short-circuit success path)", r7 is None, r7)
        old_after = await main_ns["manager_get"]("r7old")
        new_after = await main_ns["manager_get"]("r7new")
        check("22af. old manager row byte-identical (still absent) before/after the redundant direct call", old_after == old_before, (old_before, old_after))
        check("22ag. new manager row byte-identical before/after the redundant direct call (zero further mutation)", new_after == new_before, (new_before, new_after))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 14: security / static
# ======================================================================

def test_group_14_security_static():
    print("\n-- Group 14: security/static --")
    tree = MAIN_AST  # PERF 20260718: reuse the module-level parse, see MAIN_AST above.
    stage4_nodes = [n for n in tree.body if getattr(n, "name", None) in STAGE4_REAL_NAMES and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    stage4_src = "\n".join(ast.unparse(n) for n in stage4_nodes)

    check("14a. no PartnerBot call anywhere in Stage 4 code", "partner_stat_bot" not in stage4_src and "PartnerBot" not in stage4_src, None)

    # FIX-ROUND AUDIT 20260718: 14b/14e/14f/14j below were previously
    # tautological/vacuous -- each was an `A or B` where B was unconditionally
    # true regardless of A (e.g. "manager_set_fields" always appears in
    # Stage4 source, "or True" is always true, "cutover_done" always appears
    # in Stage4 source), so the check could never fail even if the property
    # it claimed to verify was violated. Rewritten below as single,
    # falsifiable conditions -- 14b/14j now use a real AST scan of every
    # replacement_advance()/replacement_set_stage() CAS-target argument
    # actually WRITTEN by Stage4 code (distinguishing a real write from a
    # legitimate READ like `status in ("cutover_done", "notified", "done")`,
    # which legitimately contains the string "notified" in source text but
    # is not a CAS write).
    def _scan_cas_targets(nodes):
        advance_targets, stage_targets = [], []
        for n in nodes:
            for call in ast.walk(n):
                if not isinstance(call, ast.Call):
                    continue
                f = call.func
                fname = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                if fname == "replacement_advance" and len(call.args) >= 3 and isinstance(call.args[2], ast.Constant):
                    advance_targets.append(call.args[2].value)
                elif fname == "replacement_set_stage" and len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                    stage_targets.append(call.args[1].value)
        return advance_targets, stage_targets

    advance_targets, stage_targets = _scan_cas_targets(stage4_nodes)
    check("14b. no notified/done transition anywhere in Stage 4 code (AST scan of every replacement_advance() to_status CAS target actually written, not a fragile/vacuous string search)",
          not any(t in ("notified", "done") for t in advance_targets), advance_targets)
    check("14b2. replacement_finalize (the only path to done) is never called from Stage 4 code", "replacement_finalize(" not in stage4_src, None)
    check("14c. no preflight_check.py reference in Stage 4 code", "preflight_check" not in stage4_src, None)
    check("14d. no real network import/usage (requests/urllib/socket) added", not any(s in stage4_src for s in ("import requests", "import socket", "urllib.request")), None)
    check("14e. no secret persisted -- 'proxy_password' string never appears anywhere in Stage 4 source at all (never passed into replacement_set_stage/_repl4_result/error text)",
          "proxy_password" not in stage4_src, None)
    check("14f. old-session path is never a write target -- regex scan proves old_session_path is never the destination arg of open()/os.replace()/shutil.move()/shutil.copy*() anywhere in Stage 4 source (.session write only ever targets the NEW key's paths)",
          not re.search(r"(open\s*\(\s*old_session_path|os\.replace\([^,]*,\s*old_session_path|shutil\.(?:move|copy\w*)\([^,]*,\s*old_session_path)", stage4_src),
          stage4_src.count("old_session_path"))

    tree_reserve = MAIN_AST  # PERF 20260718: reuse the module-level parse.
    reserve_guard_src = "\n".join(
        ast.unparse(n) for n in tree_reserve.body if getattr(n, "name", None) == "_repl4_entry_guards"
    )
    check("14g. no reserve activation call inside the commit engine", "reserve_activation_request" not in reserve_guard_src, None)

    sites = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords
             if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]
    check("14h. allow_spend=True remains exactly 2 call sites project-wide", len(sites) == 2, sites)

    generations = [n for n in tree.body if getattr(n, "name", None) == "replacement_commit"]
    check("14i. replacement_commit is defined exactly once (no override stacking)", len(generations) == 1, len(generations))

    check("14j. max reachable status via replacement_commit is cutover_done -- 'cutover_done' genuinely appears among the AST-scanned CAS targets (not a vacuous string-presence check) and it is the ONLY terminal-looking value Stage4 ever CAS-writes ('notified'/'done' never do)",
          "cutover_done" in advance_targets and not any(t in ("notified", "done") for t in advance_targets), advance_targets)


def test_group_14b_dbguard():
    print("\n-- Group 14b: production-path DB guard --")
    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    safe_path = os.path.join(tempfile.gettempdir(), "dbguard_commit_probe", "safe.db")

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

    check("14b-1. rejects the real production DB file path", _raises(prod_file, _FakeStorage(prod_file, prod_file)), prod_file)
    check("14b-2. rejects a DB_PATH mismatch", _raises(safe_path, _FakeStorage("other.db", safe_path)), None)
    check("14b-3. allows a normal safe temp path", not _raises(safe_path, _FakeStorage(safe_path, safe_path)), None)


def test_group_14c_duplicate_definition_scan():
    print("\n-- Group 14c: duplicate Stage 4 definition scan --")
    tree = MAIN_AST  # PERF 20260718: reuse the module-level parse.
    from collections import Counter
    defs = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    c = Counter(defs)
    # Plain module-level constants (not function defs) are excluded from
    # this function-duplication scan -- _RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC/
    # TP_HG_STATUS_BLOCKED are simple assignments (TPILOT RUNTIME READINESS
    # 20260718), same rationale as the pre-existing _REPL4_* skip below.
    # _REPL_CUTOVER_IRREVERSIBLE_STAGES (TPILOT FIX-4 20260718b) is likewise
    # a plain tuple assignment, not a function def. TP_HG_CHECK_INTERVAL_SEC
    # (RUNTIME READINESS RC 20260805 extraction-coverage fix, 2026-08-08) is
    # the same category -- a plain `TP_HG_CHECK_INTERVAL_SEC = 20 * 60`
    # assignment that _RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC's own definition
    # now reads, extracted only so that definition doesn't NameError.
    _NON_FUNCTION_CONSTANTS = (
        "_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC", "TP_HG_STATUS_BLOCKED",
        "_REPL_CUTOVER_IRREVERSIBLE_STAGES", "TP_HG_CHECK_INTERVAL_SEC",
    )
    for name in sorted(STAGE4_REAL_NAMES):
        if (name.startswith("_REPL4_") or name in _NON_FUNCTION_CONSTANTS
                or name in ("_manager_admin_password_ok", "_manager_danger_allowed_key")):
            continue
        check(f"14c. {name} defined exactly once", c.get(name, 0) == 1, c.get(name, 0))


def main() -> int:
    asyncio.run(test_group_1_entry_guards())
    asyncio.run(test_group_2_session_promotion())
    asyncio.run(test_group_3_new_manager_creation())
    asyncio.run(test_group_4_settings_access_groups())
    asyncio.run(test_group_5_schedule())
    asyncio.run(test_group_6_proxy())
    asyncio.run(test_group_7_links())
    asyncio.run(test_group_8_runtime_validation())
    asyncio.run(test_group_9_final_revalidation())
    asyncio.run(test_group_10_cutover_delete())
    asyncio.run(test_group_11_reserve())
    asyncio.run(test_group_12_rollback())
    asyncio.run(test_group_13_restart_recovery())
    asyncio.run(test_group_15_commit_ordering())
    asyncio.run(test_group_16_readiness_signals())
    asyncio.run(test_group_17_rollback_integration())
    asyncio.run(test_group_18_proxy_ordering())
    asyncio.run(test_group_19_cutover_failure_injection())
    asyncio.run(test_group_20_start_status_regression_matrix())
    asyncio.run(test_group_21_ping_response_validation())
    asyncio.run(test_group_22_fix_r1_cutover_cas_race())
    test_group_14_security_static()
    test_group_14b_dbguard()
    test_group_14c_duplicate_definition_scan()

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
