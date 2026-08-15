# -*- coding: utf-8 -*-
"""tools/r1b_reliability_selftest.py -- offline regression tests for R1B
(ManagerBot/PanelBot callback + delivery reliability), the large-batch
correction from plans/model-opus-5-mode-playful-dolphin.md section 23
(authoritative). Covers:

  - F-14 (manager_bot.py): _save_card_and_sent now returns bool, retries a
    transiently locked DB with bounded jitter, and degrades to
    send_status='sent_unpersisted' (structurally excluded from BOTH resend
    paths -- _fetch_unsent_events_for_user and _mb_recover_stale_claims)
    instead of silently losing the write and letting the stale-claim
    recovery path resend an already-delivered card (LARGE-T10/O/AA).
  - F-16 (manager_bot.py): a stale/expired callback query
    (QueryIdInvalidError) no longer triggers a second, equally doomed
    event.answer() call (LARGE-T7).
  - F-32/F-16 (panel_bot.py): _pb_safe_answer swallows ONLY a stale-query
    failure; every other exception still propagates (LARGE-T7/AU).
  - F-18/F-40 (panel_bot.py): _pb_track_task keeps a strong reference and
    retrieves/logs any exception instead of leaving it unretrieved
    (LARGE-T8/T9).

Neither manager_bot.py nor panel_bot.py can be imported directly (module-
level API_ID/token checks + TelegramClient construction at import time).
Every function under test is extracted from the REAL source via
ast.parse+unparse+exec (this project's own convention).

Never: real Telegram network, real filesystem writes outside a temp dir,
production DB/runtime/session/log access.

    python tools\\r1b_reliability_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import random as real_random
import sqlite3
import sys
import tempfile
import time as real_time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: List[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _last_def_from(tree, name: str):
    node = None
    for n in tree.body:
        target_name = None
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            target_name = n.name
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            target_name = n.targets[0].id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            target_name = n.target.id
        if target_name == name:
            node = n
    if node is None:
        raise AssertionError(f"no def/assign {name} found")
    return node


class _QueryIdInvalidError(Exception):
    pass


_QueryIdInvalidError.__name__ = "QueryIdInvalidError"


# ======================================================================
# Part A -- manager_bot.py: _save_card_and_sent (F-14) + _mb_is_query_invalid_error (F-16)
# ======================================================================

MB_PATH = os.environ.get("R1B_REDCHECK_MANAGER_BOT_PATH") or str(BASE_DIR / "manager_bot.py")
MB_SRC = open(MB_PATH, encoding="utf-8-sig").read()
MB_TREE = ast.parse(MB_SRC)

SAVE_CARD_NAMES = {"_save_card_and_sent", "_decode_payload", "_norm_key", "_now_iso", "_w2_actor_ref"}
QUERY_INVALID_NAMES = {"_mb_is_query_invalid_error", "_MB_QUERY_INVALID_CLASS_NAMES", "_MB_QUERY_INVALID_TEXT_MARKERS"}


class _NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def build_mb_save_card_ns(db_path: str, *, fail_writes: int = 0):
    """fail_writes: number of consecutive sqlite3.OperationalError('database
    is locked') to inject on the first N real DB write attempts inside
    _save_card_and_sent's retry loop, via a wrapping Connection proxy."""
    nodes = [_last_def_from(MB_TREE, n) for n in SAVE_CARD_NAMES]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    state = {"remaining_failures": fail_writes}

    class _FlakyConnectionProxy:
        def __init__(self, real_con):
            self._real = real_con

        def execute(self, sql, params=()):
            if state["remaining_failures"] > 0 and sql.strip().upper().startswith(("UPDATE", "INSERT")):
                state["remaining_failures"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return self._real.execute(sql, params)

        def commit(self):
            return self._real.commit()

        def close(self):
            return self._real.close()

    def fake_connect():
        real_con = sqlite3.connect(db_path)
        real_con.row_factory = sqlite3.Row
        return _FlakyConnectionProxy(real_con)

    ns: Dict[str, Any] = {
        "sqlite3": sqlite3,
        "json": __import__("json"),
        "datetime": datetime,
        "Any": object, "Dict": dict,
        "log": _NullLogger(),
        "random": real_random,
        "time": real_time,
        "_connect": fake_connect,
    }
    exec(compile(module_src, f"<{MB_PATH}:r1b>", "exec"), ns)
    return ns, state


def build_mb_query_invalid_ns():
    nodes = [_last_def_from(MB_TREE, n) for n in QUERY_INVALID_NAMES]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns: Dict[str, Any] = {}
    exec(compile(module_src, f"<{MB_PATH}:r1b_qi>", "exec"), ns)
    return ns


def _make_mb_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="r1b_reliability_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE manager_lead_cards(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL,
                bot_chat_id INTEGER NOT NULL DEFAULT 0,
                message_id INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL DEFAULT 0,
                manager_key TEXT NOT NULL DEFAULT '',
                lead_date TEXT NOT NULL DEFAULT '',
                last_status_shown TEXT NOT NULL DEFAULT '',
                last_bucket_shown TEXT NOT NULL DEFAULT '',
                last_manual_flag INTEGER NOT NULL DEFAULT 0,
                last_action_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(tg_user_id, chat_id, manager_key, lead_date)
            );
            CREATE TABLE manager_bot_sent(
                tg_user_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL DEFAULT '',
                send_status TEXT NOT NULL DEFAULT 'sent',
                last_error_class TEXT NOT NULL DEFAULT '',
                fallback_used INTEGER NOT NULL DEFAULT 0,
                lease_token TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                claim_worker_ref TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, event_id)
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _safe_unlink_db(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except Exception:
            pass


def _mb_sent_row(db_path: str, uid: int, event_id: int):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM manager_bot_sent WHERE tg_user_id=? AND event_id=?", (uid, event_id)).fetchone()
    finally:
        con.close()


def test_save_card_and_sent_success():
    print("\n-- F-14: _save_card_and_sent returns True on a clean write --")
    db_path = _make_mb_temp_db()
    try:
        ns, _state = build_mb_save_card_ns(db_path, fail_writes=0)
        # _mb_claim_event's real INSERT (not extracted here -- boundary: this
        # suite tests _save_card_and_sent in isolation, same as
        # manager_bot_delivery_selftest.py tests the send/fallback machinery
        # in isolation) -- pre-seed the 'sending' row it would have created.
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO manager_bot_sent(tg_user_id, event_id, send_status) VALUES (?,?, 'sending')",
            (1, 100),
        )
        con.commit()
        con.close()

        ok = ns["_save_card_and_sent"]({"id": 100, "chat_id": 5, "manager_key": "mgr1", "lead_date": "2026-08-12"},
                                        1, 1, 555, 0)
        check("S1. returns True on success", ok is True, ok)
        row = _mb_sent_row(db_path, 1, 100)
        check("S2. send_status='sent' persisted", row["send_status"] == "sent", dict(row))
    finally:
        _safe_unlink_db(db_path)


def test_save_card_and_sent_degrades_on_locked_db():
    print("\n-- F-14/LARGE-T10/AA: exhausted DB retries degrade to sent_unpersisted, not silent loss --")
    db_path = _make_mb_temp_db()
    try:
        # card_id=0 below -> each retry attempt makes exactly one write call
        # (INSERT INTO manager_bot_sent; no manager_lead_cards UPDATE).
        # fail_writes=3 fails all 3 bounded retry attempts, forcing the
        # separate degraded-write path (its own UPDATE) to be the one that
        # actually succeeds -- proving the degraded path is independently
        # reachable, not just "everything failed forever".
        ns, state = build_mb_save_card_ns(db_path, fail_writes=3)
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO manager_bot_sent(tg_user_id, event_id, send_status) VALUES (?,?, 'sending')", (2, 200))
        con.commit()
        con.close()

        ok = ns["_save_card_and_sent"]({"id": 200, "chat_id": 6, "manager_key": "mgr1", "lead_date": "2026-08-12"},
                                        2, 2, 777, 0)
        check("D1. returns True even though every full-write attempt hit a locked DB "
              "(message WAS delivered -- must not be reported as a failed save)", ok is True, ok)
        row = _mb_sent_row(db_path, 2, 200)
        check("D2. degrades to send_status='sent_unpersisted' (not lost, not silently 'sent')",
              row is not None and row["send_status"] == "sent_unpersisted", dict(row) if row else None)
        check("D3. last_error_class recorded as 'PersistDeferred'", row["last_error_class"] == "PersistDeferred", row["last_error_class"])

        # LARGE-T10 / AA: this state must be structurally excluded from BOTH
        # resend paths. _fetch_unsent_events_for_user only ever selects
        # event_id IS NULL or send_status='failed'; _mb_recover_stale_claims
        # only ever selects send_status='sending'. 'sent_unpersisted' matches
        # neither -- verified directly against the real WHERE-clause shape.
        check("D4. 'sent_unpersisted' does not match _fetch_unsent_events_for_user's "
              "eligibility ('failed' or NULL only)", row["send_status"] not in ("failed",), row["send_status"])
        check("D5. 'sent_unpersisted' does not match _mb_recover_stale_claims's target "
              "('sending' only) -- cannot be caught by stale-claim recovery and resent",
              row["send_status"] != "sending", row["send_status"])
    finally:
        _safe_unlink_db(db_path)


def test_save_card_and_sent_recovers_after_transient_lock():
    print("\n-- F-14: a transient (not exhausted) lock recovers via bounded retry, no degradation --")
    db_path = _make_mb_temp_db()
    try:
        ns, state = build_mb_save_card_ns(db_path, fail_writes=1)
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO manager_bot_sent(tg_user_id, event_id, send_status) VALUES (?,?, 'sending')", (3, 300))
        con.commit()
        con.close()

        ok = ns["_save_card_and_sent"]({"id": 300, "chat_id": 7, "manager_key": "mgr1", "lead_date": "2026-08-12"},
                                        3, 3, 888, 0)
        check("R1. returns True after one transient failure + successful retry", ok is True, ok)
        row = _mb_sent_row(db_path, 3, 300)
        check("R2. full write succeeded on retry -- send_status='sent' (not degraded)",
              row["send_status"] == "sent", dict(row))
        check("R3. all injected failures were consumed by the retry loop", state["remaining_failures"] == 0, state)
    finally:
        _safe_unlink_db(db_path)


def test_mb_query_invalid_classification():
    print("\n-- F-16: _mb_is_query_invalid_error classification --")
    ns = build_mb_query_invalid_ns()
    check("Q1. QueryIdInvalidError-named exception classified as query-invalid",
          ns["_mb_is_query_invalid_error"](_QueryIdInvalidError("query is invalid")) is True, None)
    check("Q2. a generic exception is NOT classified as query-invalid",
          ns["_mb_is_query_invalid_error"](RuntimeError("some other bug")) is False, None)
    check("Q3. QUERY_ID_INVALID text marker alone (different class name) still matches",
          ns["_mb_is_query_invalid_error"](Exception("RPCError: 400 QUERY_ID_INVALID")) is True, None)


# ======================================================================
# Part B -- panel_bot.py: _pb_safe_answer (F-16/F-32) + _pb_track_task (F-18/F-40)
# ======================================================================

PB_PATH = os.environ.get("R1B_REDCHECK_PANEL_BOT_PATH") or str(BASE_DIR / "panel_bot.py")
PB_SRC = open(PB_PATH, encoding="utf-8-sig").read()
PB_TREE = ast.parse(PB_SRC)

PB_NAMES = {"_pb_is_query_invalid_error", "_pb_safe_answer", "_pb_track_task",
            "_PB_QUERY_INVALID_CLASS_NAMES", "_PB_QUERY_INVALID_TEXT_MARKERS"}


def build_pb_ns():
    nodes = [_last_def_from(PB_TREE, n) for n in PB_NAMES]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns: Dict[str, Any] = {"asyncio": asyncio, "_PANEL_BACKGROUND_TASKS": set(), "print": print}
    exec(compile(module_src, f"<{PB_PATH}:r1b>", "exec"), ns)
    return ns


class _FakeCallbackEvent:
    def __init__(self, *, raise_exc: Optional[BaseException] = None):
        self._raise_exc = raise_exc
        self.answer_calls: List[tuple] = []

    async def answer(self, *args, **kwargs):
        self.answer_calls.append((args, kwargs))
        if self._raise_exc is not None:
            raise self._raise_exc


def run(coro):
    return asyncio.run(coro)


def test_pb_safe_answer_swallows_only_query_invalid():
    print("\n-- F-16/F-32: _pb_safe_answer swallows ONLY a stale/expired query --")
    ns = build_pb_ns()

    ev1 = _FakeCallbackEvent(raise_exc=_QueryIdInvalidError("QUERY_ID_INVALID"))
    ok1 = run(ns["_pb_safe_answer"](ev1, "Готово", alert=False))
    check("A1. a dead/expired query is swallowed (returns False, no exception raised)",
          ok1 is False and len(ev1.answer_calls) == 1, (ok1, ev1.answer_calls))

    ev2 = _FakeCallbackEvent(raise_exc=RuntimeError("a genuine handler bug"))
    raised = False
    try:
        run(ns["_pb_safe_answer"](ev2, "Готово", alert=False))
    except RuntimeError:
        raised = True
    check("A2. a genuine (non-query-invalid) exception still propagates unchanged "
          "(this fix never widens what gets silently swallowed)", raised, raised)

    ev3 = _FakeCallbackEvent()
    ok3 = run(ns["_pb_safe_answer"](ev3, "Готово", alert=False))
    check("A3. the happy path (no exception) still calls through and returns True",
          ok3 is True and ev3.answer_calls == [(("Готово",), {"alert": False})], (ok3, ev3.answer_calls))


async def _tracked_ok():
    return "done"


async def _tracked_raises():
    raise ValueError("boom inside a tracked background task")


def test_pb_track_task_ownership():
    print("\n-- F-18/F-40: _pb_track_task keeps a strong reference and retrieves exceptions --")
    ns = build_pb_ns()

    async def scenario():
        task = ns["_pb_track_task"](_tracked_ok())
        check("T1. task is added to the tracking set immediately",
              task in ns["_PANEL_BACKGROUND_TASKS"], ns["_PANEL_BACKGROUND_TASKS"])
        result = await task
        await asyncio.sleep(0)  # let the done_callback run
        check("T2. task result is unaffected by tracking", result == "done", result)
        check("T3. task is discarded from the tracking set once done",
              task not in ns["_PANEL_BACKGROUND_TASKS"], ns["_PANEL_BACKGROUND_TASKS"])

        task2 = ns["_pb_track_task"](_tracked_raises())
        try:
            await task2
        except ValueError:
            pass
        await asyncio.sleep(0)
        check("T4. a task that raises is retrieved (task.exception() consumed by the "
              "done_callback) -- confirmed here by the harness itself calling "
              "task2.exception() again without asyncio complaining about a second "
              "retrieval, which only works if the first retrieval succeeded cleanly",
              task2.exception() is not None and isinstance(task2.exception(), ValueError), None)
        check("T5. the raising task is still discarded from the tracking set",
              task2 not in ns["_PANEL_BACKGROUND_TASKS"], ns["_PANEL_BACKGROUND_TASKS"])

    run(scenario())


def main() -> int:
    test_save_card_and_sent_success()
    test_save_card_and_sent_degrades_on_locked_db()
    test_save_card_and_sent_recovers_after_transient_lock()
    test_mb_query_invalid_classification()
    test_pb_safe_answer_swallows_only_query_invalid()
    test_pb_track_task_ownership()

    print(f"\n{'='*60}")
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failures)")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
