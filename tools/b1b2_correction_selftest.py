# -*- coding: utf-8 -*-
"""tools/b1b2_correction_selftest.py -- offline regression tests for the
correction closing independent-review Blockers B-1 and B-2 from the R1A2+R1B
large reliability batch (2026-08-13). Review report:
C:\\Users\\PROFESSOR\\.claude\\plans\\model-opus-5-mode-glittery-teacup.md

B-1 (manager_bot.py): the active lead-card sender
(_send_event_to_user def@ ~4579, delegated to from the claim-taking outer
def@ ~5151 via _M26C_ORIG_SEND_EVENT_TO_USER) used to send a Telegram card
with buttons=None and mark the event 'sent' even when
_upsert_card_placeholder failed (card_id<=0) -- the card became permanently
un-actionable (no status buttons, no message_id, 'sent' is terminal and
never revisited). Fixed: card_id<=0 now aborts BEFORE any Telegram send and
routes the claim through the existing _mark_send_attempt bounded
retry/backoff/quarantine machinery instead of a new state.

B-2 (panel_bot.py): _tp_panel_v5_remove_auth (the single canonical revoke
path, /logout's only caller) deleted the DB row but never invalidated
_TP_PANEL_V5_AUTH_CACHE (F-15, 30s TTL) -- a revoked user stayed cached
authorized for up to 30s while the UI claimed "you're logged out". Fixed:
the cache entry is popped unconditionally inside the canonical revoke
helper.

Both files have import-time side effects (Telethon client construction /
env checks), so every function under test is extracted from the REAL source
via ast.parse+unparse+exec (this project's own convention -- see
tools/manager_bot_delivery_selftest.py, tools/r1a1_correctness_selftest.py).

R1B_CORR_REDCHECK_MANAGER_BOT_PATH / R1B_CORR_REDCHECK_PANEL_BOT_PATH:
optional env overrides to re-run this suite against the reconstructed
pre-correction backups (*.bak_b1b2fix_20260813_010228) as a one-off
RED-before-fix proof. Normal runs never set these.

Never: real Telegram network, real filesystem writes outside a temp dir,
production DB/runtime/session/log access.

    python tools\\b1b2_correction_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import json
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

import storage  # noqa: E402 -- zero import-time side effects, project convention

FAILURES: List[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _last_def(tree, name: str):
    node = None
    for n in tree.body:
        target = None
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            target = n.name
        elif isinstance(n, ast.ClassDef):
            target = n.name
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            target = n.targets[0].id
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            target = n.target.id
        if target == name:
            node = n
    if node is None:
        raise AssertionError(f"no def/class/assign {name} found")
    return node


class _NullLog:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass


# ======================================================================
# PART A -- B-1: manager_bot.py placeholder-must-block-send
# ======================================================================

MB_PATH = os.environ.get("R1B_CORR_REDCHECK_MANAGER_BOT_PATH") or str(BASE_DIR / "manager_bot.py")
MB_SRC = open(MB_PATH, encoding="utf-8-sig").read()
MB_TREE = ast.parse(MB_SRC)

SEND_NAMES = {
    "_send_event_to_user", "_upsert_card_placeholder", "_mark_send_attempt",
    "_save_card_and_sent", "_decode_payload", "_norm_key", "_now_iso", "_w2_actor_ref",
    "_mb_classify_delivery_error", "_mb_backoff_seconds_with_jitter", "_mb_backoff_seconds",
    "_mb_is_formatting_error",
    "_MB_TERMINAL_DEACTIVATED_CLASS_NAMES", "_MB_TERMINAL_MISSING_PEER_CLASS_NAMES",
    "_MB_TRANSIENT_FLOODWAIT_CLASS_NAMES", "_MB_TRANSIENT_NETWORK_CLASS_NAMES",
    "_MB_TRANSIENT_DB_MARKERS",
    "_MB_FORMATTING_ERROR_CLASS_NAMES", "_MB_FORMATTING_ERROR_TEXT_MARKERS",
}
# Only present in the FIXED source -- the RED-before run (against the
# reconstructed pre-correction backup) legitimately doesn't have it, which
# is itself part of the RED proof (see build_send_ns's try/except below).
NEW_ONLY_NAMES = {"_MbPlaceholderUnavailable"}


class _FakeMsg:
    def __init__(self, id_: int): self.id = id_


class _FakeSendClient:
    """Records every _mb_send_with_fallback call -- this is the Telegram
    send-count oracle for PH-T2/PH-T5/PH-T7. fail_first schedules an
    exception on a specific call number (1-indexed)."""
    def __init__(self):
        self.calls: List[dict] = []
        self._next_id = 1
        self.fail_first: Dict[int, BaseException] = {}

    async def send_with_fallback(self, tg_user_id, text, *, event_id=None, buttons=None):
        n = len(self.calls) + 1
        self.calls.append({"tg_user_id": tg_user_id, "text": text, "event_id": event_id, "buttons": buttons})
        if n in self.fail_first:
            raise self.fail_first[n]
        msg = _FakeMsg(self._next_id); self._next_id += 1
        return msg, False


def _send_event_to_user_card_body(tree):
    """`_send_event_to_user` is stacked 3x in manager_bot.py (dead def, the
    active card-sending body, and a claim-taking OUTER wrapper). This
    project's "last definition wins" convention identifies the OUTER
    wrapper (highest line number) -- but that wrapper does not itself send
    a lead card; for the default event type it delegates to
    `_M26C_ORIG_SEND_EVENT_TO_USER`, captured via
    `_M26C_ORIG_SEND_EVENT_TO_USER = globals().get("_send_event_to_user")`
    BEFORE the outer wrapper is (re)defined. Python name-lookup semantics
    mean that capture resolves to whichever `_send_event_to_user` def is
    most recent AT THE POINT of that assignment -- i.e. the correct
    "active" body for B-1's purposes is the one with the highest line
    number that is still BEFORE the capture assignment (this also
    correctly excludes the older, truly-dead 2107 def, which the capture
    assignment already superseded long before it ran)."""
    capture_line = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id == "_M26C_ORIG_SEND_EVENT_TO_USER"):
            capture_line = n.lineno
    if capture_line is None:
        raise AssertionError("could not find _M26C_ORIG_SEND_EVENT_TO_USER capture assignment")
    candidates = [
        n for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_send_event_to_user"
        and n.lineno < capture_line
    ]
    if not candidates:
        raise AssertionError("no _send_event_to_user def found before the capture assignment")
    chosen = max(candidates, key=lambda n: n.lineno)
    has_placeholder_call = any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "_upsert_card_placeholder"
        for c in ast.walk(chosen)
    )
    if not has_placeholder_call:
        raise AssertionError("chosen _send_event_to_user body does not call _upsert_card_placeholder -- wrong def picked")
    return chosen


def build_send_ns(db_path: str, *, placeholder_fn, send_client: _FakeSendClient, max_attempts: int = 3):
    # _upsert_card_placeholder and _mb_send_with_fallback are deliberately
    # NOT extracted -- they're boundary fakes (placeholder_fn / send_client)
    # so this test controls exactly when placeholder setup "fails" and
    # counts real Telegram sends. Extracting the real defs here would
    # silently clobber the fakes in `ns` below (exec binds the last
    # definition of a name it sees, same override-stacking rule as the rest
    # of this project).
    names = set(SEND_NAMES) - {"_send_event_to_user", "_upsert_card_placeholder", "_mb_send_with_fallback"}
    try:
        _last_def(MB_TREE, "_MbPlaceholderUnavailable")
        names |= NEW_ONLY_NAMES
        has_fix = True
    except AssertionError:
        has_fix = False
    nodes = [_last_def(MB_TREE, n) for n in names]
    nodes.append(_send_event_to_user_card_body(MB_TREE))
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    def fake_read_override(manager_key, chat_id):
        return {}

    def fake_format_lead_card(event_row, override=None):
        return "CARD TEXT"

    def fake_with_manual_marker(text, flag):
        return text

    def fake_buttons_for_manual_state(card_id, has_override, *, expanded, manager_key, chat_id, lead_date, tg_user_id):
        return [["BTN", card_id]]

    ns: Dict[str, Any] = {
        "sqlite3": sqlite3, "json": json, "random": real_random, "time": real_time,
        "datetime": datetime, "timedelta": timedelta,
        "Any": object, "Dict": dict, "List": list,
        "TPILOT_DB_PATH": db_path,
        "MANAGER_BOT_SEND_MAX_ATTEMPTS": max_attempts,
        "log": _NullLog(),
        "storage": storage,
        "_connect": lambda: _connect_row(db_path),
        "_upsert_card_placeholder": placeholder_fn,
        "_read_override": fake_read_override,
        "_format_lead_card": fake_format_lead_card,
        "_with_manual_marker": fake_with_manual_marker,
        "_buttons_for_manual_state": fake_buttons_for_manual_state,
        "_mb_send_with_fallback": send_client.send_with_fallback,
    }
    exec(compile(module_src, f"<{MB_PATH}:b1fix>", "exec"), ns)
    if not has_fix:
        ns["_MbPlaceholderUnavailable"] = type("_MbPlaceholderUnavailable", (Exception,), {})
    return ns


def _connect_row(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


MB_DDL = """
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


def _mb_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="b1b2_correction_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(MB_DDL)
        con.commit()
    finally:
        con.close()
    return path


def _safe_unlink(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except Exception:
            pass


def _mb_row(db_path: str, uid: int, event_id: int):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute("SELECT * FROM manager_bot_sent WHERE tg_user_id=? AND event_id=?", (uid, event_id)).fetchone()
    finally:
        con.close()


def _claim(db_path: str, uid: int, event_id: int, lease_expires_at: str = "2099-01-01T00:00:00") -> None:
    """Mirrors what _mb_claim_event (untouched, out of scope for this
    correction) already does before _send_event_to_user runs: an
    INSERT establishing send_status='sending' with a lease."""
    con = sqlite3.connect(db_path)
    con.execute(
        "INSERT INTO manager_bot_sent(tg_user_id, event_id, send_status, lease_expires_at, claim_worker_ref) "
        "VALUES (?,?,?,?,?)",
        (uid, event_id, "sending", lease_expires_at, "w1"),
    )
    con.commit(); con.close()


def _resend_oracle(db_path: str, uid: int, event_id: int, now: str = "2099-06-01T00:00:00"):
    """The REAL WHERE-clause shapes of the two resend paths
    (_fetch_unsent_events_for_user, _mb_recover_stale_claims), reproduced
    here as an independent oracle -- would this row be re-selected /
    re-claimed as stale?"""
    con = sqlite3.connect(db_path)
    fetch = con.execute(
        "SELECT 1 FROM manager_bot_sent s WHERE s.tg_user_id=? AND s.event_id=? "
        "AND (s.send_status='failed' AND (s.next_attempt_at='' OR s.next_attempt_at<=?))",
        (uid, event_id, now),
    ).fetchone()
    recov = con.execute(
        "SELECT 1 FROM manager_bot_sent WHERE tg_user_id=? AND event_id=? "
        "AND send_status='sending' AND lease_expires_at<>'' AND lease_expires_at<=?",
        (uid, event_id, now),
    ).fetchone()
    con.close()
    return bool(fetch), bool(recov)


def run(coro):
    return asyncio.run(coro)


EVENT_ROW = lambda eid: {"id": eid, "chat_id": 5, "manager_key": "m1", "lead_date": "2026-08-12"}


def test_ph_placeholder_failure_blocks_send():
    print("\n-- PH-T2/T3/T4/T8/T9: placeholder failure -> zero sends, no terminal state --")
    db = _mb_temp_db()
    try:
        _claim(db, 1, 100)
        client = _FakeSendClient()
        ns = build_send_ns(db, placeholder_fn=lambda row, uid: 0, send_client=client)
        run(ns["_send_event_to_user"](EVENT_ROW(100), 1))

        check("PH-T2. Telegram send call count == 0 when placeholder fails",
              len(client.calls) == 0, len(client.calls))
        row = _mb_row(db, 1, 100)
        check("PH-T3. event is NOT marked 'sent'",
              row is not None and row["send_status"] != "sent", dict(row) if row else None)
        check("PH-T8. placeholder failure never produces 'sent_unpersisted'",
              row is not None and row["send_status"] != "sent_unpersisted", dict(row) if row else None)
        check("PH-T9. no 'successful delivery' marker consumed (sent_at stays empty)",
              row is not None and (row["sent_at"] or "") == "", dict(row) if row else None)

        f, rec = _resend_oracle(db, 1, 100)
        check("PH-T4. event remains retryable (selectable via bounded retry OR stale-claim recovery, "
              "not permanently stuck in a dead-end)", f or rec, (f, rec))

        cards = sqlite3.connect(db).execute("SELECT COUNT(*) FROM manager_lead_cards").fetchone()[0]
        check("(placeholder failure) no manager_lead_cards row was fabricated", cards == 0, cards)
    finally:
        _safe_unlink(db)


def test_ph_recovery_sends_exactly_once():
    print("\n-- PH-T1/T5/T6/T7: recovery after DB restore -> exactly one send, full state --")
    db = _mb_temp_db()
    try:
        _claim(db, 2, 200)
        client = _FakeSendClient()

        # attempt 1: placeholder still failing
        ns1 = build_send_ns(db, placeholder_fn=lambda row, uid: 0, send_client=client)
        run(ns1["_send_event_to_user"](EVENT_ROW(200), 2))
        check("pre-check: attempt 1 (still locked) sent nothing", len(client.calls) == 0)

        # "restart" / DB recovers -- placeholder now succeeds
        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO manager_lead_cards(tg_user_id, chat_id, manager_key, lead_date) VALUES (2,5,'m1','2026-08-12')")
        cid = con.execute("SELECT id FROM manager_lead_cards WHERE tg_user_id=2").fetchone()[0]
        con.commit(); con.close()

        ns2 = build_send_ns(db, placeholder_fn=lambda row, uid: cid, send_client=client)
        run(ns2["_send_event_to_user"](EVENT_ROW(200), 2))

        check("PH-T1/T5. exactly ONE Telegram send after recovery", len(client.calls) == 1, client.calls)
        check("PH-T5b. the send used real buttons (not None -- card is actionable)",
              client.calls[0]["buttons"] is not None, client.calls[0])
        row = _mb_row(db, 2, 200)
        check("PH-T6. state is 'sent' with message_id-bearing send recorded",
              row is not None and row["send_status"] == "sent", dict(row) if row else None)

        card_row = sqlite3.connect(db).execute(
            "SELECT message_id FROM manager_lead_cards WHERE tg_user_id=2 AND id=?", (cid,)).fetchone()
        check("PH-T6b. manager_lead_cards.message_id populated (card identity preserved)",
              card_row is not None and card_row[0] != 0, card_row)

        # 10 more poll cycles must not re-send
        for _ in range(10):
            f, rec = _resend_oracle(db, 2, 200)
            assert not f and not rec
        check("PH-T7. 10 subsequent poll cycles -> zero additional sends (oracle never re-selects)",
              True)
    finally:
        _safe_unlink(db)


def test_ph_no_duplicate_regression():
    print("\n-- PH: attempt1 (locked) then attempt2 (recovered) never double-sends --")
    db = _mb_temp_db()
    try:
        _claim(db, 3, 300)
        client = _FakeSendClient()
        ns_fail = build_send_ns(db, placeholder_fn=lambda row, uid: 0, send_client=client)
        run(ns_fail["_send_event_to_user"](EVENT_ROW(300), 3))
        run(ns_fail["_send_event_to_user"](EVENT_ROW(300), 3))  # a second failed attempt, still locked
        check("2 consecutive placeholder failures -> still zero sends", len(client.calls) == 0, len(client.calls))

        con = sqlite3.connect(db)
        con.execute(
            "INSERT INTO manager_lead_cards(tg_user_id, chat_id, manager_key, lead_date) VALUES (3,5,'m1','2026-08-12')")
        cid = con.execute("SELECT id FROM manager_lead_cards WHERE tg_user_id=3").fetchone()[0]
        con.commit(); con.close()
        ns_ok = build_send_ns(db, placeholder_fn=lambda row, uid: cid, send_client=client)
        run(ns_ok["_send_event_to_user"](EVENT_ROW(300), 3))
        check("recovery -> exactly ONE send total across the whole sequence",
              len(client.calls) == 1, client.calls)
    finally:
        _safe_unlink(db)


def test_ph_special_card_flows_unaffected():
    print("\n-- PH-T10: duplicate_card/reserve/transfer flows (card_id=0 by design) unaffected --")
    # These flows never call _upsert_card_placeholder at all (per the
    # source, they pass card_id=0 to _save_card_and_sent directly as an
    # intentional "no manual-override state" design, not a failure). The
    # B-1 fix only touches the ONE call site inside _send_event_to_user's
    # default lead_card body -- confirmed statically: _upsert_card_placeholder
    # appears exactly once as a live call in the whole file (the other
    # definition-site match is the def itself, and one dead stacked def).
    # OVERRIDE CLEANUP 20260815: the dead stacked _send_event_to_user def
    # (which held the second, unreachable call site) was removed, so the
    # expected count dropped 2 -> 1: only the ACTIVE call site remains.
    hits = [i for i, line in enumerate(MB_SRC.splitlines(), 1) if "_upsert_card_placeholder(" in line]
    call_sites = [i for i in hits if "def _upsert_card_placeholder" not in MB_SRC.splitlines()[i - 1]]
    check("PH-T10. _upsert_card_placeholder has exactly 1 call site in the whole file "
          "(the active _send_event_to_user body) -- duplicate_card/reserve/"
          "transfer paths never call it, so B-1's guard cannot affect them",
          len(call_sites) == 1, call_sites)


def test_ph_red_before():
    """Only meaningful when re-invoked with R1B_CORR_REDCHECK_MANAGER_BOT_PATH
    pointed at the reconstructed pre-correction backup."""
    if not os.environ.get("R1B_CORR_REDCHECK_MANAGER_BOT_PATH"):
        return
    print("\n-- PH RED-before: placeholder failure on PRE-CORRECTION code --")
    db = _mb_temp_db()
    try:
        _claim(db, 9, 900)
        client = _FakeSendClient()
        ns = build_send_ns(db, placeholder_fn=lambda row, uid: 0, send_client=client)
        run(ns["_send_event_to_user"](EVENT_ROW(900), 9))
        check("RED: pre-correction code STILL sends with buttons=None on placeholder failure "
              "(this must be RED/true-old-bug here, proving the test exercises the real bug)",
              len(client.calls) == 1 and client.calls[0]["buttons"] is None, client.calls)
        row = _mb_row(db, 9, 900)
        check("RED: pre-correction code marks the degraded card 'sent' (unrepairable)",
              row is not None and row["send_status"] == "sent", dict(row) if row else None)
    finally:
        _safe_unlink(db)


# ======================================================================
# PART B -- B-2: panel_bot.py auth cache invalidation
# ======================================================================

PB_PATH = os.environ.get("R1B_CORR_REDCHECK_PANEL_BOT_PATH") or str(BASE_DIR / "panel_bot.py")
PB_SRC = open(PB_PATH, encoding="utf-8-sig").read()
PB_TREE = ast.parse(PB_SRC)

AUTH_NAMES = {
    "_tp_panel_v5_ensure_auth_table", "_tp_panel_v5_authorized", "_tp_panel_v5_remove_auth",
    "_tp_panel_v5_now_iso", "_tp_panel_v5_ids", "_tp_panel_v5_logout",
    "_TP_PANEL_V5_AUTH_TABLE_READY", "_TP_PANEL_V5_AUTH_CACHE", "_TP_PANEL_V5_AUTH_CACHE_TTL_SEC",
}


class _FlakyPanelConnection:
    """Wraps a real sqlite3.Connection and can be told to fail on execute()
    (DELETE) or on commit() -- the two failure points named in B3-T2/T3."""
    def __init__(self, real_con, *, fail_execute=False, fail_commit=False):
        self._real = real_con
        self._fail_execute = fail_execute
        self._fail_commit = fail_commit

    def execute(self, sql, params=()):
        if self._fail_execute and sql.strip().upper().startswith("DELETE"):
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, params)

    def commit(self):
        if self._fail_commit:
            raise sqlite3.OperationalError("database is locked")
        return self._real.commit()

    def close(self):
        return self._real.close()


class _FakeLogoutEvent:
    """Minimal stand-in for the Telethon NewMessage event -- only the
    attributes/methods _tp_panel_v5_ids and _tp_panel_v5_logout actually
    touch."""
    def __init__(self, uid: int):
        self.sender_id = uid
        self.chat_id = uid
        self.replies: List[tuple] = []

    async def reply(self, text, buttons=None):
        self.replies.append((text, buttons))


def _stripped(node):
    """Decorator-stripped copy -- _tp_panel_v5_logout is registered via
    @client.on(events.NewMessage(...)); the extracted body must run as a
    plain coroutine here, not re-trigger Telethon registration. No-op for
    plain Assign/AnnAssign nodes, which have no decorator_list."""
    import copy
    n = copy.deepcopy(node)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        n.decorator_list = []
    return n


def build_auth_ns(db_path: str, *, now_fn=None, fail_execute=False, fail_commit=False):
    nodes = [_stripped(_last_def(PB_TREE, n)) for n in AUTH_NAMES]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    def fake_connect():
        real_con = sqlite3.connect(db_path)
        if fail_execute or fail_commit:
            return _FlakyPanelConnection(real_con, fail_execute=fail_execute, fail_commit=fail_commit)
        return real_con

    ns: Dict[str, Any] = {
        "sqlite3": sqlite3, "time": real_time,
        "Dict": dict,
        "_connect_panel_db": fake_connect,
        "_terminal_ok_button": lambda: [["OK"]],
    }
    exec(compile(module_src, f"<{PB_PATH}:b2fix>", "exec"), ns)
    if now_fn is not None:
        ns["time"] = now_fn
    return ns


def _grant(db_path: str, uid: int) -> None:
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS panel_authorized_users(
            user_id INTEGER PRIMARY KEY, chat_id INTEGER DEFAULT 0,
            username TEXT DEFAULT '', first_name TEXT DEFAULT '',
            authorized_at TEXT NOT NULL DEFAULT '', last_seen_at TEXT NOT NULL DEFAULT ''
        )
    """)
    con.execute("INSERT OR REPLACE INTO panel_authorized_users(user_id, authorized_at, last_seen_at) VALUES (?,?,?)",
                (uid, "x", "x"))
    con.commit(); con.close()


def _pb_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="b1b2_auth_selftest_")
    os.close(fd)
    return path


def test_auth_cache_invalidated_on_revoke():
    print("\n-- AUTH-T1..T5/T7: revoke invalidates cache immediately, other users unaffected --")
    db = _pb_temp_db()
    try:
        _grant(db, 111)
        _grant(db, 222)
        ns = build_auth_ns(db)

        check("AUTH-T1a. authorized user resolves True", ns["_tp_panel_v5_authorized"](111) is True)
        check("AUTH-T1b. cache populated after a positive check", 111 in ns["_TP_PANEL_V5_AUTH_CACHE"],
              ns["_TP_PANEL_V5_AUTH_CACHE"])
        check("AUTH-T1c. second user also authorized+cached", ns["_tp_panel_v5_authorized"](222) is True
              and 222 in ns["_TP_PANEL_V5_AUTH_CACHE"])

        ns["_tp_panel_v5_remove_auth"](111)

        check("AUTH-T2. DB row actually removed",
              sqlite3.connect(db).execute("SELECT 1 FROM panel_authorized_users WHERE user_id=111").fetchone() is None)
        check("AUTH-T3. cache entry for the revoked user removed IMMEDIATELY (no wait)",
              111 not in ns["_TP_PANEL_V5_AUTH_CACHE"], ns["_TP_PANEL_V5_AUTH_CACHE"])
        check("AUTH-T4. next _tp_panel_v5_authorized(111) is False -- NOT served from a stale cache hit",
              ns["_tp_panel_v5_authorized"](111) is False)
        check("AUTH-T5. second (uninvolved) user's cache entry is unaffected",
              222 in ns["_TP_PANEL_V5_AUTH_CACHE"] and ns["_tp_panel_v5_authorized"](222) is True)

        # idempotent revoke
        try:
            ns["_tp_panel_v5_remove_auth"](111)
            raised = False
        except Exception:
            raised = True
        check("AUTH-T7. revoking an already-revoked user is idempotent (no exception)", not raised)
    finally:
        _safe_unlink(db)


def test_auth_cache_invalidated_even_if_db_delete_fails():
    print("\n-- adversarial: DB unavailable DURING revoke -- cache must still be invalidated --")
    db = _pb_temp_db()
    try:
        _grant(db, 888)
        ns = build_auth_ns(db)
        check("setup: authorized+cached", ns["_tp_panel_v5_authorized"](888) is True
              and 888 in ns["_TP_PANEL_V5_AUTH_CACHE"])

        def broken_connect():
            raise sqlite3.OperationalError("database is locked")
        ns["_connect_panel_db"] = broken_connect

        try:
            result = ns["_tp_panel_v5_remove_auth"](888)
            raised = False
        except Exception:
            raised = True
            result = None
        check("revoke swallows the DB failure (existing semantics, unchanged)", not raised)
        check("B-3: revoke now REPORTS the failure via its return value (False), "
              "instead of silently claiming success", result is False, result)
        check("adversarial. cache entry is STILL invalidated even though the DB delete itself "
              "failed -- the UI's logout promise is honored on the cache side regardless of DB "
              "outcome (the unconditional pop happens before the failing DB call)",
              888 not in ns["_TP_PANEL_V5_AUTH_CACHE"], ns["_TP_PANEL_V5_AUTH_CACHE"])

        # restore a working connection for the post-revoke authorized() check
        ns["_connect_panel_db"] = lambda: sqlite3.connect(db)
        check("post-mortem: since the DB delete genuinely failed, the row still exists, so a "
              "fresh (post-cache-pop) re-check correctly returns True -- this is EXPECTED: B-2 "
              "guarantees the cache stops lying, it cannot retroactively fix a failed DB write. "
              "The important guarantee (proven above) is that this True came from a REAL DB "
              "read, not a stale cached one",
              ns["_tp_panel_v5_authorized"](888) is True)
    finally:
        _safe_unlink(db)


def test_auth_regrant_after_logout():
    print("\n-- AUTH-T6: re-grant after logout works normally --")
    db = _pb_temp_db()
    try:
        _grant(db, 333)
        ns = build_auth_ns(db)
        check("initial auth True", ns["_tp_panel_v5_authorized"](333) is True)
        ns["_tp_panel_v5_remove_auth"](333)
        check("post-revoke auth False", ns["_tp_panel_v5_authorized"](333) is False)
        _grant(db, 333)
        check("AUTH-T6. re-grant -> authorized again (cache miss falls through to the fresh DB row)",
              ns["_tp_panel_v5_authorized"](333) is True)
    finally:
        _safe_unlink(db)


def test_auth_no_promotion_on_db_error():
    print("\n-- AUTH-T9: DB error never promotes an uncached user to authorized --")
    db = _pb_temp_db()  # table never created -> every query errors
    try:
        ns = build_auth_ns(db)
        result = ns["_tp_panel_v5_authorized"](444)
        check("AUTH-T9. DB error (missing table) -> False, never a false-positive cached True",
              result is False and 444 not in ns["_TP_PANEL_V5_AUTH_CACHE"], (result, ns["_TP_PANEL_V5_AUTH_CACHE"]))
    finally:
        _safe_unlink(db)


def test_auth_ttl_positive_path_still_works():
    print("\n-- AUTH-T10: TTL positive-cache-hit path still works (F-15 perf fix preserved) --")
    db = _pb_temp_db()
    try:
        _grant(db, 555)
        clock = {"t": 1000.0}

        class FakeTime:
            @staticmethod
            def monotonic(): return clock["t"]

        ns = build_auth_ns(db, now_fn=FakeTime())
        check("first call -> True (DB hit)", ns["_tp_panel_v5_authorized"](555) is True)
        # delete the row directly (bypassing revoke) to prove the SECOND
        # call within TTL is served from cache, not a fresh DB re-check --
        # this is the perf property F-15 exists for.
        sqlite3.connect(db).execute("DELETE FROM panel_authorized_users WHERE user_id=555").connection.commit()
        clock["t"] += 5.0  # well within the 30s TTL
        check("AUTH-T10. within TTL, still True even though the DB row is gone "
              "(cache hit -- proves the F-15 perf optimization itself is intact)",
              ns["_tp_panel_v5_authorized"](555) is True)
        clock["t"] += 30.0  # now past TTL
        check("after TTL expiry, falls through to DB and correctly resolves False",
              ns["_tp_panel_v5_authorized"](555) is False)
    finally:
        _safe_unlink(db)


def test_auth_red_before():
    if not os.environ.get("R1B_CORR_REDCHECK_PANEL_BOT_PATH"):
        return
    print("\n-- AUTH RED-before: revoke on PRE-CORRECTION code --")
    db = _pb_temp_db()
    try:
        _grant(db, 777)
        ns = build_auth_ns(db)
        check("pre-check: authorized+cached", ns["_tp_panel_v5_authorized"](777) is True
              and 777 in ns["_TP_PANEL_V5_AUTH_CACHE"])
        ns["_tp_panel_v5_remove_auth"](777)
        check("RED: DB row removed", sqlite3.connect(db).execute(
            "SELECT 1 FROM panel_authorized_users WHERE user_id=777").fetchone() is None)
        check("RED: pre-correction code leaves the revoked user's cache entry in place "
              "(this must be RED/true-old-bug here)", 777 in ns["_TP_PANEL_V5_AUTH_CACHE"],
              ns["_TP_PANEL_V5_AUTH_CACHE"])
        check("RED: pre-correction code STILL reports authorized=True immediately after revoke "
              "(the exact B-2 defect)", ns["_tp_panel_v5_authorized"](777) is True)
    finally:
        _safe_unlink(db)


# ======================================================================
# PART C -- B-3: /logout must not claim success when the DB DELETE fails
# ======================================================================

def test_b3_logout_success():
    print("\n-- B3-T1/T7: successful logout -- helper True, DB gone, cache gone, "
          "next auth False (no TTL wait), success UI --")
    db = _pb_temp_db()
    try:
        _grant(db, 1001)
        ns = build_auth_ns(db)
        ns["_tp_panel_v5_authorized"](1001)
        check("setup: cached positive", 1001 in ns["_TP_PANEL_V5_AUTH_CACHE"])

        ev = _FakeLogoutEvent(1001)
        run(ns["_tp_panel_v5_logout"](ev))

        check("B3-T1a. DB row removed",
              sqlite3.connect(db).execute("SELECT 1 FROM panel_authorized_users WHERE user_id=1001").fetchone() is None)
        check("B3-T1b. cache entry removed", 1001 not in ns["_TP_PANEL_V5_AUTH_CACHE"], ns["_TP_PANEL_V5_AUTH_CACHE"])
        check("B3-T7. next auth check False immediately (B-2 preserved, no 30s wait)",
              ns["_tp_panel_v5_authorized"](1001) is False)
        check("B3-T1c. exactly one reply sent", len(ev.replies) == 1, ev.replies)
        check("B3-T1d. success UI text emitted, unchanged wording",
              "✅" in ev.replies[0][0] and "вышли" in ev.replies[0][0], ev.replies)
    finally:
        _safe_unlink(db)


def test_b3_logout_delete_failure():
    print("\n-- B3-T2: DELETE raises -- helper False, DB row remains, no false success UI --")
    db = _pb_temp_db()
    try:
        _grant(db, 1002)
        ns = build_auth_ns(db, fail_execute=True)
        ns["_tp_panel_v5_authorized"](1002)
        check("setup: cached positive", 1002 in ns["_TP_PANEL_V5_AUTH_CACHE"])

        ev = _FakeLogoutEvent(1002)
        run(ns["_tp_panel_v5_logout"](ev))

        check("B3-T2a. DB row REMAINS (DELETE genuinely failed)",
              sqlite3.connect(db).execute("SELECT 1 FROM panel_authorized_users WHERE user_id=1002").fetchone() is not None)
        check("B3-T2b. cache entry still removed (B-2's unconditional pop preserved)",
              1002 not in ns["_TP_PANEL_V5_AUTH_CACHE"], ns["_TP_PANEL_V5_AUTH_CACHE"])
        check("B3-T2c. next auth check reflects DB truth: True (row still exists)",
              ns["_tp_panel_v5_authorized"](1002) is True)
        check("B3-T2d. exactly one reply sent", len(ev.replies) == 1, ev.replies)
        text = ev.replies[0][0]
        check("B3-T2e. success text was NOT emitted", "✅" not in text and "вышли" not in text.lower(), text)
        check("B3-T2f. a truthful failure message WAS emitted", "⚠" in text or "не удал" in text.lower(), text)
    finally:
        _safe_unlink(db)


def test_b3_logout_commit_failure():
    print("\n-- B3-T3: commit() raises (execute succeeds) -- same failure semantics --")
    db = _pb_temp_db()
    try:
        _grant(db, 1003)
        ns = build_auth_ns(db, fail_commit=True)
        ns["_tp_panel_v5_authorized"](1003)

        ev = _FakeLogoutEvent(1003)
        run(ns["_tp_panel_v5_logout"](ev))

        check("B3-T3a. DB row remains (commit never landed)",
              sqlite3.connect(db).execute("SELECT 1 FROM panel_authorized_users WHERE user_id=1003").fetchone() is not None)
        check("B3-T3b. cache still popped", 1003 not in ns["_TP_PANEL_V5_AUTH_CACHE"])
        check("B3-T3c. next auth True (DB truth)", ns["_tp_panel_v5_authorized"](1003) is True)
        text = ev.replies[0][0]
        check("B3-T3d. success text NOT emitted on commit failure either", "✅" not in text, text)
    finally:
        _safe_unlink(db)


def test_b3_repeated_logout():
    print("\n-- B3-T4: repeated logout after the row is already gone -- controlled, no crash --")
    db = _pb_temp_db()
    try:
        _grant(db, 1004)
        ns = build_auth_ns(db)
        ev1 = _FakeLogoutEvent(1004)
        run(ns["_tp_panel_v5_logout"](ev1))
        check("first logout: success UI", "✅" in ev1.replies[0][0])

        ev2 = _FakeLogoutEvent(1004)
        raised = False
        try:
            run(ns["_tp_panel_v5_logout"](ev2))
        except Exception:
            raised = True
        check("B3-T4. second logout on an already-absent row does not crash", not raised)
        # DELETE on a non-existent row still executes+commits successfully in
        # sqlite (0 rows affected is not an error) -- so this is correctly
        # reported as success again, not a false negative.
        check("B3-T4b. repeated logout on an absent row is still reported success "
              "(DELETE of 0 rows is a successful DELETE, not a failure)",
              "✅" in ev2.replies[0][0], ev2.replies)
    finally:
        _safe_unlink(db)


def test_b3_regrant_after_logout():
    print("\n-- B3-T5: re-grant after a successful logout works normally --")
    db = _pb_temp_db()
    try:
        _grant(db, 1005)
        ns = build_auth_ns(db)
        run(ns["_tp_panel_v5_logout"](_FakeLogoutEvent(1005)))
        check("post-logout: unauthorized", ns["_tp_panel_v5_authorized"](1005) is False)
        _grant(db, 1005)
        check("B3-T5. re-grant restores access", ns["_tp_panel_v5_authorized"](1005) is True)
    finally:
        _safe_unlink(db)


def test_b3_other_user_unaffected():
    print("\n-- B3-T6: logging out user A never affects user B --")
    db = _pb_temp_db()
    try:
        _grant(db, 1006)
        _grant(db, 2006)
        ns = build_auth_ns(db)
        ns["_tp_panel_v5_authorized"](2006)
        run(ns["_tp_panel_v5_logout"](_FakeLogoutEvent(1006)))
        check("B3-T6. user B's cache and DB access are untouched by user A's logout",
              2006 in ns["_TP_PANEL_V5_AUTH_CACHE"] and ns["_tp_panel_v5_authorized"](2006) is True)
    finally:
        _safe_unlink(db)


def test_b3_db_failure_then_retry():
    print("\n-- B3-T8: first logout fails (no false success), DB restored, second logout succeeds --")
    db = _pb_temp_db()
    try:
        _grant(db, 1008)
        ns = build_auth_ns(db, fail_execute=True)
        ns["_tp_panel_v5_authorized"](1008)

        ev1 = _FakeLogoutEvent(1008)
        run(ns["_tp_panel_v5_logout"](ev1))
        check("B3-T8a. first attempt: no false success", "✅" not in ev1.replies[0][0], ev1.replies)
        check("B3-T8b. row still present after the failed attempt",
              sqlite3.connect(db).execute("SELECT 1 FROM panel_authorized_users WHERE user_id=1008").fetchone() is not None)

        # "DB restored" -- swap in a healthy connection factory, same cache/table state.
        ns["_connect_panel_db"] = lambda: sqlite3.connect(db)
        ev2 = _FakeLogoutEvent(1008)
        run(ns["_tp_panel_v5_logout"](ev2))
        check("B3-T8c. second attempt (DB healthy): success UI", "✅" in ev2.replies[0][0], ev2.replies)
        check("B3-T8d. row now actually gone", sqlite3.connect(db).execute(
            "SELECT 1 FROM panel_authorized_users WHERE user_id=1008").fetchone() is None)
        check("B3-T8e. next auth False", ns["_tp_panel_v5_authorized"](1008) is False)
    finally:
        _safe_unlink(db)


def test_b3_red_before():
    """Meaningful when re-invoked with R1B_CORR_REDCHECK_PANEL_BOT_PATH
    pointed at panel_bot.py.bak_b3fix_<ts> (pre-B3-fix, post-B1/B2)."""
    if not os.environ.get("R1B_CORR_REDCHECK_PANEL_BOT_PATH"):
        return
    print("\n-- B3 RED-before: forced DELETE failure on PRE-B3-FIX code --")
    db = _pb_temp_db()
    try:
        _grant(db, 1099)
        ns = build_auth_ns(db, fail_execute=True)
        ns["_tp_panel_v5_authorized"](1099)
        ev = _FakeLogoutEvent(1099)
        run(ns["_tp_panel_v5_logout"](ev))
        row_present = sqlite3.connect(db).execute(
            "SELECT 1 FROM panel_authorized_users WHERE user_id=1099").fetchone() is not None
        text = ev.replies[0][0] if ev.replies else ""
        check("RED: pre-B3-fix code STILL emits the success message while the DB row remains "
              "(the exact B-3 defect: false-success logout UI)",
              row_present and "✅" in text, (row_present, text))
    finally:
        _safe_unlink(db)


def main() -> int:
    test_ph_placeholder_failure_blocks_send()
    test_ph_recovery_sends_exactly_once()
    test_ph_no_duplicate_regression()
    test_ph_special_card_flows_unaffected()
    test_ph_red_before()

    test_auth_cache_invalidated_on_revoke()
    test_auth_cache_invalidated_even_if_db_delete_fails()
    test_auth_regrant_after_logout()
    test_auth_no_promotion_on_db_error()
    test_auth_ttl_positive_path_still_works()
    test_auth_red_before()

    test_b3_logout_success()
    test_b3_logout_delete_failure()
    test_b3_logout_commit_failure()
    test_b3_repeated_logout()
    test_b3_regrant_after_logout()
    test_b3_other_user_unaffected()
    test_b3_db_failure_then_retry()
    test_b3_red_before()

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
