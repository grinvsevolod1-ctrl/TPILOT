# -*- coding: utf-8 -*-
"""
P5 / F0 -- SQLite contention observability.

Purpose: for instrumented write transactions, independently measure

    WAIT_MS      = T1 - T0   (time spent attempting to acquire the write lock)
    HOLD_MS      = T2 - T1   (time the transaction was held open, business work)
    FINALIZE_MS  = T3 - T2   (time spent in commit()/rollback() itself)
    TOTAL_MS     = T3 - T0

This module NEVER decides business outcomes. It only observes. Any internal
failure in this module must never break the caller's transaction (fail-soft).

Enable with env var TPILOT_DB_OBS_ENABLED=1 (default: disabled).
Trace sink path: env var TPILOT_DB_OBS_TRACE_PATH (default:
<project_root>/db_observability_traces/trace.jsonl).

Never log: phone, full_name, message/text, proxy password, session data,
tokens, .env values, raw full DB filesystem paths. Only a fixed allow-list of
fields is ever accepted into a trace record; unknown/free-text kwargs are
rejected before they can reach the sink.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Optional

try:
    import asyncio
except Exception:  # pragma: no cover - stdlib always present
    asyncio = None  # type: ignore


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _env_flag(name: str, default: str = "0") -> bool:
    return str(os.getenv(name, default) or default).strip().lower() not in ("", "0", "false", "no", "off")


_ENABLED = _env_flag("TPILOT_DB_OBS_ENABLED", "0")

_DEFAULT_TRACE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db_observability_traces")
_TRACE_PATH = os.getenv("TPILOT_DB_OBS_TRACE_PATH") or os.path.join(_DEFAULT_TRACE_DIR, "trace.jsonl")

_WRITE_LOCK = threading.Lock()
_OPEN_FILE = None  # cached (path, file_object) to avoid an open()/close() per event
_OPEN_FILE_PATH = None

_CENTRAL_DB_BASENAMES = {"data_tpilot.db", "data.db"}


def is_enabled() -> bool:
    return _ENABLED


def set_enabled(value: bool) -> None:
    """Explicit override for tests / local diagnostics. Never call from
    production request paths -- use the env var instead."""
    global _ENABLED
    _ENABLED = bool(value)


def set_trace_path(path: str) -> None:
    global _TRACE_PATH, _OPEN_FILE, _OPEN_FILE_PATH
    _TRACE_PATH = str(path)
    with _WRITE_LOCK:
        if _OPEN_FILE is not None:
            try:
                _OPEN_FILE.close()
            except Exception:
                pass
        _OPEN_FILE = None
        _OPEN_FILE_PATH = None


def get_trace_path() -> str:
    return _TRACE_PATH


# --------------------------------------------------------------------------
# PII / secret scrubbing -- allow-list, not a deny-list
# --------------------------------------------------------------------------

# Only these keys may ever appear in a trace record's "context" section.
_ALLOWED_CONTEXT_KEYS = {
    "manager_key",
    "event_key",
    "source",
    "function",
    "mode",
    "db_class",
    "conn_id",
    "op_id",
}

# Defense in depth: even an allowed key is redacted if its *value* looks like
# a secret/PII shape (long hex/base64 tokens, phone-number-like digit runs).
_SUSPICIOUS_VALUE_RE = re.compile(r"^[0-9]{7,}$|^[A-Za-z0-9+/_-]{24,}$")

_DENYLIST_SUBSTRINGS = (
    "phone", "full_name", "fullname", "name", "password", "pwd", "secret",
    "token", "session", "proxy", "message", "text", "api_key", "api_hash",
    "email",
)


def _scrub_context(raw: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Keep only allow-listed keys, coerce to short strings, drop anything
    that looks like a secret/PII value even if the key was allowed."""
    out: Dict[str, str] = {}
    if not raw:
        return out
    for k, v in raw.items():
        key = str(k)
        if key not in _ALLOWED_CONTEXT_KEYS:
            continue
        low = key.lower()
        if any(bad in low for bad in _DENYLIST_SUBSTRINGS) and key not in ("manager_key", "event_key"):
            continue
        if v is None:
            continue
        sval = str(v)[:120]
        if _SUSPICIOUS_VALUE_RE.match(sval) and key not in ("manager_key", "event_key"):
            sval = "[redacted]"
        out[key] = sval
    return out


def classify_db_path(path: str) -> Dict[str, str]:
    """Return {"db_class": "central"/"other", "db_basename": <filename only>}.
    Never returns the full filesystem path."""
    try:
        base = os.path.basename(str(path or ""))
    except Exception:
        base = "unknown"
    db_class = "central" if base in _CENTRAL_DB_BASENAMES else "other"
    return {"db_class": db_class, "db_basename": base}


# --------------------------------------------------------------------------
# Identity helpers
# --------------------------------------------------------------------------

def _thread_identity() -> Dict[str, Any]:
    try:
        t = threading.current_thread()
        return {"thread_id": t.ident, "thread_name": t.name}
    except Exception:
        return {"thread_id": None, "thread_name": None}


def _task_identity() -> Dict[str, Any]:
    if asyncio is None:
        return {"task_id": None, "task_name": None}
    try:
        task = asyncio.current_task()
    except Exception:
        task = None
    if task is None:
        return {"task_id": None, "task_name": None}
    try:
        return {"task_id": id(task), "task_name": task.get_name()}
    except Exception:
        return {"task_id": id(task), "task_name": None}


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_op_id() -> str:
    return uuid.uuid4().hex[:16]


# --------------------------------------------------------------------------
# Event sink
# --------------------------------------------------------------------------

def _emit(event: Dict[str, Any]) -> None:
    """Fail-soft append of one JSON line. Never raises. Keeps the sink file
    handle open across calls (opening/closing per event dominated overhead
    in early P5 benchmarking -- see f0_summary.md 'Performance')."""
    global _OPEN_FILE, _OPEN_FILE_PATH
    if not _ENABLED:
        return
    try:
        line = json.dumps(event, ensure_ascii=False, default=str)
    except Exception:
        return
    try:
        with _WRITE_LOCK:
            if _OPEN_FILE is None or _OPEN_FILE_PATH != _TRACE_PATH:
                if _OPEN_FILE is not None:
                    try:
                        _OPEN_FILE.close()
                    except Exception:
                        pass
                d = os.path.dirname(_TRACE_PATH)
                if d:
                    os.makedirs(d, exist_ok=True)
                _OPEN_FILE = open(_TRACE_PATH, "a", encoding="utf-8")
                _OPEN_FILE_PATH = _TRACE_PATH
            _OPEN_FILE.write(line + "\n")
            _OPEN_FILE.flush()
    except Exception:
        # Tracing must never break business logic.
        return


def emit_custom_event(event_type: str, **context: Any) -> None:
    """Escape hatch for diagnostics (e.g. event-loop heartbeat probes) that
    are not themselves a DB transaction. Same allow-list scrubbing applies."""
    if not _ENABLED:
        return
    try:
        rec = {
            "event": str(event_type),
            "ts_utc": _now_utc_iso(),
            "ts_mono": time.monotonic(),
            **_thread_identity(),
            **_task_identity(),
            "context": _scrub_context(context),
        }
        _emit(rec)
    except Exception:
        return


# --------------------------------------------------------------------------
# Transaction handle
# --------------------------------------------------------------------------

class TxTrace:
    """One instrumented transaction's timing state. Created by
    begin_*_sync/async, consumed by commit_*/rollback_*."""

    __slots__ = (
        "op_id", "conn_id", "source", "function", "mode", "db_class",
        "db_basename", "manager_key", "event_key",
        "t0", "t1", "attempt_emitted",
    )

    def __init__(self, *, op_id: str, conn_id: str, source: str, function: str,
                 mode: str, db_class: str, db_basename: str,
                 manager_key: Optional[str], event_key: Optional[str]) -> None:
        self.op_id = op_id
        self.conn_id = conn_id
        self.source = source
        self.function = function
        self.mode = mode
        self.db_class = db_class
        self.db_basename = db_basename
        self.manager_key = manager_key
        self.event_key = event_key
        self.t0 = time.monotonic()
        self.t1: Optional[float] = None
        self.attempt_emitted = False

    def _base_context(self) -> Dict[str, Any]:
        return {
            "manager_key": self.manager_key,
            "event_key": self.event_key,
            "source": self.source,
            "function": self.function,
            "mode": self.mode,
            "db_class": self.db_class,
            "conn_id": self.conn_id,
            "op_id": self.op_id,
        }

    def _record(self, event: str, *, wait_ms=None, hold_ms=None,
                finalize_ms=None, total_ms=None, result=None, error_class=None) -> None:
        try:
            rec: Dict[str, Any] = {
                "event": event,
                "ts_utc": _now_utc_iso(),
                "ts_mono": time.monotonic(),
                "db_basename": self.db_basename,
                **_thread_identity(),
                **_task_identity(),
                "context": _scrub_context(self._base_context()),
                "wait_ms": wait_ms,
                "hold_ms": hold_ms,
                "finalize_ms": finalize_ms,
                "total_ms": total_ms,
                "result": result,
                "error_class": error_class,
            }
            _emit(rec)
        except Exception:
            return


def _new_tx(*, source: str, function: str, mode: str, db_path: str,
            manager_key: Optional[str], event_key: Optional[str],
            conn_id: Optional[str]) -> TxTrace:
    cls = classify_db_path(db_path)
    return TxTrace(
        op_id=_new_op_id(),
        conn_id=conn_id or _new_op_id(),
        source=source, function=function, mode=mode,
        db_class=cls["db_class"], db_basename=cls["db_basename"],
        manager_key=manager_key, event_key=event_key,
    )


# --------------------------------------------------------------------------
# Sync (sqlite3) explicit begin/commit/rollback hooks
# --------------------------------------------------------------------------

def begin_immediate_sync(con, *, source: str, function: str, db_path: str = "",
                          mode: str = "IMMEDIATE", manager_key: str = "",
                          event_key: str = "", conn_id: str = "") -> TxTrace:
    """Drop-in replacement for `con.execute("BEGIN IMMEDIATE")` /
    `con.execute("BEGIN")`. Executes the REAL begin first (unwrapped, so a
    real sqlite error propagates exactly as before); tracing happens after
    and can never mask or alter that real call's outcome."""
    tx = _new_tx(source=source, function=function, mode=mode, db_path=db_path,
                 manager_key=manager_key or None, event_key=event_key or None,
                 conn_id=conn_id)
    if not _ENABLED:
        con.execute(f"BEGIN {mode}" if mode else "BEGIN")
        return tx
    tx._record("DB_TX_ATTEMPT")
    con.execute(f"BEGIN {mode}" if mode else "BEGIN")
    tx.t1 = time.monotonic()
    wait_ms = round((tx.t1 - tx.t0) * 1000.0, 3)
    tx._record("DB_TX_ACQUIRED", wait_ms=wait_ms)
    return tx


def _finish_sync(tx: TxTrace, con, *, action: str, error_class: Optional[str]) -> None:
    if not _ENABLED:
        if action == "commit":
            con.commit()
        else:
            con.rollback()
        return
    t1 = tx.t1 if tx.t1 is not None else tx.t0
    hold_ms = round((time.monotonic() - t1) * 1000.0, 3)
    t2 = time.monotonic()
    try:
        if action == "commit":
            con.commit()
        else:
            con.rollback()
    finally:
        t3 = time.monotonic()
        finalize_ms = round((t3 - t2) * 1000.0, 3)
        wait_ms = round((t1 - tx.t0) * 1000.0, 3)
        total_ms = round((t3 - tx.t0) * 1000.0, 3)
        event = "DB_TX_COMMIT" if action == "commit" else "DB_TX_ROLLBACK"
        if error_class:
            event = "DB_TX_ERROR"
        tx._record(event, wait_ms=wait_ms, hold_ms=hold_ms, finalize_ms=finalize_ms,
                   total_ms=total_ms, result=action, error_class=error_class)


def commit_sync(tx: TxTrace, con) -> None:
    _finish_sync(tx, con, action="commit", error_class=None)


def rollback_sync(tx: TxTrace, con, error: Optional[BaseException] = None) -> None:
    error_class = type(error).__name__ if error is not None else None
    _finish_sync(tx, con, action="rollback", error_class=error_class)


@contextmanager
def traced_tx_sync(con, *, source: str, function: str, db_path: str = "",
                    mode: str = "IMMEDIATE", manager_key: str = "", event_key: str = "",
                    conn_id: str = ""):
    """Context-manager form for NEW code. Does not auto-commit: caller must
    still call con.commit() (normal exit) — mirrors sqlite3's own semantics
    where nothing commits implicitly. On exception, rolls back and re-raises."""
    tx = begin_immediate_sync(con, source=source, function=function, db_path=db_path,
                               mode=mode, manager_key=manager_key, event_key=event_key,
                               conn_id=conn_id)
    try:
        yield tx
    except BaseException as exc:
        rollback_sync(tx, con, error=exc)
        raise
    else:
        commit_sync(tx, con)


# --------------------------------------------------------------------------
# Async (aiosqlite) explicit begin/commit/rollback hooks
# --------------------------------------------------------------------------

async def begin_immediate_async(db, *, source: str, function: str, db_path: str = "",
                                 mode: str = "IMMEDIATE", manager_key: str = "",
                                 event_key: str = "", conn_id: str = "") -> TxTrace:
    tx = _new_tx(source=source, function=function, mode=mode, db_path=db_path,
                 manager_key=manager_key or None, event_key=event_key or None,
                 conn_id=conn_id)
    if not _ENABLED:
        await db.execute(f"BEGIN {mode}" if mode else "BEGIN")
        return tx
    tx._record("DB_TX_ATTEMPT")
    await db.execute(f"BEGIN {mode}" if mode else "BEGIN")
    tx.t1 = time.monotonic()
    wait_ms = round((tx.t1 - tx.t0) * 1000.0, 3)
    tx._record("DB_TX_ACQUIRED", wait_ms=wait_ms)
    return tx


async def _finish_async(tx: TxTrace, db, *, action: str, error_class: Optional[str]) -> None:
    if not _ENABLED:
        if action == "commit":
            await db.commit()
        else:
            await db.rollback()
        return
    t1 = tx.t1 if tx.t1 is not None else tx.t0
    hold_ms = round((time.monotonic() - t1) * 1000.0, 3)
    t2 = time.monotonic()
    try:
        if action == "commit":
            await db.commit()
        else:
            await db.rollback()
    finally:
        t3 = time.monotonic()
        finalize_ms = round((t3 - t2) * 1000.0, 3)
        wait_ms = round((t1 - tx.t0) * 1000.0, 3)
        total_ms = round((t3 - tx.t0) * 1000.0, 3)
        event = "DB_TX_COMMIT" if action == "commit" else "DB_TX_ROLLBACK"
        if error_class:
            event = "DB_TX_ERROR"
        tx._record(event, wait_ms=wait_ms, hold_ms=hold_ms, finalize_ms=finalize_ms,
                   total_ms=total_ms, result=action, error_class=error_class)


async def commit_async(tx: TxTrace, db) -> None:
    await _finish_async(tx, db, action="commit", error_class=None)


async def rollback_async(tx: TxTrace, db, error: Optional[BaseException] = None) -> None:
    error_class = type(error).__name__ if error is not None else None
    await _finish_async(tx, db, action="rollback", error_class=error_class)


@asynccontextmanager
async def traced_tx_async(db, *, source: str, function: str, db_path: str = "",
                           mode: str = "IMMEDIATE", manager_key: str = "", event_key: str = "",
                           conn_id: str = ""):
    tx = await begin_immediate_async(db, source=source, function=function, db_path=db_path,
                                      mode=mode, manager_key=manager_key, event_key=event_key,
                                      conn_id=conn_id)
    try:
        yield tx
    except BaseException as exc:
        await rollback_async(tx, db, error=exc)
        raise
    else:
        await commit_async(tx, db)


# --------------------------------------------------------------------------
# Fail-soft wrappers (moved from main.py `_p5_*`, extraction pass #1,
# 2026-08-21). These are the ones a caller should import when it wants
# tracing to be strictly best-effort: any internal error here falls back to
# the plain sqlite3/aiosqlite call with identical behavior to tracing being
# disabled. Distinct from begin_immediate_sync/commit_sync/etc. above, which
# propagate real DB errors and are meant for callers that want that.
# --------------------------------------------------------------------------

def failsoft_begin_immediate_sync(con, *, source: str, function: str, db_path: str = "",
                                   manager_key: str = "", event_key: str = "") -> Optional[TxTrace]:
    try:
        return begin_immediate_sync(
            con, source=source, function=function, db_path=db_path,
            manager_key=manager_key, event_key=event_key,
        )
    except Exception:
        con.execute("BEGIN IMMEDIATE")
        return None


def failsoft_commit_sync(tx: Optional[TxTrace], con) -> None:
    if tx is None:
        con.commit()
        return
    try:
        commit_sync(tx, con)
    except Exception:
        con.commit()


def failsoft_rollback_sync(tx: Optional[TxTrace], con, error: Optional[BaseException] = None) -> None:
    if tx is None:
        con.rollback()
        return
    try:
        rollback_sync(tx, con, error=error)
    except Exception:
        con.rollback()


async def failsoft_begin_immediate_async(db, *, source: str, function: str, db_path: str = "",
                                          manager_key: str = "", event_key: str = "") -> Optional[TxTrace]:
    try:
        return await begin_immediate_async(
            db, source=source, function=function, db_path=db_path,
            manager_key=manager_key, event_key=event_key,
        )
    except Exception:
        await db.execute("BEGIN IMMEDIATE")
        return None


async def failsoft_commit_async(tx: Optional[TxTrace], db) -> None:
    if tx is None:
        await db.commit()
        return
    try:
        await commit_async(tx, db)
    except Exception:
        await db.commit()


# --------------------------------------------------------------------------
# Event-loop stall probe (local/test diagnostic only -- not auto-started)
# --------------------------------------------------------------------------

class EventLoopStallProbe:
    """Periodic heartbeat: expected tick every `interval_ms`. Records lag_ms
    = actual_gap - expected. Caller starts/stops explicitly; never started
    automatically in production import."""

    def __init__(self, interval_ms: float = 20.0) -> None:
        self.interval_ms = float(interval_ms)
        self.samples: list = []
        self._task = None
        self._stop = False

    async def _run(self) -> None:
        last = time.monotonic()
        while not self._stop:
            await asyncio.sleep(self.interval_ms / 1000.0)
            now = time.monotonic()
            gap_ms = (now - last) * 1000.0
            lag_ms = gap_ms - self.interval_ms
            last = now
            sample = {"ts_mono": now, "expected_ms": self.interval_ms,
                      "actual_gap_ms": round(gap_ms, 3), "lag_ms": round(lag_ms, 3)}
            self.samples.append(sample)
            emit_custom_event("EVENT_LOOP_HEARTBEAT")

    def start(self) -> None:
        if asyncio is None:
            return
        self._stop = False
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except Exception:
                self._task.cancel()

    def max_lag_ms(self) -> float:
        if not self.samples:
            return 0.0
        return max(s["lag_ms"] for s in self.samples)
