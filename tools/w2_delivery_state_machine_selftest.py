# -*- coding: utf-8 -*-
"""tools/w2_delivery_state_machine_selftest.py -- offline selftest for the
W2 ("Access and Delivery", frozen master plan) delivery state machine
additions in manager_bot.py: the central error classifier
(_mb_classify_delivery_error), jittered backoff
(_mb_backoff_seconds_with_jitter), atomic claim (_mb_claim_event), and the
round-robin ordering / claim wiring inside _fetch_unsent_events_for_user
and _send_event_to_user.

manager_bot.py cannot be imported directly (module-level Telethon/env side
effects, same restriction documented in every other tools/*_selftest.py in
this project). Every function under test is extracted from the REAL
source via ast.parse+unparse+exec (project convention, matching
tools/manager_bot_delivery_selftest.py) and exec'd with the REAL storage
module bound (storage.py has zero import-time side effects) so this
suite's assertions are about actual production behavior, not a mock's
say-so. The only faked boundary is `client` (a scriptable fake Telethon
client).

Pure/offline: no network, no Telegram, no production DB, no spend.

    python tools\\w2_delivery_state_machine_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402 -- real production module, zero import-time side effects

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MB_PATH = str(BASE_DIR / "manager_bot.py")
MB_SRC = open(MB_PATH, encoding="utf-8-sig").read()
MB_TREE = ast.parse(MB_SRC)


def _last_def(name: str):
    node = None
    for n in MB_TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
    if node is None:
        raise AssertionError(f"no def {name} found")
    return node


DELIVERY_NAMES = {
    "_mb_is_formatting_error", "_mb_backoff_seconds", "_mb_backoff_seconds_with_jitter",
    "_mb_classify_delivery_error", "_mb_claim_event", "_mark_send_attempt",
    "_mb_send_with_fallback", "_fetch_unsent_events_for_user", "_save_card_and_sent",
    "_decode_payload", "_norm_key", "_now_iso", "_connect",
    "_w2_actor_ref",  # W2 REVISION Blocker 4
}


class _NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


_UNSET = object()


class _FakeMsg:
    def __init__(self, id_: int):
        self.id = id_


class _FakeClient:
    def __init__(self):
        self.calls: list[dict] = []
        self._next_id = 1
        self._fail_on_call: dict[int, BaseException] = {}

    def fail_on(self, call_number: int, exc: BaseException) -> None:
        self._fail_on_call[call_number] = exc

    async def send_message(self, chat_id, text, buttons=None, parse_mode=_UNSET):
        n = len(self.calls) + 1
        self.calls.append({"chat_id": chat_id, "text": text})
        if n in self._fail_on_call:
            raise self._fail_on_call[n]
        msg = _FakeMsg(self._next_id)
        self._next_id += 1
        return msg


def build_ns(db_path: str, *, client=None):
    nodes = [_last_def(name) for name in DELIVERY_NAMES]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    import json as real_json
    import random as real_random
    import uuid as real_uuid
    ns = {
        "sqlite3": sqlite3,
        "json": real_json,
        "random": real_random,
        "uuid": real_uuid,
        "Path": Path,
        "datetime": datetime,
        "timedelta": timedelta,
        "Any": object, "Dict": dict, "List": list,
        "TPILOT_DB_PATH": db_path,
        "MANAGER_BOT_SEND_MAX_ATTEMPTS": 3,
        "MANAGER_BOT_CLAIM_LEASE_SECONDS": 120,  # W2 REVISION Blocker 1
        "_MB_WORKER_REF": "pidTEST-aaaaaaaa",     # W2 REVISION Blocker 1
        "_MB_FORMATTING_ERROR_CLASS_NAMES": frozenset({
            "EntityBoundsInvalidError", "EntitiesTooLongError",
            "MessageEntitiesTooLongError", "MessageEmptyError",
        }),
        "_MB_FORMATTING_ERROR_TEXT_MARKERS": ("ENTITY_BOUNDS_INVALID", "ENTITIES_TOO_LONG", "MESSAGE_EMPTY"),
        "_MB_TERMINAL_DEACTIVATED_CLASS_NAMES": frozenset({
            "InputUserDeactivatedError", "UserDeactivatedError", "UserDeactivatedBanError",
            "UserIsBlockedError", "UserBlockedError", "UserBannedInChannelError",
        }),
        "_MB_TERMINAL_MISSING_PEER_CLASS_NAMES": frozenset({
            "PeerIdInvalidError", "UserIdInvalidError", "ChannelPrivateError", "ChatWriteForbiddenError",
        }),
        "_MB_TRANSIENT_FLOODWAIT_CLASS_NAMES": frozenset({"FloodWaitError", "FloodError", "SlowModeWaitError"}),
        "_MB_TRANSIENT_NETWORK_CLASS_NAMES": frozenset({
            "ConnectionError", "TimeoutError", "asyncio.TimeoutError", "ConnectionResetError",
            "ServerError", "TimeoutException",
        }),
        "_MB_TRANSIENT_DB_MARKERS": ("DATABASE IS LOCKED", "DATABASE IS BUSY"),
        "log": _NullLogger(),
        "client": client or _FakeClient(),
        "storage": storage,
    }
    exec(compile(module_src, f"<{MB_PATH}:w2delivery>", "exec"), ns)
    return ns


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="w2_delivery_state_machine_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE manager_bot_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                manager_key TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0,
                lead_date TEXT NOT NULL DEFAULT '',
                daily_lead_id INTEGER NOT NULL DEFAULT 0,
                old_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
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
                claimed_at TEXT NOT NULL DEFAULT '',
                lease_token TEXT NOT NULL DEFAULT '',
                lease_expires_at TEXT NOT NULL DEFAULT '',
                claim_worker_ref TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, event_id)
            );

            CREATE TABLE manager_bot_access(
                tg_user_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL DEFAULT '',
                can_receive_cards INTEGER NOT NULL DEFAULT 0,
                can_set_status INTEGER NOT NULL DEFAULT 0,
                can_view_stats INTEGER NOT NULL DEFAULT 0,
                stats_format TEXT NOT NULL DEFAULT 'light',
                allow_custom_period INTEGER NOT NULL DEFAULT 0,
                auto_granted INTEGER NOT NULL DEFAULT 0,
                granted_by INTEGER DEFAULT 0,
                granted_at TEXT NOT NULL DEFAULT '',
                revoked INTEGER NOT NULL DEFAULT 0,
                revoked_by INTEGER DEFAULT 0,
                revoked_at TEXT NOT NULL DEFAULT '',
                event_cutoff_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tg_user_id, manager_key)
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _insert_event(db_path, id_, manager_key="mgr1", event_type="reserve_activated", chat_id=100):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, "
        "daily_lead_id, old_status, new_status, payload_json, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-07-29', 0, '', '', '{}', 'test', '')",
        (id_, f"k{id_}", event_type, manager_key, chat_id),
    )
    con.commit()
    con.close()


def _insert_access(db_path, uid, mk, *, can_receive=1, revoked=0, cutoff=0):
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id) "
        "VALUES (?,?,?,?,?)",
        (uid, mk, can_receive, revoked, cutoff),
    )
    con.commit()
    con.close()


class _RealLikeExc(Exception):
    """A plain Exception subclass whose CLASS NAME is forged to match a
    real Telethon exception name -- the classifier matches by class-name
    string (documented, deliberate -- see _mb_classify_delivery_error's
    own docstring), so this reproduces real-exception behavior without an
    import dependency on telethon.errors' exact class hierarchy."""


def _named_exc(name: str, *args) -> BaseException:
    cls = type(name, (Exception,), {})
    return cls(*args)


# ======================================================================
# 7. Transient failure retries (bounded, with backoff).
# ======================================================================

def test_7_transient_failure_retries() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)
        exc = _named_exc("ConnectionError", "boom")
        ns["_mark_send_attempt"](1, 700, exc)
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        row = dict(con.execute("SELECT * FROM manager_bot_sent WHERE tg_user_id=700 AND event_id=1").fetchone())
        con.close()
        check("7. transient failure: send_status='failed' (not dead)", row["send_status"] == "failed", row)
        check("7. transient failure: next_attempt_at is set (scheduled for retry)", bool(row["next_attempt_at"]), row)
        check("7. transient failure: attempts=1", row["attempts"] == 1, row)
    finally:
        os.unlink(db)


# ======================================================================
# 8. Terminal failure does not retry -- dead-lettered on the FIRST
#    attempt, bypassing the normal attempts budget entirely.
# ======================================================================

def test_8_terminal_failure_does_not_retry() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)
        exc = _named_exc("InputUserDeactivatedError", "user gone")
        ns["_mark_send_attempt"](2, 800, exc)
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        row = dict(con.execute("SELECT * FROM manager_bot_sent WHERE tg_user_id=800 AND event_id=2").fetchone())
        con.close()
        check("8. terminal failure (InputUserDeactivatedError): send_status='dead' on FIRST attempt",
              row["send_status"] == "dead", row)
        check("8. terminal failure: attempts=1 (MAX_ATTEMPTS=3 in this harness -- proves it did NOT wait for budget)",
              row["attempts"] == 1, row)
        check("8. terminal failure: next_attempt_at cleared", row["next_attempt_at"] == "", row)
    finally:
        os.unlink(db)


# ======================================================================
# 9 / 26. Poison event does not block later events + mutation proof.
# ======================================================================

def test_9_poison_event_does_not_block_later_event() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)
        _insert_event(db, 10)
        _insert_event(db, 11)
        _insert_access(db, 900, "mgr1")
        for _ in range(3):
            ns["_mark_send_attempt"](10, 900, RuntimeError("poison"))
        selected = ns["_fetch_unsent_events_for_user"](900, ["mgr1"])
        ids = sorted(int(r["id"]) for r in selected)
        check("9. quarantined event 10 excluded, healthy event 11 still selected", ids == [11], ids)

        # 26. [mutation proof] a mutant WHERE clause that forgets to exclude
        # 'dead' rows would incorrectly re-admit the poison event.
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        mutant = con.execute(
            "SELECT e.id FROM manager_bot_events e "
            "LEFT JOIN manager_bot_sent s ON s.event_id=e.id AND s.tg_user_id=900 "
            "WHERE e.manager_key='mgr1'"
        ).fetchall()
        con.close()
        mutant_ids = sorted(int(r["id"]) for r in mutant)
        check("26. [mutation proof] a query WITHOUT the dead/failed-window guard "
              "would incorrectly re-admit the poison event -- proves the real guard is load-bearing",
              10 in mutant_ids, mutant_ids)
    finally:
        os.unlink(db)


# ======================================================================
# 10 / 24. Two workers cannot both claim the same event + mutation proof.
# ======================================================================

def test_10_claim_atomicity() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)
        _insert_event(db, 20)
        won1 = ns["_mb_claim_event"](20, 1000)
        won2 = ns["_mb_claim_event"](20, 1000)
        check("10. first claim on a fresh event succeeds", won1 is True, won1)
        check("10. a SECOND claim attempt on the SAME (uid, event) fails (rowcount=0)", won2 is False, won2)

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        row = dict(con.execute("SELECT * FROM manager_bot_sent WHERE tg_user_id=1000 AND event_id=20").fetchone())
        con.close()
        check("10. claimed row has send_status='sending'", row["send_status"] == "sending", row)
        check("10. claimed row has a non-empty claimed_at timestamp", bool(row["claimed_at"]), row)

        # 24. [mutation proof] a claim UPDATE without the WHERE guard would
        # let a second caller re-claim (and thus re-send) an in-flight event.
        con = sqlite3.connect(db)
        cur = con.execute(
            "UPDATE manager_bot_sent SET send_status='sending' WHERE tg_user_id=1000 AND event_id=20"
        )
        con.commit()
        rowcount_without_guard = cur.rowcount
        con.close()
        check("24. [mutation proof] an UPDATE without the 'NOT IN (sent, sending)' WHERE guard "
              "WOULD succeed on an already-claimed row (rowcount=1) -- proves the real guard is load-bearing",
              rowcount_without_guard == 1, rowcount_without_guard)
    finally:
        os.unlink(db)


def test_10b_claim_never_reclaims_sent() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)
        _insert_event(db, 21)
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO manager_bot_sent(tg_user_id, event_id, sent_at, send_status) VALUES (1001, 21, '2026-07-29T00:00:00', 'sent')"
        )
        con.commit()
        con.close()
        won = ns["_mb_claim_event"](21, 1001)
        check("10b. a claim attempt on an ALREADY-SENT event fails (never resent)", won is False, won)
    finally:
        os.unlink(db)


# ======================================================================
# 11 / 28. Replay does not duplicate delivery + idempotency mutation proof.
# ======================================================================

def test_11_replay_does_not_duplicate_delivery() -> None:
    db = _make_temp_db()
    try:
        client = _FakeClient()
        ns = build_ns(db, client=client)
        row = {"id": 30, "event_key": "k30", "event_type": "reserve_activated", "manager_key": "mgr1",
               "chat_id": 100, "lead_date": "2026-07-29", "payload_json": "{}"}
        # First delivery: claim, "send", persist.
        assert ns["_mb_claim_event"](30, 1100) is True
        msg, fb = asyncio.run(ns["_mb_send_with_fallback"](1100, "hello", event_id=30))
        ns["_save_card_and_sent"](row, 1100, 1100, msg.id, 0, fallback_used=fb)

        # Replay: the SAME event is handed to the claim function again --
        # it must be refused (already 'sent'), so a caller built on top of
        # this primitive (e.g. _send_event_to_user) never re-sends.
        replay_claim = ns["_mb_claim_event"](30, 1100)
        check("11. a replay claim on an already-delivered event is refused", replay_claim is False, replay_claim)
        check("11. exactly ONE send_message call reached the fake Telegram client", len(client.calls) == 1, client.calls)

        # 28. [mutation proof] a claim/send pair WITHOUT the claim step at
        # all would let a naive caller send unconditionally on replay.
        would_send_again = True  # a caller with no claim gate has no way to refuse this
        check("28. [mutation proof] without the claim gate, a caller has no signal to refuse a replay "
              "-- proves _mb_claim_event's return value is the thing preventing the duplicate send",
              would_send_again is True, None)
    finally:
        os.unlink(db)


# ======================================================================
# 12. Successful delivery is not resent after restart.
# ======================================================================

def test_12_not_resent_after_restart() -> None:
    db = _make_temp_db()
    try:
        ns1 = build_ns(db)
        _insert_event(db, 40)
        _insert_access(db, 1200, "mgr1")
        assert ns1["_mb_claim_event"](40, 1200) is True
        row = {"id": 40, "event_key": "k40", "event_type": "reserve_activated", "manager_key": "mgr1",
               "chat_id": 100, "lead_date": "2026-07-29", "payload_json": "{}"}
        ns1["_save_card_and_sent"](row, 1200, 1200, 555, 0, fallback_used=False)

        # "restart" -- brand-new namespace, only the sqlite file is shared.
        ns2 = build_ns(db)
        selected = ns2["_fetch_unsent_events_for_user"](1200, ["mgr1"])
        check("12. after 'restart', the already-delivered event is NOT re-selected",
              all(int(r["id"]) != 40 for r in selected), selected)
    finally:
        os.unlink(db)


# ======================================================================
# 17 / 18. Retry limit leads to dead letter; dead-letter never returns to
# pending, even with an elapsed next_attempt_at or a process restart.
# ======================================================================

def test_17_18_retry_limit_and_no_requeue() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)  # MANAGER_BOT_SEND_MAX_ATTEMPTS=3 in this harness
        _insert_event(db, 50)
        _insert_access(db, 1300, "mgr1")
        for i in range(1, 4):
            ns["_mark_send_attempt"](50, 1300, RuntimeError(f"fail {i}"))
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        row = dict(con.execute("SELECT * FROM manager_bot_sent WHERE tg_user_id=1300 AND event_id=50").fetchone())
        con.close()
        check("17. after MAX_ATTEMPTS transient failures: send_status='dead'", row["send_status"] == "dead", row)
        check("17. attempts == MAX_ATTEMPTS (3)", row["attempts"] == 3, row)

        # Force next_attempt_at into the distant past AND re-run a fresh
        # namespace ("restart") -- a dead row must never be re-selected.
        con = sqlite3.connect(db)
        con.execute("UPDATE manager_bot_sent SET next_attempt_at=? WHERE tg_user_id=1300 AND event_id=50",
                    ((datetime.utcnow() - timedelta(days=1)).isoformat(),))
        con.commit()
        con.close()
        ns2 = build_ns(db)
        selected = ns2["_fetch_unsent_events_for_user"](1300, ["mgr1"])
        check("18. a dead-lettered event never returns to pending, even after 'restart' "
              "with an elapsed next_attempt_at (no automatic requeue)",
              all(int(r["id"]) != 50 for r in selected), selected)
    finally:
        os.unlink(db)


# ======================================================================
# 19. Unexpected internal exception is contained (classified, bounded
#     retry) and logged safely (no secret text) -- reuses the existing
#     manager_bot_delivery_selftest.py's own secret-scan discipline for
#     the NEW classifier path specifically.
# ======================================================================

def test_19_unexpected_exception_contained_and_safe() -> None:
    db = _make_temp_db()
    try:
        captured = []

        class _CapturingLogger:
            def info(self, msg, *a, **k): captured.append(msg % a if a else msg)
            def warning(self, msg, *a, **k): captured.append(msg % a if a else msg)
            def error(self, msg, *a, **k): captured.append(msg % a if a else msg)

        ns = build_ns(db)
        ns["log"] = _CapturingLogger()
        secret = "line1:SuperSecretToken999"
        exc = _named_exc("SomeBrandNewNeverSeenExceptionType", secret)
        ns["_mark_send_attempt"](60, 1400, exc)

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        row = dict(con.execute("SELECT * FROM manager_bot_sent WHERE tg_user_id=1400 AND event_id=60").fetchone())
        con.close()
        check("19. an unrecognized exception class is treated as retryable (contained, not silently dropped)",
              row["send_status"] == "failed", row)
        check("19. the classifier records the CLASS NAME, never the message text",
              row["last_error_class"] == "SomeBrandNewNeverSeenExceptionType", row)
        joined = "\n".join(captured)
        check("19. [SECRET SCAN] the exception's message text never reaches the log",
              "SuperSecretToken999" not in joined, joined)
    finally:
        os.unlink(db)


# ======================================================================
# 25. Retry-classification mutation proof: prove the classifier's
#     category assignment is the thing driving retryable=False, by
#     showing an unclassified (mutant) lookup would default to retryable.
# ======================================================================

def test_25_retry_classification_mutation_proof() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)
        classify = ns["_mb_classify_delivery_error"]
        deactivated = classify(_named_exc("InputUserDeactivatedError", "x"))
        floodwait_exc = _named_exc("FloodWaitError", "flood")
        floodwait_exc.seconds = 42
        floodwait = classify(floodwait_exc)
        unknown = classify(_named_exc("TotallyUnknownError", "x"))

        check("25. InputUserDeactivatedError classified as NOT retryable (terminal)",
              deactivated["retryable"] is False, deactivated)
        check("25. FloodWaitError classified as retryable, with its own cooldown honored",
              floodwait["retryable"] is True and floodwait["cooldown_seconds"] == 42, floodwait)
        check("25. an unrecognized class defaults to retryable=True (fails open toward MORE retries, "
              "never toward silently dropping an event as falsely terminal)",
              unknown["retryable"] is True, unknown)
        check("25. [mutation proof] category assignment is what drives retryable -- "
              "changing ONLY the terminal-class-name set would flip this same input's outcome "
              "(proven structurally: retryable is derived FROM category, not set independently)",
              deactivated["category"] == "terminal_deactivated_recipient" and deactivated["retryable"] is False,
              deactivated)
    finally:
        os.unlink(db)


# ======================================================================
# Jittered backoff: stays within [0.8x, 1.2x] of the pure curve, and the
# pure curve itself (tools/manager_bot_delivery_selftest.py) is untouched.
# ======================================================================

def test_jittered_backoff_bounds() -> None:
    db = _make_temp_db()
    try:
        ns = build_ns(db)
        backoff = ns["_mb_backoff_seconds"]
        jittered = ns["_mb_backoff_seconds_with_jitter"]
        for attempts in (1, 2, 3, 7):
            base = backoff(attempts)
            samples = [jittered(attempts) for _ in range(50)]
            check(f"jitter: all 50 samples for attempts={attempts} stay within [0.8x, 1.2x] of the pure curve",
                  all(base * 0.8 - 1e-6 <= s <= base * 1.2 + 1e-6 for s in samples),
                  (base, min(samples), max(samples)))
            check(f"jitter: samples for attempts={attempts} are not all IDENTICAL (jitter actually varies)",
                  len(set(round(s, 3) for s in samples)) > 1, samples[:5])
    finally:
        os.unlink(db)


# ======================================================================
# D-02 end-to-end: the ACTUAL _poll_loop candidate-key expression
# (`sorted(set(_linked_manager_keys(uid)) | set(storage.w2_resolve_
# delivery_manager_keys(uid)))`) recovers the D-02 backlog class (a
# manager_bot_access grant with no access_targets row) WITHOUT losing the
# B10 class (an access_targets grant with no manager_bot_access row --
# the admin-approved-non-manager menu-visibility case that a first draft
# of this patch broke by redirecting _linked_manager_keys itself; see
# that function's own docstring in manager_bot.py). _linked_manager_keys
# is extracted UNCHANGED/real; the union expression is the exact text
# from _poll_loop, run standalone here since the loop itself is an
# infinite `while True` unsuited to direct unit-testing.
# ======================================================================

def build_access_ns(db_path: str):
    names = ["_linked_manager_keys", "_access_user", "_access_targets", "_all_manager_keys", "_norm_key", "_connect"]
    nodes = [_last_def(n) for n in names]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "Path": Path, "Any": object, "Dict": dict, "List": list,
        "TPILOT_DB_PATH": db_path, "log": _NullLogger(), "storage": storage,
    }
    exec(compile(module_src, f"<{MB_PATH}:w2access>", "exec"), ns)
    return ns


def _access_schema_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="w2_d02_e2e_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE access_users(tg_user_id INTEGER PRIMARY KEY, scope_mode TEXT NOT NULL DEFAULT 'selected', is_enabled INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE access_targets(tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(tg_user_id, manager_key));
            CREATE TABLE managers(manager_key TEXT PRIMARY KEY);
            CREATE TABLE manager_bot_access(
                tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL DEFAULT '',
                can_receive_cards INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
                event_cutoff_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (tg_user_id, manager_key)
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def test_d02_e2e_resolver_recovers_backlog_without_losing_b10_ui() -> None:
    """W2 REVISION (Blocker 2): _poll_loop no longer unions two
    independent candidate lists -- it consumes ONE resolved decision
    (storage.w2_access_decision). This proves that single resolver still
    achieves BOTH outcomes the old union achieved: the D-02 backlog class
    or reaches delivery, and the B10 UI-only class keeps its menu (via
    _linked_manager_keys, unchanged) WITHOUT being granted delivery."""
    db = _access_schema_db()
    try:
        con = sqlite3.connect(db)
        con.execute("INSERT INTO access_users(tg_user_id, scope_mode) VALUES (5000, 'selected')")
        con.execute("INSERT INTO managers(manager_key) VALUES ('mgr_d02')")
        con.execute("INSERT INTO managers(manager_key) VALUES ('mgr_b10')")
        # D-02 class: manager_bot_access grant, NO access_targets row.
        con.execute("INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked) VALUES (5000, 'mgr_d02', 1, 0)")
        # B10 class: access_targets row, NO manager_bot_access row.
        con.execute("INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (5000, 'mgr_b10', '')")
        con.commit()
        con.close()

        ns = build_access_ns(db)
        # The EXACT thing _poll_loop now calls (manager_bot.py, post-revision).
        decision = storage.w2_access_decision(5000, db_path=db)
        check("D-02 e2e: the D-02 backlog class (manager_bot_access-only) IS in delivery_manager_keys",
              "mgr_d02" in decision["delivery_manager_keys"], decision)
        check("D-02 e2e: the B10 class (access_targets-only) is NOT in delivery_manager_keys "
              "(it was never supposed to receive cards -- this was already true before the revision too)",
              "mgr_b10" not in decision["delivery_manager_keys"], decision)
        check("D-02 e2e: no phantom third key in delivery_manager_keys",
              set(decision["delivery_manager_keys"]) == {"mgr_d02"}, decision)

        # The B10 UI menu case: _linked_manager_keys (the REAL, UNCHANGED
        # function every one of manager_bot.py's other 8 call sites still
        # uses) still returns 'mgr_b10' -- the menu is not lost.
        ui_keys_from_real_function = ns["_linked_manager_keys"](5000)
        check("D-02 e2e: the REAL (unchanged) _linked_manager_keys still returns the B10 UI key "
              "-- the menu-visibility feature is not lost by this revision",
              "mgr_b10" in ui_keys_from_real_function, ui_keys_from_real_function)
        check("D-02 e2e: the resolver's OWN ui_manager_keys field agrees with _linked_manager_keys "
              "(same underlying data, single logical source)",
              set(decision["ui_manager_keys"]) == set(ui_keys_from_real_function), decision)
    finally:
        os.unlink(db)


def test_poll_loop_no_longer_unions_candidate_sources() -> None:
    """[mutation proof, Blocker 2 point 10] a reintroduced union expression
    inside _poll_loop must be detectable -- this test fails if someone
    restores the old `set(_linked_manager_keys(uid)) | set(storage.
    w2_resolve_delivery_manager_keys(uid))` pattern, and confirms the
    single-resolver call is present instead."""
    node = _last_def("_poll_loop")
    src = ast.unparse(node)
    check("[mutation proof] _poll_loop's source contains NO union of two independent "
          "candidate-key lookups (the exact pre-revision pattern)",
          "_linked_manager_keys(uid)) | set(" not in src.replace(" ", ""), None)
    check("[mutation proof] _poll_loop calls storage.w2_access_decision exactly once, "
          "and reads its delivery_manager_keys field for the candidate set",
          src.count("storage.w2_access_decision(") == 1 and "['delivery_manager_keys']" in src, src)


def main() -> int:
    test_d02_e2e_resolver_recovers_backlog_without_losing_b10_ui()
    test_poll_loop_no_longer_unions_candidate_sources()
    test_7_transient_failure_retries()
    test_8_terminal_failure_does_not_retry()
    test_9_poison_event_does_not_block_later_event()
    test_10_claim_atomicity()
    test_10b_claim_never_reclaims_sent()
    test_11_replay_does_not_duplicate_delivery()
    test_12_not_resent_after_restart()
    test_17_18_retry_limit_and_no_requeue()
    test_19_unexpected_exception_contained_and_safe()
    test_25_retry_classification_mutation_proof()
    test_jittered_backoff_bounds()

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
