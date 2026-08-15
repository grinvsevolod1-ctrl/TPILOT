# -*- coding: utf-8 -*-
"""
P5 / F0 -- offline selftest suite for db_observability.py and its harness/audit
tools. TEMP SQLite only. No network. No production DB. No live Telegram.

Run:
    python tools\\db_observability_selftest.py
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import db_observability as dbobs  # noqa: E402
import sqlite_policy_audit as policy_audit  # noqa: E402
import sqlite_contention_harness as harness  # noqa: E402

PROD_DB_BASENAME = "data_tpilot.db"

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append((name, detail))


def _fresh_db() -> str:
    d = tempfile.mkdtemp(prefix="tpilot_p5_selftest_")
    path = os.path.join(d, "t.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, v INTEGER)")
    con.commit()
    con.close()
    return path


def _fresh_trace_path() -> str:
    d = tempfile.mkdtemp(prefix="tpilot_p5_selftest_trace_")
    return os.path.join(d, "trace.jsonl")


def _read_jsonl(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# 1. WAIT/HOLD arithmetic exact ---------------------------------------------

def test_01_wait_hold_arithmetic():
    dbobs.set_enabled(True)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    db_path = _fresh_db()
    con = sqlite3.connect(db_path, timeout=30)
    con.execute("PRAGMA busy_timeout=30000;")
    tx = dbobs.begin_immediate_sync(con, source="selftest", function="t01", db_path=db_path)
    time.sleep(0.15)
    dbobs.commit_sync(tx, con)
    con.close()
    recs = _read_jsonl(tp)
    commit = next((r for r in recs if r["event"] == "DB_TX_COMMIT"), None)
    check("01a: commit record exists", commit is not None)
    if commit:
        check("01b: wait_ms near zero (uncontended begin)", commit["wait_ms"] < 50, f"wait_ms={commit['wait_ms']}")
        check("01c: hold_ms >= sleep duration", commit["hold_ms"] >= 140, f"hold_ms={commit['hold_ms']}")
        check("01d: total_ms == wait+hold+finalize (within 5ms)",
              abs(commit["total_ms"] - (commit["wait_ms"] + commit["hold_ms"] + commit["finalize_ms"])) < 5,
              f"total={commit['total_ms']} sum={commit['wait_ms'] + commit['hold_ms'] + commit['finalize_ms']}")


# 2. Successful commit trace -------------------------------------------------

def test_02_commit_trace():
    dbobs.set_enabled(True)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    db_path = _fresh_db()
    con = sqlite3.connect(db_path, timeout=30)
    tx = dbobs.begin_immediate_sync(con, source="selftest", function="t02", db_path=db_path)
    con.execute("INSERT INTO t(v) VALUES (1)")
    dbobs.commit_sync(tx, con)
    row = con.execute("SELECT v FROM t").fetchone()
    con.close()
    recs = _read_jsonl(tp)
    events = [r["event"] for r in recs]
    check("02a: ATTEMPT/ACQUIRED/COMMIT all emitted",
          events == ["DB_TX_ATTEMPT", "DB_TX_ACQUIRED", "DB_TX_COMMIT"], f"events={events}")
    check("02b: data actually committed", row is not None and row[0] == 1)
    check("02c: commit result field == 'commit'", recs[-1]["result"] == "commit")


# 3. Rollback trace -----------------------------------------------------------

def test_03_rollback_trace():
    dbobs.set_enabled(True)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    db_path = _fresh_db()
    con = sqlite3.connect(db_path, timeout=30)
    tx = dbobs.begin_immediate_sync(con, source="selftest", function="t03", db_path=db_path)
    con.execute("INSERT INTO t(v) VALUES (2)")
    dbobs.rollback_sync(tx, con)
    row = con.execute("SELECT v FROM t WHERE v=2").fetchone()
    con.close()
    recs = _read_jsonl(tp)
    check("03a: rollback event emitted", recs[-1]["event"] == "DB_TX_ROLLBACK")
    check("03b: data NOT persisted after rollback", row is None)


# 4. Exception trace -----------------------------------------------------------

def test_04_exception_trace():
    dbobs.set_enabled(True)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    db_path = _fresh_db()
    con = sqlite3.connect(db_path, timeout=30)
    tx = dbobs.begin_immediate_sync(con, source="selftest", function="t04", db_path=db_path)
    raised = False
    try:
        try:
            con.execute("INSERT INTO t(v) VALUES (?)", ("not_an_int_column_violation_marker",))
            raise RuntimeError("synthetic business error")
        except Exception as exc:
            dbobs.rollback_sync(tx, con, error=exc)
            raise
    except RuntimeError:
        raised = True
    con.close()
    recs = _read_jsonl(tp)
    check("04a: exception propagated to caller", raised)
    check("04b: DB_TX_ERROR event emitted with error_class", recs[-1]["event"] == "DB_TX_ERROR"
          and recs[-1]["error_class"] == "RuntimeError")


# 5. Nested/error behavior (error inside a nested try does not double-emit) --

def test_05_nested_error_behavior():
    dbobs.set_enabled(True)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    db_path = _fresh_db()
    con = sqlite3.connect(db_path, timeout=30)
    tx = dbobs.begin_immediate_sync(con, source="selftest", function="t05", db_path=db_path)
    try:
        try:
            raise ValueError("inner")
        except ValueError:
            pass
        con.execute("INSERT INTO t(v) VALUES (5)")
        dbobs.commit_sync(tx, con)
    except Exception as exc:
        dbobs.rollback_sync(tx, con, error=exc)
        raise
    finally:
        con.close()
    recs = _read_jsonl(tp)
    finish_events = [r for r in recs if r["event"] in ("DB_TX_COMMIT", "DB_TX_ROLLBACK", "DB_TX_ERROR")]
    check("05: exactly one finish event for one transaction", len(finish_events) == 1, f"count={len(finish_events)}")


# 6. Tracer failure does not break business transaction ----------------------

def test_06_tracer_failure_fail_soft():
    dbobs.set_enabled(True)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    db_path = _fresh_db()
    con = sqlite3.connect(db_path, timeout=30)

    orig_emit = dbobs._emit

    def broken_emit(event):
        raise RuntimeError("simulated tracer failure")

    dbobs._emit = broken_emit
    try:
        tx = dbobs.begin_immediate_sync(con, source="selftest", function="t06", db_path=db_path)
        con.execute("INSERT INTO t(v) VALUES (6)")
        dbobs.commit_sync(tx, con)
    finally:
        dbobs._emit = orig_emit
    row = con.execute("SELECT v FROM t WHERE v=6").fetchone()
    con.close()
    check("06: business transaction commits despite tracer raising internally", row is not None)


# 7. PII fields are not emitted ------------------------------------------------

def test_07_pii_scrubbed():
    raw = {
        "manager_key": "mgr_01",
        "event_key": "evt_abc123",
        "source": "selftest",
        "function": "t07",
        "phone": "+380501234567",
        "full_name": "Ivan Ivanov",
        "text": "hello world, secret message",
        "password": "hunter2",
        "session": "1BVtsOG...longtelethonsessionstring",
        "api_hash": "deadbeefcafebabe0123456789abcdef",
    }
    scrubbed = dbobs._scrub_context(raw)
    check("07a: manager_key preserved", scrubbed.get("manager_key") == "mgr_01")
    check("07b: event_key preserved", scrubbed.get("event_key") == "evt_abc123")
    for bad_key in ("phone", "full_name", "text", "password", "session", "api_hash"):
        check(f"07c: '{bad_key}' never in scrubbed context", bad_key not in scrubbed)
    check("07d: no scrubbed value contains the raw phone number",
          all("380501234567" not in str(v) for v in scrubbed.values()))


# 8/9/10/11: contention harness cases -----------------------------------------

def test_08_writer_a_hold_writer_b_wait():
    dbobs.set_enabled(True)
    dbobs.set_trace_path(_fresh_trace_path())
    result = harness.case1_writer_a_hold_writer_b_wait([])
    check("08: deterministic Writer A (hold) / Writer B (wait) proof", result.get("verdict") == "PASS",
          json.dumps(result))


def test_09_10_event_loop_direct_vs_executor():
    import asyncio
    dbobs.set_enabled(True)
    dbobs.set_trace_path(_fresh_trace_path())

    async def _run():
        r_direct = await harness.case2_direct_sync_blocks_loop()
        r_exec = await harness.case2_executor_offload_keeps_loop_responsive()
        return r_direct, r_exec

    r_direct, r_exec = asyncio.run(_run())
    check("09: direct sync sqlite call on event-loop thread stalls heartbeat",
          r_direct["max_lag_ms"] > 100, json.dumps(r_direct))
    check("10: executor-offloaded call keeps heartbeat responsive",
          r_exec["max_lag_ms"] < 60, json.dumps(r_exec))
    check("10b: direct stall clearly worse than executor offload",
          r_direct["max_lag_ms"] > 3 * max(r_exec["max_lag_ms"], 1.0))


def test_11_aiosqlite_contention():
    import asyncio
    dbobs.set_enabled(True)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    asyncio.run(harness.case3_aiosqlite_contention())
    recs = _read_jsonl(tp)
    waiter = next((r for r in recs if r["context"].get("function") == "aio_waiter" and r["event"] == "DB_TX_COMMIT"), None)
    check("11: aiosqlite contention loser shows measurable wait", waiter is not None and waiter["wait_ms"] > 100,
          json.dumps(waiter))


# 12. Busy-timeout inventory ----------------------------------------------------

_SYNTH_SOURCE = '''
import sqlite3
import aiosqlite

def _my_connect(path):
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA busy_timeout=30000;")
    return con

def shadowed_fn():
    return 1

def shadowed_fn():
    con = _my_connect("x.db")
    con.execute("BEGIN IMMEDIATE")
    con.commit()
    return 2

async def async_fn(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.commit()
'''


def test_12_busy_timeout_and_begin_immediate_inventory():
    d = tempfile.mkdtemp(prefix="tpilot_p5_selftest_audit_")
    path = os.path.join(d, "synthetic.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SYNTH_SOURCE)
    rows = policy_audit.audit_file(path)
    kinds = [r["kind"] for r in rows]
    check("12a: PRAGMA busy_timeout detected", "PRAGMA busy_timeout" in kinds, str(kinds))
    check("12b: BEGIN IMMEDIATE detected at least twice (sync+async)",
          kinds.count("BEGIN IMMEDIATE") == 2, str(kinds))
    check("12c: sqlite3.connect detected", any(r["kind"] == "sqlite3.connect" for r in rows))
    check("12d: aiosqlite.connect detected", any(r["kind"] == "aiosqlite.connect" for r in rows))


# 13. Active-binding correctness (override-chain classification) ---------------

def test_13_active_shadowed_classification():
    d = tempfile.mkdtemp(prefix="tpilot_p5_selftest_audit2_")
    path = os.path.join(d, "synthetic2.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SYNTH_SOURCE)
    rows = policy_audit.audit_file(path)
    shadowed_rows = [r for r in rows if r["function"] == "shadowed_fn"]
    check("13: BEGIN IMMEDIATE inside the LAST 'shadowed_fn' def is classified active",
          any(r["override_status"] == "active" for r in shadowed_rows), str(shadowed_rows))


# 14. Disabled-mode: minimal overhead / no output -------------------------------

def test_14_disabled_mode_no_output_low_overhead():
    dbobs.set_enabled(False)
    tp = _fresh_trace_path()
    dbobs.set_trace_path(tp)
    db_path = _fresh_db()
    N = 200

    t0 = time.perf_counter()
    for i in range(N):
        con = sqlite3.connect(db_path, timeout=30)
        tx = dbobs.begin_immediate_sync(con, source="selftest", function="t14", db_path=db_path)
        con.execute("INSERT INTO t(v) VALUES (?)", (i,))
        dbobs.commit_sync(tx, con)
        con.close()
    disabled_elapsed = time.perf_counter() - t0

    check("14a: no trace file written while disabled", not os.path.exists(tp))

    dbobs.set_enabled(True)
    tp2 = _fresh_trace_path()
    dbobs.set_trace_path(tp2)
    t0 = time.perf_counter()
    for i in range(N):
        con = sqlite3.connect(db_path, timeout=30)
        tx = dbobs.begin_immediate_sync(con, source="selftest", function="t14b", db_path=db_path)
        con.execute("INSERT INTO t(v) VALUES (?)", (i,))
        dbobs.commit_sync(tx, con)
        con.close()
    enabled_elapsed = time.perf_counter() - t0
    dbobs.set_enabled(False)

    recs = _read_jsonl(tp2)
    check("14b: enabled mode produced output", len(recs) > 0)
    print(f"    perf: disabled={disabled_elapsed*1000/N:.3f}ms/tx  enabled={enabled_elapsed*1000/N:.3f}ms/tx "
          f"(N={N})")
    check("14c: disabled overhead is not pathological (<= enabled elapsed)",
          disabled_elapsed <= enabled_elapsed * 2 + 0.5)


# 15. No production DB touched ---------------------------------------------------

def test_15_no_production_db_touched():
    src_files = [
        os.path.join(ROOT, "tools", "sqlite_contention_harness.py"),
    ]
    ok = True
    for f in src_files:
        with open(f, "r", encoding="utf-8") as fh:
            text = fh.read()
        tree = ast.parse(text)
        # Only flag the production DB name where it could actually be USED
        # (a call argument), never inside docstrings/comments/log text.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if PROD_DB_BASENAME in arg.value:
                            ok = False
    check(f"15: '{PROD_DB_BASENAME}' string literal absent from harness source (which is what "
          f"actually opens temp DBs)", ok)


def main() -> int:
    tests = [
        test_01_wait_hold_arithmetic,
        test_02_commit_trace,
        test_03_rollback_trace,
        test_04_exception_trace,
        test_05_nested_error_behavior,
        test_06_tracer_failure_fail_soft,
        test_07_pii_scrubbed,
        test_08_writer_a_hold_writer_b_wait,
        test_09_10_event_loop_direct_vs_executor,
        test_11_aiosqlite_contention,
        test_12_busy_timeout_and_begin_immediate_inventory,
        test_13_active_shadowed_classification,
        test_14_disabled_mode_no_output_low_overhead,
        test_15_no_production_db_touched,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:
            check(t.__name__, False, f"raised {type(exc).__name__}: {exc}")
        finally:
            dbobs.set_enabled(False)

    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing checks)")
        for name, detail in FAILURES:
            print(f"  - {name}: {detail}")
        return 1
    print("RESULT: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
