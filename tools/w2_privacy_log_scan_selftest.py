# -*- coding: utf-8 -*-
"""tools/w2_privacy_log_scan_selftest.py -- offline selftest for the W2
REVISION Blocker 4 fix: no raw Telegram identifier (tg_user_id/uid/
chat_id/sender_id/peer_id) and no raw exception repr/text in any log call
reachable from the W2 delivery call graph.

Two layers:
  1. A reusable STATIC AST scanner (scan_w2_call_graph_for_raw_ids) that
     walks every named function and flags any `log.<level>(...)` call
     whose format string or arguments look like a raw-identifier leak.
     Run against the REAL manager_bot.py source -- this is the actual
     enforcement mechanism (fails the moment anyone reintroduces a
     `uid=%s`/`tg_user_id=%s`/`%r, exc` pattern in a W2-reachable
     function), not just a one-time confirmation.
  2. Runtime captured-log tests: run the REAL functions (AST-extracted,
     same convention as every other tools/*_selftest.py in this project)
     with an unmistakable synthetic tg_user_id and assert its decimal
     string form never appears in captured log output.
  3. Mutation proofs: apply a textual mutation that RESTORES a raw-id
     pattern and prove the scanner (and a captured-log run) correctly
     flags it -- proving the scanner is not vacuously passing.

Pure/offline: no network, no Telegram, no production DB, no spend.

    python tools\\w2_privacy_log_scan_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

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

W2_CALL_GRAPH_FUNCS = {
    "_mb_claim_event", "_mb_recover_stale_claims", "_mark_send_attempt", "_send_event_to_user",
    "_fetch_unsent_events_for_user", "_poll_loop", "_mb_write_terminal_state",
    "_find_manager_row_by_key", "_can_receive_cards", "_mb_send_with_fallback",
    "_mb_classify_delivery_error", "_mb_is_formatting_error", "_mb_backoff_seconds_with_jitter",
    "_upsert_card_placeholder", "_save_card_and_sent",
}

# Matches a raw-identifier-shaped format placeholder in a log format string,
# or a bare exception repr (%r on an exception variable), or the literal
# text "tg_user_id=" / "uid=" / "chat_id=" / "sender_id=" / "peer_id=".
RAW_ID_FORMAT_RX = re.compile(r"\b(uid|tg_user_id|chat_id|sender_id|peer_id|recipient_id)\s*=\s*%[sd]")
RAW_EXC_REPR_RX = re.compile(r"%r")
RAW_ID_ARG_NAMES = {"tg_user_id", "uid", "chat_id", "sender_id", "peer_id", "recipient_id"}


def scan_w2_call_graph_for_raw_ids(src: str, func_names: set) -> list:
    """Returns a list of (func_name, lineno, reason) violations. This is
    the REAL enforcement logic (not a demo) -- called against the actual
    source below, and against mutated text in the mutation-proof tests."""
    tree = ast.parse(src)
    top_level = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in func_names:
            top_level[n.name] = n

    violations = []
    for name, node in top_level.items():
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            f = sub.func
            if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "log"):
                continue
            # Format-string literal check.
            if sub.args and isinstance(sub.args[0], ast.Constant) and isinstance(sub.args[0].value, str):
                fmt = sub.args[0].value
                if RAW_ID_FORMAT_RX.search(fmt):
                    violations.append((name, sub.lineno, f"raw-id-shaped format placeholder: {fmt!r}"))
                if RAW_EXC_REPR_RX.search(fmt):
                    violations.append((name, sub.lineno, f"%r in format string (raw exception repr risk): {fmt!r}"))
            # Raw-name-argument check (a bare `tg_user_id`/`uid`/`chat_id` passed positionally).
            for arg in sub.args[1:]:
                if isinstance(arg, ast.Name) and arg.id in RAW_ID_ARG_NAMES:
                    violations.append((name, sub.lineno, f"raw identifier argument passed directly: {arg.id}"))
    return violations


# ======================================================================
# 1. Static scan against the REAL source -- the actual enforcement.
# ======================================================================

def test_static_scan_real_source_is_clean() -> None:
    violations = scan_w2_call_graph_for_raw_ids(MB_SRC, W2_CALL_GRAPH_FUNCS)
    check(f"static scan of the REAL manager_bot.py W2 call graph ({len(W2_CALL_GRAPH_FUNCS)} functions) "
          f"finds ZERO raw-identifier / raw-exception-repr log calls",
          violations == [], violations)


# ======================================================================
# 2. Mutation proof: the scanner correctly FLAGS a reintroduced pattern.
# ======================================================================

def test_mutation_proof_scanner_catches_reintroduced_uid() -> None:
    mutant_src = MB_SRC.replace(
        'log.warning(\n            "send event failed actor_ref=%s event_id=%s attempt=%s error_class=%s category=%s action=%s",\n'
        "            _w2_actor_ref(tg_user_id), event_id, attempts, err_cls, classification[\"category\"], status,\n        )",
        'log.warning(\n            "send event failed uid=%s event_id=%s attempt=%s error_class=%s category=%s action=%s",\n'
        "            tg_user_id, event_id, attempts, err_cls, classification[\"category\"], status,\n        )",
        1,
    )
    check("[mutation precondition] the mutation actually changed the source",
          mutant_src != MB_SRC, None)
    mutant_violations = scan_w2_call_graph_for_raw_ids(mutant_src, W2_CALL_GRAPH_FUNCS)
    check("[mutation proof] reintroducing 'uid=%s' + a raw tg_user_id argument in "
          "_mark_send_attempt IS caught by the scanner",
          any(v[0] == "_mark_send_attempt" for v in mutant_violations), mutant_violations)

    mutant_src_2 = MB_SRC.replace(
        'log.warning("poll loop error: error_class=%s", type(exc).__name__)',
        'log.warning("poll loop error: %r", exc)',
        1,
    )
    check("[mutation precondition 2] this mutation also changed the source", mutant_src_2 != MB_SRC, None)
    mutant_violations_2 = scan_w2_call_graph_for_raw_ids(mutant_src_2, W2_CALL_GRAPH_FUNCS)
    check("[mutation proof] reintroducing '%r' (raw exception repr) in _poll_loop IS caught by the scanner",
          any(v[0] == "_poll_loop" and "%r" in v[2] for v in mutant_violations_2), mutant_violations_2)

    mutant_src_3 = MB_SRC.replace(
        'log.warning("card placeholder failed actor_ref=%s error_class=%s",\n'
        "                    _w2_actor_ref(tg_user_id), type(exc).__name__)",
        'log.warning("card placeholder failed uid=%s error=%r", tg_user_id, exc)',
        1,
    )
    check("[mutation precondition 3] this mutation also changed the source", mutant_src_3 != MB_SRC, None)
    mutant_violations_3 = scan_w2_call_graph_for_raw_ids(mutant_src_3, W2_CALL_GRAPH_FUNCS)
    check("[mutation proof] restoring the ORIGINAL pre-revision 'uid=%s error=%r' pattern in "
          "_upsert_card_placeholder IS caught (both the raw-id AND the %r violations)",
          any(v[0] == "_upsert_card_placeholder" for v in mutant_violations_3)
          and len([v for v in mutant_violations_3 if v[0] == "_upsert_card_placeholder"]) >= 2,
          mutant_violations_3)


# ======================================================================
# 3. Runtime captured-log tests: an unmistakable synthetic uid never
# reaches the log in decimal form, across several real call paths.
# ======================================================================

def _last_def(name: str):
    node = None
    for n in MB_TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
    if node is None:
        raise AssertionError(f"no def {name} found")
    return node


class _CapturingLogger:
    def __init__(self):
        self.lines: list[str] = []

    def _fmt(self, msg, args):
        try:
            return msg % args if args else msg
        except Exception:
            return f"{msg} {args}"

    def info(self, msg, *a, **k): self.lines.append(self._fmt(msg, a))
    def warning(self, msg, *a, **k): self.lines.append(self._fmt(msg, a))
    def error(self, msg, *a, **k): self.lines.append(self._fmt(msg, a))


SYNTHETIC_UID = 88877766655  # unmistakable, never a plausible real Telegram id by coincidence


def build_ns(db_path: str, names: set, logger=None):
    nodes = [_last_def(n) for n in names]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    import json as real_json
    import random as real_random
    import uuid as real_uuid
    ns = {
        "sqlite3": sqlite3, "json": real_json, "random": real_random, "uuid": real_uuid,
        "Path": Path, "datetime": datetime, "timedelta": timedelta,
        "Any": object, "Dict": dict, "List": list,
        "TPILOT_DB_PATH": db_path,
        "MANAGER_BOT_SEND_MAX_ATTEMPTS": 3,
        "MANAGER_BOT_CLAIM_LEASE_SECONDS": 120,
        "_MB_WORKER_REF": "pidTEST-aaaaaaaa",
        "_MB_TERMINAL_SEND_STATUSES": frozenset({
            "terminal_access_denied", "terminal_manager_deleted", "terminal_invalid_event", "cutoff_suppressed",
        }),
        "_MB_FORMATTING_ERROR_CLASS_NAMES": frozenset({
            "EntityBoundsInvalidError", "EntitiesTooLongError", "MessageEntitiesTooLongError", "MessageEmptyError",
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
        "log": logger or _CapturingLogger(),
        "storage": storage,
    }
    exec(compile(module_src, f"<{MB_PATH}:w2privacy>", "exec"), ns)
    return ns


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="w2_privacy_scan_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE manager_bot_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '', manager_key TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0, lead_date TEXT NOT NULL DEFAULT '',
                daily_lead_id INTEGER NOT NULL DEFAULT 0, old_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE manager_bot_sent(
                tg_user_id INTEGER NOT NULL, event_id INTEGER NOT NULL, sent_at TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL DEFAULT '',
                send_status TEXT NOT NULL DEFAULT 'sent', last_error_class TEXT NOT NULL DEFAULT '',
                fallback_used INTEGER NOT NULL DEFAULT 0, claimed_at TEXT NOT NULL DEFAULT '',
                lease_token TEXT NOT NULL DEFAULT '', lease_expires_at TEXT NOT NULL DEFAULT '',
                claim_worker_ref TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, event_id)
            );
            CREATE TABLE manager_bot_access(
                tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL DEFAULT '',
                can_receive_cards INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
                event_cutoff_id INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (tg_user_id, manager_key)
            );
            CREATE TABLE access_users(
                tg_user_id INTEGER PRIMARY KEY, scope_mode TEXT NOT NULL DEFAULT 'selected', is_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE access_targets(
                tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, manager_key)
            );
            CREATE TABLE managers(
                manager_key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active', is_enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def test_runtime_mark_send_attempt_never_logs_raw_uid() -> None:
    db = _make_temp_db()
    try:
        logger = _CapturingLogger()
        names = {"_mark_send_attempt", "_mb_classify_delivery_error", "_mb_backoff_seconds",
                  "_mb_backoff_seconds_with_jitter", "_mb_is_formatting_error", "_now_iso", "_connect",
                  "_w2_actor_ref"}
        ns = build_ns(db, names, logger=logger)
        secret_exc = Exception(f"whatever internal detail, uid={SYNTHETIC_UID}, phone=+380991234567")
        ns["_mark_send_attempt"](42, SYNTHETIC_UID, secret_exc)
        joined = "\n".join(logger.lines)
        check(f"_mark_send_attempt: synthetic uid {SYNTHETIC_UID} never appears in captured log output",
              str(SYNTHETIC_UID) not in joined, joined)
        check("_mark_send_attempt: the exception's own message text never reaches the log either",
              "+380991234567" not in joined, joined)
    finally:
        os.unlink(db)


def test_runtime_send_event_to_user_never_logs_raw_uid() -> None:
    db = _make_temp_db()
    try:
        logger = _CapturingLogger()
        names = {
            "_send_event_to_user", "_mb_write_terminal_state", "_find_manager_row_by_key",
            "_can_receive_cards", "_mb_claim_event", "_mb_send_with_fallback", "_mark_send_attempt",
            "_save_card_and_sent", "_decode_payload", "_norm_key", "_now_iso", "_connect",
            "_mb_classify_delivery_error", "_mb_backoff_seconds", "_mb_backoff_seconds_with_jitter",
            "_mb_is_formatting_error", "_w2_actor_ref",
        }
        ns = build_ns(db, names, logger=logger)

        class _FakeMsg:
            id = 1

        class _FakeClient:
            async def send_message(self, chat_id, text, buttons=None, parse_mode=None):
                return _FakeMsg()

        ns["client"] = _FakeClient()

        # Scenario A: invalid event.
        asyncio.run(ns["_send_event_to_user"]({"id": 1, "manager_key": "", "chat_id": 0, "payload_json": "{}"}, SYNTHETIC_UID))
        # Scenario B: deleted manager.
        con = sqlite3.connect(db)
        con.execute("INSERT INTO manager_bot_events(id, event_key, event_type, manager_key, chat_id, lead_date, created_at) "
                     "VALUES (2,'k2','reserve_activated','mgr_gone',100,'2026-07-29','')")
        con.commit()
        con.close()
        asyncio.run(ns["_send_event_to_user"](
            {"id": 2, "manager_key": "mgr_gone", "chat_id": 100, "event_type": "reserve_activated", "payload_json": "{}"},
            SYNTHETIC_UID))
        # Scenario C: access denied.
        con = sqlite3.connect(db)
        con.execute("INSERT INTO managers(manager_key, status, is_enabled) VALUES ('mgr_ok','active',1)")
        con.commit()
        con.close()
        asyncio.run(ns["_send_event_to_user"](
            {"id": 3, "manager_key": "mgr_ok", "chat_id": 100, "event_type": "reserve_activated", "payload_json": "{}"},
            SYNTHETIC_UID))
        # Scenario D: successful send.
        con = sqlite3.connect(db)
        con.execute("INSERT INTO access_users(tg_user_id, is_enabled) VALUES (?,1)", (SYNTHETIC_UID,))
        con.execute("INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked) VALUES (?,'mgr_ok',1,0)", (SYNTHETIC_UID,))
        con.commit()
        con.close()
        asyncio.run(ns["_send_event_to_user"](
            {"id": 3, "manager_key": "mgr_ok", "chat_id": 100, "event_type": "reserve_activated", "payload_json": "{}"},
            SYNTHETIC_UID))

        joined = "\n".join(logger.lines)
        check(f"_send_event_to_user (4 scenarios: invalid/deleted-manager/access-denied/success): "
              f"synthetic uid {SYNTHETIC_UID} never appears in captured log output",
              str(SYNTHETIC_UID) not in joined, joined)
        check("at least one log line was actually captured (the test exercises real log calls, not a no-op)",
              len(logger.lines) > 0, logger.lines)
    finally:
        os.unlink(db)


def main() -> int:
    test_static_scan_real_source_is_clean()
    test_mutation_proof_scanner_catches_reintroduced_uid()
    test_runtime_mark_send_attempt_never_logs_raw_uid()
    test_runtime_send_event_to_user_never_logs_raw_uid()

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
