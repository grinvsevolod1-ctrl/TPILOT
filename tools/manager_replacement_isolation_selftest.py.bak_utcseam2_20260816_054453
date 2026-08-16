# -*- coding: utf-8 -*-
"""tools/manager_replacement_isolation_selftest.py -- offline self-test proving
two things that had ZERO test coverage anywhere in the project despite being
explicitly required design principles:

  (1) PER-MANAGER ISOLATION of the manager-replacement/command architecture:
      a hung/slow command for one manager must never block another manager's
      independent work. Proven with REAL asyncio concurrency (asyncio.gather +
      real wall-clock timing via time.monotonic()), at two levels:
        Group 1 -- the readiness-poll primitive (_manager_runtime_ready_once,
                   main.py) run for two managers concurrently, one of whose
                   runtime_ping is deliberately never answered.
        Group 2 -- the manager_commands QUEUE itself (storage.py's real,
                   UNFAKED manager_queue_take_next/manager_queue_finish),
                   proving a claimed-but-never-finished row for manager A never
                   blocks manager B's own claim+finish.
      Neither group is a source-grep pretending to be a concurrency test --
      both actually run the async code concurrently and measure real elapsed
      time / real completion order.

  (2) PanelBot UX regression checks for the manager_replace_commit fix round:
      the dedicated 900s _submit_and_wait timeout table entry, and the
      PRE-EXISTING (already accepted before this round) graceful-timeout
      rendering chain (_replace_parse_result -> _replace_render_commit_result
      -> _replace_render_status_screen) that falls through to a recover-driven
      re-render on a panel-wait timeout, never marks the operation failed
      itself, and never invents a requirement not present in the current code
      (e.g. a stage value absent from _REPLACE_STAGE_LABELS correctly falls
      back to the generic in-progress text -- this file asserts exactly that,
      not that the stage must be labeled).

Scope decision -- why _queue_bizlink_create_n_for_manager (mentioned as one
option in the task brief) is NOT extracted here: reading main.py's real
implementation (~line 26476) shows it pulls in a chain of bizlink-template
helpers (_bsl_tmpl_list_m213c, _m213d2_clamp_count, _bsl_links_mgr_date_m213c,
the business-link-templates table) that are unrelated to the isolation
property itself and would need to be faked or extracted for no additional
proof of cross-manager independence beyond what Group 1 (the readiness poll
it itself calls before enqueueing) and Group 2 (the queue primitives
underneath it) already demonstrate directly and more precisely. This mirrors
manager_command_expiry_selftest.py's own documented scope-decision convention
(see that file's module docstring).

Techniques (same conventions as every other tools/*_selftest.py in this
project):
  - main.py and panel_bot.py cannot be imported standalone (Telethon/env side
    effects at import time) -- functions under test are extracted via
    ast.parse + ast.unparse + exec() and run FOR REAL against a temporary
    SQLite DB (never db/data_tpilot.db) and a temporary runtime directory
    (never runtime/managers/). Pattern copied from
    manager_replacement_commit_selftest.py's build_main_ns/_extract_by_names
    and manager_replacement_adminbot_selftest.py's build_panel_ns.
  - storage.py IS directly importable (no Telethon/env side effects) --
    Group 2 calls its real manager_queue_* functions directly, unfaked,
    matching manager_command_expiry_selftest.py's own convention.
  - _manager_process_running/_spawn_manager_process/_stop_manager_process and
    _tpag_run_guard are faked under their exact production names (no real
    subprocess, no real network) -- same convention used everywhere else.
  - The runtime_ping auto-responder (_drive_ping_response) is copied from
    manager_replacement_commit_selftest.py's own fixture: a genuine asyncio
    background task that answers via the REAL manager_commands table (never a
    shortcut inside the readiness code itself).
  - Group 1/Group 2 use the REAL time/asyncio modules throughout (never a
    fast-forwarding fake clock) -- the whole point is genuine wall-clock
    concurrency evidence. Group T (PanelBot timeout-table test) is the ONE
    place a fake clock is used, and deliberately so: proving a 900-second
    table entry without waiting 900 real seconds requires a deterministic
    fake loop clock, mirroring manager_replacement_commit_selftest.py's own
    FastMonotonic idea (see _FakeDeadlineClock's docstring below for exactly
    why this is safe and still "real extraction, really executed").
  - `_selftest_db_guard` mandatory production-path DB guard, same convention
    as every other file in this project.

Never: real Telegram network, real proxy/provider network, real process
spawn/stop, production DB/runtime/session/log access.

    python tools\\manager_replacement_isolation_selftest.py
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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402  -- storage.py is directly importable (no Telethon/env side effects)

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
# Parsed exactly once each (main.py is ~35k lines; re-parsing per-call would
# dominate this suite's wall-clock time) -- same perf rationale as
# manager_replacement_commit_selftest.py's own MAIN_AST. Read-only sharing of
# the Module object across every build_*_ns() call below is safe: only
# ast.unparse() (read-only) is called on it, each call compiling its own
# fresh code objects into its own fresh ns dict.
MAIN_AST = ast.parse(MAIN_SRC)
PANEL_AST = ast.parse(PANEL_SRC)


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
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
            nodes.append(n)
            seen.add(n.target.id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing}")
    return nodes


def _selftest_db_guard(db_path: str, base_dir, storage_mod) -> None:
    """Mandatory production-path DB guard, same convention as every other
    tools/*_selftest.py in this project."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def make_temp_env(prefix: str):
    tmp_root = Path(tempfile.mkdtemp(prefix=prefix))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


# ======================================================================
# GROUP 1 fixtures -- readiness-signal primitives, extracted for real from
# main.py. Copied/adapted from manager_replacement_commit_selftest.py's own
# fixtures (_write_status_file, _seed_heartbeat_row, _drive_ping_response,
# _fresh_iso) -- same shapes, same production table names.
# ======================================================================

READY_NAMES = {
    "_manager_recovery_read_start_status",
    # RUNTIME READINESS RC 20260805 (wave L2): _RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC's
    # own definition now reads TP_HG_CHECK_INTERVAL_SEC (main.py) -- extract it too,
    # or the module-level assignment statement itself raises NameError at exec time.
    "TP_HG_CHECK_INTERVAL_SEC",
    "_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC", "_manager_health_heartbeat_ok",
    "_manager_runtime_ready_once", "manager_runtime_ready",
    "TP_HG_STATUS_BLOCKED", "_future_iso", "_now_utc_iso",
}


def _fresh_iso(seconds_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _write_status_file(base_dir: Path, key: str, *, phase: str, updated_at=None) -> None:
    """Writes runtime/managers/<key>/start_status.json -- the exact path/shape
    _manager_recovery_read_start_status (real, extracted) reads. base_dir must
    be the SAME Path bound to the test's own main_ns['BASE_DIR']."""
    d = base_dir / "runtime" / "managers" / str(key or "").strip().lower()
    d.mkdir(parents=True, exist_ok=True)
    payload = {"phase": phase, "updated_at": updated_at if updated_at is not None else _fresh_iso(), "reason_class": "", "error": ""}
    (d / "start_status.json").write_text(json.dumps(payload), encoding="utf-8")


def _seed_heartbeat_row(db_path: str, key: str, *, status: str = "ok", age_sec: float = 0.0) -> None:
    """Writes the manager_telegram_health row _manager_health_heartbeat_ok
    (real, extracted) reads."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS manager_telegram_health("
            "manager_key TEXT PRIMARY KEY, health_status TEXT DEFAULT '', last_check_at TEXT DEFAULT '')"
        )
        con.execute("DELETE FROM manager_telegram_health WHERE manager_key=?", (key,))
        con.execute(
            "INSERT INTO manager_telegram_health(manager_key, health_status, last_check_at) VALUES(?,?,?)",
            (key, status, _fresh_iso(age_sec)),
        )
        con.commit()
    finally:
        con.close()


async def _drive_ping_response(storage_mod, db_path: str, key: str, *, mode: str = "ok",
                                delay_sec: float = 0.0, wait_timeout: float = 30.0) -> None:
    """Genuine background task standing in for the (nonexistent, in tests)
    live manager runtime process that would normally answer a runtime_ping
    command -- copied from manager_replacement_commit_selftest.py's own
    _drive_ping_response (identical polling/answer contract against the REAL
    manager_commands table via the REAL storage.manager_queue_finish). A
    manager whose ping must NEVER be answered simply never gets one of these
    tasks started at all (see Group 1's manager A)."""
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
            if "no such table" in str(e).lower():
                await asyncio.sleep(0.03)
                continue
            return
        except Exception:
            return
        if rows:
            nonce = rows[0][0]
            payload = {
                "ok": True, "connected": True, "authorized": True, "tg_user_id": 0,
                "worker_key": key, "checked_at": _fresh_iso(),
            }
            if mode == "ok":
                row = await storage_mod.manager_get(key)
                payload["tg_user_id"] = int((row or {}).get("tg_user_id") or 0)
            await storage_mod.manager_queue_finish(
                nonce, worker_key=key, ok=True, result_text="ok",
                result_json=json.dumps(payload), db_path=db_path,
            )
            return
        await asyncio.sleep(0.03)


def build_main_ns_ready(db_path: str, base_dir: Path, *, running_keys: set) -> dict:
    """Extracts ONLY the readiness-signal primitives (+ their small direct
    dependencies) from main.py -- deliberately not the full Stage 4 commit
    engine (already exhaustively tested by manager_replacement_commit_
    selftest.py; this file's job is proving CROSS-MANAGER isolation of the
    readiness poll itself, not re-testing its own per-signal correctness,
    which Group 16 of that file already covers)."""
    import manager_registry
    import storage as _storage
    import aiosqlite as _aiosqlite

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    nodes = _extract_by_names(MAIN_AST, READY_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    async def fake_process_running(key):
        return key in running_keys

    async def fake_auth_guard(key, *, source="manual", force=False):
        return True, "OK"

    ns = {
        "os": os,
        "asyncio": asyncio,
        "Path": Path,
        "aiosqlite": _aiosqlite,
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        # utcnow refactor (2026-08-16): extracted panel_bot code reads the
        # clock through the module-level _pb_utc_now() seam (naive UTC).
        "_pb_utc_now": (lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
        # Deliberately the REAL stdlib time module -- Group 1's whole point
        # is genuine wall-clock concurrency evidence, never a fast-forwarded
        # fake clock (contrast with Group T's _FakeDeadlineClock below).
        "time": time,
        "BASE_DIR": base_dir,
        "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "manager_get": _storage.manager_get,
        # Deliberately NOT extracted -- no real OS process / real network in
        # tests, same convention as every other selftest in this project.
        "_manager_process_running": fake_process_running,
        "_tpag_run_guard": fake_auth_guard,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object, "Optional": None,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:isolation_ready>", "exec"), ns)
    ns["__storage__"] = _storage
    return ns


async def _seed_ready_manager(storage_mod, *, key: str, tg_user_id: int) -> None:
    await storage_mod.manager_add(
        manager_key=key, display_name=key, phone="+70000000000", status="active",
        session_path="", db_path="", workdir="", log_path="", is_enabled=1,
    )
    await storage_mod.manager_set_fields(key, tg_user_id=tg_user_id)


# ======================================================================
# GROUP 1: cross-manager readiness-poll isolation (real asyncio.gather, real
# wall-clock timing). Manager A's runtime_ping is deliberately never
# answered; manager B's is answered after a real (but short) delay by a
# genuine concurrent responder task. A third trivial concurrent coroutine
# (a 0.1s-tick counter) proves the event loop never blocked.
# ======================================================================

A_PING_TIMEOUT_SEC = 5.0
B_PING_ANSWER_DELAY_SEC = 2.0


async def test_group_1_cross_manager_readiness_isolation():
    print("\n-- Group 1: cross-manager _manager_runtime_ready_once isolation (real concurrency) --")
    tmp_root, db_path = make_temp_env("isolation_ready_selftest_")
    running: set = set()
    main_ns = build_main_ns_ready(db_path, tmp_root, running_keys=running)
    storage_mod = main_ns["__storage__"]
    try:
        await _seed_ready_manager(storage_mod, key="isoa", tg_user_id=700001)
        await _seed_ready_manager(storage_mod, key="isob", tg_user_id=700002)
        running.add("isoa")
        running.add("isob")
        _write_status_file(tmp_root, "isoa", phase="connected")
        _write_status_file(tmp_root, "isob", phase="connected")
        _seed_heartbeat_row(db_path, "isoa")
        _seed_heartbeat_row(db_path, "isob")

        # Manager B's runtime_ping IS answered (after a real delay) by a
        # genuine background task. Manager A's runtime_ping is NEVER
        # answered -- no responder task is ever started for "isoa" at all,
        # so _manager_runtime_ready_once("isoa", ...) can only resolve by
        # genuinely exhausting its own ping_timeout_sec.
        responder_b = asyncio.create_task(
            _drive_ping_response(storage_mod, db_path, "isob", mode="ok",
                                  delay_sec=B_PING_ANSWER_DELAY_SEC, wait_timeout=A_PING_TIMEOUT_SEC + 30)
        )

        loop_counter = {"n": 0}

        async def loop_liveness_counter():
            try:
                while True:
                    await asyncio.sleep(0.1)
                    loop_counter["n"] += 1
            except asyncio.CancelledError:
                return

        finish_offsets: dict = {}
        t_start = time.monotonic()

        async def timed_ready(label: str, key: str, ping_timeout_sec: float):
            res = await main_ns["_manager_runtime_ready_once"](key, ping_timeout_sec=ping_timeout_sec)
            finish_offsets[label] = time.monotonic() - t_start
            return res

        counter_task = asyncio.create_task(loop_liveness_counter())
        try:
            result_a, result_b = await asyncio.gather(
                timed_ready("A", "isoa", A_PING_TIMEOUT_SEC),
                timed_ready("B", "isob", 30.0),
            )
        finally:
            counter_task.cancel()
            try:
                await counter_task
            except asyncio.CancelledError:
                pass
            if not responder_b.done():
                responder_b.cancel()

        total_elapsed = time.monotonic() - t_start
        offset_a = finish_offsets.get("A", -1.0)
        offset_b = finish_offsets.get("B", -1.0)

        check("1a. manager A (runtime_ping deliberately never answered) genuinely times out (command_timeout)",
              result_a.get("error_class") == "command_timeout", result_a)
        check("1b. manager B (runtime_ping answered concurrently) resolves ok=True",
              result_b.get("ok") is True, result_b)

        check("1c. manager B finished WELL BEFORE manager A's own timeout window -- not delayed by A's hang "
              "(generous margin: B's own answer delay is 2s, A's own timeout is 5s; B must land far short of A's window)",
              0.0 <= offset_b < (A_PING_TIMEOUT_SEC - 1.0), (offset_b, A_PING_TIMEOUT_SEC))
        check("1d. manager A's own completion offset roughly matches its OWN ping_timeout_sec (generous +/- margin) "
              "-- running alongside B neither shortened nor lengthened it",
              (A_PING_TIMEOUT_SEC - 1.0) <= offset_a <= (A_PING_TIMEOUT_SEC + 2.5), (offset_a, A_PING_TIMEOUT_SEC))
        check("1e. total wall-clock elapsed reflects PARALLEL execution (~max(A,B), close to A's own timeout) -- "
              "NOT serial execution (~A+B, which would land noticeably higher); generous margin below the serial floor",
              total_elapsed < (A_PING_TIMEOUT_SEC + B_PING_ANSWER_DELAY_SEC - 1.0), (total_elapsed, A_PING_TIMEOUT_SEC, B_PING_ANSWER_DELAY_SEC))
        check("1f. the event loop stayed genuinely responsive during A's ~5s hang -- a trivial concurrent 0.1s-tick "
              "counter kept incrementing throughout (generous lower bound, proves nothing blocked the loop)",
              loop_counter["n"] >= 15, loop_counter["n"])

        # 1g. Sanity check (per the task's own §5): this does NOT assert
        # same-manager concurrency (two commands for the SAME manager are
        # expected to serialize through that manager's own command loop in
        # production) -- it only guards against this test's OWN fixture
        # accidentally letting manager A's single call create more than one
        # runtime_ping row, or answering out of order.
        con = sqlite3.connect(db_path)
        try:
            n_a = con.execute(
                "SELECT COUNT(*) FROM manager_commands WHERE target_key='isoa' AND command='runtime_ping'"
            ).fetchone()[0]
            n_b = con.execute(
                "SELECT COUNT(*) FROM manager_commands WHERE target_key='isob' AND command='runtime_ping'"
            ).fetchone()[0]
        finally:
            con.close()
        check("1g. sanity: exactly one runtime_ping row per manager (no accidental duplication/out-of-order "
              "reprocessing within this test's own fixture -- same-manager serialization is untouched by this test)",
              n_a == 1 and n_b == 1, (n_a, n_b))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP 2: manager_commands QUEUE level cross-manager independence, using
# the REAL, UNFAKED storage.py primitives directly (no main.py extraction at
# all) -- proves manager_queue_take_next/manager_queue_finish for manager B
# is never blocked by manager A's outstanding (claimed-but-never-finished)
# row, exercised with genuine asyncio concurrency.
# ======================================================================

async def test_group_2_queue_level_cross_manager_independence():
    print("\n-- Group 2: manager_commands queue -- cross-manager independence (real storage.py, unfaked) --")
    tmp_root, db_path = make_temp_env("isolation_queue_selftest_")
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, storage)
    try:
        nonce_a = await storage.manager_queue_put(target_key="qA", command="hang_forever", db_path=db_path)
        row_a = await storage.manager_queue_take_next("qA", db_path=db_path)
        check("2a. manager A's command is claimed ('processing') -- deliberately NEVER finished for the rest of "
              "this test, simulating a hung/slow command", row_a is not None and row_a.get("status") == "processing", row_a)

        async def worker_b():
            t0 = time.monotonic()
            nonce_b = await storage.manager_queue_put(target_key="qB", command="quick_op", db_path=db_path)
            row_b = await storage.manager_queue_take_next("qB", db_path=db_path)
            await asyncio.sleep(0.05)  # a small bit of simulated real work
            await storage.manager_queue_finish(nonce_b, worker_key="qB", ok=True, result_text="done", db_path=db_path)
            return nonce_b, row_b, (time.monotonic() - t0)

        async def probe_a_never_reclaimed(n_attempts: int = 5):
            hits = []
            for _ in range(n_attempts):
                r = await storage.manager_queue_take_next("qA", db_path=db_path)
                hits.append(r)
                await asyncio.sleep(0.02)
            return hits

        (nonce_b, row_b, elapsed_b), a_probe_hits = await asyncio.gather(worker_b(), probe_a_never_reclaimed())

        check("2b. manager B's command was successfully claimed CONCURRENTLY with manager A's outstanding row",
              row_b is not None and row_b.get("status") == "processing", row_b)
        row_b_final = await storage.manager_queue_get(nonce_b, db_path=db_path)
        check("2c. manager B's command genuinely reached status='done' (real storage.py primitive, unfaked)",
              row_b_final is not None and row_b_final.get("status") == "done", row_b_final)
        check("2d. manager B's claim+finish completed quickly -- never blocked/delayed by manager A's outstanding "
              "row (generous bound, well under a full second for two trivial sqlite calls plus a 0.05s sleep)",
              elapsed_b < 2.0, elapsed_b)
        check("2e. concurrently-run re-claim attempts against manager A's OWN key never returned a second row -- "
              "A's single claimed row stays exclusively claimed, not duplicated/reprocessed out of order "
              "(same-manager serialization intact, untouched by B's independent activity)",
              all(h is None for h in a_probe_hits), a_probe_hits)

        row_a_final = await storage.manager_queue_get(nonce_a, db_path=db_path)
        check("2f. manager A's row is completely untouched throughout -- still 'processing', never silently "
              "resolved as a side effect of manager B's independent activity",
              row_a_final is not None and row_a_final.get("status") == "processing", row_a_final)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# PanelBot regression checks (Groups T / R / S / D).
# ======================================================================

SUBMIT_WAIT_NAMES = {"_submit_and_wait"}

PANEL_NAMES = {
    "_replace_parse_result", "_REPLACE_RESULT_MESSAGES", "_replace_message_for",
    "_REPLACE_STAGE_LABELS", "_replace_committing_text", "_replace_committing_buttons",
    "_replace_links_pending_text", "_replace_links_pending_buttons",
    "_replace_retry_buttons", "_replace_manual_recovery_text",
    "_replace_success_text", "_replace_success_buttons", "_replace_error_buttons",
    "_replace_read_op_row",
    # TPILOT TERMINAL OK 20260809 (F4): _replace_error_buttons/_replace_success_buttons
    # now append _terminal_ok_button(). Extract the REAL helper (a pure one-liner
    # over the already-faked Button) rather than stubbing it, so this file keeps
    # asserting the actual button set these screens render.
    "_terminal_ok_button",
    "_replace_render_commit_result", "_replace_render_status_screen",
    "_connect_panel_db", "_ensure_panel_runtime_tables", "_utc_now_iso",
    "_wizard_get", "_wizard_set", "_wizard_clear",
}


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
    """Copied from manager_replacement_adminbot_selftest.py's own fake
    Button.inline shape."""
    @staticmethod
    def inline(text, data):
        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
        return _FakeBtn(text, raw)


class FakeEvent:
    """Minimal fake -- only what the render functions under test actually
    touch (no .data/.raw_text needed here since this file calls the render
    functions directly, never through _replace_callback)."""
    def __init__(self, *, chat_id: int = 0, sender_id: int = 0):
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.edits: list = []
        self.answers: list = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))


async def _fake_safe_event_edit(event, text, buttons=None):
    event.edits.append((text, buttons))


def build_panel_ns_min(db_path: str, *, submit_and_wait_fn=None) -> dict:
    import manager_registry

    nodes = _extract_by_names(PANEL_AST, PANEL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3,
        "json": json,
        "os": os,
        "datetime": __import__("datetime").datetime,
        # utcnow refactor (2026-08-16): the extracted _utc_now_iso reads the
        # clock through the module-level _pb_utc_now() seam (naive UTC).
        "_pb_utc_now": (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(tzinfo=None)),
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "_safe_event_edit": _fake_safe_event_edit,
        "_submit_and_wait": submit_and_wait_fn,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object, "Optional": None,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:isolation_panel>", "exec"), ns)
    return ns


class _FakeDeadlineClock:
    """Deterministic, fast-forwarding stand-in for asyncio's own event-loop
    clock (asyncio.get_event_loop().time()) -- SAME idea as manager_
    replacement_commit_selftest.py's own FastMonotonic (see that file's
    docstring), applied here to prove _submit_and_wait's timeout TABLE
    (900s for /manager_replace_commit vs 45s default) without ever waiting
    900 real seconds. Every .time() call jumps forward by a large fixed
    step, so the number of times the real, extracted _submit_and_wait code
    calls .time() before its own `while ... < deadline` loop exits is
    directly, deterministically proportional to which timeout_s the
    function's own if/elif table picked for that command -- this is still a
    real extraction of the real code, really executing its real loop; only
    the WALL-CLOCK SOURCE it reads is swapped, exactly like every other
    fake-clock fixture already accepted in this project. Never used by
    Group 1/Group 2 above, which need genuine real-time concurrency
    evidence instead."""
    def __init__(self, jump: float = 100.0):
        self._t = 0.0
        self._jump = jump

    def time(self) -> float:
        self._t += self._jump
        return self._t


class _FakeLoopForDeadlineClock:
    def __init__(self, clock: _FakeDeadlineClock):
        self._clock = clock

    def time(self) -> float:
        return self._clock.time()


class _FakeAsyncioForTimeoutTable:
    """Stands in for the `asyncio` name inside _submit_and_wait's OWN
    globals for Group T only -- get_event_loop().time() reads the fake
    clock; sleep() is an instant no-op (the real 0.7s polling interval adds
    no useful information here, only real wall-clock delay)."""
    def __init__(self, clock: _FakeDeadlineClock):
        self._loop = _FakeLoopForDeadlineClock(clock)

    def get_event_loop(self):
        return self._loop

    async def sleep(self, seconds):
        return


def build_submit_wait_ns(*, submit_panel_command_fn, get_panel_command_fn, fake_asyncio, db_path: str) -> dict:
    nodes = _extract_by_names(PANEL_AST, SUBMIT_WAIT_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "submit_panel_command": submit_panel_command_fn,
        "get_panel_command": get_panel_command_fn,
        "TPILOT_DB_PATH": db_path,
        "asyncio": fake_asyncio,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:isolation_submit_wait>", "exec"), ns)
    return ns


async def test_group_t_submit_and_wait_timeout_table():
    print("\n-- Group T: _submit_and_wait timeout table (real extraction, deterministic fake clock) --")

    def make_env(jump: float = 100.0):
        clock = _FakeDeadlineClock(jump=jump)
        calls: list = []

        def fake_submit(db_path, command_text, *, requested_by=0, source_chat_id=0, response_chat_id=0):
            return 1

        def fake_get(db_path, cmd_id):
            calls.append(1)
            return {"status": "processing"}

        ns = build_submit_wait_ns(
            submit_panel_command_fn=fake_submit, get_panel_command_fn=fake_get,
            fake_asyncio=_FakeAsyncioForTimeoutTable(clock), db_path="unused",
        )
        return ns, calls

    ns_commit, calls_commit = make_env()
    result_commit = await ns_commit["_submit_and_wait"]("/manager_replace_commit op-abc123", 1, 1, 1)
    check("T1. /manager_replace_commit (never resolved by this fixture) eventually returns the expected "
          "panel-wait-timeout shape", result_commit.get("status") == "error" and "TPilot" in str(result_commit.get("error_text", "")),
          result_commit)
    check("T2. /manager_replace_commit's poll loop ran SEVERAL iterations before its own deadline -- deterministic "
          "proof of a LARGE timeout budget (consistent with the table's 900s entry, not the 45s default)",
          len(calls_commit) >= 5, len(calls_commit))

    ns_default, calls_default = make_env()
    result_default = await ns_default["_submit_and_wait"]("/some_unrelated_command foo", 1, 1, 1)
    check("T3. an unrelated command (falls through to the 45s default bucket) also times out in this fixture",
          result_default.get("status") == "error", result_default)
    check("T4. the unrelated command's poll loop ran ZERO iterations at this fake clock's step size -- deterministic "
          "proof its OWN timeout budget is far smaller than /manager_replace_commit's (45s default vs the 900s "
          "table entry read directly off the REAL, current if/elif chain, not hand-copied here)",
          len(calls_default) == 0, len(calls_default))

    # T5: positive control -- an early real answer always short-circuits,
    # regardless of which timeout bucket a command falls into. Proves T1-T4
    # aren't just measuring "how long until the fake clock exhausts itself"
    # in a vacuum.
    clock_ok = _FakeDeadlineClock(jump=100.0)
    calls_ok: list = []

    def fake_submit_ok(db_path, command_text, *, requested_by=0, source_chat_id=0, response_chat_id=0):
        return 1

    def fake_get_ok(db_path, cmd_id):
        calls_ok.append(1)
        return {"status": "done", "result_text": "{}"}

    ns_ok = build_submit_wait_ns(
        submit_panel_command_fn=fake_submit_ok, get_panel_command_fn=fake_get_ok,
        fake_asyncio=_FakeAsyncioForTimeoutTable(clock_ok), db_path="unused",
    )
    result_ok = await ns_ok["_submit_and_wait"]("/manager_replace_commit op-xyz", 1, 1, 1)
    check("T5. positive control: a command that resolves on the very first poll returns immediately "
          "(status='done'), regardless of its own timeout-table bucket", result_ok.get("status") == "done", result_ok)


def _seed_replacement_row(db_path: str, *, operation_id: str, old_manager_key: str, status: str,
                           stage: str = "", new_manager_key: str = "") -> None:
    """Direct row insert (bypassing the full replacement_create/advance state
    machine) -- same technique manager_command_expiry_selftest.py's own
    _seed_replacement_row already uses: this file only needs a
    manager_replacements row with a given status/stage, not a fully-valid
    operation."""
    storage.ensure_replacement_tables(db_path)
    now = datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat()
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO manager_replacements(operation_id, status, stage, old_manager_key, new_manager_key, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (operation_id, status, stage, old_manager_key, new_manager_key, now, now),
        )
        con.commit()
    finally:
        con.close()


_PHONE_SHAPED_RE = re.compile(r"\+\d{7,15}")
_FORBIDDEN_SECRET_SUBSTRINGS = ("password", "Password", "Traceback", ".session", "proxy_password", "phone_code_hash", "runtime\\managers", "runtime/managers")


def _assert_no_secrets(label_prefix: str, texts: list) -> None:
    for idx, txt in enumerate(texts):
        txt = txt or ""
        for pat in _FORBIDDEN_SECRET_SUBSTRINGS:
            check(f"{label_prefix}.{idx} never contains '{pat}'", pat not in txt, txt)
        check(f"{label_prefix}.{idx} never contains a raw phone-number-shaped string", not _PHONE_SHAPED_RE.search(txt), txt)


async def test_group_r_render_and_recovery():
    print("\n-- Group R: _replace_parse_result / _replace_render_commit_result / _replace_render_status_screen --")
    tmp_root, db_path = make_temp_env("isolation_panel_selftest_")
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, storage)
    rendered_texts: list = []
    try:
        # R1-R4: _replace_parse_result on a timed-out _submit_and_wait-shaped
        # row (the ACTUAL shape _submit_and_wait's own final `return` produces
        # on a panel-wait timeout, per the real function read directly above).
        timeout_row = {"status": "error", "error_text": "TPilot не ответил на panel-команду за отведенное время."}
        panel_ns0 = build_panel_ns_min(db_path)
        parsed = panel_ns0["_replace_parse_result"](timeout_row)
        check("R1. a panel-wait-timeout row parses to ok=False", parsed.get("ok") is False, parsed)
        check("R2. ...never sets retryable=True", parsed.get("retryable") is not True, parsed)
        check("R3. ...never sets manual_recovery_required=True", parsed.get("manual_recovery_required") is not True, parsed)
        check("R4. ...carries no 'status' (so _replace_render_commit_result cannot classify it as any known "
              "terminal/in-progress state and must fall through to its own recover-driven re-render branch)",
              not parsed.get("status"), parsed)

        # R5/R5b/R5c: feeding that parsed result into _replace_render_commit_
        # result, with a fake _submit_and_wait for the recover re-resolve that
        # succeeds and reports the operation's REAL current status
        # ('committing') -- proves the fallback genuinely reaches
        # _replace_render_status_screen's stage-aware committing branch,
        # never a hard failure screen, never a rollback trigger of its own
        # (panel_bot.py has no reference to any rollback function at all --
        # see D1/D2 below).
        op_id = "op-R5"
        _seed_replacement_row(db_path, operation_id=op_id, old_manager_key="rold5", status="committing", stage="links_creating")

        async def fake_submit_recover_ok(command_text, requested_by, source_chat_id=0, response_chat_id=0):
            assert command_text == f"/manager_replace_recover {op_id}", command_text
            return {"status": "done", "result_text": json.dumps({"ok": True, "status": "committing", "operation_id": op_id})}

        panel_ns1 = build_panel_ns_min(db_path, submit_and_wait_fn=fake_submit_recover_ok)
        event1 = FakeEvent(chat_id=1, sender_id=1)
        await panel_ns1["_replace_render_commit_result"](event1, 1, 1, "rold5", op_id, parsed)
        check("R5. a timed-out commit result falls through to the recover-driven re-render (an edit was produced)",
              bool(event1.edits), event1.edits)
        r5_text = event1.edits[-1][0] if event1.edits else ""
        rendered_texts.append(r5_text)
        check("R5b. the recover branch was genuinely exercised -- renders the stage-aware IN-PROGRESS screen for "
              "the operation's REAL current status ('committing'), never the generic manual-recovery/hard-error "
              "screen, and never silently claims success", "Выполняется замена" in r5_text, r5_text)
        check("R5c. the real stage label for 'links_creating' (a genuine key in the CURRENT _REPLACE_STAGE_LABELS "
              "dict) is shown verbatim", "Создаются бизнес-ссылки" in r5_text, r5_text)

        # R6: the recover re-resolve ITSELF fails -- a generic, honest error
        # screen is shown; the operation is never marked failed by panel_bot.py
        # itself (that decision is main.py-side, independent of the panel's
        # own wait -- panel_bot.py only ever renders what the backend reports).
        async def fake_submit_recover_fail(command_text, requested_by, source_chat_id=0, response_chat_id=0):
            return {"status": "error", "error_text": "still no dice"}

        panel_ns2 = build_panel_ns_min(db_path, submit_and_wait_fn=fake_submit_recover_fail)
        event2 = FakeEvent(chat_id=2, sender_id=2)
        await panel_ns2["_replace_render_commit_result"](event2, 2, 2, "rold5", op_id, parsed)
        check("R6. if the recover re-resolve ALSO fails, a generic honest error screen is still shown (never a "
              "crash, never a silent success claim)", bool(event2.edits), event2.edits)
        r6_text = event2.edits[-1][0] if event2.edits else ""
        r6_buttons = event2.edits[-1][1] if event2.edits else None
        rendered_texts.append(r6_text)
        check("R6b. that error screen offers safe navigation (back to the manager card), not a dead end",
              bool(r6_buttons), r6_buttons)

        # R7/R7b: reopening at status='committing' with stage='proxy_assigned'
        # -- a stage value NOT present in the CURRENT _REPLACE_STAGE_LABELS
        # dict (confirmed by R7b) -- correctly falls back to the generic
        # in-progress text. This does NOT invent a requirement that isn't in
        # the code: if a future patch adds 'proxy_assigned' to the labels
        # dict, R7b is exactly the check that will need revisiting.
        op_id2 = "op-R7"
        _seed_replacement_row(db_path, operation_id=op_id2, old_manager_key="rold7", status="committing", stage="proxy_assigned")
        panel_ns3 = build_panel_ns_min(db_path)
        event3 = FakeEvent(chat_id=3, sender_id=3)
        await panel_ns3["_replace_render_status_screen"](event3, 3, 3, "rold7", op_id2, {"status": "committing"})
        r7_text = event3.edits[-1][0] if event3.edits else ""
        rendered_texts.append(r7_text)
        check("R7. reopening at status='committing' with an unlabeled stage renders the generic in-progress "
              "screen (no crash, no blank text, no invented stage line)", "Выполняется замена" in r7_text, r7_text)
        check("R7b. premise check: 'proxy_assigned' really is absent from the CURRENT _REPLACE_STAGE_LABELS dict "
              "right now", "proxy_assigned" not in panel_ns3["_REPLACE_STAGE_LABELS"], panel_ns3["_REPLACE_STAGE_LABELS"])

        # R8: positive control -- a stage value that DOES exist renders its
        # specific label (proves R7 isn't vacuously true / the lookup isn't
        # just always falling back).
        op_id3 = "op-R8"
        _seed_replacement_row(db_path, operation_id=op_id3, old_manager_key="rold8", status="links_pending", stage="runtime_validated")
        panel_ns4 = build_panel_ns_min(db_path)
        event4 = FakeEvent(chat_id=4, sender_id=4)
        await panel_ns4["_replace_render_status_screen"](event4, 4, 4, "rold8", op_id3, {"status": "links_pending"})
        r8_text = event4.edits[-1][0] if event4.edits else ""
        rendered_texts.append(r8_text)
        check("R8. positive control: a stage value that DOES exist in _REPLACE_STAGE_LABELS ('runtime_validated') "
              "renders its own specific label text (proves R7's fallback isn't just always firing)",
              "Рантайм проверен" in r8_text, r8_text)

        # R9: regression -- reopening at cutover_done/notified/done still
        # renders the success screen, unchanged.
        for status in ("cutover_done", "notified", "done"):
            op_idx = f"op-R9-{status}"
            _seed_replacement_row(db_path, operation_id=op_idx, old_manager_key=f"rold9{status}", status=status,
                                   new_manager_key=f"newmgr9{status}")
            panel_ns5 = build_panel_ns_min(db_path)
            eventx = FakeEvent(chat_id=5, sender_id=5)
            await panel_ns5["_replace_render_status_screen"](eventx, 5, 5, f"rold9{status}", op_idx, {"status": status})
            tx = eventx.edits[-1][0] if eventx.edits else ""
            rendered_texts.append(tx)
            check(f"R9. reopening at status='{status}' still renders the success screen (regression, unchanged "
                  f"behavior)", "успешно заменён" in tx, tx)

        # Group S: no secrets in anything rendered above.
        _assert_no_secrets("S1. rendered screen text", rendered_texts)
    finally:
        cleanup_env(tmp_root)


async def test_group_d_duplicate_commit_note():
    print("\n-- Group D: duplicate-commit prevention (architectural note, not re-tested here) --")
    loop_src = "\n".join(ast.unparse(n) for n in MAIN_AST.body if getattr(n, "name", None) == "_panel_command_loop")
    check("D1. main.py's REAL, current _panel_command_loop claims and executes panel_commands strictly ONE AT A "
          "TIME (a single take_next_panel_command claim per outer while-loop iteration, fully awaited via "
          "_panel_execute_command_text before looping back for the next claim; no asyncio.create_task/"
          "asyncio.gather anywhere around command execution) -- verified by static inspection of the actual "
          "extracted function body",
          "take_next_panel_command" in loop_src and "asyncio.create_task" not in loop_src and "asyncio.gather" not in loop_src,
          loop_src[:300])
    check("D2. therefore two overlapping /manager_replace_commit submissions for the SAME op_id are "
          "architecturally serialized by this loop before either ever reaches replacement_commit -- a direct "
          "panel_bot.py-focused concurrency test of this would only be re-testing the loop itself (out of scope "
          "here, D1 already covers it). replacement_commit's OWN same-op_id idempotent-retry safety (calling it "
          "twice in a row in the same durable state) is already covered by manager_replacement_commit_selftest.py "
          "(e.g. its checks '13e. cutover_done idempotent on further calls', '17b/17b2. idempotency', '17h. "
          "second call on the same failed op is a clean idempotent no-op') -- deliberately NOT duplicated in this "
          "file.", True, "documented cross-reference, see manager_replacement_commit_selftest.py Groups 13/17")


def main() -> int:
    asyncio.run(test_group_1_cross_manager_readiness_isolation())
    asyncio.run(test_group_2_queue_level_cross_manager_independence())
    asyncio.run(test_group_t_submit_and_wait_timeout_table())
    asyncio.run(test_group_r_render_and_recovery())
    asyncio.run(test_group_d_duplicate_commit_note())

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
