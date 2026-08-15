# -*- coding: utf-8 -*-
"""
P5 / F0 -- deterministic SQLite contention harness.

TEMP DATABASES ONLY. Never touches C:\\ALM_TPilot\\db\\data_tpilot.db or any
other project database. Every case creates its own file under a fresh
tempfile.mkdtemp() directory.

Cases:
  1. Deterministic Writer A (long HOLD, near-zero WAIT) vs Writer B
     (long WAIT, short HOLD) -- proves db_observability tells WAIT and HOLD
     apart, which the historical emergency tracer could not do.
  2. Event-loop stall: a background writer holds the write lock while (a) a
     sync sqlite3 BEGIN IMMEDIATE runs directly on the event-loop thread vs
     (b) the same blocking call offloaded to an executor thread. A heartbeat
     probe proves (a) stalls the loop and (b) does not.
  3. aiosqlite contention: two aiosqlite connections race for the same
     write lock; the loser's WAIT is recorded.

Usage:
    python tools\\sqlite_contention_harness.py --out-dir <report_dir>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db_observability as dbobs  # noqa: E402


def _fresh_temp_db(tag: str) -> str:
    d = tempfile.mkdtemp(prefix=f"tpilot_p5_{tag}_")
    path = os.path.join(d, "contention.db")
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA busy_timeout=30000;")
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER);")
    con.commit()
    con.close()
    return path


def _sync_conn(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA busy_timeout=30000;")
    return con


# --------------------------------------------------------------------------
# Case 1: deterministic Writer A (hold) vs Writer B (wait)
# --------------------------------------------------------------------------

def case1_writer_a_hold_writer_b_wait(events_sink: list) -> dict:
    db_path = _fresh_temp_db("case1")
    a_acquired = threading.Event()
    HOLD_S = 0.5
    results = {}

    def writer_a():
        con = _sync_conn(db_path)
        tx = dbobs.begin_immediate_sync(con, source="harness", function="writer_a",
                                         db_path=db_path, event_key="case1")
        a_acquired.set()
        time.sleep(HOLD_S)
        con.execute("INSERT INTO t(v) VALUES (1)")
        dbobs.commit_sync(tx, con)
        con.close()

    def writer_b():
        a_acquired.wait(timeout=5)
        time.sleep(0.02)  # ensure A is definitely holding before B attempts
        con = _sync_conn(db_path)
        t_start = time.monotonic()
        tx = dbobs.begin_immediate_sync(con, source="harness", function="writer_b",
                                         db_path=db_path, event_key="case1")
        t_acquired = time.monotonic()
        con.execute("INSERT INTO t(v) VALUES (2)")
        dbobs.commit_sync(tx, con)
        con.close()
        results["writer_b_observed_wait_s"] = t_acquired - t_start

    ta = threading.Thread(target=writer_a)
    tb = threading.Thread(target=writer_b)
    ta.start()
    tb.start()
    ta.join(timeout=10)
    tb.join(timeout=10)

    recs = _read_recent_trace_records(events_sink)
    a_commit = next((r for r in recs if r["context"].get("function") == "writer_a" and r["event"] == "DB_TX_COMMIT"), None)
    b_commit = next((r for r in recs if r["context"].get("function") == "writer_b" and r["event"] == "DB_TX_COMMIT"), None)

    ok = bool(a_commit and b_commit)
    verdict = "FAIL"
    if ok:
        a_wait, a_hold = a_commit["wait_ms"], a_commit["hold_ms"]
        b_wait, b_hold = b_commit["wait_ms"], b_commit["hold_ms"]
        verdict = "PASS" if (a_wait < 100 and a_hold > 300 and b_wait > 300 and b_hold < 200) else "FAIL"
        return {"case": "writer_a_hold_writer_b_wait", "verdict": verdict,
                "writer_a": {"wait_ms": a_wait, "hold_ms": a_hold},
                "writer_b": {"wait_ms": b_wait, "hold_ms": b_hold}}
    return {"case": "writer_a_hold_writer_b_wait", "verdict": "FAIL", "error": "missing trace records"}


def _read_recent_trace_records(sink: list) -> list:
    """Re-reads the JSONL trace file dbobs is currently pointed at and
    returns all records (small file per case -- harness uses a fresh trace
    file per invocation)."""
    path = dbobs.get_trace_path()
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    sink.extend(out[len(sink):])
    return out


# --------------------------------------------------------------------------
# Case 2: event-loop stall -- direct sync blocking vs executor offload
# --------------------------------------------------------------------------

def _background_lock_holder(db_path: str, hold_s: float, start_evt: threading.Event) -> threading.Thread:
    def run():
        con = _sync_conn(db_path)
        tx = dbobs.begin_immediate_sync(con, source="harness", function="lock_holder",
                                         db_path=db_path, event_key="case2")
        start_evt.set()
        time.sleep(hold_s)
        dbobs.commit_sync(tx, con)
        con.close()
    t = threading.Thread(target=run)
    t.start()
    return t


async def _heartbeat(interval_ms: float, duration_s: float) -> list:
    samples = []
    last = time.monotonic()
    deadline = last + duration_s
    while time.monotonic() < deadline:
        await asyncio.sleep(interval_ms / 1000.0)
        now = time.monotonic()
        gap_ms = (now - last) * 1000.0
        last = now
        samples.append(gap_ms - interval_ms)
    return samples


def _blocking_begin_commit(db_path: str, tag: str) -> None:
    con = _sync_conn(db_path)
    tx = dbobs.begin_immediate_sync(con, source="harness", function=tag,
                                     db_path=db_path, event_key="case2")
    con.execute("INSERT INTO t(v) VALUES (3)")
    dbobs.commit_sync(tx, con)
    con.close()


async def case2_direct_sync_blocks_loop() -> dict:
    db_path = _fresh_temp_db("case2a")
    start_evt = threading.Event()
    holder = _background_lock_holder(db_path, 0.4, start_evt)
    start_evt.wait(timeout=5)

    hb_task = asyncio.ensure_future(_heartbeat(10.0, 0.8))
    await asyncio.sleep(0.05)
    # DIRECT sync call on the event-loop thread -- this blocks the loop itself.
    _blocking_begin_commit(db_path, "direct_sync_on_loop")
    lags = await hb_task
    holder.join(timeout=5)
    max_lag = max(lags) if lags else 0.0
    return {"case": "direct_sync_blocks_loop", "max_lag_ms": round(max_lag, 2), "samples": len(lags)}


async def case2_executor_offload_keeps_loop_responsive() -> dict:
    db_path = _fresh_temp_db("case2b")
    start_evt = threading.Event()
    holder = _background_lock_holder(db_path, 0.4, start_evt)
    start_evt.wait(timeout=5)

    hb_task = asyncio.ensure_future(_heartbeat(10.0, 0.8))
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _blocking_begin_commit, db_path, "executor_offload")
    lags = await hb_task
    holder.join(timeout=5)
    max_lag = max(lags) if lags else 0.0
    return {"case": "executor_offload_keeps_loop_responsive", "max_lag_ms": round(max_lag, 2), "samples": len(lags)}


# --------------------------------------------------------------------------
# Case 3: aiosqlite contention
# --------------------------------------------------------------------------

async def case3_aiosqlite_contention() -> dict:
    import aiosqlite
    db_path = _fresh_temp_db("case3")

    async def writer(tag: str, hold_s: float, start_barrier: asyncio.Event, release_first: bool):
        async with aiosqlite.connect(db_path) as db:
            await db.execute("PRAGMA busy_timeout=30000;")
            if release_first:
                tx = await dbobs.begin_immediate_async(db, source="harness", function=tag,
                                                        db_path=db_path, event_key="case3")
                start_barrier.set()
                await asyncio.sleep(hold_s)
                await db.execute("INSERT INTO t(v) VALUES (4)")
                await dbobs.commit_async(tx, db)
            else:
                await start_barrier.wait()
                await asyncio.sleep(0.02)
                tx = await dbobs.begin_immediate_async(db, source="harness", function=tag,
                                                        db_path=db_path, event_key="case3")
                await db.execute("INSERT INTO t(v) VALUES (5)")
                await dbobs.commit_async(tx, db)

    barrier = asyncio.Event()
    await asyncio.gather(
        writer("aio_holder", 0.3, barrier, True),
        writer("aio_waiter", 0.0, barrier, False),
    )
    return {"case": "aiosqlite_contention", "db_path_basename": os.path.basename(db_path)}


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    contention_jsonl = os.path.join(args.out_dir, "contention_cases.jsonl")
    stall_report_md = os.path.join(args.out_dir, "event_loop_stall_report.md")

    dbobs.set_enabled(True)
    all_events: list = []
    summary = {}

    # Case 1 -- its own trace file so re-reads stay clean
    trace1 = os.path.join(tempfile.mkdtemp(prefix="tpilot_p5_trace1_"), "trace.jsonl")
    dbobs.set_trace_path(trace1)
    summary["case1"] = case1_writer_a_hold_writer_b_wait(all_events)

    # Case 2 -- fresh trace file, run both sub-cases sequentially (single event loop)
    trace2 = os.path.join(tempfile.mkdtemp(prefix="tpilot_p5_trace2_"), "trace.jsonl")
    dbobs.set_trace_path(trace2)

    async def _run_case2():
        r_direct = await case2_direct_sync_blocks_loop()
        r_exec = await case2_executor_offload_keeps_loop_responsive()
        return r_direct, r_exec

    r_direct, r_exec = asyncio.run(_run_case2())
    summary["case2_direct_sync"] = r_direct
    summary["case2_executor_offload"] = r_exec
    verdict2 = "PASS" if (r_direct["max_lag_ms"] > 100 and r_exec["max_lag_ms"] < 60
                           and r_direct["max_lag_ms"] > 3 * max(r_exec["max_lag_ms"], 1.0)) else "FAIL"
    summary["case2_verdict"] = verdict2

    # Case 3 -- fresh trace file
    trace3 = os.path.join(tempfile.mkdtemp(prefix="tpilot_p5_trace3_"), "trace.jsonl")
    dbobs.set_trace_path(trace3)
    summary["case3"] = asyncio.run(case3_aiosqlite_contention())
    case3_events = []
    _read_recent_trace_records(case3_events)
    waiter_rec = next((r for r in case3_events
                        if r["context"].get("function") == "aio_waiter" and r["event"] == "DB_TX_COMMIT"), None)
    holder_rec = next((r for r in case3_events
                        if r["context"].get("function") == "aio_holder" and r["event"] == "DB_TX_COMMIT"), None)
    if waiter_rec and holder_rec:
        summary["case3"]["aio_holder"] = {"wait_ms": holder_rec["wait_ms"], "hold_ms": holder_rec["hold_ms"]}
        summary["case3"]["aio_waiter"] = {"wait_ms": waiter_rec["wait_ms"], "hold_ms": waiter_rec["hold_ms"]}
        summary["case3_verdict"] = "PASS" if waiter_rec["wait_ms"] > 100 else "FAIL"
    else:
        summary["case3_verdict"] = "FAIL"

    # Combine all trace events from the three trace files into contention_cases.jsonl
    combined = []
    for tp in (trace1, trace2, trace3):
        if os.path.exists(tp):
            with open(tp, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        combined.append(line)
    with open(contention_jsonl, "w", encoding="utf-8") as f:
        f.write("\n".join(combined) + ("\n" if combined else ""))

    with open(stall_report_md, "w", encoding="utf-8") as f:
        f.write("# P5/F0 -- event-loop stall report\n\n")
        f.write("TEMP databases only. Deterministic comparison: direct sync sqlite3 call on\n")
        f.write("the event-loop thread vs the same blocking call offloaded to an executor.\n\n")
        f.write(f"- direct_sync max_lag_ms: {r_direct['max_lag_ms']}\n")
        f.write(f"- executor_offload max_lag_ms: {r_exec['max_lag_ms']}\n")
        f.write(f"- verdict: {verdict2}\n")

    print(json.dumps(summary, indent=2, default=str))
    print(f"written: {contention_jsonl}")
    print(f"written: {stall_report_md}")

    all_pass = (summary.get("case1", {}).get("verdict") == "PASS"
                and summary.get("case2_verdict") == "PASS"
                and summary.get("case3_verdict") == "PASS")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
