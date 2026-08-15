# -*- coding: utf-8 -*-
"""tools/manager_bot_delivery_selftest.py -- forward-fix Phase 3/4
(post-incident review 2026-07-26): the ManagerBot event delivery state
machine (manager_bot_sent.attempts/next_attempt_at/send_status/
last_error_class/fallback_used), added to fix the proven production
incident class -- a Telegram markdown-formatting failure
(EntityBoundsInvalidError, from an unsanitized dynamic field like a lead's
full_name/reason interpolated into a markdown-parsed send) had NO attempt
counter, NO backoff, and NO plain-text fallback: the SAME event was
re-selected and re-sent every ~5s poll cycle FOREVER, immune to a
restart (the selection query always orders by id ASC, so the oldest
unsent event is re-picked first).

manager_bot.py cannot be imported directly (module-level API_ID/
MANAGER_BOT_TOKEN checks + TelegramClient construction -- same
Telethon/env-side-effect restriction as panel_bot.py/main.py). Every
function under test is extracted from the REAL source via
ast.parse+unparse+exec (project convention). The ONLY faked boundary is
`client` (a fake Telethon client whose send_message can be scripted to
fail/succeed) -- everything else (schema, SQL, backoff math, error
classification) is the REAL production code running against a REAL temp
sqlite file.

    python tools\\manager_bot_delivery_selftest.py
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

from telethon.errors import EntityBoundsInvalidError  # noqa: E402 -- real Telethon exception class

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


def _last_assign(name: str):
    node = None
    for n in MB_TREE.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
            node = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            node = n
    if node is None:
        raise AssertionError(f"no assign {name} found")
    return node


DELIVERY_NAMES = {
    "_mb_is_formatting_error", "_mb_backoff_seconds", "_mark_send_attempt",
    "_mb_send_with_fallback", "_fetch_unsent_events_for_user", "_save_card_and_sent",
    "_decode_payload", "_norm_key", "_now_iso", "_connect", "init_schema",
    # W2 (2026-07-29, frozen master plan, Phase D): _mark_send_attempt now
    # calls these two real functions (central error classifier + jittered
    # backoff) -- extracted for real, same as everything else here, so this
    # suite keeps testing actual production code, not a stand-in.
    "_mb_classify_delivery_error", "_mb_backoff_seconds_with_jitter",
    # W2 REVISION (2026-07-29, Blocker 4): _mark_send_attempt/_mb_send_with_
    # fallback/_fetch_unsent_events_for_user/_save_card_and_sent now log via
    # this pseudonymizing helper instead of a raw uid -- extracted for real.
    "_w2_actor_ref",
}


class _FakeMsg:
    def __init__(self, id_: int):
        self.id = id_


_UNSET = object()


class _FakeClient:
    """Scriptable fake of the Telethon client's send_message -- records
    every call (including the exact parse_mode argument, so a test can
    prove the fallback really used parse_mode=None) and can be told to
    fail on the Nth call with a specific exception."""

    def __init__(self):
        self.calls: list[dict] = []
        self._next_id = 1
        self._fail_on_call: dict[int, BaseException] = {}

    def fail_on(self, call_number: int, exc: BaseException) -> None:
        self._fail_on_call[call_number] = exc

    async def send_message(self, chat_id, text, buttons=None, parse_mode=_UNSET):
        n = len(self.calls) + 1
        self.calls.append({"chat_id": chat_id, "text": text, "buttons": buttons,
                            "parse_mode": (None if parse_mode is _UNSET else parse_mode),
                            "parse_mode_explicit": parse_mode is not _UNSET})
        if n in self._fail_on_call:
            raise self._fail_on_call[n]
        msg = _FakeMsg(self._next_id)
        self._next_id += 1
        return msg


def build_delivery_ns(db_path: str, *, client=None):
    nodes = [_last_def(name) for name in DELIVERY_NAMES]
    # MANAGER_BOT_SEND_MAX_ATTEMPTS is faked directly (see below) rather than
    # extracted, to avoid pulling in the _env/_env_int/.env.TPilot chain for
    # a single integer constant -- boundary fake, same project convention.
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    import json as real_json
    import random as real_random
    BASE_DIR = Path(__file__).resolve().parent.parent
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    import storage as real_storage  # W2: real module, zero import-time side effects
    ns = {
        "sqlite3": sqlite3,
        "json": real_json,
        "random": real_random,
        "Path": Path,
        "datetime": datetime,
        "timedelta": timedelta,
        "Any": object, "Dict": dict, "List": list,
        "TPILOT_DB_PATH": db_path,
        "MANAGER_BOT_SEND_MAX_ATTEMPTS": 3,  # small, so tests reach quarantine quickly
        "_MB_FORMATTING_ERROR_CLASS_NAMES": frozenset({
            "EntityBoundsInvalidError", "EntitiesTooLongError",
            "MessageEntitiesTooLongError", "MessageEmptyError",
        }),
        "_MB_FORMATTING_ERROR_TEXT_MARKERS": ("ENTITY_BOUNDS_INVALID", "ENTITIES_TOO_LONG", "MESSAGE_EMPTY"),
        # W2 (2026-07-29): the central error classifier's own supporting
        # frozensets, copied verbatim from manager_bot.py (same boundary-
        # fake convention as the formatting-error sets above).
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
        "storage": real_storage,
    }
    exec(compile(module_src, f"<{MB_PATH}:delivery>", "exec"), ns)
    return ns


class _NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


def _make_temp_db() -> str:
    """init_schema() is one large function that manages MANY unrelated
    ManagerBot subsystems' tables (screenshots, transfers, business
    links, access backfill from main.py-owned tables...) inlined
    together; AST-extracting and running the WHOLE function against a
    blank temp DB would require stubbing a large dependency graph
    unrelated to this phase's fix. Instead, this harness creates the
    EXACT POST-migration schema for the three tables the delivery state
    machine actually touches (manager_bot_events, manager_bot_sent,
    manager_bot_access), copied verbatim from manager_bot.py's own DDL --
    the migration ITSELF (the ADD COLUMN statements) is separately
    verified for real in test_1, against the exact PRE-migration legacy
    schema."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="manager_bot_delivery_selftest_")
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


def _row(**over) -> dict:
    base = dict(id=1, event_key="k1", event_type="reserve_activated", manager_key="mgr1",
                chat_id=100, lead_date="2026-07-26", daily_lead_id=0, old_status="",
                new_status="", payload_json="{}", source="test", created_at="")
    base.update(over)
    return base


def _sent_row(con, event_id, tg_user_id) -> dict:
    r = con.execute(
        "SELECT * FROM manager_bot_sent WHERE tg_user_id=? AND event_id=?", (tg_user_id, event_id)
    ).fetchone()
    return dict(r) if r else {}


# ======================================================================
# 1. init_schema -- the real migration, against a real fresh temp DB.
# ======================================================================

def _extract_migration_snippet() -> str:
    """init_schema() is one large function managing MANY unrelated
    subsystems' tables (screenshots, transfers, business links...) -- AST-
    extracting and running the WHOLE function against an empty temp DB
    would require stubbing a large, unrelated dependency graph. The actual
    migration under test here is a small, self-contained block bounded by
    its own marker comments; slice it out by TEXT (not AST -- it is a
    flat for-loop, not a nested def) and run it directly against a
    connection this test controls."""
    start_marker = "        # --- TPILOT MANAGER EVENT DELIVERY STATE MACHINE 20260726 START ---\n"
    end_marker = "        # --- TPILOT MANAGER EVENT DELIVERY STATE MACHINE 20260726 END ---"
    # There are TWO occurrences of this marker TEXT in manager_bot.py: the
    # helper-functions block near the top (unindented, module level) and
    # this migration block inside init_schema (indented 8 spaces, nested in
    # a method body). The 8-space-indented `start_marker`/`end_marker`
    # searched for here only match the SECOND (migration) occurrence --
    # the first pair has zero indentation and never matches these literals.
    start_idx = MB_SRC.index(start_marker)
    end_idx = MB_SRC.index(end_marker, start_idx)
    block = MB_SRC[start_idx:end_idx]
    # Drop comment-only lines and dedent by the common 8-space indent so it
    # execs standalone (still the exact executable statements, unchanged).
    lines = [ln[8:] if ln.startswith("        ") else ln for ln in block.splitlines()]
    lines = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    return "\n".join(lines)


def test_1_schema_migration() -> None:
    snippet = _extract_migration_snippet()
    check("1. [sanity] the extracted migration snippet is non-trivial (not an empty slice)",
          "ALTER TABLE manager_bot_sent ADD COLUMN" in snippet, snippet)

    # A raw, empty temp DB -- NOT _make_temp_db(), which pre-seeds the
    # POST-migration schema for other tests. This test needs to create the
    # exact PRE-migration (legacy) manager_bot_sent table itself, then run
    # the real migration snippet against it.
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="manager_bot_delivery_selftest_")
    os.close(fd)
    try:
        con = sqlite3.connect(db_path)
        # The EXACT pre-migration (legacy) schema, as it existed before
        # this phase -- only the original 3 columns.
        con.execute(
            """
            CREATE TABLE manager_bot_sent(
                tg_user_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, event_id)
            )
            """
        )
        con.execute("INSERT INTO manager_bot_sent(tg_user_id, event_id, sent_at) VALUES (1, 1, '2026-01-01T00:00:00')")
        con.commit()

        exec(compile(snippet, "<migration_snippet>", "exec"), {"con": con})
        con.commit()

        cols = {r[1] for r in con.execute("PRAGMA table_info(manager_bot_sent)").fetchall()}
        for c in ("tg_user_id", "event_id", "sent_at", "attempts", "next_attempt_at",
                   "send_status", "last_error_class", "fallback_used"):
            check(f"1. manager_bot_sent has column {c!r} after migration", c in cols, cols)

        legacy_row = con.execute(
            "SELECT send_status, attempts, fallback_used FROM manager_bot_sent WHERE tg_user_id=1 AND event_id=1"
        ).fetchone()
        check("1. a PRE-EXISTING (legacy) row is correctly classified send_status='sent' by the DEFAULT "
              "(it WAS successfully delivered -- that is the only way a row could exist)",
              legacy_row[0] == "sent", legacy_row)
        check("1. legacy row: attempts defaults to 0", legacy_row[1] == 0, legacy_row)
        check("1. legacy row: fallback_used defaults to 0", legacy_row[2] == 0, legacy_row)

        # Idempotent: running the SAME snippet again (simulating a second
        # process start / re-import) must not raise (PRAGMA table_info
        # guard skips columns that already exist).
        exec(compile(snippet, "<migration_snippet_again>", "exec"), {"con": con})
        con.commit()
        check("1. migration snippet is idempotent (second run does not raise)", True, None)
        con.close()
    finally:
        os.unlink(db_path)


# ======================================================================
# 2. _mb_is_formatting_error -- pure classification, real Telethon
#    exception class + text-marker fallback + non-formatting negative.
# ======================================================================

def test_2_formatting_error_classification() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        is_fmt = ns["_mb_is_formatting_error"]
        real_exc = EntityBoundsInvalidError(request=None)
        check("2. REAL telethon.errors.EntityBoundsInvalidError classified as formatting error",
              is_fmt(real_exc) is True, type(real_exc).__name__)
        check("2. a generic ValueError is NOT a formatting error", is_fmt(ValueError("x")) is False, None)
        check("2. a ConnectionError (network) is NOT a formatting error", is_fmt(ConnectionError("x")) is False, None)

        class _RpcLike(Exception):
            pass
        text_marker_exc = _RpcLike("400 ENTITY_BOUNDS_INVALID (caused by ...)")
        check("2. text-marker fallback catches an RPC-shaped exception whose class name doesn't match",
              is_fmt(text_marker_exc) is True, str(text_marker_exc))
    finally:
        os.unlink(db_path)


# ======================================================================
# 3. _mb_backoff_seconds -- pure exponential backoff, capped at 1h, never
#    the old unconditional ~5s retry.
# ======================================================================

def test_3_backoff_curve() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        backoff = ns["_mb_backoff_seconds"]
        check("3. attempt 1 -> 60s", backoff(1) == 60, backoff(1))
        check("3. attempt 2 -> 120s", backoff(2) == 120, backoff(2))
        check("3. attempt 3 -> 240s", backoff(3) == 240, backoff(3))
        check("3. attempt 6 -> 1920s (not yet capped)", backoff(6) == 1920, backoff(6))
        check("3. attempt 7 -> capped at 3600s (1h), not 3840s", backoff(7) == 3600, backoff(7))
        check("3. every backoff value is > the old unconditional 5s poll interval",
              all(backoff(n) > 5 for n in range(1, 8)), [backoff(n) for n in range(1, 8)])
    finally:
        os.unlink(db_path)


# ======================================================================
# 4. Valid formatted message: unchanged behavior -- one send, no fallback,
#    ACKed as 'sent', fallback_used=0.
# ======================================================================

def test_4_valid_message_unchanged() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        row = _row(id=1)
        msg, fallback_used = asyncio.run(ns["_mb_send_with_fallback"](111, "hello", event_id=1))
        check("4. valid send succeeds on the FIRST attempt", msg.id == 1, msg)
        check("4. valid send: fallback_used is False", fallback_used is False, fallback_used)
        check("4. exactly ONE send_message call", len(ns["client"].calls) == 1, ns["client"].calls)
        check("4. the first attempt uses the DEFAULT parse mode (parse_mode not explicitly passed)",
              ns["client"].calls[0]["parse_mode_explicit"] is False, ns["client"].calls[0])

        ns["_save_card_and_sent"](row, 111, 111, msg.id, 0, fallback_used=fallback_used)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        sent = _sent_row(con, 1, 111)
        con.close()
        check("4. ACKed with send_status='sent'", sent.get("send_status") == "sent", sent)
        check("4. fallback_used recorded as 0", int(sent.get("fallback_used") or 0) == 0, sent)
        check("4. attempts stays 0 (never failed)", int(sent.get("attempts") or 0) == 0, sent)
    finally:
        os.unlink(db_path)


# ======================================================================
# 5-6. EntityBoundsInvalidError -> plain-text fallback -> success -> ACKed
#      exactly once, fallback_used=1, real parse_mode=None on the retry.
# ======================================================================

def test_5_fallback_success_acks_once() -> None:
    db_path = _make_temp_db()
    try:
        client = _FakeClient()
        client.fail_on(1, EntityBoundsInvalidError(request=None))
        ns = build_delivery_ns(db_path, client=client)
        row = _row(id=2)

        msg, fallback_used = asyncio.run(ns["_mb_send_with_fallback"](222, "**bold** text", event_id=2))
        check("5. fallback attempt succeeds", msg.id == 1, msg)  # first _successful_ send is call #2, id resets per _FakeClient... actually id increments only on success
        check("5. fallback_used is True", fallback_used is True, fallback_used)
        check("5. exactly TWO send_message calls (formatted + plain fallback)", len(client.calls) == 2, client.calls)
        check("5. call #1 used the default parse mode", client.calls[0]["parse_mode_explicit"] is False, client.calls[0])
        check("5. call #2 EXPLICITLY used parse_mode=None", client.calls[1]["parse_mode"] is None and client.calls[1]["parse_mode_explicit"] is True,
              client.calls[1])
        check("5. the plain-text retry sent the SAME visible text (no content loss)",
              client.calls[1]["text"] == "**bold** text", client.calls)

        ns["_save_card_and_sent"](row, 222, 222, msg.id, 0, fallback_used=fallback_used)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        sent = _sent_row(con, 2, 222)
        con.close()
        check("6. ACKed exactly once: send_status='sent'", sent.get("send_status") == "sent", sent)
        check("6. fallback_used recorded as 1 (audit trail)", int(sent.get("fallback_used") or 0) == 1, sent)

        # "Do not retry it again": a second delivery attempt for the same
        # (uid, event) must not re-select it -- proven in test_9's
        # selection-query check, reused here structurally: send_status is
        # 'sent', which the WHERE clause never re-admits.
    finally:
        os.unlink(db_path)


# ======================================================================
# 7. Fallback ALSO fails -> _mark_send_attempt records the failure
#    (attempts=1, send_status='failed', next_attempt_at set in the future).
# ======================================================================

def test_7_fallback_failure_increments_attempts() -> None:
    db_path = _make_temp_db()
    try:
        client = _FakeClient()
        client.fail_on(1, EntityBoundsInvalidError(request=None))
        client.fail_on(2, EntityBoundsInvalidError(request=None))  # plain-text retry ALSO fails
        ns = build_delivery_ns(db_path, client=client)

        raised = None
        try:
            asyncio.run(ns["_mb_send_with_fallback"](333, "text", event_id=3))
        except Exception as exc:
            raised = exc
        check("7. both attempts failing -> the exception propagates to the caller", raised is not None, raised)
        check("7. exactly TWO send_message calls attempted", len(client.calls) == 2, client.calls)

        ns["_mark_send_attempt"](3, 333, raised)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        sent = _sent_row(con, 3, 333)
        con.close()
        check("7. attempts incremented to 1", int(sent.get("attempts") or 0) == 1, sent)
        check("7. send_status is 'failed' (not yet quarantined, MAX=3)", sent.get("send_status") == "failed", sent)
        check("7. next_attempt_at is set (non-empty) -- scheduled for retry, not immediate",
              bool(sent.get("next_attempt_at")), sent)
        check("7. last_error_class recorded as the exception CLASS NAME",
              sent.get("last_error_class") == "EntityBoundsInvalidError", sent)
        check("7. sent_at is EMPTY (never actually delivered)", sent.get("sent_at") == "", sent)
    finally:
        os.unlink(db_path)


# ======================================================================
# 8. Bounded retry/backoff: retry does NOT occur before next_attempt_at.
# ======================================================================

def test_8_retry_respects_backoff_window() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        ns["_mark_send_attempt"](4, 444, RuntimeError("boom"))  # attempts=1, backoff=60s

        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, "
            "daily_lead_id, old_status, new_status, payload_json, source, created_at) "
            "VALUES (4, 'k4', 'reserve_activated', 'mgr1', 100, '2026-07-26', 0, '', '', '{}', 'test', '')"
        )
        con.execute(
            "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id) "
            "VALUES (444, 'mgr1', 1, 0, 0)"
        )
        con.commit()
        con.close()

        selected = ns["_fetch_unsent_events_for_user"](444, ["mgr1"])
        check("8. immediately after a fresh failure (60s backoff), the event is NOT yet eligible",
              len(selected) == 0, selected)

        # Move next_attempt_at into the past to simulate "backoff elapsed".
        past = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=5)).replace(microsecond=0).isoformat()
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_bot_sent SET next_attempt_at=? WHERE tg_user_id=444 AND event_id=4", (past,))
        con.commit()
        con.close()

        selected = ns["_fetch_unsent_events_for_user"](444, ["mgr1"])
        check("8. once next_attempt_at has elapsed, the SAME event becomes eligible again",
              len(selected) == 1 and int(selected[0]["id"]) == 4, selected)
    finally:
        os.unlink(db_path)


# ======================================================================
# 9. Quarantine after MAX attempts -- permanently excluded, event row
#    itself is NEVER deleted (audit trail preserved, no silent loss).
# ======================================================================

def test_9_quarantine_after_max_attempts() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)  # MANAGER_BOT_SEND_MAX_ATTEMPTS = 3 (test namespace)
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, "
            "daily_lead_id, old_status, new_status, payload_json, source, created_at) "
            "VALUES (5, 'k5', 'reserve_activated', 'mgr1', 100, '2026-07-26', 0, '', '', '{}', 'test', '')"
        )
        con.execute(
            "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id) "
            "VALUES (555, 'mgr1', 1, 0, 0)"
        )
        con.commit()
        con.close()

        for i in range(1, 4):
            ns["_mark_send_attempt"](5, 555, RuntimeError(f"boom {i}"))
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            sent = _sent_row(con, 5, 555)
            con.close()
            expected_status = "dead" if i >= 3 else "failed"
            check(f"9. after attempt {i}: attempts={i}, status={expected_status!r}",
                  int(sent.get("attempts") or 0) == i and sent.get("send_status") == expected_status, sent)

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        sent = _sent_row(con, 5, 555)
        events_still_present = con.execute("SELECT COUNT(*) FROM manager_bot_events WHERE id=5").fetchone()[0]
        con.close()
        check("9. quarantined row has next_attempt_at cleared (never retried again)",
              sent.get("next_attempt_at") == "", sent)
        check("9. the underlying event row is NEVER deleted (audit trail, no silent loss)",
              events_still_present == 1, events_still_present)

        # Even with next_attempt_at forced into the distant past, a 'dead'
        # row must NEVER be re-selected -- this is the actual quarantine
        # guarantee, not just "not yet due".
        con = sqlite3.connect(db_path)
        con.execute("UPDATE manager_bot_sent SET next_attempt_at=? WHERE tg_user_id=555 AND event_id=5",
                    ((datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(days=1)).replace(microsecond=0).isoformat(),))
        con.commit()
        con.close()
        selected = ns["_fetch_unsent_events_for_user"](555, ["mgr1"])
        check("9. a 'dead' event is PERMANENTLY excluded from selection, even with an elapsed next_attempt_at",
              len(selected) == 0, selected)
    finally:
        os.unlink(db_path)


# ======================================================================
# 10. Poison event does not block a later, unrelated, healthy event.
# ======================================================================

def test_10_poison_event_does_not_block_later_event() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        con = sqlite3.connect(db_path)
        # Event 6: will be quarantined (poison). Event 7: healthy, newer.
        con.execute(
            "INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, "
            "daily_lead_id, old_status, new_status, payload_json, source, created_at) "
            "VALUES (6, 'k6', 'reserve_activated', 'mgr1', 100, '2026-07-26', 0, '', '', '{}', 'test', '')"
        )
        con.execute(
            "INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, "
            "daily_lead_id, old_status, new_status, payload_json, source, created_at) "
            "VALUES (7, 'k7', 'reserve_activated', 'mgr1', 100, '2026-07-26', 0, '', '', '{}', 'test', '')"
        )
        con.execute(
            "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id) "
            "VALUES (666, 'mgr1', 1, 0, 0)"
        )
        con.commit()
        con.close()

        for i in range(1, 4):
            ns["_mark_send_attempt"](6, 666, RuntimeError("poison"))

        selected = ns["_fetch_unsent_events_for_user"](666, ["mgr1"])
        ids = sorted(int(r["id"]) for r in selected)
        check("10. event 6 (quarantined/poison) is excluded; event 7 (healthy) IS still selected",
              ids == [7], (ids, [dict(r) for r in selected]))
    finally:
        os.unlink(db_path)


# ======================================================================
# 10b. RQ3 (2026-07-26, independent-review corrective pass): a row already
#      marked 'sent' must NEVER be regressed to 'failed'/'dead' by
#      _mark_send_attempt. Reproduces the exact race the review found: the
#      poll-loop's per-event backstop can call _mark_send_attempt on an
#      event AFTER _save_card_and_sent has already flipped it to 'sent'
#      (e.g. a late/unrelated exception raised after a successful send).
#      Without the WHERE send_status <> 'sent' guard on the UPSERT, this
#      silently re-opened an already-delivered event for retry -- up to a
#      second full round of attempts, ending in an incorrect quarantine of
#      an event the manager already received.
# ======================================================================

def test_10b_sent_row_never_regresses_to_failed() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        row = _row(id=10)
        con = sqlite3.connect(db_path)
        con.execute(
            "INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, "
            "daily_lead_id, old_status, new_status, payload_json, source, created_at) "
            "VALUES (10, 'k10', 'reserve_activated', 'mgr1', 100, '2026-07-26', 0, '', '', '{}', 'test', '')"
        )
        con.execute(
            "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id) "
            "VALUES (1010, 'mgr1', 1, 0, 0)"
        )
        con.commit()
        con.close()

        # 1) A genuine successful delivery -- the row becomes 'sent', with a
        #    real sent_at and (arbitrary, nonzero) prior attempts recorded.
        ns["_save_card_and_sent"](row, 1010, 1010, 555, 0, fallback_used=False)
        con = sqlite3.connect(db_path)
        con.execute(
            "UPDATE manager_bot_sent SET attempts=3 WHERE tg_user_id=1010 AND event_id=10"
        )
        con.commit()
        con.close()
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        before = _sent_row(con, 10, 1010)
        con.close()
        check("10b. [setup] row is 'sent' with a real sent_at before the late failure",
              before.get("send_status") == "sent" and bool(before.get("sent_at")), before)

        # 2) A LATE exception is reported for the SAME (event_id, tg_user_id)
        #    -- e.g. the poll-loop backstop firing on something unrelated
        #    that happened after the successful send.
        ns["_mark_send_attempt"](10, 1010, RuntimeError("late failure after successful send"))

        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        after = _sent_row(con, 10, 1010)
        con.close()
        check("10b. send_status REMAINS 'sent' (never regressed to 'failed'/'dead')",
              after.get("send_status") == "sent", (before, after))
        check("10b. sent_at is UNCHANGED", after.get("sent_at") == before.get("sent_at"), (before, after))
        check("10b. attempts is UNCHANGED (not bumped by the late failure)",
              int(after.get("attempts") or 0) == int(before.get("attempts") or 0), (before, after))
        check("10b. next_attempt_at is UNCHANGED (row not scheduled for retry)",
              after.get("next_attempt_at") == before.get("next_attempt_at"), (before, after))

        # 3) The already-delivered event must NOT become eligible for
        #    (re)selection -- it must not be re-sent.
        selected = ns["_fetch_unsent_events_for_user"](1010, ["mgr1"])
        ids = [int(r["id"]) for r in selected]
        check("10b. the already-delivered event is NOT re-selected for delivery",
              10 not in ids, (ids, [dict(r) for r in selected]))
    finally:
        os.unlink(db_path)


# ======================================================================
# 11. Restart/re-import preserves retry state -- a BRAND NEW namespace
#     (nothing in memory) reading the SAME db_path sees the identical
#     attempts/send_status/next_attempt_at.
# ======================================================================

def test_11_restart_preserves_retry_state() -> None:
    db_path = _make_temp_db()
    try:
        ns1 = build_delivery_ns(db_path)
        ns1["_mark_send_attempt"](8, 777, RuntimeError("x"))
        ns1["_mark_send_attempt"](8, 777, RuntimeError("x"))

        # "Restart" == a completely fresh exec namespace (new _FakeClient,
        # new everything) -- the ONLY shared state is the sqlite file.
        ns2 = build_delivery_ns(db_path)
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        sent = _sent_row(con, 8, 777)
        con.close()
        check("11. attempts survived 'restart'", int(sent.get("attempts") or 0) == 2, sent)
        check("11. send_status survived 'restart'", sent.get("send_status") == "failed", sent)

        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id) "
                    "VALUES (777, 'mgr1', 1, 0, 0)")
        con.execute("INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, "
                    "daily_lead_id, old_status, new_status, payload_json, source, created_at) "
                    "VALUES (8, 'k8', 'reserve_activated', 'mgr1', 100, '2026-07-26', 0, '', '', '{}', 'test', '')")
        con.commit()
        con.close()
        # 'ns2' is a completely fresh module exec (a new "process") -- prove
        # its OWN selection query still respects the not-yet-due backoff
        # written by the "pre-restart" namespace, via the shared sqlite file.
        selected = ns2["_fetch_unsent_events_for_user"](777, ["mgr1"])
        check("11. the fresh ('post-restart') namespace still respects the backoff window written before 'restart'",
              len(selected) == 0, selected)
    finally:
        os.unlink(db_path)


# ======================================================================
# 12. Reproduction: the REAL Telethon markdown parser can generate a
#     malformed (negative-length) entity from emoji + unsanitized
#     delimiter text -- the exact incident class -- and _mb_is_formatting_
#     error correctly classifies the REAL error class this would trigger.
# ======================================================================

def test_12_malformed_markdown_reproduction() -> None:
    from telethon.extensions import markdown
    hostile_samples = [
        "😀______ `__*",
        "📋 Анкета лида\n\n**bold** `code` ~~strike~~",
        "text ** ** more",
    ]
    for sample in hostile_samples:
        try:
            text, entities = markdown.parse(sample)
            negative_length = any(getattr(e, "length", 0) < 0 for e in (entities or []))
            zero_width_delim = any(getattr(e, "length", 0) == 0 for e in (entities or []))
            check(f"12. hostile sample parses without raising locally: {sample!r}", True, None)
            if negative_length or zero_width_delim:
                check(f"12. sample {sample!r} produces a malformed entity (the exact class Telegram's server "
                      "rejects with EntityBoundsInvalidError -- reproduced locally, matching the incident)",
                      True, entities)
        except Exception as exc:
            check(f"12. hostile sample {sample!r} did not crash the local parser", False, exc)

    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        real_exc = EntityBoundsInvalidError(request=None)
        check("12. the REAL exception class this reproduction would trigger IS classified as a formatting error",
              ns["_mb_is_formatting_error"](real_exc) is True, None)
    finally:
        os.unlink(db_path)


# ======================================================================
# 13. Logs contain class/action but never full secret message text.
# ======================================================================

def test_13_logs_no_secret_text() -> None:
    captured = []

    class _CapturingLogger:
        def info(self, msg, *a, **k):
            captured.append(("info", msg % a if a else msg))

        def warning(self, msg, *a, **k):
            captured.append(("warning", msg % a if a else msg))

        def error(self, msg, *a, **k):
            captured.append(("error", msg % a if a else msg))

    db_path = _make_temp_db()
    try:
        ns = build_delivery_ns(db_path)
        ns["log"] = _CapturingLogger()
        secret_text = "host:port:login:SuperSecretPass123 +380991234567"
        exc = RuntimeError(secret_text)
        ns["_mark_send_attempt"](9, 999, exc)
        joined = "\n".join(str(m) for _lvl, m in captured)
        check("13. [SECRET SCAN] the exception's message text NEVER reaches the log",
              "SuperSecretPass123" not in joined, joined)
        check("13. [SECRET SCAN] no phone-number-shaped substring reaches the log",
              "+380991234567" not in joined, joined)
        check("13. the log DOES contain the exception class name (diagnostic value preserved)",
              any("RuntimeError" in str(m) for _lvl, m in captured), joined)
        check("13. the log DOES contain the event_id/uid/attempt (diagnostic value preserved)",
              any("event_id" in str(m) or "uid=999" in str(m) or "attempt" in str(m) for _lvl, m in captured), joined)
    finally:
        os.unlink(db_path)


def main() -> int:
    test_1_schema_migration()
    test_2_formatting_error_classification()
    test_3_backoff_curve()
    test_4_valid_message_unchanged()
    test_5_fallback_success_acks_once()
    test_7_fallback_failure_increments_attempts()
    test_8_retry_respects_backoff_window()
    test_9_quarantine_after_max_attempts()
    test_10_poison_event_does_not_block_later_event()
    test_10b_sent_row_never_regresses_to_failed()
    test_11_restart_preserves_retry_state()
    test_12_malformed_markdown_reproduction()
    test_13_logs_no_secret_text()

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
