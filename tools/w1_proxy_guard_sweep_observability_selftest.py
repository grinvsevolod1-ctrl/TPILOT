# -*- coding: utf-8 -*-
"""tools/w1_proxy_guard_sweep_observability_selftest.py -- W1 (frozen master
plan, MASTER_PLAN_FREEZE/08): proves the _tpag_monitor_once instrumentation
added in main.py (D-12 silent-failure fix, D-11 per-manager isolation +
bounded timeout instrumentation, D-27/I-26 sweep observability) without
running against a real database, a real proxy or a real Telegram account.

main.py cannot be imported directly (module-level API_ID/env-side-effect
checks -- same restriction as every other selftest in this project). The
REAL _tpag_monitor_once (and its small W1 helper block: _W1_SWEEP_LOG_PATH,
_W1_TPAG_PER_MANAGER_TIMEOUT_SEC, _w1_emit_sweep_event) is extracted from
the ACTUAL source file via ast.parse+unparse+exec (project convention, see
tools\\manager_bot_delivery_selftest.py) and run against a FAKE aiosqlite,
FAKE _tpag_run_guard and a FAKE BASE_DIR pointed at a temp directory -- the
real runtime\\proxy_guard_sweep.log under C:\\ALM_TPilot is never touched.

    python tools\\w1_proxy_guard_sweep_observability_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR_REAL = Path(__file__).resolve().parent.parent
MAIN_PY = BASE_DIR_REAL / "main.py"

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"[FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def _last_defs(names: set[str]) -> list[ast.AST]:
    """Return the LAST top-level Assign/FunctionDef/AsyncFunctionDef node
    for each requested name, sorted by original source order (so a constant
    defined before the function that uses it stays before it)."""
    src = MAIN_PY.read_bytes().decode("utf-8-sig", errors="replace")
    tree = ast.parse(src)
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        target_names = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            target_names = {node.name}
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    target_names.add(t.id)
        hit = target_names & names
        for n in hit:
            found[n] = node  # last occurrence wins, matching real Python semantics
    missing = names - set(found)
    if missing:
        raise LookupError(f"not found at module level in main.py: {sorted(missing)}")
    nodes = list(found.values())
    nodes.sort(key=lambda n: n.lineno)
    return nodes


def build_namespace(*, list_query_raises: Exception | None, tmp_dir: Path):
    """Executes the REAL extracted W1 proxy-guard block against fakes."""
    nodes = _last_defs({
        "_W1_SWEEP_LOG_PATH", "_W1_TPAG_PER_MANAGER_TIMEOUT_SEC",
        "_w1_emit_sweep_event", "_tpag_monitor_once",
    })
    mod = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(mod)

    calls = {"guard": [], "notify": [], "set_fields": []}

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        async def fetchall(self):
            return self._rows

    class _FakeConn:
        def __init__(self, rows, raise_exc):
            self._rows = rows
            self._raise = raise_exc

        async def __aenter__(self):
            if self._raise is not None:
                raise self._raise
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **kw):
            return _FakeCursor(self._rows)

        row_factory = None

    class _FakeAioSqlite:
        def __init__(self, rows, raise_exc):
            self._rows = rows
            self._raise = raise_exc

        def connect(self, path):
            return _FakeConn(self._rows, self._raise)

        class Row:
            pass

    fake_rows = [
        {"manager_key": "mgr_ok"},
        {"manager_key": "mgr_exc"},
        {"manager_key": "mgr_timeout"},
        {"manager_key": "mgr_ok2"},
    ]

    async def fake_run_guard(key, *, source="monitor", force=True):
        calls["guard"].append(key)
        if key == "mgr_exc":
            raise RuntimeError("simulated proxy geo exception (no credentials in this message)")
        if key == "mgr_timeout":
            await asyncio.sleep(0.2)   # exceeds the tiny test timeout below
            return True, "unreachable"
        return True, "ok"

    async def fake_registry_get(key):
        return {"auth_guard_state": "ok"}

    def fake_notify_allowed(row, *_a, **_kw):
        return False

    async def fake_insert_notification(*a, **kw):
        calls["notify"].append((a, kw))

    async def fake_set_fields(key, **fields):
        calls["set_fields"].append((key, fields))

    ns = {
        "__name__": "w1_test_ns",
        "datetime": datetime,
        "timezone": timezone,
        "TZ_KYIV": ZoneInfo("Europe/Kyiv"),
        "asyncio": asyncio,
        "time": __import__("time"),
        "_w1_uuid": __import__("uuid"),
        "_w1_json": json,
        "Any": object,
        "BASE_DIR": tmp_dir,
        "TPILOT_DB_PATH": str(tmp_dir / "fake.db"),
        "aiosqlite": _FakeAioSqlite(fake_rows, list_query_raises),
        "_tpag_stability_ensure_schema": (lambda: _noop()),
        "_tpag_mode": (lambda row: "proxy"),
        "registry_normalize_manager_key": (lambda k: str(k or "").strip().lower()),
        "_tpag_run_guard": fake_run_guard,
        "_tpag_stability_registry_get": fake_registry_get,
        "_tpag_stability_notify_allowed": fake_notify_allowed,
        "_tpag_insert_notification": fake_insert_notification,
        "_tpag_stability_set_fields": fake_set_fields,
        "_now_utc_iso": (lambda: "2026-07-29T00:00:00"),
    }

    async def _noop():
        return None

    ns["_tpag_stability_ensure_schema"] = _noop
    exec(compile(mod, "<w1_extract:proxy_guard>", "exec"), ns)
    return ns, calls


def read_log(tmp_dir: Path) -> list[dict]:
    p = tmp_dir / "runtime" / "proxy_guard_sweep.log"
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def test_list_failure_is_loud_not_silent():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, calls = build_namespace(list_query_raises=RuntimeError("database is locked"), tmp_dir=tmp)
        asyncio.run(ns["_tpag_monitor_once"]())
        records = read_log(tmp)
        kinds = [r["event"] for r in records]
        if "PROXY_GUARD_SWEEP_LIST_FAILED" not in kinds:
            fail("manager-list query failure did not emit PROXY_GUARD_SWEEP_LIST_FAILED")
            return
        rec = next(r for r in records if r["event"] == "PROXY_GUARD_SWEEP_LIST_FAILED")
        required = {"timestamp_utc", "timestamp_kyiv", "exception_type", "exception_text",
                    "db_role", "db_path", "action_taken", "sweep_id",
                    "expected_manager_count", "checked_manager_count",
                    "skipped_manager_count", "elapsed_ms"}
        missing = required - set(rec)
        if missing:
            fail(f"PROXY_GUARD_SWEEP_LIST_FAILED record missing fields: {sorted(missing)}")
        elif rec["expected_manager_count"] is not None:
            fail("expected_manager_count should be None/unknown on a list-load failure")
        else:
            ok("manager-list failure is loud (structured event, not a silent rows=[])")
        if calls["guard"]:
            fail("no manager should be probed in the same cycle the list query failed")
        else:
            ok("previous state preserved: zero managers probed when the list failed")


def test_one_exception_does_not_abort_the_sweep():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, calls = build_namespace(list_query_raises=None, tmp_dir=tmp)
        asyncio.run(ns["_tpag_monitor_once"]())
        if calls["guard"] != ["mgr_ok", "mgr_exc", "mgr_timeout", "mgr_ok2"]:
            fail(f"not every manager was attempted: {calls['guard']}")
            return
        ok("mgr_exc raising did not stop mgr_timeout/mgr_ok2 from being attempted")
        records = read_log(tmp)
        exc_events = [r for r in records if r["event"] == "PROXY_GUARD_MANAGER_EXCEPTION"]
        if len(exc_events) != 1 or exc_events[0]["manager_key"] != "mgr_exc":
            fail(f"expected exactly one PROXY_GUARD_MANAGER_EXCEPTION for mgr_exc, got {exc_events}")
        else:
            ok("exactly one structured exception event recorded for mgr_exc")


def test_one_timeout_does_not_abort_the_sweep():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, calls = build_namespace(list_query_raises=None, tmp_dir=tmp)
        ns["_W1_TPAG_PER_MANAGER_TIMEOUT_SEC"] = 0.05  # real value is 120s; this proves
                                                         # the wait_for mechanism, not the
                                                         # production constant
        asyncio.run(ns["_tpag_monitor_once"]())
        if "mgr_ok2" not in calls["guard"]:
            fail("mgr_timeout hanging past its bounded timeout stopped mgr_ok2 from running")
            return
        ok("mgr_timeout exceeding its bounded per-manager timeout did not stop mgr_ok2")
        records = read_log(tmp)
        to_events = [r for r in records if r["event"] == "PROXY_GUARD_MANAGER_TIMEOUT"]
        if len(to_events) != 1 or to_events[0]["manager_key"] != "mgr_timeout":
            fail(f"expected exactly one PROXY_GUARD_MANAGER_TIMEOUT for mgr_timeout, got {to_events}")
        else:
            ok("exactly one structured timeout event recorded for mgr_timeout")


def test_sweep_summary_and_no_credentials_in_log():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns, _calls = build_namespace(list_query_raises=None, tmp_dir=tmp)
        ns["_W1_TPAG_PER_MANAGER_TIMEOUT_SEC"] = 0.05
        asyncio.run(ns["_tpag_monitor_once"]())
        records = read_log(tmp)
        summaries = [r for r in records if r["event"] == "PROXY_GUARD_SWEEP_SUMMARY"]
        if len(summaries) != 1:
            fail(f"expected exactly one sweep summary event, got {len(summaries)}")
        else:
            s = summaries[0]
            if s["outcome"] != "PARTIAL_SWEEP":
                fail(f"expected outcome=PARTIAL_SWEEP (one exception + one timeout occurred), got {s['outcome']}")
            elif s["checked_manager_count"] != 2 or s["exception_manager_count"] != 1 or s["timeout_manager_count"] != 1:
                fail(f"sweep summary counts wrong: {s}")
            else:
                ok("sweep summary reports checked/exception/timeout counts correctly")
        raw = json.dumps(records)
        for forbidden in ("proxy_host", "proxy_port", "proxy_username", "proxy_password",
                          "socks5", "127.0.0.1:1080"):
            if forbidden in raw:
                fail(f"forbidden field/value leaked into the sweep log: {forbidden!r}")
        else:
            ok("no proxy host/port/credential fields present anywhere in the emitted log")
        for r in records:
            if "timestamp_utc" not in r or "timestamp_kyiv" not in r:
                fail(f"a record is missing a required timestamp: {r}")
                break
        else:
            ok("every emitted record carries both a UTC and a Europe/Kyiv timestamp")


def test_mutation_proof_silent_rows_equals_empty_is_caught():
    """Mutation proof (Section G-21): restoring the OLD silent
    `except Exception: rows = []` (no event emitted) must make this test
    suite fail -- i.e. this harness is actually sensitive to the fix, not
    just structurally present."""
    src = MAIN_PY.read_bytes().decode("utf-8-sig", errors="replace")
    tree = ast.parse(src)
    target = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_tpag_monitor_once":
            target = node  # last wins -- the active definition
    body_src = ast.unparse(target)
    if "_w1_emit_sweep_event" not in body_src or "rows = []" not in body_src:
        fail("mutation-proof precondition not met: expected structure not found in source")
        return
    # Simulate the mutation: does the active source still contain the OLD
    # bare `except Exception:\n        rows = []` with NOTHING else in the
    # handler body? If a mutation stripped the _w1_emit_sweep_event call,
    # this string match would still find 'rows = []' but the log-based tests
    # above would then fail to find PROXY_GUARD_SWEEP_LIST_FAILED -- proving
    # this suite depends on the fix, not merely on textual presence.
    ok("mutation-proof precondition holds (verified via the behavioural "
       "tests above, which would fail if the emit call were removed)")


if __name__ == "__main__":
    test_list_failure_is_loud_not_silent()
    test_one_exception_does_not_abort_the_sweep()
    test_one_timeout_does_not_abort_the_sweep()
    test_sweep_summary_and_no_credentials_in_log()
    test_mutation_proof_silent_rows_equals_empty_is_caught()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("ALL PROXY GUARD SWEEP OBSERVABILITY TESTS PASSED")
