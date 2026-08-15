# -*- coding: utf-8 -*-
"""tools/bizlink_readiness_integration_selftest.py -- offline integration
selftest for the RUNTIME READINESS + BUSINESS LINK RECOVERY RELIABILITY
release candidate.

Where the two unit selftests pin down one function each, this one exercises the
controller seam that the production incident actually crossed:

    /bizlink_create_n  ->  _queue_bizlink_create_n_for_manager
                       ->  readiness gate
                       ->  manager_queue_put('bizlink_create_n')
                       ->  manager_command_loop gen4

`panel_commands` id 2173 (`/bizlink_create_n dwanders0 15 2026-08-06 force`)
died at the readiness gate: taken_at 22:59:18 -> finished_at 22:59:19, one
second, with the text «Рантайм менеджера не запущен или ещё не готов». Nothing
reached `manager_commands`, so no runtime_ping and no bizlink_create_n row
exist for it, and the tg_limit recovery logic was never involved at all.

What is asserted here:
  * a live runtime whose only problem is a stale background heartbeat DOES get
    its command enqueued (the incident, reproduced end to end);
  * a genuinely dead runtime enqueues NOTHING (no enqueue-into-the-void);
  * the failure text distinguishes "not running" from "not ready";
  * the structured signal / error_class / detail survive to the log instead of
    being collapsed into one human sentence;
  * that log leaks no link_url, message_text, session path or proxy string.

No Telegram. No production DB. Throwaway SQLite under %TEMP%.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()

# Failure details quote the project's Russian admin-facing strings; the default
# Windows console codepage cannot encode them and would abort the run at the
# first FAIL, hiding every later check.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES: list = []
PENDING: list = []

SECRET_URL = "https://t.me/m/SUPERSECRETSLUG"
SECRET_SESSION = r"C:\ALM_TPilot\sessions\dwanders0.session"
SECRET_PROXY = "proxy.example.com:1080:login123:passw0rd"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def check_post_fix(label: str, condition: bool, detail: str = "",
                   *, unavailable: bool = False) -> None:
    if unavailable and not fix_applied():
        print(f"[PEND] {label}  (waiting for wave L2)")
        PENDING.append(label)
        return
    check(label, condition, detail)


_PARSE_CACHE: dict = {}


def _cached_parse(src: str):
    cached = _PARSE_CACHE.get(id(src))
    if cached is not None and cached[0] is src:
        return cached[1]
    tree = ast.parse(src)
    _PARSE_CACHE[id(src)] = (src, tree)
    return tree


def _top_level_node(src: str, name: str):
    tree = _cached_parse(src)
    nodes = [n for n in tree.body if getattr(n, "name", None) == name]
    if not nodes:
        raise AssertionError(f"{name} not found as a top-level def")
    return nodes[-1]


def _top_level_assign_src(src: str, name: str) -> str:
    tree = _cached_parse(src)
    found = [n for n in tree.body if isinstance(n, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]
    if not found:
        raise AssertionError(f"{name} not found as a top-level assignment")
    return ast.unparse(found[-1])


def fix_applied() -> bool:
    node = _top_level_node(MAIN_SRC, "_manager_runtime_ready_once")
    return any(a.arg == "heartbeat_advisory"
               for a in getattr(node.args, "kwonlyargs", []))


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

class FakeQueue:
    """storage.manager_queue_put / manager_queue_get stand-in shared by the
    readiness ping AND the bizlink_create_n enqueue, so a test can see exactly
    what -- and in what order -- reached manager_commands."""

    def __init__(self, *, ping_result: Optional[dict] = None,
                 batch_result_text: str = "OK", batch_ok: int = 1):
        self.ping_result = ping_result
        self.batch_result_text = batch_result_text
        self.batch_ok = batch_ok
        self.puts: List[dict] = []
        self._n = 0

    async def manager_queue_put(self, **kw):
        self._n += 1
        self.puts.append(dict(kw))
        return f"{kw.get('command', 'cmd')}#{self._n}"

    async def manager_queue_get(self, nonce, db_path=None):
        import json as _j
        if str(nonce).startswith("runtime_ping"):
            if self.ping_result is None:
                return {"status": "pending", "result_json": ""}
            return {"status": "done", "result_json": _j.dumps(self.ping_result)}
        return {"status": "done" if self.batch_ok else "error",
                "result_ok": self.batch_ok,
                "result_text": self.batch_result_text,
                "result_json": "{}"}

    @property
    def commands(self) -> List[str]:
        return [str(p.get("command") or "") for p in self.puts]


def make_db(*, health_age_sec: Optional[int] = 60,
            health_status: str = "ok", key: str = "dwanders0") -> str:
    fd, path = tempfile.mkstemp(prefix="tpilot_integ_", suffix=".db",
                                dir=tempfile.gettempdir())
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE manager_telegram_health("
                    "manager_key TEXT PRIMARY KEY, health_status TEXT, last_check_at TEXT)")
        last = "" if health_age_sec is None else (
            datetime.utcnow() - timedelta(seconds=int(health_age_sec))
        ).replace(microsecond=0).isoformat()
        con.execute("INSERT INTO manager_telegram_health VALUES (?,?,?)",
                    (key, health_status, last))
        con.commit()
    finally:
        con.close()
    return path


HEALTHY_PING = {
    "ok": True, "connected": True, "authorized": True, "tg_user_id": 777001,
    "worker_key": "dwanders0",
}

GATE_FNS = ("_now_utc_iso", "_future_iso", "_bizlink_safe_error_snippet",
            "_bizlink_human_error_reason", "_bizlink_delete_safe_sanitize",
            "_bizlink_readiness_safe_log",
            "_manager_health_heartbeat_ok", "_manager_runtime_ready_once",
            "manager_runtime_ready", "_queue_bizlink_create_n_for_manager")
GATE_CONSTS = ("TP_HG_CHECK_INTERVAL_SEC", "_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC",
               "_BIZLINK_ERROR_TOKEN_RE")


def build_gate(*, db_path: str, queue: FakeQueue, process_running: bool = True,
               start_status: Optional[dict] = None, count: int = 2) -> dict:
    """Extract + exec the whole controller gate with fake collaborators."""
    import re as _re
    import time as _time
    import aiosqlite as _aiosqlite

    mod = types.ModuleType("storage")
    mod.manager_queue_put = queue.manager_queue_put        # type: ignore[attr-defined]
    mod.manager_queue_get = queue.manager_queue_get        # type: ignore[attr-defined]
    sys.modules["storage"] = mod

    if start_status is None:
        start_status = {"phase": "running", "pid": 4628,
                        "updated_at": "2026-08-04T16:00:24+00:00"}

    async def _manager_process_running(k):
        return bool(process_running)

    def _manager_recovery_read_start_status(k):
        return dict(start_status or {})

    async def manager_get(k):
        return {"manager_key": "dwanders0", "tg_user_id": 777001}

    async def _tpag_run_guard(k, *, source="manual", force=False, min_consecutive_ok=1):
        return True, ""

    def registry_normalize_manager_key(raw):
        return str(raw or "").strip().lower()

    def _clamp(req, dbp=None):
        return int(req), 15

    def _links_for(mk, td, dbp=None):
        return []

    def _tmpl_list(dbp=None):
        # message_text deliberately carries a secret-looking payload so the
        # sanitization assertions have something real to catch.
        return [{"slot_no": i, "message_text": f"text {SECRET_URL} {i}"}
                for i in range(1, 16)]

    return {
        "aiosqlite": _aiosqlite, "asyncio": asyncio, "time": _time, "re": _re,
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple,
        "TPILOT_DB_PATH": db_path,
        "TP_HG_STATUS_BLOCKED": "blocked",
        "_manager_process_running": _manager_process_running,
        "_manager_recovery_read_start_status": _manager_recovery_read_start_status,
        "manager_get": manager_get,
        "_tpag_run_guard": _tpag_run_guard,
        "registry_normalize_manager_key": registry_normalize_manager_key,
        "_m213d2_clamp_count": _clamp,
        "_bsl_links_mgr_date_m213c": _links_for,
        "_bsl_tmpl_list_m213c": _tmpl_list,
        "_m213c_bsl_delay": 1,
    }


class MutationAnchorError(AssertionError):
    """Anchor missing or ambiguous -- never mistakable for a caught mutation."""


def _apply_subs(src: str, subs) -> str:
    if subs and isinstance(subs[0], str):
        subs = [subs]
    for old, new in subs:
        count = src.count(old)
        if count == 0:
            raise MutationAnchorError(f"mutation anchor not found: {old!r}")
        if count > 1:
            raise MutationAnchorError(
                f"mutation anchor is not unique ({count} occurrences): {old!r}")
        src = src.replace(old, new, 1)
    return src


def load_gate(env: dict, mutations: Optional[dict] = None) -> dict:
    parts = [_top_level_assign_src(MAIN_SRC, c) for c in GATE_CONSTS]
    for n in GATE_FNS:
        src = ast.unparse(_top_level_node(MAIN_SRC, n))
        if mutations and n in mutations:
            src = _apply_subs(src, mutations[n])
        parts.append(src)
    ns = dict(env)
    exec(compile("\n\n".join(parts), "<main.py:gate>", "exec"), ns)
    return ns


def run_gate(ns, *, count=2, target_date="2026-08-06"):
    return asyncio.run(ns["_queue_bizlink_create_n_for_manager"](
        "dwanders0", target_date, count, user_id=0, timeout_sec=8))


# ----------------------------------------------------------------------

def run_checks() -> None:
    print("\n--- integration: readiness gate -> enqueue ---")

    # The incident, reproduced: live runtime, stale background heartbeat.
    q = FakeQueue(ping_result=dict(HEALTHY_PING,
                                   checked_at=datetime.utcnow().isoformat()))
    ns = load_gate(build_gate(db_path=make_db(health_age_sec=1140), queue=q))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok, text = run_gate(ns)
    check_post_fix("N1 stale heartbeat + live runtime -> bizlink_create_n IS enqueued",
                   ok and "bizlink_create_n" in q.commands,
                   f"ok={ok} commands={q.commands} text={text[:120]!r}",
                   unavailable=not fix_applied())

    # A genuinely dead runtime must enqueue nothing at all.
    q = FakeQueue(ping_result=None)
    ns = load_gate(build_gate(db_path=make_db(), queue=q, process_running=False))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok, text = run_gate(ns)
    log = buf.getvalue()
    check("N2 dead runtime enqueues nothing (no enqueue-into-the-void)",
          (not ok) and "bizlink_create_n" not in q.commands,
          f"ok={ok} commands={q.commands}")

    # The two runtime failure classes must not share one sentence.
    ns2 = load_gate(build_gate(db_path=make_db(), queue=FakeQueue()))
    human = ns2["_bizlink_human_error_reason"]
    check("N3 runtime_not_running and runtime_not_ready read differently",
          human("runtime_not_running", "") != human("runtime_not_ready", ""),
          f"both say: {human('runtime_not_running', '')!r}")

    # The structured signal must survive to the log.
    check_post_fix("N4 the readiness failure logs signal + error_class",
                   ("signal=" in log and "error_class=" in log),
                   f"log={log[:300]!r}",
                   unavailable=not fix_applied())

    # ...and that log must not carry secrets. reason_class=session_unauthorized
    # is the path that propagates arbitrary upstream text verbatim into
    # `detail` (see _manager_runtime_ready_once signal #2), so this is a real
    # leak channel, not a scenario where detail happens to be a fixed string.
    q = FakeQueue(ping_result=None)
    ns = load_gate(build_gate(db_path=make_db(), queue=q,
                              start_status={"phase": "exited",
                                            "reason_class": "session_unauthorized",
                                            "error": f"died: {SECRET_URL} "
                                                     f"{SECRET_SESSION} {SECRET_PROXY}"}))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok, text = run_gate(ns)
    leak = buf.getvalue() + text
    check("N5 no link_url / session path / proxy string is ever logged",
          SECRET_URL not in leak and ".session" not in leak
          and "passw0rd" not in leak,
          f"leaked in: {leak[:300]!r}")

    # A ping that never answers must fail bounded, not hang, and still enqueue
    # no batch command.
    q = FakeQueue(ping_result=None)
    ns = load_gate(build_gate(db_path=make_db(), queue=q))
    started = datetime.utcnow()
    ok, text = run_gate(ns)
    elapsed = (datetime.utcnow() - started).total_seconds()
    check("N6 an unanswered ping fails bounded and enqueues no batch command",
          (not ok) and "bizlink_create_n" not in q.commands and elapsed < 120,
          f"ok={ok} elapsed={elapsed:.1f}s commands={q.commands}")

    # Ordering: whatever else happens, the ping is enqueued before the batch.
    q = FakeQueue(ping_result=dict(HEALTHY_PING,
                                   checked_at=datetime.utcnow().isoformat()))
    ns = load_gate(build_gate(db_path=make_db(), queue=q))
    with contextlib.redirect_stdout(io.StringIO()):
        ok, text = run_gate(ns)
    cmds = q.commands
    check("N7 readiness ping precedes the batch enqueue",
          "bizlink_create_n" in cmds
          and cmds.index("runtime_ping") < cmds.index("bizlink_create_n"),
          f"commands={cmds} ok={ok}")


def _m_red(label: str, fn) -> None:
    try:
        caught = not bool(fn())
    except MutationAnchorError as e:
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
    print("\n--- mutation controls ---")

    # M5: the readiness gate stops gating -- enqueue proceeds regardless.
    def m5():
        q = FakeQueue(ping_result=None)
        ns = load_gate(build_gate(db_path=make_db(), queue=q, process_running=False),
                       mutations={"_queue_bizlink_create_n_for_manager":
                                  ("if not readiness.get('ok'):", "if False:")})
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                run_gate(ns)
            except Exception:
                pass
        return "bizlink_create_n" not in q.commands
    _m_red("M5 enqueue no longer gated on readiness", m5)

    # M6: the diagnostic log stops sanitizing its free-text detail.
    def m6():
        q = FakeQueue(ping_result=None)
        ns = load_gate(
            build_gate(db_path=make_db(), queue=q,
                       start_status={"phase": "exited",
                                     "reason_class": "session_unauthorized",
                                     "error": f"died: {SECRET_URL} {SECRET_SESSION} {SECRET_PROXY}"}),
            mutations={"_bizlink_readiness_safe_log":
                       ("safe_detail = _sanitize(detail) if callable(_sanitize) else '[unavailable]'",
                        "safe_detail = detail")})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok, text = run_gate(ns)
        leak = buf.getvalue() + text
        return SECRET_URL not in leak and ".session" not in leak and "passw0rd" not in leak
    _m_red("M6 diagnostic sanitizer removed", m6)


def main() -> int:
    print("=" * 72)
    print("bizlink_readiness_integration_selftest")
    print("=" * 72)
    run_checks()
    run_mutation_controls()
    print("\n" + "=" * 72)
    if PENDING:
        print(f"PENDING: {len(PENDING)}")
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
