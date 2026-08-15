# -*- coding: utf-8 -*-
"""
TPilot ManagerBot Stage M2.1 skeleton.

Scope:
- standalone Telethon bot
- private chat only
- /start (clean UI + access auto-grant/self-heal)
- fail-closed access via access_users and access_targets
- additive schema init for ManagerBot tables only
- no lead cards yet
- no manual status yet
- no main.py hooks yet
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Tuple

from dotenv import load_dotenv
from telethon import TelegramClient, events, Button, Button

import storage  # TPILOT MANAGERBOT ACCESS AUTO-GRANT 20260727: canonical
# storage.manager_bot_access_ensure_sync -- storage.py has zero import-time
# side effects (only imports/assignments/def's at module scope, no DB open),
# same convention main.py's own `from storage import (...)` already relies on.

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env.TPilot"
load_dotenv(ENV_PATH, override=True)


def _env(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _resolve_path(raw: str, default_rel: str) -> str:
    value = str(raw or "").strip() or default_rel
    p = Path(value)
    if p.is_absolute():
        return str(p)
    return str((BASE_DIR / p).resolve())


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)) or default)
    except Exception:
        return default


API_ID = _env_int("API_ID", 0)
API_HASH = _env("API_HASH")
MANAGER_BOT_TOKEN = _env("MANAGER_BOT_TOKEN")
MANAGER_BOT_SESSION_FILE = _resolve_path(_env("MANAGER_BOT_SESSION_FILE"), "sessions/session_manager_bot")
TPILOT_DB_PATH = _resolve_path(_env("DB_PATH"), "db/data_tpilot.db")
MANAGER_BOT_LOG_FILE = _resolve_path(_env("MANAGER_BOT_LOG_FILE"), "logs/manager_bot.log")
MANAGER_BOT_POLL_SEC = max(5, _env_int("MANAGER_BOT_POLL_SEC", 5))

# R1B/F-40: strong reference to the long-lived poll-loop task, set in main().
_MB_POLL_LOOP_TASK = None

if API_ID <= 0 or not API_HASH:
    raise RuntimeError("API_ID/API_HASH are not set in .env.TPilot")
if not MANAGER_BOT_TOKEN:
    raise RuntimeError("MANAGER_BOT_TOKEN is not set in .env.TPilot")

CALLBACK_PREFIX = b"mb:"


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("manager_bot")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        Path(MANAGER_BOT_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            MANAGER_BOT_LOG_FILE,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except Exception as exc:
        logger.warning("file logging disabled: %r", exc)

    return logger


log = _build_logger()
client = TelegramClient(MANAGER_BOT_SESSION_FILE, API_ID, API_HASH)


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


# --- TPILOT MANAGER EVENT DELIVERY STATE MACHINE 20260726 START ---
# Forward-fix Phase 3 (post-incident review 2026-07-26): before this, a
# send failure (e.g. Telethon's EntityBoundsInvalidError from an
# unsanitized dynamic field parsed as markdown) had NO attempt counter, NO
# backoff, and NO plain-text fallback -- the SAME poison event was
# re-selected and re-sent every ~5s poll cycle FOREVER (a restart did not
# help: the query always orders by id ASC, so the oldest unsent event is
# re-picked first). See tools/manager_bot_delivery_selftest.py.
MANAGER_BOT_SEND_MAX_ATTEMPTS = max(1, _env_int("MANAGER_BOT_SEND_MAX_ATTEMPTS", 5))
# REVISION (2026-07-29, Blocker 1): bounded claim-lease duration. A claim
# older than this (with no confirmed outcome) is eligible for explicit
# recovery -- see _mb_recover_stale_claims. Deliberately well above the
# poll interval (MANAGER_BOT_POLL_SEC, minimum 5s) so a live, healthy
# claim is never mistaken for stale mid-send.
MANAGER_BOT_CLAIM_LEASE_SECONDS = max(10, _env_int("MANAGER_BOT_CLAIM_LEASE_SECONDS", 120))
# A stable per-process correlation reference (diagnostic only -- never
# logged/printed with any Telegram identifier attached; see Blocker 4).
_MB_WORKER_REF = "pid%s-%s" % (os.getpid(), uuid.uuid4().hex[:8])


def _w2_actor_ref(tg_user_id: Any) -> str:
    """REVISION (2026-07-29, Blocker 4): the ONLY form a Telegram user id
    may take in any W2 delivery-path log line. SHA256-namespaced, never
    the raw decimal id, never Python's unstable per-process hash(). Same
    construction as storage._w2_pseudonymize (reused directly, not
    reimplemented, so the SAME uid always maps to the SAME actor_ref
    whether pseudonymized in manager_bot.py or storage.py)."""
    try:
        return storage._w2_pseudonymize("w2-actor", int(tg_user_id or 0))
    except Exception:
        return "unknown"

# Telethon error classes (by NAME, not by import -- avoids a hard dependency
# on telethon.errors' exact module layout, and matches on the class name
# even if a different exception object type surfaces the same RPC error)
# that mean "the formatting entities Telegram was asked to apply are
# invalid/too large for this text", i.e. a markdown-parse problem with the
# text itself -- NOT a network/auth/rate-limit failure, and NOT something a
# retry with the SAME parse_mode could ever fix. A plain-text (parse_mode=
# None) resend is the correct, delivery-preserving response.
_MB_FORMATTING_ERROR_CLASS_NAMES = frozenset({
    "EntityBoundsInvalidError",
    "EntitiesTooLongError",
    "MessageEntitiesTooLongError",
    "MessageEmptyError",
})
_MB_FORMATTING_ERROR_TEXT_MARKERS = ("ENTITY_BOUNDS_INVALID", "ENTITIES_TOO_LONG", "MESSAGE_EMPTY")


def _mb_is_formatting_error(exc: BaseException) -> bool:
    if type(exc).__name__ in _MB_FORMATTING_ERROR_CLASS_NAMES:
        return True
    text = str(exc).upper()
    return any(marker in text for marker in _MB_FORMATTING_ERROR_TEXT_MARKERS)


def _mb_backoff_seconds(attempts: int) -> int:
    """Exponential backoff capped at 1h: 60, 120, 240, 480, 960... never
    the old unconditional ~5s retry."""
    return min(60 * (2 ** max(0, attempts - 1)), 3600)


def _mark_send_attempt(event_id: int, tg_user_id: int, exc: BaseException) -> None:
    """Records one FAILED delivery attempt (after the plain-text fallback,
    if attempted, has ALSO failed -- see _send_event_to_user). Bounded:
    once attempts reaches MANAGER_BOT_SEND_MAX_ATTEMPTS the row is marked
    'dead' (quarantined) and _fetch_unsent_events_for_user's WHERE clause
    permanently excludes it -- the event row itself is NEVER deleted (no
    silent message loss; the audit trail of who/what/when/how-many-times
    stays in manager_bot_sent). Restart-safe: all state is in the DB, nothing
    in memory. Only the exception CLASS NAME is ever logged/stored -- never
    exc's str()/repr() (which can echo request content), never any part of
    the event's payload/text.

    RQ3 (2026-07-26, independent-review corrective pass): a row that is
    ALREADY 'sent' must never be regressed to 'failed'/'dead' by this
    function. Without a guard, a late exception raised (e.g. by the
    poll-loop's per-event backstop) AFTER _save_card_and_sent already
    flipped the row to 'sent' would silently re-open it for retry -- the
    event was genuinely delivered, but would be re-selected by
    _fetch_unsent_events_for_user and sent again, up to a second full round
    of attempts, ending in an incorrect quarantine of an already-delivered
    event. Guarded with a WHERE clause on the UPSERT's DO UPDATE action
    (SQLite ON CONFLICT ... DO UPDATE ... WHERE, supported since 3.24): if
    the existing row's send_status is already 'sent', the UPDATE branch is
    skipped entirely -- attempts/next_attempt_at/send_status/
    last_error_class/sent_at all stay exactly as they were. A brand-new row
    (no prior INSERT) is unaffected -- the INSERT branch only ever fires
    for a genuinely new failure record. Bounded retry/backoff for actually
    undelivered events is unchanged.

    W2 (2026-07-29, frozen master plan, Phase D/I-08): the failure is first
    run through _mb_classify_delivery_error. A TERMINAL class (deactivated/
    blocked recipient, invalid peer) is dead-lettered IMMEDIATELY -- it
    never consumes the ordinary bounded-retry budget, because no number of
    retries will ever change the outcome. A retryable transient class uses
    the jittered backoff curve (D-01 item 6); a flood-wait's own reported
    cooldown overrides the generic curve when present. Every attempt is
    also written to manager_bot_delivery_failures (Phase D structured log)
    on a best-effort basis -- a logging failure there can never block or
    alter this function's own state transition."""
    if not event_id or not tg_user_id:
        return
    con = _connect()
    try:
        cur = con.execute(
            "SELECT attempts, send_status FROM manager_bot_sent WHERE tg_user_id=? AND event_id=?",
            (int(tg_user_id), int(event_id)),
        )
        row = cur.fetchone()
        if row is not None and str(row[1] or "") == "sent":
            # Already delivered -- nothing to record, nothing to mutate.
            return
        prev_attempts = int(row[0]) if row and row[0] is not None else 0
        attempts = prev_attempts + 1

        classification = _mb_classify_delivery_error(exc)
        terminal = not classification["retryable"]
        dead = terminal or attempts >= MANAGER_BOT_SEND_MAX_ATTEMPTS
        status = "dead" if dead else "failed"
        if dead:
            next_attempt_at = ""
        elif classification.get("cooldown_seconds"):
            next_attempt_at = (datetime.utcnow() + timedelta(
                seconds=max(1, int(classification["cooldown_seconds"])))).replace(microsecond=0).isoformat()
        else:
            next_attempt_at = (datetime.utcnow() + timedelta(
                seconds=_mb_backoff_seconds_with_jitter(attempts))).replace(microsecond=0).isoformat()
        err_cls = type(exc).__name__[:120]
        con.execute(
            """
            INSERT INTO manager_bot_sent(
                tg_user_id, event_id, sent_at, attempts, next_attempt_at, send_status, last_error_class, fallback_used
            ) VALUES (?, ?, '', ?, ?, ?, ?, 0)
            ON CONFLICT(tg_user_id, event_id) DO UPDATE SET
                attempts=excluded.attempts,
                next_attempt_at=excluded.next_attempt_at,
                send_status=excluded.send_status,
                last_error_class=excluded.last_error_class
            WHERE manager_bot_sent.send_status <> 'sent'
            """,
            (int(tg_user_id), int(event_id), attempts, next_attempt_at, status, err_cls),
        )
        con.commit()
        log.warning(
            "send event failed actor_ref=%s event_id=%s attempt=%s error_class=%s category=%s action=%s",
            _w2_actor_ref(tg_user_id), event_id, attempts, err_cls, classification["category"], status,
        )
        try:
            mk_row = con.execute("SELECT manager_key FROM manager_bot_events WHERE id=?", (int(event_id),)).fetchone()
            manager_key = str(mk_row[0] or "") if mk_row else ""
            storage.w2_log_delivery_failure(
                TPILOT_DB_PATH, event_id=int(event_id), manager_key=manager_key, tg_user_id=int(tg_user_id),
                failure_class=classification["failure_class"], retryable=not dead, attempt_number=attempts,
                next_retry_at=next_attempt_at, terminal_action=("dead_letter" if dead else ""),
            )
        except Exception:
            pass
    except Exception as db_exc:
        log.warning("mark send attempt failed actor_ref=%s event_id=%s error_class=%s",
                    _w2_actor_ref(tg_user_id), event_id, type(db_exc).__name__)
    finally:
        con.close()


async def _mb_send_with_fallback(tg_user_id: int, text: str, *, event_id: Any = None, buttons: Any = None):
    """Sends `text` with the project's default (Telethon markdown) parse
    mode; on a formatting-specific failure (_mb_is_formatting_error),
    retries EXACTLY ONCE with parse_mode=None (plain text) -- delivery-
    preserving: the manager gets the card with literal '**'/'__'/backtick
    characters rather than losing the message entirely. Returns
    (message, fallback_used). A non-formatting exception on the first
    attempt, or ANY exception on the plain-text retry, propagates to the
    caller unchanged -- callers wrap this in their own try/except (same
    shape as before this fix) and call _mark_send_attempt on failure; this
    helper performs no DB writes itself."""
    try:
        if buttons is not None:
            msg = await client.send_message(int(tg_user_id), text, buttons=buttons)
        else:
            msg = await client.send_message(int(tg_user_id), text)
        return msg, False
    except Exception as exc:
        if not _mb_is_formatting_error(exc):
            raise
        log.warning(
            "card formatted send failed, plain fallback actor_ref=%s event_id=%s error_class=%s",
            _w2_actor_ref(tg_user_id), event_id, type(exc).__name__,
        )
        if buttons is not None:
            msg = await client.send_message(int(tg_user_id), text, buttons=buttons, parse_mode=None)
        else:
            msg = await client.send_message(int(tg_user_id), text, parse_mode=None)
        return msg, True
# --- TPILOT MANAGER EVENT DELIVERY STATE MACHINE 20260726 END ---


# --- TPILOT W2 ACCESS & DELIVERY 20260729 START ---
# Central error classifier for the delivery path (Phase D of the frozen W2
# task). Classifies by exception CLASS NAME string (same convention as
# _mb_is_formatting_error above) rather than isinstance/import, because
# manager_bot.py's own selftest harness (and any future one) extracts
# functions via AST without importing telethon.errors -- string matching
# keeps the classifier testable with a plain Exception subclass. A class
# NOT in any of these sets is "unexpected_internal" -- retryable (bounded,
# via the existing attempts/backoff/dead-letter machinery), never silently
# swallowed, never assumed safe.
_MB_TERMINAL_DEACTIVATED_CLASS_NAMES = frozenset({
    "InputUserDeactivatedError", "UserDeactivatedError", "UserDeactivatedBanError",
    "UserIsBlockedError", "UserBlockedError", "UserBannedInChannelError",
})
_MB_TERMINAL_MISSING_PEER_CLASS_NAMES = frozenset({
    "PeerIdInvalidError", "UserIdInvalidError", "ChannelPrivateError", "ChatWriteForbiddenError",
})
_MB_TRANSIENT_FLOODWAIT_CLASS_NAMES = frozenset({"FloodWaitError", "FloodError", "SlowModeWaitError"})
_MB_TRANSIENT_NETWORK_CLASS_NAMES = frozenset({
    "ConnectionError", "TimeoutError", "asyncio.TimeoutError", "ConnectionResetError",
    "ServerError", "TimeoutException",
})
_MB_TRANSIENT_DB_MARKERS = ("DATABASE IS LOCKED", "DATABASE IS BUSY")


def _mb_classify_delivery_error(exc: BaseException) -> Dict[str, Any]:
    """Returns {"failure_class": <exception class name>, "category": ...,
    "retryable": bool, "cooldown_seconds": Optional[int]}. "retryable"=False
    means the caller (_mark_send_attempt) must dead-letter immediately
    (I-08), regardless of how many attempts remain in the normal budget --
    a permanently-deactivated/blocked recipient or an invalid peer will
    never succeed no matter how many times it is retried, so counting it
    against the ordinary bounded-retry budget only delays the correct
    terminal outcome. A DB-busy/locked error or a network/flood-wait error
    is always retryable; flood-wait additionally reports the server's own
    cooldown (cooldown_seconds) so the caller can honor it instead of the
    generic exponential curve."""
    cls_name = type(exc).__name__
    text_upper = str(exc).upper()

    if cls_name in _MB_TERMINAL_DEACTIVATED_CLASS_NAMES:
        return {"failure_class": cls_name, "category": "terminal_deactivated_recipient",
                "retryable": False, "cooldown_seconds": None}
    if cls_name in _MB_TERMINAL_MISSING_PEER_CLASS_NAMES:
        return {"failure_class": cls_name, "category": "terminal_missing_peer",
                "retryable": False, "cooldown_seconds": None}
    if cls_name in _MB_TRANSIENT_FLOODWAIT_CLASS_NAMES:
        seconds = getattr(exc, "seconds", None)
        try:
            seconds = int(seconds) if seconds is not None else None
        except Exception:
            seconds = None
        return {"failure_class": cls_name, "category": "transient_floodwait",
                "retryable": True, "cooldown_seconds": seconds}
    if isinstance(exc, sqlite3.OperationalError) and any(m in text_upper for m in _MB_TRANSIENT_DB_MARKERS):
        return {"failure_class": cls_name, "category": "transient_db_busy",
                "retryable": True, "cooldown_seconds": None}
    if cls_name in _MB_TRANSIENT_NETWORK_CLASS_NAMES:
        return {"failure_class": cls_name, "category": "transient_network",
                "retryable": True, "cooldown_seconds": None}
    if _mb_is_formatting_error(exc):
        # A formatting error that reaches _mark_send_attempt already
        # survived _mb_send_with_fallback's own plain-text retry AND still
        # failed -- both parse modes were rejected, so this is a poison
        # event for THIS text, not a transient condition. Bounded retry
        # (not immediate dead-letter) is kept deliberately: a manager's
        # display_name/reason field could still change before the next
        # attempt (e.g. a later profile edit), so it is not provably
        # permanent the way a deactivated account is.
        return {"failure_class": cls_name, "category": "poison_formatting",
                "retryable": True, "cooldown_seconds": None}
    return {"failure_class": cls_name, "category": "unexpected_internal",
            "retryable": True, "cooldown_seconds": None}


def _mb_backoff_seconds_with_jitter(attempts: int) -> float:
    """Scheduling-time wrapper around the pure _mb_backoff_seconds curve
    (left untouched -- tools/manager_bot_delivery_selftest.py asserts its
    exact values). Applies uniform(0.8, 1.2) jitter (D-01 item 6) so many
    simultaneously-failing events don't all wake up and retry in the same
    instant -- only the ACTUAL next_attempt_at written to the DB is
    jittered; the curve itself stays deterministic and separately
    testable."""
    return _mb_backoff_seconds(attempts) * random.uniform(0.8, 1.2)


def _mb_claim_event(event_id: int, tg_user_id: int) -> bool:
    """Atomic claim with a BOUNDED LEASE (Blocker 1 revision; the CLAIMED
    state in the W2 delivery state machine, I-04, contract E1/E5/E6):
    before any send attempt, atomically flip this (tg_user_id, event_id)
    row to send_status='sending', with a fresh lease_token, a
    lease_expires_at MANAGER_BOT_CLAIM_LEASE_SECONDS in the future, and
    this process's claim_worker_ref. Returns True only if THIS call
    performed the claim -- a second concurrent caller (two worker
    processes briefly overlapping across a restart, or a duplicate
    poll-loop pass) sees rowcount=0 and must skip the event rather than
    send it again.

    Never reclaims a row already 'sent' (an already-delivered event must
    never be resent) or already 'sending' -- critically, this INCLUDES an
    EXPIRED 'sending' row: a live OR expired lease is NEVER stolen by this
    function (point 3 of the Blocker 1 contract). The only way a stale
    ('sending', lease expired) row ever leaves that state is the SEPARATE,
    explicit _mb_recover_stale_claims -- never this ordinary claim path,
    and never a silent auto-resend. Restart-safe: the claim lives in the
    DB, not memory."""
    if not event_id or not tg_user_id:
        return False
    con = _connect()
    try:
        now_dt = datetime.utcnow()
        now = now_dt.replace(microsecond=0).isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=MANAGER_BOT_CLAIM_LEASE_SECONDS)).replace(microsecond=0).isoformat()
        lease_token = uuid.uuid4().hex
        cur = con.execute(
            """
            INSERT INTO manager_bot_sent(
                tg_user_id, event_id, sent_at, attempts, next_attempt_at,
                send_status, last_error_class, fallback_used, claimed_at,
                lease_token, lease_expires_at, claim_worker_ref
            ) VALUES (?, ?, '', 0, '', 'sending', '', 0, ?, ?, ?, ?)
            ON CONFLICT(tg_user_id, event_id) DO UPDATE SET
                send_status='sending',
                claimed_at=excluded.claimed_at,
                lease_token=excluded.lease_token,
                lease_expires_at=excluded.lease_expires_at,
                claim_worker_ref=excluded.claim_worker_ref
            WHERE manager_bot_sent.send_status NOT IN ('sent', 'sending')
            """,
            (int(tg_user_id), int(event_id), now, lease_token, lease_expires_at, _MB_WORKER_REF),
        )
        con.commit()
        return cur.rowcount > 0
    except Exception as exc:
        log.warning("claim event failed event_id=%s error_class=%s", event_id, type(exc).__name__)
        return False
    finally:
        con.close()


def _mb_recover_stale_claims(now_iso: str = "") -> List[Dict[str, Any]]:
    """Explicit, observable stale-claim recovery (Blocker 1 revision,
    points 4-8 and 11-12). Finds every manager_bot_sent row still
    send_status='sending' whose lease_expires_at has PASSED, and
    atomically transitions each one to the SAME RETRY_WAIT representation
    _mark_send_attempt already uses (send_status='failed',
    next_attempt_at computed via the existing jittered backoff curve) --
    never DEAD_LETTER directly unless the attempts bound is already
    reached, and never an immediate resend. The row's lease is cleared
    (lease_token='', lease_expires_at='') so it reads unambiguously as no
    longer claimed.

    Atomicity / point 5 (two recovery workers cannot both reclaim): each
    row's UPDATE re-checks send_status='sending' AND lease_expires_at<=?
    AND lease_token=? in its OWN WHERE clause -- SQLite serializes writers
    on the same database file, so whichever caller's UPDATE commits FIRST
    flips send_status away from 'sending', making every subsequent
    caller's WHERE fail to match (rowcount=0) for that row, even if both
    callers read the same stale row in their initial SELECT.

    Points 9-10 (terminal/delivered events are never recovered): 'dead'
    and 'sent' rows never match send_status='sending' in the first place
    -- excluded structurally by the SELECT itself, not by a separate check.

    Point 7 (documented attempts rule): attempts is INCREMENTED by 1 on
    every recovery. The true outcome of a stale claim is unknowable --
    the owning process may have crashed before ever calling
    send_message(), or it may have sent successfully and crashed before
    persisting that fact. Incrementing is the conservative choice for the
    retry BUDGET: it can only make the event reach DEAD_LETTER sooner
    (bounded, safe, no data loss -- the event row is never deleted), never
    causes a duplicate SEND (recovery itself never calls send_message();
    the actual resend only happens later, through the ordinary
    poll-loop -> claim -> send path, which establishes a brand-new lease
    first -- satisfying point 11, no two simultaneous sends can result
    from recovery). last_error_class is set to the sentinel
    'StaleClaimRecovered' (never a real Telegram exception class) so this
    is distinguishable in the audit trail from a genuine reported failure.

    Returns the list of recovered (event_id, attempts, status) records
    actually recovered by THIS call, with a pseudonymized actor_ref
    (never a raw tg_user_id) -- for logging/observability (point 5). An
    empty list is the normal, expected result on most calls."""
    now = now_iso or _now_iso()
    con = _connect()
    recovered: List[Dict[str, Any]] = []
    try:
        stale = con.execute(
            """
            SELECT tg_user_id, event_id, attempts, lease_token
            FROM manager_bot_sent
            WHERE send_status='sending' AND lease_expires_at<>'' AND lease_expires_at<=?
            """,
            (now,),
        ).fetchall()
        for row in stale:
            uid, eid = int(row["tg_user_id"]), int(row["event_id"])
            prev_attempts = int(row["attempts"] or 0)
            token = row["lease_token"]
            attempts = prev_attempts + 1
            dead = attempts >= MANAGER_BOT_SEND_MAX_ATTEMPTS
            status = "dead" if dead else "failed"
            next_attempt_at = "" if dead else (
                datetime.utcnow() + timedelta(seconds=_mb_backoff_seconds_with_jitter(attempts))
            ).replace(microsecond=0).isoformat()
            cur = con.execute(
                """
                UPDATE manager_bot_sent
                SET send_status=?, attempts=?, next_attempt_at=?,
                    last_error_class='StaleClaimRecovered',
                    lease_token='', lease_expires_at=''
                WHERE tg_user_id=? AND event_id=? AND send_status='sending'
                  AND lease_expires_at<>'' AND lease_expires_at<=? AND lease_token=?
                """,
                (status, attempts, next_attempt_at, uid, eid, now, token),
            )
            if cur.rowcount > 0:
                recovered.append({
                    "event_id": eid, "attempts": attempts, "status": status,
                    "actor_ref": storage._w2_pseudonymize("w2-recovery", uid),
                })
        con.commit()
        if recovered:
            log.warning(
                "W2 stale-claim recovery: %d event(s) moved out of 'sending' (lease expired, worker=%s)",
                len(recovered), _MB_WORKER_REF,
            )
    except Exception as exc:
        log.warning("stale claim recovery failed error_class=%s", type(exc).__name__)
    finally:
        con.close()
    return recovered


_MB_TERMINAL_SEND_STATUSES = frozenset({
    "terminal_access_denied", "terminal_manager_deleted", "terminal_invalid_event", "cutoff_suppressed",
})


def _find_manager_row_by_key(manager_key: str) -> Dict[str, Any]:
    """REVISION (2026-07-29, Blocker 3): read-only lookup used by
    _send_event_to_user's TERMINAL_MANAGER_DELETED check. SAME active-
    manager condition storage.w2_access_decision's own deleted_manager
    branch uses (status='active' AND is_enabled=1) -- kept identical so
    the two never disagree about what "deleted" means."""
    mk = _norm_key(manager_key)
    if not mk:
        return {}
    con = _connect()
    try:
        row = con.execute(
            "SELECT manager_key, status, is_enabled FROM managers WHERE manager_key=?", (mk,)
        ).fetchone()
        if row is None or str(row["status"] or "") != "active" or int(row["is_enabled"] or 0) != 1:
            return {}
        return dict(row)
    except Exception as exc:
        log.warning("manager row lookup failed error_class=%s", type(exc).__name__)
        return {}
    finally:
        con.close()


def _mb_write_terminal_state(event_id: int, tg_user_id: int, state: str, reason_code: str) -> bool:
    """REVISION (2026-07-29, Blocker 3): persist a REAL, observable
    terminal outcome for an event that will never be attempted -- instead
    of the pre-revision behavior (log.info + return, with NO row written
    at all, so the event stayed structurally indistinguishable from
    'never yet polled'). `state` must be one of _MB_TERMINAL_SEND_STATUSES.

    Idempotent (point 9): the UPSERT's WHERE guard mirrors _mark_send_
    attempt's own RQ3 guard -- never overwrites a row already 'sent', and
    ALSO never overwrites a row already in ANY terminal state (so replaying
    the classifier on the same event twice, point 10, is a no-op the
    second time -- the row is simply left as it was). attempts/
    next_attempt_at are left at 0/'' (these are not retry-budget
    consumptions; a terminal write is a distinct outcome, not a failed
    send attempt). Never writes exception text -- `reason_code` is a
    short, pre-defined, non-sensitive string, stored in the SAME
    last_error_class column _mark_send_attempt already uses for its own
    (also non-sensitive) exception class names.

    Also appends a structured, privacy-safe audit entry to
    manager_bot_delivery_failures (retryable=False, terminal_action=state)
    on a best-effort basis, via the SAME storage.w2_log_delivery_failure
    Phase D already established -- reusing existing infrastructure rather
    than inventing a second log (Blocker 3's own "do not invent duplicate
    state storage" instruction)."""
    if state not in _MB_TERMINAL_SEND_STATUSES or not event_id or not tg_user_id:
        return False
    con = _connect()
    try:
        now = _now_iso()
        cur = con.execute(
            """
            INSERT INTO manager_bot_sent(
                tg_user_id, event_id, sent_at, attempts, next_attempt_at,
                send_status, last_error_class, fallback_used, claimed_at
            ) VALUES (?, ?, '', 0, '', ?, ?, 0, ?)
            ON CONFLICT(tg_user_id, event_id) DO UPDATE SET
                send_status=excluded.send_status,
                last_error_class=excluded.last_error_class
            WHERE manager_bot_sent.send_status NOT IN (
                'sent', 'dead', 'terminal_access_denied', 'terminal_manager_deleted',
                'terminal_invalid_event', 'cutoff_suppressed'
            )
            """,
            (int(tg_user_id), int(event_id), state, reason_code[:120], now),
        )
        con.commit()
        wrote = cur.rowcount > 0
        if wrote:
            log.warning("W2 terminal state event_id=%s state=%s reason=%s", event_id, state, reason_code)
        try:
            mk_row = con.execute("SELECT manager_key FROM manager_bot_events WHERE id=?", (int(event_id),)).fetchone()
            manager_key = str(mk_row[0] or "") if mk_row else ""
            storage.w2_log_delivery_failure(
                TPILOT_DB_PATH, event_id=int(event_id), manager_key=manager_key, tg_user_id=int(tg_user_id),
                failure_class=reason_code, retryable=False, attempt_number=0,
                next_retry_at="", terminal_action=state,
            )
        except Exception:
            pass
        return wrote
    except Exception as exc:
        log.warning("write terminal state failed event_id=%s state=%s error_class=%s",
                    event_id, state, type(exc).__name__)
        return False
    finally:
        con.close()
# --- TPILOT W2 ACCESS & DELIVERY 20260729 END ---


def _norm_key(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _connect() -> sqlite3.Connection:
    Path(TPILOT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(TPILOT_DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    return con


def init_schema() -> None:
    con = _connect()
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS manager_bot_events(
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

            CREATE INDEX IF NOT EXISTS manager_bot_events_type_idx
                ON manager_bot_events(event_type, id);

            CREATE INDEX IF NOT EXISTS manager_bot_events_created_idx
                ON manager_bot_events(created_at);

            CREATE INDEX IF NOT EXISTS manager_bot_events_lead_idx
                ON manager_bot_events(manager_key, chat_id, lead_date);

            CREATE TABLE IF NOT EXISTS manager_bot_sent(
                tg_user_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, event_id)
            );

            CREATE TABLE IF NOT EXISTS manager_lead_cards(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL,
                bot_chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL DEFAULT '',
                lead_date TEXT NOT NULL DEFAULT '',
                last_status_shown TEXT NOT NULL DEFAULT '',
                last_bucket_shown TEXT NOT NULL DEFAULT '',
                last_manual_flag INTEGER NOT NULL DEFAULT 0,
                last_action_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                -- W2 REVISION (Blocker 5, 2026-07-29): the CURRENT (wide)
                -- key, declared directly here so every FRESH database gets
                -- it correctly from creation, never touching the legacy
                -- narrow-key rebuild path at all. CREATE TABLE IF NOT
                -- EXISTS never alters an EXISTING (legacy) table -- see
                -- w2_assert_lead_cards_schema_compatible below for that case.
                UNIQUE(tg_user_id, chat_id, manager_key, lead_date)
            );

            CREATE INDEX IF NOT EXISTS manager_lead_cards_msg_idx
                ON manager_lead_cards(bot_chat_id, message_id);

            CREATE INDEX IF NOT EXISTS manager_lead_cards_lead_idx
                ON manager_lead_cards(chat_id, manager_key);
            """
        )

        # --- TPILOT MANAGER EVENT DELIVERY STATE MACHINE 20260726 START ---
        # Forward-fix Phase 3 (post-incident review 2026-07-26): additive,
        # idempotent columns on manager_bot_sent (same PRAGMA table_info
        # guard idiom as event_cutoff_id above -- SQLite has no ADD COLUMN
        # IF NOT EXISTS). Every PRE-EXISTING row in this table represents a
        # message that was ALREADY successfully delivered (that is the only
        # way a row is ever inserted -- see _save_card_and_sent), so
        # send_status DEFAULT 'sent' classifies every legacy row correctly
        # without a backfill pass; a genuinely not-yet-attempted event has
        # NO row here at all (the existing "unsent" definition), so no
        # 'pending' status value is needed. attempts/next_attempt_at/
        # last_error_class only ever matter for a 'failed' row (bounded
        # retry with backoff) or a 'dead' one (quarantined, never retried
        # again, never silently deleted -- audit trail preserved).
        cols = [r[1] for r in con.execute("PRAGMA table_info(manager_bot_sent)").fetchall()]
        for _col, _decl in (
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "TEXT NOT NULL DEFAULT ''"),
            ("send_status", "TEXT NOT NULL DEFAULT 'sent'"),
            ("last_error_class", "TEXT NOT NULL DEFAULT ''"),
            ("fallback_used", "INTEGER NOT NULL DEFAULT 0"),
            ("claimed_at", "TEXT NOT NULL DEFAULT ''"),  # W2 20260729: CLAIMED-state timestamp, see _mb_claim_event
            # REVISION (2026-07-29, Blocker 1): bounded claim LEASE, so a
            # claim survives a crash without either (a) staying 'sending'
            # forever (silent loss) or (b) being stealable while still
            # legitimately in flight. lease_token is the unique ownership
            # marker (ties one claim to one worker instance); lease_expires_at
            # is when that ownership lapses and the row becomes eligible for
            # explicit, observable recovery (never automatic re-send);
            # claim_worker_ref is a correlation reference identifying which
            # process instance holds/held the lease (diagnostic only).
            ("lease_token", "TEXT NOT NULL DEFAULT ''"),
            ("lease_expires_at", "TEXT NOT NULL DEFAULT ''"),
            ("claim_worker_ref", "TEXT NOT NULL DEFAULT ''"),
        ):
            if _col not in cols:
                con.execute(f"ALTER TABLE manager_bot_sent ADD COLUMN {_col} {_decl}")
        # --- TPILOT MANAGER EVENT DELIVERY STATE MACHINE 20260726 END ---

        # --- TPILOT W2 ACCESS & DELIVERY 20260729 START ---
        # Additive/idempotent, delegated to storage.py (single source of
        # truth for both migrations, unit-tested there against synthetic
        # fixtures): add the cutoff_reason/cutoff_set_at observability
        # columns + manager_bot_cutoff_log table (D-24/D-03) -- purely
        # additive (ALTER TABLE ADD COLUMN / CREATE TABLE IF NOT EXISTS),
        # safe to run unconditionally on every startup, same guarded-import
        # pattern as the schedule/transfer table init calls above.
        try:
            storage.w2_cutoff_columns_ensure(TPILOT_DB_PATH)
        except Exception as _w2_cutoff_exc:
            log.warning("W2 cutoff columns migration failed: %r", _w2_cutoff_exc)

        # REVISION (2026-07-29, Blocker 5): the manager_lead_cards
        # lead_date-identity REBUILD (CREATE new table / COPY / DROP /
        # RENAME) used to run HERE, at every ordinary startup, wrapped in a
        # bare `except Exception: log.warning(...)` -- a destructive-shaped
        # migration silently attempted (or silently SKIPPED on failure) on
        # every process start, with the process continuing regardless of
        # the outcome. That is exactly the unsafe pattern Blocker 5 exists
        # to remove. Startup now performs ONLY a read-only compatibility
        # check; the rebuild itself moved to a dedicated, explicitly
        # operator-invoked tool (tools/w2_lead_date_migration_runner.py)
        # and is never reachable from this code path. A fresh CREATE TABLE
        # IF NOT EXISTS above already declares the CURRENT (wide) key, so a
        # brand-new database is compatible the instant it's created -- this
        # check only ever fires for a genuine pre-W2 legacy table, and it
        # is DELIBERATELY NOT CAUGHT: an incompatible schema must stop
        # ManagerBot from starting, not be silently logged and ignored.
        storage.w2_assert_lead_cards_schema_compatible(TPILOT_DB_PATH)
        # --- TPILOT W2 ACCESS & DELIVERY 20260729 END ---

        # --- TPILOT MANAGER BOT ACCESS SCHEMA M2.6A 20260601 START ---
        # Additive only. No behavior change in M2.6A.
        # manager_bot_access stores per-manager ManagerBot permissions.
        # manager_access_requests stores pending username/manual access requests.
        con.executescript("""
        CREATE TABLE IF NOT EXISTS manager_bot_access (
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

        CREATE INDEX IF NOT EXISTS idx_manager_bot_access_manager
            ON manager_bot_access(manager_key);

        CREATE INDEX IF NOT EXISTS idx_manager_bot_access_revoked
            ON manager_bot_access(revoked);

        CREATE TABLE IF NOT EXISTS manager_access_requests (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            first_name TEXT NOT NULL DEFAULT '',
            last_name TEXT NOT NULL DEFAULT '',
            matched_manager_key TEXT NOT NULL DEFAULT '',
            match_kind TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            requested_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_manager_access_requests_status
            ON manager_access_requests(status);

        CREATE INDEX IF NOT EXISTS idx_manager_access_requests_manager
            ON manager_access_requests(matched_manager_key);
        """)

        # --- TPILOT MANAGER BOT ACCESS BACKFILL M2.6B 20260601 START ---
        # Additive only. No behavior change in M2.6B.
        # Backfill explicit permission rows for current access_users/access_targets.
        # Existing manager_bot_access rows are preserved because INSERT OR IGNORE is used.
        con.execute("""
        INSERT OR IGNORE INTO manager_bot_access (
            tg_user_id,
            manager_key,
            can_receive_cards,
            can_set_status,
            can_view_stats,
            stats_format,
            allow_custom_period,
            auto_granted,
            granted_by,
            granted_at,
            revoked,
            revoked_by,
            revoked_at,
            created_at,
            updated_at
        )
        SELECT
            au.tg_user_id,
            at.manager_key,
            1 AS can_receive_cards,
            1 AS can_set_status,
            0 AS can_view_stats,
            'light' AS stats_format,
            0 AS allow_custom_period,
            0 AS auto_granted,
            COALESCE(au.created_by, 0) AS granted_by,
            COALESCE(NULLIF(at.created_at, ''), strftime('%Y-%m-%dT%H:%M:%S','now')) AS granted_at,
            0 AS revoked,
            0 AS revoked_by,
            '' AS revoked_at,
            strftime('%Y-%m-%dT%H:%M:%S','now') AS created_at,
            strftime('%Y-%m-%dT%H:%M:%S','now') AS updated_at
        FROM access_users au
        JOIN access_targets at
          ON at.tg_user_id = au.tg_user_id
        WHERE COALESCE(au.is_enabled, 0) = 1
          AND COALESCE(at.manager_key, '') <> ''
        """)
        # --- TPILOT MANAGER BOT ACCESS BACKFILL M2.6B 20260601 END ---

        # --- TPILOT MANAGER BOT ACCESS SCHEMA M2.6A 20260601 END ---


        # --- TPILOT MANAGER BOT EVENT CUTOFF M2.6E 20260602 START ---
        # Additive migration for existing DB.
        # Prevents auto-granted users from receiving historical backlog.
        cols = [r[1] for r in con.execute("PRAGMA table_info(manager_bot_access)").fetchall()]
        if "event_cutoff_id" not in cols:
            con.execute("ALTER TABLE manager_bot_access ADD COLUMN event_cutoff_id INTEGER NOT NULL DEFAULT 0")

        # Existing auto-granted rows with zero cutoff are upgraded to current max event id.
        # Manual/backfilled accesses keep 0 so their existing behavior does not change.
        con.execute("""
        UPDATE manager_bot_access
           SET event_cutoff_id = COALESCE((
                   SELECT MAX(e.id)
                   FROM manager_bot_events e
                   WHERE e.manager_key = manager_bot_access.manager_key
               ), 0),
               updated_at = strftime('%Y-%m-%dT%H:%M:%S','now')
         WHERE COALESCE(auto_granted, 0) = 1
           AND COALESCE(event_cutoff_id, 0) = 0
        """)
        # --- TPILOT MANAGER BOT EVENT CUTOFF M2.6E 20260602 END ---

        # --- TPILOT M2.9A/M2.9C SCREENSHOT CONTROL SCHEMA START ---
        # Additive, idempotent. Never drops columns or rows.
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS manager_screenshot_config(
                manager_key TEXT PRIMARY KEY,
                require_screenshots INTEGER NOT NULL DEFAULT 0,
                mode TEXT NOT NULL DEFAULT 'off',
                reminder_enabled INTEGER NOT NULL DEFAULT 1,
                reminder_time TEXT NOT NULL DEFAULT '16:50',
                last_reminder_date TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS manager_screenshot_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0,
                lead_date TEXT NOT NULL DEFAULT '',
                tg_user_id INTEGER NOT NULL DEFAULT 0,
                status_code TEXT NOT NULL DEFAULT '',
                request_chat_id INTEGER NOT NULL DEFAULT 0,
                request_message_id INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT 'pending',
                kind TEXT NOT NULL DEFAULT 'exact',
                status_date TEXT NOT NULL DEFAULT '',
                screenshot_message_id INTEGER NOT NULL DEFAULT 0,
                screenshot_path TEXT NOT NULL DEFAULT '',
                source_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                uploaded_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS manager_screenshot_requests_lead_idx
                ON manager_screenshot_requests(manager_key, chat_id, lead_date);
            CREATE INDEX IF NOT EXISTS manager_screenshot_requests_reply_idx
                ON manager_screenshot_requests(request_chat_id, request_message_id);
            CREATE INDEX IF NOT EXISTS manager_screenshot_requests_state_idx
                ON manager_screenshot_requests(state, manager_key);
            CREATE INDEX IF NOT EXISTS manager_screenshot_requests_src_date_idx
                ON manager_screenshot_requests(manager_key, source_key, status_date, state);
            CREATE INDEX IF NOT EXISTS manager_screenshot_requests_kind_date_idx
                ON manager_screenshot_requests(manager_key, status_date, kind);
            """
        )

        # M2.9C / M2.9B0: additive ALTER TABLE migrations (idempotent via try/except).
        _ss_schema_alters = [
            "ALTER TABLE manager_screenshot_config ADD COLUMN mode TEXT NOT NULL DEFAULT 'off'",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN kind TEXT NOT NULL DEFAULT 'exact'",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN status_date TEXT NOT NULL DEFAULT ''",
            # M2.9B0: retention tracking columns.
            "ALTER TABLE manager_screenshot_requests ADD COLUMN file_deleted_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN file_deleted_reason TEXT NOT NULL DEFAULT ''",
            # M1: lead-card anchoring, active/replace model, per-lead screenshot reminder tracking.
            "ALTER TABLE manager_screenshot_requests ADD COLUMN card_message_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN card_chat_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN active INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN replaced_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN replaced_by_user_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN replaces_request_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN last_screenshot_reminder_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE manager_screenshot_requests ADD COLUMN last_screenshot_reminder_message_id INTEGER NOT NULL DEFAULT 0",
            # M1: lead-card no-status reminder tracking.
            "ALTER TABLE manager_lead_cards ADD COLUMN status_set_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE manager_lead_cards ADD COLUMN last_status_reminder_at TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE manager_lead_cards ADD COLUMN last_status_reminder_message_id INTEGER NOT NULL DEFAULT 0",
        ]
        for _ddl in _ss_schema_alters:
            try:
                con.execute(_ddl)
            except Exception:
                pass

        # M1: index for card-anchored screenshot upload/reminder lookups.
        try:
            con.execute(
                "CREATE INDEX IF NOT EXISTS manager_screenshot_requests_card_idx "
                "ON manager_screenshot_requests(card_chat_id, card_message_id)"
            )
        except Exception:
            pass

        # M2.9C: mode migration — map require_screenshots to mode for existing rows.
        # Fix: ALTER DEFAULT fills existing rows with 'off', so 'mode empty' check
        # never fired. We must explicitly promote require_screenshots=1 rows to instant
        # even when mode is already 'off' (the ALTER default), unless already set to daily.
        con.execute(
            """
            UPDATE manager_screenshot_config
               SET mode = 'instant'
             WHERE COALESCE(require_screenshots,0)=1
               AND COALESCE(mode,'') IN ('', 'off')
            """
        )
        con.execute(
            """
            UPDATE manager_screenshot_config
               SET mode = 'off'
             WHERE COALESCE(require_screenshots,0)=0
               AND COALESCE(mode,'') = ''
            """
        )
        # M2.9C: backfill source_key from manager_source_links.
        con.execute(
            """
            UPDATE manager_screenshot_requests
               SET source_key = COALESCE(
                   (SELECT source_key FROM manager_source_links WHERE manager_key=manager_screenshot_requests.manager_key LIMIT 1),
                   ''
               )
             WHERE COALESCE(source_key,'') = ''
            """
        )
        # M2.9C: backfill status_date from created_at where missing.
        con.execute(
            """
            UPDATE manager_screenshot_requests
               SET status_date = date(created_at)
             WHERE COALESCE(status_date,'') = '' AND COALESCE(created_at,'') <> ''
            """
        )

        # --- TPILOT M2.9A/M2.9C SCREENSHOT CONTROL SCHEMA END ---

        con.commit()
        # M2.13D-1: schedule tables (uses its own connection via storage helper)
        try:
            from storage import ensure_manager_schedule_tables as _sch_init_m213d
            _sch_init_m213d(TPILOT_DB_PATH)
        except Exception as _sch_init_err:
            log.warning("schedule tables init: %r", _sch_init_err)
        # TPILOT TRANSFERS STAGE2: transfer tables (Stage 1 storage helper, idempotent).
        try:
            from storage import ensure_transfer_tables as _tr_init_tables
            _tr_init_tables(TPILOT_DB_PATH)
        except Exception as _tr_init_err:
            log.warning("transfer tables init: %r", _tr_init_err)
        log.info("schema ready")
    finally:
        con.close()


def _access_user(tg_user_id: int) -> Dict[str, Any]:
    if not tg_user_id:
        return {}
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM access_users WHERE tg_user_id=?",
            (int(tg_user_id),),
        ).fetchone()
        return dict(row) if row else {}
    except Exception as exc:
        log.warning("access user read failed uid=%s error=%r", tg_user_id, exc)
        return {}
    finally:
        con.close()


def _access_allowed(tg_user_id: int) -> bool:
    row = _access_user(tg_user_id)
    try:
        return bool(row) and int(row.get("is_enabled") or 0) == 1
    except Exception:
        return False


def _access_targets(tg_user_id: int) -> List[str]:
    if not tg_user_id:
        return []
    con = _connect()
    try:
        rows = con.execute(
            "SELECT manager_key FROM access_targets WHERE tg_user_id=? ORDER BY manager_key ASC",
            (int(tg_user_id),),
        ).fetchall()
        return [_norm_key(r["manager_key"]) for r in rows if _norm_key(r["manager_key"])]
    except Exception as exc:
        log.warning("access targets read failed uid=%s error=%r", tg_user_id, exc)
        return []
    finally:
        con.close()


def _all_manager_keys() -> List[str]:
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT manager_key
            FROM managers
            WHERE COALESCE(manager_key, '') <> ''
              AND COALESCE(status, '') <> 'prepared'
            ORDER BY manager_key ASC
            """
        ).fetchall()
        return [_norm_key(r["manager_key"]) for r in rows if _norm_key(r["manager_key"])]
    except Exception as exc:
        log.warning("manager list read failed error=%r", exc)
        return []
    finally:
        con.close()


def _linked_manager_keys(tg_user_id: int) -> List[str]:
    """UNCHANGED by W2 (2026-07-29) -- deliberately. An earlier draft of
    this patch redirected this function's 'selected'-scope branch to
    manager_bot_access directly; that broke a separate, already-shipped,
    already-tested feature (blocker B-1 / tools/manager_bot_access_grant_
    selftest.py's B10/B11/B12/R-BTN): panel_bot.py's admin-approval flow
    (_mba_approve) can grant access_users+access_targets to a uid WITHOUT
    creating a manager_bot_access row at all (e.g. an approved non-manager
    supervisor) -- that uid is legitimately supposed to keep seeing the
    ManagerBot menu via THIS function, even though (correctly) they never
    receive lead-card deliveries, because _fetch_unsent_events_for_user's
    own JOIN already requires an active manager_bot_access row regardless
    of how this function resolved its candidate keys. Redirecting this
    shared helper would have silently taken the menu away from that
    already-supported class of user. See _poll_loop for the actual W2
    fix (D-02): the delivery candidate-key list there is the UNION of
    this function's result and storage.w2_resolve_delivery_manager_keys,
    so a manager_bot_access grant that access_targets doesn't know about
    yet is still offered to _fetch_unsent_events_for_user -- without
    changing what any OTHER caller of _linked_manager_keys sees."""
    row = _access_user(tg_user_id)
    if not row:
        return []
    scope = str(row.get("scope_mode") or "selected").strip().lower()
    if scope == "all":
        return _all_manager_keys()
    return _access_targets(tg_user_id)


# --- W1 SAFE SELF-DIAGNOSTIC 20260729 START (D-19, D-20, WAVE0 contract 07) -
# Forward-fix W1 (frozen master plan, MASTER_PLAN_FREEZE/08 + WAVE0 contract
# 07_safe_diagnostics_contract.md): /whoami was removed in an earlier round
# and stays removed -- this button is its approved, safer replacement
# (button, not a command, so /start stays clean). Strictly read-only:
# _w1_describe_access never grants, never revokes, never restores access --
# it only reads manager_bot_access.ensure_sync's OWN tables (access_users,
# managers, manager_bot_access, access_targets) via THIS file's existing
# read helpers/_connect(), exactly as _access_user/_access_targets already
# do. Fail-closed (A-2 in the contract): any DB error returns
# status='error', never 'active'. Never restores a revoked/disabled account
# (B7/C6) -- this function does not write anything at all.
def _w1_describe_access(uid: int) -> Dict[str, Any]:
    """Read-only self-diagnostic snapshot for exactly ONE caller's own uid.
    Returns a dict with at minimum a 'status' key in
    {'active','disabled','pending','not_found','identity_conflict','error'}.
    Never raises. Whitelisted output only (contract A-3, privacy correction
    20260729) -- callers must build user-facing text ONLY from the fields
    this function returns, never by dumping a raw DB row. The raw uid
    argument is NEVER included in the returned dict in any state -- it is
    used only to look the caller up, never echoed back."""
    uid = int(uid or 0)
    if not uid:
        return {"status": "error"}
    con = None
    try:
        con = _connect()
        mgr_rows = [
            dict(r) for r in con.execute(
                "SELECT manager_key, display_name, telegram_username, status, "
                "is_enabled, manual_stopped FROM managers WHERE tg_user_id=?",
                (uid,),
            ).fetchall()
        ]
        au_row = con.execute(
            "SELECT is_enabled, scope_mode FROM access_users WHERE tg_user_id=?",
            (uid,),
        ).fetchone()
    except Exception as exc:
        # Privacy correction 20260729 (round 2): never pass the raw uid or
        # the exception's repr/text to the ORDINARY application logger --
        # only the module's own myaccess_audit.log is allowed a pseudonym,
        # and even there only actor_ref, never the exception text (which
        # could contain a query fragment, a path, or a DB value). Log only
        # a non-reversible actor_ref and the exception's CLASS NAME.
        log.warning(
            "[w1-my-access] managers/access_users read failed actor_ref=%s error_class=%s",
            _w1_myaccess_actor_ref(uid), type(exc).__name__,
        )
        return {"status": "error"}
    finally:
        if con is not None:
            con.close()

    if len(mgr_rows) > 1:
        # Two active manager rows resolve to the same tg_user_id -- fail
        # closed with the SAME identity_conflict semantics
        # manager_bot_access_ensure_sync uses; never guess which one.
        return {"status": "identity_conflict"}
    if not mgr_rows:
        return {"status": "not_found"}

    mrow = mgr_rows[0]
    key = _norm_key(mrow.get("manager_key") or "")
    if not key:
        return {"status": "not_found"}

    # access_users.is_enabled=0 is an explicit administrator action -- fail
    # closed, and never show tg_user_id back to a user who is already known
    # to the administrator (same rule _handle_start already follows).
    if au_row is not None and int(au_row["is_enabled"] or 0) != 1:
        return {"status": "disabled", "manager_key": key}

    try:
        con = _connect()
        mba_row = con.execute(
            "SELECT can_receive_cards, revoked, updated_at FROM manager_bot_access "
            "WHERE tg_user_id=? AND manager_key=?",
            (uid, key),
        ).fetchone()
        at_row = con.execute(
            "SELECT 1 FROM access_targets WHERE tg_user_id=? AND manager_key=?",
            (uid, key),
        ).fetchone()
    except Exception as exc:
        # Privacy correction 20260729 (round 2): see the note on the
        # sibling except-block above -- actor_ref + exception class only.
        log.warning(
            "[w1-my-access] manager_bot_access/access_targets read failed actor_ref=%s error_class=%s",
            _w1_myaccess_actor_ref(uid), type(exc).__name__,
        )
        return {"status": "error"}
    finally:
        con.close()

    if mba_row is not None and int(mba_row["revoked"] or 0) != 0:
        # Explicit revocation -- fail closed, never restored by this
        # read-only screen (B7/C6).
        return {"status": "disabled", "manager_key": key}

    in_mba = mba_row is not None
    in_at = at_row is not None
    display_name = str(mrow.get("display_name") or key).strip() or key
    username = str(mrow.get("telegram_username") or "").strip().lstrip("@")

    if not in_mba and not in_at:
        return {"status": "pending", "manager_key": key,
                "display_name": display_name, "username": username}

    source = "both" if (in_mba and in_at) else ("manager_bot_access_only" if in_mba else "access_targets_only")
    return {
        "status": "active",
        "manager_key": key,
        "display_name": display_name,
        "username": username,
        "source": source,
        "updated_at": str((mba_row["updated_at"] if mba_row else "") or ""),
    }


def _w1_my_access_text(info: Dict[str, Any]) -> str:
    """Pure -- builds the user-facing text ONLY from _w1_describe_access's
    whitelisted fields (contract A-3, privacy correction 20260729). Raw
    tg_user_id is NEVER shown, in ANY state, including not_found and
    identity_conflict -- this supersedes the earlier design that showed it
    on those two screens "to hand to an admin"; the admin can identify the
    requester from the Telegram chat itself, so no raw ID needs to appear
    in bot-rendered text."""
    status = str(info.get("status") or "error")
    if status == "active":
        name = str(info.get("display_name") or info.get("manager_key") or "менеджер")
        uname = str(info.get("username") or "").strip()
        username_line = f"🔗 Username: @{uname}" if uname else "🔗 Username: не задан"
        source = str(info.get("source") or "")
        if source == "both":
            lines = [
                "🔎 Мой доступ", "",
                f"👤 Аккаунт: {name}", username_line,
                f"🗝 Ключ менеджера: {info.get('manager_key') or ''}", "",
                "✅ Доступ: активен",
                "📚 Источник: обе таблицы согласованы",
            ]
        else:
            missing = "access_targets" if source == "manager_bot_access_only" else "manager_bot_access"
            lines = [
                "🔎 Мой доступ", "",
                f"👤 Аккаунт: {name}", username_line,
                f"🗝 Ключ менеджера: {info.get('manager_key') or ''}", "",
                "⚠️ Доступ: активен, но настройки рассинхронизированы",
                f"📚 Источник: только {source} (в {missing} записи нет)",
                "❗ Карточки могут не приходить. Сообщите администратору об этом расхождении.",
            ]
        updated_at = str(info.get("updated_at") or "").strip()
        if updated_at:
            lines.append(f"🕐 Синхронизировано: {updated_at}")
        return "\n".join(lines).rstrip()
    if status == "pending":
        return "\n".join([
            "🔎 Мой доступ", "",
            f"🗝 Ключ менеджера: {info.get('manager_key') or ''}", "",
            "⏳ Заявка на доступ ещё не подтверждена администратором.",
        ]).rstrip()
    if status == "disabled":
        return "🚫 Доступ отключён администратором"
    if status in ("not_found", "identity_conflict"):
        return "\n".join([
            "🔎 Мой доступ", "",
            "❌ Учётная запись не найдена", "",
            "Напишите администратору из этого же чата Telegram --",
            "он определит вас по чату, отдельный идентификатор не нужен.",
        ]).rstrip()
    # status == "error" -- fail-closed wording (A-2), same style _handle_start
    # already uses for a transient canon failure. Never "доступ активен".
    return "⚠️ Не удалось проверить доступ. Повторите позже."


# Rate limit (contract §7.8): 5 taps / 5 min for a resolved identity, 3 / 15
# min for an unresolved one (a not_found screen must not become a probing
# surface). In-memory only -- a process restart resetting the window is an
# acceptable, explicitly-scoped trade-off for W1 observability, not a
# security control on its own (the audit log below is the durable record).
_W1_MYACCESS_HITS: Dict[int, List[float]] = {}
_W1_MYACCESS_LIMIT_KNOWN = (5, 5 * 60)
_W1_MYACCESS_LIMIT_UNKNOWN = (3, 15 * 60)

_W1_MYACCESS_AUDIT_LOG = BASE_DIR / "runtime" / "myaccess_audit.log"

# Privacy correction 20260729: the audit log must never contain the raw
# Telegram uid. This namespace string is a fixed, hardcoded domain
# separator for the digest below -- it is NOT a secret and is never itself
# written to the log; it only exists so this log's pseudonyms don't
# collide with any other subsystem's hash of the same uid. Python's
# built-in hash() is intentionally NOT used -- it is randomized per
# process (PYTHONHASHSEED) and would make the same uid produce a
# different, non-stable pseudonym on every restart.
_W1_MYACCESS_ACTOR_NAMESPACE = "tpilot.manager_bot.myaccess_audit.v1"


def _w1_myaccess_actor_ref(uid: int) -> str:
    """Stable, non-reversible pseudonym for a uid, safe to write to a log.
    SHA256(fixed namespace + uid), truncated -- same uid always yields the
    same actor_ref (so repeat taps by one user are still correlatable in
    the log), but the raw uid cannot be recovered from it."""
    digest = hashlib.sha256(
        (_W1_MYACCESS_ACTOR_NAMESPACE + ":" + str(int(uid or 0))).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def _w1_myaccess_audit_log(uid: int, outcome: str, resolved_manager_key: str, *,
                           operation_id: str) -> None:
    """Append-only structured audit line for every '🔎 Мой доступ' tap
    (contract §7.8, privacy correction 20260729). Never raises. Never
    writes the raw uid, the response text, or any secret -- only a
    non-reversible actor_ref pseudonym, outcome, the resolved manager_key
    and an operation_id. This is manager_bot.py's OWN process -- main.py's
    _manager_auth_audit_log lives in a separate running process and is not
    reachable from here; this mirrors the same append-only, always-
    timestamped convention independently, scoped to this file."""
    try:
        ts = datetime.utcnow().replace(microsecond=0).isoformat()
        key = _norm_key(resolved_manager_key or "")
        actor_ref = _w1_myaccess_actor_ref(uid)
        line = (
            f"time_utc={ts}\tevent=my_access\tactor_ref={actor_ref}\t"
            f"manager_key={key}\toutcome={outcome}\toperation_id={operation_id}\n"
        )
        _W1_MYACCESS_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _W1_MYACCESS_AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _w1_myaccess_rate_limited(uid: int, *, known: bool) -> bool:
    import time as _w1_time
    now = _w1_time.monotonic()
    limit, window = _W1_MYACCESS_LIMIT_KNOWN if known else _W1_MYACCESS_LIMIT_UNKNOWN
    hits = _W1_MYACCESS_HITS.setdefault(int(uid or 0), [])
    hits[:] = [t for t in hits if now - t < window]
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


async def _w1_handle_my_access_callback(event) -> None:
    """Callback handler for the '🔎 Мой доступ' button (mb:myaccess:0).
    Invariant A-1: uid is taken ONLY from event.sender_id -- the callback
    payload itself carries no identifier, so there is no path by which this
    button can be used to query another user's access state."""
    try:
        uid = int(getattr(event, "sender_id", 0) or 0)
        if uid <= 0:
            await event.answer()
            return
        op_id = uuid.uuid4().hex[:12]
        # Cheap pre-check so the rate limiter (and audit log) reflect the
        # ACTUAL resolved identity, not a guess -- describe_access itself is
        # read-only and safe to call before the limiter.
        info = _w1_describe_access(uid)
        known = str(info.get("status") or "") in ("active", "pending", "disabled")
        if _w1_myaccess_rate_limited(uid, known=known):
            _w1_myaccess_audit_log(
                uid, "rate_limited", str(info.get("manager_key") or ""),
                operation_id=op_id,
            )
            await event.answer("Слишком часто, попробуйте позже.", alert=True)
            return
        await event.answer()
        _w1_myaccess_audit_log(
            uid, str(info.get("status") or "error"), str(info.get("manager_key") or ""),
            operation_id=op_id,
        )
        await event.respond(_w1_my_access_text(info))
    except Exception as exc:
        # Privacy correction 20260729 (round 2): `uid` may be unbound here
        # if the very first line of the try (int(getattr(event,
        # "sender_id", 0) or 0)) itself raised -- guard with locals().
        _uid_for_log = locals().get("uid")
        _ref_for_log = _w1_myaccess_actor_ref(_uid_for_log) if _uid_for_log else "unresolved"
        log.warning(
            "[w1-my-access] callback failed actor_ref=%s error_class=%s",
            _ref_for_log, type(exc).__name__,
        )
        try:
            await event.answer()
        except Exception:
            pass
# --- W1 SAFE SELF-DIAGNOSTIC 20260729 END -----------------------------------


def _is_private_chat(event) -> bool:
    try:
        return bool(getattr(event, "is_private", False))
    except Exception:
        return False


@client.on(events.NewMessage)
async def on_message(event):
    try:
        if not _is_private_chat(event):
            return

        sender = await event.get_sender()
        tg_user_id = int(getattr(sender, "id", 0) or 0)
        if tg_user_id <= 0:
            return

        # M2.9A/M2.9C media routing (must come before text/pending logic).
        is_media = (getattr(event, "photo", None) is not None or getattr(event, "document", None) is not None)
        if is_media:
            # 1. Reply to an exact ForceReply request (any state check, handles expired/cancelled too).
            if getattr(event, "is_reply", False):
                handled = await _handle_screenshot_reply(event, tg_user_id)
                if handled:
                    return
            # 2. Active batch upload session.
            if _ss_batch_active(tg_user_id):
                await _handle_batch_media(event, tg_user_id)
                return
            # 3. No active session — prompt.
            try:
                await event.respond("Открой /skrin или отправь скрин ответом на запрос.")
            except Exception:
                pass
            return

        text = str(event.raw_text or "").strip()
        cmd = text.split(maxsplit=1)[0].lower() if text else ""

        if cmd == "/start":
            await _handle_start(event, tg_user_id)
            return

        if cmd in ("/stat", "/stats"):
            await _mbstat_handle_command(event, tg_user_id, text)
            return

        if cmd == "/skrin":
            await _handle_skrin_command(event, tg_user_id)
            return

        if cmd == "/skrin_done":
            await _handle_skrin_done_command(event, tg_user_id)
            return

        if cmd == "/refresh_cards":
            await _handle_refresh_cards_command(event, tg_user_id)
            return

        # TPILOT TRANSFERS STAGE2: reply to an open transfer_drafts ForceReply prompt.
        # Restart-safe: matched by (request_chat_id, request_message_id, tg_user_id) in the DB,
        # not in-memory state. Returns False fast for any unrelated reply.
        if getattr(event, "is_reply", False) and text:
            handled = await _tr_handle_text_reply(event, tg_user_id, text)
            if handled:
                return

        if tg_user_id in _MBSTAT_PENDING and not text.startswith("/"):
            await _mbstat_handle_pending_period(event, tg_user_id, text)
            return

        # M2.1 skeleton intentionally ignores all other private messages.
        return
    except Exception as exc:
        log.exception("message handler failed: %r", exc)
        try:
            if _is_private_chat(event):
                await event.respond("Internal error. Try again later.")
        except Exception:
            pass


@client.on(events.CallbackQuery)
async def on_callback(event):
    try:
        data = bytes(event.data or b"")
        # ss: callbacks (screenshot batch) — checked BEFORE CALLBACK_PREFIX gate
        # because these use ss: namespace, not mb:
        if data == b"ss:batch_done":
            tg_uid = int(getattr(event, "sender_id", 0) or 0)
            log.info("batch done callback uid=%s", tg_uid)
            await event.answer()
            await _handle_skrin_done_command(event, tg_uid)
            return
        if data.startswith(b"ss:batch_mgr:"):
            tg_uid = int(getattr(event, "sender_id", 0) or 0)
            cb_mk = data[len(b"ss:batch_mgr:"):].decode("utf-8", "ignore").strip()
            log.info("batch manager callback uid=%s manager=%s", tg_uid, cb_mk)
            await _handle_batch_mgr_callback(event, cb_mk, tg_uid)
            return
        # cls: callbacks (closer self-service settings) — checked BEFORE the
        # CALLBACK_PREFIX gate, same as ss:, so the dedicated _cls_callback
        # handler owns the answer/toast for its own namespace instead of this
        # generic handler answering (or no-op'ing) first.
        if data.startswith(b"cls:"):
            return
        if not data.startswith(CALLBACK_PREFIX):
            await event.answer()
            return

        if data.startswith(b"mb:s:"):
            await _handle_status_callback(event, data)
            return

        if data.startswith(b"mb:e:"):
            await _handle_edit_status_callback(event, data)
            return

        if data.startswith(b"mb:b:"):
            await _handle_back_status_callback(event, data)
            return

        if data.startswith(b"mb:rs:"):
            await _handle_replace_screenshot_callback(event, data)
            return

        if data.startswith(b"mb:up:"):
            await _handle_screenshot_upload_prompt_callback(event, data)
            return

        if data.startswith(b"mb:st:"):
            await _mbstat_handle_callback(event, data)
            return

        if data.startswith(b"mb:sch:"):
            await _sched_handle_callback(event, data)
            return

        if data.startswith(b"mb:tr:"):
            await _tr_handle_callback(event, data)
            return

        if data == b"mb:refresh_cards":
            tg_uid = int(getattr(event, "sender_id", 0) or 0)
            await event.answer("\u041e\u0431\u043d\u043e\u0432\u043b\u044f\u044e...")
            await _handle_refresh_cards_command(event, tg_uid)
            return

        # W1 (D-19, D-20): '\ud83d\udd0e \u041c\u043e\u0439 \u0434\u043e\u0441\u0442\u0443\u043f' -- a NEW, distinct callback under
        # this file's existing mb: namespace (CALLBACK_PREFIX = b"mb:"),
        # never colliding with mb:s:/mb:e:/mb:b:/mb:rs:/mb:up:/mb:st:/
        # mb:sch:/mb:tr: above or with AdminBot's own callback namespaces
        # (menu:/ssc:/wiz:/rw:/relogin:/devlogin:/... in panel_bot.py, a
        # separate bot/client entirely). card_id=0 per the frozen contract
        # (mb:<action>:<card_id>[:<code>]) -- 0 because this action is not
        # about any specific lead card.
        if data.startswith(b"mb:myaccess:"):
            await _w1_handle_my_access_callback(event)
            return

        await event.answer("\u0424\u0443\u043d\u043a\u0446\u0438\u044f \u0435\u0449\u0451 \u043d\u0435 \u0430\u043a\u0442\u0438\u0432\u043d\u0430", alert=False)
    except Exception as exc:
        log.warning("callback handler failed: %r", exc)
        try:
            await event.answer("\u041e\u0448\u0438\u0431\u043a\u0430", alert=True)
        except Exception:
            pass


# --- TPILOT MANAGER BOT CLEAN UI M2.4 20260531 START ---
# Single clean ManagerBot consumer block.
# Russian UI is stored through unicode escapes to avoid Windows encoding damage.
# Buttons:
# liquid, geo, under18, na, trash, clear

STATUS_ACTIONS: Dict[str, Dict[str, str]] = {
    "liq": {
        "status": "liquid",
        "bucket": "liquid",
        "reason": "manual_liquid_rf_18_plus",
        "label": "\u2705 \u041b\u0438\u043a\u0432\u0438\u0434 \u0420\u0424 18+",
    },
    "geo": {
        "status": "nonliquid",
        "bucket": "geo",
        "reason": "manual_geo",
        "label": "\U0001f30d GEO",
    },
    "u18": {
        "status": "nonliquid",
        "bucket": "under18",
        "reason": "manual_under18",
        "label": "\U0001f51e -18",
    },
    "na": {
        "status": "na",
        "bucket": "na",
        "reason": "manual_na",
        "label": "\u2754 NA",
    },
    "trash": {
        "status": "trash",
        "bucket": "trash",
        "reason": "manual_trash",
        "label": "\U0001f5d1 Trash",
    },
}


def _decode_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(str(row.get("payload_json") or "{}"))
    except Exception:
        return {}


def _db_candidates_for_manager(manager_key: str) -> List[str]:
    mk = _norm_key(manager_key)
    candidates = [TPILOT_DB_PATH]
    if mk:
        candidates.append(str((BASE_DIR / "runtime" / "managers" / mk / f"{mk}.db").resolve()))

    result = []
    seen = set()
    for item in candidates:
        item = str(item or "").strip()
        if item and item not in seen and Path(item).exists():
            seen.add(item)
            result.append(item)
    return result


def _connect_path(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=30000;")
    except Exception:
        pass
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _read_override(manager_key: str, chat_id: int) -> Dict[str, Any]:
    for db_path in _db_candidates_for_manager(manager_key):
        con = _connect_path(db_path)
        try:
            if not _table_exists(con, "lead_status_overrides"):
                continue
            row = con.execute(
                """
                SELECT *
                FROM lead_status_overrides
                WHERE manager_key=? AND chat_id=?
                LIMIT 1
                """,
                (_norm_key(manager_key), int(chat_id)),
            ).fetchone()
            if row:
                return dict(row)
        except Exception:
            pass
        finally:
            con.close()
    return {}


def _read_daily_row(manager_key: str, chat_id: int, lead_date: str) -> Dict[str, Any]:
    for db_path in _db_candidates_for_manager(manager_key):
        con = _connect_path(db_path)
        try:
            if not _table_exists(con, "daily_leads"):
                continue
            row = con.execute(
                """
                SELECT *
                FROM daily_leads
                WHERE manager_key=? AND chat_id=? AND lead_date=?
                LIMIT 1
                """,
                (_norm_key(manager_key), int(chat_id), str(lead_date or "")),
            ).fetchone()
            if row:
                return dict(row)
        except Exception:
            pass
        finally:
            con.close()
    return {}


def _status_ru(status: str) -> str:
    value = str(status or "").strip().lower()
    return {
        "liquid": "\u2705 \u041b\u0438\u043a\u0432\u0438\u0434 \u0420\u0424 18+",
        "geo": "\U0001f30d GEO",
        "under18": "\U0001f51e -18",
        "age_missing": "\u2754 NA",
        "geo_missing": "\u2754 NA",
        "na": "\u2754 NA",
        "na_pending": "\u2754 NA",
        "trash": "\U0001f5d1 Trash",
        "pending": "pending",
        "unknown": "pending",
        "nonliquid": "\u041d\u0435\u043b\u0438\u043a\u0432\u0438\u0434",
    }.get(value, value or "pending")


def _manual_label_from_override(override: Dict[str, Any]) -> str:
    if not override:
        return "\u043d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d"

    bucket = str(override.get("bucket") or "").strip()
    status = str(override.get("status") or "").strip()

    if bucket == "liquid" or status == "liquid":
        return "\u2705 \u041b\u0438\u043a\u0432\u0438\u0434 \u0420\u0424 18+"
    if bucket == "geo":
        return "\U0001f30d GEO"
    if bucket == "under18":
        return "\U0001f51e -18"
    if bucket == "na":
        return "\u2754 NA"
    if bucket == "trash" or status == "trash":
        return "\U0001f5d1 Trash"
    return _status_ru(bucket or status)


def _enabled_access_users() -> List[int]:
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT tg_user_id
            FROM access_users
            WHERE COALESCE(is_enabled, 0) = 1
            ORDER BY tg_user_id ASC
            """
        ).fetchall()
        return [int(r["tg_user_id"]) for r in rows if int(r["tg_user_id"] or 0) > 0]
    except Exception as exc:
        log.warning("enabled access users read failed: %r", exc)
        return []
    finally:
        con.close()


def _fetch_unsent_events_for_user(tg_user_id: int, manager_keys: List[str]) -> List[Dict[str, Any]]:
    if not tg_user_id or not manager_keys:
        return []

    placeholders = ",".join(["?"] * len(manager_keys))
    params: List[Any] = [int(tg_user_id), *manager_keys]

    con = _connect()
    try:
        rows = con.execute(
            f"""
            SELECT e.*
            FROM manager_bot_events e
            LEFT JOIN manager_bot_sent s
              ON s.event_id = e.id
             AND s.tg_user_id = ?
            WHERE s.event_id IS NULL
              AND e.manager_key IN ({placeholders})
            ORDER BY e.id ASC
            LIMIT 30
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("unsent events read failed uid=%s error=%r", tg_user_id, exc)
        return []
    finally:
        con.close()


def _format_lead_card(event_row: Dict[str, Any], override: Dict[str, Any] | None = None) -> str:
    payload = _decode_payload(event_row)

    manager_key = str(event_row.get("manager_key") or payload.get("manager_key") or "")
    chat_id = int(event_row.get("chat_id") or payload.get("chat_id") or 0)
    lead_date = str(event_row.get("lead_date") or payload.get("lead_date") or "")
    username = str(payload.get("username") or "").strip()
    full_name = str(payload.get("full_name") or "").strip()
    phone = str(payload.get("phone") or "").strip()

    override = override if override is not None else _read_override(manager_key, chat_id)

    auto_raw = str(
        payload.get("quality_bucket")
        or payload.get("quality_status")
        or payload.get("status")
        or event_row.get("new_status")
        or "pending"
    ).strip()

    reason = str(
        payload.get("quality_reason")
        or payload.get("nonliquid_reason")
        or payload.get("trash_reason")
        or ""
    ).strip()

    manual_text = _manual_label_from_override(override)
    auto_text = _status_ru(auto_raw)
    final_text = manual_text if override else auto_text

    lines = [
        "\U0001f4cb \u0410\u043d\u043a\u0435\u0442\u0430 \u043b\u0438\u0434\u0430",
        "",
        f"\u041c\u0435\u043d\u0435\u0434\u0436\u0435\u0440: {manager_key}",
        f"\u041a\u043e\u043d\u0442\u0430\u043a\u0442: {('@' + username) if (username and not username.startswith('@')) else (username or full_name or '-')}",
    ]

    if phone:
        lines.append(f"\u0422\u0435\u043b\u0435\u0444\u043e\u043d: {phone}")

    lines.extend(
        [
            "",
            f"\u0410\u0432\u0442\u043e\u0441\u0442\u0430\u0442\u0443\u0441: {auto_text}",
            f"\u0420\u0443\u0447\u043d\u043e\u0439 \u0441\u0442\u0430\u0442\u0443\u0441: {manual_text}",
            f"\u0418\u0442\u043e\u0433\u043e\u0432\u044b\u0439 \u0441\u0442\u0430\u0442\u0443\u0441: {final_text}",
        ]
    )

    if reason:
        lines.append(f"\u041f\u0440\u0438\u0447\u0438\u043d\u0430: {reason}")

    if override and manual_text != auto_text:
        lines.append(f"\u26a0\ufe0f \u041a\u043e\u043d\u0444\u043b\u0438\u043a\u0442: \u0430\u0432\u0442\u043e {auto_text}, \u0440\u0443\u0447\u043d\u043e\u0439 {manual_text}")

    if lead_date:
        lines.append(f"\u0414\u0430\u0442\u0430 \u043b\u0438\u0434\u0430: {lead_date}")

    # M1: screenshot-required/accepted state is shown in the card HEADER (top), not as a
    # footer line, so the card itself is the single anchor for the proof workflow. The
    # manual-status marker is inserted here (in the right header position) when needed;
    # callers' later _with_manual_marker(...) call is a no-op once the marker is present.
    _ss_state = _screenshot_card_state(manager_key, chat_id, lead_date)
    if _ss_state in ("pending", "uploaded"):
        manual_marker = "\u270f\ufe0f \u0421\u0442\u0430\u0442\u0443\u0441 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d \u0432\u0440\u0443\u0447\u043d\u0443\u044e"
        rest = lines[2:]
        if _ss_state == "pending":
            header_block = ["\u26a0\ufe0f\u0412\u0410\u0416\u041d\u041e \u0417\u0410\u0413\u0420\u0423\u0417\u0418\u0422\u042c \u0421\u041a\u0420\u0418\u041d\u0428\u041e\u0422\u26a0\ufe0f"]
            if override:
                header_block.append(manual_marker)
        else:
            header_block = ["\U0001f4cb \u0410\u043d\u043a\u0435\u0442\u0430 \u043b\u0438\u0434\u0430", "\u2705 \u0421\u043a\u0440\u0438\u043d \u043f\u0440\u0438\u043d\u044f\u0442"]
            if override:
                header_block += ["", manual_marker]
        lines = header_block + [""] + rest

    return "\n".join(lines).strip()


def _manual_buttons(card_id: int):
    return [
        [
            Button.inline("\u2705 \u041b\u0438\u043a\u0432\u0438\u0434 \u0420\u0424 18+", f"mb:s:{card_id}:liq".encode("utf-8")),
        ],
        [
            Button.inline("\U0001f30d GEO", f"mb:s:{card_id}:geo".encode("utf-8")),
            Button.inline("\U0001f51e -18", f"mb:s:{card_id}:u18".encode("utf-8")),
        ],
        [
            Button.inline("\u2754 NA", f"mb:s:{card_id}:na".encode("utf-8")),
            Button.inline("\U0001f5d1 Trash", f"mb:s:{card_id}:trash".encode("utf-8")),
        ],
        [
            Button.inline("\u21a9\ufe0f \u0421\u0431\u0440\u043e\u0441", f"mb:s:{card_id}:clear".encode("utf-8")),
        ],
    ]


def _upsert_card_placeholder(event_row: Dict[str, Any], tg_user_id: int) -> int:
    payload = _decode_payload(event_row)
    now = _now_iso()

    chat_id = int(event_row.get("chat_id") or payload.get("chat_id") or 0)
    manager_key = _norm_key(event_row.get("manager_key") or payload.get("manager_key") or "")
    lead_date = str(event_row.get("lead_date") or payload.get("lead_date") or "").strip()

    if not tg_user_id or not chat_id or not manager_key:
        return 0

    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO manager_lead_cards(
                tg_user_id, bot_chat_id, message_id, chat_id, manager_key, lead_date,
                last_status_shown, last_bucket_shown, last_manual_flag,
                last_action_at, created_at, updated_at
            )
            VALUES (?, ?, 0, ?, ?, ?, '', '', 0, '', ?, ?)
            ON CONFLICT(tg_user_id, chat_id, manager_key, lead_date)
            DO UPDATE SET
                bot_chat_id=excluded.bot_chat_id,
                updated_at=excluded.updated_at
            """,
            (int(tg_user_id), int(tg_user_id), int(chat_id), manager_key, lead_date, now, now),
        )
        row = con.execute(
            """
            SELECT id
            FROM manager_lead_cards
            WHERE tg_user_id=? AND chat_id=? AND manager_key=? AND lead_date=?
            LIMIT 1
            """,
            (int(tg_user_id), int(chat_id), manager_key, lead_date),
        ).fetchone()
        con.commit()
        return int(row["id"] or 0) if row else 0
    except Exception as exc:
        log.warning("card placeholder failed actor_ref=%s error_class=%s",
                    _w2_actor_ref(tg_user_id), type(exc).__name__)
        return 0
    finally:
        con.close()


def _save_card_and_sent(
    event_row: Dict[str, Any], tg_user_id: int, bot_chat_id: int, message_id: int, card_id: int,
    *, fallback_used: bool = False,
) -> bool:
    """R1B/F-14 (plan section 23-F, "start without a receipt journal"): this
    runs AFTER Telegram has already confirmed delivery, so a write failure
    here must never be silently swallowed as if nothing happened -- the old
    behavior (broad except, no return value, caller never checked) let a
    locked-DB failure leave the manager_bot_sent row stuck at 'sending' until
    _mb_recover_stale_claims flips it to 'failed' ~120s later, and
    _fetch_unsent_events_for_user would then genuinely resend an
    already-delivered card. Now: bounded retry (3 attempts, small jitter)
    against a transient lock; if that's exhausted, a single-column degraded
    write flips send_status to 'sent_unpersisted' -- a state structurally
    excluded from BOTH resend paths (_fetch_unsent_events_for_user only
    selects NULL/'failed'; _mb_recover_stale_claims only selects 'sending'),
    exactly like the already-existing 'sent'/'dead' terminal states, so it
    can never be resent even though the full card metadata (message_id,
    buttons state) may not have been persisted. Returns True iff EITHER the
    full write or the degraded write succeeded; False only if a genuinely
    new manager_bot_sent row could not be established at all (should not
    happen in practice -- _mb_claim_event always inserts a 'sending' row
    before send is attempted, so the degraded UPDATE has a row to target)."""
    payload = _decode_payload(event_row)
    now = _now_iso()

    event_id = int(event_row.get("id") or 0)
    chat_id = int(event_row.get("chat_id") or payload.get("chat_id") or 0)
    manager_key = _norm_key(event_row.get("manager_key") or payload.get("manager_key") or "")
    lead_date = str(event_row.get("lead_date") or payload.get("lead_date") or "").strip()

    status = str(payload.get("quality_status") or payload.get("status") or event_row.get("new_status") or "")
    bucket = str(payload.get("quality_bucket") or "")
    manual_flag = int(payload.get("manual_status_override") or 0)

    if not event_id or not tg_user_id:
        return False

    last_exc: Any = None
    for _attempt in range(3):
        con = _connect()
        try:
            if card_id:
                con.execute(
                    """
                    UPDATE manager_lead_cards
                    SET bot_chat_id=?, message_id=?, lead_date=?, last_status_shown=?,
                        last_bucket_shown=?, last_manual_flag=?, updated_at=?
                    WHERE id=? AND tg_user_id=?
                    """,
                    (int(bot_chat_id), int(message_id), lead_date, status, bucket, manual_flag, now, int(card_id), int(tg_user_id)),
                )

            # Forward-fix Phase 3 (post-incident review 2026-07-26): was
            # INSERT OR IGNORE, which meant a SUCCESSFUL delivery after one or
            # more prior FAILED attempts (see _mark_send_attempt, which inserts
            # a placeholder row with send_status='failed') would silently keep
            # the OLD 'failed' row forever -- IGNORE never fires the UPDATE
            # path, so the event would look permanently undelivered even after
            # it truly was. Now an UPSERT: a fresh success always flips
            # send_status back to 'sent' and refreshes sent_at/fallback_used,
            # regardless of how many attempts it took. attempts/last_error_class
            # are left untouched on success -- they remain as the historical
            # record of how many tries it took, never reset to look as if the
            # first attempt always succeeded.
            con.execute(
                """
                INSERT INTO manager_bot_sent(
                    tg_user_id, event_id, sent_at, attempts, next_attempt_at, send_status, last_error_class, fallback_used
                ) VALUES (?, ?, ?, 0, '', 'sent', '', ?)
                ON CONFLICT(tg_user_id, event_id) DO UPDATE SET
                    sent_at=excluded.sent_at,
                    send_status='sent',
                    next_attempt_at='',
                    fallback_used=CASE WHEN excluded.fallback_used=1 THEN 1 ELSE manager_bot_sent.fallback_used END
                """,
                (int(tg_user_id), event_id, now, 1 if fallback_used else 0),
            )
            con.commit()
            return True
        except sqlite3.OperationalError as exc:
            last_exc = exc
            con.close()
            time.sleep(random.uniform(0.05, 0.15))
            continue
        except Exception as exc:
            log.warning("save card/sent failed actor_ref=%s event_id=%s error_class=%s",
                        _w2_actor_ref(tg_user_id), event_id, type(exc).__name__)
            return False
        finally:
            try:
                con.close()
            except Exception:
                pass

    # Retries exhausted -- degraded minimal write. _mb_claim_event already
    # inserted a 'sending' row for (tg_user_id, event_id) before the send was
    # attempted, so this is an UPDATE (not an upsert) targeting that row.
    try:
        con = _connect()
        try:
            con.execute(
                """
                UPDATE manager_bot_sent
                SET send_status='sent_unpersisted', sent_at=?, last_error_class='PersistDeferred'
                WHERE tg_user_id=? AND event_id=? AND send_status <> 'sent'
                """,
                (now, int(tg_user_id), event_id),
            )
            con.commit()
        finally:
            con.close()
        log.warning(
            "save card/sent degraded to sent_unpersisted actor_ref=%s event_id=%s error_class=%s",
            _w2_actor_ref(tg_user_id), event_id, type(last_exc).__name__ if last_exc else "",
        )
        return True
    except Exception as exc2:
        log.warning("save card/sent fully failed, no state persisted actor_ref=%s event_id=%s error_class=%s",
                    _w2_actor_ref(tg_user_id), event_id, type(exc2).__name__)
        return False


async def _send_event_to_user(event_row: Dict[str, Any], tg_user_id: int) -> None:
    card_id = _upsert_card_placeholder(event_row, int(tg_user_id))
    text = _format_lead_card(event_row)
    base_buttons = _manual_buttons(card_id) if card_id else None
    buttons = _with_copy_button(base_buttons, _ss_username_from_event_row(event_row))

    try:
        msg = await client.send_message(int(tg_user_id), text, buttons=buttons)
    except Exception as exc:
        log.warning("card send with copy button failed, retry plain: %r", exc)
        msg = await client.send_message(int(tg_user_id), text, buttons=base_buttons)
    message_id = int(getattr(msg, "id", 0) or 0)

    _save_card_and_sent(event_row, int(tg_user_id), int(tg_user_id), message_id, card_id)

    log.info(
        "lead card sent uid=%s event_id=%s manager=%s chat_id=%s msg_id=%s card_id=%s",
        tg_user_id, event_row.get("id"), event_row.get("manager_key"), event_row.get("chat_id"), message_id, card_id,
    )


def _card_by_id(card_id: int, tg_user_id: int) -> Dict[str, Any]:
    con = _connect()
    try:
        row = con.execute(
            """
            SELECT *
            FROM manager_lead_cards
            WHERE id=? AND tg_user_id=?
            LIMIT 1
            """,
            (int(card_id), int(tg_user_id)),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


def _check_throttle(card: Dict[str, Any], seconds: int = 20) -> int:
    raw = str(card.get("last_action_at") or "").strip()
    if not raw:
        return 0
    try:
        last = datetime.fromisoformat(raw)
        diff = (datetime.utcnow() - last).total_seconds()
        if diff < seconds:
            return max(1, int(seconds - diff))
    except Exception:
        return 0
    return 0


def _touch_card_action(card_id: int, tg_user_id: int) -> None:
    """R1A1-H (F-27): pure bookkeeping, explicitly non-authoritative. Called
    AFTER the real status write has already committed (see _apply_manual_status)
    -- a failure here (e.g. database is locked) must never propagate and turn an
    already-successful status write into a reported failure for the caller."""
    try:
        con = _connect()
        try:
            now = _now_iso()
            con.execute(
                """
                UPDATE manager_lead_cards
                SET last_action_at=?, updated_at=?
                WHERE id=? AND tg_user_id=?
                """,
                (now, now, int(card_id), int(tg_user_id)),
            )
            con.commit()
        finally:
            con.close()
    except Exception as exc:
        log.warning("touch card action failed (non-authoritative, ignored) card_id=%s tg_user_id=%s error=%r", card_id, tg_user_id, exc)


async def _card_mark_status_set(card: Dict[str, Any], tg_user_id: int, set_now: bool) -> None:
    """M1: mark a card's status as set (stops the no-status reminder) or cleared (resumes it).

    When setting, also delete the rolling no-status reminder message (if any) and clear its id.
    Only ever deletes a stored bot-reminder id — never the lead card or a screenshot."""
    card_id = int(card.get("id") or 0)
    if not card_id:
        return
    now = _now_iso()
    prev_reminder_id = int(card.get("last_status_reminder_message_id") or 0)
    bot_chat_id = int(card.get("bot_chat_id") or tg_user_id or 0)
    con = _connect()
    try:
        if set_now:
            # Set status_set_at only if not already set; drop the rolling no-status reminder.
            con.execute(
                """
                UPDATE manager_lead_cards
                SET status_set_at=CASE WHEN COALESCE(status_set_at,'')='' THEN ? ELSE status_set_at END,
                    last_status_reminder_message_id=0, updated_at=?
                WHERE id=? AND tg_user_id=?
                """,
                (now, now, card_id, int(tg_user_id)),
            )
        else:
            # Cleared: resume reminders by blanking status_set_at and the rolling reminder.
            con.execute(
                """
                UPDATE manager_lead_cards
                SET status_set_at='', last_status_reminder_message_id=0, updated_at=?
                WHERE id=? AND tg_user_id=?
                """,
                (now, card_id, int(tg_user_id)),
            )
        con.commit()
    finally:
        con.close()
    if set_now and prev_reminder_id:
        await _safe_delete_message(bot_chat_id, prev_reminder_id)


# --- TPILOT RECONNECT SELF-HEAL M2.6F START ---
def _selfheal_restore_daily_lead(target_db_path: str, manager_key: str, chat_id: int, lead_date: str) -> bool:
    """Rebuild a missing daily_leads row from manager_bot_events.payload_json in central DB.
    Returns True if a row was successfully inserted. Idempotent (INSERT OR IGNORE)."""
    if not target_db_path or not manager_key or not chat_id or not lead_date:
        return False
    if target_db_path == TPILOT_DB_PATH:
        return False
    src_con = _connect()
    try:
        row = src_con.execute(
            """
            SELECT payload_json FROM manager_bot_events
            WHERE manager_key=? AND chat_id=? AND lead_date=? AND event_type='lead_card'
            ORDER BY id DESC LIMIT 1
            """,
            (_norm_key(manager_key), int(chat_id), str(lead_date)),
        ).fetchone()
        if not row:
            return False
        try:
            payload = json.loads(str(row["payload_json"] or "{}") or "{}")
        except Exception:
            payload = {}
    finally:
        src_con.close()
    now = _now_iso()
    first_seen_utc = str(payload.get("first_seen_utc") or now)
    con = _connect_path(target_db_path)
    try:
        con.execute(
            """
            INSERT OR IGNORE INTO daily_leads(
                manager_key, chat_id, lead_date, first_seen_utc, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (_norm_key(manager_key), int(chat_id), str(lead_date), first_seen_utc, now, now),
        )
        con.commit()
        log.info(
            "[selfheal] restored daily_leads manager=%s chat_id=%s lead_date=%s db=%s",
            manager_key, chat_id, lead_date, target_db_path,
        )
        return True
    except Exception as exc:
        log.warning(
            "[selfheal] restore failed manager=%s chat_id=%s lead_date=%s error=%r",
            manager_key, chat_id, lead_date, exc,
        )
        return False
    finally:
        con.close()
# --- TPILOT RECONNECT SELF-HEAL M2.6F END ---


def _write_manual_status_to_db(db_path: str, card: Dict[str, Any], code: str, tg_user_id: int) -> bool:
    manager_key = _norm_key(card.get("manager_key") or "")
    chat_id = int(card.get("chat_id") or 0)
    lead_date = str(card.get("lead_date") or "").strip()
    now = _now_iso()

    if not manager_key or not chat_id or not lead_date:
        return False

    # B-4 correction (2026-08-12 independent review): _connect_path used to be
    # called OUTSIDE this try block, so sqlite3.connect() raising (e.g. a missing
    # parent directory) propagated an unhandled OperationalError all the way out
    # of _apply_manual_status. sqlite3.connect() also silently CREATES an empty
    # file for a path whose parent directory exists but whose file doesn't -- so
    # a resolved-but-never-created authoritative DB could leak a stray file.
    #
    # B-4 defense-in-depth (2026-08-12, second independent review): the guard now
    # lives in the helper itself, not only at the _apply_manual_status boundary.
    # Every caller of this function is a manual-status write into an EXISTING
    # lead store (the authoritative target and the existence-filtered
    # _db_candidates_for_manager mirrors) -- none of them has a create-the-DB
    # contract, so refusing a nonexistent file here cannot break a legitimate
    # caller, and it closes the TOCTOU window between the caller's existence
    # check and this connect.
    try:
        if not os.path.exists(db_path):
            log.warning("manual status skipped: db file does not exist db=%s card=%s code=%s", db_path, card.get("id"), code)
            return False
    except Exception:
        return False
    try:
        con = _connect_path(db_path)
    except Exception as exc:
        log.warning("manual status connect failed db=%s card=%s code=%s error=%r", db_path, card.get("id"), code, exc)
        return False
    try:
        if not _table_exists(con, "daily_leads"):
            return False

        old = con.execute(
            """
            SELECT *
            FROM daily_leads
            WHERE manager_key=? AND chat_id=? AND lead_date=?
            LIMIT 1
            """,
            (manager_key, chat_id, lead_date),
        ).fetchone()
        if not old:
            if db_path != TPILOT_DB_PATH:
                try:
                    _selfheal_restore_daily_lead(db_path, manager_key, chat_id, lead_date)
                    old = con.execute(
                        "SELECT * FROM daily_leads WHERE manager_key=? AND chat_id=? AND lead_date=? LIMIT 1",
                        (manager_key, chat_id, lead_date),
                    ).fetchone()
                except Exception:
                    pass
            if not old:
                return False

        old = dict(old)
        old_status = str(old.get("status") or "")
        old_bucket = str(old.get("quality_bucket") or "")
        old_reason = str(old.get("quality_reason") or old.get("nonliquid_reason") or "")

        if code == "clear":
            new_status = str(old.get("status") or "")
            new_bucket = str(old.get("quality_bucket") or "")
            new_reason = "manual_clear"

            con.execute(
                """
                UPDATE daily_leads
                SET manual_status_override=0,
                    manual_status_reason='',
                    manual_status_by='',
                    manual_status_at='',
                    updated_at=?
                WHERE manager_key=? AND chat_id=? AND lead_date=?
                """,
                (now, manager_key, chat_id, lead_date),
            )

            if _table_exists(con, "lead_status_overrides"):
                con.execute(
                    "DELETE FROM lead_status_overrides WHERE chat_id=? AND manager_key=?",
                    (chat_id, manager_key),
                )

        else:
            action = STATUS_ACTIONS.get(code)
            if not action:
                return False

            status = action["status"]
            bucket = action["bucket"]
            reason = action["reason"]

            con.execute(
                """
                UPDATE daily_leads
                SET manual_status_override=1,
                    manual_status_reason=?,
                    manual_status_by=?,
                    manual_status_at=?,
                    quality_status=?,
                    quality_bucket=?,
                    quality_reason=?,
                    quality_confidence='manual',
                    quality_source='manager_bot',
                    quality_checked_at=?,
                    quality_version='manager_bot_m24',
                    status=?,
                    nonliquid_reason=?,
                    trash=CASE WHEN ?='trash' THEN 1 ELSE trash END,
                    trash_reason=CASE WHEN ?='trash' THEN ? ELSE trash_reason END,
                    updated_at=?
                WHERE manager_key=? AND chat_id=? AND lead_date=?
                """,
                (
                    reason, str(tg_user_id), now, status, bucket, reason, now, status,
                    "" if status == "liquid" else reason,
                    status, status, reason, now, manager_key, chat_id, lead_date,
                ),
            )

            if _table_exists(con, "lead_status_overrides"):
                con.execute(
                    """
                    INSERT OR REPLACE INTO lead_status_overrides(
                        chat_id, manager_key, status, bucket, reason, comment, updated_by, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (chat_id, manager_key, status, bucket, reason, action["label"], str(tg_user_id), now),
                )

            new_status = status
            new_bucket = bucket
            new_reason = reason

        if _table_exists(con, "lead_status_audit"):
            con.execute(
                """
                INSERT INTO lead_status_audit(
                    manager_key, chat_id, lead_date, old_status, new_status,
                    old_bucket, new_bucket, old_reason, new_reason,
                    source, confidence, raw_text, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manager_bot', 'manual', ?, ?)
                """,
                (
                    manager_key, chat_id, lead_date, old_status, new_status, old_bucket, new_bucket,
                    old_reason, new_reason, f"tg_user_id={tg_user_id}; code={code}", now,
                ),
            )

        con.commit()
        return True
    except Exception as exc:
        log.warning("manual status write failed db=%s card=%s code=%s error=%r", db_path, card.get("id"), code, exc)
        return False
    finally:
        con.close()


def _mb_registry_lookup_status(manager_key: str) -> Tuple[str, Dict[str, Any]]:
    """ADV-14 (2026-08-12, third independent review): a THREE-state registry
    lookup, used ONLY by the authoritative-write resolution boundary.

    `_mbstat_manager_row` collapses two very different outcomes into the same
    empty dict:
      * the `managers` row genuinely does not exist (deleted manager), and
      * the registry could not be read at all (central DB locked/corrupt, or
        carrying no `managers` table).

    For a read-only renderer that ambiguity is an acceptable fail-soft. For the
    manual-status WRITER it is not. An independent review reproduced the exact
    forbidden state: with `managers.db_path` = DB_A and a canonical DB_B both
    present, a registry read that failed AT WRITE TIME made the resolver fall
    back to DB_B, the write succeeded there and reported True -- and once the
    registry was readable again the live /stat path resolved DB_A and showed
    stale data. Registry state UNKNOWN must never be read as "no such manager".

    Returns (state, row), state being exactly one of:
      "found"       -- query succeeded, the row exists (row = registry row)
      "not_found"   -- query succeeded, no such manager_key (row = {})
      "unavailable" -- registry unreadable; NOTHING may be inferred from it

    `_mbstat_manager_row` itself is deliberately left untouched: its many
    read-only callers must keep their current fail-soft behavior."""
    mk = _norm_key(manager_key)
    if not mk:
        return "not_found", {}
    try:
        con = _connect()
    except Exception as exc:
        log.warning("authoritative registry connect failed manager=%s error=%r", mk, exc)
        return "unavailable", {}
    try:
        if not _table_exists(con, "managers"):
            # A reachable control-plane DB carrying no registry at all is an
            # unusable registry -- NOT proof that this manager was deleted.
            log.warning("authoritative registry has no managers table manager=%s", mk)
            return "unavailable", {}
        r = con.execute("SELECT * FROM managers WHERE manager_key=? LIMIT 1", (mk,)).fetchone()
        if r is None:
            return "not_found", {}
        return "found", dict(r)
    except Exception as exc:
        log.warning("authoritative registry read failed manager=%s error=%r", mk, exc)
        return "unavailable", {}
    finally:
        try:
            con.close()
        except Exception:
            pass


def _mb_stats_candidates_from_registry_row(manager_key: str, row: Dict[str, Any]) -> List[str]:
    """ADV-14: `_mbstat_db_candidates_for_manager`'s exact candidate list, built
    from an ALREADY-VALIDATED registry row instead of re-querying the registry.

    The degraded branch of `_mb_stats_authoritative_db_path` used to call
    `_mbstat_db_candidates_for_manager`, which performs its own
    `_mbstat_manager_row` lookup and swallows failures -- a second read that
    could silently drop `db_path` and re-introduce the very ambiguity ADV-14
    closes. One validated read, one candidate list."""
    mk = _norm_key(manager_key)
    candidates: List[str] = []
    if mk:
        raw_db_path = str((row or {}).get("db_path") or "").strip()
        if raw_db_path:
            p = Path(raw_db_path)
            if not p.is_absolute():
                p = (BASE_DIR / raw_db_path).resolve()
            candidates.append(str(p))
        candidates.append(str((BASE_DIR / "runtime" / "managers" / mk / f"{mk}.db").resolve()))

    result: List[str] = []
    seen = set()
    for item in candidates:
        item = str(item or "").strip()
        if item and item not in seen and Path(item).exists():
            seen.add(item)
            result.append(item)
    return result


def _mb_stats_authoritative_row(manager_key: str) -> Tuple[str, Dict[str, Any]]:
    """R1A1-E B-3 FINAL (2026-08-12, second independent review): the registry row
    EXACTLY as the LIVE ManagerBot statistics path builds it.

    ADV-14 (third independent review): now returns `(registry_state, row)` so
    the caller can tell a genuinely absent manager from an unreadable registry.
    A deleted manager (`"not_found"`) still gets the injected-key row and keeps
    resolving its retained canonical DB -- that behavior is preserved exactly.

    This is the whole root cause of the proven false success. The live /stat path
    is `_mbstat_manager_rows_for_user` -> `_mbstat_light_body`/`_mbstat_window_body`
    (the ACTIVE defs are the stats_engine-wired ones near the end of this file,
    which shadow the earlier pre-Stage-4D bodies) -> `se_light_body`/`se_window_body`
    with their DEFAULT provider -> `se_leads_for_manager(row, dates)` ->
    `se_manager_db_path(row)`.

    `_mbstat_manager_rows_for_user` does:
        row = dict(_mbstat_manager_row(mk) or {})
        row["manager_key"] = mk          # <-- ALWAYS injected
    so when a manager's `managers` row is gone (deletion is a hard
    `DELETE FROM managers WHERE manager_key=?`, main.py), the reader still
    resolves the canonical `runtime/managers/<mk>/<mk>.db` from the injected key
    and keeps reporting that manager's retained statistics.

    The previous correction passed the BARE `_mbstat_manager_row(mk)` result to
    `se_manager_db_path`. For a deleted/missing manager that is `{}`, so the
    resolver returned "" -- and the caller then fell back to a mirror-only write
    that reported success while the DB the reader actually consumes never got the
    status. Building the row here the same way the reader does removes the
    divergence at its source instead of adding a fourth resolver."""
    mk = _norm_key(manager_key)
    if not mk:
        return "not_found", {}
    state, reg_row = _mb_registry_lookup_status(mk)
    if state == "unavailable":
        # Nothing may be inferred -- do NOT synthesize an injected-key row here,
        # or the caller would resolve the canonical fallback from a guess.
        return state, {}
    row = dict(reg_row or {})
    row["manager_key"] = mk
    return state, row


def _mb_stats_authoritative_db_path(manager_key: str) -> str:
    """The single physical DB the live ManagerBot statistics reader will consume
    for `manager_key` -- i.e. the only legitimate authoritative target for a
    manual status write. Returns "" when no such target can be determined, and
    the caller MUST then fail closed (never a mirror-only success).

    Two live reader modes, both covered with the reader's own semantics:
      * normal: stats_engine imported (Stage 4D wiring active) -> the reader
        resolves via se_manager_db_path on the injected-key row, so we do too;
      * degraded: stats_engine unimportable -> the Stage 4D overrides never took
        effect and the earlier `_mbstat_light_body`/`_mbstat_window_body` bodies
        stay active, which read through `_mbstat_leads_for_manager`: the FIRST
        `_mbstat_db_candidates_for_manager` entry that actually has a
        `daily_leads` table (it `continue`s past candidates without one). We
        reproduce that same first-with-table precedence rather than guessing.

    Mirror writes are unaffected -- `_db_candidates_for_manager` keeps its own
    scope, per its existing comment."""
    mk = _norm_key(manager_key)
    if not mk:
        return ""
    reg_state, row = _mb_stats_authoritative_row(mk)
    if reg_state == "unavailable":
        # ADV-14: the registry state is UNKNOWN, so a configured
        # `managers.db_path` may well exist and be exactly the DB /stat reads.
        # Guessing the canonical fallback here is precisely how a write lands in
        # the wrong file while the caller is told it succeeded. Fail closed --
        # nothing is written anywhere and the same status stays retryable once
        # the registry is readable again.
        log.warning("authoritative stats db unresolved: registry unavailable manager=%s", mk)
        return ""
    try:
        import stats_engine as _mb_se_r1a1fix
    except Exception:
        _mb_se_r1a1fix = None
    if _mb_se_r1a1fix is not None:
        try:
            return str(_mb_se_r1a1fix.se_manager_db_path(row) or "")
        except Exception as exc:
            log.warning("authoritative stats db resolve failed manager=%s error=%r", mk, exc)
            return ""
    # Degraded mode: mirror `_mbstat_leads_for_manager`'s candidate precedence,
    # from the registry row already validated above (never a second lookup).
    for db_path in _mb_stats_candidates_from_registry_row(mk, row):
        try:
            con = _connect_path(db_path)
        except Exception:
            continue
        try:
            if _table_exists(con, "daily_leads"):
                return db_path
        except Exception:
            pass
        finally:
            con.close()
    return ""


def _mb_authoritative_override(manager_key: str, chat_id: int) -> Dict[str, Any]:
    """R1A1-E B-3 FINAL: the manual override as the AUTHORITATIVE stats DB has it.

    `_read_override` scans `_db_candidates_for_manager`, which puts the central
    control-plane DB FIRST. A status that only ever reached that mirror (legacy
    rows written before the authoritative-first ordering, or a hand-edited
    central DB) therefore made the callback answer "Этот статус уже установлен"
    and return WITHOUT ever writing the DB statistics are read from -- a
    mirror-only short circuit. The same-status gate must consult the
    authoritative DB only. Display/rendering keeps using `_read_override`."""
    mk = _norm_key(manager_key)
    if not mk:
        return {}
    db_path = _mb_stats_authoritative_db_path(mk)
    if not db_path or not os.path.exists(db_path):
        return {}
    try:
        con = _connect_path(db_path)
    except Exception:
        return {}
    try:
        if not _table_exists(con, "lead_status_overrides"):
            return {}
        row = con.execute(
            "SELECT * FROM lead_status_overrides WHERE manager_key=? AND chat_id=? LIMIT 1",
            (mk, int(chat_id)),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


def _apply_manual_status(card: Dict[str, Any], code: str, tg_user_id: int) -> bool:
    """R1A1-E (F-23) + B-2/B-4 correction (2026-08-12 independent review):
    the authoritative stats DB is now resolved and written FIRST, and its
    result is the ONLY thing that decides success -- mirrors are written only
    AFTER an authoritative commit, best-effort, and never flip the result.

    B-2: the previous ordering (mirrors first, authoritative second, success
    gated on authoritative alone) created a retry dead-end -- a mirror-only
    write on authoritative failure poisoned _read_override with a status the
    stats-authoritative DB never confirmed; on retry, _same_manual_status saw
    that poisoned override and short-circuited with "already installed"
    without ever calling _apply_manual_status again. Now: no confirmed
    authoritative write => nothing is written anywhere, no throttle consumed
    (_touch_card_action is not called), so the exact same status remains
    retryable once the authoritative DB is reachable again.

    B-4: an authoritative path that resolves but does not exist on disk is an
    explicit failure, not a target to fabricate via sqlite3.connect().

    B-3 FINAL (2026-08-12, second independent review): the hard business
    invariant is now unconditional --

        _apply_manual_status(...) is True
        =>  the live ManagerBot statistics read path observes the new status.

    The previous correction still had one escape hatch: when the authoritative
    target could not be resolved it fell back to legacy mirror-only semantics
    and returned True. An independent review proved that path produces exactly
    the forbidden state (central mirror updated, the DB /stat reads untouched,
    user told the status was saved) for any manager whose `managers` row is
    gone. That branch is REMOVED: unresolvable authoritative target => False,
    fail closed. Mirror-only persistence never counts as success."""
    manager_key = str(card.get("manager_key") or "")
    authoritative_db = _mb_stats_authoritative_db_path(manager_key)

    if not authoritative_db:
        # B-3 CASE C: no DB that the live statistics reader consumes could be
        # determined for this manager_key (malformed/empty key, or stats_engine
        # unavailable AND no candidate carries a daily_leads table). Fail closed.
        # No mirror write, no throttle consumed -- the status stays retryable.
        log.warning("manual status: no authoritative stats db resolved manager_key=%s card=%s code=%s",
                    manager_key, card.get("id"), code)
        return False

    if not os.path.exists(authoritative_db):
        # B-4: the authoritative stats DB was resolved but does not exist on
        # disk yet (e.g. this manager has never had a countable lead). Report
        # an explicit failure -- never let sqlite3.connect() silently create it
        # from this path. No mirror write, no throttle consumed: the same
        # status stays retryable once the DB exists.
        log.warning("manual status: authoritative db does not exist path=%s manager_key=%s card=%s",
                    authoritative_db, manager_key, card.get("id"))
        return False

    authoritative_ok = _write_manual_status_to_db(authoritative_db, card, code, tg_user_id)
    if not authoritative_ok:
        # B-2: do not touch mirrors on authoritative failure -- see docstring.
        return False

    for db_path in _db_candidates_for_manager(manager_key):
        try:
            if os.path.normcase(os.path.abspath(db_path)) == os.path.normcase(os.path.abspath(authoritative_db)):
                continue  # already written above -- skip to avoid a duplicate lead_status_audit row
        except Exception:
            pass
        _write_manual_status_to_db(db_path, card, code, tg_user_id)  # best-effort mirror; failure logged internally, never fatal here

    try:
        # Defense-in-depth beyond F-27's own internal try/except: bookkeeping must
        # never be able to turn an already-confirmed authoritative write into a
        # reported failure, even if _touch_card_action's own protection were ever
        # bypassed by a future edit.
        _touch_card_action(int(card.get("id") or 0), tg_user_id)
    except Exception as exc:
        log.warning("touch card action raised despite F-27 (non-authoritative, ignored) card=%s error=%r", card.get("id"), exc)
    return True


def _event_row_from_card(card: Dict[str, Any]) -> Dict[str, Any]:
    daily = _read_daily_row(str(card.get("manager_key") or ""), int(card.get("chat_id") or 0), str(card.get("lead_date") or ""))
    return {
        "id": 0,
        "manager_key": card.get("manager_key"),
        "chat_id": card.get("chat_id"),
        "lead_date": card.get("lead_date"),
        "new_status": daily.get("quality_bucket") or daily.get("status") or "",
        "payload_json": json.dumps(daily, ensure_ascii=False, default=str),
    }


async def _handle_status_callback(event, data: bytes) -> None:
    try:
        text = data.decode("utf-8", errors="ignore")
        parts = text.split(":")
        if len(parts) != 4 or parts[0] != "mb" or parts[1] != "s":
            await event.answer()
            return

        card_id = int(parts[2])
        code = parts[3]
        tg_user_id = int(event.sender_id or 0)

        if code != "clear" and code not in STATUS_ACTIONS:
            await event.answer("\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0441\u0442\u0430\u0442\u0443\u0441", alert=True)
            return
        if not _access_allowed(tg_user_id):
            await event.answer("\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430", alert=True)
            return

        card = _card_by_id(card_id, tg_user_id)
        if not card:
            await event.answer("\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430", alert=True)
            return

        wait = _check_throttle(card, 20)
        if wait:
            await event.answer(f"\u041f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435 {wait}\u0441", alert=False)
            return

        ok = _apply_manual_status(card, code, tg_user_id)
        if not ok:
            await event.answer("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0441\u0442\u0430\u0442\u0443\u0441", alert=True)
            return

        event_row = _event_row_from_card(card)
        override = _read_override(str(card.get("manager_key") or ""), int(card.get("chat_id") or 0))
        new_text = _format_lead_card(event_row, override=override)

        try:
            await event.edit(new_text, buttons=_manual_buttons(card_id))
        except Exception as exc:
            log.warning("card edit failed card_id=%s error=%r", card_id, exc)

        label = "\u0421\u0431\u0440\u043e\u0448\u0435\u043d\u043e" if code == "clear" else STATUS_ACTIONS[code]["label"]
        await event.answer(f"\u0421\u0442\u0430\u0442\u0443\u0441 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d: {label}", alert=False)
        log.info("manual status set uid=%s card_id=%s code=%s", tg_user_id, card_id, code)
    except Exception as exc:
        log.warning("status callback failed: %r", exc)
        try:
            await event.answer("\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438", alert=True)
        except Exception:
            pass


async def _poll_loop() -> None:
    log.info("poll loop started, interval=%s, mode=manager_bot_events+m24", MANAGER_BOT_POLL_SEC)

    while True:
        try:
            await _maybe_send_screenshot_reminders()
            # M1: per-lead reply-anchored reminders (no-status + missing screenshot).
            try:
                await _maybe_send_lead_card_reminders()
            except Exception as _lr_exc:
                log.warning("lead card reminder pass error: error_class=%s", type(_lr_exc).__name__)
            # M2.9B0: run retention at most once per calendar day.
            _retention_today = _mbstat_now().date().isoformat()
            if _retention_today != _SS_RETENTION_STATE.get("last_day", ""):
                _SS_RETENTION_STATE["last_day"] = _retention_today
                try:
                    _ss_retention_purge()
                except Exception as _ret_exc:
                    log.warning("retention purge loop error: error_class=%s", type(_ret_exc).__name__)
            # W2 (2026-07-29, D-02 step (a)/I-05, D-24/G): log-only
            # divergence detection and cutoff-suppression observability, at
            # most once per calendar day (same cadence idiom as the
            # retention purge above). Read-only/additive-log only -- never
            # writes access_targets, manager_bot_access, or event_cutoff_id
            # from this loop (no automatic access restoration; see
            # storage.w2_manager_bot_access_repair_backlog for the
            # separate, explicit, dry-run-by-default repair path).
            _w2_obs_today = _mbstat_now().date().isoformat()
            if _w2_obs_today != _W2_OBSERVABILITY_STATE.get("last_day", ""):
                _W2_OBSERVABILITY_STATE["last_day"] = _w2_obs_today
                try:
                    _w2_div = storage.w2_detect_access_divergence(TPILOT_DB_PATH)
                    log.info(
                        "W2 access divergence snapshot mba_without_access_target=%s access_target_without_mba=%s",
                        _w2_div.get("mba_without_access_target_count"),
                        _w2_div.get("access_target_without_mba_count"),
                    )
                except Exception as _w2_div_exc:
                    log.warning("W2 divergence detection failed: error_class=%s", type(_w2_div_exc).__name__)
                try:
                    _w2_cut = storage.w2_cutoff_observability_snapshot(TPILOT_DB_PATH)
                    log.info("W2 cutoff observability snapshot pairs=%s", len(_w2_cut))
                except Exception as _w2_cut_exc:
                    log.warning("W2 cutoff observability failed: error_class=%s", type(_w2_cut_exc).__name__)
                # REVISION (2026-07-29, Blocker 3): CUTOFF_SUPPRESSED as a
                # real, per-event persisted outcome (not just the aggregate
                # count above) -- idempotent, never changes event_cutoff_id
                # or _fetch_unsent_events_for_user's own selection (which
                # already excludes these events unconditionally).
                try:
                    _w2_cut_persist = storage.w2_persist_cutoff_suppressed_events(TPILOT_DB_PATH)
                    log.info("W2 cutoff-suppressed persistence rows_inserted=%s",
                             _w2_cut_persist.get("rows_inserted"))
                except Exception as _w2_cut_persist_exc:
                    log.warning("W2 cutoff-suppressed persistence failed: error_class=%s", type(_w2_cut_persist_exc).__name__)

            # REVISION (2026-07-29, Blocker 1): explicit stale-claim
            # recovery, EVERY poll cycle (leases expire on the order of
            # MANAGER_BOT_CLAIM_LEASE_SECONDS, far more often than the
            # once-per-day passes above) -- a single indexed query, cheap
            # even when it finds nothing (the normal case). Logs
            # observably only when it actually recovers something.
            try:
                _mb_recover_stale_claims()
            except Exception as _w2_recover_exc:
                log.warning("W2 stale-claim recovery pass failed error_class=%s", type(_w2_recover_exc).__name__)

            users = _enabled_access_users()
            for uid in users:
                # REVISION (2026-07-29, Blocker 2): ONE resolved access
                # decision drives delivery -- no union of independently-
                # called candidate lists. storage.w2_access_decision reads
                # manager_bot_access directly (never access_targets) for
                # delivery_manager_keys, so a grant that access_targets
                # does not (yet) know about is still offered here -- the
                # D-02 fix is now inherent to the resolver itself, not a
                # unioned second lookup. _linked_manager_keys (unchanged)
                # remains the source for the bot's 8 other, purely-UI call
                # sites (menus, permission checks) -- see that function's
                # own docstring for why it was deliberately left alone.
                decision = storage.w2_access_decision(uid, db_path=TPILOT_DB_PATH)
                manager_keys = decision["delivery_manager_keys"]
                if not manager_keys:
                    continue

                events_to_send = _fetch_unsent_events_for_user(uid, manager_keys)
                for event_row in events_to_send:
                    try:
                        await _send_event_to_user(event_row, uid)
                    except Exception as exc:
                        # Forward-fix Phase 3 (post-incident review
                        # 2026-07-26): last-resort backstop for any failure
                        # that occurs BEFORE the send itself (e.g. inside
                        # _upsert_card_placeholder, ahead of the per-branch
                        # try/except in _send_event_to_user) -- still routed
                        # through the SAME bounded attempts/backoff/
                        # quarantine bookkeeping, so no exception path can
                        # silently fall back to the old unconditional ~5s
                        # retry-forever behavior. Only the exception CLASS
                        # NAME is logged, never str()/repr() of the
                        # exception or any event payload/text.
                        _mark_send_attempt(int(event_row.get("id") or 0), int(uid), exc)
                        log.warning(
                            "send event failed actor_ref=%s event_id=%s error_class=%s",
                            _w2_actor_ref(uid), event_row.get("id"), type(exc).__name__,
                        )

            await asyncio.sleep(MANAGER_BOT_POLL_SEC)
        except asyncio.CancelledError:
            log.info("poll loop cancelled")
            raise
        except Exception as exc:
            log.warning("poll loop error: error_class=%s", type(exc).__name__)
            await asyncio.sleep(10)

# --- TPILOT MANAGER BOT CLEAN UI M2.4 20260531 END ---



# --- TPILOT MANAGER BOT UI COLLAPSE M2.7 20260601 START ---
# Collapsed manual-status keyboard.
# No schema changes.
# Full keyboard callback format:
# mb:s:<card_id>:<code>
# Edit callback:
# mb:e:<card_id>
# Back callback:
# mb:b:<card_id>

def _can_set_status(tg_user_id: int, manager_key: str) -> bool:
    # M2.7 keeps current access behavior.
    # M2.6 permissions will replace this with can_set_status.
    return _access_allowed(int(tg_user_id))


def _expanded_status_buttons(card_id: int):
    return [
        [
            Button.inline("\u2705 \u041b\u0438\u043a\u0432\u0438\u0434 \u0420\u0424 18+", f"mb:s:{card_id}:liq".encode("utf-8")),
        ],
        [
            Button.inline("\U0001f30d GEO", f"mb:s:{card_id}:geo".encode("utf-8")),
            Button.inline("\U0001f51e -18", f"mb:s:{card_id}:u18".encode("utf-8")),
        ],
        [
            Button.inline("\u2754 NA", f"mb:s:{card_id}:na".encode("utf-8")),
            Button.inline("\U0001f5d1 Trash", f"mb:s:{card_id}:trash".encode("utf-8")),
        ],
        [
            Button.inline("\u21a9\ufe0f \u0421\u0431\u0440\u043e\u0441", f"mb:s:{card_id}:clear".encode("utf-8")),
        ],
        [
            Button.inline("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", f"mb:b:{card_id}".encode("utf-8")),
        ],
    ]


def _collapsed_status_buttons(card_id: int):
    return [
        [
            Button.inline("\u270f\ufe0f \u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c \u0441\u0442\u0430\u0442\u0443\u0441", f"mb:e:{card_id}".encode("utf-8")),
        ]
    ]


def _with_upload_button(buttons, manager_key: str = "", chat_id: int = 0, lead_date: str = ""):
    """M1: append '\U0001f4ce Загрузить скрин'
    only while an active pending screenshot request exists for this lead (same lookup key
    used by the card header state), so the button and header always agree. Tapping it sends
    a small ForceReply helper message -- the helper becomes the visible Telegram reply
    target, not the card itself. Inert (no-op) when manager_key/chat_id are not supplied, so
    existing callers are unaffected unless updated to pass them."""
    if not manager_key or not chat_id:
        return buttons
    try:
        req = _screenshot_latest_request(manager_key, chat_id, lead_date)
    except Exception:
        return buttons
    if not req:
        return buttons
    if str(req.get("state") or "") != "pending" or int(req.get("active") or 0) != 1:
        return buttons
    req_id = int(req.get("id") or 0)
    if not req_id:
        return buttons
    base = [list(r) for r in (buttons or [])]
    base.append([Button.inline(
        "\U0001f4ce Загрузить скрин",
        f"mb:up:{req_id}".encode("utf-8"),
    )])
    return base


# --- TPILOT TRANSFERS STAGE2 START ---
def _with_transfer_button(buttons, manager_key: str = "", chat_id: int = 0, tg_user_id: int = 0, card_id: int = 0):
    """Secondary '🤝 Передача' action, appended after status/upload buttons — never
    replaces them. Shown only when: transfer_config.transfer_enabled=1 for manager_key,
    manager_key is not a closer, and tg_user_id has status-set permission for it."""
    if not manager_key or not chat_id or not tg_user_id or not card_id:
        return buttons
    mk = _norm_key(manager_key)
    try:
        if _tr_is_closer(mk) or not _tr_transfer_enabled(mk):
            return buttons
    except Exception:
        return buttons
    if not _can_set_status(int(tg_user_id), mk):
        return buttons
    base = [list(r) for r in (buttons or [])]
    base.append([Button.inline("🤝 Передача", f"mb:tr:new:{int(card_id)}".encode("utf-8"))])
    return base
# --- TPILOT TRANSFERS STAGE2 END ---


def _buttons_for_manual_state(
    card_id: int, override_present: bool, expanded: bool = False, *,
    manager_key: str = "", chat_id: int = 0, lead_date: str = "", tg_user_id: int = 0,
):
    if expanded:
        base = _expanded_status_buttons(card_id)
    elif override_present:
        base = _collapsed_status_buttons(card_id)
    else:
        base = _expanded_status_buttons(card_id)
    base = _with_upload_button(base, manager_key, chat_id, lead_date)
    base = _with_transfer_button(base, manager_key, chat_id, tg_user_id, card_id)
    return base


def _manual_buttons(card_id: int):
    # Backward-compatible default used by old call sites.
    return _expanded_status_buttons(card_id)


def _same_manual_status(override: Dict[str, Any], code: str) -> bool:
    if not override or code not in STATUS_ACTIONS:
        return False
    action = STATUS_ACTIONS[code]
    return (
        str(override.get("status") or "") == str(action.get("status") or "")
        and str(override.get("bucket") or "") == str(action.get("bucket") or "")
    )


def _with_manual_marker(text: str, override_present: bool) -> str:
    if not override_present:
        return text
    marker = "\u270f\ufe0f \u0421\u0442\u0430\u0442\u0443\u0441 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d \u0432\u0440\u0443\u0447\u043d\u0443\u044e"
    if marker in text:
        return text
    lines = str(text or "").splitlines()
    if not lines:
        return marker
    return "\n".join([lines[0], marker, *lines[1:]]).strip()


async def _safe_edit_card(event, text: str, buttons) -> None:
    try:
        await event.edit(text, buttons=buttons)
    except Exception as exc:
        if "MessageNotModifiedError" in repr(exc):
            return
        log.warning("card edit failed error=%r", exc)


def _card_by_chat_message(bot_chat_id, message_id) -> Dict[str, Any]:
    """M1: look up a lead card by its (chat, message) anchor — used from contexts that
    only have the card anchor (screenshot upload/replace), not the card's row id."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM manager_lead_cards WHERE bot_chat_id=? AND message_id=? LIMIT 1",
            (int(bot_chat_id or 0), int(message_id or 0)),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


async def _refresh_lead_card_by_card_row(card: Dict[str, Any]) -> None:
    """M1: re-render a lead card's text/buttons from its stored row, without a Telethon
    callback event (used after a screenshot upload or replace). Never deletes the card; on
    any failure logs and returns so callers can keep going safely."""
    try:
        bot_chat_id = int(card.get("bot_chat_id") or 0)
        message_id = int(card.get("message_id") or 0)
        card_id = int(card.get("id") or 0)
        if not bot_chat_id or not message_id:
            return
        manager_key = str(card.get("manager_key") or "")
        chat_id = int(card.get("chat_id") or 0)
        event_row = _event_row_from_card(card)
        override = _read_override(manager_key, chat_id)
        new_text = _format_lead_card(event_row, override=override)
        new_text = _with_manual_marker(new_text, bool(override))
        buttons = _buttons_for_manual_state(
            card_id, bool(override), expanded=False,
            manager_key=manager_key, chat_id=chat_id, lead_date=str(card.get("lead_date") or ""),
            tg_user_id=int(card.get("tg_user_id") or 0),
        )
        buttons = _with_copy_button(buttons, _ss_username_from_event_row(event_row))
        try:
            await client.edit_message(bot_chat_id, message_id, new_text, buttons=buttons)
        except Exception as exc:
            if "MessageNotModifiedError" not in repr(exc):
                log.warning("lead card refresh failed chat=%s msg=%s error=%r", bot_chat_id, message_id, exc)
    except Exception as exc:
        log.warning("lead card refresh outer failed: %r", exc)


# --- TPILOT REFRESH CARDS COMMAND START ---
async def _handle_refresh_cards_command(event, tg_user_id: int) -> None:
    """Hidden /refresh_cards command: re-render (edit in place) the caller's own last 50
    lead cards, so newly-enabled secondary buttons (e.g. transfer) appear on already-sent
    cards without waiting for a new lead. Strictly scoped to this tg_user_id -- never
    touches another manager's/user's cards. Edits existing messages only, never sends
    duplicates. Any single-card failure (deleted message, etc.) is skipped and does not
    stop the rest of the batch."""
    if not _access_allowed(tg_user_id):
        await event.respond("Нет доступа")
        return

    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM manager_lead_cards WHERE tg_user_id=? ORDER BY id DESC LIMIT 50",
            (int(tg_user_id),),
        ).fetchall()
        cards = [dict(r) for r in rows or []]
    except Exception as exc:
        log.warning("refresh_cards query failed uid=%s error=%r", tg_user_id, exc)
        cards = []
    finally:
        con.close()

    if not cards:
        await event.respond("Карточек для обновления не найдено.")
        return

    updated = 0
    for card in cards:
        try:
            await _refresh_lead_card_by_card_row(card)
            updated += 1
        except Exception as exc:
            log.warning("refresh_cards single card failed uid=%s card_id=%s error=%r", tg_user_id, card.get("id"), exc)
        await asyncio.sleep(0.3)

    await event.respond(f"🔄 Обновлено карточек: {updated}\nВсего найдено: {len(cards)}")
# --- TPILOT REFRESH CARDS COMMAND END ---


# --- TPILOT MANAGER BOT SCREENSHOT CONTROL M2.9C START ---
# Replaces the M2.9A block entirely (fully backward compatible).
# screenshot modes: off | instant | daily
# instant = ForceReply per lead (M2.9A behavior)
# daily   = batch upload session, no per-lead ForceReply
# off     = no screenshots at all
import time as _ss_time
import re as _ss_re

SCREENSHOT_CODES = {"geo", "u18", "na", "trash"}

SCREENSHOT_REQUEST_TEXT = (
    "⚠️ ВАЖНО\n\n"
    "Статус принят.\n"
    "Пришли скриншот для подтверждения по этому лиду.\n\n"
    "Отправь скрин ответом на это сообщение."
)

# In-memory batch upload sessions: uid -> dict
_SS_BATCH: Dict[int, Dict[str, Any]] = {}
_SS_BATCH_EXPIRY_SEC = 1800  # 30 minutes


def _ss_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _ss_safe_name(s) -> str:
    return _ss_re.sub(r"[^A-Za-z0-9_.-]+", "_", str(s or "")).strip("_") or "x"


# --- M2.9B0: safe path validator + retention ---

# Once-per-day guard for retention (mutable dict avoids global declaration).
_SS_RETENTION_STATE: Dict[str, str] = {"last_day": ""}
_W2_OBSERVABILITY_STATE: Dict[str, str] = {"last_day": ""}  # W2 20260729: divergence/cutoff snapshot cadence

_SS_RETENTION_ROOT = (BASE_DIR / "runtime" / "screenshots").resolve()


def _ss_path_is_safe(path: str) -> bool:
    """Return True iff path resolves strictly inside BASE_DIR/runtime/screenshots/."""
    try:
        if not path:
            return False
        p = Path(path).resolve()
        p.relative_to(_SS_RETENTION_ROOT)  # raises ValueError if outside
        return True
    except Exception:
        return False


def _ss_retention_purge(retention_days: int = 7) -> None:
    """
    Delete screenshot files older than retention_days from disk and mark DB rows purged.
    Idempotent: only touches state='uploaded' rows with file_deleted_at empty.
    Never deletes DB rows; never touches anything outside runtime/screenshots/.
    Uses uploaded_at (UTC) converted to Kyiv local date; falls back to status_date.
    """
    try:
        now_kyiv = _mbstat_now()
        # Cutoff: dates strictly before (today - retention_days + 1) are purgeable.
        cutoff = (now_kyiv - _mbstat_timedelta(days=retention_days - 1)).date()
        now_iso = _ss_now_iso()
        purged = 0
        skipped_unsafe = 0

        con = _connect()
        try:
            rows = con.execute(
                """
                SELECT id, screenshot_path, uploaded_at, status_date
                FROM manager_screenshot_requests
                WHERE state = 'uploaded'
                  AND COALESCE(screenshot_path, '') <> ''
                  AND COALESCE(file_deleted_at, '') = ''
                """
            ).fetchall()
            candidates = [dict(r) for r in rows]
        finally:
            con.close()

        log.info("retention started: candidates=%d cutoff=%s", len(candidates), cutoff.isoformat())

        for row in candidates:
            rid = int(row.get("id") or 0)
            path = str(row.get("screenshot_path") or "")
            uploaded_at_str = str(row.get("uploaded_at") or "")
            status_date_str = str(row.get("status_date") or "")

            # Derive local (Kyiv) upload day from UTC uploaded_at; fall back to status_date.
            upload_day = None
            if uploaded_at_str:
                try:
                    dt_utc = datetime.fromisoformat(uploaded_at_str).replace(tzinfo=_mbstat_timezone.utc)
                    upload_day = dt_utc.astimezone(_MBSTAT_TZ).date()
                except Exception:
                    pass
            if upload_day is None and status_date_str:
                try:
                    upload_day = _mbstat_date_cls.fromisoformat(status_date_str)
                except Exception:
                    pass
            if upload_day is None:
                continue  # Cannot determine date; leave untouched.
            if upload_day >= cutoff:
                continue  # Within retention window.

            # Validate path is inside our screenshot root.
            if not _ss_path_is_safe(path):
                log.warning("retention skipped unsafe path row_id=%s path=%r", rid, path)
                skipped_unsafe += 1
                continue

            # Delete file from disk (non-fatal if already missing).
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as exc:
                    log.warning("retention file delete failed row_id=%s path=%r error=%r", rid, path, exc)
                    # Still mark purged below to prevent repeated attempts.

            # Mark DB row purged.
            try:
                con2 = _connect()
                try:
                    con2.execute(
                        "UPDATE manager_screenshot_requests "
                        "SET state='purged', file_deleted_at=?, file_deleted_reason='retention_7_days' "
                        "WHERE id=?",
                        (now_iso, rid),
                    )
                    con2.commit()
                finally:
                    con2.close()
                purged += 1
            except Exception as exc:
                log.warning("retention db update failed row_id=%s error=%r", rid, exc)

        log.info("retention done: purged=%d skipped_unsafe=%d", purged, skipped_unsafe)

    except Exception as exc:
        log.warning("retention purge failed: %r", exc)

# --- M2.9B0: end ---


def _screenshot_config(manager_key) -> Dict[str, Any]:
    mk = _norm_key(manager_key)
    cfg = {
        "manager_key": mk,
        "require_screenshots": 0,
        "mode": "off",
        "reminder_enabled": 1,
        "reminder_time": "16:50",
        "last_reminder_date": "",
    }
    if not mk:
        return cfg
    con = _connect()
    try:
        row = con.execute("SELECT * FROM manager_screenshot_config WHERE manager_key=?", (mk,)).fetchone()
        if row:
            d = dict(row)
            raw_mode = str(d.get("mode") or "").strip().lower()
            # Backward compat: if mode is empty, derive from require_screenshots.
            if not raw_mode:
                raw_mode = "instant" if int(d.get("require_screenshots") or 0) == 1 else "off"
            if raw_mode not in ("off", "instant", "daily"):
                raw_mode = "off"
            cfg.update({
                "require_screenshots": 1 if raw_mode != "off" else 0,
                "mode": raw_mode,
                "reminder_enabled": int(d.get("reminder_enabled") or 0),
                "reminder_time": str(d.get("reminder_time") or "16:50"),
                "last_reminder_date": str(d.get("last_reminder_date") or ""),
            })
    except Exception as exc:
        log.warning("screenshot config read failed manager=%s error=%r", mk, exc)
    finally:
        con.close()
    return cfg


def _screenshot_mode(manager_key) -> str:
    """Return 'off', 'instant', or 'daily'."""
    return _screenshot_config(manager_key).get("mode") or "off"


def _screenshot_required(manager_key) -> bool:
    """Backward compat — true when mode is instant (M2.9A callers)."""
    return _screenshot_mode(manager_key) == "instant"


# ---------- source resolution ----------

def _ss_source_for_manager(manager_key) -> str:
    mk = _norm_key(manager_key)
    con = _connect()
    try:
        row = con.execute(
            "SELECT source_key FROM manager_source_links WHERE manager_key=? AND COALESCE(source_key,'')<>'' LIMIT 1",
            (mk,),
        ).fetchone()
        return str(row[0] or "").strip() if row else ""
    except Exception:
        return ""
    finally:
        con.close()


# ---------- request lookup ----------

def _screenshot_latest_request(manager_key, chat_id, lead_date) -> Dict[str, Any]:
    mk = _norm_key(manager_key)
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM manager_screenshot_requests WHERE manager_key=? AND chat_id=? AND lead_date=? ORDER BY id DESC LIMIT 1",
            (mk, int(chat_id or 0), str(lead_date or "")),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


def _screenshot_card_state(manager_key, chat_id, lead_date) -> str:
    row = _screenshot_latest_request(manager_key, chat_id, lead_date)
    if not row:
        return "none"
    st = str(row.get("state") or "")
    if st == "uploaded":
        return "uploaded"
    if st in ("expired", "cancelled"):
        return "none"
    return "pending"


def _screenshot_request_by_reply_any_state(request_chat_id, request_message_id) -> Dict[str, Any]:
    """Lookup a request by reply keys, any state (to detect expired/cancelled)."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM manager_screenshot_requests WHERE request_chat_id=? AND request_message_id=? ORDER BY id DESC LIMIT 1",
            (int(request_chat_id or 0), int(request_message_id or 0)),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


# ---------- ForceReply (instant mode) ----------

async def _safe_reply_send(chat_id, text, reply_to=0, buttons=None):
    """M1: send a message anchored as a reply to the lead card. If the reply target was
    deleted (or any reply error), fall back to a normal send so the message still arrives.
    Returns the sent message id (int), or 0 on total failure. Never raises."""
    cid = int(chat_id or 0)
    rt = int(reply_to or 0)
    if cid <= 0:
        return 0
    if rt > 0:
        try:
            msg = await client.send_message(cid, text, reply_to=rt, buttons=buttons)
            return int(getattr(msg, "id", 0) or 0)
        except Exception as exc:
            log.warning("reply send failed chat=%s reply_to=%s error=%r — falling back", cid, rt, exc)
    try:
        msg = await client.send_message(cid, text, buttons=buttons)
        return int(getattr(msg, "id", 0) or 0)
    except Exception as exc:
        log.warning("plain send failed chat=%s error=%r", cid, exc)
        return 0


async def _safe_delete_message(chat_id, message_id) -> None:
    """M1: delete a single bot reminder/service message. Safe if it was already deleted.
    Callers must only ever pass a stored bot-reminder/service id — never the lead card
    message id and never an uploaded screenshot message id."""
    cid = int(chat_id or 0)
    mid = int(message_id or 0)
    if cid <= 0 or mid <= 0:
        return
    try:
        await client.delete_messages(cid, [mid])
    except Exception:
        pass  # Already deleted / no rights — ignore safely.


async def _ss_send_or_refresh_helper(req_id: int, chat_id: int, prev_chat_id: int = 0, prev_msg_id: int = 0) -> int:
    """M1: send a fresh ForceReply helper for a pending screenshot request and store its
    (chat_id, message_id) into request_chat_id/request_message_id on that row -- the helper,
    not the lead card, is the message Telegram shows as the reply target near the input
    field; the card stays the visual/DB anchor. If a previous unused helper for the same
    pending request is supplied, it is deleted first (anti-spam) -- this is only ever called
    before any screenshot has been uploaded for the row, so it never touches a helper that
    was already replied to. Returns the new helper's message id, or 0 if the send failed
    (row left untouched on failure)."""
    if prev_chat_id and prev_msg_id:
        await _safe_delete_message(prev_chat_id, prev_msg_id)
    try:
        helper = await client.send_message(
            chat_id,
            "Пришлите скриншот ответом на это сообщение",
            buttons=Button.force_reply(single_use=True, selective=True),
        )
    except Exception as exc:
        log.warning("screenshot helper send failed req_id=%s error=%r", req_id, exc)
        return 0
    helper_msg_id = int(getattr(helper, "id", 0) or 0)
    con = _connect()
    try:
        con.execute(
            "UPDATE manager_screenshot_requests SET request_chat_id=?, request_message_id=? WHERE id=?",
            (chat_id, helper_msg_id, req_id),
        )
        con.commit()
    finally:
        con.close()
    return helper_msg_id


async def _send_screenshot_request(
    manager_chat_id, manager_key, chat_id, lead_date, tg_user_id, status_code,
    card_message_id=0, card_chat_id=0,
) -> None:
    mk = _norm_key(manager_key)
    source_key = _ss_source_for_manager(mk)
    today = _mbstat_now().date().isoformat()
    card_msg = int(card_message_id or 0)
    card_chat = int(card_chat_id or manager_chat_id or 0)
    # Avoid duplicate pending spam: reuse existing pending for same lead.
    latest = _screenshot_latest_request(mk, chat_id, lead_date)
    req_id = 0
    prev_req_chat = 0
    prev_req_msg = 0
    if latest and str(latest.get("state") or "") == "pending":
        req_id = int(latest.get("id") or 0)
        prev_req_chat = int(latest.get("request_chat_id") or 0)
        prev_req_msg = int(latest.get("request_message_id") or 0)
        con = _connect()
        try:
            con.execute(
                "UPDATE manager_screenshot_requests SET status_code=?, tg_user_id=?, source_key=?, "
                "status_date=?, card_message_id=?, card_chat_id=? WHERE id=?",
                (str(status_code or ""), int(tg_user_id or 0), source_key, today,
                 card_msg, card_chat, req_id),
            )
            con.commit()
        except Exception:
            pass
        finally:
            con.close()
    else:
        # M1: the lead card itself (below) is edited to show the "screenshot required"
        # header and the on-demand "📎 Загрузить скрин" button; in 'instant' mode (the only
        # mode that ever calls this function) a ForceReply helper is also auto-sent right
        # below, immediately after this row is created -- it is the helper, never the card,
        # that becomes the visible Telegram reply target. request_message_id starts at 0
        # here and is filled in by the auto-send below; older back-compat rows created
        # before this change keep their own nonzero request_message_id.
        con = _connect()
        try:
            cur = con.execute(
                """
                INSERT INTO manager_screenshot_requests(
                    manager_key, chat_id, lead_date, tg_user_id, status_code,
                    request_chat_id, request_message_id, state, kind, source_key, status_date, created_at,
                    card_message_id, card_chat_id, active
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'pending', 'exact', ?, ?, ?, ?, ?, 1)
                """,
                (mk, int(chat_id or 0), str(lead_date or ""), int(tg_user_id or 0), str(status_code or ""),
                 int(card_chat or 0), source_key, today, _ss_now_iso(),
                 card_msg, card_chat),
            )
            con.commit()
            req_id = int(cur.lastrowid or 0)
        except Exception as exc:
            log.warning("screenshot request insert failed manager=%s error=%r", mk, exc)
        finally:
            con.close()

    # M1: reflect the now-current screenshot state on the original lead card header.
    if card_chat and card_msg:
        try:
            card_row = _card_by_chat_message(card_chat, card_msg)
            if card_row:
                await _refresh_lead_card_by_card_row(card_row)
        except Exception as exc:
            log.warning("lead card refresh after screenshot request failed: %r", exc)

    # M1: 'instant' mode auto-sends the ForceReply helper right after the status button
    # press (this function is only ever invoked for instant-mode statuses -- see the
    # SCREENSHOT_CODES + _screenshot_mode(...) == 'instant' guard at the call site). Any
    # previous unused helper for this same pending request is replaced (anti-spam); once a
    # screenshot is actually uploaded, this function is not called again for that row, so a
    # used helper is never touched here.
    if req_id and card_chat:
        try:
            await _ss_send_or_refresh_helper(req_id, card_chat, prev_req_chat, prev_req_msg)
        except Exception as exc:
            log.warning("auto screenshot helper send failed req_id=%s error=%r", req_id, exc)


def _ss_request_by_card_pending(card_chat_id, card_message_id) -> Dict[str, Any]:
    """M1: latest pending+active screenshot request anchored to this lead card."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM manager_screenshot_requests "
            "WHERE card_chat_id=? AND card_message_id=? AND state='pending' AND COALESCE(active,1)=1 "
            "ORDER BY id DESC LIMIT 1",
            (int(card_chat_id or 0), int(card_message_id or 0)),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


async def _handle_screenshot_reply(event, tg_user_id) -> bool:
    """Handle a media reply to a screenshot request. Returns True if handled.

    M1: matches either (a) the ForceReply service prompt (back-compat, by
    request_message_id) or (b) the original lead card itself (by card anchor), so a
    manager can reply with the proof directly on the card. On accept, the original lead
    card is edited to show "✅ Скрин принят" (never deleted); an old service prompt is
    deleted only when this is a back-compat row that still has one.
    """
    try:
        if not getattr(event, "is_reply", False):
            return False
        reply_to = int(getattr(event, "reply_to_msg_id", 0) or 0)
        if not reply_to:
            return False
        chat_id = int(getattr(event, "chat_id", 0) or 0)

        # (a) Service-prompt match (any state, to catch expired/cancelled). Back-compat.
        req = _screenshot_request_by_reply_any_state(chat_id, reply_to)
        # (b) Card-anchor match: the manager replied to the lead card directly.
        if not req:
            req = _ss_request_by_card_pending(chat_id, reply_to)
        if not req:
            return False

        state = str(req.get("state") or "")
        if state in ("expired", "cancelled"):
            try:
                await event.reply("⚠️ Запрос уже закрыт. Скрин не принят.")
            except Exception:
                pass
            return True  # Claimed; don't fall to batch handler.

        if state != "pending":
            return False  # Already uploaded or unknown — ignore.

        mk = _norm_key(req.get("manager_key"))
        source_key = str(req.get("source_key") or "") or _ss_source_for_manager(mk)
        today = _mbstat_now().date().isoformat()
        status_date = str(req.get("status_date") or "") or today

        base_dir = str((BASE_DIR / "runtime" / "screenshots" / (mk or "unknown")).resolve())
        os.makedirs(base_dir, exist_ok=True)
        ts = _ss_time.strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_req{int(req.get('id') or 0)}_{_ss_safe_name(mk)}"
        target = os.path.join(base_dir, fname)
        saved = await event.download_media(file=target)
        saved_path = str(saved or "")

        req_id = int(req.get("id") or 0)
        req_chat_id = int(req.get("request_chat_id") or 0)
        req_msg_id = int(req.get("request_message_id") or 0)
        prev_reminder_msg_id = int(req.get("last_screenshot_reminder_message_id") or 0)

        con = _connect()
        try:
            con.execute(
                """UPDATE manager_screenshot_requests
                   SET state='uploaded', kind='exact', active=1, source_key=?, status_date=?,
                       screenshot_message_id=?, screenshot_path=?, uploaded_at=?,
                       last_screenshot_reminder_message_id=0
                   WHERE id=?""",
                (source_key, status_date, int(getattr(event, "id", 0) or 0), saved_path, _ss_now_iso(), req_id),
            )
            con.commit()
        finally:
            con.close()

        # M1: edit the original lead card itself to show the accepted state.
        card_chat = int(req.get("card_chat_id") or 0)
        card_msg = int(req.get("card_message_id") or 0)
        if card_chat and card_msg:
            try:
                card_row = _card_by_chat_message(card_chat, card_msg)
                if card_row:
                    await _refresh_lead_card_by_card_row(card_row)
            except Exception as exc:
                log.warning("lead card refresh after upload failed: %r", exc)

        # M1: confirmation reply carries the scoped replace button (never edits the lead card).
        try:
            await event.reply(
                "✅ Скрин принят",
                buttons=[[Button.inline(
                    "🔁 Заменить скриншот", f"mb:rs:{req_id}".encode("utf-8"),
                )]],
            )
        except Exception:
            pass

        # M1: the ForceReply helper (or, for old back-compat rows, the service prompt) is
        # never deleted after a successful upload -- it must stay in chat history as the
        # message the manager actually replied to. There is no reliable way to tell a new
        # on-demand helper apart from an old back-compat service prompt from this row alone,
        # so request_message_id is left untouched in both cases. req_chat_id/req_msg_id are
        # still read above only because they feed the upload-matching logic elsewhere.
        # M1: clear the rolling missing-screenshot reminder, if any (never the card/screenshot).
        if prev_reminder_msg_id:
            await _safe_delete_message(req_chat_id or chat_id, prev_reminder_msg_id)

        log.info("screenshot uploaded exact manager=%s chat_id=%s req_id=%s path=%s", mk, req.get("chat_id"), req_id, saved_path)
        return True
    except Exception as exc:
        log.warning("screenshot reply handler failed: %r", exc)
        return False


def _ss_request_by_id(request_id) -> Dict[str, Any]:
    """M1: load a single screenshot request row by id."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM manager_screenshot_requests WHERE id=? LIMIT 1",
            (int(request_id or 0),),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


async def _handle_replace_screenshot_callback(event, data: bytes) -> None:
    """M1: 'Заменить скриншот' (mb:rs:<request_id>). Soft-replaces the current active proof
    for a specific lead: the old request becomes active=0 (kept for audit, file untouched),
    a fresh pending+active request is opened for the same card/lead, and the manager is asked
    to upload a new screenshot as a reply to the original lead card."""
    try:
        text = data.decode("utf-8", errors="ignore")
        parts = text.split(":")
        if len(parts) != 3 or parts[0] != "mb" or parts[1] != "rs":
            await event.answer()
            return
        try:
            old_id = int(parts[2])
        except Exception:
            old_id = 0
        uid = int(getattr(event, "sender_id", 0) or 0)

        if not _access_allowed(uid):
            await event.answer("Нет доступа", alert=True)
            return

        old = _ss_request_by_id(old_id)
        if not old:
            await event.answer("Запрос не найден", alert=True)
            return

        # Scope to the same manager/lead owner: only the manager-bot user who owns this
        # lead-card flow may replace its proof.
        owner_uid = int(old.get("tg_user_id") or 0)
        mk = _norm_key(old.get("manager_key"))
        if uid != owner_uid and mk not in set(_linked_manager_keys(uid)):
            await event.answer("Нет доступа к этому лиду", alert=True)
            return

        # M1 MF-1: only an active, uploaded request can be replaced. This rejects a
        # double-tap of the same button (the first tap already flipped this row to
        # active=0), so a stale/repeated callback can never open a second request.
        if int(old.get("active") or 0) != 1 or str(old.get("state") or "") != "uploaded":
            await event.answer("Скриншот уже заменён или не активен", alert=True)
            return

        now = _ss_now_iso()
        card_msg = int(old.get("card_message_id") or 0)
        card_chat = int(old.get("card_chat_id") or old.get("request_chat_id") or 0)
        prev_reminder_id = int(old.get("last_screenshot_reminder_message_id") or 0)

        # 1) retire the old proof (soft): no longer the active screenshot. Keep file + row.
        con = _connect()
        try:
            con.execute(
                "UPDATE manager_screenshot_requests "
                "SET active=0, replaced_at=?, replaced_by_user_id=? WHERE id=?",
                (now, uid, old_id),
            )
            # M1 MF-1: cancel any other pending+active request already anchored to this
            # card (defensive cleanup for orphans from earlier edge cases) so at most one
            # pending+active request ever exists per card before the new one is inserted.
            if card_chat and card_msg:
                con.execute(
                    "UPDATE manager_screenshot_requests "
                    "SET active=0, state='cancelled', replaced_at=?, replaced_by_user_id=? "
                    "WHERE card_chat_id=? AND card_message_id=? AND state='pending' AND active=1",
                    (now, uid, card_chat, card_msg),
                )
            con.commit()
        finally:
            con.close()

        # 2) drop the old rolling missing-screenshot reminder, if any.
        if prev_reminder_id and card_chat:
            await _safe_delete_message(card_chat, prev_reminder_id)

        # 3) open a fresh pending+active request for the same lead/card. M1: no separate
        # "⚠️ ВАЖНО..." service prompt and no Button.force_reply — request_message_id=0
        # marks this as a new-style request; the manager replies directly to the lead
        # card, which is switched back to the "screenshot required" header below.
        con = _connect()
        try:
            con.execute(
                """
                INSERT INTO manager_screenshot_requests(
                    manager_key, chat_id, lead_date, tg_user_id, status_code,
                    request_chat_id, request_message_id, state, kind, source_key, status_date,
                    created_at, card_message_id, card_chat_id, active, replaces_request_id
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 'pending', 'exact', ?, ?, ?, ?, ?, 1, ?)
                """,
                (mk, int(old.get("chat_id") or 0), str(old.get("lead_date") or ""), owner_uid,
                 str(old.get("status_code") or ""), card_chat,
                 str(old.get("source_key") or _ss_source_for_manager(mk)),
                 str(old.get("status_date") or _mbstat_now().date().isoformat()),
                 now, card_msg, card_chat, old_id),
            )
            con.commit()
        finally:
            con.close()

        # 4) switch the original lead card back to the "screenshot required" header.
        if card_chat and card_msg:
            try:
                card_row = _card_by_chat_message(card_chat, card_msg)
                if card_row:
                    await _refresh_lead_card_by_card_row(card_row)
            except Exception as exc:
                log.warning("lead card refresh after replace failed: %r", exc)

        await event.answer("Загрузите новый скриншот ответом на карточку лида")
        log.info("screenshot replace requested uid=%s old_req=%s manager=%s", uid, old_id, mk)
    except Exception as exc:
        log.warning("replace screenshot callback failed: %r", exc)
        try:
            await event.answer("Ошибка", alert=True)
        except Exception:
            pass


async def _handle_screenshot_upload_prompt_callback(event, data: bytes) -> None:
    """M1: '\U0001f4ce Загрузить скрин' (mb:up:<request_id>). Sends a small ForceReply helper
    message on demand so the manager gets a visible Telegram reply target near the input
    field. The helper message is that reply target -- the Bot API cannot turn the
    already-sent, inline-keyboard lead card itself into a ForceReply target (ForceReply is
    send-time-only, targets only its own message, and cannot coexist with inline buttons).
    The lead card stays the persistent visual/DB anchor; the upload still attaches to this
    same request row, exactly like the old back-compat service-prompt path. This is the
    on-demand fallback path -- 'instant' mode also auto-sends a first helper right after the
    status button press (see _send_screenshot_request); this handler exists so the manager
    can get a fresh helper again later (e.g. the first one scrolled away or was dismissed)
    without having to replace the screenshot. Never deletes the card or any uploaded
    screenshot."""
    try:
        text = data.decode("utf-8", errors="ignore")
        parts = text.split(":")
        if len(parts) != 3 or parts[0] != "mb" or parts[1] != "up":
            await event.answer()
            return
        try:
            req_id = int(parts[2])
        except Exception:
            req_id = 0

        uid = int(getattr(event, "sender_id", 0) or 0)
        if not _access_allowed(uid):
            await event.answer("Нет доступа", alert=True)
            return

        req = _ss_request_by_id(req_id)
        if not req:
            await event.answer("Запрос не найден или уже закрыт", alert=True)
            return

        owner_uid = int(req.get("tg_user_id") or 0)
        mk = _norm_key(req.get("manager_key"))
        if uid != owner_uid and mk not in set(_linked_manager_keys(uid)):
            await event.answer("Нет доступа к этому лиду", alert=True)
            return

        if int(req.get("active") or 0) != 1 or str(req.get("state") or "") != "pending":
            await event.answer("Скриншот уже загружен или запрос не активен", alert=True)
            return

        chat_id = int(getattr(event, "chat_id", 0) or uid)

        # Avoid helper spam: drop any previous unused helper for this same pending request
        # (e.g. a double-tap of this button) before storing the new one. Only ever the
        # stored helper id -- never the card and never an uploaded screenshot.
        prev_req_chat = int(req.get("request_chat_id") or 0)
        prev_req_msg = int(req.get("request_message_id") or 0)

        helper_msg_id = await _ss_send_or_refresh_helper(req_id, chat_id, prev_req_chat, prev_req_msg)
        if not helper_msg_id:
            await event.answer("Не удалось отправить запрос, попробуйте ещё раз", alert=True)
            return

        await event.answer("Ок, отправьте скриншот")
        log.info("screenshot upload helper sent uid=%s req_id=%s helper_msg_id=%s", uid, req_id, helper_msg_id)
    except Exception as exc:
        log.warning("screenshot upload prompt callback failed: %r", exc)
        try:
            await event.answer("Ошибка", alert=True)
        except Exception:
            pass


# ---------- Batch upload session (daily mode) ----------

def _ss_batch_active(uid) -> Dict[str, Any]:
    """Return active batch session or {} if none/expired."""
    sess = _SS_BATCH.get(int(uid or 0)) or {}
    if not sess:
        return {}
    expires = float(sess.get("expires_at") or 0)
    if _ss_time.time() > expires:
        _SS_BATCH.pop(int(uid or 0), None)
        return {}
    return sess


def _ss_batch_start(uid, manager_key, source_key) -> Dict[str, Any]:
    now = _ss_time.time()
    sess = {
        "manager_key": _norm_key(manager_key),
        "source_key": str(source_key or ""),
        "status_date": _mbstat_now().date().isoformat(),
        "accepted_count": 0,
        "created_at": now,
        "expires_at": now + _SS_BATCH_EXPIRY_SEC,
    }
    _SS_BATCH[int(uid or 0)] = sess
    return sess


def _ss_batch_clear(uid) -> None:
    _SS_BATCH.pop(int(uid or 0), None)


async def _handle_batch_media(event, tg_user_id) -> bool:
    """Accept a photo/document into the active batch session. Returns True if handled."""
    try:
        uid = int(tg_user_id or 0)
        sess = _ss_batch_active(uid)
        if not sess:
            return False
        mk = str(sess.get("manager_key") or "")
        sk = str(sess.get("source_key") or "")
        sd = str(sess.get("status_date") or _mbstat_now().date().isoformat())

        base_dir = str((BASE_DIR / "runtime" / "screenshots" / (mk or "unknown")).resolve())
        os.makedirs(base_dir, exist_ok=True)
        ts = _ss_time.strftime("%Y%m%d_%H%M%S")
        fname = f"{ts}_batch_{_ss_safe_name(mk)}_{_ss_safe_name(sd)}"
        target = os.path.join(base_dir, fname)
        saved = await event.download_media(file=target)
        saved_path = str(saved or "")

        now_iso = _ss_now_iso()
        con = _connect()
        try:
            con.execute(
                """
                INSERT INTO manager_screenshot_requests(
                    manager_key, chat_id, lead_date, tg_user_id, status_code,
                    request_chat_id, request_message_id, state, kind,
                    source_key, status_date, screenshot_message_id,
                    screenshot_path, created_at, uploaded_at
                ) VALUES (?, 0, '', ?, '', ?, 0, 'uploaded', 'batch', ?, ?, ?, ?, ?, ?)
                """,
                (mk, uid, int(getattr(event, "chat_id", 0) or 0), sk, sd,
                 int(getattr(event, "id", 0) or 0), saved_path, now_iso, now_iso),
            )
            con.commit()
        finally:
            con.close()

        sess["accepted_count"] = int(sess.get("accepted_count") or 0) + 1
        try:
            await event.respond(f"✅ Скрин принят. Всего принято: {sess['accepted_count']}")
        except Exception:
            pass
        log.info("screenshot batch accepted uid=%s manager=%s source=%s count=%s", uid, mk, sk, sess["accepted_count"])
        return True
    except Exception as exc:
        log.warning("batch media handler failed: %r", exc)
        return False


def _ss_accessible_manager_keys(uid: int) -> List[str]:
    """Return manager keys the user can access (respects scope_mode, uses _linked_manager_keys)."""
    return [_norm_key(k) for k in _linked_manager_keys(int(uid or 0)) if _norm_key(k)]


async def _ss_start_batch_for_manager(event, uid: int, mk: str) -> None:
    """Core batch-session start for a validated manager key. Reused by single and picker paths."""
    mk = _norm_key(mk)
    mode = _screenshot_mode(mk)
    if mode == "off":
        log.warning("batch start refused: mode=off uid=%s manager=%s", uid, mk)
        await event.respond("Загрузка скринов выключена для этого менеджера.")
        return
    sk = _ss_source_for_manager(mk)
    if not sk:
        log.warning("batch start refused: no source uid=%s manager=%s", uid, mk)
        await event.respond("Не удалось определить источник для этого менеджера.")
        return
    _ss_batch_start(uid, mk, sk)
    log.info("batch session started uid=%s manager=%s source=%s", uid, mk, sk)
    await event.respond(
        f"📤 Загрузка скринов включена\n\n"
        f"Менеджер: {mk}\n"
        f"Источник: {sk}\n\n"
        f"Отправляй скрины пачками.\n"
        f"Когда закончишь, нажми ✅ Готово или напиши /skrin_done.",
        buttons=[[Button.inline("✅ Готово", b"ss:batch_done")]],
    )


async def _handle_skrin_command(event, tg_user_id) -> None:
    """Handle /skrin command — start a batch upload session.

    If the user has exactly one accessible manager, start immediately.
    If multiple, show an inline picker so they can choose.
    """
    uid = int(tg_user_id or 0)
    if not _access_allowed(uid):
        await event.respond("Нет доступа.")
        return
    keys = _ss_accessible_manager_keys(uid)
    if not keys:
        await event.respond("Не удалось определить вашего менеджера. Обратитесь к администратору.")
        return
    if len(keys) == 1:
        # Single manager — start directly (original behaviour).
        await _ss_start_batch_for_manager(event, uid, keys[0])
        return
    # Multiple managers — show picker.
    log.info("batch picker shown uid=%s managers=%s", uid, keys)
    buttons = [
        [Button.inline(mk, f"ss:batch_mgr:{mk}".encode("utf-8"))]
        for mk in keys
    ]
    await event.respond(
        "📤 Загрузка скринов\n\nВыбери менеджера:",
        buttons=buttons,
    )


async def _handle_batch_mgr_callback(event, mk: str, uid: int) -> None:
    """Handle ss:batch_mgr:<key> — verify access then start batch for chosen manager."""
    mk = _norm_key(mk)
    uid = int(uid or 0)
    if not mk or not uid:
        await event.answer("Неверные данные.", alert=True)
        return
    if not _access_allowed(uid):
        log.warning("batch manager callback unauthorized uid=%s manager=%s", uid, mk)
        await event.answer("Нет доступа.", alert=True)
        return
    accessible = {_norm_key(k) for k in _ss_accessible_manager_keys(uid)}
    if mk not in accessible:
        log.warning("batch manager callback inaccessible uid=%s manager=%s accessible=%s", uid, mk, sorted(accessible))
        await event.answer("Нет доступа к этому менеджеру.", alert=True)
        return
    await event.answer()
    await _ss_start_batch_for_manager(event, uid, mk)


async def _handle_skrin_done_command(event, tg_user_id) -> None:
    """Handle /skrin_done or batch_done callback — close session, show summary."""
    uid = int(tg_user_id or 0)
    sess = _ss_batch_active(uid)
    if not sess:
        await event.respond("Активной сессии загрузки нет.")
        return
    count = int(sess.get("accepted_count") or 0)
    _ss_batch_clear(uid)
    await event.respond(
        f"Загрузка завершена.\n"
        f"Принято скринов: {count}"
    )


# ---------- card/button helpers (unchanged interface) ----------

def _copy_username_row(username):
    u = str(username or "").strip()
    if not u:
        return None
    if not u.startswith("@"):
        u = "@" + u
    try:
        from telethon.tl.types import KeyboardButtonCopy
        return [KeyboardButtonCopy(f"📋 {u}", u)]
    except Exception:
        return None


def _with_copy_button(buttons, username):
    row = _copy_username_row(username)
    if not row:
        return buttons
    base = [list(r) for r in (buttons or [])]
    base.append(row)
    return base


def _ss_username_from_event_row(event_row) -> str:
    try:
        return str(_decode_payload(event_row).get("username") or "").strip()
    except Exception:
        return ""


# ---------- reconciliation helpers ----------

def _ss_expected_nonliquid_leads(manager_key, status_date) -> List[Dict[str, Any]]:
    """
    Return daily_leads rows that are non-liquid for the given status_date.
    status_date is the date of manual_status_at (or quality_checked_at fallback).
    Matches paid stats bucket logic:
      geo, under18, age_missing (->under18), na, geo_missing (->na), trash.
    Only countable leads: prefer lead_countable column; fall back to duplicate flag.
    """
    mk = _norm_key(manager_key)
    results: List[Dict[str, Any]] = []
    for db_path in _db_candidates_for_manager(mk):
        con = _connect_path(db_path)
        try:
            if not _table_exists(con, "daily_leads"):
                continue
            rows = con.execute(
                """
                SELECT *,
                    COALESCE(
                        CASE WHEN COALESCE(manual_status_at,'') <> '' THEN date(manual_status_at) ELSE NULL END,
                        CASE WHEN COALESCE(quality_checked_at,'') <> '' THEN date(quality_checked_at) ELSE NULL END
                    ) AS _computed_status_date
                FROM daily_leads
                WHERE quality_bucket IN ('geo','under18','age_missing','na','geo_missing','trash')
                  AND COALESCE(
                        CASE WHEN COALESCE(manual_status_at,'') <> '' THEN date(manual_status_at) ELSE NULL END,
                        CASE WHEN COALESCE(quality_checked_at,'') <> '' THEN date(quality_checked_at) ELSE NULL END
                      ) = ?
                ORDER BY first_seen_utc ASC, id ASC
                """,
                (str(status_date or ""),),
            ).fetchall()
            results.extend([dict(r) for r in rows])
        except Exception:
            continue
        finally:
            con.close()
    # Apply countable filter (mirrors _mbstat_countable / _psf3_countable logic):
    # if lead_countable column present use it; else fall back to duplicate flag;
    # if neither column present treat as countable (do not exclude).
    def _ss_is_countable(lead):
        try:
            if "lead_countable" in lead:
                return int(lead.get("lead_countable") or 0) == 1
        except Exception:
            pass
        try:
            return int(lead.get("duplicate") or 0) != 1
        except Exception:
            return True
    return [r for r in results if _ss_is_countable(r)]


def _ss_uploaded_count(manager_key, source_key, status_date) -> int:
    """Count uploaded screenshots for manager/source/date (both exact and batch)."""
    mk = _norm_key(manager_key)
    sk = str(source_key or "")
    con = _connect()
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM manager_screenshot_requests WHERE manager_key=? AND source_key=? AND status_date=? AND state='uploaded'",
            (mk, sk, str(status_date or "")),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0
    finally:
        con.close()


def _ss_contact_line_from_daily(daily) -> str:
    username = str((daily or {}).get("username") or "").strip()
    full_name = str((daily or {}).get("full_name") or "").strip()
    if username:
        return ("@" + username) if not username.startswith("@") else username
    if full_name:
        return full_name
    return "-"


def _ss_contact_line(req) -> str:
    daily = _read_daily_row(str(req.get("manager_key") or ""), int(req.get("chat_id") or 0), str(req.get("lead_date") or ""))
    return _ss_contact_line_from_daily(daily)


def _ss_refresh_manager(manager_key) -> None:
    """
    Reconciliation refresh for one manager:
    - Expire old pending exact requests from previous dates.
    - Cancel pending exact requests if lead is no longer non-liquid.
    - Create expected-slot rows for today's non-liquid leads if missing.
    Never deletes rows.
    """
    mk = _norm_key(manager_key)
    today = _mbstat_now().date().isoformat()
    sk = _ss_source_for_manager(mk)
    con = _connect()
    try:
        # Expire pending exact requests from previous dates.
        con.execute(
            """UPDATE manager_screenshot_requests
               SET state='expired'
               WHERE manager_key=? AND kind='exact' AND state='pending'
                 AND COALESCE(status_date,'') <> '' AND status_date < ?""",
            (mk, today),
        )
        # Cancel pending exact requests if the lead is no longer non-liquid.
        # We pull request rows and check each against daily_leads.
        pending_rows = con.execute(
            "SELECT id, chat_id, lead_date FROM manager_screenshot_requests WHERE manager_key=? AND kind='exact' AND state='pending'",
            (mk,),
        ).fetchall()
        con.commit()
    except Exception:
        con.close()
        return
    finally:
        con.close()

    for pr in (pending_rows or []):
        pr = dict(pr)
        daily = _read_daily_row(mk, int(pr.get("chat_id") or 0), str(pr.get("lead_date") or ""))
        if not daily:
            continue
        bucket = str(daily.get("quality_bucket") or "").strip().lower()
        if bucket not in ("geo", "under18", "age_missing", "na", "geo_missing", "trash"):
            con2 = _connect()
            try:
                con2.execute("UPDATE manager_screenshot_requests SET state='cancelled' WHERE id=?", (pr["id"],))
                con2.commit()
            except Exception:
                pass
            finally:
                con2.close()

    # Create expected-slot rows for today's non-liquid leads if not already present.
    # Normalize age_missing->under18, geo_missing->na for status_code (mirrors paid stats mapping).
    _SS_BUCKET_NORM = {"age_missing": "under18", "geo_missing": "na"}
    today_leads = _ss_expected_nonliquid_leads(mk, today)
    for lead in today_leads:
        chat_id = int(lead.get("chat_id") or 0)
        lead_date = str(lead.get("lead_date") or "")
        bucket = _SS_BUCKET_NORM.get(
            str(lead.get("quality_bucket") or "").strip().lower(),
            str(lead.get("quality_bucket") or ""),
        )
        existing = _screenshot_latest_request(mk, chat_id, lead_date)
        if existing:
            continue  # Already have a row (pending/uploaded/expected) for this lead.
        con3 = _connect()
        try:
            con3.execute(
                """INSERT INTO manager_screenshot_requests(
                    manager_key, chat_id, lead_date, tg_user_id, status_code,
                    request_chat_id, request_message_id, state, kind,
                    source_key, status_date, created_at
                ) VALUES (?, ?, ?, 0, ?, 0, 0, 'expected', 'expected', ?, ?, ?)""",
                (mk, chat_id, lead_date, bucket, sk, today, _ss_now_iso()),
            )
            con3.commit()
        except Exception:
            pass
        finally:
            con3.close()


def _screenshot_set_last_reminder(manager_key, day_iso):
    mk = _norm_key(manager_key)
    con = _connect()
    try:
        con.execute(
            "INSERT INTO manager_screenshot_config(manager_key, last_reminder_date, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(manager_key) DO UPDATE SET last_reminder_date=excluded.last_reminder_date, updated_at=excluded.updated_at",
            (mk, str(day_iso or ""), _ss_now_iso()),
        )
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


def _screenshot_pending_for_manager(manager_key):
    mk = _norm_key(manager_key)
    con = _connect()
    try:
        rows = con.execute(
            "SELECT * FROM manager_screenshot_requests WHERE manager_key=? AND state='pending' ORDER BY id ASC",
            (mk,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        con.close()


# ---------- reminder + reconciliation ----------

async def _maybe_send_screenshot_reminders() -> None:
    """
    At reminder_time, for each manager with mode instant|daily and reminder_enabled=1:
    - Run reconciliation refresh.
    - Compute expected/uploaded counts for today.
    - Send ONE summary message per manager (per their chat).
    - Guard once/day via last_reminder_date.
    """
    try:
        now = _mbstat_now()
    except Exception:
        return
    hhmm = now.strftime("%H:%M")
    today = now.date().isoformat()
    con = _connect()
    try:
        rows = con.execute(
            "SELECT manager_key, mode, reminder_enabled, reminder_time, last_reminder_date "
            "FROM manager_screenshot_config WHERE COALESCE(reminder_enabled,0)=1"
        ).fetchall()
        configs = [dict(r) for r in rows]
    except Exception:
        configs = []
    finally:
        con.close()

    for cfg in configs:
        mk = _norm_key(cfg.get("manager_key"))
        if not mk:
            continue
        # Derive mode safely — the SELECT does not include require_screenshots,
        # so never call int() on it here. Default to 'off' if absent or invalid.
        raw_mode = str(cfg.get("mode") or "").strip().lower() or "off"
        if raw_mode not in ("off", "instant", "daily"):
            raw_mode = "off"
        if raw_mode == "off":
            continue
        if str(cfg.get("reminder_time") or "16:50") != hhmm:
            continue
        if str(cfg.get("last_reminder_date") or "") == today:
            continue

        # Mark once/day immediately to prevent repeated fires within the minute.
        _screenshot_set_last_reminder(mk, today)

        try:
            _ss_refresh_manager(mk)
        except Exception as exc:
            log.warning("ss refresh in reminder failed manager=%s error=%r", mk, exc)

        sk = _ss_source_for_manager(mk)
        expected_leads = _ss_expected_nonliquid_leads(mk, today)
        expected_count = len(expected_leads)
        if expected_count == 0:
            continue

        uploaded_count = _ss_uploaded_count(mk, sk, today)
        missing = max(0, expected_count - uploaded_count)

        # Determine which chat(s) to notify.
        # Use the most recently pending request's request_chat_id, or find via manager tg_user_id.
        notify_chats: List[int] = []
        con = _connect()
        try:
            chat_rows = con.execute(
                "SELECT DISTINCT request_chat_id FROM manager_screenshot_requests WHERE manager_key=? AND COALESCE(request_chat_id,0)>0",
                (mk,),
            ).fetchall()
            notify_chats = [int(r[0]) for r in chat_rows if int(r[0] or 0) > 0]
        except Exception:
            pass
        finally:
            con.close()

        if not notify_chats:
            # Fall back to manager's tg_user_id.
            con2 = _connect()
            try:
                r = con2.execute("SELECT tg_user_id FROM managers WHERE manager_key=? LIMIT 1", (mk,)).fetchone()
                if r and int(r[0] or 0) > 0:
                    notify_chats = [int(r[0])]
            except Exception:
                pass
            finally:
                con2.close()

        if not notify_chats:
            continue

        if uploaded_count >= expected_count:
            msg_text = (
                "✅ Супер\n\n"
                "Все скрины загружены.\n"
                f"Неликвиды: {expected_count}\n"
                f"Скрины: {uploaded_count}"
            )
        else:
            contact_lines = ""
            for i, lead in enumerate(expected_leads, 1):
                contact_lines += f"\n{i}. {_ss_contact_line_from_daily(lead)}"
            batch_hint = "" if raw_mode == "instant" else "\n\n📤 Загрузи скрины: /skrin"
            msg_text = (
                "⚠️ Не хватает скринов\n\n"
                f"Неликвиды: {expected_count}\n"
                f"Скрины: {uploaded_count}\n"
                f"Не хватает: {missing}\n"
                f"\nПроверь неликвиды за сегодня:{contact_lines}"
                f"{batch_hint}"
            )

        for chat_id in notify_chats:
            try:
                await client.send_message(int(chat_id), msg_text)
            except Exception as exc:
                log.warning("screenshot reminder send failed manager=%s chat=%s error=%r", mk, chat_id, exc)
# --- TPILOT MANAGER BOT SCREENSHOT CONTROL M2.9C END ---


# --- TPILOT MANAGER BOT M1 PER-LEAD REMINDERS START ---
# Reply-anchored, restart-safe, single rolling reminder per card/type.
_M1_NO_STATUS_DELAY_MIN = 30
_M1_REPEAT_MIN = 10
_M1_WORK_HOUR_START = 8   # Kyiv
_M1_WORK_HOUR_END = 22    # Kyiv (exclusive)
_M1_REMINDER_CAP_PER_CYCLE = 25
_M1_NO_STATUS_TEXT = "🚨 Поставьте статус по этому лиду"
_M1_SCREENSHOT_TEXT = "🚨 По неликвиду нужен скриншот. Ответьте скриншотом на эту карточку"


def _m1_age_minutes(iso_str) -> Optional[float]:
    """Minutes since a stored UTC iso timestamp; None if empty/unparseable."""
    raw = str(iso_str or "").strip()
    if not raw:
        return None
    try:
        return (datetime.utcnow() - datetime.fromisoformat(raw)).total_seconds() / 60.0
    except Exception:
        return None


def _m1_due(anchor_iso, last_reminder_iso, first_delay_min) -> bool:
    """Due if no reminder yet and the anchor is older than first_delay_min, OR the last
    reminder is older than the repeat interval."""
    last_age = _m1_age_minutes(last_reminder_iso)
    if last_age is None:
        anchor_age = _m1_age_minutes(anchor_iso)
        return anchor_age is not None and anchor_age >= first_delay_min
    return last_age >= _M1_REPEAT_MIN


def _m1_within_working_hours() -> bool:
    try:
        h = _mbstat_now().hour
    except Exception:
        return True  # fail-open: never go silent due to a clock error
    return _M1_WORK_HOUR_START <= h < _M1_WORK_HOUR_END


async def _maybe_send_lead_card_reminders() -> None:
    """M1: send reply-anchored per-lead reminders. Single rolling reminder per card/type:
    delete the previous reminder message before sending the next. Never deletes the lead
    card or an uploaded screenshot. Restart-safe (all state persisted)."""
    if not _m1_within_working_hours():
        return
    now = _now_iso()

    # ---- No-status reminders ----
    con = _connect()
    try:
        card_rows = con.execute(
            """
            SELECT id, tg_user_id, bot_chat_id, message_id, manager_key, created_at,
                   last_status_reminder_at, last_status_reminder_message_id
            FROM manager_lead_cards
            WHERE COALESCE(message_id,0) > 0
              AND COALESCE(status_set_at,'') = ''
              AND COALESCE(last_status_shown,'') = ''
            """
        ).fetchall()
        cards = [dict(r) for r in card_rows]
    except Exception:
        cards = []
    finally:
        con.close()

    sent = 0
    for c in cards:
        if sent >= _M1_REMINDER_CAP_PER_CYCLE:
            break
        tg_user_id = int(c.get("tg_user_id") or 0)
        manager_key = str(c.get("manager_key") or "")
        # Only managers with the status workflow.
        if not (tg_user_id and _access_allowed(tg_user_id) and _can_set_status(tg_user_id, manager_key)):
            continue
        if not _m1_due(c.get("created_at"), c.get("last_status_reminder_at"), _M1_NO_STATUS_DELAY_MIN):
            continue
        bot_chat_id = int(c.get("bot_chat_id") or tg_user_id or 0)
        card_msg = int(c.get("message_id") or 0)
        prev_id = int(c.get("last_status_reminder_message_id") or 0)
        if prev_id:
            await _safe_delete_message(bot_chat_id, prev_id)
        new_id = await _safe_reply_send(bot_chat_id, _M1_NO_STATUS_TEXT, reply_to=card_msg)
        con = _connect()
        try:
            con.execute(
                "UPDATE manager_lead_cards SET last_status_reminder_at=?, "
                "last_status_reminder_message_id=? WHERE id=?",
                (now, int(new_id or 0), int(c.get("id") or 0)),
            )
            con.commit()
        except Exception:
            pass
        finally:
            con.close()
        sent += 1

    # ---- Missing-screenshot reminders ----
    con = _connect()
    try:
        req_rows = con.execute(
            """
            SELECT id, manager_key, card_chat_id, card_message_id, created_at,
                   last_screenshot_reminder_at, last_screenshot_reminder_message_id
            FROM manager_screenshot_requests
            WHERE state='pending' AND COALESCE(active,1)=1 AND COALESCE(card_message_id,0) > 0
            """
        ).fetchall()
        reqs = [dict(r) for r in req_rows]
    except Exception:
        reqs = []
    finally:
        con.close()

    sent = 0
    for r in reqs:
        if sent >= _M1_REMINDER_CAP_PER_CYCLE:
            break
        # First reminder 10 min after the request was created; then every 10 min.
        if not _m1_due(r.get("created_at"), r.get("last_screenshot_reminder_at"), _M1_REPEAT_MIN):
            continue
        card_chat = int(r.get("card_chat_id") or 0)
        card_msg = int(r.get("card_message_id") or 0)
        if not (card_chat and card_msg):
            continue
        prev_id = int(r.get("last_screenshot_reminder_message_id") or 0)
        if prev_id:
            await _safe_delete_message(card_chat, prev_id)
        new_id = await _safe_reply_send(card_chat, _M1_SCREENSHOT_TEXT, reply_to=card_msg)
        con = _connect()
        try:
            con.execute(
                "UPDATE manager_screenshot_requests SET last_screenshot_reminder_at=?, "
                "last_screenshot_reminder_message_id=? WHERE id=?",
                (now, int(new_id or 0), int(r.get("id") or 0)),
            )
            con.commit()
        except Exception:
            pass
        finally:
            con.close()
        sent += 1
# --- TPILOT MANAGER BOT M1 PER-LEAD REMINDERS END ---


def _parse_card_callback(data: bytes, expected_action: str) -> int:
    text = data.decode("utf-8", errors="ignore")
    parts = text.split(":")
    if len(parts) != 3:
        return 0
    if parts[0] != "mb" or parts[1] != expected_action:
        return 0
    try:
        return int(parts[2])
    except Exception:
        return 0


class _MbPlaceholderUnavailable(Exception):
    """R1B correction (2026-08-13, closes independent-review Blocker B-1):
    raised internally (never sent to Telegram, never shown to a user) when
    _upsert_card_placeholder could not establish the manager_lead_cards row
    for this event before send. Routed through the EXISTING bounded
    retry/backoff/quarantine machinery (_mark_send_attempt) instead of a new
    state -- classifies as the default 'unexpected_internal' category
    (retryable=True), so a transient DB lock gets normal exponential backoff
    and a persistently broken placeholder eventually quarantines ('dead')
    exactly like any other undeliverable event, rather than looping forever
    or resending. Only the class name is ever logged (existing convention),
    never a text payload."""


async def _send_event_to_user(event_row: Dict[str, Any], tg_user_id: int) -> None:
    card_id = _upsert_card_placeholder(event_row, int(tg_user_id))

    # B-1 fix: a normal lead card must NEVER be sent without the card state
    # (manager_lead_cards row / message_id anchor / status buttons) that
    # _upsert_card_placeholder is responsible for establishing. The old
    # behavior silently degraded to buttons=None and still sent+marked
    # 'sent' -- the card became permanently un-actionable (no status
    # buttons, no message_id, and 'sent' is a terminal state that neither
    # _fetch_unsent_events_for_user nor _mb_recover_stale_claims ever
    # revisits). Now: on placeholder failure, NO Telegram send happens at
    # all, and the claim taken by _mb_claim_event above this call is
    # explicitly released into the bounded retry/backoff state via
    # _mark_send_attempt -- the event becomes selectable again on its own
    # backoff schedule (or is claimed by _mb_recover_stale_claims sooner if
    # the lease is still short), without waiting out the full stale-claim
    # lease window, and without ever inventing a resend of an
    # already-delivered card (nothing was delivered yet).
    if card_id <= 0:
        _mark_send_attempt(int(event_row.get("id") or 0), int(tg_user_id), _MbPlaceholderUnavailable())
        log.warning(
            "lead card send deferred, placeholder unavailable actor_ref=%s event_id=%s",
            _w2_actor_ref(tg_user_id), event_row.get("id"),
        )
        return

    payload = _decode_payload(event_row)

    manager_key = str(event_row.get("manager_key") or payload.get("manager_key") or "")
    chat_id = int(event_row.get("chat_id") or payload.get("chat_id") or 0)
    lead_date = str(event_row.get("lead_date") or payload.get("lead_date") or "")
    override = _read_override(manager_key, chat_id)

    text = _format_lead_card(event_row, override=override)
    text = _with_manual_marker(text, bool(override))

    buttons = _buttons_for_manual_state(
        card_id, bool(override), expanded=False,
        manager_key=manager_key, chat_id=chat_id, lead_date=lead_date,
        tg_user_id=int(tg_user_id or 0),
    )

    # Forward-fix Phase 3 (post-incident review 2026-07-26): this was the
    # ONE completely unguarded send in the whole delivery pipeline -- no
    # try/except at all, so an EntityBoundsInvalidError (or any other
    # formatting error, from the unsanitized interpolated lead reason/name
    # fields in _format_lead_card) propagated straight past this function
    # to the poll loop's own except, which only logged and moved on --
    # leaving NO recorded state, so the SAME event was re-selected and
    # re-sent every ~5s poll cycle forever. Now: a formatting failure gets
    # ONE plain-text retry (delivery-preserving); any failure that still
    # remains is recorded via _mark_send_attempt (bounded attempts,
    # backoff, eventual quarantine) instead of silently repeating.
    try:
        msg, fallback_used = await _mb_send_with_fallback(
            int(tg_user_id), text, event_id=event_row.get("id"), buttons=buttons,
        )
    except Exception as exc:
        _mark_send_attempt(int(event_row.get("id") or 0), int(tg_user_id), exc)
        log.warning(
            "lead card send failed uid=%s event_id=%s error_class=%s",
            tg_user_id, event_row.get("id"), type(exc).__name__,
        )
        return
    message_id = int(getattr(msg, "id", 0) or 0)

    if not _save_card_and_sent(event_row, int(tg_user_id), int(tg_user_id), message_id, card_id, fallback_used=fallback_used):
        log.warning("lead card sent but state persistence failed uid=%s event_id=%s card_id=%s msg_id=%s",
                    tg_user_id, event_row.get("id"), card_id, message_id)

    log.info(
        "lead card sent uid=%s event_id=%s manager=%s chat_id=%s msg_id=%s card_id=%s fallback=%s",
        tg_user_id,
        event_row.get("id"),
        event_row.get("manager_key"),
        event_row.get("chat_id"),
        message_id,
        card_id,
        fallback_used,
    )


# R1B/F-16 (plan section 23, parts F-16/F-32): QueryIdInvalidError means the
# callback query is already dead (expired, or already answered once by this
# same handler/dispatch race) -- calling event.answer() again cannot ever
# succeed and previously fell into the generic except-block "try again,
# swallow" pattern, which still attempted a second doomed RPC and logged the
# stale callback as if it were a real handler bug (noise indistinguishable
# from an actual defect). No telethon.errors import needed -- same
# class-name string-match convention as _mb_is_formatting_error above (keeps
# this testable via a plain Exception subclass, consistent with the rest of
# this file's error classification).
_MB_QUERY_INVALID_CLASS_NAMES = frozenset({"QueryIdInvalidError"})
_MB_QUERY_INVALID_TEXT_MARKERS = ("QUERY_ID_INVALID",)


def _mb_is_query_invalid_error(exc: BaseException) -> bool:
    if type(exc).__name__ in _MB_QUERY_INVALID_CLASS_NAMES:
        return True
    return any(marker in str(exc).upper() for marker in _MB_QUERY_INVALID_TEXT_MARKERS)


async def _handle_edit_status_callback(event, data: bytes) -> None:
    try:
        card_id = _parse_card_callback(data, "e")
        tg_user_id = int(event.sender_id or 0)

        if not card_id:
            await event.answer("\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430", alert=True)
            return

        card = _card_by_id(card_id, tg_user_id)
        if not card:
            await event.answer("\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430", alert=True)
            return

        manager_key = str(card.get("manager_key") or "")
        if not _can_set_status(tg_user_id, manager_key):
            await event.answer("\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430", alert=True)
            return

        event_row = _event_row_from_card(card)
        override = _read_override(manager_key, int(card.get("chat_id") or 0))

        text = _format_lead_card(event_row, override=override)
        text = _with_manual_marker(text, bool(override))

        await _safe_edit_card(event, text, _expanded_status_buttons(card_id))
        await event.answer("\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0441\u0442\u0430\u0442\u0443\u0441", alert=False)
    except Exception as exc:
        if _mb_is_query_invalid_error(exc):
            log.info("edit status callback: stale/expired query, no re-answer: %r", exc)
            return
        log.warning("edit status callback failed: %r", exc)
        try:
            await event.answer("\u041e\u0448\u0438\u0431\u043a\u0430", alert=True)
        except Exception:
            pass


async def _handle_back_status_callback(event, data: bytes) -> None:
    try:
        card_id = _parse_card_callback(data, "b")
        tg_user_id = int(event.sender_id or 0)

        if not card_id:
            await event.answer()
            return

        card = _card_by_id(card_id, tg_user_id)
        if not card:
            await event.answer("\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430", alert=True)
            return

        manager_key = str(card.get("manager_key") or "")
        chat_id = int(card.get("chat_id") or 0)
        event_row = _event_row_from_card(card)
        override = _read_override(manager_key, chat_id)

        text = _format_lead_card(event_row, override=override)
        text = _with_manual_marker(text, bool(override))

        buttons = _buttons_for_manual_state(
            card_id, bool(override), expanded=False,
            manager_key=manager_key, chat_id=chat_id, lead_date=str(card.get("lead_date") or ""),
            tg_user_id=tg_user_id,
        )
        await _safe_edit_card(event, text, buttons)
        await event.answer()
    except Exception as exc:
        if _mb_is_query_invalid_error(exc):
            log.info("back status callback: stale/expired query, no re-answer: %r", exc)
            return
        log.warning("back status callback failed: %r", exc)
        try:
            await event.answer("\u041e\u0448\u0438\u0431\u043a\u0430", alert=True)
        except Exception:
            pass


async def _handle_status_callback(event, data: bytes) -> None:
    try:
        text = data.decode("utf-8", errors="ignore")
        parts = text.split(":")
        if len(parts) != 4 or parts[0] != "mb" or parts[1] != "s":
            await event.answer()
            return

        card_id = int(parts[2])
        code = parts[3]
        tg_user_id = int(event.sender_id or 0)

        if code != "clear" and code not in STATUS_ACTIONS:
            await event.answer("\u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u044b\u0439 \u0441\u0442\u0430\u0442\u0443\u0441", alert=True)
            return

        if not _access_allowed(tg_user_id):
            await event.answer("\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430", alert=True)
            return

        card = _card_by_id(card_id, tg_user_id)
        if not card:
            await event.answer("\u041a\u0430\u0440\u0442\u043e\u0447\u043a\u0430 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u0430", alert=True)
            return

        manager_key = str(card.get("manager_key") or "")
        chat_id = int(card.get("chat_id") or 0)

        if not _can_set_status(tg_user_id, manager_key):
            await event.answer("\u041d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u0430", alert=True)
            return

        override_before = _read_override(manager_key, chat_id)

        wait = _check_throttle(card, 20)
        if wait:
            await event.answer(f"\u041f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435 {wait}\u0441", alert=False)
            return

        if code == "clear" and not override_before:
            event_row = _event_row_from_card(card)
            new_text = _format_lead_card(event_row, override={})
            buttons = _with_upload_button(
                _expanded_status_buttons(card_id), manager_key, chat_id, str(card.get("lead_date") or ""),
            )
            buttons = _with_transfer_button(buttons, manager_key, chat_id, tg_user_id, card_id)
            await _safe_edit_card(event, new_text, _with_copy_button(buttons, _ss_username_from_event_row(event_row)))
            await event.answer("\u0420\u0443\u0447\u043d\u043e\u0439 \u0441\u0442\u0430\u0442\u0443\u0441 \u0443\u0436\u0435 \u043d\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d", alert=False)
            return

        # R1A1-E B-3 FINAL: the "already installed" short circuit must be decided
        # by the AUTHORITATIVE stats DB, not by _read_override (which scans the
        # central control-plane mirror FIRST). A status present only in a mirror
        # must NOT be able to skip the authoritative write -- that is the same
        # false-success class as the one B-3 closes on the write side.
        if code != "clear" and _same_manual_status(_mb_authoritative_override(manager_key, chat_id), code):
            event_row = _event_row_from_card(card)
            new_text = _format_lead_card(event_row, override=override_before)
            new_text = _with_manual_marker(new_text, True)
            buttons = _with_upload_button(
                _collapsed_status_buttons(card_id), manager_key, chat_id, str(card.get("lead_date") or ""),
            )
            buttons = _with_transfer_button(buttons, manager_key, chat_id, tg_user_id, card_id)
            await _safe_edit_card(event, new_text, _with_copy_button(buttons, _ss_username_from_event_row(event_row)))
            await event.answer("\u042d\u0442\u043e\u0442 \u0441\u0442\u0430\u0442\u0443\u0441 \u0443\u0436\u0435 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d", alert=False)
            return

        ok = _apply_manual_status(card, code, tg_user_id)
        if not ok:
            await event.answer("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0441\u0442\u0430\u0442\u0443\u0441", alert=True)
            return

        event_row = _event_row_from_card(card)
        override_after = _read_override(manager_key, chat_id)

        new_text = _format_lead_card(event_row, override=override_after)
        new_text = _with_manual_marker(new_text, bool(override_after))

        buttons = _buttons_for_manual_state(
            card_id,
            bool(override_after),
            expanded=(code == "clear"),
            manager_key=manager_key, chat_id=chat_id, lead_date=str(card.get("lead_date") or ""),
            tg_user_id=tg_user_id,
        )
        buttons = _with_copy_button(buttons, _ss_username_from_event_row(event_row))

        await _safe_edit_card(event, new_text, buttons)

        # M1: track status_set_at so the no-status reminder stops once any status is set
        # (and resumes if the manager clears it). Also drop the rolling no-status reminder.
        try:
            await _card_mark_status_set(card, int(tg_user_id), set_now=(code != "clear"))
        except Exception as exc:
            log.warning("status_set_at update failed: %r", exc)

        # M2.9C: ForceReply only in 'instant' mode. 'daily'/'off' do nothing here.
        if code in SCREENSHOT_CODES and _screenshot_mode(manager_key) == "instant":
            try:
                await _send_screenshot_request(
                    int(event.chat_id or tg_user_id),
                    manager_key,
                    chat_id,
                    str(card.get("lead_date") or ""),
                    tg_user_id,
                    code,
                    card_message_id=int(card.get("message_id") or 0),
                    card_chat_id=int(card.get("bot_chat_id") or event.chat_id or tg_user_id),
                )
            except Exception as exc:
                log.warning("screenshot request hook failed: %r", exc)

        if code == "clear":
            label = "\u0421\u0431\u0440\u043e\u0448\u0435\u043d\u043e"
        else:
            label = STATUS_ACTIONS[code]["label"]

        await event.answer(f"\u0421\u0442\u0430\u0442\u0443\u0441 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d: {label}", alert=False)
        log.info("manual status set uid=%s card_id=%s code=%s", tg_user_id, card_id, code)
    except Exception as exc:
        if _mb_is_query_invalid_error(exc):
            # R1B/F-16: the status was already applied above (_apply_manual_status
            # already committed) -- only the final acknowledgement RPC hit a dead
            # query. A second answer() attempt is pointless and previously
            # produced exactly the "Task exception was never retrieved /
            # QueryIdInvalidError" noise seen in production panel_bot.err.log.
            log.info("status callback: stale/expired query, no re-answer: %r", exc)
        else:
            log.warning("status callback failed: %r", exc)
            try:
                await event.answer("\u041e\u0448\u0438\u0431\u043a\u0430 \u043e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0438", alert=True)
            except Exception:
                pass

# --- TPILOT MANAGER BOT UI COLLAPSE M2.7 20260601 END ---


# --- TPILOT MANAGER BOT PERMISSION GATES M2.6C 20260601 START ---
# Enforce manager_bot_access permissions.
# Requires M2.6B backfill.
# Fail closed:
# - no row = no cards / no status buttons action
# - revoked=1 = no cards / no status buttons action

_MANAGER_BOT_PERMISSION_FLAGS = {
    "can_receive_cards",
    "can_set_status",
    "can_view_stats",
}


def _manager_bot_access_row(tg_user_id: int, manager_key: str) -> Dict[str, Any]:
    uid = int(tg_user_id or 0)
    mk = _norm_key(manager_key)

    if not uid or not mk:
        return {}

    con = _connect()
    try:
        row = con.execute(
            """
            SELECT *
            FROM manager_bot_access
            WHERE tg_user_id=? AND manager_key=?
            LIMIT 1
            """,
            (uid, mk),
        ).fetchone()
        return dict(row) if row else {}
    except Exception as exc:
        log.warning("manager_bot_access read failed uid=%s manager=%s error=%r", uid, mk, exc)
        return {}
    finally:
        con.close()


def _manager_bot_has_permission(tg_user_id: int, manager_key: str, flag: str) -> bool:
    uid = int(tg_user_id or 0)
    mk = _norm_key(manager_key)

    if flag not in _MANAGER_BOT_PERMISSION_FLAGS:
        return False

    if not uid or not mk:
        return False

    if not _access_allowed(uid):
        return False

    linked = {_norm_key(k) for k in _linked_manager_keys(uid)}
    if mk not in linked:
        return False

    row = _manager_bot_access_row(uid, mk)
    if not row:
        return False

    if int(row.get("revoked") or 0) != 0:
        return False

    return int(row.get(flag) or 0) == 1


def _can_receive_cards(tg_user_id: int, manager_key: str) -> bool:
    """REVISION (2026-07-29, Blocker 2 follow-up): delivery eligibility is
    resolved via storage.w2_access_decision (manager_bot_access-grounded)
    directly -- NOT via _manager_bot_has_permission's access_targets-
    linked-set requirement, which _can_set_status still (correctly) uses
    for its own UI-interaction purpose (a manager clicking a status button
    on an already-delivered card, which legitimately still requires UI
    linkage). Routing card-delivery eligibility through the linked-set
    gate would silently reproduce D-02 one layer deeper: _fetch_unsent_
    events_for_user already selects a candidate event using storage.
    w2_access_decision's delivery_manager_keys (see _poll_loop); if THIS
    function then re-gated on a DIFFERENT (access_targets-based) set, a
    manager_bot_access grant with no access_targets row would be selected
    upstream and then silently rejected right here -- exactly the bug
    this whole revision exists to remove. (Found and fixed during this
    same revision pass -- see 02_ACCESS_AUTHORITY.md.)"""
    decision = storage.w2_access_decision(int(tg_user_id or 0), manager_key=manager_key, db_path=TPILOT_DB_PATH)
    return decision["status"] == "allowed"


def _can_set_status(tg_user_id: int, manager_key: str) -> bool:
    return _manager_bot_has_permission(int(tg_user_id or 0), manager_key, "can_set_status")


def _fetch_unsent_events_for_user(tg_user_id: int, manager_keys: List[str]) -> List[Dict[str, Any]]:
    uid = int(tg_user_id or 0)
    keys = sorted({_norm_key(k) for k in (manager_keys or []) if _norm_key(k)})

    if not uid or not keys:
        return []

    placeholders = ",".join(["?"] * len(keys))
    # Forward-fix Phase 3 (post-incident review 2026-07-26): "eligible to
    # send" now admits TWO cases -- (a) never attempted at all (no row in
    # manager_bot_sent, the original definition, unchanged), OR (b)
    # previously FAILED but its backoff window has elapsed
    # (send_status='failed' AND next_attempt_at<=now). A 'dead'
    # (quarantined) row matches NEITHER branch, so it is permanently
    # excluded without ever deleting the underlying event row. A 'sent'
    # row (the only status a legacy pre-migration row can have) also
    # matches neither branch -- unchanged behavior for every already-
    # delivered event.
    now_iso = _now_iso()
    params: List[Any] = [uid, uid, now_iso, *keys]

    con = _connect()
    try:
        rows = con.execute(
            f"""
            SELECT e.*
            FROM manager_bot_events e
            JOIN manager_bot_access mba
              ON mba.tg_user_id = ?
             AND mba.manager_key = e.manager_key
            LEFT JOIN manager_bot_sent s
              ON s.event_id = e.id
             AND s.tg_user_id = ?
            WHERE (
                s.event_id IS NULL
                OR (
                    s.send_status = 'failed'
                    AND (s.next_attempt_at = '' OR s.next_attempt_at <= ?)
                )
            )
              AND e.manager_key IN ({placeholders})
              AND COALESCE(mba.can_receive_cards, 0) = 1
              AND COALESCE(mba.revoked, 0) = 0
              AND e.id > COALESCE(mba.event_cutoff_id, 0)
            ORDER BY ROW_NUMBER() OVER (PARTITION BY e.manager_key ORDER BY e.id ASC), e.id ASC
            LIMIT 30
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.warning("permissioned unsent events read failed actor_ref=%s error_class=%s",
                    _w2_actor_ref(uid), type(exc).__name__)
        return []
    finally:
        con.close()
# W2 (2026-07-29, D-01 item 5, "honesty under poison events"): ordering is
# round-robin ACROSS manager_key (rank-within-manager_key first, global id
# only as the tiebreaker) instead of a single global `ORDER BY e.id ASC`.
# One manager_key that has produced a long, low-id run of new events (a
# burst, or a poison event sitting at the front of ITS OWN queue) can no
# longer consume the entire LIMIT 30 window and starve a different
# manager_key's events for this same tg_user_id. Every existing selftest
# uses a single manager_key, where this degenerates to the prior plain
# `ORDER BY e.id ASC` -- byte-identical results, so no existing assertion
# changes.


_M26C_ORIG_SEND_EVENT_TO_USER = globals().get("_send_event_to_user")


def _format_duplicate_card(event_row: Dict[str, Any]) -> str:
    """M2.11C / M2.12B: render a ManagerBot cross-manager duplicate card.
    Shows lead identity (Лид block), current/origin manager + timestamps.
    Never shows buyer/source_key/payment info. No status buttons attached."""
    payload = _decode_payload(event_row)

    def _fmt_dt(utc_v: Any, kyiv_v: Any) -> str:
        k = str(kyiv_v or "").strip()
        if k:
            return k
        return str(utc_v or "").strip()

    # --- Lead identity ---
    full_name = str(payload.get("full_name") or "").strip()
    username = str(payload.get("username") or "").strip().lstrip("@")
    phone = str(payload.get("phone") or "").strip()
    chat_id_val = int(payload.get("chat_id") or 0)

    # --- Current manager ---
    cur_disp = str(payload.get("manager_display_name") or "").strip()
    cur_user = str(payload.get("manager_username") or "").strip().lstrip("@")
    cur_key = str(payload.get("manager_key") or "").strip()
    # TPILOT IDENTITY RECONCILE 20260809: manager_display_name/manager_username above
    # come from the frozen event payload_json snapshot (written once when the event
    # was created) -- prefer the LIVE managers row via the existing
    # _sched_manager_label_mb (already used by the schedule/transfer screens; never
    # raises, returns the bare key when there is no row or the row itself is blank).
    # Fall back to the snapshot-built label only when that lookup can't improve on it.
    cur_current = _sched_manager_label_mb(cur_key) if cur_key else ""
    if cur_current and cur_current != cur_key:
        cur_label = cur_current
    elif cur_disp and cur_user:
        cur_label = f"{cur_disp} | @{cur_user}"
    elif cur_user:
        cur_label = f"@{cur_user}"
    else:
        cur_label = cur_disp or cur_key or "-"

    # --- Origin/previous manager ---
    prev_disp = str(payload.get("previous_manager_display_name") or "").strip()
    prev_user = str(payload.get("previous_manager_username") or "").strip().lstrip("@")
    prev_key = _norm_key(payload.get("previous_manager_key") or "")
    prev_current = _sched_manager_label_mb(prev_key) if prev_key else ""
    if prev_current and prev_current != prev_key:
        prev_label = prev_current
    elif prev_disp and prev_user:
        prev_label = f"{prev_disp} | @{prev_user}"
    elif prev_user:
        prev_label = f"@{prev_user}"
    elif prev_disp:
        prev_label = prev_disp
    else:
        prev_label = ""

    cur_seen = _fmt_dt(payload.get("current_seen_utc"), payload.get("current_seen_kyiv"))
    first_seen = _fmt_dt(payload.get("first_seen_global_at"), payload.get("first_seen_global_kyiv"))
    prev_seen = _fmt_dt(payload.get("previous_seen_utc"), payload.get("previous_seen_kyiv"))

    # M2.11C.1: only show "Последний предыдущий контакт" when the latest previous
    # contact provably belongs to the SAME manager shown as the previous manager
    # (first_seen_global_manager_key). The local inbound_events timestamp comes
    # from the current manager, so for cross-manager duplicates it must NOT be
    # attributed under the origin manager's label. When attribution is uncertain
    # or mismatched, fall back to "Первый контакт" only.
    prev_mgr_key = _norm_key(payload.get("previous_manager_key") or "")
    origin_mgr_key = _norm_key(payload.get("first_seen_global_manager_key") or "")
    show_latest_prev = bool(
        prev_mgr_key
        and origin_mgr_key
        and prev_mgr_key == origin_mgr_key
        and prev_seen
        and prev_seen != first_seen
    )

    lines = ["🔁 Дубликат", ""]

    # Лид block — show only non-empty identity fields, never show blank labels.
    lead_lines: list = []
    if full_name:
        lead_lines.append(f"Имя: {full_name}")
    if username:
        lead_lines.append(f"Юзер: @{username}")
    if phone:
        lead_lines.append(f"Телефон: {phone}")
    if chat_id_val:
        lead_lines.append(f"Telegram ID: {chat_id_val}")
    if lead_lines:
        lines.append("Лид:")
        lines.extend(lead_lines)
        lines.append("")

    lines.append("Сейчас написал:")
    lines.append(cur_label)
    lines.append(f"Дата сейчас: {cur_seen or '-'}")
    lines.append("")

    if prev_label:
        lines.append("Ранее писал:")
        lines.append(prev_label)
        if first_seen:
            lines.append(f"Первый контакт: {first_seen}")
        if show_latest_prev:
            lines.append(f"Последний предыдущий контакт: {prev_seen}")
    else:
        # No safe previous manager label — generic internal note, no buyer/source.
        lines.append("Ранее уже писал в систему")
        if first_seen:
            lines.append(f"Первый контакт: {first_seen}")
    lines.append("")
    lines.append("Статус:")
    lines.append(str(payload.get("status_text") or "не считается в оплату"))
    return "\n".join(lines).rstrip()


async def _send_event_to_user(event_row: Dict[str, Any], tg_user_id: int) -> None:
    payload = _decode_payload(event_row)
    manager_key = _norm_key(event_row.get("manager_key") or payload.get("manager_key") or "")
    event_id = int(event_row.get("id") or 0)
    uid = int(tg_user_id or 0)

    # REVISION (2026-07-29, Blocker 3): every non-attempt outcome below
    # gets a REAL, persisted, observable terminal state -- not just a
    # log.info + silent return. TERMINAL_INVALID_EVENT and
    # TERMINAL_MANAGER_DELETED are new checks (the pre-revision code had
    # no structural-validity check at all, and no direct managers-table
    # cross-reference -- it relied entirely on manager_bot_access already
    # being correctly revoked for a deleted manager, which is usually but
    # not provably always true). TERMINAL_ACCESS_DENIED replaces the old
    # silent return for the pre-existing _can_receive_cards safety-net
    # check (this is normally a no-op, since _fetch_unsent_events_for_user
    # already filters by the same manager_bot_access condition -- this
    # branch only fires on a genuine TOCTOU race, access revoked in the
    # narrow window between selection and send).
    chat_id = int(event_row.get("chat_id") or payload.get("chat_id") or 0)
    if not manager_key or not chat_id:
        _mb_write_terminal_state(event_id, uid, "terminal_invalid_event", "missing_required_fields")
        log.info("event skipped, structurally invalid event_id=%s", event_id)
        return

    mgr_row = _find_manager_row_by_key(manager_key)
    if not mgr_row:
        _mb_write_terminal_state(event_id, uid, "terminal_manager_deleted", "manager_row_absent_or_inactive")
        log.info("event skipped, manager deleted/inactive manager=%s event_id=%s", manager_key, event_id)
        return

    if not _can_receive_cards(uid, manager_key):
        _mb_write_terminal_state(event_id, uid, "terminal_access_denied", "access_denied_at_send_time")
        log.info(
            "lead card skipped by permission actor_ref=%s manager=%s event_id=%s",
            _w2_actor_ref(tg_user_id),
            manager_key,
            event_row.get("id"),
        )
        return

    # W2 (2026-07-29, CLAIMED state, I-04/claim-atomicity): this is the ONE
    # entry point every event type below goes through (duplicate_card,
    # reserve_activated/deactivated, transfer_created/confirmed, and the
    # default lead_card path delegated to _M26C_ORIG_SEND_EVENT_TO_USER at
    # the bottom of this function) -- claiming here, once, covers all of
    # them. If the claim fails (another pass already claimed or already
    # delivered this exact (tg_user_id, event_id)), this call returns
    # immediately without sending -- prevents a double-send across two
    # overlapping workers/poll passes.
    if not _mb_claim_event(int(event_row.get("id") or 0), int(tg_user_id or 0)):
        log.info(
            "event skipped, already claimed or delivered actor_ref=%s event_id=%s",
            _w2_actor_ref(tg_user_id), event_row.get("id"),
        )
        return

    # M2.11C: duplicate / repeat-contact card — informational only, no manual
    # paid-status buttons. Reuses manager_bot_sent dedupe via _save_card_and_sent
    # with card_id=0 (no manager_lead_cards / manual-override state created).
    if str(event_row.get("event_type") or "") == "duplicate_card":
        text = _format_duplicate_card(event_row)
        try:
            msg, fallback_used = await _mb_send_with_fallback(int(tg_user_id), text, event_id=event_row.get("id"))
        except Exception as exc:
            _mark_send_attempt(int(event_row.get("id") or 0), int(tg_user_id), exc)
            log.warning(
                "duplicate card send failed actor_ref=%s event_id=%s error_class=%s",
                _w2_actor_ref(tg_user_id), event_row.get("id"), type(exc).__name__,
            )
            return
        message_id = int(getattr(msg, "id", 0) or 0)
        if not _save_card_and_sent(event_row, int(tg_user_id), int(tg_user_id), message_id, 0, fallback_used=fallback_used):
            log.warning("duplicate card sent but state persistence failed actor_ref=%s event_id=%s manager=%s msg_id=%s",
                        _w2_actor_ref(tg_user_id), event_row.get("id"), manager_key, message_id)
        log.info(
            "duplicate card sent actor_ref=%s event_id=%s manager=%s msg_id=%s",
            _w2_actor_ref(tg_user_id), event_row.get("id"), manager_key, message_id,
        )
        return

    # PATCH B: reserve_activated — plain informational message, no status buttons.
    if str(event_row.get("event_type") or "") == "reserve_activated":
        _rsv_primary = str(payload.get("primary") or manager_key or "")
        _rsv_reserve = str(payload.get("reserve") or "")
        _rsv_text = (
            "⚠️ Активирован "
            "резервный аккаунт\n\n"
            "Ваш резервный аккаунт "
            "включён в работу "
            "вместо основного.\n"
            "Пожалуйста, зайдите "
            "на резервный аккаунт "
            "и проверьте, что "
            "всё работает корректно.\n\n"
            f"Основной аккаунт: {_rsv_primary}\n"
            f"Резервный аккаунт: {_rsv_reserve}"
        )
        try:
            msg, fallback_used = await _mb_send_with_fallback(int(tg_user_id), _rsv_text, event_id=event_row.get("id"))
        except Exception as exc:
            _mark_send_attempt(int(event_row.get("id") or 0), int(tg_user_id), exc)
            log.warning(
                "reserve_activated send failed actor_ref=%s event_id=%s error_class=%s",
                _w2_actor_ref(tg_user_id), event_row.get("id"), type(exc).__name__,
            )
            return
        message_id = int(getattr(msg, "id", 0) or 0)
        if not _save_card_and_sent(event_row, int(tg_user_id), int(tg_user_id), message_id, 0, fallback_used=fallback_used):
            log.warning("reserve_activated sent but state persistence failed actor_ref=%s event_id=%s msg_id=%s",
                        _w2_actor_ref(tg_user_id), event_row.get("id"), message_id)
        log.info(
            "reserve_activated sent actor_ref=%s event_id=%s primary=%s reserve=%s msg_id=%s",
            _w2_actor_ref(tg_user_id), event_row.get("id"), _rsv_primary, _rsv_reserve, message_id,
        )
        return

    # PATCH B2: reserve_deactivated — plain informational message, no status buttons.
    if str(event_row.get("event_type") or "") == "reserve_deactivated":
        _rsvd_primary = str(payload.get("primary") or manager_key or "")
        _rsvd_reserve = str(payload.get("reserve") or "")
        _rsvd_text = (
            "ℹ️ "
            + "Резервный аккаунт отключён\n\n"
            + "Резервный аккаунт больше не работает вместо основного.\n"
            + "Продолжайте работу с основного аккаунта.\n\n"
            + "Основной аккаунт: " + _rsvd_primary + "\n"
            + "Резервный аккаунт: " + _rsvd_reserve
        )
        try:
            msg, fallback_used = await _mb_send_with_fallback(int(tg_user_id), _rsvd_text, event_id=event_row.get("id"))
        except Exception as exc:
            _mark_send_attempt(int(event_row.get("id") or 0), int(tg_user_id), exc)
            log.warning(
                "reserve_deactivated send failed actor_ref=%s event_id=%s error_class=%s",
                _w2_actor_ref(tg_user_id), event_row.get("id"), type(exc).__name__,
            )
            return
        message_id = int(getattr(msg, "id", 0) or 0)
        if not _save_card_and_sent(event_row, int(tg_user_id), int(tg_user_id), message_id, 0, fallback_used=fallback_used):
            log.warning("reserve_deactivated sent but state persistence failed actor_ref=%s event_id=%s msg_id=%s",
                        _w2_actor_ref(tg_user_id), event_row.get("id"), message_id)
        log.info(
            "reserve_deactivated sent actor_ref=%s event_id=%s primary=%s reserve=%s msg_id=%s",
            _w2_actor_ref(tg_user_id), event_row.get("id"), _rsvd_primary, _rsvd_reserve, message_id,
        )
        return

    # TPILOT TRANSFERS STAGE2: transfer_created — plain informational card for the closer.
    # No lead status buttons, no questionnaire buttons. Dedupe via manager_bot_sent
    # (tg_user_id, event_id) PK, same as reserve_activated/reserve_deactivated above.
    if str(event_row.get("event_type") or "") == "transfer_created":
        _trc_payload = payload
        _trc_from_label = str(_trc_payload.get("from_manager_label") or _trc_payload.get("from_manager_key") or manager_key or "")
        _trc_raw_text = str(_trc_payload.get("raw_text") or "")
        _trc_text = "\n".join([
            "🤝 Новая передача",
            "",
            f"От: {_trc_from_label}",
            "",
            _trc_raw_text,
        ]).rstrip()
        try:
            msg, fallback_used = await _mb_send_with_fallback(int(tg_user_id), _trc_text, event_id=event_row.get("id"))
        except Exception as exc:
            _mark_send_attempt(int(event_row.get("id") or 0), int(tg_user_id), exc)
            log.warning(
                "transfer_created send failed actor_ref=%s event_id=%s error_class=%s",
                _w2_actor_ref(tg_user_id), event_row.get("id"), type(exc).__name__,
            )
            return
        message_id = int(getattr(msg, "id", 0) or 0)
        if not _save_card_and_sent(event_row, int(tg_user_id), int(tg_user_id), message_id, 0, fallback_used=fallback_used):
            log.warning("transfer_created sent but state persistence failed actor_ref=%s event_id=%s msg_id=%s",
                        _w2_actor_ref(tg_user_id), event_row.get("id"), message_id)
        log.info(
            "transfer_created sent actor_ref=%s event_id=%s from=%s msg_id=%s",
            _w2_actor_ref(tg_user_id), event_row.get("id"), _trc_from_label, message_id,
        )
        return

    # TPILOT TRANSFERS STAGE3: transfer_confirmed — one shared branch for both sides,
    # keyed by payload['target'] ('manager' | 'closer'). No status buttons, no
    # questionnaire buttons. Dedupe via manager_bot_sent (tg_user_id, event_id) PK.
    if str(event_row.get("event_type") or "") == "transfer_confirmed":
        _tcf_payload = payload
        _tcf_target = str(_tcf_payload.get("target") or "").strip().lower()
        _tcf_raw_text = str(_tcf_payload.get("raw_text") or "")
        _tcf_confirmed_at = str(_tcf_payload.get("confirmed_at") or "")
        if _tcf_target == "manager":
            _tcf_other_key = str(_tcf_payload.get("closer_key") or "")
            _tcf_other_line = f"Клоузер: {_tr_manager_label(_tcf_other_key) if _tcf_other_key else '-'}"
        else:
            _tcf_other_key = str(_tcf_payload.get("manager_key") or "")
            _tcf_other_line = f"От: {_tr_manager_label(_tcf_other_key) if _tcf_other_key else '-'}"
        _tcf_lines = ["✅ Лид написал клоузеру", "", _tcf_other_line]
        if _tcf_confirmed_at:
            _tcf_lines.append(f"Подтверждено: {_tcf_confirmed_at}")
        _tcf_lines.append("")
        _tcf_lines.append(_tcf_raw_text)
        _tcf_text = "\n".join(_tcf_lines).rstrip()
        try:
            msg, fallback_used = await _mb_send_with_fallback(int(tg_user_id), _tcf_text, event_id=event_row.get("id"))
        except Exception as exc:
            _mark_send_attempt(int(event_row.get("id") or 0), int(tg_user_id), exc)
            log.warning(
                "transfer_confirmed send failed actor_ref=%s event_id=%s error_class=%s",
                _w2_actor_ref(tg_user_id), event_row.get("id"), type(exc).__name__,
            )
            return
        message_id = int(getattr(msg, "id", 0) or 0)
        if not _save_card_and_sent(event_row, int(tg_user_id), int(tg_user_id), message_id, 0, fallback_used=fallback_used):
            log.warning("transfer_confirmed sent but state persistence failed actor_ref=%s event_id=%s msg_id=%s",
                        _w2_actor_ref(tg_user_id), event_row.get("id"), message_id)
        log.info(
            "transfer_confirmed sent actor_ref=%s event_id=%s target=%s msg_id=%s",
            _w2_actor_ref(tg_user_id), event_row.get("id"), _tcf_target, message_id,
        )
        return

    if callable(_M26C_ORIG_SEND_EVENT_TO_USER):
        await _M26C_ORIG_SEND_EVENT_TO_USER(event_row, tg_user_id)

# --- TPILOT MANAGER BOT PERMISSION GATES M2.6C 20260601 END ---


# --- TPILOT MANAGER BOT AUTO GRANT M2.6D 20260601 START ---
# Auto-grant ManagerBot access only by exact Telegram user_id match.
# Username match creates pending request only.
# Disabled/revoked users are not re-enabled automatically.
#
# 20260727: /start no longer delegates to a captured "original" handler --
# the canonical storage.manager_bot_access_ensure_sync (called directly from
# the active _handle_start, below) is now the single grant+self-heal path
# for every case this delegate used to cover.


def _sender_display_name(first_name: str, last_name: str) -> str:
    return " ".join([x for x in [str(first_name or "").strip(), str(last_name or "").strip()] if x]).strip()


async def _manager_bot_sender_identity(event) -> Dict[str, Any]:
    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        sender = None

    username = str(getattr(sender, "username", "") or "").strip()
    first_name = str(getattr(sender, "first_name", "") or "").strip()
    last_name = str(getattr(sender, "last_name", "") or "").strip()

    return {
        "tg_user_id": int(getattr(event, "sender_id", 0) or 0),
        "username": username,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": _sender_display_name(first_name, last_name),
    }


def _find_active_manager_by_tg_user_id(tg_user_id: int) -> Dict[str, Any]:
    uid = int(tg_user_id or 0)
    if not uid:
        return {}

    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT manager_key, display_name, tg_user_id, telegram_username,
                   status, is_enabled, manual_stopped
            FROM managers
            WHERE tg_user_id=?
              AND COALESCE(is_enabled, 0) = 1
              AND COALESCE(manual_stopped, 0) = 0
              AND COALESCE(status, '') = 'active'
              AND COALESCE(manager_key, '') <> ''
            ORDER BY manager_key ASC
            """,
            (uid,),
        ).fetchall()

        if len(rows) != 1:
            return {}

        return dict(rows[0])
    except Exception as exc:
        log.warning("active manager by tg_user_id lookup failed uid=%s error=%r", uid, exc)
        return {}
    finally:
        con.close()


def _find_active_manager_by_username(username: str) -> Dict[str, Any]:
    uname = str(username or "").strip().lstrip("@").lower()
    if not uname:
        return {}

    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT manager_key, display_name, tg_user_id, telegram_username,
                   status, is_enabled, manual_stopped
            FROM managers
            WHERE lower(trim(replace(COALESCE(telegram_username, ''), '@', ''))) = ?
              AND COALESCE(is_enabled, 0) = 1
              AND COALESCE(manual_stopped, 0) = 0
              AND COALESCE(status, '') = 'active'
              AND COALESCE(manager_key, '') <> ''
            ORDER BY manager_key ASC
            """,
            (uname,),
        ).fetchall()

        if len(rows) != 1:
            return {}

        return dict(rows[0])
    except Exception as exc:
        log.warning("active manager by username lookup failed username=%s error=%r", uname, exc)
        return {}
    finally:
        con.close()


def _manager_bot_permission_revoked(tg_user_id: int, manager_key: str) -> bool:
    row = _manager_bot_access_row(int(tg_user_id or 0), manager_key)
    return bool(row and int(row.get("revoked") or 0) != 0)


# --- TPILOT MANAGERBOT DEAD-CODE REMOVAL 20260727 (round-7 review, R-2) ---
# _auto_grant_manager_bot_access (formerly here) was a zero-caller top-level
# def -- confirmed via project-wide grep (only its own signature) and a scan
# of manager_bot.py for globals()/getattr(sys.modules)/eval/exec-based
# dynamic dispatch (none reference it). Its own `ON CONFLICT(tg_user_id) DO
# UPDATE SET access_level=1, scope_mode='selected', is_enabled=1` would have
# downgraded a scope_mode='all' user, reset access_level, and silently
# re-enabled an admin-disabled account -- exactly what A3/A18/F8 in
# tools/manager_bot_access_grant_selftest.py now assert must never happen.
# Fully removed rather than left unreachable, per the review's verdict.
# Superseded entirely by storage.manager_bot_access_ensure_sync.


def _upsert_manager_access_request(identity: Dict[str, Any], manager_row: Dict[str, Any], match_kind: str) -> None:
    uid = int(identity.get("tg_user_id") or 0)
    if not uid:
        return

    now = _now_iso()
    username = str(identity.get("username") or "").strip().lstrip("@")
    first_name = str(identity.get("first_name") or "").strip()
    last_name = str(identity.get("last_name") or "").strip()
    manager_key = _norm_key(manager_row.get("manager_key") or "")

    con = _connect()
    try:
        con.execute(
            """
            INSERT INTO manager_access_requests(
                user_id, username, first_name, last_name,
                matched_manager_key, match_kind, status,
                requested_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                matched_manager_key=excluded.matched_manager_key,
                match_kind=excluded.match_kind,
                status=CASE
                    WHEN manager_access_requests.status IN ('approved', 'denied') THEN manager_access_requests.status
                    ELSE 'new'
                END,
                updated_at=excluded.updated_at
            """,
            (uid, username, first_name, last_name, manager_key, str(match_kind or ""), now, now),
        )
        con.commit()
        log.info("manager_bot access request saved uid=%s username=%s manager=%s kind=%s", uid, username, manager_key, match_kind)
    except Exception as exc:
        log.warning("manager_bot access request failed uid=%s error=%r", uid, exc)
    finally:
        con.close()


# --- TPILOT RECONNECT RECONCILE M2.6F START ---

def _old_keys_for_tg_user(uid: int, current_key: str) -> List[str]:
    """Return distinct manager_keys from lead cards for this uid, excluding current_key."""
    uid = int(uid or 0)
    ck = _norm_key(current_key)
    if not uid or not ck:
        return []
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT DISTINCT manager_key FROM manager_lead_cards
            WHERE tg_user_id=? AND COALESCE(manager_key,'') <> '' AND manager_key <> ?
            """,
            (uid, ck),
        ).fetchall()
        return [_norm_key(r["manager_key"]) for r in rows if _norm_key(r["manager_key"])]
    except Exception as exc:
        log.warning("[reconcile] old_keys lookup failed uid=%s error=%r", uid, exc)
        return []
    finally:
        con.close()


def _diagnose_reconnect(uid: int) -> dict:
    """Dry-run: return counts of what would be reconciled for this uid. No writes."""
    uid = int(uid or 0)
    result: dict = {"uid": uid, "old_keys": [], "cards": 0, "events": 0}
    if not uid:
        return result
    active = _find_active_manager_by_tg_user_id(uid)
    if not active:
        result["note"] = "no single active manager found"
        return result
    current_key = _norm_key(active.get("manager_key") or "")
    result["current_key"] = current_key
    old_keys = _old_keys_for_tg_user(uid, current_key)
    if not old_keys:
        result["note"] = "no old keys found"
        return result
    result["old_keys"] = old_keys
    con = _connect()
    try:
        for ok in old_keys:
            row = con.execute(
                "SELECT COUNT(*) FROM manager_lead_cards WHERE tg_user_id=? AND manager_key=?",
                (uid, ok),
            ).fetchone()
            result["cards"] += int(row[0] or 0) if row else 0
            row2 = con.execute(
                "SELECT COUNT(*) FROM manager_bot_events WHERE manager_key=?",
                (ok,),
            ).fetchone()
            result["events"] += int(row2[0] or 0) if row2 else 0
    except Exception as exc:
        log.warning("[reconcile] diagnose failed uid=%s error=%r", uid, exc)
    finally:
        con.close()
    return result


# --- TPILOT MANAGERBOT DEAD-CODE REMOVAL 20260727 (round-7 review, R-2) ---
# _reconcile_reconnected_manager (formerly here) was a zero-caller top-level
# def -- confirmed via project-wide grep (only its own signature) and a scan
# of manager_bot.py for globals()/getattr(sys.modules)/eval/exec-based
# dynamic dispatch (none reference it). Its own access_users write used
# `ON CONFLICT(tg_user_id) DO UPDATE SET is_enabled=1` unconditionally --
# the same class of admin-disabled-account bypass R-2 removes elsewhere.
# Fully removed rather than left unreachable, per the review's verdict.
# _old_keys_for_tg_user/_diagnose_reconnect above remain: read-only,
# still referenced by each other, out of this fix's scope.

# --- TPILOT RECONNECT RECONCILE M2.6F END ---


async def _handle_start(event, tg_user_id: int | None = None) -> None:
    # --- TPILOT MANAGERBOT ACCESS AUTO-GRANT (canonical) 20260727 ---
    # Single call to the canonical storage.manager_bot_access_ensure_sync:
    # this IS the self-heal (an active manager with no/partial ManagerBot
    # access gets it here, idempotently, same as every onboarding call site
    # in main.py) and the ONLY source of the user-facing response. No
    # technical status text (tg_user_id/access level/scope/managers count/
    # Commands:/whoami) is ever shown to a known user; tg_user_id is shown
    # ONLY on the "not found" screen for a genuinely unknown/ambiguous
    # identity, where it is operationally needed to hand to an admin.
    try:
        if not getattr(event, "is_private", False):
            return

        identity = await _manager_bot_sender_identity(event)
        if tg_user_id is not None:
            identity["tg_user_id"] = int(tg_user_id or 0)
        uid = int(identity.get("tg_user_id") or 0)

        if not uid:
            await event.respond("\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0438\u0442\u044c Telegram ID.")
            return

        result = storage.manager_bot_access_ensure_sync(
            tg_user_id=uid,
            display_name=str(identity.get("display_name") or "").strip(),
            username=str(identity.get("username") or "").strip(),
            db_path=TPILOT_DB_PATH,
        )
        status = str(result.get("status") or "error")

        if status in ("granted_now", "already_granted"):
            name = (
                str(result.get("display_name") or "").strip()
                or str(result.get("manager_key") or "").strip()
                or "\u043c\u0435\u043d\u0435\u0434\u0436\u0435\u0440"
            )
            uname = str(result.get("username") or "").strip().lstrip("@")
            username_line = f"\U0001f517 Username: @{uname}" if uname else "\U0001f517 Username: \u043d\u0435 \u0437\u0430\u0434\u0430\u043d"
            header = "\u2705 \u0414\u043e\u0441\u0442\u0443\u043f \u0432\u044b\u0434\u0430\u043d \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0447\u0435\u0441\u043a\u0438" if status == "granted_now" else "\u2705 \u0414\u043e\u0441\u0442\u0443\u043f \u0430\u043a\u0442\u0438\u0432\u0435\u043d"
            await event.respond(
                f"{header}\n\n"
                f"\U0001f464 \u0410\u043a\u043a\u0430\u0443\u043d\u0442: {name}\n"
                f"{username_line}\n"
                "\U0001f7e2 \u0412\u0441\u0451 \u0440\u0430\u0431\u043e\u0442\u0430\u0435\u0442 \u043a\u043e\u0440\u0440\u0435\u043a\u0442\u043d\u043e",
                buttons=_mbstat_start_buttons(uid),
            )
            return

        # --- TPILOT MANAGERBOT ERROR-BEFORE-FALLBACK 20260727 (round-7 review, R-1) ---
        # This MUST be checked before the fallback block below: a transient
        # failure of the canonical grant call (status == "error") must show
        # the honest "retry later" screen, never the fallback's "access is
        # active" text. The fallback is a convenience read against the OLD
        # access tables for uids the new canon does not resolve -- it must
        # never mask a genuine canon failure as if everything were fine.
        if status == "error":
            await event.respond("⚠️ Не удалось проверить доступ. Повторите /start немного позже.")
            return

        # --- TPILOT MANAGERBOT ACCESS FALLBACK 20260727 ---
        # The canonical grant resolves identity ONLY via managers.tg_user_id.
        # Three classes of legitimate users are deliberately NOT there and
        # would otherwise lose their working menu:
        #   1. a uid approved by an admin through AdminBot (_mba_approve,
        #      panel_bot.py) -- that flow exists PRECISELY because the
        #      Telegram ID did not match a managers row;
        #   2. a supervisor with scope_mode='all' (never a manager himself);
        #   3. a uid with healthy access to manager key A whose OWN manager
        #      identity B is revoked/disabled -> canon says "disabled",
        #      but A must keep working (regression "HIGH R1").
        # Strictly read-only: nothing is granted, no permission is changed --
        # this only renders access the uid ALREADY has. Must stay ahead of
        # every denial branch below.
        # _access_user opens its connection OUTSIDE its own try, so a dead DB
        # raises straight through _access_allowed/_linked_manager_keys. Contain
        # it here: an unreachable DB must fall through to the clean "retry
        # later" screen below, never to the generic crash handler.
        try:
            fallback_keys = _linked_manager_keys(uid) if _access_allowed(uid) else []
        except Exception as _fb_exc:
            log.warning("[access-fallback] lookup failed uid=%s error=%r", uid, _fb_exc)
            fallback_keys = []
        if fallback_keys:
            fb_name = (
                str(identity.get("display_name") or "").strip()
                or str(fallback_keys[0] or "").strip()
                or "менеджер"
            )
            fb_uname = str(identity.get("username") or "").strip().lstrip("@")
            fb_username_line = f"🔗 Username: @{fb_uname}" if fb_uname else "🔗 Username: не задан"
            await event.respond(
                "✅ Доступ активен\n\n"
                f"👤 Аккаунт: {fb_name}\n"
                f"{fb_username_line}\n"
                "🟢 Всё работает корректно",
                buttons=_mbstat_start_buttons(uid),
            )
            return

        if status == "disabled":
            # A known manager whose own access_users row is disabled, or
            # whose manager_bot_access permission was revoked -- the admin
            # already knows who this is, so no tg_user_id here.
            await event.respond("\U0001f6ab \u0414\u043e\u0441\u0442\u0443\u043f \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u043e\u043c")
            return

        # --- TPILOT MANAGERBOT ACCESS REQUEST 20260727 ---
        # Username matches an active manager but the Telegram ID does not.
        # NEVER grants access (username alone must never be sufficient) --
        # it only files a request. This is the ONLY producer of
        # manager_access_requests rows, which AdminBot consumes: request
        # list, "new" badge counter and the approve/deny buttons
        # (_mba_approve/_mba_deny in panel_bot.py). Dropping it would leave
        # that whole AdminBot screen permanently empty.
        try:
            username_match = _find_active_manager_by_username(identity.get("username") or "")
        except Exception as _um_exc:
            log.warning("[access-request] username lookup failed uid=%s error=%r", uid, _um_exc)
            username_match = None
        if username_match:
            try:
                _upsert_manager_access_request(identity, username_match, "username")
            except Exception as _req_exc:
                log.warning("[access-request] upsert failed uid=%s error=%r", uid, _req_exc)
            await event.respond("⏳ Заявка создана, ожидайте подтверждения")
            return

        # status in ("not_eligible", "identity_conflict"): genuinely unknown
        # or ambiguous identity -- the ONLY screen that shows tg_user_id,
        # because it is this user's OWN id and is operationally needed to
        # hand to an admin for a manual grant.
        await event.respond(
            "\U0001f6ab \u0414\u043e\u0441\u0442\u0443\u043f \u043a TPilot ManagerBot \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\n\n"
            "\u041f\u0435\u0440\u0435\u0434\u0430\u0439\u0442\u0435 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0443 TPilot \u0432\u0430\u0448 ID:\n"
            f"`{uid}`"
        )

    except Exception as exc:
        log.warning("M2.6D start handler failed: %r", exc)
        try:
            await event.respond("\u041e\u0448\u0438\u0431\u043a\u0430 ManagerBot /start.")
        except Exception:
            pass

# --- TPILOT MANAGER BOT AUTO GRANT M2.6D 20260601 END ---


# --- TPILOT MANAGER BOT EVENT CUTOFF M2.6E 20260602 START ---
# Cutoff helper for auto-grant.
# New ManagerBot users should receive only future lead cards.

def _manager_bot_event_cutoff_for_manager(manager_key: str) -> int:
    mk = _norm_key(manager_key)
    if not mk:
        return 0

    con = _connect()
    try:
        row = con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM manager_bot_events WHERE manager_key=?",
            (mk,),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception as exc:
        log.warning("manager_bot event cutoff lookup failed manager=%s error=%r", mk, exc)
        return 0
    finally:
        con.close()

# --- TPILOT MANAGER BOT EVENT CUTOFF M2.6E 20260602 END ---


# --- TPILOT MANAGER BOT STATS M2.8 20260605 START ---
# Read-only stats view for ManagerBot.
# Mirrors PartnerBot LIGHT/PRO/BOTH semantics. Logic copied (NOT imported) from
# partner_stat_bot.py _psf3_* / _tp_pdf_* (that module builds a TelegramClient at import,
# so it is not import-safe). Keep these helpers in sync with the PartnerBot source.
# Deliberate deviations from a verbatim copy:
#   1) manager rows come from the user's permitted linked managers (not a buyer source);
#   2) a read-only override overlay from lead_status_overrides is authoritative for bucket.
# Per-manager stats_format only: never a single global mode per user.
import re as _mbstat_re
from datetime import date as _mbstat_date_cls, timedelta as _mbstat_timedelta, timezone as _mbstat_timezone
from zoneinfo import ZoneInfo as _MBStatZoneInfo

_MBSTAT_TZ = storage.w3_tz()  # W3.3-B redirect: canonical storage timezone (was _MBStatZoneInfo("Europe/Kyiv"))

# In-memory pending state for the "Период" button (uid -> True). No DB writes.
_MBSTAT_PENDING: Dict[int, bool] = {}


def _mbstat_now() -> datetime:
    return datetime.now(_MBSTAT_TZ)


def _mbstat_today():
    return _mbstat_now().date()


def _mbstat_parse_date_token(token: str = ""):
    t = str(token or "today").strip().lower()
    today = _mbstat_today()

    def _one(raw: str):
        s = str(raw or "").strip().lower()
        if s in ("today", "сегодня", ""):
            return today
        if s in ("yesterday", "вчера"):
            return today - _mbstat_timedelta(days=1)
        for fmt in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d-%m-%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                pass
        return None

    if t in ("today", "сегодня", ""):
        return today, today, today.strftime("%d.%m.%Y")
    if t in ("yesterday", "вчера"):
        d = today - _mbstat_timedelta(days=1)
        return d, d, d.strftime("%d.%m.%Y")
    if t in ("week", "7d", "неделя"):
        return today - _mbstat_timedelta(days=6), today, f"{(today - _mbstat_timedelta(days=6)).strftime('%d.%m.%Y')} - {today.strftime('%d.%m.%Y')}"
    if t in ("month", "30d", "31d", "месяц"):
        return today - _mbstat_timedelta(days=30), today, f"{(today - _mbstat_timedelta(days=30)).strftime('%d.%m.%Y')} - {today.strftime('%d.%m.%Y')}"

    parts = [p for p in _mbstat_re.split(r"[\s,;]+", t) if p]
    if parts and parts[0] in ("range", "period", "период", "с"):
        parts = parts[1:]
    dates = []
    for p in parts:
        d = _one(p)
        if d:
            dates.append(d)
    if len(dates) >= 2:
        start, end = dates[0], dates[1]
        if start > end:
            start, end = end, start
        min_day = today - _mbstat_timedelta(days=30)
        # Clamp BOTH ends into [min_day, today] (fixes start>end when both dates
        # are older than min_day, which previously produced empty reports).
        if start < min_day:
            start = min_day
        if start > today:
            start = today
        if end < min_day:
            end = min_day
        if end > today:
            end = today
        # After clamping, keep start <= end.
        if start > end:
            start, end = end, start
        # Max 30-day window protection.
        if (end - start).days > 30:
            start = end - _mbstat_timedelta(days=30)
        return start, end, f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}"

    d = _one(t)
    if d:
        min_day = today - _mbstat_timedelta(days=30)
        if d > today:
            d = today
        if d < min_day:
            d = min_day
        return d, d, d.strftime("%d.%m.%Y")
    return today, today, today.strftime("%d.%m.%Y")


def _mbstat_parse_period_input(text: str):
    """Validate manager-typed period text. Returns parsed tuple or None (invalid)."""
    t = str(text or "").strip().lower()
    if not t:
        return None
    if t in ("today", "сегодня", "yesterday", "вчера", "week", "7d", "неделя", "month", "30d", "31d", "месяц"):
        return _mbstat_parse_date_token(t)
    parts = [p for p in _mbstat_re.split(r"[\s,;]+", t) if p]
    fmts = ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d-%m-%Y")
    found = False
    for p in parts:
        for f in fmts:
            try:
                datetime.strptime(p, f)
                found = True
                break
            except Exception:
                pass
    if not found:
        return None
    return _mbstat_parse_date_token(t)


def _mbstat_date_list(start, end):
    out = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d = d + _mbstat_timedelta(days=1)
    return out


def _mbstat_local_dt(lead):
    raw_local = str((lead or {}).get("first_seen_kyiv") or "").strip()
    if raw_local:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw_local[:19], fmt).replace(tzinfo=_MBSTAT_TZ)
            except Exception:
                pass
    raw_utc = str((lead or {}).get("first_seen_utc") or "").strip()
    if raw_utc:
        if raw_utc[-1:] in ("Z", "z"):
            raw_utc = raw_utc[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw_utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_mbstat_timezone.utc)
            return dt.astimezone(_MBSTAT_TZ)
        except Exception:
            pass
    return None


def _mbstat_date_iter(start, end):
    cur = start
    while cur <= end:
        yield cur
        cur = cur + _mbstat_timedelta(days=1)


def _mbstat_windows(start, end, kind="day"):
    kind = str(kind or "day").lower()
    for d in _mbstat_date_iter(start, end):
        base = datetime(d.year, d.month, d.day, tzinfo=_MBSTAT_TZ)
        if kind == "flight":
            yield (base - _mbstat_timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0), base.replace(hour=8, minute=0, second=0, microsecond=0)
        else:
            yield base.replace(hour=8, minute=0, second=0, microsecond=0), base.replace(hour=17, minute=0, second=0, microsecond=0)


def _mbstat_in_windows(lead, start, end, kind="day"):
    dt = _mbstat_local_dt(lead)
    if not dt:
        return False
    for ws, we in _mbstat_windows(start, end, kind):
        if ws <= dt < we:
            return True
    return False


def _mbstat_manager_row(manager_key: str) -> Dict[str, Any]:
    mk = _norm_key(manager_key)
    if not mk:
        return {}
    con = _connect()
    try:
        r = con.execute("SELECT * FROM managers WHERE manager_key=? LIMIT 1", (mk,)).fetchone()
        return dict(r) if r else {}
    except Exception as exc:
        log.warning("mbstat manager row read failed manager=%s error=%r", mk, exc)
        return {}
    finally:
        con.close()


def _mbstat_db_candidates_for_manager(manager_key: str) -> List[str]:
    """Authoritative DB resolution for ManagerBot stats reads (leads/overrides only).

    Mirrors AdminBot's registry-based resolution (_manager_rows_for_reporting in
    main.py): prefer the manager's own `managers.db_path` from the control-plane
    registry, falling back to the canonical runtime/managers/<key>/<key>.db layout.

    Deliberately does NOT include the central TPILOT_DB_PATH. That DB is the
    control/queue plane, not a per-manager lead store; M2.10B root-cause analysis
    found it can contain an empty/foreign `daily_leads` table, and the previous
    candidate order (TPILOT_DB_PATH first) caused _mbstat_leads_for_manager to
    return 0 rows from it without ever reaching the manager's real per-manager DB.
    This helper is scoped to stats reads only -- _db_candidates_for_manager (used
    by cards/status/poll-loop paths) is left untouched.
    """
    mk = _norm_key(manager_key)
    candidates: List[str] = []
    if mk:
        try:
            reg_row = _mbstat_manager_row(mk)
        except Exception:
            reg_row = {}
        raw_db_path = str((reg_row or {}).get("db_path") or "").strip()
        if raw_db_path:
            p = Path(raw_db_path)
            if not p.is_absolute():
                p = (BASE_DIR / raw_db_path).resolve()
            candidates.append(str(p))
        candidates.append(str((BASE_DIR / "runtime" / "managers" / mk / f"{mk}.db").resolve()))

    result = []
    seen = set()
    for item in candidates:
        item = str(item or "").strip()
        if item and item not in seen and Path(item).exists():
            seen.add(item)
            result.append(item)
    return result


def _mbstat_leads_for_manager(manager_key: str, start, end) -> List[Dict[str, Any]]:
    mk = _norm_key(manager_key)
    dates = _mbstat_date_list(start, end)
    if not mk or not dates:
        return []
    q = ",".join(["?"] * len(dates))
    for db_path in _mbstat_db_candidates_for_manager(mk):
        con = _connect_path(db_path)
        try:
            if not _table_exists(con, "daily_leads"):
                continue
            # Schema-safe ORDER BY: only order by columns that actually exist in
            # this daily_leads table. Older/variant schemas may lack `id`; ordering
            # by a missing column raises and previously zeroed out the whole report.
            try:
                cols = {str(r[1]) for r in con.execute("PRAGMA table_info(daily_leads)").fetchall()}
            except Exception:
                cols = set()
            order_cols = [c for c in ("first_seen_utc", "first_seen_kyiv", "lead_date", "chat_id", "id") if c in cols]
            order_sql = (" ORDER BY " + ", ".join(f"{c} ASC" for c in order_cols)) if order_cols else ""
            # Defense-in-depth: filter by manager_key when the column exists, so a
            # shared/legacy DB can never attribute foreign rows to this manager.
            # Older/variant schemas lacking manager_key fall back to date-only.
            if "manager_key" in cols:
                where_sql = f"lead_date IN ({q}) AND manager_key=?"
                params: tuple = tuple(dates) + (mk,)
            else:
                where_sql = f"lead_date IN ({q})"
                params = tuple(dates)
            rows = con.execute(
                f"SELECT * FROM daily_leads WHERE {where_sql}{order_sql}",
                params,
            ).fetchall()
            out = []
            for r in rows or []:
                d = dict(r)
                d["manager_key"] = _norm_key(d.get("manager_key") or mk)
                out.append(d)
            return out
        except Exception as exc:
            log.warning("mbstat daily_leads read failed manager=%s db=%s error=%r", mk, db_path, exc)
            continue
        finally:
            con.close()
    return []


def _mbstat_overrides_for_manager(manager_key: str) -> Dict[int, Dict[str, Any]]:
    mk = _norm_key(manager_key)
    out: Dict[int, Dict[str, Any]] = {}
    if not mk:
        return out
    for db_path in _mbstat_db_candidates_for_manager(mk):
        con = _connect_path(db_path)
        try:
            if not _table_exists(con, "lead_status_overrides"):
                continue
            rows = con.execute(
                "SELECT chat_id, status, bucket, reason FROM lead_status_overrides WHERE manager_key=?",
                (mk,),
            ).fetchall()
            for r in rows or []:
                cid = int((r["chat_id"] if "chat_id" in r.keys() else 0) or 0)
                if cid:
                    out[cid] = {"status": r["status"], "bucket": r["bucket"], "reason": r["reason"]}
            return out
        except Exception:
            continue
        finally:
            con.close()
    return out


def _mbstat_age(lead):
    try:
        raw = (lead or {}).get("age")
        if raw is None or str(raw).strip() == "":
            return None
        return int(raw)
    except Exception:
        return None


def _mbstat_countable(lead):
    try:
        if "lead_countable" in (lead or {}):
            return int((lead or {}).get("lead_countable") or 0) == 1
    except Exception:
        return False
    try:
        return int((lead or {}).get("duplicate") or 0) != 1
    except Exception:
        return True


def _mbstat_duplicate_for_light(lead):
    kind = str((lead or {}).get("contact_kind") or "").strip().lower()
    reason = str((lead or {}).get("dedupe_reason") or "").strip().lower()
    try:
        if int((lead or {}).get("duplicate") or 0) == 1:
            return True
    except Exception:
        pass
    if kind == "duplicate":
        return True
    if "other_manager" in reason or "other manager" in reason:
        return True
    return False


def _mbstat_bucket(lead, override=None):
    # Override overlay is authoritative (manual status from lead_status_overrides).
    if override:
        ob = str((override or {}).get("bucket") or "").strip().lower()
        ost = str((override or {}).get("status") or "").strip().lower()
        # Respect override.status even when override.bucket is empty.
        if ob == "liquid" or ost == "liquid":
            return "liquid"
        if ob == "geo" or ost == "geo":
            return "geo"
        if ob == "under18" or ost == "under18":
            return "under18"
        if ob == "trash" or ost == "trash":
            return "trash"
        if ob == "na" or ost == "na":
            return "na"
        # unknown override bucket/status -> fall through to daily_leads-derived logic
    qb = str((lead or {}).get("quality_bucket") or "").strip().lower()
    qs = str((lead or {}).get("quality_status") or "").strip().lower()
    reason = str((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "").strip().lower()
    status = str((lead or {}).get("status") or "").strip().lower()
    country = str((lead or {}).get("country") or "").strip()
    age = _mbstat_age(lead)
    if qb == "liquid" or status == "liquid" or qs == "liquid":
        return "liquid"
    if qb == "geo":
        return "geo"
    if qb in ("under18", "age_missing"):
        return "under18"
    if qb == "trash":
        return "trash"
    if qb in ("na", "geo_missing", "age_and_geo_missing", "unknown", "unclear"):
        return "na"
    if status == "trash" or "trash" in reason or "blocked" in reason or "send_failed" in reason or "inaccessible" in reason or "недоступ" in reason:
        return "trash"
    if age is not None and age < 18:
        return "under18"
    if country and country != "Россия":
        return "geo"
    if "age_missing" in reason or "нет 18" in reason or "under18" in reason:
        return "under18"
    return "na"


def _mbstat_resolve_bucket(lead, overrides):
    ov = None
    try:
        ov = (overrides or {}).get(int((lead or {}).get("chat_id") or 0))
    except Exception:
        ov = None
    return _mbstat_bucket(lead, ov)


def _mbstat_bucket_empty():
    return {"otpisok": 0, "nonliquid": 0, "geo": 0, "under18": 0, "na": 0, "trash": 0, "liquid": 0}


def _mbstat_bucket_add(b, bkt):
    b["otpisok"] += 1
    if bkt == "liquid":
        b["liquid"] += 1
    elif bkt == "geo":
        b["geo"] += 1
    elif bkt == "under18":
        b["under18"] += 1
    elif bkt == "trash":
        b["trash"] += 1
    else:
        b["na"] += 1
    b["nonliquid"] = b["geo"] + b["under18"] + b["na"] + b["trash"]


def _mbstat_bucket_lines(b):
    return [
        f"ОТПИСОК: {int(b.get('otpisok') or 0)}",
        f"НЕЛИКВИД: {int(b.get('nonliquid') or 0)}",
        f"ГЕО: {int(b.get('geo') or 0)}",
        f"-18: {int(b.get('under18') or 0)}",
        f"NA: {int(b.get('na') or 0)}",
        f"TRASH: {int(b.get('trash') or 0)}",
        f"ЛИКВИД: {int(b.get('liquid') or 0)}",
    ]


def _mbstat_country_title(country, reason=""):
    raw = str(country or "").strip()
    low = (raw or str(reason or "")).strip().lower()
    if low.startswith("country_"):
        low = low.split("country_", 1)[1].strip()
    low = _mbstat_re.sub(r"[^a-zа-яёіїєґ\s_-]+", " ", low, flags=_mbstat_re.IGNORECASE).strip()
    mapping = {
        "украина": "Украина", "ukraine": "Украина", "ua": "Украина",
        "беларусь": "Беларусь", "белоруссия": "Беларусь", "by": "Беларусь",
        "казахстан": "Казахстан", "kz": "Казахстан",
        "узбекистан": "Узбекистан", "uz": "Узбекистан",
        "кыргызстан": "Кыргызстан", "киргизия": "Кыргызстан", "kg": "Кыргызстан",
        "таджикистан": "Таджикистан", "tj": "Таджикистан",
        "азербайджан": "Азербайджан", "az": "Азербайджан",
        "молдова": "Молдова", "md": "Молдова",
        "армения": "Армения", "am": "Армения",
        "грузия": "Грузия", "georgia": "Грузия", "ge": "Грузия",
        "германия": "Германия", "germany": "Германия",
        "индия": "Индия", "india": "Индия",
        "гана": "Гана", "ghana": "Гана",
        "таиланд": "Таиланд", "thailand": "Таиланд",
        "турция": "Турция", "turkey": "Турция",
    }
    for key, value in mapping.items():
        if key in low:
            return value
    if raw:
        return raw[:1].upper() + raw[1:]
    if low:
        return low[:1].upper() + low[1:]
    return "Страна не определена"


def _mbstat_na_reason_ru(reason):
    low = str(reason or "").strip().lower()
    if "age_and_geo_missing" in low:
        return "нет города и возраста"
    if "geo_missing" in low:
        return "нет города"
    if "age_missing" in low:
        return "нет возраста"
    if "unclear" in low or "unknown" in low:
        return "ответ не разобран"
    return "данных не хватает"


def _mbstat_trash_reason_ru(reason):
    low = str(reason or "").strip().lower()
    if "send_failed" in low or "blocked" in low or "inaccessible" in low or "недоступ" in low:
        return "чат недоступен / отправка не прошла"
    return "🗑 trash"


def _mbstat_details(items):
    geo = {}
    under18 = {}
    na = {}
    trash = {}
    for lead, bkt in list(items or []):
        reason = str((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "").strip()
        if bkt == "geo":
            if str((lead or {}).get("country") or "").strip() not in ("Россия", "РФ", "Российская Федерация"):
                title = _mbstat_country_title(str((lead or {}).get("country") or ""), reason)
                geo[title] = geo.get(title, 0) + 1
        elif bkt == "under18":
            age = _mbstat_age(lead)
            if age is not None and age < 18:
                title = f"{age} лет"
                under18[title] = under18.get(title, 0) + 1
        elif bkt == "na":
            title = _mbstat_na_reason_ru(reason)
            na[title] = na.get(title, 0) + 1
        elif bkt == "trash":
            title = _mbstat_trash_reason_ru(reason)
            trash[title] = trash.get(title, 0) + 1
    lines = []
    if geo or under18 or na or trash:
        lines.append("Детализация неликвида")
    if geo:
        lines.append("ГЕО")
        for title, cnt in sorted(geo.items(), key=lambda x: (-int(x[1]), str(x[0]))):
            if int(cnt) > 0:
                lines.append(f"{title}: {int(cnt)}")
    if under18:
        lines.append("-18")
        for title, cnt in sorted(under18.items(), key=lambda x: (-int(x[1]), str(x[0]))):
            if int(cnt) > 0:
                lines.append(f"{title}: {int(cnt)}")
    if na:
        lines.append("NA")
        for title, cnt in sorted(na.items(), key=lambda x: (-int(x[1]), str(x[0]))):
            if int(cnt) > 0:
                lines.append(f"{title}: {int(cnt)}")
    if trash:
        lines.append("TRASH")
        for title, cnt in sorted(trash.items(), key=lambda x: (-int(x[1]), str(x[0]))):
            if int(cnt) > 0:
                lines.append(f"{title}: {int(cnt)}")
    return lines


def _mbstat_manager_label(row):
    username = str((row or {}).get("telegram_username") or (row or {}).get("manager_username") or "").strip().lstrip("@")
    name = str((row or {}).get("display_name") or (row or {}).get("manager_key") or "").strip()
    if username and name and name.lower() != username.lower():
        return f"{name} | @{username}"
    if username:
        return f"@{username}"
    return name or str((row or {}).get("manager_key") or "_")


def _mbstat_normalize_format(raw):
    # manager_bot_access.stats_format schema default is 'light' (see init_schema).
    # Fail safe to light for empty/unknown values, never pro.
    r = str(raw or "light").strip().lower().replace(" ", "")
    r = r.replace("лёгкий", "light").replace("легкий", "light").replace("лайт", "light").replace("про", "pro").replace("оба", "both")
    if r in ("both", "all", "lightpro", "light+pro", "litepro", "lite+pro", "light_pro"):
        return "both"
    if "light" in r and "pro" in r:
        return "both"
    if r in ("light", "lite") or ("light" in r and "pro" not in r):
        return "light"
    if "pro" in r:
        return "pro"
    return "light"


def _mbstat_manager_rows_for_user(tg_user_id: int) -> List[Dict[str, Any]]:
    uid = int(tg_user_id or 0)
    out: List[Dict[str, Any]] = []
    if not uid or not _access_allowed(uid):
        return out
    seen = set()
    for mk in _linked_manager_keys(uid):
        mk = _norm_key(mk)
        if not mk or mk in seen:
            continue
        seen.add(mk)
        # Fail-closed: re-check can_view_stats for this exact manager row.
        if not _manager_bot_has_permission(uid, mk, "can_view_stats"):
            continue
        acc = _manager_bot_access_row(uid, mk)
        row = dict(_mbstat_manager_row(mk) or {})
        row["manager_key"] = mk
        row["stats_format"] = _mbstat_normalize_format(acc.get("stats_format"))
        row["allow_custom_period"] = int(acc.get("allow_custom_period") or 0)
        out.append(row)
    return out


def _mbstat_partition(rows):
    light = [r for r in rows if r.get("stats_format") in ("light", "both")]
    pro = [r for r in rows if r.get("stats_format") in ("pro", "both")]
    flight = [r for r in rows if r.get("stats_format") in ("pro", "both")]
    period = [r for r in rows if int(r.get("allow_custom_period") or 0) == 1]
    return {
        "light": light,
        "pro": pro,
        "flight": flight,
        "period": period,
        "show_stats": bool(rows),
        "show_flight": bool(pro),
        "show_period": bool(period),
    }


def _mbstat_user_can_view(tg_user_id: int) -> bool:
    return bool(_mbstat_manager_rows_for_user(int(tg_user_id or 0)))


def _mbstat_light_body(rows, start, end):
    lines = []
    total_all = 0
    dup_all = 0
    for row in rows:
        leads_all = _mbstat_leads_for_manager(row.get("manager_key"), start, end)
        total = len(leads_all)
        dup = sum(1 for lead in leads_all if _mbstat_duplicate_for_light(lead))
        total_all += total
        dup_all += dup
        lines.append(_mbstat_manager_label(row))
        lines.append(f"Всего написавших: {total}")
        lines.append(f"Дубликаты: {dup}")
        lines.append("")
    lines.append("ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ")
    lines.append(f"Всего написавших: {total_all}")
    lines.append(f"Дубликаты: {dup_all}")
    return "\n".join(lines).rstrip()


def _mbstat_window_body(rows, start, end, kind):
    lines = []
    total_bucket = _mbstat_bucket_empty()
    all_items = []
    # For flight (night) windows, date D covers D-1 17:00 -> D 08:00, so the
    # SQL fetch must also include the previous calendar day (its 17:00-23:59
    # leads belong to D's flight window). The window filter below still uses the
    # original [start, end] so only true night-window leads are counted.
    if kind == "flight":
        fetch_start = start - _mbstat_timedelta(days=1)
    else:
        fetch_start = start
    fetch_end = end
    for row in rows:
        mk = row.get("manager_key")
        overrides = _mbstat_overrides_for_manager(mk)
        leads_all = _mbstat_leads_for_manager(mk, fetch_start, fetch_end)
        b = _mbstat_bucket_empty()
        for lead in leads_all:
            if not _mbstat_in_windows(lead, start, end, kind):
                continue
            if not _mbstat_countable(lead):
                continue
            bkt = _mbstat_resolve_bucket(lead, overrides)
            _mbstat_bucket_add(b, bkt)
            _mbstat_bucket_add(total_bucket, bkt)
            all_items.append((lead, bkt))
        lines.append(_mbstat_manager_label(row))
        lines.extend(_mbstat_bucket_lines(b))
        lines.append("")
    lines.append("ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ")
    lines.extend(_mbstat_bucket_lines(total_bucket))
    details = _mbstat_details(all_items)
    if details:
        lines.append("")
        lines.extend(details)
    return "\n".join(lines).rstrip()


def _mbstat_render_today_text(tg_user_id: int) -> str:
    rows = _mbstat_manager_rows_for_user(int(tg_user_id or 0))
    if not rows:
        return "Статистика недоступна для вашего доступа."
    part = _mbstat_partition(rows)
    today = _mbstat_today()
    out = [f"📊 Статистика за {today.strftime('%d.%m.%Y')} {_mbstat_now().strftime('%H:%M:%S')}"]
    if part["light"]:
        out += ["", "LIGHT, сутки 00:00-24:00", "", _mbstat_light_body(part["light"], today, today)]
    if part["pro"]:
        out += ["", "PRO, день 08:00-17:00", "", _mbstat_window_body(part["pro"], today, today, "day")]
    return "\n".join(out).rstrip()


def _mbstat_render_flight_text(tg_user_id: int) -> str:
    rows = _mbstat_manager_rows_for_user(int(tg_user_id or 0))
    if not rows:
        return "Статистика недоступна для вашего доступа."
    part = _mbstat_partition(rows)
    if not part["flight"]:
        return "Долёты доступны только в PRO-формате."
    today = _mbstat_today()
    out = [
        f"🌙 Долёты за {today.strftime('%d.%m.%Y')}",
        "Окно: 17:00-08:00",
        "",
        _mbstat_window_body(part["flight"], today, today, "flight"),
    ]
    return "\n".join(out).rstrip()


def _mbstat_render_period_text(tg_user_id: int, token: str) -> str:
    uid = int(tg_user_id or 0)
    rows = _mbstat_manager_rows_for_user(uid)
    if not rows:
        return "Статистика недоступна для вашего доступа."
    part = _mbstat_partition(rows)
    if not part["period"]:
        return "Произвольный период недоступен для вашего доступа."
    start, end, label = _mbstat_parse_date_token(token)
    prows = part["period"]
    light = [r for r in prows if r.get("stats_format") in ("light", "both")]
    pro = [r for r in prows if r.get("stats_format") in ("pro", "both")]
    out = [f"📊 Период: {label}"]
    if light:
        out += ["", "LIGHT, сутки 00:00-24:00", "", _mbstat_light_body(light, start, end)]
    if pro:
        out += ["", "PRO, день 08:00-17:00", "", _mbstat_window_body(pro, start, end, "day")]
        out += ["", "🌙 Долёты 17:00-08:00", "", _mbstat_window_body(pro, start, end, "flight")]
    return "\n".join(out).rstrip()


def _mbstat_start_buttons(tg_user_id: int):
    try:
        if _mbstat_user_can_view(int(tg_user_id or 0)):
            return [[Button.inline("📊 Статистика", b"mb:st:menu")]]
    except Exception:
        pass
    return None


async def _mbstat_send_long(event, text) -> None:
    text = str(text or "").strip() or "—"
    limit = 3500
    if len(text) <= limit:
        await event.respond(text)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit and chunk:
            await event.respond(chunk.rstrip())
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await event.respond(chunk.rstrip())


async def _mbstat_send_menu(event, tg_user_id: int) -> None:
    uid = int(tg_user_id or 0)
    rows = _mbstat_manager_rows_for_user(uid)
    if not rows:
        await event.respond("Статистика недоступна для вашего доступа.")
        return
    part = _mbstat_partition(rows)
    buttons = [[Button.inline("📊 Сегодня", b"mb:st:today")]]
    if part["show_flight"]:
        buttons.append([Button.inline("🌙 Долёты", b"mb:st:flight")])
    if part["show_period"]:
        buttons.append([Button.inline("📅 Период", b"mb:st:period")])
    await event.respond("📊 Статистика — выберите вид:", buttons=buttons)


async def _mbstat_handle_command(event, tg_user_id: int, text: str) -> None:
    uid = int(tg_user_id or 0)
    if not _mbstat_user_can_view(uid):
        _MBSTAT_PENDING.pop(uid, None)
        await event.respond("Статистика недоступна для вашего доступа.")
        return
    parts = str(text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if arg:
        part = _mbstat_partition(_mbstat_manager_rows_for_user(uid))
        if not part["show_period"]:
            await event.respond("Произвольный период недоступен для вашего доступа.")
            return
        if not _mbstat_parse_period_input(arg):
            await event.respond("Не удалось распознать дату. Отправьте ДД.ММ.ГГГГ или период ДД.ММ.ГГГГ ДД.ММ.ГГГГ")
            return
        _MBSTAT_PENDING.pop(uid, None)
        await _mbstat_send_long(event, _mbstat_render_period_text(uid, arg))
        return
    _MBSTAT_PENDING.pop(uid, None)
    await _mbstat_send_menu(event, uid)


async def _mbstat_handle_pending_period(event, tg_user_id: int, text: str) -> None:
    uid = int(tg_user_id or 0)
    _MBSTAT_PENDING.pop(uid, None)
    if not _mbstat_user_can_view(uid):
        await event.respond("Статистика недоступна для вашего доступа.")
        return
    part = _mbstat_partition(_mbstat_manager_rows_for_user(uid))
    if not part["show_period"]:
        await event.respond("Произвольный период недоступен для вашего доступа.")
        return
    if not _mbstat_parse_period_input(text):
        await event.respond("Не удалось распознать дату. Отправьте ДД.ММ.ГГГГ или период ДД.ММ.ГГГГ ДД.ММ.ГГГГ")
        return
    await _mbstat_send_long(event, _mbstat_render_period_text(uid, str(text or "").strip()))


async def _mbstat_handle_callback(event, data) -> None:
    try:
        action = bytes(data or b"")[len(b"mb:st:"):].decode("utf-8", "ignore")
    except Exception:
        action = ""
    uid = int(getattr(event, "sender_id", 0) or 0)
    # Fail-closed: re-check permission on every callback.
    if not uid or not _mbstat_user_can_view(uid):
        await event.answer("Статистика недоступна", alert=True)
        return
    if action in ("menu", ""):
        await event.answer()
        await _mbstat_send_menu(event, uid)
        return
    if action == "today":
        await event.answer()
        await _mbstat_send_long(event, _mbstat_render_today_text(uid))
        return
    if action == "flight":
        await event.answer()
        await _mbstat_send_long(event, _mbstat_render_flight_text(uid))
        return
    if action == "period":
        part = _mbstat_partition(_mbstat_manager_rows_for_user(uid))
        if not part["show_period"]:
            await event.answer("Период недоступен", alert=True)
            return
        _MBSTAT_PENDING[uid] = True
        await event.answer()
        await event.respond("Отправьте дату ДД.ММ.ГГГГ или период ДД.ММ.ГГГГ ДД.ММ.ГГГГ")
        return
    await event.answer()

# --- TPILOT MANAGER BOT STATS M2.8 20260605 END ---


# --- TPILOT M2.13D-1 MANAGER SCHEDULE UI 20260609 START ---
# Monthly work-schedule calendar for ManagerBot.
# Each manager manually marks working/off days by pressing day buttons.
# No templates, no auto-fill, no weekday logic. Saturday and Sunday are not special.
# Missing row = day off. Only is_working=1 = working.
# Past days: view-only (alert on tap). Today and future: toggleable.

import calendar as _sched_cal_mod
from datetime import date as _sched_date_cls

_SCHED_TZ_MB = storage.w3_tz()   # W3.3-B redirect: canonical storage timezone (was _MBStatZoneInfo("Europe/Kyiv"))

try:
    from storage import (
        manager_schedule_get_month as _sched_get_month,
        manager_schedule_set_day as _sched_set_day,
        manager_schedule_is_working as _sched_is_working,
    )
    _SCHED_MB_OK = True
except Exception as _sched_mb_imp_err:
    _SCHED_MB_OK = False
    _sched_get_month = None
    _sched_set_day = None
    _sched_is_working = None
    log.warning("M2.13D-1 schedule storage import: %r", _sched_mb_imp_err)

# Patch C3b: future-day changes become admin approval requests (separate optional import so a
# storage without the C3a helpers still leaves today/past behavior fully working).
try:
    from storage import (
        schedule_request_create as _sched_req_create,
        schedule_request_list_pending as _sched_req_list_pending,
    )
    _SCHED_REQ_OK = True
except Exception as _sched_req_imp_err:
    _SCHED_REQ_OK = False
    _sched_req_create = None
    _sched_req_list_pending = None
    log.warning("C3b schedule-request storage import: %r", _sched_req_imp_err)

_SCHED_MONTH_NAMES_RU = [
    "", "Январь", "Февраль",
    "Март", "Апрель", "Май",
    "Июнь", "Июль", "Август",
    "Сентябрь", "Октябрь",
    "Ноябрь", "Декабрь",
]


def _sched_kyiv_today() -> _sched_date_cls:
    from datetime import datetime as _dt
    return _dt.now(tz=_SCHED_TZ_MB).date()


def _sched_manager_label_mb(mk: str) -> str:
    """Return 'display_name / @username' or just mk for calendar header."""
    con = _connect()
    try:
        row = con.execute(
            "SELECT display_name, telegram_username FROM managers WHERE manager_key=?",
            (_norm_key(mk),),
        ).fetchone()
        if not row:
            return mk
        name = str(row[0] or "").strip() or mk
        uname = str(row[1] or "").strip()
        return "{} / @{}".format(name, uname) if uname else name
    except Exception:
        return mk
    finally:
        con.close()


def _sched_pending_days_mb(mk: str, year: int, month: int) -> set:
    """Return the set of day-of-month ints (1..31) that have a PENDING admin approval request for
    this manager/month (Patch C3b). Empty set if the C3a helper is unavailable or on any error, so
    pending markers degrade gracefully without breaking the calendar."""
    out: set = set()
    if not (_SCHED_REQ_OK and callable(_sched_req_list_pending)):
        return out
    try:
        yyyymm_dash = "{:04d}-{:02d}".format(year, month)
        for r in _sched_req_list_pending(manager_key=mk, month=yyyymm_dash, db_path=TPILOT_DB_PATH) or []:
            wd = str((r or {}).get("work_date") or "")
            try:
                out.add(int(wd[8:10]))  # 'YYYY-MM-DD' -> DD
            except Exception:
                pass
    except Exception:
        pass
    return out


def _sched_month_text_mb(mk: str, year: int, month: int) -> str:
    label = _sched_manager_label_mb(mk)
    month_name = _SCHED_MONTH_NAMES_RU[month] if 1 <= month <= 12 else ""
    try:
        month_data = _sched_get_month(mk, year, month, TPILOT_DB_PATH) if callable(_sched_get_month) else {}
    except Exception:
        month_data = {}
    days_in_month = _sched_cal_mod.monthrange(year, month)[1]
    n_working = sum(1 for d in range(1, days_in_month + 1) if month_data.get(d, 0) == 1)
    n_off = days_in_month - n_working
    n_pending = len(_sched_pending_days_mb(mk, year, month))
    lines = [
        "\U0001f4c5 Мой график",
        "",
        "Менеджер: {}".format(label),
        "Месяц: {} {}".format(month_name, year),
        "",
        "✅ Рабочие: {}".format(n_working),
        "⚪ Выходные: {}".format(n_off),
    ]
    if n_pending:
        lines.append("⏳ На одобрении у админа: {}".format(n_pending))
    lines += [
        "",
        "Сегодня можно менять сразу. Будущие дни — по заявке администратору.",
    ]
    return "\n".join(lines)


def _sched_month_buttons_mb(mk: str, year: int, month: int) -> list:
    today = _sched_kyiv_today()
    try:
        month_data = _sched_get_month(mk, year, month, TPILOT_DB_PATH) if callable(_sched_get_month) else {}
    except Exception:
        month_data = {}
    pending_days = _sched_pending_days_mb(mk, year, month)
    yyyymm = "{:04d}{:02d}".format(year, month)
    rows = []
    # Weekday header row
    hdr_labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    rows.append([Button.inline(d, b"mb:sch:noop") for d in hdr_labels])
    # Calendar weeks
    cal = _sched_cal_mod.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(Button.inline(" ", b"mb:sch:noop"))
            else:
                is_working = month_data.get(day, 0) == 1
                # ⏳ marks a day with a pending admin approval request (C3b); the day's tap target
                # (mb:sch:t:) and grammar are unchanged.
                if day in pending_days:
                    label = "⏳{}".format(day)
                else:
                    label = "✅{}".format(day) if is_working else "⚪{}".format(day)
                cb_str = "mb:sch:t:{}:{}:{:02d}".format(mk, yyyymm, day)
                cb = cb_str.encode("utf-8")
                if len(cb) > 64:
                    cb = b"mb:sch:noop"
                row.append(Button.inline(label, cb))
        rows.append(row)
    # Navigation: prev / next month
    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    prev_cb = "mb:sch:p:{}:{:04d}{:02d}".format(mk, prev_y, prev_m).encode("utf-8")
    next_cb = "mb:sch:n:{}:{:04d}{:02d}".format(mk, next_y, next_m).encode("utf-8")
    rows.append([
        Button.inline("⬅️", prev_cb if len(prev_cb) <= 64 else b"mb:sch:noop"),
        Button.inline("➡️", next_cb if len(next_cb) <= 64 else b"mb:sch:noop"),
    ])
    rows.append([Button.inline("✅ Готово", b"mb:sch:done")])
    return rows


async def _sched_handle_callback(event, data: bytes) -> None:
    """Dispatcher for mb:sch: callbacks in ManagerBot."""
    uid = int(getattr(event, "sender_id", 0) or 0)
    if not uid or not _access_allowed(uid):
        await event.answer("Нет доступа", alert=True)
        return
    if not _SCHED_MB_OK:
        await event.answer("Функция недоступна", alert=True)
        return

    linked_keys = set(_linked_manager_keys(uid))
    try:
        action = data[len(b"mb:sch:"):].decode("utf-8", "ignore")
    except Exception:
        action = ""

    # noop — header cells and padding days
    if action == "noop":
        await event.answer()
        return

    # done — exit calendar
    if action == "done":
        await event.answer()
        try:
            await _safe_edit_card(
                event,
                "\U0001f4c5 График сохранён. Нажмите /start.",
                buttons=None,
            )
        except Exception:
            pass
        return

    # menu — open current month (or picker if multiple managers)
    if action == "menu":
        if not linked_keys:
            await event.answer("Нет связанных менеджеров", alert=True)
            return
        today = _sched_kyiv_today()
        if len(linked_keys) == 1:
            mk = next(iter(linked_keys))
            await _safe_edit_card(
                event,
                _sched_month_text_mb(mk, today.year, today.month),
                _sched_month_buttons_mb(mk, today.year, today.month),
            )
        else:
            btns = []
            for mk in sorted(linked_keys):
                label = _sched_manager_label_mb(mk)
                cb = "mb:sch:pick:{}".format(mk).encode("utf-8")
                if len(cb) <= 64:
                    btns.append([Button.inline(label[:40], cb)])
            btns.append([Button.inline("✅ Готово", b"mb:sch:done")])
            await _safe_edit_card(event, "\U0001f4c5 Выберите менеджера:", buttons=btns)
        await event.answer()
        return

    # pick:<mk>
    if action.startswith("pick:"):
        mk = _norm_key(action[len("pick:"):])
        if not mk or mk not in linked_keys:
            await event.answer("Нет доступа", alert=True)
            return
        today = _sched_kyiv_today()
        await _safe_edit_card(
            event,
            _sched_month_text_mb(mk, today.year, today.month),
            _sched_month_buttons_mb(mk, today.year, today.month),
        )
        await event.answer()
        return

    # m:<mk>:<YYYYMM>  p:<mk>:<YYYYMM>  n:<mk>:<YYYYMM> — open / prev / next month
    if len(action) >= 2 and action[1] == ":" and action[0] in ("m", "p", "n"):
        rest = action[2:]
        colon = rest.rfind(":")
        if colon < 0:
            await event.answer("Ошибка навигации", alert=True)
            return
        mk = _norm_key(rest[:colon])
        yyyymm = rest[colon + 1:]
        if not mk or mk not in linked_keys:
            await event.answer("Нет доступа", alert=True)
            return
        try:
            year = int(yyyymm[:4])
            month = int(yyyymm[4:6])
            if not (1 <= month <= 12):
                raise ValueError("month out of range")
        except Exception:
            await event.answer("Ошибка даты", alert=True)
            return
        await _safe_edit_card(
            event,
            _sched_month_text_mb(mk, year, month),
            _sched_month_buttons_mb(mk, year, month),
        )
        await event.answer()
        return

    # t:<mk>:<YYYYMM>:<DD> — toggle day
    if action.startswith("t:"):
        rest = action[len("t:"):]
        parts = rest.split(":")
        if len(parts) < 3:
            await event.answer("Ошибка", alert=True)
            return
        mk = _norm_key(parts[0])
        yyyymm = parts[1]
        dd_str = parts[2]
        if not mk or mk not in linked_keys:
            await event.answer("Нет доступа", alert=True)
            return
        try:
            year = int(yyyymm[:4])
            month = int(yyyymm[4:6])
            day = int(dd_str)
            work_date = "{:04d}-{:02d}-{:02d}".format(year, month, day)
            toggle_date = _sched_date_cls(year, month, day)
        except Exception:
            await event.answer("Ошибка даты", alert=True)
            return
        today_mb = _sched_kyiv_today()
        # Past-date guard (unchanged)
        if toggle_date < today_mb:
            await event.answer("Прошлые дни нельзя редактировать", alert=True)
            return
        # Patch C3b: FUTURE days are not changed directly by the manager — they become an admin
        # approval request. Today stays a direct toggle (branch below). The C1 effective state is
        # used so an inherited working day flips to a requested day-off and vice-versa.
        if toggle_date > today_mb:
            if not (_SCHED_REQ_OK and callable(_sched_req_create)):
                await event.answer("Изменение будущих дней пока недоступно", alert=True)
                return
            try:
                cur_eff = bool(_sched_is_working(mk, work_date, TPILOT_DB_PATH)) if callable(_sched_is_working) else False
            except Exception:
                cur_eff = False
            desired = 0 if cur_eff else 1
            try:
                _sched_req_create(
                    mk, work_date, desired,
                    requested_by_user_id=uid,
                    effective_at_request=(1 if cur_eff else 0),
                    db_path=TPILOT_DB_PATH,
                )
            except Exception as exc:
                # A rare concurrent double-tap can trip the partial-unique pending index; in that
                # case the pending request already exists, so degrade to the same success outcome.
                log.warning("sched request create mk=%s date=%s err=%r", mk, work_date, exc)
            await _safe_edit_card(
                event,
                _sched_month_text_mb(mk, year, month),
                _sched_month_buttons_mb(mk, year, month),
            )
            await event.answer("✅ Запрос отправлен администратору на одобрение", alert=True)
            return
        # Today: direct toggle (unchanged behavior)
        try:
            cur = _sched_is_working(mk, work_date, TPILOT_DB_PATH) if callable(_sched_is_working) else False
            new_val = 0 if cur else 1
            if callable(_sched_set_day):
                _sched_set_day(
                    mk, work_date, new_val,
                    source="manual_toggle",
                    updated_by_user_id=uid,
                    updated_by_role="manager",
                    db_path=TPILOT_DB_PATH,
                )
        except Exception as exc:
            log.warning("sched toggle mk=%s date=%s err=%r", mk, work_date, exc)
            await event.answer("Ошибка сохранения", alert=True)
            return
        await _safe_edit_card(
            event,
            _sched_month_text_mb(mk, year, month),
            _sched_month_buttons_mb(mk, year, month),
        )
        await event.answer()
        return

    await event.answer()


# Override _mbstat_start_buttons to also add the 📅 Мой график button.
_SCHED_PREV_START_BTNS = globals().get("_mbstat_start_buttons")


def _mbstat_start_buttons(tg_user_id: int):  # type: ignore[override]
    """Chain: stats buttons (from M2.8) + 📅 Мой график (M2.13D-1)."""
    rows: list = []
    try:
        prev = _SCHED_PREV_START_BTNS
        if callable(prev):
            result = prev(int(tg_user_id or 0))
            if result:
                rows.extend(result)
    except Exception:
        pass
    try:
        uid = int(tg_user_id or 0)
        if _access_allowed(uid) and _linked_manager_keys(uid):
            rows.append([Button.inline("\U0001f4c5 Мой график", b"mb:sch:menu")])
    except Exception:
        pass
    return rows if rows else None

# --- TPILOT M2.13D-1 MANAGER SCHEDULE UI 20260609 END ---


# --- TPILOT REFRESH CARDS BUTTON START ---
_REFRESH_PREV_START_BTNS = globals().get("_mbstat_start_buttons")


def _mbstat_start_buttons(tg_user_id: int):  # type: ignore[override]
    """Chain: previous /start buttons + 🔄 Обновить карточки."""
    rows: list = []
    try:
        prev = _REFRESH_PREV_START_BTNS
        if callable(prev):
            result = prev(int(tg_user_id or 0))
            if result:
                rows.extend(result)
    except Exception:
        pass
    try:
        if _access_allowed(int(tg_user_id or 0)):
            rows.append([Button.inline("\U0001f504 Обновить карточки", b"mb:refresh_cards")])
    except Exception:
        pass
    return rows if rows else None
# --- TPILOT REFRESH CARDS BUTTON END ---


# --- TPILOT STAGE 4D: Wire ManagerBot stats bodies to stats_engine START ---
_SE4D_VERSION = "manager_engine_wire_v1_20260618"

try:
    import stats_engine as _se4d
    _SE4D_OK = True
except Exception:
    _SE4D_OK = False

if _SE4D_OK:
    def _mbstat_light_body(rows, start, end):  # type: ignore[override]
        return _se4d.se_light_body(rows, start, end)

    def _mbstat_window_body(rows, start, end, kind):  # type: ignore[override]
        return _se4d.se_window_body(
            rows, start, end, kind,
            schedule_aware=False,
        )

# --- TPILOT STAGE 4D: Wire ManagerBot stats bodies to stats_engine END ---


# --- TPILOT TRANSFERS STAGE2 MAIN START ---
# Lead transfers (manager -> closer handoffs), creation-only. No PartnerBot exposure.
# No confirmation detection yet (Stage 3), no stats/Excel yet (Stage 4).
# Main lead status (liquid/geo/under18/na/trash) is never touched by any function below.

def _tr_manager_label(manager_key: str) -> str:
    return _sched_manager_label_mb(manager_key)


def _tr_is_closer(manager_key: str) -> bool:
    mk = _norm_key(manager_key)
    if not mk:
        return False
    con = _connect()
    try:
        row = con.execute("SELECT role FROM managers WHERE manager_key=?", (mk,)).fetchone()
        return bool(row) and str(row["role"] or "").strip().lower() == "closer"
    except Exception:
        return False
    finally:
        con.close()


def _tr_transfer_enabled(manager_key: str) -> bool:
    mk = _norm_key(manager_key)
    if not mk:
        return False
    con = _connect()
    try:
        row = con.execute(
            "SELECT transfer_enabled FROM transfer_config WHERE manager_key=?", (mk,)
        ).fetchone()
        return bool(row) and int(row["transfer_enabled"] or 0) == 1
    except Exception:
        return False
    finally:
        con.close()


def _tr_group_chat_id(manager_key: str) -> int:
    mk = _norm_key(manager_key)
    con = _connect()
    try:
        row = con.execute(
            "SELECT transfer_group_chat_id FROM transfer_config WHERE manager_key=?", (mk,)
        ).fetchone()
        return int(row["transfer_group_chat_id"] or 0) if row else 0
    except Exception:
        return 0
    finally:
        con.close()


def _tr_list_active_closers() -> List[Dict[str, Any]]:
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT manager_key, display_name, telegram_username
            FROM managers
            WHERE COALESCE(role,'manager')='closer'
              AND COALESCE(is_enabled,0)=1
              AND COALESCE(manual_stopped,0)=0
              AND COALESCE(status,'')='active'
            ORDER BY manager_key ASC
            """
        ).fetchall()
        return [dict(r) for r in rows or []]
    except Exception as exc:
        log.warning("active closers list failed: %r", exc)
        return []
    finally:
        con.close()


# --- TPILOT TRANSFERS UX FINAL START ---
def _tr_closer_config_keys(manager_key: str) -> List[str]:
    """Raw configured transfer_config.default_closers keys for manager_key ([] if unset)."""
    mk = _norm_key(manager_key)
    if not mk:
        return []
    con = _connect()
    try:
        row = con.execute(
            "SELECT default_closers FROM transfer_config WHERE manager_key=?", (mk,)
        ).fetchone()
        raw = str(row["default_closers"] or "") if row else ""
        return [k.strip() for k in raw.split(",") if k.strip()]
    except Exception as exc:
        log.warning("transfer_config default_closers read failed manager=%s error=%r", manager_key, exc)
        return []
    finally:
        con.close()


def _tr_closers_for_manager(manager_key: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Closer picker list for manager_key: (closer_rows, was_configured).

    When transfer_config.default_closers is set, only those closer keys that are still
    active role='closer' accounts are shown. When unset, falls back to all active closers.
    was_configured=True even if the filtered result ends up empty, so callers can show the
    specific "configured but disabled" message instead of the generic "not configured" one."""
    configured = _tr_closer_config_keys(manager_key)
    all_active = _tr_list_active_closers()
    if not configured:
        return all_active, False
    configured_set = {_norm_key(k) for k in configured}
    filtered = [r for r in all_active if _norm_key(r.get("manager_key") or "") in configured_set]
    return filtered, True


def _tr_no_closers_message(was_configured: bool) -> str:
    if was_configured:
        return "Клоузеры для этого менеджера не настроены или отключены. Обратитесь к администратору."
    return "Клоузеры не настроены. Обратитесь к администратору."
# --- TPILOT TRANSFERS UX FINAL END ---


def _tr_draft_create(*, manager_key: str, tg_user_id: int, lead_chat_id: int, known_contact_id: int,
                      lead_date: str, card_chat_id: int, card_message_id: int) -> int:
    now = _now_iso()
    con = _connect()
    try:
        cur = con.execute(
            """
            INSERT INTO transfer_drafts(
                manager_key, tg_user_id, lead_chat_id, known_contact_id, lead_date,
                card_chat_id, card_message_id, state, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?, 'await_text', ?, ?)
            """,
            (_norm_key(manager_key), int(tg_user_id), int(lead_chat_id), int(known_contact_id or 0),
             str(lead_date or ""), int(card_chat_id or 0), int(card_message_id or 0), now, now),
        )
        con.commit()
        return int(cur.lastrowid or 0)
    except Exception as exc:
        log.warning("transfer draft create failed: %r", exc)
        return 0
    finally:
        con.close()


def _tr_draft_get(draft_id: int) -> Dict[str, Any]:
    con = _connect()
    try:
        row = con.execute("SELECT * FROM transfer_drafts WHERE id=?", (int(draft_id),)).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


def _tr_draft_set_reply_anchor(draft_id: int, request_chat_id: int, request_message_id: int) -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE transfer_drafts SET request_chat_id=?, request_message_id=?, updated_at=? WHERE id=?",
            (int(request_chat_id), int(request_message_id), _now_iso(), int(draft_id)),
        )
        con.commit()
    except Exception as exc:
        log.warning("transfer draft anchor update failed draft_id=%s error=%r", draft_id, exc)
    finally:
        con.close()


def _tr_draft_find_by_reply(request_chat_id: int, request_message_id: int, tg_user_id: int) -> Dict[str, Any]:
    con = _connect()
    try:
        row = con.execute(
            """
            SELECT * FROM transfer_drafts
            WHERE request_chat_id=? AND request_message_id=? AND tg_user_id=? AND state='await_text'
            ORDER BY id DESC LIMIT 1
            """,
            (int(request_chat_id), int(request_message_id), int(tg_user_id)),
        ).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}
    finally:
        con.close()


def _tr_draft_set_text_and_advance(draft_id: int, form_text: str) -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE transfer_drafts SET form_text=?, state='await_closer', updated_at=? "
            "WHERE id=? AND state='await_text'",
            (str(form_text or ""), _now_iso(), int(draft_id)),
        )
        con.commit()
    except Exception as exc:
        log.warning("transfer draft text update failed draft_id=%s error=%r", draft_id, exc)
    finally:
        con.close()


def _tr_draft_set_closers(draft_id: int, closer_keys: List[str]) -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE transfer_drafts SET chosen_closers=?, updated_at=? WHERE id=?",
            (",".join(closer_keys), _now_iso(), int(draft_id)),
        )
        con.commit()
    except Exception as exc:
        log.warning("transfer draft closers update failed draft_id=%s error=%r", draft_id, exc)
    finally:
        con.close()


def _tr_draft_try_finalize_lock(draft_id: int) -> bool:
    """Atomically transition await_closer -> done. True only for the single caller that wins
    a race against a duplicate confirm click (Part H idempotency guarantee)."""
    con = _connect()
    try:
        cur = con.execute(
            "UPDATE transfer_drafts SET state='done', updated_at=? WHERE id=? AND state='await_closer'",
            (_now_iso(), int(draft_id)),
        )
        con.commit()
        return cur.rowcount > 0
    except Exception as exc:
        log.warning("transfer draft finalize-lock failed draft_id=%s error=%r", draft_id, exc)
        return False
    finally:
        con.close()


def _tr_draft_cancel(draft_id: int) -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE transfer_drafts SET state='cancelled', updated_at=? "
            "WHERE id=? AND state IN ('await_text','await_closer')",
            (_now_iso(), int(draft_id)),
        )
        con.commit()
    except Exception as exc:
        log.warning("transfer draft cancel failed draft_id=%s error=%r", draft_id, exc)
    finally:
        con.close()


_TR_FORM_LABEL_MAP = {
    "фио": "full_name", "имя": "full_name",
    "username": "username", "юзернейм": "username", "ник": "username",
    "телефон": "phone", "тел": "phone",
    "возраст": "age",
    "город": "city",
    "права": "car_license", "права/машина": "car_license", "машина": "car_license", "авто": "car_license",
    "комментарий": "comment", "коммент": "comment",
}


def _tr_parse_form_text(raw_text: str) -> Dict[str, str]:
    """Best-effort line-by-line parse of the ForceReply template. raw_text is always kept
    separately regardless of parse success (Part E requirement)."""
    out = {"full_name": "", "username": "", "phone": "", "age": "", "city": "", "car_license": "", "comment": ""}
    for line in str(raw_text or "").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = _TR_FORM_LABEL_MAP.get(label.strip().lower())
        if key:
            out[key] = value.strip()
    return out


def _tr_audit(transfer_id: int, action: str, actor: str, detail: str = "") -> None:
    con = _connect()
    try:
        con.execute(
            "INSERT INTO transfer_audit(transfer_id, action, actor, detail, created_at) VALUES (?,?,?,?,?)",
            (int(transfer_id or 0), str(action or ""), str(actor or ""), str(detail or "")[:500], _now_iso()),
        )
        con.commit()
    except Exception as exc:
        log.warning("transfer audit insert failed transfer_id=%s error=%r", transfer_id, exc)
    finally:
        con.close()


def _tr_finalize(draft: Dict[str, Any], closer_keys: List[str]):
    """Create one lead_transfers row per chosen closer (same batch_id), status='created'.
    Idempotent: UNIQUE(closer_key, lead_chat_id, lead_date) + INSERT OR IGNORE means a retry
    never creates duplicate rows; the existing row's id is looked up instead."""
    batch_id = uuid.uuid4().hex[:16]
    manager_key = _norm_key(draft.get("manager_key") or "")
    lead_chat_id = int(draft.get("lead_chat_id") or 0)
    known_contact_id = int(draft.get("known_contact_id") or 0)
    lead_date = str(draft.get("lead_date") or "")
    raw_text = str(draft.get("form_text") or "")
    created_by_uid = int(draft.get("tg_user_id") or 0)
    parsed = _tr_parse_form_text(raw_text)
    now = _now_iso()
    # N2-2: explicit (closer_key, transfer_id) pairs -- avoids zip() silently
    # mis-pairing if a closer is ever skipped (tid=0) in a future edit.
    transfer_pairs: List[Tuple[str, int]] = []
    con = _connect()
    try:
        for ck in closer_keys:
            ck = _norm_key(ck)
            if not ck:
                continue
            cur = con.execute(
                """
                INSERT OR IGNORE INTO lead_transfers(
                    batch_id, manager_key, closer_key, lead_chat_id, known_contact_id, lead_date,
                    full_name, username, phone, age, city, car_license, comment, raw_text,
                    status, created_by_uid, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'created',?,?,?)
                """,
                (
                    batch_id, manager_key, ck, lead_chat_id, known_contact_id, lead_date,
                    parsed.get("full_name", ""), parsed.get("username", ""), parsed.get("phone", ""),
                    parsed.get("age", ""), parsed.get("city", ""), parsed.get("car_license", ""),
                    parsed.get("comment", ""), raw_text,
                    created_by_uid, now, now,
                ),
            )
            if cur.rowcount:
                tid = int(cur.lastrowid or 0)
            else:
                row = con.execute(
                    "SELECT id FROM lead_transfers WHERE closer_key=? AND lead_chat_id=? AND lead_date=?",
                    (ck, lead_chat_id, lead_date),
                ).fetchone()
                tid = int(row["id"]) if row else 0
            if tid:
                transfer_pairs.append((ck, tid))
                con.execute(
                    "INSERT INTO transfer_audit(transfer_id, action, actor, detail, created_at) VALUES (?,?,?,?,?)",
                    (tid, "created", "manager", f"by_uid={created_by_uid} batch={batch_id}", now),
                )
        con.commit()
    except Exception as exc:
        log.warning("transfer finalize failed manager=%s error=%r", manager_key, exc)
    finally:
        con.close()
    return transfer_pairs, batch_id


def _tr_enqueue_closer_event(closer_key: str, transfer_id: int, batch_id: str,
                              draft: Dict[str, Any], manager_label: str) -> None:
    """Deliver via the existing manager_bot_events poll loop -- same mechanism used for lead
    cards and reserve notifications. event_key is unique per transfer_id, so a retry never
    enqueues a duplicate closer notification."""
    ck = _norm_key(closer_key)
    if not ck or not transfer_id:
        return
    payload = {
        "transfer_id": int(transfer_id),
        "batch_id": batch_id,
        "from_manager_key": _norm_key(draft.get("manager_key") or ""),
        "from_manager_label": manager_label,
        "closer_key": ck,
        "lead_chat_id": int(draft.get("lead_chat_id") or 0),
        "known_contact_id": int(draft.get("known_contact_id") or 0),
        "lead_date": str(draft.get("lead_date") or ""),
        "raw_text": str(draft.get("form_text") or ""),
    }
    payload.update(_tr_parse_form_text(str(draft.get("form_text") or "")))
    now = _now_iso()
    con = _connect()
    try:
        con.execute(
            """
            INSERT OR IGNORE INTO manager_bot_events(
                event_key, event_type, manager_key, chat_id, lead_date, daily_lead_id,
                old_status, new_status, payload_json, source, created_at
            ) VALUES (?, 'transfer_created', ?, ?, ?, 0, '', '', ?, 'transfer', ?)
            """,
            (
                f"transfer_created:{int(transfer_id)}",
                ck,
                int(draft.get("lead_chat_id") or 0),
                str(draft.get("lead_date") or ""),
                json.dumps(payload, ensure_ascii=False),
                now,
            ),
        )
        con.commit()
    except Exception as exc:
        log.warning("transfer closer event enqueue failed closer=%s tid=%s error=%r", ck, transfer_id, exc)
        raise
    finally:
        con.close()


async def _tr_notify_all(draft: Dict[str, Any], transfer_pairs: List[Tuple[str, int]], batch_id: str) -> None:
    """Fail-open: the lead_transfers rows already exist regardless of what happens here.
    Every failure is logged to transfer_audit and never raised further."""
    manager_key = _norm_key(draft.get("manager_key") or "")
    tg_user_id = int(draft.get("tg_user_id") or 0)
    raw_text = str(draft.get("form_text") or "")
    closer_labels = [_tr_manager_label(ck) for ck, _tid in transfer_pairs]
    manager_label = _tr_manager_label(manager_key)
    transfer_ids = [tid for _ck, tid in transfer_pairs]

    # 1. Notify the original manager directly (synchronous, best-effort).
    try:
        await client.send_message(
            tg_user_id,
            "✅ Передача создана\n\nКлоузер(ы): " + ", ".join(closer_labels) + "\n\n" + raw_text,
        )
    except Exception as exc:
        log.warning("transfer manager notify failed tg_user_id=%s error=%r", tg_user_id, exc)
        for tid in transfer_ids:
            _tr_audit(tid, "notify_fail", "system", f"manager_dm error={exc!r}")

    # 2. Notify each closer via the existing manager_bot_events poll loop.
    for ck, tid in transfer_pairs:
        try:
            _tr_enqueue_closer_event(ck, tid, batch_id, draft, manager_label)
        except Exception as exc:
            _tr_audit(tid, "notify_fail", "system", f"closer_event error={exc!r}")

    # 3. Notify the transfer group chat, if configured for the originating manager.
    try:
        group_id = _tr_group_chat_id(manager_key)
    except Exception:
        group_id = 0
    if group_id:
        try:
            text = "\n".join([
                "🤝 Новая передача",
                "",
                f"От: {manager_label}",
                f"Кому: {', '.join(closer_labels)}",
                "",
                raw_text,
            ])
            await client.send_message(int(group_id), text)
        except Exception as exc:
            log.warning("transfer group notify failed group=%s error=%r", group_id, exc)
            for tid in transfer_ids:
                _tr_audit(tid, "notify_fail", "system", f"group error={exc!r}")


def _tr_closer_picker_buttons(draft_id: int, chosen: List[str], closer_rows: List[Dict[str, Any]]):
    rows = []
    for r in closer_rows[:20]:
        ck = _norm_key(r.get("manager_key") or "")
        if not ck:
            continue
        mark = "✅" if ck in chosen else "☐"
        cb = f"mb:tr:cl:{int(draft_id)}:{ck}"
        if len(cb.encode("utf-8")) > 64:
            continue
        rows.append([Button.inline(f"{mark} {_tr_manager_label(ck)}", cb.encode("utf-8"))])
    action_row = []
    if chosen:
        action_row.append(Button.inline("✅ Подтвердить", f"mb:tr:ok:{int(draft_id)}".encode("utf-8")))
    action_row.append(Button.inline("❌ Отмена", f"mb:tr:cancel:{int(draft_id)}".encode("utf-8")))
    rows.append(action_row)
    return rows


async def _tr_handle_text_reply(event, tg_user_id: int, text: str) -> bool:
    """Restart-safe: matches transfer_drafts by (request_chat_id, request_message_id,
    tg_user_id, state='await_text'), stored in the DB, not in-memory. Returns False fast
    (no side effects) for any reply that doesn't match an open draft."""
    try:
        reply_to = int(getattr(event, "reply_to_msg_id", 0) or 0)
        if not reply_to:
            return False
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        draft = _tr_draft_find_by_reply(chat_id, reply_to, tg_user_id)
        if not draft:
            return False
        draft_id = int(draft.get("id") or 0)
        _tr_draft_set_text_and_advance(draft_id, text)
        closer_rows, was_configured = _tr_closers_for_manager(draft.get("manager_key") or "")
        if not closer_rows:
            try:
                await event.reply(_tr_no_closers_message(was_configured))
            except Exception:
                pass
            return True
        try:
            await event.reply(
                "Выберите 1-2 клоузеров:",
                buttons=_tr_closer_picker_buttons(draft_id, [], closer_rows),
            )
        except Exception as exc:
            log.warning("transfer closer picker send failed draft_id=%s error=%r", draft_id, exc)
        return True
    except Exception as exc:
        log.warning("transfer text reply handling failed uid=%s error=%r", tg_user_id, exc)
        return True


async def _tr_cb_new(event, tg_user_id: int, card_id: int) -> None:
    card = _card_by_id(card_id, tg_user_id)
    if not card:
        await event.answer("Карточка не найдена", alert=True)
        return
    manager_key = _norm_key(card.get("manager_key") or "")
    if _tr_is_closer(manager_key):
        await event.answer("Недоступно", alert=True)
        return
    if not _can_set_status(tg_user_id, manager_key):
        await event.answer("Нет доступа", alert=True)
        return
    if not _tr_transfer_enabled(manager_key):
        await event.answer("Передачи выключены для этого менеджера", alert=True)
        return
    closer_rows, was_configured = _tr_closers_for_manager(manager_key)
    if not closer_rows:
        await event.answer()
        try:
            await client.send_message(tg_user_id, _tr_no_closers_message(was_configured))
        except Exception:
            pass
        return

    lead_chat_id = int(card.get("chat_id") or 0)
    lead_date = str(card.get("lead_date") or "")
    daily = _read_daily_row(manager_key, lead_chat_id, lead_date)
    known_contact_id = int(daily.get("known_contact_id") or 0)

    draft_id = _tr_draft_create(
        manager_key=manager_key, tg_user_id=tg_user_id, lead_chat_id=lead_chat_id,
        known_contact_id=known_contact_id, lead_date=lead_date,
        card_chat_id=int(card.get("bot_chat_id") or 0), card_message_id=int(card.get("message_id") or 0),
    )
    if not draft_id:
        await event.answer("Ошибка создания передачи", alert=True)
        return

    await event.answer()
    prompt = "\n".join([
        "Заполните передачу одним сообщением:",
        "ФИО:",
        "Username:",
        "Телефон:",
        "Возраст:",
        "Город:",
        "Права/машина:",
        "Комментарий:",
    ])
    try:
        helper = await client.send_message(
            tg_user_id, prompt, buttons=Button.force_reply(single_use=True, selective=True),
        )
        helper_msg_id = int(getattr(helper, "id", 0) or 0)
        _tr_draft_set_reply_anchor(draft_id, tg_user_id, helper_msg_id)
    except Exception as exc:
        log.warning("transfer draft prompt send failed draft_id=%s error=%r", draft_id, exc)


async def _tr_cb_pick_closer(event, tg_user_id: int, draft_id: int, closer_key: str) -> None:
    draft = _tr_draft_get(draft_id)
    if not draft or int(draft.get("tg_user_id") or 0) != tg_user_id or str(draft.get("state") or "") != "await_closer":
        await event.answer("Черновик недоступен", alert=True)
        return
    chosen = [k.strip() for k in str(draft.get("chosen_closers") or "").split(",") if k.strip()]
    if closer_key in chosen:
        chosen = [k for k in chosen if k != closer_key]
    elif len(chosen) >= 2:
        await event.answer("Можно выбрать не более 2 клоузеров.", alert=True)
        return
    else:
        chosen.append(closer_key)
    _tr_draft_set_closers(draft_id, chosen)
    closer_rows, _was_configured = _tr_closers_for_manager(draft.get("manager_key") or "")
    try:
        await event.edit("Выберите 1-2 клоузеров:", buttons=_tr_closer_picker_buttons(draft_id, chosen, closer_rows))
    except Exception as exc:
        if "MessageNotModifiedError" not in repr(exc):
            log.warning("transfer closer picker edit failed draft_id=%s error=%r", draft_id, exc)
    await event.answer()


async def _tr_cb_confirm(event, tg_user_id: int, draft_id: int) -> None:
    draft = _tr_draft_get(draft_id)
    if not draft or int(draft.get("tg_user_id") or 0) != tg_user_id:
        await event.answer("Черновик недоступен", alert=True)
        return
    state = str(draft.get("state") or "")
    if state == "done":
        # Part H idempotency: a duplicate confirm click after success is a no-op, not an error.
        await event.answer("Уже создано", alert=False)
        return
    if state != "await_closer":
        await event.answer("Черновик недоступен", alert=True)
        return
    chosen = [k.strip() for k in str(draft.get("chosen_closers") or "").split(",") if k.strip()]
    if not chosen:
        await event.answer("Выберите хотя бы одного клоузера", alert=True)
        return
    active_closer_keys = {_norm_key(r.get("manager_key") or "") for r in _tr_list_active_closers()}
    chosen_valid = [k for k in chosen if _norm_key(k) in active_closer_keys]
    if not chosen_valid:
        await event.answer("Выбранные клоузеры больше недоступны", alert=True)
        return

    if not _tr_draft_try_finalize_lock(draft_id):
        # Lost the race to a concurrent duplicate click -- no second set of rows created.
        await event.answer("Уже обработано", alert=False)
        return

    transfer_pairs, batch_id = _tr_finalize(draft, chosen_valid)
    await event.answer("Передача создана")
    try:
        await event.edit("✅ Передача создана.")
    except Exception:
        pass
    await _tr_notify_all(draft, transfer_pairs, batch_id)


async def _tr_cb_cancel(event, tg_user_id: int, draft_id: int) -> None:
    draft = _tr_draft_get(draft_id)
    if draft and int(draft.get("tg_user_id") or 0) == tg_user_id and str(draft.get("state") or "") in ("await_text", "await_closer"):
        _tr_draft_cancel(draft_id)
    await event.answer("Отменено")
    try:
        await event.edit("❌ Передача отменена.")
    except Exception:
        pass


async def _tr_handle_callback(event, data: bytes) -> None:
    try:
        raw = data.decode("utf-8", "ignore")
        parts = raw.split(":")
        action = parts[2] if len(parts) > 2 else ""
        tg_user_id = int(event.sender_id or 0)

        if not _access_allowed(tg_user_id):
            await event.answer("Нет доступа", alert=True)
            return

        if action == "new" and len(parts) >= 4:
            await _tr_cb_new(event, tg_user_id, int(parts[3]))
            return

        if action == "cl" and len(parts) >= 5:
            await _tr_cb_pick_closer(event, tg_user_id, int(parts[3]), _norm_key(parts[4]))
            return

        if action == "ok" and len(parts) >= 4:
            await _tr_cb_confirm(event, tg_user_id, int(parts[3]))
            return

        if action == "cancel" and len(parts) >= 4:
            await _tr_cb_cancel(event, tg_user_id, int(parts[3]))
            return

        await event.answer()
    except Exception as exc:
        log.warning("[transfers-stage2] callback error: %r", exc)
        try:
            await event.answer("Ошибка", alert=True)
        except Exception:
            pass

# --- TPILOT TRANSFERS STAGE2 MAIN END ---


# --- TPILOT CLOSER SELF-SERVICE SETTINGS 20260713 START --------------------
# Closer-only (role='closer' in `managers`) self-service settings from their
# own ManagerBot: work-window start/end (writes the SAME central
# manager_client_message_schedule table main.py's _tp_gq_get_schedule
# already reads as a per-manager override -- zero changes needed to that
# resolver) and custom greeting/away texts (new 2-column settings KV keys,
# read by main.py's _maybe_auto_reply_to_lead via a small new helper -- see
# main.py's own "TPILOT CLOSER SELF-SERVICE SETTINGS" block).
#
# Visible and editable ONLY for the user's OWN closer manager_key(s):
# _cls_closer_keys() intersects this uid's access_targets with role='closer'
# rows in `managers`. EVERY callback re-validates the target manager_key
# against _cls_closer_keys(tg_user_id) before doing anything -- a forged
# callback naming another closer's key is always rejected, and a normal
# (non-closer) manager never even sees the entry button and gets a plain
# "Доступ запрещён" alert if they somehow send a cls: callback. No
# PartnerBot/AdminBot exposure. No destructive schema change: only reuses
# the pre-existing manager_client_message_schedule table (already created
# by main.py) and the standard 2-column settings KV pattern used elsewhere
# in this project (panel_bot.py/partner_stat_bot.py).
#
# Intentionally simple for this first cut (matches the read-only audit):
# work window is day-only (start < end, no midnight wraparound) -- source
# schedules (source_work_schedule) and work-day schedules
# (manager_work_schedule_days) are never touched.

import re as _cls_re

_CLS_TEXT_MAX_LEN = 1500
_CLS_PRESETS = {
    "0900_1800": ("09:00", "18:00"),
    "1000_1900": ("10:00", "19:00"),
    "1200_2100": ("12:00", "21:00"),
}
_CLS_PENDING: Dict[int, Dict[str, str]] = {}   # tg_user_id -> {"manager_key":, "field": "start"|"end"|"greet"|"away"}
_CLS_DRAFT: Dict[int, Dict[str, str]] = {}     # tg_user_id -> {"manager_key":, "field": "greet"|"away", "text":}


def _cls_closer_keys(tg_user_id: int) -> List[str]:
    try:
        keys = _access_targets(int(tg_user_id or 0))
    except Exception:
        keys = []
    out: List[str] = []
    for k in keys:
        try:
            if _tr_is_closer(k):
                out.append(k)
        except Exception:
            pass
    return out


def _cls_owns_key(tg_user_id: int, manager_key: str) -> bool:
    mk = _norm_key(manager_key)
    return bool(mk) and mk in _cls_closer_keys(tg_user_id)


def _cls_manager_label(manager_key: str) -> str:
    try:
        return _tr_manager_label(manager_key) or manager_key
    except Exception:
        return manager_key


def _cls_get_setting(key: str) -> str:
    try:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
            )
            row = con.execute("SELECT value FROM settings WHERE key=? LIMIT 1", (str(key),)).fetchone()
            return str(row["value"] or "") if row else ""
        finally:
            con.close()
    except Exception:
        return ""


def _cls_set_setting(key: str, value: str) -> None:
    try:
        con = _connect()
        try:
            con.execute(
                "CREATE TABLE IF NOT EXISTS settings("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
            )
            con.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key), str(value)),
            )
            con.commit()
        finally:
            con.close()
    except Exception as e:
        log.warning("closer settings KV write failed key=%s error=%r", key, e)


def _cls_greeting_key(manager_key: str) -> str:
    return f"closer_greeting_text_{manager_key}"


def _cls_away_key(manager_key: str) -> str:
    return f"closer_away_text_{manager_key}"


def _cls_updated_key(manager_key: str) -> str:
    return f"closer_settings_updated_at_{manager_key}"


def _cls_parse_hhmm(raw: str):
    s = str(raw or "").strip()
    m = _cls_re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", s)
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _cls_fmt_hhmm(minutes: int) -> str:
    mm = int(minutes) % 1440
    return f"{mm // 60:02d}:{mm % 60:02d}"


def _cls_ensure_schedule_table(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS manager_client_message_schedule(
            manager_key TEXT PRIMARY KEY,
            day_start TEXT NOT NULL DEFAULT '08:00',
            day_end TEXT NOT NULL DEFAULT '17:00',
            night_start TEXT NOT NULL DEFAULT '17:00',
            night_end TEXT NOT NULL DEFAULT '08:00',
            updated_at TEXT NOT NULL DEFAULT '',
            updated_by_user_id INTEGER
        );
        """
    )


def _cls_schedule_get(manager_key: str) -> Tuple[str, str]:
    mk = _norm_key(manager_key)
    con = _connect()
    try:
        _cls_ensure_schedule_table(con)
        con.commit()
        row = con.execute(
            "SELECT day_start, day_end FROM manager_client_message_schedule WHERE manager_key=?",
            (mk,),
        ).fetchone()
    finally:
        con.close()
    if row:
        return str(row["day_start"] or "08:00"), str(row["day_end"] or "17:00")
    return "08:00", "17:00"


def _cls_schedule_set(manager_key: str, day_start: str, day_end: str, tg_user_id: int) -> None:
    mk = _norm_key(manager_key)
    if not mk:
        return
    con = _connect()
    try:
        _cls_ensure_schedule_table(con)
        con.execute(
            """
            INSERT INTO manager_client_message_schedule(
                manager_key, day_start, day_end, night_start, night_end, updated_at, updated_by_user_id
            ) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(manager_key) DO UPDATE SET
                day_start=excluded.day_start,
                day_end=excluded.day_end,
                night_start=excluded.night_start,
                night_end=excluded.night_end,
                updated_at=excluded.updated_at,
                updated_by_user_id=excluded.updated_by_user_id
            """,
            (mk, day_start, day_end, day_end, day_start, _now_iso(), int(tg_user_id or 0) or None),
        )
        con.commit()
    finally:
        con.close()


def _cls_apply_hour(manager_key: str, field: str, hour: int, tg_user_id: int) -> Tuple[bool, str]:
    """Apply a single hour-picker selection (00-23) to the closer's
    schedule -- replaces start or end, keeps the other side, validates
    start<end (no midnight crossing in this release), saves via the
    existing _cls_schedule_set (day: start->end, night: end->start,
    exactly as before). Returns (ok, message) for the caller to show."""
    cur_start, cur_end = _cls_schedule_get(manager_key)
    cur_start_m = _cls_parse_hhmm(cur_start) or 8 * 60
    cur_end_m = _cls_parse_hhmm(cur_end) or 17 * 60
    hour_m = int(hour) * 60
    new_start_m = hour_m if field == "start" else cur_start_m
    new_end_m = hour_m if field == "end" else cur_end_m
    if new_start_m >= new_end_m:
        return False, "Начало должно быть раньше конца. Выберите другое время."
    _cls_schedule_set(manager_key, _cls_fmt_hhmm(new_start_m), _cls_fmt_hhmm(new_end_m), tg_user_id)
    log.info(
        "closer schedule saved (hour picker) manager=%s uid=%s field=%s hour=%s start=%s end=%s",
        manager_key, tg_user_id, field, hour, _cls_fmt_hhmm(new_start_m), _cls_fmt_hhmm(new_end_m),
    )
    return True, f"✅ График сохранён: {_cls_fmt_hhmm(new_start_m)}–{_cls_fmt_hhmm(new_end_m)}"


def _cls_menu_text(manager_key: str) -> str:
    return f"⚙️ Настройки клоузера: {_cls_manager_label(manager_key)}\n\nВыберите раздел:"


def _cls_menu_buttons(manager_key: str) -> list:
    # TPILOT CLOSER AUTOMATION ISOLATION 20260716: explicit day/night greeting
    # enable toggles, default OFF (fail-closed) -- missing key or any value
    # other than "1" reads as OFF. main.py's _lead_automation_gate reads
    # these SAME KV keys directly (own _tp_closer_setting_get, same
    # `settings` table) before ever sending a closer greeting. Inlined here
    # (not a separate helper) so this stays extractable by the existing
    # tools/closer_settings_selftest.py fixed name-set. Independent from the
    # greeting/away TEXT keys below -- saving new text never flips a toggle.
    day_on = _cls_get_setting(f"closer_day_greeting_enabled_{manager_key}").strip() == "1"
    night_on = _cls_get_setting(f"closer_night_greeting_enabled_{manager_key}").strip() == "1"
    return [
        [Button.inline("🕘 График работы", f"cls:sched:{manager_key}".encode("utf-8"))],
        [Button.inline("💬 Текст приветствия", f"cls:txt:greet:{manager_key}".encode("utf-8"))],
        [Button.inline("🌙 Текст «нет на месте»", f"cls:txt:away:{manager_key}".encode("utf-8"))],
        [Button.inline(f"☀️ Дневное приветствие: {'ВКЛ' if day_on else 'ВЫКЛ'}", f"cls:tg:day:{manager_key}".encode("utf-8"))],
        [Button.inline(f"🌙 Ночное приветствие: {'ВКЛ' if night_on else 'ВЫКЛ'}", f"cls:tg:night:{manager_key}".encode("utf-8"))],
        [Button.inline("👁 Посмотреть настройки", f"cls:view:{manager_key}".encode("utf-8"))],
        [Button.inline("⬅️ Назад", b"cls:home")],
    ]


def _cls_sched_text(manager_key: str) -> str:
    start, end = _cls_schedule_get(manager_key)
    return f"🕘 График работы: {_cls_manager_label(manager_key)}\n\nТекущий график: {start}–{end}"


def _cls_sched_buttons(manager_key: str) -> list:
    rows = [
        [
            Button.inline("🟢 Начало работы", f"cls:hp:start:{manager_key}".encode("utf-8")),
            Button.inline("🔴 Конец работы", f"cls:hp:end:{manager_key}".encode("utf-8")),
        ],
    ]
    preset_row = [
        Button.inline(f"{s}–{e}", f"cls:p:{token}:{manager_key}".encode("utf-8"))
        for token, (s, e) in _CLS_PRESETS.items()
    ]
    rows.append(preset_row)
    rows.append([Button.inline("⬅️ Назад", f"cls:m:{manager_key}".encode("utf-8"))])
    return rows


def _cls_hour_picker_text(manager_key: str, field: str) -> str:
    title = "Выберите час начала работы" if field == "start" else "Выберите час окончания работы"
    return f"{title}\n{_cls_manager_label(manager_key)}"


def _cls_hour_buttons(manager_key: str, field: str) -> list:
    rows = []
    for row_start in range(0, 24, 6):
        rows.append([
            Button.inline(f"{h:02d}", f"cls:h:{field}:{manager_key}:{h}".encode("utf-8"))
            for h in range(row_start, row_start + 6)
        ])
    rows.append([Button.inline("⬅️ Назад", f"cls:sched:{manager_key}".encode("utf-8"))])
    return rows


def _cls_view_text(manager_key: str) -> str:
    start, end = _cls_schedule_get(manager_key)
    greet = _cls_get_setting(_cls_greeting_key(manager_key)).strip()
    away = _cls_get_setting(_cls_away_key(manager_key)).strip()
    updated = _cls_get_setting(_cls_updated_key(manager_key)).strip()
    day_on = _cls_get_setting(f"closer_day_greeting_enabled_{manager_key}").strip() == "1"
    night_on = _cls_get_setting(f"closer_night_greeting_enabled_{manager_key}").strip() == "1"
    lines = [
        f"👁 Текущие настройки: {_cls_manager_label(manager_key)}",
        "",
        f"График: {start}–{end}",
        "",
        f"Дневное приветствие: {'🟢 ВКЛ' if day_on else '🔴 ВЫКЛ'}",
        f"Ночное приветствие: {'🟢 ВКЛ' if night_on else '🔴 ВЫКЛ'}",
        "",
        "Текст приветствия:",
        greet if greet else "(используется общий текст по умолчанию)",
        "",
        "Текст «нет на месте»:",
        away if away else "(используется общий текст по умолчанию)",
    ]
    if updated:
        lines.append("")
        lines.append(f"Обновлено: {updated}")
    return "\n".join(lines)


def _cls_text_prompt(field: str) -> str:
    label = "приветствия" if field == "greet" else "«нет на месте»"
    return (
        f"Отправьте новый текст {label} одним сообщением.\n"
        f"Максимум {_CLS_TEXT_MAX_LEN} символов. Перенос строк сохраняется."
    )


def _cls_validate_text(raw: str) -> Tuple[bool, str]:
    t = str(raw or "").strip("\n\r\t ")
    if not t:
        return False, "Текст пустой. Отправьте текст ещё раз или нажмите /start чтобы выйти."
    if len(t) > _CLS_TEXT_MAX_LEN:
        return False, f"Слишком длинный текст ({len(t)} символов). Максимум {_CLS_TEXT_MAX_LEN}. Отправьте короче."
    return True, t


async def _cls_handle_text_input(event, tg_user_id: int, pending: dict, raw_text: str) -> None:
    manager_key = str(pending.get("manager_key") or "")
    field = str(pending.get("field") or "")
    if not _cls_owns_key(tg_user_id, manager_key):
        _CLS_PENDING.pop(tg_user_id, None)
        return
    ok, result = _cls_validate_text(raw_text)
    if not ok:
        try:
            await event.respond(result)
        except Exception:
            pass
        return  # stay pending, allow retry
    _CLS_PENDING.pop(tg_user_id, None)
    _CLS_DRAFT[tg_user_id] = {"manager_key": manager_key, "field": field, "text": result}
    label = "приветствия" if field == "greet" else "«нет на месте»"
    try:
        await event.respond(
            f"Предпросмотр текста {label}:\n\n{result}",
            buttons=[[
                Button.inline("✅ Сохранить", b"cls:save"),
                Button.inline("❌ Отмена", b"cls:cancel"),
            ]],
        )
    except Exception:
        pass


@client.on(events.CallbackQuery)
async def _cls_callback(event):
    try:
        data = (event.data or b"").decode("utf-8", "ignore")
        if not data.startswith("cls:"):
            return
        tg_user_id = int(event.sender_id or 0)
        if tg_user_id <= 0:
            return
        closer_keys = _cls_closer_keys(tg_user_id)
        if not closer_keys:
            try:
                await event.answer("Доступ запрещён", alert=True)
            except Exception:
                pass
            return

        if data == "cls:menu":
            if len(closer_keys) == 1:
                await event.edit(_cls_menu_text(closer_keys[0]), buttons=_cls_menu_buttons(closer_keys[0]))
            else:
                await event.edit(
                    "Выберите аккаунт клоузера:",
                    buttons=[[Button.inline(_cls_manager_label(k), f"cls:m:{k}".encode("utf-8"))] for k in closer_keys]
                    + [[Button.inline("⬅️ Назад", b"cls:home")]],
                )
            await event.answer()
            return

        if data == "cls:home":
            _CLS_PENDING.pop(tg_user_id, None)
            _CLS_DRAFT.pop(tg_user_id, None)
            try:
                await event.edit("Готово.", buttons=_mbstat_start_buttons(tg_user_id) or None)
            except Exception:
                pass
            await event.answer()
            return

        if data == "cls:save":
            draft = _CLS_DRAFT.get(tg_user_id)
            if not draft or not _cls_owns_key(tg_user_id, draft.get("manager_key") or ""):
                await event.answer("Нет несохранённого текста.", alert=True)
                return
            manager_key = draft["manager_key"]
            field = draft["field"]
            text_val = draft["text"]
            setting_key = _cls_greeting_key(manager_key) if field == "greet" else _cls_away_key(manager_key)
            _cls_set_setting(setting_key, text_val)
            _cls_set_setting(_cls_updated_key(manager_key), _now_iso())
            _CLS_DRAFT.pop(tg_user_id, None)
            log.info("closer settings saved manager=%s uid=%s field=%s len=%s", manager_key, tg_user_id, field, len(text_val))
            await event.edit("✅ Сохранено.", buttons=_cls_menu_buttons(manager_key))
            await event.answer()
            return

        if data == "cls:cancel":
            _CLS_DRAFT.pop(tg_user_id, None)
            await event.edit("❌ Отменено.", buttons=_mbstat_start_buttons(tg_user_id) or None)
            await event.answer()
            return

        # cls:h:{start|end}:{manager_key}:{hour} -- hour-picker final selection.
        # Handled BEFORE the generic parts[-1]-is-manager_key parsing below
        # because this namespace has an extra trailing hour segment; the
        # manager_key ownership re-check still happens here, same as every
        # other write path.
        if data.startswith("cls:h:"):
            hour_parts = data.split(":")
            if len(hour_parts) != 5 or hour_parts[2] not in ("start", "end"):
                await event.answer()
                return
            hour_manager_key = _norm_key(hour_parts[3])
            if not _cls_owns_key(tg_user_id, hour_manager_key):
                await event.answer("Доступ запрещён", alert=True)
                return
            try:
                hour_val = int(hour_parts[4])
            except Exception:
                await event.answer("Некорректный час.", alert=True)
                return
            if hour_val < 0 or hour_val > 23:
                await event.answer("Некорректный час.", alert=True)
                return
            ok, msg = _cls_apply_hour(hour_manager_key, hour_parts[2], hour_val, tg_user_id)
            if not ok:
                await event.answer(msg, alert=True)
                return
            await event.edit(_cls_sched_text(hour_manager_key), buttons=_cls_sched_buttons(hour_manager_key))
            await event.answer("Сохранено")
            return

        parts = data.split(":")
        if len(parts) < 3:
            await event.answer()
            return

        # Everything below carries an explicit manager_key as the LAST segment --
        # re-validated against this uid's own closer_keys before any action.
        manager_key = _norm_key(parts[-1])
        if not _cls_owns_key(tg_user_id, manager_key):
            await event.answer("Доступ запрещён", alert=True)
            return

        if parts[1] == "m" and len(parts) == 3:
            await event.edit(_cls_menu_text(manager_key), buttons=_cls_menu_buttons(manager_key))
            await event.answer()
            return

        if parts[1] == "sched" and len(parts) == 3:
            await event.edit(_cls_sched_text(manager_key), buttons=_cls_sched_buttons(manager_key))
            await event.answer()
            return

        if parts[1] == "view" and len(parts) == 3:
            await event.edit(_cls_view_text(manager_key), buttons=[[Button.inline("⬅️ Назад", f"cls:m:{manager_key}".encode("utf-8"))]])
            await event.answer()
            return

        if parts[1] == "hp" and len(parts) == 4 and parts[2] in ("start", "end"):
            await event.edit(_cls_hour_picker_text(manager_key, parts[2]), buttons=_cls_hour_buttons(manager_key, parts[2]))
            await event.answer()
            return

        if parts[1] == "p" and len(parts) == 4:
            preset = _CLS_PRESETS.get(parts[2])
            if not preset:
                await event.answer("Неизвестный пресет.", alert=True)
                return
            _cls_schedule_set(manager_key, preset[0], preset[1], tg_user_id)
            log.info("closer schedule preset saved manager=%s uid=%s preset=%s", manager_key, tg_user_id, parts[2])
            await event.edit(_cls_sched_text(manager_key), buttons=_cls_sched_buttons(manager_key))
            await event.answer("Сохранено")
            return

        if parts[1] == "tg" and len(parts) == 4 and parts[2] in ("day", "night"):
            toggle_key = f"closer_{parts[2]}_greeting_enabled_{manager_key}"
            new_val = _cls_get_setting(toggle_key).strip() != "1"
            _cls_set_setting(toggle_key, "1" if new_val else "0")
            _cls_set_setting(_cls_updated_key(manager_key), _now_iso())
            log.info("closer greeting toggle saved manager=%s uid=%s which=%s enabled=%s", manager_key, tg_user_id, parts[2], new_val)
            await event.edit(_cls_menu_text(manager_key), buttons=_cls_menu_buttons(manager_key))
            await event.answer("Включено" if new_val else "Выключено")
            return

        if parts[1] == "txt" and len(parts) == 4 and parts[2] in ("greet", "away"):
            _CLS_PENDING[tg_user_id] = {"manager_key": manager_key, "field": parts[2]}
            _CLS_DRAFT.pop(tg_user_id, None)
            await event.answer()
            try:
                await event.respond(_cls_text_prompt(parts[2]))
            except Exception:
                pass
            return

        await event.answer()
    except Exception as exc:
        log.warning("[closer-settings] callback error: %r", exc)
        try:
            await event.answer("Ошибка", alert=True)
        except Exception:
            pass


@client.on(events.NewMessage)
async def _cls_pending_input(event):
    try:
        if not getattr(event, "is_private", False):
            return
        sender = await event.get_sender()
        tg_user_id = int(getattr(sender, "id", 0) or 0)
        if tg_user_id <= 0:
            return
        pending = _CLS_PENDING.get(tg_user_id)
        if not pending or tg_user_id in _MBSTAT_PENDING:
            return
        if getattr(event, "photo", None) is not None or getattr(event, "document", None) is not None:
            return  # not a text reply -- ignore, stay pending
        text = str(event.raw_text or "")
        if text.strip().startswith("/"):
            return  # let the main command dispatcher handle it; pending stays for a later retry

        field = str(pending.get("field") or "")
        if field in ("greet", "away"):
            await _cls_handle_text_input(event, tg_user_id, pending, text)
        else:
            _CLS_PENDING.pop(tg_user_id, None)
    except Exception as exc:
        log.warning("[closer-settings] pending input error: %r", exc)


# Closer-only entry point on /start: appends "⚙️ Настройки клоузера" to the
# existing button chain, same additive PREV-capture pattern as the
# "TPILOT REFRESH CARDS BUTTON" override above. Invisible to any user with
# no closer-role manager_key in their own access_targets.
_CLS_PREV_START_BTNS = globals().get("_mbstat_start_buttons")


def _mbstat_start_buttons(tg_user_id: int):  # type: ignore[override]
    rows: list = []
    try:
        prev = _CLS_PREV_START_BTNS
        if callable(prev):
            result = prev(int(tg_user_id or 0))
            if result:
                rows.extend(result)
    except Exception:
        pass
    try:
        if _cls_closer_keys(int(tg_user_id or 0)):
            rows.append([Button.inline("⚙️ Настройки клоузера", b"cls:menu")])
    except Exception:
        pass
    return rows if rows else None

# --- TPILOT CLOSER SELF-SERVICE SETTINGS 20260713 END -----------------------


# --- W1 SAFE SELF-DIAGNOSTIC 20260729 (button wiring) -----------------------
# Same additive PREV-capture override chain every prior block in this file
# uses (see _CLS_PREV_START_BTNS above) -- delegates to whatever chain was
# active before this override, then appends exactly one new button. Does
# NOT reintroduce /whoami as a command and does NOT change any text
# elsewhere on /start -- only adds one row to the existing buttons list.
# Contract (WAVE0 07_safe_diagnostics_contract.md, Section E): the button is
# visible ONLY to an authenticated ManagerBot user -- gated on the SAME
# _access_allowed(uid) check both real _handle_start call sites already
# satisfy before reaching this function, so this is defense-in-depth (a
# uid with no access at all must still get an empty/None menu, exactly as
# before this change) rather than a behaviour change for any real caller.
_W1_PREV_START_BTNS = globals().get("_mbstat_start_buttons")


def _mbstat_start_buttons(tg_user_id: int):  # type: ignore[override]
    rows: list = []
    try:
        prev = _W1_PREV_START_BTNS
        if callable(prev):
            result = prev(int(tg_user_id or 0))
            if result:
                rows.extend(result)
    except Exception:
        pass
    try:
        if _access_allowed(int(tg_user_id or 0)):
            rows.append([Button.inline("🔎 Мой доступ", b"mb:myaccess:0")])
    except Exception:
        pass
    return rows if rows else None
# --- W1 SAFE SELF-DIAGNOSTIC 20260729 END -----------------------------------


async def main() -> None:
    init_schema()
    Path(MANAGER_BOT_SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)

    log.info(
        "ManagerBot starting db=%s session=%s poll_sec=%s",
        TPILOT_DB_PATH,
        MANAGER_BOT_SESSION_FILE,
        MANAGER_BOT_POLL_SEC,
    )
    await client.start(bot_token=MANAGER_BOT_TOKEN)
    me = await client.get_me()
    log.info("ManagerBot online username=%s id=%s", getattr(me, "username", ""), getattr(me, "id", ""))
    # R1B/F-40: keep a strong reference and retrieve/log any exception. _poll_loop
    # already catches its own errors internally (sleeps 10s and continues), so this
    # is a backstop for a genuinely unexpected crash escaping that loop -- without
    # it, the task would be eligible for GC and any such exception would only ever
    # surface as an "exception was never retrieved" log line from asyncio itself.
    global _MB_POLL_LOOP_TASK
    _MB_POLL_LOOP_TASK = asyncio.create_task(_poll_loop())

    def _mb_poll_loop_done(task: "asyncio.Task") -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("poll loop task ended with unhandled exception error_class=%s", type(exc).__name__)

    _MB_POLL_LOOP_TASK.add_done_callback(_mb_poll_loop_done)
    await client.run_until_disconnected()


if __name__ == "__main__":
    client.loop.run_until_complete(main())
