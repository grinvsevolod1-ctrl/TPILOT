# -*- coding: utf-8 -*-
"""tools/manager_runtime_readiness_selftest.py -- offline selftest for the
RUNTIME READINESS + BUSINESS LINK RECOVERY RELIABILITY release candidate,
wave L2 (readiness).

Root cause under test (confirmed by a read-only production forensic audit,
not reproduced here):

  TP_HG_CHECK_INTERVAL_SEC            = 20*60 = 1200 s   (main.py, health writer)
  _RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC =        900 s    (main.py, readiness reader)

1200 > 900, so during the last >=5 minutes of every 20-minute health cycle a
perfectly alive manager runtime is deterministically declared
`runtime_not_ready` by the readiness gate -- >=25 % of wall-clock time. The
production incident (`panel_commands` id 2173, `/bizlink_create_n dwanders0 15
2026-08-06 force`) matches exactly: taken_at 22:59:18 -> finished_at 22:59:19,
i.e. ONE second, which excludes the runtime_ping round trip (ping_timeout_sec=5)
and leaves the heartbeat signal as the only pre-ping signal that yields
`runtime_not_ready`. Heartbeat age at that instant on the 20-minute grid was
~1140 s > 900 s.

Three secondary defects of the same gate are covered here as well:
  * any heartbeat read exception (`database is locked` is confirmed in this
    project) becomes a FINAL runtime_not_ready instead of a transient signal;
  * the gate is a single non-polling pass (`_manager_runtime_ready_once`,
    ping_timeout_sec=5) even though a bounded poller (`manager_runtime_ready`)
    already exists in the same file and is used by the replacement path;
  * `runtime_not_running` and `runtime_not_ready` share one user-facing text.

Invariants this selftest MUST keep green (they are not defects and must never
be weakened by the fix):
  * TP_HG_STATUS_BLOCKED is a hard fail in every mode;
  * the replacement/onboarding path stays STRICT (advisory is opt-in only, so
    the default value of the new flag must remain False);
  * runtime_ping stays the authoritative connected/authorized/identity proof;
  * Auth Guard / Proxy Guard semantics are untouched.

Technique: main.py cannot be imported standalone (Telethon/env side effects at
import time). Every function under test is extracted straight from the CURRENT
main.py source via ast.parse + ast.unparse + exec(), the same idiom used by
tools/bizlink_delete_result_selftest.py. Mutation controls re-extract with one
targeted textual substitution applied to the unparsed source, simulating a
regression, and assert the corresponding check goes RED.

No Telegram. No production DB. Everything runs against a throwaway SQLite file
under %TEMP%.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES: list = []
PENDING: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def check_post_fix(label: str, condition: bool, detail: str = "",
                   *, unavailable: bool = False) -> None:
    """A post-L2 invariant. Before wave L2 lands, the code under test simply
    does not have the parameter/anchor yet, so the check cannot run at all --
    that is reported as PENDING, never as a silent pass. Once the fix is in
    (FIX_APPLIED), the very same check is enforced as a hard failure."""
    if unavailable and not fix_applied():
        print(f"[PEND] {label}  (waiting for wave L2)")
        PENDING.append(label)
        return
    check(label, condition, detail)


# ----------------------------------------------------------------------
# Generic AST extraction helpers (same contract as
# tools/bizlink_delete_result_selftest.py)
# ----------------------------------------------------------------------

_PARSE_CACHE: dict = {}


def _cached_parse(main_src: str):
    cached = _PARSE_CACHE.get(id(main_src))
    if cached is not None and cached[0] is main_src:
        return cached[1]
    tree = ast.parse(main_src)
    _PARSE_CACHE[id(main_src)] = (main_src, tree)
    return tree


def _top_level_node(main_src: str, name: str):
    tree = _cached_parse(main_src)
    nodes = [n for n in tree.body if getattr(n, "name", None) == name]
    if not nodes:
        raise AssertionError(f"{name} not found as a top-level def in main.py")
    return nodes[-1]  # last-wins, per this project's override convention


def _top_level_assign_src(main_src: str, name: str) -> str:
    """Unparsed source of the LAST module-level `name = ...` assignment."""
    tree = _cached_parse(main_src)
    found = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    found.append(node)
    if not found:
        raise AssertionError(f"{name} not found as a top-level assignment in main.py")
    return ast.unparse(found[-1])


class MutationAnchorError(AssertionError):
    """Raised when a mutation anchor is missing or not unique. A distinct type
    so a mutation control can never count a silently-unapplied anchor as a
    successful RED."""


def _apply_subs(src: str, subs) -> str:
    if subs and isinstance(subs[0], str):
        subs = [subs]
    for old, new in subs:
        count = src.count(old)
        if count == 0:
            raise MutationAnchorError(f"mutation anchor not found: {old!r}")
        if count > 1:
            raise MutationAnchorError(
                f"mutation anchor is not unique ({count} occurrences): {old!r}"
            )
        src = src.replace(old, new, 1)
    return src


def _build_ns(main_src: str, names, extra_ns: dict, mutations: Optional[dict] = None,
              consts=()) -> dict:
    """Extract the named top-level defs (and optional module-level constant
    assignments) and exec them into a fresh namespace seeded with extra_ns."""
    parts = [_top_level_assign_src(main_src, c) for c in consts]
    for nm in names:
        src = ast.unparse(_top_level_node(main_src, nm))
        if mutations and nm in mutations:
            src = _apply_subs(src, mutations[nm])
    # constants first, then functions (a constant may reference another)
        parts.append(src)
    module_src = "\n\n".join(parts)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<main.py:{','.join(sorted(names))}>", "exec"), ns)
    return ns


# ----------------------------------------------------------------------
# Fake environment
# ----------------------------------------------------------------------

READINESS_FNS = ("_manager_health_heartbeat_ok", "_manager_runtime_ready_once",
                 "manager_runtime_ready", "_now_utc_iso", "_future_iso")
READINESS_CONSTS = ("TP_HG_CHECK_INTERVAL_SEC", "_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC")


class FakeQueue:
    """Stands in for storage.manager_queue_put / manager_queue_get.

    Records every enqueue so a test can assert that NOTHING was queued when
    readiness failed, and serves a scripted runtime_ping answer.
    """

    def __init__(self, *, ping_result: Optional[dict] = None, ping_status: str = "done",
                 put_error: Optional[Exception] = None, answer_from_attempt: int = 1):
        self.ping_result = ping_result
        self.ping_status = ping_status
        self.put_error = put_error
        self.answer_from_attempt = int(answer_from_attempt)
        self.puts: list = []
        self._attempt = 0

    async def manager_queue_put(self, **kw):
        if self.put_error is not None:
            raise self.put_error
        self._attempt += 1
        self.puts.append(dict(kw))
        return f"nonce-{self._attempt}"

    async def manager_queue_get(self, nonce, db_path=None):
        import json as _j
        attempt = int(str(nonce).rsplit("-", 1)[-1] or 0)
        if attempt < self.answer_from_attempt:
            # this attempt's ping is never answered -> command_timeout
            return {"status": "pending", "result_json": ""}
        if self.ping_result is None:
            return {"status": "pending", "result_json": ""}
        return {"status": self.ping_status,
                "result_json": _j.dumps(self.ping_result)}


def install_fake_storage(queue: FakeQueue) -> None:
    """_manager_runtime_ready_once does `from storage import manager_queue_put,
    manager_queue_get` at call time. Serve a stub module so the test never
    touches the real queue or the production DB."""
    mod = types.ModuleType("storage")
    mod.manager_queue_put = queue.manager_queue_put       # type: ignore[attr-defined]
    mod.manager_queue_get = queue.manager_queue_get       # type: ignore[attr-defined]
    sys.modules["storage"] = mod


class FlakyAiosqlite:
    """Wraps the real aiosqlite, raising `database is locked` for the first
    `fail_times` connect() calls. Models the confirmed SQLite contention in this
    project without needing a second writer process."""

    def __init__(self, real, fail_times: int = 0):
        self._real = real
        self.remaining = int(fail_times)
        self.calls = 0

    def connect(self, *a, **kw):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise sqlite3.OperationalError("database is locked")
        return self._real.connect(*a, **kw)

    def __getattr__(self, item):
        return getattr(self._real, item)


def make_health_db(*, health_status: str = "ok", age_sec: Optional[int] = 60,
                   key: str = "dwanders0", row: bool = True) -> str:
    """Throwaway SQLite with a single manager_telegram_health row.
    age_sec=None writes an empty last_check_at."""
    fd, path = tempfile.mkstemp(prefix="tpilot_readiness_", suffix=".db",
                                dir=tempfile.gettempdir())
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE manager_telegram_health("
            "manager_key TEXT PRIMARY KEY, health_status TEXT, last_check_at TEXT)"
        )
        if row:
            if age_sec is None:
                last = ""
            else:
                last = (datetime.utcnow() - timedelta(seconds=int(age_sec))
                        ).replace(microsecond=0).isoformat()
            con.execute(
                "INSERT INTO manager_telegram_health(manager_key, health_status, last_check_at)"
                " VALUES (?,?,?)", (key, health_status, last))
        con.commit()
    finally:
        con.close()
    return path


def build_env(*, db_path: str, process_running: bool = True,
              start_status: Optional[dict] = None,
              registry_row: Optional[dict] = None,
              guard: Tuple[bool, str] = (True, ""),
              guard_exc: Optional[Exception] = None,
              queue: Optional[FakeQueue] = None,
              aiosqlite_fail_times: int = 0) -> dict:
    """Namespace for the extracted readiness functions. Every collaborator is a
    fake: no process lookup, no start_status.json on disk, no registry, no Auth
    Guard network call, no Telegram."""
    import aiosqlite as _real_aiosqlite

    if start_status is None:
        start_status = {"phase": "running", "pid": 4628,
                        "updated_at": "2026-08-04T16:00:24+00:00"}
    if registry_row is None:
        registry_row = {"manager_key": "dwanders0", "tg_user_id": 777001}
    if queue is None:
        queue = FakeQueue(ping_result={
            "ok": True, "connected": True, "authorized": True,
            "tg_user_id": 777001, "worker_key": "dwanders0",
            "checked_at": datetime.utcnow().replace(microsecond=0).isoformat(),
        })
    install_fake_storage(queue)

    async def _manager_process_running(manager_key: str) -> bool:
        return bool(process_running)

    def _manager_recovery_read_start_status(manager_key: str) -> dict:
        return dict(start_status or {})

    async def manager_get(key: str):
        return dict(registry_row) if registry_row else None

    async def _tpag_run_guard(key: str, *, source: str = "manual", force: bool = False,
                              min_consecutive_ok: int = 1):
        if guard_exc is not None:
            raise guard_exc
        return guard

    def registry_normalize_manager_key(raw) -> str:
        return str(raw or "").strip().lower()

    ns = {
        "aiosqlite": FlakyAiosqlite(_real_aiosqlite, aiosqlite_fail_times),
        "asyncio": asyncio,
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        "time": __import__("time"),
        "Any": Any, "Dict": Dict, "Optional": Optional, "Tuple": Tuple,
        "TPILOT_DB_PATH": db_path,
        "TP_HG_STATUS_BLOCKED": "blocked",
        "TP_HG_STATUS_LIMITED": "limited",
        "_manager_process_running": _manager_process_running,
        "_manager_recovery_read_start_status": _manager_recovery_read_start_status,
        "manager_get": manager_get,
        "_tpag_run_guard": _tpag_run_guard,
        "registry_normalize_manager_key": registry_normalize_manager_key,
    }
    ns["_fake_queue"] = queue
    return ns


def load_readiness(extra_ns: dict, mutations: Optional[dict] = None,
                   const_mutations: Optional[dict] = None) -> dict:
    """Extract + exec the readiness functions. const_mutations lets a mutation
    control rewrite a module-level constant (e.g. restore the 900 s threshold)."""
    parts = []
    for c in READINESS_CONSTS:
        src = _top_level_assign_src(MAIN_SRC, c)
        if const_mutations and c in const_mutations:
            src = f"{c} = {const_mutations[c]}"
        parts.append(src)
    for nm in READINESS_FNS:
        src = ast.unparse(_top_level_node(MAIN_SRC, nm))
        if mutations and nm in mutations:
            src = _apply_subs(src, mutations[nm])
        parts.append(src)
    ns = dict(extra_ns)
    exec(compile("\n\n".join(parts), "<main.py:readiness>", "exec"), ns)
    return ns


def fix_applied() -> bool:
    """True once wave L2 has introduced the heartbeat_advisory parameter."""
    node = _top_level_node(MAIN_SRC, "_manager_runtime_ready_once")
    return any(a.arg == "heartbeat_advisory"
               for a in getattr(node.args, "kwonlyargs", []))


def call_ready_once(ns, key="dwanders0", **kw):
    """Call _manager_runtime_ready_once, tolerating the pre-fix signature that
    has no heartbeat_advisory parameter -- an unsupported kwarg is itself the
    defect and is reported as such rather than crashing the run."""
    fn = ns["_manager_runtime_ready_once"]
    try:
        return asyncio.run(fn(key, **kw))
    except TypeError as e:
        if "heartbeat_advisory" in str(e):
            return {"ok": False, "error_class": "SIGNATURE_MISSING",
                    "detail": "heartbeat_advisory parameter does not exist yet",
                    "signal": "n/a"}
        raise


def call_ready_poll(ns, key="dwanders0", **kw):
    fn = ns["manager_runtime_ready"]
    try:
        return asyncio.run(fn(key, **kw))
    except TypeError as e:
        if "heartbeat_advisory" in str(e):
            return {"ok": False, "error_class": "SIGNATURE_MISSING",
                    "detail": "heartbeat_advisory parameter does not exist yet",
                    "signal": "n/a"}
        raise


# ----------------------------------------------------------------------
# A. Confirmed-defect checks (RED before wave L2, GREEN after)
# ----------------------------------------------------------------------

def run_defect_checks() -> None:
    print("\n--- A. confirmed-defect checks (must be RED before the L2 fix) ---")

    # A0: the arithmetic root cause itself.
    ns = load_readiness(build_env(db_path=make_health_db()))
    interval = int(ns["TP_HG_CHECK_INTERVAL_SEC"])
    threshold = int(ns["_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC"])
    check("A0 stale threshold covers >= 2 health-writer cycles",
          threshold >= interval * 2,
          f"threshold={threshold}s must be >= 2*{interval}s; a threshold below one "
          f"full cycle rejects a live runtime for {max(0, interval - threshold)}s "
          f"of every {interval}s")

    # A1: live runtime, heartbeat older than the threshold, ping healthy.
    # This is the production incident, reproduced.
    db = make_health_db(age_sec=1140)  # 19 min -- exactly the incident's age
    ns = load_readiness(build_env(db_path=db))
    res = call_ready_once(ns, heartbeat_advisory=True)
    check("A1 stale heartbeat + healthy ping -> ALLOWED",
          bool(res.get("ok")),
          f"got error_class={res.get('error_class')!r} signal={res.get('signal')!r} "
          f"detail={res.get('detail')!r}")

    # A2: heartbeat read raises `database is locked`; ping healthy.
    db = make_health_db(age_sec=60)
    env = build_env(db_path=db, aiosqlite_fail_times=1)
    ns = load_readiness(env)
    res = call_ready_once(ns, heartbeat_advisory=True)
    check("A2 transient SQLite lock on heartbeat + healthy ping -> ALLOWED",
          bool(res.get("ok")),
          f"got error_class={res.get('error_class')!r} detail={res.get('detail')!r}")

    # A2b: the same transient failure through the bounded poller.
    db = make_health_db(age_sec=60)
    env = build_env(db_path=db, aiosqlite_fail_times=1)
    ns = load_readiness(env)
    res = call_ready_poll(ns, deadline_sec=6, poll_interval_sec=0.5,
                          ping_timeout_sec=2, heartbeat_advisory=True)
    check("A2b bounded poller recovers from a transient heartbeat failure",
          bool(res.get("ok")),
          f"got error_class={res.get('error_class')!r} detail={res.get('detail')!r}")

    # A_missing: no heartbeat row at all, ping healthy.
    db = make_health_db(row=False)
    ns = load_readiness(build_env(db_path=db))
    res = call_ready_once(ns, heartbeat_advisory=True)
    check("A1b missing heartbeat row + healthy ping -> ALLOWED",
          bool(res.get("ok")),
          f"got error_class={res.get('error_class')!r} detail={res.get('detail')!r}")

    # A10-source: the readiness gate must expose the exact signal, not only a
    # human sentence, and must use the bounded poller rather than a one-shot.
    caller_src = ast.unparse(_top_level_node(MAIN_SRC, "_queue_bizlink_create_n_for_manager"))
    check("A10 readiness gate uses the bounded poller, not the one-shot pass",
          "manager_runtime_ready(" in caller_src
          and "_manager_runtime_ready_once(" not in caller_src,
          "expected manager_runtime_ready(...) at the /bizlink_create_n gate")
    check("A10b readiness failure records the exact signal + error_class",
          "readiness.get('signal')" in caller_src or 'readiness.get("signal")' in caller_src,
          "the structured signal is dropped; only the human sentence survives")


# ----------------------------------------------------------------------
# B. Invariant checks (GREEN before AND after the fix)
# ----------------------------------------------------------------------

def run_invariant_checks() -> None:
    print("\n--- B. invariants (must stay GREEN before and after the fix) ---")

    # A3: no OS process.
    db = make_health_db()
    ns = load_readiness(build_env(db_path=db, process_running=False))
    res = asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("A3 no process -> runtime_not_running/process",
          res.get("error_class") == "runtime_not_running" and res.get("signal") == "process",
          repr(res))

    # A4: phase starting / exited.
    for phase in ("starting", "exited"):
        db = make_health_db()
        ns = load_readiness(build_env(db_path=db, start_status={"phase": phase}))
        res = asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
        check(f"A4 phase={phase} -> runtime_not_ready/start_status",
              res.get("error_class") == "runtime_not_ready" and res.get("signal") == "start_status",
              repr(res))

    # A5: ping never answers -> command_timeout (bounded, not a hang).
    db = make_health_db()
    q = FakeQueue(ping_result=None)
    ns = load_readiness(build_env(db_path=db, queue=q))
    # heartbeat is fresh here, so this case is mode-independent and must be
    # green both before and after wave L2.
    res = call_ready_poll(ns, deadline_sec=3, poll_interval_sec=0.5,
                          ping_timeout_sec=1)
    check("A5 ping timeout after retries -> command_timeout/ping",
          res.get("error_class") == "command_timeout" and res.get("signal") == "ping",
          repr(res))

    # A6: connected=false.
    db = make_health_db()
    q = FakeQueue(ping_result={"connected": False, "authorized": False, "tg_user_id": 0,
                               "worker_key": "dwanders0",
                               "checked_at": datetime.utcnow().isoformat(),
                               "error": "not connected"})
    ns = load_readiness(build_env(db_path=db, queue=q))
    res = asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("A6 ping connected=false -> telegram_disconnected",
          res.get("error_class") == "telegram_disconnected", repr(res))

    # A7: authorized=false.
    db = make_health_db()
    q = FakeQueue(ping_result={"connected": True, "authorized": False, "tg_user_id": 0,
                               "worker_key": "dwanders0",
                               "checked_at": datetime.utcnow().isoformat()})
    ns = load_readiness(build_env(db_path=db, queue=q))
    res = asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("A7 ping authorized=false -> session_unauthorized",
          res.get("error_class") == "session_unauthorized", repr(res))

    # A8: tg_user_id mismatch vs registry.
    db = make_health_db()
    q = FakeQueue(ping_result={"connected": True, "authorized": True, "tg_user_id": 999999,
                               "worker_key": "dwanders0",
                               "checked_at": datetime.utcnow().isoformat()})
    ns = load_readiness(build_env(db_path=db, queue=q))
    res = asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("A8 tg_user_id mismatch -> telegram_identity_mismatch",
          res.get("error_class") == "telegram_identity_mismatch", repr(res))

    # A9: ping answered by a foreign worker_key.
    db = make_health_db()
    q = FakeQueue(ping_result={"connected": True, "authorized": True, "tg_user_id": 777001,
                               "worker_key": "someone_else",
                               "checked_at": datetime.utcnow().isoformat()})
    ns = load_readiness(build_env(db_path=db, queue=q))
    res = asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("A9 foreign worker_key -> runtime_not_ready/ping",
          res.get("error_class") == "runtime_not_ready" and res.get("signal") == "ping",
          repr(res))

    # A13: BLOCKED health is a hard fail in BOTH modes.
    for advisory in (False, True):
        db = make_health_db(health_status="blocked", age_sec=10)
        ns = load_readiness(build_env(db_path=db))
        kw = {"heartbeat_advisory": True} if advisory else {}
        res = call_ready_once(ns, **kw)
        ok = (not res.get("ok")) and res.get("error_class") != "SIGNATURE_MISSING"
        check_post_fix(f"A13 health_status=blocked is a hard fail (advisory={advisory})",
                       ok, repr(res), unavailable=advisory)

    # C: the replacement/onboarding contract stays STRICT -- the advisory
    # relaxation must be opt-in, i.e. default False, and the replacement call
    # sites must not pass it.
    once_node = _top_level_node(MAIN_SRC, "_manager_runtime_ready_once")
    defaults = {}
    kwonly = list(getattr(once_node.args, "kwonlyargs", []))
    kwdefs = list(getattr(once_node.args, "kw_defaults", []))
    for a, d in zip(kwonly, kwdefs):
        defaults[a.arg] = ast.unparse(d) if d is not None else None
    check("C1 heartbeat_advisory defaults to False (strict) when present",
          defaults.get("heartbeat_advisory", "False") == "False",
          f"kwonly defaults: {defaults}")
    repl_src = ast.unparse(_top_level_node(MAIN_SRC, "_repl4_validate_new_runtime"))
    check("C2 replacement path never opts into advisory mode",
          "heartbeat_advisory" not in repl_src,
          "replacement must keep the strict readiness contract")

    # B/Auth-Proxy: guard failures keep their exact class and are not reordered
    # away by the fix.
    db = make_health_db()
    ns = load_readiness(build_env(db_path=db, guard=(False, "proxy dead")))
    res = asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("B1 auth/proxy guard failure -> proxy_connect_error/auth_guard",
          res.get("error_class") == "proxy_connect_error" and res.get("signal") == "auth_guard",
          repr(res))

    # D: nothing is enqueued beyond the single runtime_ping on a failing path.
    db = make_health_db()
    q = FakeQueue(ping_result=None)
    ns = load_readiness(build_env(db_path=db, process_running=False, queue=q))
    asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("D1 a pre-ping failure enqueues nothing at all", len(q.puts) == 0,
          f"enqueued: {[p.get('command') for p in q.puts]}")

    # D: the ping that IS enqueued is a runtime_ping for this exact key.
    db = make_health_db()
    q = FakeQueue(ping_result={"connected": True, "authorized": True, "tg_user_id": 777001,
                               "worker_key": "dwanders0",
                               "checked_at": datetime.utcnow().isoformat()})
    ns = load_readiness(build_env(db_path=db, queue=q))
    asyncio.run(ns["_manager_runtime_ready_once"]("dwanders0"))
    check("D2 readiness enqueues exactly one runtime_ping for its own key",
          len(q.puts) == 1 and q.puts[0].get("command") == "runtime_ping"
          and q.puts[0].get("target_key") == "dwanders0",
          repr(q.puts))


# ----------------------------------------------------------------------
# C. Mutation controls -- each must be caught (go RED)
# ----------------------------------------------------------------------

def _m_red(label: str, fn, *, anchored_in_fix: bool = False) -> None:
    """fn() must raise or return False for the mutation to count as caught.

    `anchored_in_fix` marks a control whose textual anchor only exists once
    wave L2 has landed: before that it is PENDING (never a silent pass), after
    that a missing anchor is a hard failure -- an anchor that drifted out of
    sync with main.py must fail loudly, not masquerade as a caught mutation."""
    try:
        caught = not bool(fn())
    except MutationAnchorError as e:
        if anchored_in_fix and not fix_applied():
            print(f"[PEND] {label}  (anchor lands with wave L2)")
            PENDING.append(label)
            return
        print(f"[FAIL] {label}  mutation anchor drifted: {e}")
        FAILURES.append(label)
        return
    except Exception:
        caught = True
    if caught:
        print(f"[OK]   {label} (mutation caught)")
    else:
        print(f"[FAIL] {label}  mutation NOT caught")
        FAILURES.append(label)


def run_mutation_controls() -> None:
    print("\n--- C. mutation controls (each must be caught) ---")

    # M1: restore the 900 s threshold.
    def m1():
        ns = load_readiness(build_env(db_path=make_health_db(age_sec=1140)),
                            const_mutations={"_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC": "900"})
        interval = int(ns["TP_HG_CHECK_INTERVAL_SEC"])
        threshold = int(ns["_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC"])
        return threshold >= interval * 2
    _m_red("M1 threshold restored to 900 s", m1)

    # M3: TP_HG_STATUS_BLOCKED weakened to a non-blocking signal.
    def m3():
        ns = load_readiness(
            build_env(db_path=make_health_db(health_status="blocked", age_sec=10)),
            mutations={"_manager_health_heartbeat_ok":
                       ("if status == TP_HG_STATUS_BLOCKED:", "if False:")})
        # heartbeat is fresh (10 s), so BLOCKED is the only thing that can stop
        # this call -- mode-independent, works before and after wave L2.
        res = call_ready_once(ns)
        return not res.get("ok")  # still blocked => mutation caught
    _m_red("M3 BLOCKED weakened", m3)

    # M4: one-shot readiness restored at the /bizlink_create_n gate.
    def m4():
        caller_src = ast.unparse(
            _top_level_node(MAIN_SRC, "_queue_bizlink_create_n_for_manager"))
        mutated = _apply_subs(caller_src, ("manager_runtime_ready(",
                                           "_manager_runtime_ready_once("))
        return "manager_runtime_ready(" in mutated and "_manager_runtime_ready_once(" not in mutated
    _m_red("M4 one-shot readiness restored", m4, anchored_in_fix=True)

    # M2: advisory made the global default (breaks the strict replacement
    # contract). Caught by C1.
    def m2():
        once_node = _top_level_node(MAIN_SRC, "_manager_runtime_ready_once")
        src = _apply_subs(ast.unparse(once_node),
                          ("heartbeat_advisory: bool=False", "heartbeat_advisory: bool=True"))
        tree = ast.parse(src)
        fn = tree.body[0]
        for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
            if a.arg == "heartbeat_advisory":
                return ast.unparse(d) == "False"
        return False
    _m_red("M2 advisory default flipped to True", m2, anchored_in_fix=True)


def main() -> int:
    print("=" * 72)
    print("manager_runtime_readiness_selftest -- wave L2 (readiness)")
    print("=" * 72)
    run_defect_checks()
    run_invariant_checks()
    run_mutation_controls()
    print("\n" + "=" * 72)
    if PENDING:
        print(f"PENDING (cannot run until wave L2 lands): {len(PENDING)}")
        for p in PENDING:
            print(f"  ~ {p}")
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    if PENDING:
        print("RESULT: no failures, but wave L2 is not applied yet")
        return 2
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
