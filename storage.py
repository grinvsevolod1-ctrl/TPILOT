# -*- coding: utf-8 -*-
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone as _utc_tz
from typing import Any, Dict, List, Optional, Sequence, Tuple

import aiosqlite

try:
    import db_observability as _p5_dbobs  # P5/F0: SQLite contention observability (fail-soft, default disabled)
except Exception:
    _p5_dbobs = None  # tracing must never block storage.py


def _p5_begin_immediate_sync(con, *, source, function, db_path="", manager_key="", event_key=""):
    """Fail-soft wrapper: identical behavior to con.execute("BEGIN IMMEDIATE")
    when db_observability is unavailable or tracing is disabled."""
    if _p5_dbobs is None:
        con.execute("BEGIN IMMEDIATE")
        return None
    try:
        return _p5_dbobs.begin_immediate_sync(
            con, source=source, function=function, db_path=db_path,
            manager_key=manager_key, event_key=event_key,
        )
    except Exception:
        con.execute("BEGIN IMMEDIATE")
        return None


def _p5_commit_sync(tx, con):
    if _p5_dbobs is None or tx is None:
        con.commit()
        return
    try:
        _p5_dbobs.commit_sync(tx, con)
    except Exception:
        con.commit()


def _p5_rollback_sync(tx, con, error=None):
    if _p5_dbobs is None or tx is None:
        con.rollback()
        return
    try:
        _p5_dbobs.rollback_sync(tx, con, error=error)
    except Exception:
        con.rollback()

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "db", "data.db")
DB_PATH = (os.getenv("DB_PATH") or DEFAULT_DB_PATH)
DEFAULT_QUEUE_DB_PATH = os.path.join(os.path.dirname(__file__), "db", "data_tpilot.db")
QUEUE_DB_PATH = (os.getenv("TPILOT_DB_PATH") or os.getenv("QUEUE_DB_PATH") or DEFAULT_QUEUE_DB_PATH)

# --- perf/reliability: shared connection settings (patch perf_conn, 2026-08-22) ---
# Before this helper each aiosqlite.connect() site used per-connection defaults,
# which meant busy_timeout=0 on 87 of 88 sites. With the controller + N manager
# runtimes + 3 bots all writing one SQLite file, that surfaces as spurious
# "database is locked" instead of a short wait.
#
# busy_timeout and synchronous are PER-CONNECTION and must be re-applied on every
# connect; journal_mode=WAL is persisted in the DB file, so it is applied once per
# path to avoid paying for a redundant PRAGMA on every single query.
_DB_BUSY_TIMEOUT_MS = max(0, int(os.getenv("DB_BUSY_TIMEOUT_MS") or "5000"))
_DB_WAL_DONE: set = set()


async def _db_apply_pragmas(db: aiosqlite.Connection, path: str) -> None:
    """Fail-soft PRAGMA setup: a PRAGMA failure must never break the caller's query."""
    try:
        await db.execute(f"PRAGMA busy_timeout={int(_DB_BUSY_TIMEOUT_MS)}")
    except Exception:
        pass
    if path not in _DB_WAL_DONE:
        # Mark first: a locked/failing PRAGMA must not turn into a retry storm.
        _DB_WAL_DONE.add(path)
        try:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass


@contextlib.asynccontextmanager
async def _db_conn(path: Optional[str] = None):
    """Drop-in replacement for `aiosqlite.connect(path)` that applies shared PRAGMAs.

    Call sites keep the exact same shape: `async with _db_conn(DB_PATH) as db:`
    """
    p = path or DB_PATH
    async with aiosqlite.connect(p) as db:
        await _db_apply_pragmas(db, p)
        yield db


def _db_conn_sync(path: Optional[str] = None, *, timeout: float = 30.0) -> sqlite3.Connection:
    """Synchronous counterpart used by the sqlite3-based helpers in this module."""
    p = path or DB_PATH
    con = sqlite3.connect(p, timeout=timeout)
    try:
        con.execute(f"PRAGMA busy_timeout={int(_DB_BUSY_TIMEOUT_MS)}")
    except Exception:
        pass
    if p not in _DB_WAL_DONE:
        _DB_WAL_DONE.add(p)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
    return con


def _utc_now() -> datetime:
    """Naive-UTC clock seam (utcnow refactor, 2026-08-16).

    Single point through which ALL of storage.py reads the UTC wall clock.
    Returns exactly what _utc_now() used to return -- a NAIVE datetime
    in UTC -- so every existing .isoformat() DB string, cutoff comparison and
    timedelta stays byte-identical (an AWARE value would append "+00:00" to
    isoformat() and break string-ordering against stored rows).

    Why a seam instead of a bare call: _utc_now() is deprecated on
    Python 3.12+, and selftests need ONE patch point to freeze the clock
    (they set storage._utc_now = <frozen fn>; the previous technique of
    swapping the whole datetime class only intercepted .utcnow() and silently
    missed datetime.now(tz), which is exactly how the first refactor attempt
    broke -- see the cf66a21 revert).
    """
    return datetime.now(_utc_tz.utc).replace(tzinfo=None)


def _now_iso() -> str:
    return _utc_now().replace(microsecond=0).isoformat()


async def _table_columns(db: aiosqlite.Connection, table: str) -> List[str]:
    cur = await db.execute(f"PRAGMA table_info({table});")
    rows = await cur.fetchall()
    return [r[1] for r in rows]


async def init_db() -> None:
    # NOTE: keep migrations idempotent (prod SQLite).
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    async with _db_conn(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")

        leads_sql = "\n".join([
            "CREATE TABLE IF NOT EXISTS leads(",
            "  chat_id INTEGER PRIMARY KEY,",
            "  username TEXT,",
            "  full_name TEXT,",
            "  first_seen TEXT,",
            "  last_seen TEXT,",
            "  age INTEGER,",
            "  city TEXT,",
            "  country TEXT,",
            "  city_raw TEXT,",
            "  country_raw TEXT,",
            "  remote_intent INTEGER DEFAULT 0,",
            "  welcome_sent INTEGER DEFAULT 0,",
            "  ai_stat_sent INTEGER DEFAULT 0,",
            "  status TEXT,",
            "  nonliquid_reason TEXT,",
            "  manual INTEGER DEFAULT 0,",
            "  profile_done INTEGER DEFAULT 0,",
            "  tw_node TEXT,",
            "  not_liquid_sent INTEGER DEFAULT 0,",
            "  afterhours_pending INTEGER DEFAULT 0,",
            "  afterhours_sent INTEGER DEFAULT 0,",
            "  geo_attempts INTEGER DEFAULT 0,",
            "  night_reply_key TEXT DEFAULT '',",
            "  send_disabled INTEGER DEFAULT 0,",
            "  send_disabled_reason TEXT DEFAULT '',",
            "  send_disabled_at TEXT DEFAULT '',",
            "  spam_strikes INTEGER NOT NULL DEFAULT 0,",
            "  spam_block_until INTEGER NULL,",
            "  spam_last_strike_at INTEGER NULL,",
            "  last_voice_warn_at INTEGER NULL,",
            "  city_asked INTEGER NOT NULL DEFAULT 0,",
            "  city_asked_once INTEGER NOT NULL DEFAULT 0,",
            "  profile_req_attempts INTEGER NOT NULL DEFAULT 0,",
            "  profile_gate_open INTEGER NOT NULL DEFAULT 0,",
            "  profile_gate_open_at TEXT DEFAULT '',",
            "  profile_bad_attempts INTEGER NOT NULL DEFAULT 0,",
            "  profile_name TEXT DEFAULT ''",
            ");",
        ])
        await db.execute(leads_sql)

        messages_sql = "\n".join([
            "CREATE TABLE IF NOT EXISTS messages(",
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,",
            "  chat_id INTEGER,",
            "  direction TEXT,",
            "  text TEXT,",
            "  ts TEXT",
            ");",
        ])
        await db.execute(messages_sql)

        settings_sql = "\n".join([
            "CREATE TABLE IF NOT EXISTS settings(",
            "  key TEXT PRIMARY KEY,",
            "  value TEXT",
            ");",
        ])
        await db.execute(settings_sql)

        followups_sql = "\n".join([
            "CREATE TABLE IF NOT EXISTS followups(",
            "  chat_id INTEGER PRIMARY KEY,",
            "  phase TEXT,",
            "  step INTEGER DEFAULT 0,",
            "  attempts INTEGER DEFAULT 0,",
            "  next_run TEXT,",
            "  last_user_ts TEXT,",
            "  last_bot_ts TEXT,",
            "  last_ping_ts TEXT",
            ");",
        ])
        await db.execute(followups_sql)

        await db.execute("CREATE TABLE IF NOT EXISTS managers(id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL DEFAULT '', phone TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new', session_path TEXT DEFAULT '', db_path TEXT DEFAULT '', workdir TEXT DEFAULT '', log_path TEXT DEFAULT '', is_enabled INTEGER NOT NULL DEFAULT 1, manual_stopped INTEGER NOT NULL DEFAULT 0, owner_user_id INTEGER, tg_user_id INTEGER, telegram_username TEXT DEFAULT '', first_name TEXT DEFAULT '', last_name TEXT DEFAULT '', last_login_at TEXT DEFAULT '', last_error TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
        await db.execute("CREATE TABLE IF NOT EXISTS manager_onboarding(owner_user_id INTEGER PRIMARY KEY, manager_key TEXT NOT NULL, step TEXT NOT NULL DEFAULT '', phone TEXT DEFAULT '', phone_code_hash TEXT DEFAULT '', tmp_session_path TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT '', expires_at TEXT DEFAULT '', next_code_allowed_at TEXT DEFAULT '', last_code_sent_at TEXT DEFAULT '', last_send_error TEXT DEFAULT '', proxy_type TEXT DEFAULT '', proxy_host TEXT DEFAULT '', proxy_port INTEGER, proxy_username TEXT DEFAULT '', proxy_password TEXT DEFAULT '', proxy_enabled INTEGER NOT NULL DEFAULT 0, proxy_bypass_allowed INTEGER NOT NULL DEFAULT 0, proxy_mode TEXT DEFAULT '')")
        await db.execute("CREATE TABLE IF NOT EXISTS manager_auth_sessions(user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '')")
        await db.execute("CREATE TABLE IF NOT EXISTS access_users(tg_user_id INTEGER PRIMARY KEY, display_name TEXT DEFAULT '', username TEXT DEFAULT '', access_level INTEGER NOT NULL DEFAULT 1, scope_mode TEXT NOT NULL DEFAULT 'selected', is_enabled INTEGER NOT NULL DEFAULT 1, created_by INTEGER, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
        await db.execute("CREATE TABLE IF NOT EXISTS access_targets(tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(tg_user_id, manager_key))")
        await db.execute("CREATE TABLE IF NOT EXISTS manager_commands(id INTEGER PRIMARY KEY AUTOINCREMENT, nonce TEXT UNIQUE NOT NULL, target_key TEXT NOT NULL, command TEXT NOT NULL DEFAULT '', args TEXT DEFAULT '', payload_json TEXT DEFAULT '', created_by TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL DEFAULT '', available_at TEXT DEFAULT '', expires_at TEXT DEFAULT '', taken_at TEXT DEFAULT '', started_at TEXT DEFAULT '', finished_at TEXT DEFAULT '', worker_key TEXT DEFAULT '', result_ok INTEGER, result_text TEXT DEFAULT '', result_json TEXT DEFAULT '', error_text TEXT DEFAULT '')")
        for ddl in [
            "ALTER TABLE managers ADD COLUMN manual_stopped INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE managers ADD COLUMN proxy_type TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN proxy_host TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN proxy_port INTEGER",
            "ALTER TABLE managers ADD COLUMN proxy_username TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN proxy_password TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN proxy_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE managers ADD COLUMN proxy_updated_at TEXT DEFAULT ''",
            # TPILOT_PROXY_AUTH_GUARD_V2_COLUMNS_20260509,
            "ALTER TABLE managers ADD COLUMN proxy_mode TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_guard_ok INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE managers ADD COLUMN auth_guard_checked_at TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_proxy_ip TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_proxy_country TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_proxy_city TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_proxy_region TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_direct_ip TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_direct_country TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_direct_city TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_direct_region TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_guard_error TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_guard_source TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_guard_last_bad_at TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_guard_last_ok_at TEXT DEFAULT ''",
            "ALTER TABLE managers ADD COLUMN auth_guard_notified_bad INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE managers ADD COLUMN proxy_required INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE managers ADD COLUMN proxy_test_ok INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE managers ADD COLUMN proxy_test_at TEXT DEFAULT ''",
        "ALTER TABLE managers ADD COLUMN proxy_last_error TEXT DEFAULT ''",
        "ALTER TABLE managers ADD COLUMN proxy_bypass_allowed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE managers ADD COLUMN proxy_bypass_by INTEGER",
        "ALTER TABLE managers ADD COLUMN proxy_bypass_at TEXT DEFAULT ''",
        "ALTER TABLE managers ADD COLUMN proxy_bypass_reason TEXT DEFAULT ''",
        # MANAGER_PROFILE_SYNC_20260711: 'auto' = display_name may be refreshed from the
        # manager's live Telegram profile; 'manual' = admin set it via manager_rename and
        # it must never be overwritten by the Telegram profile sync. Additive, idempotent.
        "ALTER TABLE managers ADD COLUMN display_name_source TEXT NOT NULL DEFAULT 'auto'",
        # TPILOT TDATA/SESSION IMPORT AUTH PROFILE 20260719: 'project' (default) = this
        # manager's runtime/probe connects with the project's own API_ID/API_HASH+device
        # (phone/code/2FA and QR managers -- unchanged); 'tdesktop' = this manager was
        # authorized via direct .session/tdata import and MUST keep connecting under the
        # Telegram Desktop API/device profile it was originally authorized with (opentele
        # UseCurrentSession semantics) on every future runtime start, not just the probe.
        "ALTER TABLE managers ADD COLUMN auth_profile TEXT NOT NULL DEFAULT 'project'",
            "ALTER TABLE manager_onboarding ADD COLUMN next_code_allowed_at TEXT DEFAULT ''",
            "ALTER TABLE manager_onboarding ADD COLUMN last_code_sent_at TEXT DEFAULT ''",
            "ALTER TABLE manager_onboarding ADD COLUMN last_send_error TEXT DEFAULT ''",
            # Stage 2 fix R2 (2026-07-15): durable proxy scratch state, same
            # security model/table as the existing phone/phone_code_hash
            # columns above -- lets a replacement operation reconnect to its
            # temp Telethon session after a restart with the EXACT proxy it
            # started with, never silently falling back to direct.
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_type TEXT DEFAULT ''",
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_host TEXT DEFAULT ''",
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_port INTEGER",
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_username TEXT DEFAULT ''",
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_password TEXT DEFAULT ''",
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_bypass_allowed INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manager_onboarding ADD COLUMN proxy_mode TEXT DEFAULT ''",
        ]:
            try:
                await db.execute(ddl)
            except Exception:
                pass

        await db.commit()

        # ---- migrations (idempotent) ----
        leads_cols = await _table_columns(db, "leads")
        lead_migrations: List[Tuple[str, str]] = [
            # identity / timestamps
            ("username", "ALTER TABLE leads ADD COLUMN username TEXT"),
            ("full_name", "ALTER TABLE leads ADD COLUMN full_name TEXT"),
            ("first_seen", "ALTER TABLE leads ADD COLUMN first_seen TEXT"),
            ("last_seen", "ALTER TABLE leads ADD COLUMN last_seen TEXT"),

            # анкета
            ("age", "ALTER TABLE leads ADD COLUMN age INTEGER"),
            ("city", "ALTER TABLE leads ADD COLUMN city TEXT"),
            ("country", "ALTER TABLE leads ADD COLUMN country TEXT"),
            ("city_raw", "ALTER TABLE leads ADD COLUMN city_raw TEXT"),
            ("country_raw", "ALTER TABLE leads ADD COLUMN country_raw TEXT"),
            ("status", "ALTER TABLE leads ADD COLUMN status TEXT"),
            ("nonliquid_reason", "ALTER TABLE leads ADD COLUMN nonliquid_reason TEXT"),
            ("profile_done", "ALTER TABLE leads ADD COLUMN profile_done INTEGER NOT NULL DEFAULT 0"),
            ("profile_gate_open", "ALTER TABLE leads ADD COLUMN profile_gate_open INTEGER NOT NULL DEFAULT 0"),
            ("city_asked", "ALTER TABLE leads ADD COLUMN city_asked INTEGER NOT NULL DEFAULT 0"),
            ("city_asked_once", "ALTER TABLE leads ADD COLUMN city_asked_once INTEGER NOT NULL DEFAULT 0"),

            # дополнительные поля, которые main_final.py использует
            ("remote_intent", "ALTER TABLE leads ADD COLUMN remote_intent INTEGER NOT NULL DEFAULT 0"),
            ("welcome_sent", "ALTER TABLE leads ADD COLUMN welcome_sent INTEGER NOT NULL DEFAULT 0"),
            ("ai_stat_sent", "ALTER TABLE leads ADD COLUMN ai_stat_sent INTEGER NOT NULL DEFAULT 0"),
            ("manual", "ALTER TABLE leads ADD COLUMN manual INTEGER NOT NULL DEFAULT 0"),
            ("tw_node", "ALTER TABLE leads ADD COLUMN tw_node TEXT"),
            ("not_liquid_sent", "ALTER TABLE leads ADD COLUMN not_liquid_sent INTEGER NOT NULL DEFAULT 0"),
            ("afterhours_pending", "ALTER TABLE leads ADD COLUMN afterhours_pending INTEGER NOT NULL DEFAULT 0"),
            ("afterhours_sent", "ALTER TABLE leads ADD COLUMN afterhours_sent INTEGER NOT NULL DEFAULT 0"),
            ("geo_attempts", "ALTER TABLE leads ADD COLUMN geo_attempts INTEGER NOT NULL DEFAULT 0"),
            ("night_reply_key", "ALTER TABLE leads ADD COLUMN night_reply_key TEXT DEFAULT ''"),
            ("profile_req_attempts", "ALTER TABLE leads ADD COLUMN profile_req_attempts INTEGER NOT NULL DEFAULT 0"),
            ("profile_gate_open_at", "ALTER TABLE leads ADD COLUMN profile_gate_open_at TEXT DEFAULT ''"),
            ("profile_bad_attempts", "ALTER TABLE leads ADD COLUMN profile_bad_attempts INTEGER NOT NULL DEFAULT 0"),
            ("profile_name", "ALTER TABLE leads ADD COLUMN profile_name TEXT DEFAULT ''"),

            # антиспам / send_disabled
            ("spam_strikes", "ALTER TABLE leads ADD COLUMN spam_strikes INTEGER NOT NULL DEFAULT 0"),
            ("spam_block_until", "ALTER TABLE leads ADD COLUMN spam_block_until INTEGER NULL"),
            ("spam_last_strike_at", "ALTER TABLE leads ADD COLUMN spam_last_strike_at INTEGER NULL"),
            ("send_disabled", "ALTER TABLE leads ADD COLUMN send_disabled INTEGER NOT NULL DEFAULT 0"),
            ("send_disabled_reason", "ALTER TABLE leads ADD COLUMN send_disabled_reason TEXT DEFAULT ''"),
            ("send_disabled_at", "ALTER TABLE leads ADD COLUMN send_disabled_at TEXT DEFAULT ''"),
            ("last_voice_warn_at", "ALTER TABLE leads ADD COLUMN last_voice_warn_at INTEGER NULL"),
        ]
        for col, ddl in lead_migrations:
            if col not in leads_cols:
                try:
                    await db.execute(ddl)
                except Exception:
                    pass

        fu_cols: List[str] = []
        try:
            fu_cols = await _table_columns(db, "followups")
        except Exception:
            fu_cols = []

        fu_migrations: List[Tuple[str, str]] = [
            ("phase", "ALTER TABLE followups ADD COLUMN phase TEXT"),
            ("step", "ALTER TABLE followups ADD COLUMN step INTEGER DEFAULT 0"),
            ("attempts", "ALTER TABLE followups ADD COLUMN attempts INTEGER DEFAULT 0"),
            ("next_run", "ALTER TABLE followups ADD COLUMN next_run TEXT"),
            ("last_user_ts", "ALTER TABLE followups ADD COLUMN last_user_ts TEXT"),
            ("last_bot_ts", "ALTER TABLE followups ADD COLUMN last_bot_ts TEXT"),
            ("last_ping_ts", "ALTER TABLE followups ADD COLUMN last_ping_ts TEXT"),
        ]
        for col, ddl in fu_migrations:
            if col not in fu_cols:
                try:
                    await db.execute(ddl)
                except Exception:
                    pass

        # indices (performance)
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS leads_stat_idx ON leads(profile_done, status, send_disabled, country, age);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS followups_due_idx ON followups(next_run);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS managers_key_idx ON managers(manager_key);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS managers_owner_idx ON managers(owner_user_id);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS managers_status_idx ON managers(status, is_enabled);")
        except Exception:
            pass
        try:
            # perf (patch perf_idx): tg_user_id had no index despite being a hot
            # lookup key. Verified unindexed via PRAGMA index_list; managers is
            # low-write, so the added INSERT cost is negligible.
            await db.execute("CREATE INDEX IF NOT EXISTS managers_tg_user_idx ON managers(tg_user_id);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS manager_commands_target_status_idx ON manager_commands(target_key, status, available_at, created_at);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS manager_commands_nonce_idx ON manager_commands(nonce);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS manager_commands_status_expires_idx ON manager_commands(status, expires_at);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS access_users_enabled_idx ON access_users(is_enabled, access_level);")
        except Exception:
            pass
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS access_targets_user_idx ON access_targets(tg_user_id, manager_key);")
        except Exception:
            pass

        await db.commit()

        # One-time cleanup for inflated stats (first_seen based on inbound messages)
        await _maybe_fix_first_seen(db)


async def upsert_lead(
    chat_id: int,
    username: Optional[str],
    full_name: Optional[str],
    *,
    set_first_seen: bool = False,
    first_seen_iso: Optional[str] = None,
    afterhours_pending: int = 0,
) -> bool:
    # Ensure lead exists and update last_seen.
    # IMPORTANT:
    # - By default does NOT set first_seen (so outbound/bot messages won't create fake "new wrote" stats).
    # - When set_first_seen=True: sets first_seen ONLY if it is currently NULL.
    #   Returns True if first_seen was set by this call.
    now = _now_iso()
    first_seen_iso = (first_seen_iso or now)

    async with _db_conn(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO leads(chat_id, username, full_name, first_seen, last_seen) VALUES(?,?,?,?,?)",
            (chat_id, username, full_name, first_seen_iso if set_first_seen else None, now),
        )

        await db.execute(
            "UPDATE leads SET username=COALESCE(?, username), full_name=COALESCE(?, full_name), last_seen=? WHERE chat_id=?",
            (username, full_name, now, chat_id),
        )

        changed = False
        if set_first_seen:
            cur = await db.execute(
                "UPDATE leads SET first_seen=?, afterhours_pending=? WHERE chat_id=? AND first_seen IS NULL",
                (first_seen_iso, int(afterhours_pending or 0), chat_id),
            )
            try:
                changed = (cur.rowcount or 0) > 0
            except Exception:
                changed = False

        await db.commit()
        return changed


async def get_lead(chat_id: int) -> Optional[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM leads WHERE chat_id=?", (chat_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def _cleanup_followups_for_chat(db: aiosqlite.Connection, chat_id: int) -> None:
    try:
        await db.execute("DELETE FROM followups WHERE chat_id=?", (int(chat_id),))
    except Exception:
        # fallback: if delete is blocked by schema differences, at least clear fields
        try:
            await db.execute("UPDATE followups SET phase='', step=0, attempts=0, next_run='' WHERE chat_id=?", (int(chat_id),))
        except Exception:
            pass


async def set_lead_fields(chat_id: int, **fields: Any) -> None:
    if not fields:
        return

    # Keep backward compatibility: main uses city_asked; canon doc uses city_asked_once.
    if "city_asked" in fields and "city_asked_once" not in fields:
        fields["city_asked_once"] = fields.get("city_asked")
    if "city_asked_once" in fields and "city_asked" not in fields:
        fields["city_asked"] = fields.get("city_asked_once")

    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sql = "UPDATE leads SET " + ", ".join([f"{k}=?" for k in keys]) + " WHERE chat_id=?"
    vals.append(chat_id)

    async with _db_conn(DB_PATH) as db:
        await db.execute(sql, tuple(vals))

        # If send_disabled was set here -> cleanup followups so they don't "wake up" later
        try:
            if "send_disabled" in fields and int(fields.get("send_disabled") or 0) == 1:
                await _cleanup_followups_for_chat(db, chat_id)
        except Exception:
            pass

        await db.commit()


async def set_tw_node(chat_id: int, node: str) -> None:
    await set_lead_fields(chat_id, tw_node=node)


async def set_manual(chat_id: int, manual: int) -> None:
    await set_lead_fields(chat_id, manual=int(manual))


async def list_manual_chats(limit: int = 30) -> List[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT chat_id, username, full_name, last_seen, city, country FROM leads WHERE manual=1 ORDER BY last_seen DESC LIMIT ?",
            (int(limit),),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def add_message(chat_id: int, direction: str, text: str) -> None:
    ts = _now_iso()
    async with _db_conn(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages(chat_id, direction, text, ts) VALUES(?,?,?,?)",
            (int(chat_id), str(direction), str(text or ""), ts),
        )
        await db.commit()


async def list_afterhours_pending(limit: int = 500) -> List[int]:
    # Return chat_ids that first wrote вне рабочего времени and are pending for /add.
    async with _db_conn(DB_PATH) as db:
        cur = await db.execute(
            "SELECT chat_id FROM leads WHERE afterhours_pending=1 AND afterhours_sent=0 AND first_seen IS NOT NULL ORDER BY first_seen ASC LIMIT ?",
            (int(limit),),
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def mark_afterhours_sent(chat_id: int) -> None:
    async with _db_conn(DB_PATH) as db:
        await db.execute(
            "UPDATE leads SET afterhours_pending=0, afterhours_sent=1 WHERE chat_id=?",
            (int(chat_id),),
        )
        await db.commit()


async def clear_afterhours_pending(chat_id: int) -> None:
    async with _db_conn(DB_PATH) as db:
        await db.execute(
            "UPDATE leads SET afterhours_pending=0 WHERE chat_id=?",
            (int(chat_id),),
        )
        await db.commit()


async def _maybe_fix_first_seen(db: aiosqlite.Connection) -> None:
    # One-time cleanup:
    # - If chat has inbound messages => first_seen = MIN(in.ts)
    # - If no inbound messages => first_seen = NULL
    # This prevents stats from being inflated by outbound/bot messages.
    try:
        cur = await db.execute("SELECT value FROM settings WHERE key='first_seen_fix_v1'")
        row = await cur.fetchone()
        if row and str(row[0]) == "1":
            return
    except Exception:
        pass

    try:
        await db.execute(
            "\n".join([
                "UPDATE leads",
                "SET first_seen = (",
                "  SELECT MIN(ts) FROM messages m",
                "  WHERE m.chat_id = leads.chat_id AND m.direction='in'",
                ")",
                "WHERE EXISTS (",
                "  SELECT 1 FROM messages m",
                "  WHERE m.chat_id = leads.chat_id AND m.direction='in'",
                ")",
            ])
        )
        await db.execute(
            "\n".join([
                "UPDATE leads",
                "SET first_seen = NULL",
                "WHERE NOT EXISTS (",
                "  SELECT 1 FROM messages m",
                "  WHERE m.chat_id = leads.chat_id AND m.direction='in'",
                ")",
            ])
        )
        await db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('first_seen_fix_v1','1')")
        await db.commit()
    except Exception:
        pass


async def get_last_bot_reply(chat_id: int) -> Optional[str]:
    async with _db_conn(DB_PATH) as db:
        cur = await db.execute(
            "SELECT text FROM messages WHERE chat_id=? AND direction LIKE 'out%' ORDER BY id DESC LIMIT 1",
            (int(chat_id),),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def _ensure_settings_table(db: aiosqlite.Connection) -> None:
    try:
        await db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);")
    except Exception:
        pass


async def set_setting(key: str, value: str) -> None:
    async with _db_conn(DB_PATH) as db:
        await _ensure_settings_table(db)
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(key), str(value)),
        )
        await db.commit()


async def get_setting(key: str) -> Optional[str]:
    async with _db_conn(DB_PATH) as db:
        await _ensure_settings_table(db)
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (str(key),))
        row = await cur.fetchone()
        return row[0] if row else None


async def get_pause_state() -> Tuple[bool, Optional[str]]:
    enabled = await get_setting("pause_enabled")
    since = await get_setting("pause_since")
    return (enabled == "1"), since


async def set_pause_enabled(enabled: bool) -> None:
    if enabled:
        await set_setting("pause_enabled", "1")
        await set_setting("pause_since", _now_iso())
    else:
        await set_setting("pause_enabled", "0")
        await set_setting("pause_since", "")


async def set_last_active_chat_id(chat_id: int) -> None:
    await set_setting("last_active_chat_id", str(int(chat_id)))


async def get_last_active_chat_id() -> Optional[int]:
    v = await get_setting("last_active_chat_id")
    try:
        return int(v) if v else None
    except Exception:
        return None


async def ensure_followup(chat_id: int) -> None:
    async with _db_conn(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO followups(chat_id, phase, step, attempts) VALUES(?,?,?,?)",
            (int(chat_id), "", 0, 0),
        )
        await db.commit()


async def get_followup(chat_id: int) -> Optional[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM followups WHERE chat_id=?", (int(chat_id),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_followup_fields(chat_id: int, **fields: Any) -> None:
    if not fields:
        return
    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sql = "UPDATE followups SET " + ", ".join([f"{k}=?" for k in keys]) + " WHERE chat_id=?"
    vals.append(int(chat_id))
    async with _db_conn(DB_PATH) as db:
        await db.execute(sql, tuple(vals))
        await db.commit()


async def get_due_followups(now_iso: str) -> List[int]:
    # Returns list of chat_id where next_run <= now_iso (ISO strings).
    async with _db_conn(DB_PATH) as db:
        cur = await db.execute(
            "SELECT chat_id FROM followups WHERE next_run IS NOT NULL AND next_run<>'' AND next_run<=?",
            (str(now_iso),),
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


# ---- canon helpers (requested) ----
async def set_send_disabled(chat_id: int, reason: str) -> None:
    # Sets send_disabled=1 and clears ALL followups for this chat_id.
    now = _now_iso()
    async with _db_conn(DB_PATH) as db:
        await db.execute(
            "UPDATE leads SET send_disabled=1, send_disabled_reason=?, send_disabled_at=? WHERE chat_id=?",
            (str(reason or ""), now, int(chat_id)),
        )
        await _cleanup_followups_for_chat(db, chat_id)
        await db.commit()


async def schedule_city_wait(chat_id: int, *, delay_sec: int = 60) -> None:
    # Upsert followup row and schedule city_wait phase.
    await ensure_followup(chat_id)
    next_run = (_utc_now() + timedelta(seconds=int(delay_sec))).replace(microsecond=0).isoformat()
    await set_followup_fields(chat_id, phase="city_wait", step=0, attempts=0, next_run=next_run, last_bot_ts=_now_iso())


async def finish_city_wait(chat_id: int) -> None:
    # Clear city_wait phase.
    await ensure_followup(chat_id)
    fu = await get_followup(chat_id)
    if fu and (fu.get("phase") or "") == "city_wait":
        await set_followup_fields(chat_id, phase="", step=0, attempts=0, next_run="")


# ---- /stat (critical invariants) ----
def _valid_lead_where(alias: str = "l") -> str:
    # total selection (must match canon):
    # profile_done=1 AND status in (liquid/nonliquid) AND send_disabled=0 AND age+country present
    a = alias
    return (
        f"COALESCE({a}.profile_done,0)=1 "
        f"AND {a}.status IN ('liquid','nonliquid') "
        f"AND COALESCE({a}.send_disabled,0)=0 "
        f"AND {a}.age IS NOT NULL AND {a}.country IS NOT NULL"
    )


async def _stat_bucket_for_window(
    db: aiosqlite.Connection,
    *,
    start_iso: str,
    end_iso: str,
    day_start_iso: Optional[str] = None,
    day_end_iso: Optional[str] = None,
    work_start_iso: Optional[str] = None,
    work_end_iso: Optional[str] = None,
    is_afterhours: bool = False,
) -> Dict[str, Any]:
    # Writers are defined by inbound messages in the time window.
    # For afterhours we take inbound messages in [day_start, day_end) EXCLUDING [work_start, work_end).
    if is_afterhours:
        if not (day_start_iso and day_end_iso and work_start_iso and work_end_iso):
            return {"total": 0, "liquid": 0, "nonliquid": 0, "reasons": []}
        writers_where = "direction='in' AND ts>=? AND ts<? AND (ts<? OR ts>=?)"
        writers_params = [day_start_iso, day_end_iso, work_start_iso, work_end_iso]
    else:
        writers_where = "direction='in' AND ts>=? AND ts<?"
        writers_params = [start_iso, end_iso]

    valid_where = _valid_lead_where("l")

    sql_counts = "\n".join([
        "WITH writers AS (",
        "  SELECT DISTINCT chat_id",
        "  FROM messages",
        f"  WHERE {writers_where}",
        "),",
        "valid AS (",
        "  SELECT l.status AS status, l.country AS country, l.age AS age, l.nonliquid_reason AS nonliquid_reason",
        "  FROM writers w",
        "  JOIN leads l ON l.chat_id = w.chat_id",
        f"  WHERE {valid_where}",
        ")",
        "SELECT",
        "  COUNT(*) AS total,",
        "  SUM(CASE WHEN status='liquid' AND country='Россия' AND age>=18 THEN 1 ELSE 0 END) AS liquid,",
        "  SUM(CASE WHEN status='nonliquid' THEN 1 ELSE 0 END) AS nonliquid",
        "FROM valid",
    ])

    cur = await db.execute(sql_counts, tuple(writers_params))
    row = await cur.fetchone()
    total = int((row[0] if row and row[0] is not None else 0) or 0)
    liquid = int((row[1] if row and row[1] is not None else 0) or 0)
    nonliquid = int((row[2] if row and row[2] is not None else 0) or 0)

    sql_reasons = "\n".join([
        "WITH writers AS (",
        "  SELECT DISTINCT chat_id",
        "  FROM messages",
        f"  WHERE {writers_where}",
        ")",
        "SELECT COALESCE(l.nonliquid_reason,'') AS reason, COUNT(*) AS cnt",
        "FROM writers w",
        "JOIN leads l ON l.chat_id = w.chat_id",
        f"WHERE {valid_where} AND l.status='nonliquid'",
        "GROUP BY COALESCE(l.nonliquid_reason,'')",
        "ORDER BY cnt DESC",
    ])
    cur2 = await db.execute(sql_reasons, tuple(writers_params))
    reasons_rows = await cur2.fetchall()
    reasons: List[Tuple[Optional[str], int]] = []
    for rr in (reasons_rows or []):
        try:
            reasons.append((rr[0], int(rr[1] or 0)))
        except Exception:
            pass

    # Enforce invariants explicitly (defensive)
    if nonliquid > total:
        nonliquid = total
    if liquid > total:
        liquid = total

    return {"total": total, "liquid": liquid, "nonliquid": nonliquid, "reasons": reasons}


async def stats_today_kyiv(work_start_hour: int = 8, work_end_hour: int = 17) -> Dict[str, Any]:
    # Returns buckets for today in Europe/Kyiv:
    #  - work: inbound writers in [work_start, work_end)
    #  - afterhours: inbound writers outside work window within same local day
    from datetime import timezone

    tz = w3_tz()
    now_local = datetime.now(tz=tz)

    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    work_start_local = day_start_local.replace(hour=int(work_start_hour), minute=0, second=0, microsecond=0)
    work_end_local = day_start_local.replace(hour=int(work_end_hour), minute=0, second=0, microsecond=0)

    def _to_utc_iso(dt_local: datetime) -> str:
        return dt_local.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()

    day_start_iso = _to_utc_iso(day_start_local)
    day_end_iso = _to_utc_iso(day_end_local)
    work_start_iso = _to_utc_iso(work_start_local)
    work_end_iso = _to_utc_iso(work_end_local)

    async with _db_conn(DB_PATH) as db:
        work = await _stat_bucket_for_window(db, start_iso=work_start_iso, end_iso=work_end_iso)
        afterhours = await _stat_bucket_for_window(
            db,
            start_iso=day_start_iso,
            end_iso=day_end_iso,
            day_start_iso=day_start_iso,
            day_end_iso=day_end_iso,
            work_start_iso=work_start_iso,
            work_end_iso=work_end_iso,
            is_afterhours=True,
        )

    return {
        "now_kyiv": now_local,
        "work": work,
        "afterhours": afterhours,
        "work_start_hour": int(work_start_hour),
        "work_end_hour": int(work_end_hour),
    }




async def stats_flights_kyiv(*, start_hour: int = 17, end_hour: int = 8) -> Dict[str, Any]:
    # Returns a single bucket for "flights" window in Europe/Kyiv.
    # The window is [start_hour, end_hour) crossing midnight (e.g. 17:00 -> 08:00 next day).
    # The label date is the local date of the window start.
    from datetime import timezone

    tz = w3_tz()
    now_local = datetime.now(tz=tz)

    today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    today_17 = today_start.replace(hour=int(start_hour), minute=0, second=0, microsecond=0)
    today_8 = today_start.replace(hour=int(end_hour), minute=0, second=0, microsecond=0)

    # If now is before end_hour (e.g. 08:00), we are still in the window that started yesterday at start_hour.
    # If now is between end_hour and start_hour, the last window also started yesterday at start_hour.
    # If now is after start_hour, the current window started today at start_hour.
    if now_local >= today_17:
        start_local = today_17
    else:
        start_local = (today_start - timedelta(days=1)).replace(hour=int(start_hour), minute=0, second=0, microsecond=0)

    end_local = start_local + timedelta(hours=((24 - int(start_hour)) + int(end_hour)) if int(end_hour) <= int(start_hour) else (int(end_hour) - int(start_hour)))

    def _to_utc_iso(dt_local: datetime) -> str:
        return dt_local.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()

    start_iso = _to_utc_iso(start_local)
    end_iso = _to_utc_iso(end_local)

    async with _db_conn(DB_PATH) as db:
        flights = await _stat_bucket_for_window(db, start_iso=start_iso, end_iso=end_iso)

    return {
        "now_kyiv": now_local,
        "start_local": start_local,
        "end_local": end_local,
        "flights": flights,
    }


async def stats_today_msk() -> Dict[str, Any]:
    # backward-compatible alias
    return await stats_today_kyiv()


async def fetch_messages_for_export(
    start_utc_iso: str,
    end_utc_iso: str,
    exclude_chat_ids: List[int],
) -> List[Tuple[int, str, str, str]]:
    placeholders = ",".join(["?"] * len(exclude_chat_ids)) if exclude_chat_ids else ""
    sql = "SELECT chat_id, direction, text, ts FROM messages WHERE ts>=? AND ts<?"
    params: List[Any] = [str(start_utc_iso), str(end_utc_iso)]
    if exclude_chat_ids:
        sql += f" AND chat_id NOT IN ({placeholders})"
        params.extend([int(x) for x in exclude_chat_ids])
    sql += " ORDER BY chat_id, id"

    async with _db_conn(DB_PATH) as db:
        cur = await db.execute(sql, tuple(params))
        rows = await cur.fetchall()
        return [(int(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in rows]


# --- multi-instance helpers ---
async def get_setting_from_db(db_path: str, key: str):
    async with _db_conn(db_path) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (str(key),))
        row = await cur.fetchone()
        return row[0] if row else None


async def stats_today_kyiv_for_db(db_path: str, work_start_hour: int = 8, work_end_hour: int = 17) -> Dict[str, Any]:
    # Same as stats_today_kyiv(), but for an explicit SQLite file.
    from datetime import timezone

    tz = w3_tz()
    now_local = datetime.now(tz=tz)

    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end_local = day_start_local + timedelta(days=1)
    work_start_local = day_start_local.replace(hour=int(work_start_hour), minute=0, second=0, microsecond=0)
    work_end_local = day_start_local.replace(hour=int(work_end_hour), minute=0, second=0, microsecond=0)

    def _to_utc_iso(dt_local: datetime) -> str:
        return dt_local.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()

    day_start_iso = _to_utc_iso(day_start_local)
    day_end_iso = _to_utc_iso(day_end_local)
    work_start_iso = _to_utc_iso(work_start_local)
    work_end_iso = _to_utc_iso(work_end_local)

    async with _db_conn(db_path) as db:
        work = await _stat_bucket_for_window(db, start_iso=work_start_iso, end_iso=work_end_iso)
        afterhours = await _stat_bucket_for_window(
            db,
            start_iso=day_start_iso,
            end_iso=day_end_iso,
            day_start_iso=day_start_iso,
            day_end_iso=day_end_iso,
            work_start_iso=work_start_iso,
            work_end_iso=work_end_iso,
            is_afterhours=True,
        )

    return {
        "now_kyiv": now_local,
        "work": work,
        "afterhours": afterhours,
        "work_start_hour": int(work_start_hour),
        "work_end_hour": int(work_end_hour),
    }


async def _manager_table_ready(db: aiosqlite.Connection) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS managers(id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL DEFAULT '', phone TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'new', session_path TEXT DEFAULT '', db_path TEXT DEFAULT '', workdir TEXT DEFAULT '', log_path TEXT DEFAULT '', is_enabled INTEGER NOT NULL DEFAULT 1, manual_stopped INTEGER NOT NULL DEFAULT 0, owner_user_id INTEGER, tg_user_id INTEGER, telegram_username TEXT DEFAULT '', first_name TEXT DEFAULT '', last_name TEXT DEFAULT '', last_login_at TEXT DEFAULT '', last_error TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
    await db.execute("CREATE TABLE IF NOT EXISTS manager_onboarding(owner_user_id INTEGER PRIMARY KEY, manager_key TEXT NOT NULL, step TEXT NOT NULL DEFAULT '', phone TEXT DEFAULT '', phone_code_hash TEXT DEFAULT '', tmp_session_path TEXT DEFAULT '', created_at TEXT NOT NULL DEFAULT '', expires_at TEXT DEFAULT '', next_code_allowed_at TEXT DEFAULT '', last_code_sent_at TEXT DEFAULT '', last_send_error TEXT DEFAULT '', proxy_type TEXT DEFAULT '', proxy_host TEXT DEFAULT '', proxy_port INTEGER, proxy_username TEXT DEFAULT '', proxy_password TEXT DEFAULT '', proxy_enabled INTEGER NOT NULL DEFAULT 0, proxy_bypass_allowed INTEGER NOT NULL DEFAULT 0, proxy_mode TEXT DEFAULT '')")
    await db.execute("CREATE TABLE IF NOT EXISTS manager_auth_sessions(user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '')")
    for ddl in [
        "ALTER TABLE managers ADD COLUMN proxy_type TEXT DEFAULT ''",
        "ALTER TABLE managers ADD COLUMN proxy_host TEXT DEFAULT ''",
        "ALTER TABLE managers ADD COLUMN proxy_port INTEGER",
        "ALTER TABLE managers ADD COLUMN proxy_username TEXT DEFAULT ''",
        "ALTER TABLE managers ADD COLUMN proxy_password TEXT DEFAULT ''",
        "ALTER TABLE managers ADD COLUMN proxy_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE managers ADD COLUMN proxy_updated_at TEXT DEFAULT ''",
        # MANAGER_PROFILE_SYNC_20260711: see the matching ALTER in init_db() for semantics.
        "ALTER TABLE managers ADD COLUMN display_name_source TEXT NOT NULL DEFAULT 'auto'",
        # TPILOT TDATA/SESSION IMPORT AUTH PROFILE 20260719: see the matching ALTER in
        # init_db() for semantics ('project' default vs 'tdesktop' for direct imports).
        "ALTER TABLE managers ADD COLUMN auth_profile TEXT NOT NULL DEFAULT 'project'",
        # Stage 2 fix R2 (2026-07-15): see the matching ALTER block in init_db() for semantics.
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_type TEXT DEFAULT ''",
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_host TEXT DEFAULT ''",
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_port INTEGER",
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_username TEXT DEFAULT ''",
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_password TEXT DEFAULT ''",
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_bypass_allowed INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE manager_onboarding ADD COLUMN proxy_mode TEXT DEFAULT ''",
    ]:
        try:
            await db.execute(ddl)
        except Exception:
            pass
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS managers_proxy_enabled_idx ON managers(proxy_enabled, proxy_type);")
    except Exception:
        pass
    try:
        await db.commit()
    except Exception:
        pass


async def manager_get(manager_key: str) -> Optional[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM managers WHERE manager_key=?", (str(manager_key or ''),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def manager_list_rows(*, include_removed: bool = False, only_enabled: Optional[bool] = None, only_active: Optional[bool] = None) -> List[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        db.row_factory = aiosqlite.Row
        parts = []
        if not include_removed:
            parts.append("COALESCE(status,'') != 'archived'")
        if only_enabled is True:
            parts.append("COALESCE(is_enabled,0)=1")
        elif only_enabled is False:
            parts.append("COALESCE(is_enabled,0)=0")
        if only_active is True:
            parts.append("COALESCE(status,'')='active'")
        elif only_active is False:
            parts.append("COALESCE(status,'')!='active'")
        sql = "SELECT * FROM managers"
        if parts:
            sql += " WHERE " + " AND ".join(parts)
        sql += " ORDER BY id ASC"
        cur = await db.execute(sql)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def manager_add(*, manager_key: str, display_name: str, phone: str, status: str, session_path: str, db_path: str, workdir: str, log_path: str, is_enabled: int = 1, owner_user_id: Optional[int] = None, role: Optional[str] = None) -> None:
    now = _now_iso()
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        await db.execute(
            "INSERT INTO managers(manager_key, display_name, phone, status, session_path, db_path, workdir, log_path, is_enabled, manual_stopped, owner_user_id, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(manager_key), str(display_name), str(phone or ''), str(status), str(session_path or ''), str(db_path or ''), str(workdir or ''), str(log_path or ''), int(is_enabled or 0), 0, owner_user_id, now, now),
        )
        await db.commit()
    # Optional, backward-compatible: existing callers that omit role keep the
    # column's own default ('manager') untouched -- reuses the already-tested
    # manager_set_role primitive (which itself ensures the additive column
    # exists) instead of duplicating that logic in the INSERT above.
    if role is not None and str(role).strip():
        try:
            # Explicit db_path=DB_PATH: manager_set_role's own default falls
            # back to QUEUE_DB_PATH (a distinct global that only happens to
            # equal DB_PATH in production's single-central-db setup) -- pin
            # it to the exact connection the INSERT above just used.
            manager_set_role(manager_key, role, db_path=DB_PATH)
        except Exception:
            pass


async def manager_set_fields(manager_key: str, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields['updated_at'] = _now_iso()
    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sql = "UPDATE managers SET " + ", ".join([f"{k}=?" for k in keys]) + " WHERE manager_key=?"
    vals.append(str(manager_key))
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        await db.execute(sql, tuple(vals))
        await db.commit()


async def manager_rename(manager_key: str, display_name: str) -> None:
    # Admin/manual alias: marks display_name_source='manual' so the Telegram profile
    # sync (manager_sync_telegram_profile_in_db) never overwrites it again.
    await manager_set_fields(manager_key, display_name=str(display_name or ''), display_name_source='manual')


async def manager_soft_remove(manager_key: str) -> None:
    await manager_set_fields(manager_key, is_enabled=0, status='archived')


async def manager_find_by_owner(owner_user_id: int) -> Optional[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM managers WHERE owner_user_id=? AND COALESCE(status,'')!='archived' ORDER BY id DESC LIMIT 1", (int(owner_user_id),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def manager_update_profile_in_db(db_path: str, manager_key: str, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields['updated_at'] = _now_iso()
    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sql = "UPDATE managers SET " + ", ".join([f"{k}=?" for k in keys]) + " WHERE manager_key=?"
    vals.append(str(manager_key))
    async with _db_conn(db_path) as db:
        await _manager_table_ready(db)
        await db.execute(sql, tuple(vals))
        await db.commit()


async def manager_sync_telegram_profile_in_db(
    db_path: str,
    manager_key: str,
    *,
    tg_user_id: Optional[int] = None,
    telegram_username: str = '',
    first_name: str = '',
    last_name: str = '',
    phone: str = '',
    status: str = '',
    last_login_at: str = '',
    last_error: Optional[str] = None,
) -> None:
    """MANAGER_PROFILE_SYNC_20260711: registry-DB profile sync run at manager-runtime
    startup (or any future login/refresh point). Always refreshes telegram_username /
    first_name / last_name / tg_user_id (when provided) -- the fields Telegram itself
    reports. display_name is refreshed from the live Telegram name ONLY when the row's
    display_name_source is not 'manual' (an admin manager_rename); a manual alias is
    never overwritten. Uses the caller-supplied db_path (the registry TPILOT_DB_PATH in
    production), never the module-level DB_PATH, so it cannot accidentally target the
    per-manager runtime DB. Idempotent: re-running with the same Telegram profile writes
    the same values again (a no-op in effect). last_error uses a None sentinel (not '')
    so callers can explicitly clear it (last_error='') on a successful sync without
    every other caller being forced to pass it."""
    key = str(manager_key or '').strip()
    if not key:
        return
    async with _db_conn(db_path) as db:
        await _manager_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT display_name, display_name_source FROM managers WHERE manager_key=?", (key,)
        )
        row = await cur.fetchone()
        current_source = str((row["display_name_source"] if row else '') or '').strip().lower()

        fields: Dict[str, Any] = {
            "telegram_username": str(telegram_username or ''),
            "first_name": str(first_name or ''),
            "last_name": str(last_name or ''),
        }
        if tg_user_id is not None:
            fields["tg_user_id"] = int(tg_user_id) or None
        if phone:
            fields["phone"] = str(phone)
        if status:
            fields["status"] = str(status)
        if last_login_at:
            fields["last_login_at"] = str(last_login_at)
        if last_error is not None:
            fields["last_error"] = str(last_error)

        if current_source != 'manual':
            tg_name = " ".join(p for p in (str(first_name or '').strip(), str(last_name or '').strip()) if p).strip()
            if not tg_name and telegram_username:
                tg_name = f"@{telegram_username}"
            if not tg_name:
                tg_name = key
            fields["display_name"] = tg_name
            fields["display_name_source"] = 'auto'

        fields["updated_at"] = _now_iso()
        keys = list(fields.keys())
        vals = [fields[k] for k in keys]
        sql = "UPDATE managers SET " + ", ".join(f"{k}=?" for k in keys) + " WHERE manager_key=?"
        vals.append(key)
        await db.execute(sql, tuple(vals))
        await db.commit()


async def manager_save_onboarding(owner_user_id: int, *, manager_key: str, step: str, phone: str = '', phone_code_hash: str = '', tmp_session_path: str = '', expires_at: str = '', next_code_allowed_at: str = '', last_code_sent_at: str = '', last_send_error: str = '', proxy_type: str = '', proxy_host: str = '', proxy_port: Optional[int] = None, proxy_username: str = '', proxy_password: str = '', proxy_enabled: int = 0, proxy_bypass_allowed: int = 0, proxy_mode: str = '') -> None:
    now = _now_iso()
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        await db.execute(
            "INSERT INTO manager_onboarding(owner_user_id, manager_key, step, phone, phone_code_hash, tmp_session_path, created_at, expires_at, next_code_allowed_at, last_code_sent_at, last_send_error, proxy_type, proxy_host, proxy_port, proxy_username, proxy_password, proxy_enabled, proxy_bypass_allowed, proxy_mode)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(owner_user_id) DO UPDATE SET manager_key=excluded.manager_key, step=excluded.step, phone=excluded.phone, phone_code_hash=excluded.phone_code_hash, tmp_session_path=excluded.tmp_session_path, expires_at=excluded.expires_at, next_code_allowed_at=excluded.next_code_allowed_at, last_code_sent_at=excluded.last_code_sent_at, last_send_error=excluded.last_send_error,"
            " proxy_type=excluded.proxy_type, proxy_host=excluded.proxy_host, proxy_port=excluded.proxy_port, proxy_username=excluded.proxy_username, proxy_password=excluded.proxy_password, proxy_enabled=excluded.proxy_enabled, proxy_bypass_allowed=excluded.proxy_bypass_allowed, proxy_mode=excluded.proxy_mode",
            (int(owner_user_id), str(manager_key), str(step), str(phone or ''), str(phone_code_hash or ''), str(tmp_session_path or ''), now, str(expires_at or ''), str(next_code_allowed_at or ''), str(last_code_sent_at or ''), str(last_send_error or ''),
             str(proxy_type or ''), str(proxy_host or ''), (int(proxy_port) if proxy_port not in (None, '') else None), str(proxy_username or ''), str(proxy_password or ''), int(proxy_enabled or 0), int(proxy_bypass_allowed or 0), str(proxy_mode or '')),
        )
        await db.commit()


async def manager_get_onboarding(owner_user_id: int) -> Optional[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM manager_onboarding WHERE owner_user_id=?", (int(owner_user_id),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def manager_delete_onboarding(owner_user_id: int) -> None:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        await db.execute("DELETE FROM manager_onboarding WHERE owner_user_id=?", (int(owner_user_id),))
        await db.commit()


async def manager_delete_onboarding_by_key(manager_key: str) -> None:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        await db.execute("DELETE FROM manager_onboarding WHERE manager_key=?", (str(manager_key or ''),))
        await db.commit()


async def manager_list_pending() -> List[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM manager_onboarding ORDER BY created_at ASC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def manager_clear_expired_onboarding(now_iso: Optional[str] = None) -> int:
    now_iso = str(now_iso or _now_iso())
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        cur = await db.execute("DELETE FROM manager_onboarding WHERE COALESCE(expires_at,'') != '' AND expires_at < ?", (now_iso,))
        await db.commit()
        try:
            return int(cur.rowcount or 0)
        except Exception:
            return 0


async def manager_create_auth_session(user_id: int, expires_at: str) -> None:
    now = _now_iso()
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        await db.execute(
            "INSERT INTO manager_auth_sessions(user_id, expires_at, created_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at, created_at=excluded.created_at",
            (int(user_id), str(expires_at), now),
        )
        await db.commit()


async def manager_has_auth_session(user_id: int, now_iso: Optional[str] = None) -> bool:
    now_iso = str(now_iso or _now_iso())
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        cur = await db.execute("SELECT expires_at FROM manager_auth_sessions WHERE user_id=?", (int(user_id),))
        row = await cur.fetchone()
        if not row:
            return False
        expires_at = str(row[0] or '')
        return bool(expires_at and expires_at >= now_iso)


async def manager_delete_auth_session(user_id: int) -> None:
    async with _db_conn(DB_PATH) as db:
        await _manager_table_ready(db)
        await db.execute("DELETE FROM manager_auth_sessions WHERE user_id=?", (int(user_id),))
        await db.commit()



async def _access_tables_ready(db: aiosqlite.Connection) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS access_users(tg_user_id INTEGER PRIMARY KEY, display_name TEXT DEFAULT '', username TEXT DEFAULT '', access_level INTEGER NOT NULL DEFAULT 1, scope_mode TEXT NOT NULL DEFAULT 'selected', is_enabled INTEGER NOT NULL DEFAULT 1, created_by INTEGER, created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '')")
    await db.execute("CREATE TABLE IF NOT EXISTS access_targets(tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '', PRIMARY KEY(tg_user_id, manager_key))")
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS access_users_enabled_idx ON access_users(is_enabled, access_level)")
    except Exception:
        pass
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS access_targets_user_idx ON access_targets(tg_user_id, manager_key)")
    except Exception:
        pass


async def access_get_user(tg_user_id: int) -> Optional[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM access_users WHERE tg_user_id=?", (int(tg_user_id),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def access_list_users(*, include_disabled: bool = True) -> List[Dict[str, Any]]:
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM access_users"
        if not include_disabled:
            sql += " WHERE COALESCE(is_enabled,0)=1"
        sql += " ORDER BY access_level DESC, tg_user_id ASC"
        cur = await db.execute(sql)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def access_upsert_user(*, tg_user_id: int, access_level: int, scope_mode: str, display_name: str = '', username: str = '', is_enabled: int = 1, created_by: Optional[int] = None) -> None:
    now = _now_iso()
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        await db.execute(
            "INSERT INTO access_users(tg_user_id, display_name, username, access_level, scope_mode, is_enabled, created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(tg_user_id) DO UPDATE SET display_name=excluded.display_name, username=excluded.username, access_level=excluded.access_level, scope_mode=excluded.scope_mode, is_enabled=excluded.is_enabled, updated_at=excluded.updated_at",
            (int(tg_user_id), str(display_name or ''), str(username or ''), int(access_level), str(scope_mode or 'selected'), int(is_enabled or 0), created_by, now, now),
        )
        await db.commit()


async def access_set_user_fields(tg_user_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields['updated_at'] = _now_iso()
    keys = list(fields.keys())
    vals = [fields[k] for k in keys]
    sql = "UPDATE access_users SET " + ", ".join([f"{k}=?" for k in keys]) + " WHERE tg_user_id=?"
    vals.append(int(tg_user_id))
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        await db.execute(sql, tuple(vals))
        await db.commit()


async def access_remove_user(tg_user_id: int) -> None:
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        await db.execute("DELETE FROM access_targets WHERE tg_user_id=?", (int(tg_user_id),))
        await db.execute("DELETE FROM access_users WHERE tg_user_id=?", (int(tg_user_id),))
        await db.commit()


async def access_list_targets(tg_user_id: int) -> List[str]:
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        cur = await db.execute("SELECT manager_key FROM access_targets WHERE tg_user_id=? ORDER BY manager_key ASC", (int(tg_user_id),))
        rows = await cur.fetchall()
        return [str(r[0] or '') for r in rows if str(r[0] or '').strip()]


async def access_add_target(tg_user_id: int, manager_key: str) -> None:
    now = _now_iso()
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        await db.execute("INSERT OR IGNORE INTO access_targets(tg_user_id, manager_key, created_at) VALUES(?,?,?)", (int(tg_user_id), str(manager_key or '').strip(), now))
        await db.commit()


async def access_remove_target(tg_user_id: int, manager_key: str) -> None:
    async with _db_conn(DB_PATH) as db:
        await _access_tables_ready(db)
        await db.execute("DELETE FROM access_targets WHERE tg_user_id=? AND manager_key=?", (int(tg_user_id), str(manager_key or '').strip()))
        await db.commit()


# -------------------- shared manager command queue (TPilot DB) --------------------

def _queue_now_iso() -> str:
    return _now_iso()


def _queue_db_path(db_path: Optional[str] = None) -> str:
    raw = str(db_path or QUEUE_DB_PATH or DEFAULT_QUEUE_DB_PATH).strip()
    if not raw:
        raw = DEFAULT_QUEUE_DB_PATH
    return raw


async def _manager_queue_ready(db: aiosqlite.Connection) -> None:
    await db.execute(
        "CREATE TABLE IF NOT EXISTS manager_commands("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "nonce TEXT UNIQUE NOT NULL,"
        "target_key TEXT NOT NULL,"
        "command TEXT NOT NULL DEFAULT '',"
        "args TEXT DEFAULT '',"
        "payload_json TEXT DEFAULT '',"
        "created_by TEXT DEFAULT '',"
        "status TEXT NOT NULL DEFAULT 'new',"
        "created_at TEXT NOT NULL DEFAULT '',"
        "available_at TEXT DEFAULT '',"
        "expires_at TEXT DEFAULT '',"
        "taken_at TEXT DEFAULT '',"
        "started_at TEXT DEFAULT '',"
        "finished_at TEXT DEFAULT '',"
        "worker_key TEXT DEFAULT '',"
        "result_ok INTEGER,"
        "result_text TEXT DEFAULT '',"
        "result_json TEXT DEFAULT '',"
        "error_text TEXT DEFAULT ''"
        ")"
    )
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS manager_commands_target_status_idx ON manager_commands(target_key, status, available_at, created_at)")
    except Exception:
        pass
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS manager_commands_nonce_idx ON manager_commands(nonce)")
    except Exception:
        pass
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS manager_commands_status_expires_idx ON manager_commands(status, expires_at)")
    except Exception:
        pass


def _queue_make_nonce() -> str:
    import os as _os
    import time as _time
    return f"{int(_time.time() * 1000)}:{int(_os.getpid())}:{int(_time.monotonic_ns() % 1000000)}"


async def manager_queue_put(
    *,
    target_key: str,
    command: str,
    args: str = "",
    payload_json: str = "",
    created_by: str = "",
    nonce: Optional[str] = None,
    available_at: str = "",
    expires_at: str = "",
    db_path: Optional[str] = None,
) -> str:
    qdb = _queue_db_path(db_path)
    os.makedirs(os.path.dirname(qdb), exist_ok=True)
    nonce_v = str(nonce or _queue_make_nonce())
    created_at = _queue_now_iso()
    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        await db.execute(
            "INSERT INTO manager_commands(nonce, target_key, command, args, payload_json, created_by, status, created_at, available_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                nonce_v,
                str(target_key or "").strip(),
                str(command or "").strip(),
                str(args or ""),
                str(payload_json or ""),
                str(created_by or ""),
                "new",
                created_at,
                str(available_at or ""),
                str(expires_at or ""),
            ),
        )
        await db.commit()
    return nonce_v


async def manager_queue_take_next(
    manager_key: str,
    *,
    stale_after_sec: int = 30,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    qdb = _queue_db_path(db_path)
    os.makedirs(os.path.dirname(qdb), exist_ok=True)
    key = str(manager_key or "").strip()
    now_iso = _queue_now_iso()
    stale_cutoff = (_utc_now() - timedelta(seconds=max(1, int(stale_after_sec or 30)))).replace(microsecond=0).isoformat()

    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=5000;")
        await db.execute("BEGIN IMMEDIATE")
        try:
            cur = await db.execute(
                "\n".join([
                    "SELECT * FROM manager_commands",
                    "WHERE (target_key=? OR target_key='all')",
                    "  AND (",
                    "       (status='new' AND (COALESCE(available_at,'')='' OR available_at<=?))",
                    "    OR (status='processing' AND COALESCE(started_at,'')<>'' AND started_at<=?)",
                    "  )",
                    "  AND (COALESCE(expires_at,'')='' OR expires_at>?)",
                    "ORDER BY",
                    "  CASE WHEN target_key='all' THEN 1 ELSE 0 END ASC,",
                    "  created_at ASC,",
                    "  id ASC",
                    "LIMIT 1",
                ]),
                (key, now_iso, stale_cutoff, now_iso),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return None

            cmd_id = int(row["id"])
            await db.execute(
                "UPDATE manager_commands SET status='processing', worker_key=?, taken_at=?, started_at=? WHERE id=?",
                (key, now_iso, now_iso, cmd_id),
            )
            await db.commit()
            cur2 = await db.execute("SELECT * FROM manager_commands WHERE id=?", (cmd_id,))
            row2 = await cur2.fetchone()
            return dict(row2) if row2 else dict(row)
        except Exception:
            await db.rollback()
            raise


async def manager_queue_finish(
    nonce: str,
    *,
    worker_key: str,
    ok: bool,
    result_text: str = "",
    result_json: str = "",
    error_text: str = "",
    db_path: Optional[str] = None,
) -> None:
    qdb = _queue_db_path(db_path)
    finished_at = _queue_now_iso()
    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        await db.execute(
            "UPDATE manager_commands SET status=?, worker_key=?, finished_at=?, result_ok=?, result_text=?, result_json=?, error_text=? WHERE nonce=?",
            (
                "done" if bool(ok) else "error",
                str(worker_key or ""),
                finished_at,
                1 if bool(ok) else 0,
                str(result_text or ""),
                str(result_json or ""),
                str(error_text or ""),
                str(nonce or ""),
            ),
        )
        await db.commit()


async def manager_queue_get(nonce: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    qdb = _queue_db_path(db_path)
    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM manager_commands WHERE nonce=?", (str(nonce or ""),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def manager_queue_list_ready(
    manager_key: str,
    *,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    qdb = _queue_db_path(db_path)
    key = str(manager_key or "").strip()
    now_iso = _queue_now_iso()
    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "\n".join([
                "SELECT * FROM manager_commands",
                "WHERE (target_key=? OR target_key='all')",
                "  AND status='new'",
                "  AND (COALESCE(available_at,'')='' OR available_at<=?)",
                "  AND (COALESCE(expires_at,'')='' OR expires_at>?)",
                "ORDER BY created_at ASC, id ASC",
                "LIMIT ?",
            ]),
            (key, now_iso, now_iso, int(limit or 50)),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def manager_queue_mark_timeouts(*, now_iso: Optional[str] = None, db_path: Optional[str] = None) -> int:
    qdb = _queue_db_path(db_path)
    now_v = str(now_iso or _queue_now_iso())
    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        cur = await db.execute(
            "UPDATE manager_commands SET status='timeout', finished_at=?, error_text=CASE WHEN COALESCE(error_text,'')='' THEN 'timeout' ELSE error_text END WHERE status IN ('new','processing') AND COALESCE(expires_at,'')<>'' AND expires_at<=?",
            (now_v, now_v),
        )
        await db.commit()
        try:
            return int(cur.rowcount or 0)
        except Exception:
            return 0


async def manager_queue_finalize_all_for_manager(
    manager_key: str, *, error_text: str = "rollback", db_path: Optional[str] = None,
) -> int:
    """TPILOT C AUTOMATIC ROLLBACK 20260718: finalizes every still-pending
    (new/processing) command targeting manager_key to 'timeout' regardless of
    expires_at -- used when a replacement rollback abandons an incomplete
    new manager so its queue can never be silently consumed later (e.g. if
    the operator manually restarts that process). Idempotent: a manager with
    no pending commands is a no-op (rowcount 0)."""
    qdb = _queue_db_path(db_path)
    now_v = _queue_now_iso()
    key = str(manager_key or "").strip()
    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        cur = await db.execute(
            "UPDATE manager_commands SET status='timeout', finished_at=?,"
            " error_text=CASE WHEN COALESCE(error_text,'')='' THEN ? ELSE error_text END"
            " WHERE target_key=? AND status IN ('new','processing')",
            (now_v, str(error_text or "rollback"), key),
        )
        await db.commit()
        try:
            return int(cur.rowcount or 0)
        except Exception:
            return 0


async def manager_queue_cleanup_finished(
    *,
    older_than_sec: int = 86400,
    db_path: Optional[str] = None,
) -> int:
    qdb = _queue_db_path(db_path)
    cutoff = (_utc_now() - timedelta(seconds=max(60, int(older_than_sec or 86400)))).replace(microsecond=0).isoformat()
    async with _db_conn(qdb) as db:
        await _manager_queue_ready(db)
        cur = await db.execute(
            "DELETE FROM manager_commands WHERE status IN ('done','error','timeout') AND COALESCE(finished_at,'')<>'' AND finished_at<?",
            (cutoff,),
        )
        await db.commit()
        try:
            return int(cur.rowcount or 0)
        except Exception:
            return 0


# --- TPILOT M2.13A BIZLINK STORAGE 20260609 START ---
import sqlite3 as _bsl_sqlite3

_BIZLINK_MAX_TEXT_LEN: int = 1024
_BIZLINK_SLOT_COUNT: int = 15
BIZLINKS_DELAY_BETWEEN_LINKS_SEC: int = 10    # seconds between link creates per manager (M2.13B+)
BIZLINKS_DELAY_BETWEEN_MANAGERS_SEC: int = 90  # seconds between managers (M2.13B+)

_BIZLINK_DEFAULT_TEXTS: List[str] = [
    "Привет! Я менеджер проекта. Напишите мне, чтобы начать.",
    "Добро пожаловать! Готов ответить на ваши вопросы.",
    "Здравствуйте! Чем могу помочь?",
    "Привет! Оставьте заявку, и я свяжусь с вами.",
    "Добрый день! Напишите, что вас интересует.",
    "Привет! Я здесь, чтобы помочь вам.",
    "Здравствуйте! Расскажите, чем могу быть полезен.",
    "Привет! Готов обсудить детали.",
    "Добро пожаловать! Напишите ваш вопрос.",
    "Здравствуйте! Рад помочь вам.",
    "Привет! Напишите мне — отвечу быстро.",
    "Добрый день! Оставьте сообщение, перезвоню.",
    "Привет! Жду вашего сообщения.",
    "Здравствуйте! Напишите для консультации.",
    "Привет! Я онлайн. Напишите мне.",
]


def _bsl_db_path(db_path: Optional[str] = None) -> str:
    return str(db_path or QUEUE_DB_PATH)


def _bsl_connect(db_path: Optional[str] = None) -> _bsl_sqlite3.Connection:
    path = _bsl_db_path(db_path)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = _bsl_sqlite3.connect(path, timeout=30)
    con.row_factory = _bsl_sqlite3.Row
    if path not in _DB_WAL_DONE:
        # Mark first: a locked/failing PRAGMA must not turn into a retry storm.
        _DB_WAL_DONE.add(path)
        try:
            con.execute("PRAGMA journal_mode=WAL;")
            # perf (patch perf_conn): NORMAL drops the per-commit fsync that
            # FULL forces. Safe under WAL -- a crash can lose the last commits
            # but never corrupts the DB file.
            con.execute("PRAGMA synchronous=NORMAL;")
        except Exception:
            pass
    return con


def ensure_bizlink_tables(db_path: Optional[str] = None) -> None:
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bizlink_templates(
                slot_no INTEGER PRIMARY KEY CHECK(slot_no BETWEEN 1 AND 15),
                message_text TEXT NOT NULL DEFAULT '',
                title_prefix TEXT NOT NULL DEFAULT 'TPilot',
                is_active INTEGER NOT NULL DEFAULT 1,
                updated_by INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bizlinks(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT NOT NULL,
                target_date TEXT NOT NULL,
                slot_no INTEGER NOT NULL CHECK(slot_no BETWEEN 1 AND 15),
                title TEXT NOT NULL DEFAULT '',
                message_text TEXT NOT NULL DEFAULT '',
                link_url TEXT NOT NULL DEFAULT '',
                slug TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                last_error_class TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                flood_wait_seconds INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS bizlinks_mgr_date_slot ON bizlinks(manager_key, target_date, slot_no)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS bizlinks_mgr_created ON bizlinks(manager_key, created_at)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS bizlinks_mgr_slug ON bizlinks(manager_key, slug)"
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS bizlinks_date_mgr ON bizlinks(target_date, manager_key)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bizlink_template_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_no INTEGER NOT NULL,
                old_text TEXT NOT NULL DEFAULT '',
                new_text TEXT NOT NULL DEFAULT '',
                updated_by INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS bizlink_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_key TEXT NOT NULL UNIQUE,
                manager_key TEXT NOT NULL DEFAULT '',
                target_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_by TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL DEFAULT '',
                finished_at TEXT NOT NULL DEFAULT '',
                last_error_class TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.commit()
        # Seed exactly 15 template slots — INSERT OR IGNORE preserves existing edits
        now = _now_iso()
        for i, default_text in enumerate(_BIZLINK_DEFAULT_TEXTS, start=1):
            con.execute(
                """
                INSERT OR IGNORE INTO bizlink_templates
                    (slot_no, message_text, title_prefix, is_active, updated_by, updated_at)
                VALUES (?, ?, 'TPilot', 1, 0, ?)
                """,
                (i, default_text, now),
            )
        con.commit()
    finally:
        con.close()


def bizlink_template_list(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_bizlink_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute("SELECT * FROM bizlink_templates ORDER BY slot_no")
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def bizlink_template_get(slot_no: int, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    ensure_bizlink_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute("SELECT * FROM bizlink_templates WHERE slot_no=?", (int(slot_no),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def bizlink_template_validate(slot_no: int, text: str) -> Tuple[bool, str]:
    n = int(slot_no or 0)
    if n < 1 or n > _BIZLINK_SLOT_COUNT:
        return False, f"Номер слота должен быть от 1 до {_BIZLINK_SLOT_COUNT}."
    cleaned = str(text or "").strip()
    if not cleaned:
        return False, "Текст не может быть пустым."
    if len(cleaned) > _BIZLINK_MAX_TEXT_LEN:
        return False, f"Текст слишком длинный. Максимум {_BIZLINK_MAX_TEXT_LEN} символов."
    return True, ""


def bizlink_template_set(
    slot_no: int,
    new_text: str,
    updated_by: int = 0,
    db_path: Optional[str] = None,
) -> Tuple[bool, str]:
    ok, err = bizlink_template_validate(slot_no, new_text)
    if not ok:
        return False, err
    cleaned = str(new_text or "").strip()
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "SELECT message_text FROM bizlink_templates WHERE slot_no=?", (int(slot_no),)
        )
        row = cur.fetchone()
        old_text = str(row["message_text"]) if row else ""
        con.execute(
            "UPDATE bizlink_templates SET message_text=?, updated_by=?, updated_at=? WHERE slot_no=?",
            (cleaned, int(updated_by or 0), now, int(slot_no)),
        )
        con.execute(
            "INSERT INTO bizlink_template_audit(slot_no, old_text, new_text, updated_by, updated_at) VALUES(?,?,?,?,?)",
            (int(slot_no), old_text, cleaned, int(updated_by or 0), now),
        )
        con.commit()
        return True, ""
    except Exception as exc:
        try:
            con.rollback()
        except Exception:
            pass
        return False, str(exc)
    finally:
        con.close()


def bizlinks_list_for_date(
    target_date: str,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    ensure_bizlink_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "SELECT * FROM bizlinks WHERE target_date=? ORDER BY manager_key, slot_no",
            (str(target_date or ""),),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def bizlinks_list_for_manager_date(
    manager_key: str,
    target_date: str,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    ensure_bizlink_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "SELECT * FROM bizlinks WHERE manager_key=? AND target_date=? ORDER BY slot_no",
            (str(manager_key or ""), str(target_date or "")),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def bizlink_job_create_or_get(
    manager_key: str,
    target_date: str,
    *,
    created_by: str = "",
    created_by_user_id: int = 0,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    ensure_bizlink_tables(db_path)
    mk = str(manager_key or "").strip()
    td = str(target_date or "").strip()
    job_key = f"bizlink:{mk}:{td}"
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            INSERT OR IGNORE INTO bizlink_jobs
                (job_key, manager_key, target_date, status, created_by, created_by_user_id, created_at)
            VALUES (?, ?, ?, 'queued', ?, ?, ?)
            """,
            (job_key, mk, td, str(created_by or ""), int(created_by_user_id or 0), now),
        )
        con.commit()
        cur = con.execute("SELECT * FROM bizlink_jobs WHERE job_key=?", (job_key,))
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def bizlink_job_list_for_date(
    target_date: str,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    ensure_bizlink_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "SELECT * FROM bizlink_jobs WHERE target_date=? ORDER BY manager_key",
            (str(target_date or ""),),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()

# --- TPILOT M2.13A BIZLINK STORAGE 20260609 END ---

# --- TPILOT M2.13B BIZLINK CREATE STORAGE 20260609 START ---


def _bsl_ensure_views_column(db_path: Optional[str] = None) -> None:
    """Add views column to bizlinks if it does not exist (M2.13B migration)."""
    con = _bsl_connect(db_path)
    try:
        cols = [str(r[1]) for r in con.execute("PRAGMA table_info(bizlinks)").fetchall()]
        if "views" not in cols:
            con.execute("ALTER TABLE bizlinks ADD COLUMN views INTEGER NOT NULL DEFAULT 0")
            con.commit()
    except Exception:
        pass
    finally:
        con.close()


def bizlink_get_one(
    manager_key: str,
    target_date: str,
    slot_no: int,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Return the single bizlinks row for (manager_key, target_date, slot_no) or None."""
    ensure_bizlink_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "SELECT * FROM bizlinks WHERE manager_key=? AND target_date=? AND slot_no=?",
            (str(manager_key or ""), str(target_date or ""), int(slot_no)),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def bizlink_upsert_pending(
    manager_key: str,
    target_date: str,
    slot_no: int,
    title: str,
    message_text: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """INSERT OR IGNORE a pending bizlinks row; return the current row."""
    ensure_bizlink_tables(db_path)
    now = _utc_now().replace(microsecond=0).isoformat()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            INSERT OR IGNORE INTO bizlinks(
                manager_key, target_date, slot_no, title, message_text,
                status, created_at, updated_at
            ) VALUES(?,?,?,?,?,'pending',?,?)
            """,
            (
                str(manager_key or ""),
                str(target_date or ""),
                int(slot_no),
                str(title or ""),
                str(message_text or ""),
                now,
                now,
            ),
        )
        con.commit()
        cur = con.execute(
            "SELECT * FROM bizlinks WHERE manager_key=? AND target_date=? AND slot_no=?",
            (str(manager_key or ""), str(target_date or ""), int(slot_no)),
        )
        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def bizlink_mark_created(
    manager_key: str,
    target_date: str,
    slot_no: int,
    *,
    link_url: str,
    slug: str,
    title: str,
    message_text: str,
    views: int = 0,
    db_path: Optional[str] = None,
) -> None:
    """UPDATE bizlinks row to status='created' with link details.

    Also clears any stale soft-delete markers (deleted_at/deleted_by_user_id/
    delete_error) left by a prior deletion of this same (manager_key,
    target_date, slot_no) row -- otherwise a legitimately recreated link would
    keep a non-empty deleted_at forever (status='created' + deleted_at set),
    which downstream "is this link active" checks must never treat as active.
    Bug fix (2026-07-07): future writes only, no data migration.
    """
    ensure_bizlink_tables(db_path)
    ensure_bizlink_delete_tables(db_path)  # guarantees deleted_at/deleted_by_user_id/delete_error exist
    _bsl_ensure_views_column(db_path)
    now = _utc_now().replace(microsecond=0).isoformat()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            UPDATE bizlinks
               SET status='created',
                   link_url=?,
                   slug=?,
                   title=?,
                   message_text=?,
                   views=?,
                   last_error_class='',
                   last_error='',
                   flood_wait_seconds=0,
                   deleted_at='',
                   deleted_by_user_id=0,
                   delete_error='',
                   updated_at=?
             WHERE manager_key=? AND target_date=? AND slot_no=?
            """,
            (
                str(link_url or ""),
                str(slug or ""),
                str(title or ""),
                str(message_text or ""),
                int(views or 0),
                now,
                str(manager_key or ""),
                str(target_date or ""),
                int(slot_no),
            ),
        )
        con.commit()
    finally:
        con.close()


def bizlink_mark_failed(
    manager_key: str,
    target_date: str,
    slot_no: int,
    *,
    last_error_class: str,
    last_error: str,
    flood_wait_seconds: int = 0,
    db_path: Optional[str] = None,
) -> None:
    """UPDATE bizlinks row to status='failed' with error details."""
    ensure_bizlink_tables(db_path)
    now = _utc_now().replace(microsecond=0).isoformat()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            UPDATE bizlinks
               SET status='failed',
                   last_error_class=?,
                   last_error=?,
                   flood_wait_seconds=?,
                   updated_at=?
             WHERE manager_key=? AND target_date=? AND slot_no=?
            """,
            (
                str(last_error_class or ""),
                str(last_error or ""),
                int(flood_wait_seconds or 0),
                now,
                str(manager_key or ""),
                str(target_date or ""),
                int(slot_no),
            ),
        )
        con.commit()
    finally:
        con.close()


# --- TPILOT M2.13D-1 WORK SCHEDULE STORAGE 20260609 START ---
# manager_work_schedule_days: explicit per-day work schedule set by managers/admins.
# IMPORTANT: DISTINCT from main.py manager_work_days (presence/manual-out tracker).
#   manager_work_days (main.py): NO row = manager IS working (presence semantics).
#   manager_work_schedule_days: NO row = day off (OPPOSITE semantics).
# Only is_working=1 means "working". Missing row and is_working=0 both mean "day off".


def ensure_manager_schedule_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Create manager_work_schedule_days and manager_work_schedule_audit if missing."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS manager_work_schedule_days(
                manager_key        TEXT NOT NULL,
                work_date          TEXT NOT NULL,
                is_working         INTEGER NOT NULL,
                source             TEXT NOT NULL DEFAULT '',
                updated_by_user_id INTEGER,
                updated_by_role    TEXT,
                updated_at         TEXT,
                PRIMARY KEY(manager_key, work_date)
            );
            CREATE INDEX IF NOT EXISTS mwsd_date_idx
                ON manager_work_schedule_days(work_date, manager_key);
            CREATE TABLE IF NOT EXISTS manager_work_schedule_audit(
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key        TEXT NOT NULL,
                work_date          TEXT NOT NULL,
                old_is_working     INTEGER,
                new_is_working     INTEGER,
                source             TEXT NOT NULL DEFAULT '',
                updated_by_user_id INTEGER,
                updated_by_role    TEXT,
                updated_at         TEXT NOT NULL DEFAULT ''
            );
        """)
        con.commit()
    finally:
        con.close()


def manager_schedule_get_month(manager_key, year, month, db_path=None):
    # type: (str, int, int, Optional[str]) -> Dict[int, int]
    """Return {day_int: is_working_int} for stored rows in the given month.
    Days without a row are not included; caller treats missing as is_working=0."""
    con = _bsl_connect(db_path)
    try:
        prefix = "{:04d}-{:02d}-".format(int(year), int(month))
        rows = con.execute(
            "SELECT work_date, is_working FROM manager_work_schedule_days "
            "WHERE manager_key=? AND work_date LIKE ? ORDER BY work_date ASC",
            (str(manager_key or ""), prefix + "%"),
        ).fetchall()
        result = {}
        for r in rows:
            wdate = str(r[0] or "")
            if len(wdate) >= 10:
                try:
                    day = int(wdate[8:10])
                    result[day] = int(r[1] or 0)
                except Exception:
                    pass
        return result
    finally:
        con.close()


def manager_schedule_set_day(
    manager_key, work_date, is_working, *,
    source, updated_by_user_id, updated_by_role, db_path=None
):
    """Upsert one day. Toggling off writes is_working=0 (never deletes row). Reads old value for audit."""
    now = _now_iso()
    mk = str(manager_key or "")
    wd = str(work_date or "")
    iw = int(is_working)
    uid = updated_by_user_id
    role = str(updated_by_role or "")
    src = str(source or "")
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT is_working FROM manager_work_schedule_days WHERE manager_key=? AND work_date=?",
            (mk, wd),
        ).fetchone()
        old_iw = int(row[0]) if row is not None else None
        con.execute(
            "INSERT OR REPLACE INTO manager_work_schedule_days"
            "(manager_key, work_date, is_working, source, updated_by_user_id, updated_by_role, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (mk, wd, iw, src, uid, role, now),
        )
        con.execute(
            "INSERT INTO manager_work_schedule_audit"
            "(manager_key, work_date, old_is_working, new_is_working, source, updated_by_user_id, updated_by_role, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (mk, wd, old_iw, iw, src, uid, role, now),
        )
        con.commit()
    finally:
        con.close()


def manager_schedule_is_working(manager_key, work_date, db_path=None):
    """Return True when explicitly working, or (TPILOT PATCH C1) inherited from an
    enabled source weekly schedule when no explicit row exists. Delegates to
    manager_effective_is_working (defined below, TPILOT PATCH C1 block); when no source
    schedule is enabled this is byte-identical to the original explicit-row-only check."""
    return manager_effective_is_working(manager_key, work_date, db_path=db_path)


def manager_schedule_list_working_on_date(work_date, db_path=None):
    """Return sorted list of manager_key values working on the given date: explicit
    is_working=1 rows, plus (TPILOT PATCH C1) managers inheriting from an enabled source
    weekly schedule when they have no explicit row for this date. An explicit row (ON or
    OFF) always wins over inheritance. With no enabled source schedule this returns
    exactly the original explicit-only result.

    [W3.3-A] Candidates (managers with an explicit row for this date, union managers
    with a non-empty source link -- deliberately broader than the legacy query, since
    the tier logic below does the filtering) are each resolved through the clock-free
    C1 entry point (_w3_c1_is_working) on ONE shared connection. A failing candidate
    degrades to _manager_effective_is_working_row on the SAME connection and the loop
    always continues -- one failure never truncates, empties or aborts the list. A
    failure enumerating the candidate set, ensuring the required tables, or an
    unparseable/empty date degrades to the single verbatim legacy body,
    _c1_legacy_list_working_on_date -- see the W3.3 plan freeze S9.2."""
    wd = str(work_date or "").strip()
    if not wd:
        return []
    try:
        datetime.strptime(wd, "%Y-%m-%d")
    except Exception:
        return _c1_legacy_list_working_on_date(wd, db_path)

    con = None
    try:
        ensure_manager_schedule_tables(db_path)
        ensure_source_work_schedule(db_path)
        con = _bsl_connect(db_path)
        _w3_c1_ensure_validated(con, db_path)
        candidate_rows = con.execute(
            "SELECT manager_key FROM manager_work_schedule_days WHERE work_date=? "
            "UNION "
            "SELECT manager_key FROM manager_source_links WHERE COALESCE(source_key,'') <> ''",
            (wd,),
        ).fetchall()
        candidates = sorted({str(r[0] or "") for r in candidate_rows if r[0]})

        result = set()
        for mk in candidates:
            try:
                ok, _tier, _prov = _w3_c1_is_working(con, mk, wd)
            except Exception:
                try:
                    ok = _manager_effective_is_working_row(con, mk, wd)
                except Exception:
                    ok = False
            if ok:
                result.add(mk)
        return sorted(result)
    except Exception:
        return _c1_legacy_list_working_on_date(wd, db_path)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass


def manager_schedule_prev_working_day(manager_key, before_date, lookback_days=31, db_path=None):
    """Return ISO date (YYYY-MM-DD) of the most recent working day strictly before
    `before_date`, within `lookback_days`. Returns None when none found.

    Read-only. Uses manager_effective_is_working per day (TPILOT PATCH C1: explicit row,
    else enabled source weekly inheritance, else OFF) so inherited working days are
    found too. Walks backward day-by-day, bounded by lookback_days (no unbounded scan).
    Used by the unified stats engine to compute schedule-aware dolyoty (carry across
    off-days to the next working day).
    """
    try:
        bd = str(before_date or "").strip()
        if not bd:
            return None
        before_dt = datetime.strptime(bd, "%Y-%m-%d").date()
    except Exception:
        return None
    mk = str(manager_key or "").strip()
    if not mk:
        return None
    lb = int(lookback_days or 0)
    if lb <= 0:
        return None
    ensure_manager_schedule_tables(db_path)
    ensure_source_work_schedule(db_path)
    con = _bsl_connect(db_path)
    try:
        for offset in range(1, lb + 1):
            day_iso = (before_dt - timedelta(days=offset)).isoformat()
            if _manager_effective_is_working_row(con, mk, day_iso):
                return day_iso
        return None
    finally:
        con.close()


def manager_schedule_count_for_month(manager_key, year, month, db_path=None):
    """Return count of is_working=1 days in the given month."""
    con = _bsl_connect(db_path)
    try:
        prefix = "{:04d}-{:02d}-".format(int(year), int(month))
        row = con.execute(
            "SELECT COUNT(*) FROM manager_work_schedule_days "
            "WHERE manager_key=? AND work_date LIKE ? AND is_working=1",
            (str(manager_key or ""), prefix + "%"),
        ).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()

# --- TPILOT M2.13D-1 WORK SCHEDULE STORAGE 20260609 END ---


# --- TPILOT STAGE C1 SOURCE WORK WINDOWS 20260621 START ---
# Per-source day work window (Kyiv minutes-of-day). The dolyot/night window is the
# exact complement, derived by the stats engine from the same config so day+night
# tile with no overlap/gap. Absence of a row (or enabled=0) means the engine keeps
# its hardcoded default (08:00-17:00 day / 17:00-08:00 night) => byte-identical
# current behavior. Read-only from the engine's perspective; only the operator UI
# writes via source_work_window_set. Lives in the central TPILOT DB next to
# manager_work_schedule_days (mirrors that feature's storage pattern).

# Default Kyiv day window as minutes-from-midnight: 08:00 and 17:00.
SOURCE_WINDOW_DEFAULT_DAY_START = 8 * 60   # 480
SOURCE_WINDOW_DEFAULT_DAY_END = 17 * 60    # 1020

# Source-level period-mode defaults (C-add-1). These reproduce current PartnerBot
# behavior exactly: Light defaults to calendar 00:00-00:00 (no dolyoty), Pro defaults
# to the work window + flights (dolyoty on). Report type itself stays buyer-scoped;
# only the period behavior is source-scoped.
SOURCE_WINDOW_DEFAULT_LIGHT_INCLUDE_DOLYOTY = 0
SOURCE_WINDOW_DEFAULT_PRO_INCLUDE_DOLYOTY = 1


def _swc_table_columns(con, table):
    # type: (Any, str) -> set
    """Return the set of column names for a table (empty set if it does not exist)."""
    try:
        rows = con.execute("PRAGMA table_info({})".format(table)).fetchall()
        return {str(r[1]) for r in rows}
    except Exception:
        return set()


def ensure_source_work_windows(db_path=None):
    # type: (Optional[str]) -> None
    """Create source_work_windows and source_work_windows_audit if missing.

    Additive migration only: fresh installs get the period-mode flag columns directly;
    pre-existing tables (from the first C1 deploy) gain them via guarded ALTER TABLE
    ADD COLUMN. SQLite applies the NOT NULL DEFAULT to existing rows, so no backfill is
    needed and existing rows keep current behavior (light=0, pro=1).
    """
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS source_work_windows(
                source_key            TEXT PRIMARY KEY,
                day_start             INTEGER NOT NULL,
                day_end               INTEGER NOT NULL,
                timezone              TEXT NOT NULL DEFAULT 'Europe/Kyiv',
                enabled               INTEGER NOT NULL DEFAULT 1,
                light_include_dolyoty INTEGER NOT NULL DEFAULT 0,
                pro_include_dolyoty   INTEGER NOT NULL DEFAULT 1,
                updated_by_user_id    INTEGER,
                updated_at            TEXT
            );
            CREATE TABLE IF NOT EXISTS source_work_windows_audit(
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key         TEXT NOT NULL,
                old_day_start      INTEGER,
                old_day_end        INTEGER,
                old_enabled        INTEGER,
                old_light_include_dolyoty INTEGER,
                old_pro_include_dolyoty   INTEGER,
                new_day_start      INTEGER,
                new_day_end        INTEGER,
                new_enabled        INTEGER,
                new_light_include_dolyoty INTEGER,
                new_pro_include_dolyoty   INTEGER,
                updated_by_user_id INTEGER,
                updated_at         TEXT NOT NULL DEFAULT ''
            );
        """)
        # Guarded additive migration for tables created by the first C1 deploy.
        main_cols = _swc_table_columns(con, "source_work_windows")
        if "light_include_dolyoty" not in main_cols:
            con.execute(
                "ALTER TABLE source_work_windows "
                "ADD COLUMN light_include_dolyoty INTEGER NOT NULL DEFAULT 0"
            )
        if "pro_include_dolyoty" not in main_cols:
            con.execute(
                "ALTER TABLE source_work_windows "
                "ADD COLUMN pro_include_dolyoty INTEGER NOT NULL DEFAULT 1"
            )
        audit_cols = _swc_table_columns(con, "source_work_windows_audit")
        for col in (
            "old_light_include_dolyoty", "old_pro_include_dolyoty",
            "new_light_include_dolyoty", "new_pro_include_dolyoty",
        ):
            if col not in audit_cols:
                con.execute(
                    "ALTER TABLE source_work_windows_audit ADD COLUMN {} INTEGER".format(col)
                )
        con.commit()
    finally:
        con.close()


def source_work_window_get(source_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    """Return the enabled window config for source_key, else None.

    Returns None when there is no row, when enabled=0, or on any error (fail-open:
    the caller must treat None as "use default 08:00-17:00, light=0, pro=1"). Read-only.
    Result dict: {source_key, day_start, day_end, timezone, enabled,
                  light_include_dolyoty, pro_include_dolyoty, updated_at}.
    """
    sk = str(source_key or "").strip()
    if not sk:
        return None
    try:
        ensure_source_work_windows(db_path)
        con = _bsl_connect(db_path)
        try:
            row = con.execute(
                "SELECT source_key, day_start, day_end, timezone, enabled, "
                "light_include_dolyoty, pro_include_dolyoty, updated_at "
                "FROM source_work_windows WHERE source_key=?",
                (sk,),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            return None
        if int(row[4] or 0) != 1:
            return None
        day_start = int(row[1])
        day_end = int(row[2])
        # Defensive: ignore any malformed/overnight row that slipped past the writer.
        if not (0 <= day_start < day_end <= 1439):
            return None
        return {
            "source_key": str(row[0] or ""),
            "day_start": day_start,
            "day_end": day_end,
            "timezone": str(row[3] or "Europe/Kyiv"),
            "enabled": 1,
            "light_include_dolyoty": 1 if int(row[5] or 0) == 1 else 0,
            "pro_include_dolyoty": 1 if int(row[6] or 0) == 1 else 0,
            "updated_at": str(row[7] or ""),
        }
    except Exception:
        return None


def source_work_window_set(
    source_key, day_start, day_end, *,
    light_include_dolyoty=None, pro_include_dolyoty=None,
    updated_by_user_id=None, enabled=1, db_path=None
):
    # type: (str, int, int, ...) -> None
    """Upsert one source's day window (Kyiv minutes-of-day) with an audit row.

    Validates 0 <= day_start < day_end <= 1439 (intra-day only; overnight day windows
    are rejected). Raises ValueError on invalid input or empty source_key.

    Period-mode flags are optional: when light_include_dolyoty / pro_include_dolyoty are
    None, the existing row's values are preserved (or the defaults 0 / 1 when no row
    exists yet). When provided they are normalized to 0/1.
    """
    sk = str(source_key or "").strip()
    if not sk:
        raise ValueError("source_key is required")
    try:
        ds = int(day_start)
        de = int(day_end)
    except (TypeError, ValueError):
        raise ValueError("day_start and day_end must be integers (minutes of day)")
    if not (0 <= ds <= 1439 and 0 <= de <= 1439):
        raise ValueError("day_start/day_end must be within 0..1439 minutes")
    if ds >= de:
        raise ValueError("day_start must be strictly before day_end (overnight day windows are not allowed)")
    en = 1 if int(enabled or 0) == 1 else 0
    now = _now_iso()
    ensure_source_work_windows(db_path)
    con = _bsl_connect(db_path)
    try:
        old = con.execute(
            "SELECT day_start, day_end, enabled, light_include_dolyoty, pro_include_dolyoty "
            "FROM source_work_windows WHERE source_key=?",
            (sk,),
        ).fetchone()
        old_ds = int(old[0]) if old is not None else None
        old_de = int(old[1]) if old is not None else None
        old_en = int(old[2]) if old is not None else None
        old_light = int(old[3]) if old is not None else None
        old_pro = int(old[4]) if old is not None else None
        # Preserve existing flags when caller did not pass an explicit value; fall back
        # to the period-mode defaults for a brand-new row.
        if light_include_dolyoty is None:
            light = old_light if old_light is not None else SOURCE_WINDOW_DEFAULT_LIGHT_INCLUDE_DOLYOTY
        else:
            light = 1 if int(light_include_dolyoty) == 1 else 0
        if pro_include_dolyoty is None:
            pro = old_pro if old_pro is not None else SOURCE_WINDOW_DEFAULT_PRO_INCLUDE_DOLYOTY
        else:
            pro = 1 if int(pro_include_dolyoty) == 1 else 0
        con.execute(
            "INSERT OR REPLACE INTO source_work_windows"
            "(source_key, day_start, day_end, timezone, enabled,"
            " light_include_dolyoty, pro_include_dolyoty, updated_by_user_id, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (sk, ds, de, "Europe/Kyiv", en, light, pro, updated_by_user_id, now),
        )
        con.execute(
            "INSERT INTO source_work_windows_audit"
            "(source_key, old_day_start, old_day_end, old_enabled,"
            " old_light_include_dolyoty, old_pro_include_dolyoty,"
            " new_day_start, new_day_end, new_enabled,"
            " new_light_include_dolyoty, new_pro_include_dolyoty,"
            " updated_by_user_id, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sk, old_ds, old_de, old_en, old_light, old_pro,
             ds, de, en, light, pro, updated_by_user_id, now),
        )
        con.commit()
    finally:
        con.close()

# --- TPILOT STAGE C1 SOURCE WORK WINDOWS 20260621 END ---


# --- TPILOT PATCH C1 SOURCE SCHEDULE INHERITANCE 20260622 START ---
# Manager working DAYS (which weekday a manager works) inherit from a per-source weekly
# pattern by default, while an explicit manager_work_schedule_days row (ON or OFF) always
# wins. source_work_schedule.enabled=0 (the default, and any missing row) means the source
# is "not configured" -> inheritance is inert and every reader below returns exactly the
# pre-patch explicit-row-only result. Inheritance only engages once an admin explicitly
# enables a source's weekly schedule. No backfill of existing manager rows.
#
# manager_source_links lives in the same central DB (created by main.py's
# _ensure_sources_groups_tables); the CREATE TABLE IF NOT EXISTS below mirrors that exact
# schema defensively so the resolver's join works even if this module runs first.

_SWS_WEEKDAY_COLUMNS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index = date.weekday()


def ensure_source_work_schedule(db_path=None):
    # type: (Optional[str]) -> None
    """Create source_work_schedule (and defensively manager_source_links) if missing."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS manager_source_links(
                manager_key TEXT PRIMARY KEY,
                source_key TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS source_work_schedule(
                source_key         TEXT PRIMARY KEY,
                mon                INTEGER NOT NULL DEFAULT 1,
                tue                INTEGER NOT NULL DEFAULT 1,
                wed                INTEGER NOT NULL DEFAULT 1,
                thu                INTEGER NOT NULL DEFAULT 1,
                fri                INTEGER NOT NULL DEFAULT 1,
                sat                INTEGER NOT NULL DEFAULT 1,
                sun                INTEGER NOT NULL DEFAULT 1,
                enabled            INTEGER NOT NULL DEFAULT 0,
                updated_by_user_id INTEGER,
                updated_at         TEXT
            );
        """)
        con.commit()
    finally:
        con.close()


def source_work_schedule_get(source_key, db_path=None, include_disabled=False):
    # type: (str, Optional[str], bool) -> Optional[Dict[str, Any]]
    """Return the source's weekly schedule dict, or None when missing/disabled
    (unless include_disabled=True, used by the future PanelBot editor to show the
    current config even while off)."""
    sk = str(source_key or "").strip()
    if not sk:
        return None
    ensure_source_work_schedule(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT source_key, mon, tue, wed, thu, fri, sat, sun, enabled, "
            "updated_by_user_id, updated_at FROM source_work_schedule WHERE source_key=?",
            (sk,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    enabled = 1 if int(row[8] or 0) == 1 else 0
    if not enabled and not include_disabled:
        return None
    return {
        "source_key": str(row[0] or ""),
        "mon": int(row[1] or 0), "tue": int(row[2] or 0), "wed": int(row[3] or 0),
        "thu": int(row[4] or 0), "fri": int(row[5] or 0), "sat": int(row[6] or 0),
        "sun": int(row[7] or 0),
        "enabled": enabled,
        "updated_by_user_id": row[9],
        "updated_at": str(row[10] or ""),
    }


def source_work_schedule_set(
    source_key, *, mon=None, tue=None, wed=None, thu=None, fri=None, sat=None, sun=None,
    enabled=None, updated_by_user_id=None, db_path=None
):
    # type: (str, ...) -> None
    """Upsert one source's weekly working-day pattern. Any weekday flag or `enabled`
    left as None preserves the existing row's value (or the all-working/disabled default
    for a brand-new row), mirroring source_work_window_set's partial-update style."""
    sk = str(source_key or "").strip()
    if not sk:
        raise ValueError("source_key is required")
    ensure_source_work_schedule(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        old = con.execute(
            "SELECT mon, tue, wed, thu, fri, sat, sun, enabled "
            "FROM source_work_schedule WHERE source_key=?",
            (sk,),
        ).fetchone()
        defaults = tuple(old) if old is not None else (1, 1, 1, 1, 1, 1, 1, 0)

        def _flag(value, idx):
            if value is None:
                return int(defaults[idx] or 0)
            return 1 if int(value) == 1 else 0

        vals = [
            _flag(mon, 0), _flag(tue, 1), _flag(wed, 2), _flag(thu, 3),
            _flag(fri, 4), _flag(sat, 5), _flag(sun, 6), _flag(enabled, 7),
        ]
        con.execute(
            "INSERT OR REPLACE INTO source_work_schedule"
            "(source_key, mon, tue, wed, thu, fri, sat, sun, enabled, updated_by_user_id, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (sk, vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], vals[6], vals[7],
             updated_by_user_id, now),
        )
        con.commit()
    finally:
        con.close()


def _manager_effective_is_working_row(con, mk, wd):
    # type: (Any, str, str) -> bool
    """Resolve effective is_working for (mk, wd) using an already-open connection.
    Internal helper shared by manager_effective_is_working and the prev_working_day walk
    so a backward day-by-day scan does not reopen a connection per day."""
    row = con.execute(
        "SELECT is_working FROM manager_work_schedule_days WHERE manager_key=? AND work_date=?",
        (mk, wd),
    ).fetchone()
    if row is not None:
        return int(row[0] or 0) == 1
    link = con.execute(
        "SELECT source_key FROM manager_source_links WHERE manager_key=? AND COALESCE(source_key,'')<>''",
        (mk,),
    ).fetchone()
    if not link:
        return False
    sk = str(link[0] or "").strip()
    if not sk:
        return False
    sched = con.execute(
        "SELECT mon, tue, wed, thu, fri, sat, sun, enabled FROM source_work_schedule WHERE source_key=?",
        (sk,),
    ).fetchone()
    if sched is None or int(sched[7] or 0) != 1:
        return False
    try:
        weekday_idx = datetime.strptime(wd, "%Y-%m-%d").date().weekday()
    except Exception:
        return False
    return int(sched[weekday_idx] or 0) == 1


def manager_effective_is_working(manager_key, work_date, db_path=None):
    # type: (str, str, Optional[str]) -> bool
    """Resolve whether a manager works on a date: explicit manager_work_schedule_days
    row wins (ON or OFF); else an enabled source_work_schedule weekday flag via
    manager_source_links; else False (today's safe default when nothing is configured).

    [W3.3-A] The decision itself routes through the clock-free C1 tier resolver
    (_w3_c1_is_working) on ONE shared connection, guarded by the presence-validated
    ensure-memo (_w3_c1_ensure_validated). Any resolver/ensure failure at that stage
    degrades to the pre-W3 row lookup (_manager_effective_is_working_row) on the SAME
    connection -- never a second connection, never a raised exception, never a silent
    True. See the W3.3 plan freeze S5.10/S9.1."""
    mk = str(manager_key or "").strip()
    wd = str(work_date or "").strip()
    if not mk or not wd:
        return False
    ensure_manager_schedule_tables(db_path)
    ensure_source_work_schedule(db_path)
    con = _bsl_connect(db_path)
    try:
        try:
            _w3_c1_ensure_validated(con, db_path)
            is_working, _tier, _prov = _w3_c1_is_working(con, mk, wd)
            return bool(is_working)
        except Exception:
            return _manager_effective_is_working_row(con, mk, wd)
    finally:
        con.close()


def manager_schedule_clear_day(manager_key, work_date, *, updated_by_user_id=None, updated_by_role=None, db_path=None):
    # type: (str, str, ...) -> None
    """Delete the explicit override row for (manager_key, work_date), reverting that day
    to inherited (source schedule, or the OFF default if uninherited). No-op if there is
    no explicit row. Logs an audit row using the existing manager_work_schedule_audit
    table so the revert-to-inherit action stays traceable like any other schedule edit."""
    mk = str(manager_key or "").strip()
    wd = str(work_date or "").strip()
    if not mk or not wd:
        return
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT is_working FROM manager_work_schedule_days WHERE manager_key=? AND work_date=?",
            (mk, wd),
        ).fetchone()
        if row is None:
            return
        old_iw = int(row[0] or 0)
        con.execute(
            "DELETE FROM manager_work_schedule_days WHERE manager_key=? AND work_date=?",
            (mk, wd),
        )
        con.execute(
            "INSERT INTO manager_work_schedule_audit"
            "(manager_key, work_date, old_is_working, new_is_working, source, updated_by_user_id, updated_by_role, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (mk, wd, old_iw, None, "cleared_to_inherit", updated_by_user_id, str(updated_by_role or ""), now),
        )
        con.commit()
    finally:
        con.close()

# --- TPILOT PATCH C1 SOURCE SCHEDULE INHERITANCE 20260622 END ---


# --- TPILOT PATCH C2 SOURCE MESSAGE SCHEDULE (WORK TIME) 20260623 START ---
# Source-level day/night client-message TIME windows for the greeting/away
# ("Приветствие / нет на месте") system. A manager inherits these windows from its linked
# source when source_message_schedule.enabled=1 AND the manager has no explicit per-manager
# override (manager_client_message_schedule, a main.py-owned table). The inheritance resolver
# itself lives in main.py's _tp_gq_get_schedule (Patch C2c); THIS C2a step only adds the
# source-level storage table + helpers and is completely inert on its own.
#
# enabled=0 (the default, and any missing row) means the source's time schedule is "not
# configured" -> no inheritance. This is independent of source_work_schedule.enabled (C1 work
# DAYS): a source can inherit days, times, both, or neither. Times default to the standard
# 08:00-17:00 day / 17:00-08:00 night, matching main.py's TP_GQ defaults. No backfill; existing
# per-manager manager_client_message_schedule rows are untouched and keep winning as overrides.
#
# source_message_schedule mirrors the per-manager manager_client_message_schedule shape but
# keyed by source_key (same central DB, same source_key form as source_work_schedule).

_SMS_TIME_DEFAULTS = ("08:00", "17:00", "17:00", "08:00")  # day_start, day_end, night_start, night_end


def ensure_source_message_schedule(db_path=None):
    # type: (Optional[str]) -> None
    """Create source_message_schedule if missing. Idempotent; additive; never destructive."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS source_message_schedule(
                source_key         TEXT PRIMARY KEY,
                day_start          TEXT NOT NULL DEFAULT '08:00',
                day_end            TEXT NOT NULL DEFAULT '17:00',
                night_start        TEXT NOT NULL DEFAULT '17:00',
                night_end          TEXT NOT NULL DEFAULT '08:00',
                enabled            INTEGER NOT NULL DEFAULT 0,
                updated_by_user_id INTEGER,
                updated_at         TEXT
            );
        """)
        con.commit()
    finally:
        con.close()


def source_message_schedule_get(source_key, db_path=None, include_disabled=False):
    # type: (str, Optional[str], bool) -> Optional[Dict[str, Any]]
    """Return the source's day/night message-time schedule dict, or None when missing/disabled
    (unless include_disabled=True, used by the future PanelBot editor to show the current config
    even while inheritance is off). Mirrors source_work_schedule_get's contract."""
    sk = str(source_key or "").strip()
    if not sk:
        return None
    ensure_source_message_schedule(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT source_key, day_start, day_end, night_start, night_end, enabled, "
            "updated_by_user_id, updated_at FROM source_message_schedule WHERE source_key=?",
            (sk,),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    enabled = 1 if int(row[5] or 0) == 1 else 0
    if not enabled and not include_disabled:
        return None
    return {
        "source_key": str(row[0] or ""),
        "day_start": str(row[1] or _SMS_TIME_DEFAULTS[0]),
        "day_end": str(row[2] or _SMS_TIME_DEFAULTS[1]),
        "night_start": str(row[3] or _SMS_TIME_DEFAULTS[2]),
        "night_end": str(row[4] or _SMS_TIME_DEFAULTS[3]),
        "enabled": enabled,
        "updated_by_user_id": row[6],
        "updated_at": str(row[7] or ""),
    }


def source_message_schedule_set(
    source_key, *, day_start=None, day_end=None, night_start=None, night_end=None,
    enabled=None, updated_by_user_id=None, db_path=None
):
    # type: (str, ...) -> None
    """Upsert one source's day/night message-time windows. Any time field or `enabled` left as
    None preserves the existing row's value (or the standard 08:00-17:00 / 17:00-08:00, disabled
    default for a brand-new row), mirroring source_work_schedule_set's partial-update style.
    Returns None (write-only), like source_work_schedule_set / source_work_window_set."""
    sk = str(source_key or "").strip()
    if not sk:
        raise ValueError("source_key is required")
    ensure_source_message_schedule(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        old = con.execute(
            "SELECT day_start, day_end, night_start, night_end, enabled "
            "FROM source_message_schedule WHERE source_key=?",
            (sk,),
        ).fetchone()
        # defaults for a brand-new row: standard windows, disabled.
        defaults = tuple(old) if old is not None else (
            _SMS_TIME_DEFAULTS[0], _SMS_TIME_DEFAULTS[1], _SMS_TIME_DEFAULTS[2], _SMS_TIME_DEFAULTS[3], 0
        )

        def _time(value, idx):
            if value is None:
                return str(defaults[idx] or _SMS_TIME_DEFAULTS[idx])
            return str(value)

        new_day_start = _time(day_start, 0)
        new_day_end = _time(day_end, 1)
        new_night_start = _time(night_start, 2)
        new_night_end = _time(night_end, 3)
        if enabled is None:
            new_enabled = int(defaults[4] or 0)
        else:
            new_enabled = 1 if int(enabled) == 1 else 0

        con.execute(
            "INSERT OR REPLACE INTO source_message_schedule"
            "(source_key, day_start, day_end, night_start, night_end, enabled, updated_by_user_id, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (sk, new_day_start, new_day_end, new_night_start, new_night_end, new_enabled,
             updated_by_user_id, now),
        )
        con.commit()
    finally:
        con.close()

# --- TPILOT PATCH C2 SOURCE MESSAGE SCHEDULE (WORK TIME) 20260623 END ---


# --- TPILOT PATCH C3 SCHEDULE CHANGE REQUESTS 20260623 START ---
# Managers may change ONLY today's work-day directly in ManagerBot. Tomorrow/future changes become
# admin approval requests stored here. THIS C3a step only adds the table + helpers and is completely
# inert on its own: no runtime reader consults this table until C3b (ManagerBot gating) and C3c
# (PanelBot push/inbox approval) are wired. Approval (C3c) applies the change through the EXISTING
# manager_schedule_set_day, so C1 day-inheritance and explicit-override precedence are unchanged.
#
# Scope is ON/OFF only (requested_is_working 0/1). "Revert to inherited" stays an admin-only action.
# At most one pending row per (manager_key, work_date), enforced by a partial unique index; a re-tap
# upserts that pending row instead of duplicating. work_date is 'YYYY-MM-DD'. manager_key is stored
# in the same normalized form ManagerBot/manager_work_schedule_days already use (caller-normalized).

_MSCR_COLUMNS = (
    "id", "manager_key", "work_date", "requested_is_working", "effective_at_request",
    "source_key", "inherited_at_request", "status", "requested_by_user_id", "created_at",
    "notified", "decided_by_user_id", "decided_at", "note",
)


def ensure_schedule_request_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Create manager_schedule_change_requests + its partial-unique pending index if missing.
    Idempotent; additive; never destructive."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS manager_schedule_change_requests(
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key          TEXT NOT NULL,
                work_date            TEXT NOT NULL,
                requested_is_working INTEGER NOT NULL,
                effective_at_request INTEGER,
                source_key           TEXT,
                inherited_at_request INTEGER,
                status               TEXT NOT NULL DEFAULT 'pending',
                requested_by_user_id INTEGER,
                created_at           TEXT NOT NULL,
                notified             INTEGER NOT NULL DEFAULT 0,
                decided_by_user_id   INTEGER,
                decided_at           TEXT,
                note                 TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_mscr_pending
                ON manager_schedule_change_requests(manager_key, work_date)
                WHERE status='pending';
            CREATE INDEX IF NOT EXISTS ix_mscr_status_notified
                ON manager_schedule_change_requests(status, notified);
        """)
        con.commit()
    finally:
        con.close()


def _mscr_row_to_dict(row):
    # type: (Any) -> Dict[str, Any]
    return {k: row[k] for k in _MSCR_COLUMNS}


def schedule_request_create(
    manager_key, work_date, requested_is_working, *,
    requested_by_user_id=None, effective_at_request=None, source_key=None,
    inherited_at_request=None, db_path=None
):
    # type: (str, str, Any, ...) -> Optional[Dict[str, Any]]
    """Upsert the single pending change-request for (manager_key, work_date).

    If a pending row already exists for that manager+date, UPDATE it in place (new
    requested_is_working / context / requested_by_user_id, created_at refreshed, notified reset to 0)
    so a re-tap never produces a duplicate. Otherwise INSERT a new pending row. Returns the
    created/updated request row as a dict (or None if manager_key/work_date is blank)."""
    mk = str(manager_key or "").strip()
    wd = str(work_date or "").strip()
    if not mk or not wd:
        return None
    riw = 1 if int(requested_is_working) == 1 else 0
    eff = None if effective_at_request is None else (1 if int(effective_at_request) == 1 else 0)
    inh = None if inherited_at_request is None else (1 if int(inherited_at_request) == 1 else 0)
    sk = None if source_key is None else str(source_key)
    now = _now_iso()
    ensure_schedule_request_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        existing = con.execute(
            "SELECT id FROM manager_schedule_change_requests "
            "WHERE manager_key=? AND work_date=? AND status='pending'",
            (mk, wd),
        ).fetchone()
        if existing is not None:
            req_id = int(existing["id"])
            con.execute(
                "UPDATE manager_schedule_change_requests SET "
                "requested_is_working=?, effective_at_request=?, source_key=?, "
                "inherited_at_request=?, requested_by_user_id=?, created_at=?, notified=0 "
                "WHERE id=?",
                (riw, eff, sk, inh, requested_by_user_id, now, req_id),
            )
        else:
            cur = con.execute(
                "INSERT INTO manager_schedule_change_requests"
                "(manager_key, work_date, requested_is_working, effective_at_request, source_key, "
                "inherited_at_request, status, requested_by_user_id, created_at, notified) "
                "VALUES(?,?,?,?,?,?,'pending',?,?,0)",
                (mk, wd, riw, eff, sk, inh, requested_by_user_id, now),
            )
            req_id = int(cur.lastrowid)
        con.commit()
        row = con.execute(
            "SELECT * FROM manager_schedule_change_requests WHERE id=?", (req_id,)
        ).fetchone()
        return _mscr_row_to_dict(row) if row is not None else None
    finally:
        con.close()


def schedule_request_get(request_id, db_path=None):
    # type: (Any, Optional[str]) -> Optional[Dict[str, Any]]
    """Return the request row as a dict, or None when not found."""
    try:
        rid = int(request_id)
    except Exception:
        return None
    ensure_schedule_request_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM manager_schedule_change_requests WHERE id=?", (rid,)
        ).fetchone()
        return _mscr_row_to_dict(row) if row is not None else None
    finally:
        con.close()


def schedule_request_list_pending(manager_key=None, month=None, notified=None, db_path=None):
    # type: (Optional[str], Optional[str], Optional[Any], Optional[str]) -> List[Dict[str, Any]]
    """List pending requests, optionally filtered by manager_key (exact), month ('YYYY-MM' prefix on
    work_date), and notified (0/1). Ordered by work_date, created_at, id."""
    ensure_schedule_request_tables(db_path)
    where = ["status='pending'"]
    params = []  # type: List[Any]
    mk = str(manager_key or "").strip()
    if mk:
        where.append("manager_key=?")
        params.append(mk)
    mo = str(month or "").strip()
    if mo:
        where.append("work_date LIKE ?")
        params.append(mo + "%")
    if notified is not None:
        where.append("notified=?")
        params.append(1 if int(notified) == 1 else 0)
    sql = (
        "SELECT * FROM manager_schedule_change_requests WHERE "
        + " AND ".join(where)
        + " ORDER BY work_date ASC, created_at ASC, id ASC"
    )
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(sql, tuple(params)).fetchall()
        return [_mscr_row_to_dict(r) for r in rows or []]
    finally:
        con.close()


def schedule_request_mark_notified(request_id, db_path=None):
    # type: (Any, Optional[str]) -> bool
    """Mark a pending request as notified (notified=1). Returns True if a pending row was updated."""
    try:
        rid = int(request_id)
    except Exception:
        return False
    ensure_schedule_request_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_schedule_change_requests SET notified=1 "
            "WHERE id=? AND status='pending'",
            (rid,),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    finally:
        con.close()


def schedule_request_decide(request_id, status, decided_by_user_id, *, db_path=None):
    # type: (Any, str, Any, ...) -> bool
    """Decide a PENDING request: set status to 'approved' | 'rejected' | 'withdrawn' plus
    decided_at / decided_by_user_id. No-op (returns False) if the request is missing, not pending,
    or status is not allowed. Returns True only when a pending row was transitioned."""
    try:
        rid = int(request_id)
    except Exception:
        return False
    st = str(status or "").strip().lower()
    if st not in ("approved", "rejected", "withdrawn"):
        return False
    now = _now_iso()
    ensure_schedule_request_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_schedule_change_requests SET status=?, decided_at=?, decided_by_user_id=? "
            "WHERE id=? AND status='pending'",
            (st, now, decided_by_user_id, rid),
        )
        con.commit()
        return int(cur.rowcount or 0) > 0
    finally:
        con.close()

# --- TPILOT PATCH C3 SCHEDULE CHANGE REQUESTS 20260623 END ---


# --- TPILOT STAGE D R1 RESERVE MANAGER PAIRS 20260621 START ---
# manager_reserve_pairs: admin-managed primary<->reserve account pairing (metadata only).
# reserve_activation_events: cross-process request queue/audit for a future partner-
# triggered activation flow (consumed by a controller loop that is NOT part of R1).
# R1 is purely additive: no existing table/row is read or modified by these helpers.


def ensure_reserve_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Create manager_reserve_pairs and reserve_activation_events if missing. Idempotent."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS manager_reserve_pairs(
                reserve_key        TEXT PRIMARY KEY,
                primary_key        TEXT NOT NULL,
                source_key         TEXT NOT NULL DEFAULT '',
                status             TEXT NOT NULL DEFAULT 'linked',
                created_by_user_id INTEGER,
                created_at         TEXT,
                updated_at         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_mrp_primary ON manager_reserve_pairs(primary_key);
            CREATE INDEX IF NOT EXISTS ix_mrp_source ON manager_reserve_pairs(source_key);
            CREATE TABLE IF NOT EXISTS reserve_activation_events(
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key            TEXT NOT NULL,
                primary_key           TEXT NOT NULL,
                reserve_key           TEXT NOT NULL,
                target_date           TEXT NOT NULL,
                requested_by_user_id  INTEGER NOT NULL,
                response_chat_id      INTEGER NOT NULL DEFAULT 0,
                status                TEXT NOT NULL DEFAULT 'requested',
                batch_job_key         TEXT DEFAULT '',
                last_error            TEXT DEFAULT '',
                created_at            TEXT,
                updated_at            TEXT,
                UNIQUE(reserve_key, target_date)
            );
            CREATE INDEX IF NOT EXISTS ix_rae_status ON reserve_activation_events(status);
            CREATE INDEX IF NOT EXISTS ix_rae_src_date ON reserve_activation_events(source_key, target_date);
        """)
        con.commit()
    finally:
        con.close()


def _rsv_norm_key(raw):
    # type: (Any) -> str
    """Normalize a key using the same plain strip() convention as the rest of storage.py
    (no casefold here; callers that need full manager-key normalization, e.g. panel_bot.py,
    apply manager_registry.normalize_manager_key before calling these helpers)."""
    return str(raw or "").strip()


def reserve_pair_set(primary_key, reserve_key, source_key="", *, created_by_user_id=None, db_path=None):
    # type: (str, str, str, ..., Optional[str]) -> Dict[str, Any]
    """Upsert reserve_key as the unique reserve paired to primary_key (reserve_key is the
    PK, so re-pairing an existing reserve moves it). Always sets status='linked', even if
    the row previously existed or was retired."""
    pk = _rsv_norm_key(primary_key)
    rk = _rsv_norm_key(reserve_key)
    sk = _rsv_norm_key(source_key)
    if not pk or not rk:
        raise ValueError("primary_key and reserve_key are required")
    if pk == rk:
        raise ValueError("reserve_key must differ from primary_key")
    ensure_reserve_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        existing = con.execute(
            "SELECT created_at FROM manager_reserve_pairs WHERE reserve_key=?", (rk,)
        ).fetchone()
        created_at = str(existing[0]) if existing and existing[0] else now
        con.execute(
            "INSERT OR REPLACE INTO manager_reserve_pairs"
            "(reserve_key, primary_key, source_key, status, created_by_user_id, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (rk, pk, sk, "linked", created_by_user_id, created_at, now),
        )
        con.commit()
    finally:
        con.close()
    return reserve_pair_get_by_reserve(rk, db_path=db_path) or {}


def reserve_pair_get_by_reserve(reserve_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    rk = _rsv_norm_key(reserve_key)
    if not rk:
        return None
    ensure_reserve_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM manager_reserve_pairs WHERE reserve_key=?", (rk,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def reserve_pair_list_for_primary(primary_key, db_path=None):
    # type: (str, Optional[str]) -> List[Dict[str, Any]]
    pk = _rsv_norm_key(primary_key)
    if not pk:
        return []
    ensure_reserve_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM manager_reserve_pairs WHERE primary_key=? ORDER BY reserve_key ASC",
            (pk,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def reserve_pair_list_for_source(source_key, db_path=None):
    # type: (str, Optional[str]) -> List[Dict[str, Any]]
    sk = _rsv_norm_key(source_key)
    if not sk:
        return []
    ensure_reserve_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM manager_reserve_pairs WHERE source_key=? ORDER BY reserve_key ASC",
            (sk,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def reserve_pair_retire(reserve_key, *, db_path=None):
    # type: (str, ..., Optional[str]) -> bool
    """Set status='retired'. Returns False when no such reserve row exists."""
    rk = _rsv_norm_key(reserve_key)
    if not rk:
        return False
    ensure_reserve_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_reserve_pairs SET status='retired', updated_at=? WHERE reserve_key=?",
            (now, rk),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def reserve_pair_release_for_primary(primary_key, *, db_path=None):
    # type: (str, ..., Optional[str]) -> int
    """RESERVE RELEASE 2026-07-11: retire every 'linked' pair whose primary_key matches.

    Called when a primary manager is fully deleted, so its reserve accounts become
    reusable instead of staying invisibly locked to a nonexistent primary (the picker
    excludes reserves by status='linked'). Metadata-only, mirrors reserve_pair_retire:
    never deletes rows, never touches sessions or any other table. Idempotent -- a
    repeat call matches 0 rows. Caller normalizes the key (same convention as the
    other reserve_pair_* helpers). Returns the number of pairs released."""
    pk = _rsv_norm_key(primary_key)
    if not pk:
        return 0
    ensure_reserve_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_reserve_pairs SET status='retired', updated_at=?"
            " WHERE primary_key=? AND status='linked'",
            (now, pk),
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


# --- TPILOT DELETED MANAGER STATS RETENTION 20260711 START ---
# manager_stats_tombstones: identity + per-manager-DB-path snapshot captured at
# full-delete time (_panel_manager_delete_full_command in main.py), so AdminBot /
# PartnerBot stats can still render a hard-deleted manager's historical identity and
# daily_leads for a bounded retention window (default 60 days) after deletion.
# Metadata-only, additive, lazy (no import-time DB mutation). Never resurrects the
# manager as active: operational manager lists (manager_list_rows / picker / onboarding)
# never read this table -- only the stats-reporting enumerators do.


def manager_stats_tombstone_ready(db_path=None):
    # type: (Optional[str]) -> None
    """Create manager_stats_tombstones if missing. Idempotent."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS manager_stats_tombstones(
                manager_key       TEXT PRIMARY KEY,
                display_name      TEXT NOT NULL DEFAULT '',
                telegram_username TEXT NOT NULL DEFAULT '',
                db_path           TEXT NOT NULL DEFAULT '',
                source_key        TEXT NOT NULL DEFAULT '',
                deleted_at        TEXT NOT NULL DEFAULT '',
                retention_until   TEXT NOT NULL DEFAULT '',
                created_at        TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_mst_retention ON manager_stats_tombstones(retention_until);
        """)
        con.commit()
    finally:
        con.close()


def manager_stats_tombstone_upsert(
    manager_key,
    *,
    display_name="",
    telegram_username="",
    manager_db_path="",
    source_key="",
    deleted_at=None,
    retention_days=60,
    db_path=None,
):
    # type: (str, ..., Optional[str]) -> bool
    """Upsert a tombstone snapshot for a manager being hard-deleted.

    `manager_db_path` is the manager's OWN per-manager DB path (the backup location
    the deletion moves runtime/managers/<key>/ into) -- stored in the `db_path` COLUMN.
    `db_path` (the keyword) is the CONNECTION target for THIS storage call, same
    convention as every other reserve_pair_*/ensure_* helper in this file (defaults to
    the module DB_PATH family via _bsl_connect); it is never confused with the stored
    column because the two are named differently at the Python call-site on purpose.

    deleted_at defaults to now (UTC ISO) when not given. retention_until is computed
    once at upsert time as deleted_at + retention_days (default 60) and stored, so
    later reads never need to recompute it. Additive/idempotent: INSERT OR REPLACE
    keyed on manager_key; created_at is preserved across repeat upserts for the same
    key. Returns True on success, False on invalid input (missing manager_key)."""
    key = _rsv_norm_key(manager_key)
    if not key:
        return False
    manager_stats_tombstone_ready(db_path)
    now = _now_iso()
    deleted_at_s = str(deleted_at or now)
    try:
        deleted_dt = datetime.fromisoformat(deleted_at_s)
    except Exception:
        deleted_dt = _utc_now()
        deleted_at_s = deleted_dt.replace(microsecond=0).isoformat()
    retention_until_s = (deleted_dt + timedelta(days=int(retention_days or 60))).replace(microsecond=0).isoformat()
    con = _bsl_connect(db_path)
    try:
        existing = con.execute(
            "SELECT created_at FROM manager_stats_tombstones WHERE manager_key=?", (key,)
        ).fetchone()
        created_at = str(existing[0]) if existing and existing[0] else now
        con.execute(
            "INSERT OR REPLACE INTO manager_stats_tombstones"
            "(manager_key, display_name, telegram_username, db_path, source_key,"
            " deleted_at, retention_until, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                key, str(display_name or ""), str(telegram_username or ""), str(manager_db_path or ""),
                str(source_key or ""), deleted_at_s, retention_until_s, created_at, now,
            ),
        )
        con.commit()
        return True
    finally:
        con.close()


def manager_stats_tombstone_get(manager_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    key = _rsv_norm_key(manager_key)
    if not key:
        return None
    manager_stats_tombstone_ready(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute("SELECT * FROM manager_stats_tombstones WHERE manager_key=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def manager_stats_tombstone_list_active(now=None, retention_days=60, db_path=None):
    # type: (Optional[str], int, Optional[str]) -> List[Dict[str, Any]]
    """Return tombstone rows still within their retention window: retention_until >= now
    (plain ISO-string comparison, safe because both sides are always zero-padded
    YYYY-MM-DDTHH:MM:SS UTC). Rows with an empty retention_until (should never happen
    via manager_stats_tombstone_upsert, tolerated only for forward safety) fall back to
    deleted_at + retention_days computed at read time; an unparseable deleted_at is
    treated as already-expired (fail closed -- never over-includes)."""
    manager_stats_tombstone_ready(db_path)
    now_s = str(now or _now_iso())
    con = _bsl_connect(db_path)
    try:
        rows = con.execute("SELECT * FROM manager_stats_tombstones ORDER BY manager_key ASC").fetchall()
        out = []
        for r in rows or []:
            d = dict(r)
            until = str(d.get("retention_until") or "")
            if not until:
                try:
                    deleted_dt = datetime.fromisoformat(str(d.get("deleted_at") or ""))
                    until = (deleted_dt + timedelta(days=int(retention_days or 60))).replace(microsecond=0).isoformat()
                except Exception:
                    until = ""  # unparseable -> stays '' -> excluded below (fail closed)
            if until and until >= now_s:
                out.append(d)
        return out
    finally:
        con.close()


def manager_stats_tombstone_purge_expired(now=None, db_path=None):
    # type: (Optional[str], Optional[str]) -> int
    """Delete tombstone METADATA rows whose retention window has passed
    (retention_until < now). Never touches the backup runtime folder or per-manager
    DB file on disk -- purging the filesystem backup is explicitly out of scope for
    this stage. Idempotent: a repeat call after the first purge matches 0 rows."""
    manager_stats_tombstone_ready(db_path)
    now_s = str(now or _now_iso())
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "DELETE FROM manager_stats_tombstones WHERE retention_until<>'' AND retention_until<?",
            (now_s,),
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()
# --- TPILOT DELETED MANAGER STATS RETENTION 20260711 END ---


def reserve_activation_request(
    source_key, primary_key, reserve_key, target_date, requested_by_user_id,
    response_chat_id=0, db_path=None,
):
    # type: (str, str, str, str, int, int, Optional[str]) -> Dict[str, Any]
    """Insert a 'requested' activation event. Idempotent on UNIQUE(reserve_key, target_date):
    when an event already exists for that reserve/day with an active status
    (requested/creating/done), returns the existing row unchanged instead of raising or
    duplicating. STAGE D R3C: if the existing row was 'cancelled' (a prior deactivation),
    it is revived back to 'requested' (same id, fresh requester/chat/timestamps, error
    cleared) so re-activation works despite the UNIQUE constraint."""
    sk = _rsv_norm_key(source_key)
    pk = _rsv_norm_key(primary_key)
    rk = _rsv_norm_key(reserve_key)
    td = str(target_date or "").strip()
    if not (pk and rk and td):
        raise ValueError("primary_key, reserve_key and target_date are required")
    ensure_reserve_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO reserve_activation_events"
            "(source_key, primary_key, reserve_key, target_date, requested_by_user_id,"
            " response_chat_id, status, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,'requested',?,?)",
            (sk, pk, rk, td, int(requested_by_user_id or 0), int(response_chat_id or 0), now, now),
        )
        con.execute(
            "UPDATE reserve_activation_events SET status='requested', requested_by_user_id=?,"
            " response_chat_id=?, last_error='', updated_at=?"
            " WHERE reserve_key=? AND target_date=? AND status='cancelled'",
            (int(requested_by_user_id or 0), int(response_chat_id or 0), now, rk, td),
        )
        con.commit()
        row = con.execute(
            "SELECT * FROM reserve_activation_events WHERE reserve_key=? AND target_date=?",
            (rk, td),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def reserve_activation_take_next(db_path=None):
    # type: (Optional[str]) -> Optional[Dict[str, Any]]
    """Atomically claim one 'requested' event, marking it 'creating'. Returns None when
    none are pending. Uses BEGIN IMMEDIATE to serialize concurrent claims (mirrors the
    manager_queue_take_next claim pattern)."""
    ensure_reserve_tables(db_path)
    con = _bsl_connect(db_path)
    tx = None
    try:
        tx = _p5_begin_immediate_sync(
            con, source="storage.py", function="reserve_activation_take_next", db_path=db_path,
        )
        try:
            row = con.execute(
                "SELECT * FROM reserve_activation_events WHERE status='requested' "
                "ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                _p5_rollback_sync(tx, con)
                return None
            event_id = int(row["id"])
            now = _now_iso()
            con.execute(
                "UPDATE reserve_activation_events SET status='creating', updated_at=? "
                "WHERE id=? AND status='requested'",
                (now, event_id),
            )
            _p5_commit_sync(tx, con)
        except Exception as _p5_exc:
            try:
                _p5_rollback_sync(tx, con, error=_p5_exc)
            except Exception:
                pass
            raise
        row2 = con.execute(
            "SELECT * FROM reserve_activation_events WHERE id=?", (event_id,)
        ).fetchone()
        return dict(row2) if row2 else None
    finally:
        con.close()


def reserve_activation_finish(event_id, status, *, batch_job_key="", last_error="", db_path=None):
    # type: (int, str, ..., Optional[str]) -> bool
    """Mark an event 'done' or 'failed' with optional job key/error. Returns False when
    no such event id exists OR when it is no longer 'creating' (e.g. STAGE D R3C: the
    partner cancelled it mid-batch). This guard prevents a late finish() call from
    overwriting a 'cancelled' status and resurrecting a deactivated reserve."""
    st = str(status or "").strip()
    if st not in ("done", "failed"):
        raise ValueError("status must be 'done' or 'failed'")
    ensure_reserve_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE reserve_activation_events SET status=?, batch_job_key=?, last_error=?, updated_at=? "
            "WHERE id=? AND status='creating'",
            (st, str(batch_job_key or ""), str(last_error or ""), now, int(event_id)),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def reserve_activation_cancel(source_key, reserve_key, target_date, *, db_path=None):
    # type: (str, str, str, ..., Optional[str]) -> bool
    """STAGE D R3C: deactivate an in-flight/active reserve activation for a specific
    source/reserve/date by flipping it to 'cancelled'. Source-scoped. Never deletes the
    event row, the reserve pair, or any bizlinks. Returns False when no matching row in
    status requested/creating/done exists (e.g. wrong source, wrong date, already
    cancelled/failed, or never activated)."""
    sk = _rsv_norm_key(source_key)
    rk = _rsv_norm_key(reserve_key)
    td = str(target_date or "").strip()
    if not (sk and rk and td):
        return False
    ensure_reserve_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE reserve_activation_events SET status='cancelled', updated_at=?"
            " WHERE source_key=? AND reserve_key=? AND target_date=?"
            " AND status IN ('requested','creating','done')",
            (now, sk, rk, td),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def reserve_activation_get(event_id, db_path=None):
    # type: (int, Optional[str]) -> Optional[Dict[str, Any]]
    ensure_reserve_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM reserve_activation_events WHERE id=?", (int(event_id),)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()
# --- TPILOT STAGE D R1 RESERVE MANAGER PAIRS 20260621 END ---


# --- TPILOT MANAGER REPLACEMENT STAGE1 20260715 START (fix round 20260715b) ---
# manager_replacements: canonical, durable state machine for a fresh-Telegram-
# authorization account replacement (audit 2026-07-15, corrected per the
# independent Stage 1 review 2026-07-15b). Distinct domain from
# manager_reserve_pairs/reserve_activation_events (temporary/standby accounts) --
# a replacement is a PERMANENT successor account and is never modeled as a
# reserve pair. Additive only: no existing table/column touched.
#
# State machine (forward-only, enforced by _REPLACEMENT_TRANSITIONS below):
#   draft -> auth_phone -> auth_code -> {auth_pass ->} identity_ok -> ready_commit
#   -> committing -> links_pending -> links_ready -> cutover_done -> notified -> done
# Corrected ordering (fix round): the account cutover (cutover_done) MUST
# complete before the operation is considered 'notified' -- PartnerBot's
# success notification must never fire before the full account cutover.
# 'notified' -> 'done' is reachable ONLY through the guarded
# replacement_finalize() below, never through generic replacement_advance() --
# see replacement_finalize's docstring for the completion prerequisites it
# enforces atomically.
# 'cancelled' is reachable only from the pre-commit statuses (draft..ready_commit)
# -- once committing has begun the operation is durable and can only move forward
# or transition to 'failed' (compensating cleanup is the caller's job; storage.py
# only records the terminal status + error_stage/error_text).
# 'failed' is reachable from any non-terminal status.
#
# Concurrency/idempotency, enforced by the schema itself (never left to callers):
#   - ux_repl_active_old: at most ONE non-terminal replacement per old_manager_key.
#   - ux_repl_new_key: at most ONE non-(failed/cancelled) replacement per
#     new_manager_key (once assigned) -- so a 'done' operation's new_manager_key
#     is blocked from reuse PERMANENTLY (it is now a real, live manager), while
#     a 'failed'/'cancelled' operation's reservation is released.
#   - manager_replacements.operation_id UNIQUE: replacement_create() is idempotent
#     on operation_id -- a retry with the same operation_id returns the existing
#     row unchanged instead of raising or duplicating (mirrors
#     reserve_activation_request's idempotent-request pattern above).
#   - replacement_advance()/replacement_cancel()/replacement_fail()/
#     replacement_finalize() are all compare-and-set (UPDATE ... WHERE
#     operation_id=? AND status=? [AND prerequisite checks]), mirroring
#     reserve_activation_finish's WHERE id=? AND status='creating' guard -- a
#     stale/duplicate caller simply gets rowcount=0 (False), never a silent
#     double-transition.

REPLACEMENT_STATUSES = (
    "draft", "auth_phone", "auth_code", "auth_pass", "identity_ok",
    "ready_commit", "committing", "links_pending", "links_ready",
    "cutover_done", "notified", "done", "failed", "cancelled",
)

# Stage 2 fix (2026-07-15): new_tg_user_id (R3) durably records the verified
# Telegram identity once, guarded by ux_repl_new_tgid the same way
# new_manager_key is guarded by ux_repl_new_key -- failed/cancelled release
# the reservation, every other status (incl. done) keeps it blocked.
# proxy_mode/proxy_ref/proxy_confirmed (R2) durably record which proxy ROUTE
# an operation committed to at draft->auth_phone time ('direct' or 'proxy';
# proxy_ref is a non-secret caller-supplied route tag such as
# 'manual'/'pool'/'purchased') -- never the credentials themselves, which
# live only in manager_onboarding's proxy_* scratch columns (same security
# model as its existing phone/phone_code_hash columns).

REPLACEMENT_TERMINAL_STATUSES = frozenset(("done", "failed", "cancelled"))

# Statuses at/after which the account cutover is COMPLETE -- the earliest point
# a (future) PartnerBot notify sweep may consider an operation for delivery.
# Deliberately begins at 'cutover_done', never 'links_ready' -- a success
# message must never be sent before the account cutover itself has finished.
# 'notified' means only "the outbox producer has picked this up at least
# once" (a global, monotonic publish-side signal) -- per-user read state is
# tracked separately in replacement_notify_ack, never here.
REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES = ("cutover_done", "notified", "done")

_REPLACEMENT_PRE_COMMIT_STATUSES = frozenset(
    ("draft", "auth_phone", "auth_code", "auth_pass", "identity_ok", "ready_commit")
)

# Forward-transition graph for replacement_advance(). Deliberately excludes
# 'cancelled'/'failed' here -- those go through the dedicated
# replacement_cancel()/replacement_fail() helpers below, which apply their own
# (different) reachability rules instead of this table. 'notified' has NO
# entry here (no generic-advance target at all) -- notified -> done exists
# ONLY via the guarded replacement_finalize(), never via this table, so a
# caller cannot bypass the completion-prerequisite checks.
_REPLACEMENT_TRANSITIONS = {
    "draft": ("auth_phone",),
    # TPILOT AUTH SAFETY 20260809 (Ф3, D-QR): additive edge for the QR-based
    # replacement path -- QR has no phone/code exchange, so the operation goes
    # draft -> auth_phone (key reservation + proxy route + display name, via
    # replacement_send_qr_start) then STRAIGHT to identity_ok once the QR
    # session is confirmed authorized (via _replacement_after_signin, whose
    # from_status computation now recognizes 'auth_phone' as a valid prior
    # status alongside the existing 'auth_code'/'auth_pass'). The phone flow's
    # own auth_phone -> auth_code edge is unchanged; this only ADDS a second
    # legal target from the same source status, never removes/redirects the
    # existing one.
    "auth_phone": ("auth_code", "identity_ok"),
    "auth_code": ("auth_pass", "identity_ok"),
    "auth_pass": ("identity_ok",),
    "identity_ok": ("ready_commit",),
    "ready_commit": ("committing",),
    "committing": ("links_pending",),
    "links_pending": ("links_ready",),
    "links_ready": ("cutover_done",),
    "cutover_done": ("notified",),
}

# links_ready_at and completed_at are deliberately NOT here -- they are owned
# exclusively by replacement_update_links() and replacement_finalize()
# respectively, never settable through the generic replacement_advance(fields=...)
# path (see replacement_advance's docstring).
#
# new_tg_user_id (Stage 2 fix R3) and proxy_mode/proxy_ref/proxy_confirmed
# (Stage 2 fix R2) follow the exact same CAS-guarded pattern as
# new_manager_key: set once via replacement_advance's fields=... at the
# transition that durably learns the value (draft->auth_phone for the proxy
# route choice; auth_code/auth_pass->identity_ok for the verified Telegram
# identity), race-guarded by ux_repl_new_tgid the same way new_manager_key is
# guarded by ux_repl_new_key.
_REPLACEMENT_MUTABLE_FIELDS = frozenset((
    "new_manager_key", "new_display_name", "new_username", "new_tg_user_id",
    "proxy_mode", "proxy_ref", "proxy_confirmed",
    "source_key", "target_date",
    "commit_deadline_at",  # TPILOT REPLACEMENT ROLLBACK 20260718
))

_REPLACEMENT_ERROR_STAGE_MAX = 64
_REPLACEMENT_ERROR_TEXT_MAX = 500


class ReplacementConflict(Exception):
    """Raised by replacement_create/replacement_advance when a uniqueness guard
    (one active replacement per old_manager_key, or new_manager_key already
    claimed by another non-terminal operation) blocks the write. Callers decide
    the user-facing message; storage.py never guesses one -- it only guarantees
    the invariant was never silently violated."""


def ensure_replacement_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Create manager_replacements and replacement_notify_ack if missing, and
    additively migrate either table to the current Stage 1 column set FIRST if
    an earlier-revision table already exists locally -- CREATE TABLE IF NOT
    EXISTS alone does not retrofit columns onto a pre-existing table, and the
    CREATE INDEX statements below reference columns (source_key on the ack
    table) that a pre-existing old-shape table would not have yet, so the
    column migration must run BEFORE this function's own CREATE TABLE/INDEX
    script. Always safe to call repeatedly (idempotent, additive, never
    drops/recreates anything). On a brand-new db this migration step is a
    complete no-op (PRAGMA table_info on a nonexistent table returns nothing),
    so it never interferes with a fresh CREATE TABLE."""
    _repl_ensure_stage1_columns(db_path)
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS manager_replacements(
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id         TEXT UNIQUE NOT NULL,
                status               TEXT NOT NULL DEFAULT 'draft',
                stage                TEXT NOT NULL DEFAULT '',
                source_key           TEXT NOT NULL DEFAULT '',
                old_manager_key      TEXT NOT NULL,
                new_manager_key      TEXT NOT NULL DEFAULT '',
                old_display_name     TEXT NOT NULL DEFAULT '',
                old_username         TEXT NOT NULL DEFAULT '',
                new_display_name     TEXT NOT NULL DEFAULT '',
                new_username         TEXT NOT NULL DEFAULT '',
                new_tg_user_id       INTEGER,
                proxy_mode           TEXT NOT NULL DEFAULT '',
                proxy_ref            TEXT NOT NULL DEFAULT '',
                proxy_confirmed      INTEGER NOT NULL DEFAULT 0,
                target_date          TEXT NOT NULL DEFAULT '',
                required_links       INTEGER NOT NULL DEFAULT 15,
                ready_links          INTEGER NOT NULL DEFAULT 0,
                links_ready_at       TEXT DEFAULT '',
                completed_at         TEXT DEFAULT '',
                created_by_user_id   INTEGER,
                error_stage          TEXT DEFAULT '',
                error_text           TEXT DEFAULT '',
                rollback_stage       TEXT NOT NULL DEFAULT '',
                rollback_at          TEXT NOT NULL DEFAULT '',
                commit_deadline_at   TEXT NOT NULL DEFAULT '',
                created_at           TEXT,
                updated_at           TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_repl_active_old
                ON manager_replacements(old_manager_key)
                WHERE status NOT IN ('done','failed','cancelled');
            CREATE UNIQUE INDEX IF NOT EXISTS ux_repl_new_key
                ON manager_replacements(new_manager_key)
                WHERE new_manager_key <> '' AND status NOT IN ('failed','cancelled');
            CREATE UNIQUE INDEX IF NOT EXISTS ux_repl_new_tgid
                ON manager_replacements(new_tg_user_id)
                WHERE new_tg_user_id IS NOT NULL AND new_tg_user_id <> 0
                      AND status NOT IN ('failed','cancelled');
            CREATE INDEX IF NOT EXISTS ix_repl_old ON manager_replacements(old_manager_key);
            CREATE INDEX IF NOT EXISTS ix_repl_source_date ON manager_replacements(source_key, target_date);
            CREATE INDEX IF NOT EXISTS ix_repl_status ON manager_replacements(status);

            CREATE TABLE IF NOT EXISTS replacement_notify_ack(
                user_id           INTEGER NOT NULL,
                replacement_id    INTEGER NOT NULL,
                source_key        TEXT NOT NULL DEFAULT '',
                acknowledged_at   TEXT NOT NULL DEFAULT '',
                created_at        TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, replacement_id)
            );
            CREATE INDEX IF NOT EXISTS ix_repl_ack_user ON replacement_notify_ack(user_id);
            CREATE INDEX IF NOT EXISTS ix_repl_ack_source ON replacement_notify_ack(source_key);
            CREATE INDEX IF NOT EXISTS ix_repl_ack_acked_at ON replacement_notify_ack(acknowledged_at);
        """)
        con.commit()
    finally:
        con.close()


def _repl_ensure_stage1_columns(db_path=None):
    # type: (Optional[str]) -> None
    """Additive column migration for an earlier-revision manager_replacements/
    replacement_notify_ack table (required_links/ready_links; ack's
    source_key/created_at; acked_at -> acknowledged_at rename). Idempotent,
    additive, never destructive -- mirrors _bsl_ensure_views_column's
    PRAGMA-check-then-ALTER pattern above. Existing rows get safe defaults via
    the column DEFAULT clause, never a bulk UPDATE. A fresh table (just
    created with the full current column set) makes every check here a no-op."""
    con = _bsl_connect(db_path)
    try:
        cols = [str(r[1]) for r in con.execute("PRAGMA table_info(manager_replacements)").fetchall()]
        if cols:
            if "required_links" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN required_links INTEGER NOT NULL DEFAULT 15")
            if "ready_links" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN ready_links INTEGER NOT NULL DEFAULT 0")
            if "new_tg_user_id" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN new_tg_user_id INTEGER")
            if "proxy_mode" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN proxy_mode TEXT NOT NULL DEFAULT ''")
            if "proxy_ref" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN proxy_ref TEXT NOT NULL DEFAULT ''")
            if "proxy_confirmed" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN proxy_confirmed INTEGER NOT NULL DEFAULT 0")
            # TPILOT REPLACEMENT ROLLBACK 20260718: additive columns for the
            # automatic-rollback path (root-cause audit, valeria1/valery_onn
            # stuck-replacement incident) -- never destructive, existing rows
            # get safe empty-string defaults via the column DEFAULT clause.
            if "rollback_stage" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN rollback_stage TEXT NOT NULL DEFAULT ''")
            if "rollback_at" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN rollback_at TEXT NOT NULL DEFAULT ''")
            if "commit_deadline_at" not in cols:
                con.execute("ALTER TABLE manager_replacements ADD COLUMN commit_deadline_at TEXT NOT NULL DEFAULT ''")
            con.commit()
        ack_cols = [str(r[1]) for r in con.execute("PRAGMA table_info(replacement_notify_ack)").fetchall()]
        if ack_cols:
            if "source_key" not in ack_cols:
                con.execute("ALTER TABLE replacement_notify_ack ADD COLUMN source_key TEXT NOT NULL DEFAULT ''")
            if "created_at" not in ack_cols:
                con.execute("ALTER TABLE replacement_notify_ack ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
            if "acked_at" in ack_cols and "acknowledged_at" not in ack_cols:
                con.execute("ALTER TABLE replacement_notify_ack RENAME COLUMN acked_at TO acknowledged_at")
            con.commit()
    except Exception:
        pass
    finally:
        con.close()


# --- TPILOT HEALTH INCIDENT LIFECYCLE 20260718 START ---
# Replaces the in-memory hourly-cooldown re-notify (main.py's
# _health_agg_notified dict, cooldown-only, no lifecycle, reset on every
# controller restart) with a durable per-(manager_key, signature) incident
# record: notify once on open, an optional reminder only after a long
# configurable interval, notify once on resolve, reopen only after a
# genuinely new incident following a resolve. Idempotent, additive, never
# destructive.

def ensure_health_incidents_table(db_path=None):
    # type: (Optional[str]) -> None
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS health_incidents(
                manager_key      TEXT NOT NULL,
                signature        TEXT NOT NULL,
                status           TEXT NOT NULL DEFAULT 'open',
                opened_at        TEXT NOT NULL DEFAULT '',
                last_notified_at TEXT NOT NULL DEFAULT '',
                reminder_count   INTEGER NOT NULL DEFAULT 0,
                resolved_at      TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(manager_key, signature)
            );
            CREATE INDEX IF NOT EXISTS ix_health_incidents_status ON health_incidents(status);
        """)
        con.commit()
    finally:
        con.close()


def health_incident_upsert_open(manager_key, signature, *, db_path=None, preserve_notify_within_sec=0):
    # type: (str, str, ..., Optional[str], int) -> Dict[str, Any]
    """Opens a new incident for (manager_key, signature), or returns the
    existing OPEN one UNCHANGED (idempotent -- a persistent flap re-evaluated
    every tick must never reset opened_at/reminder_count/last_notified_at).
    A previously RESOLVED incident with the same signature is reopened as a
    genuinely new incident (fresh opened_at, reminder_count=0,
    last_notified_at='') -- the only way a row transitions resolved->open.

    PEERFLOOD RECOVERY 20260812 (finding M5, additive): preserve_notify_within_sec
    (default 0 -- prior behavior, unaffected for existing callers) lets a
    reopen that follows very soon after its own resolution keep the PRIOR
    last_notified_at/reminder_count instead of resetting them, so a fast
    OK<->LIMITED flap does not defeat health_incident_claim_notify's repeat-
    after window (it would otherwise re-fire an immediate alert on every
    flap). Only applies when the existing row's resolved_at is within
    preserve_notify_within_sec seconds of now; a genuinely stale/old
    resolution still reopens fresh, exactly as before."""
    ensure_health_incidents_table(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM health_incidents WHERE manager_key=? AND signature=?", (mk, sig),
        ).fetchone()
        if row and str(row["status"]) == "open":
            return dict(row)
        preserve_notified_at = ""
        preserve_reminder_count = 0
        if row and int(preserve_notify_within_sec or 0) > 0:
            resolved_at = str(row["resolved_at"] or "").strip()
            if resolved_at:
                elapsed = None
                try:
                    elapsed = (_utc_now().replace(microsecond=0) - datetime.fromisoformat(resolved_at)).total_seconds()
                except Exception:
                    elapsed = None
                if elapsed is not None and 0 <= elapsed < int(preserve_notify_within_sec):
                    preserve_notified_at = str(row["last_notified_at"] or "")
                    try:
                        preserve_reminder_count = int(row["reminder_count"] or 0)
                    except Exception:
                        preserve_reminder_count = 0
        con.execute(
            "INSERT INTO health_incidents(manager_key, signature, status, opened_at, last_notified_at, reminder_count, resolved_at)"
            " VALUES(?,?,'open',?,?,?,'')"
            " ON CONFLICT(manager_key, signature) DO UPDATE SET"
            " status='open', opened_at=excluded.opened_at, last_notified_at=excluded.last_notified_at, reminder_count=excluded.reminder_count, resolved_at=''",
            (mk, sig, now, preserve_notified_at, preserve_reminder_count),
        )
        con.commit()
        row2 = con.execute(
            "SELECT * FROM health_incidents WHERE manager_key=? AND signature=?", (mk, sig),
        ).fetchone()
        return dict(row2) if row2 else {}
    finally:
        con.close()


def health_incident_mark_notified(manager_key, signature, *, is_reminder=False, db_path=None):
    # type: (str, str, ..., bool, Optional[str]) -> None
    """CAS-guarded to status='open' -- a resolve racing this call simply
    means the notify-mark is a harmless no-op (nothing to mark anymore)."""
    ensure_health_incidents_table(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    con = _bsl_connect(db_path)
    try:
        if is_reminder:
            con.execute(
                "UPDATE health_incidents SET last_notified_at=?, reminder_count=reminder_count+1"
                " WHERE manager_key=? AND signature=? AND status='open'",
                (now, mk, sig),
            )
        else:
            con.execute(
                "UPDATE health_incidents SET last_notified_at=?"
                " WHERE manager_key=? AND signature=? AND status='open'",
                (now, mk, sig),
            )
        con.commit()
    finally:
        con.close()


def health_incident_claim_notify(manager_key, signature, *, repeat_after_sec, db_path=None):
    # type: (str, str, ..., int, Optional[str]) -> Optional[str]
    """TPILOT HEALTH RECOVERY HYBRID-D CORRECTIVE 20260723b: atomically claims
    ownership of ONE notification delivery for an OPEN incident -- BEFORE any
    external delivery is attempted. This is the fix for the read-decide-write
    race in the old pattern (health_incident_get -> decide "was_open" ->
    health_incident_upsert_open -> deliver -> health_incident_mark_notified):
    two callers (two asyncio tasks in one process, or two separate OS
    processes sharing this SQLite file) could both read "not yet notified"
    before either one's mark_notified ran, and both would deliver. Here the
    SQL UPDATE itself is the single point of arbitration -- only the caller
    whose UPDATE actually flips a row wins, via two CAS paths tried in order:

      1. first-notification claim: WHERE status='open' AND last_notified_at=''
      2. reminder claim: WHERE status='open' AND last_notified_at<>''
         AND last_notified_at<=<now - repeat_after_sec>

    Returns 'open' (this call owns the first notification), 'reminder' (this
    call owns a repeat notification), or None (nothing to claim right now --
    already notified within the window, or the incident is not open/does not
    exist). Callers MUST deliver a notification (panel insert / Telegram
    send) ONLY after a non-None return, and only the winning caller may
    deliver.

    Delivery tradeoff (intentional, documented): the claim happens BEFORE
    delivery is attempted, not after. If BOTH the panel-notification insert
    and the Telegram delivery fail after a successful claim, the incident is
    still marked notified -- the next opportunity to notify is the normal
    repeat_after_sec reminder window, not an immediate retry. The panel
    notification (a local DB insert) is the primary durable delivery path;
    Telegram delivery on top of it is best-effort. This trades "a delivery
    outage delays the next alert" for "concurrent callers can never both
    deliver" -- the old post-delivery health_incident_mark_notified (kept
    below, unchanged, for any other existing caller) made the opposite
    tradeoff and is what produced duplicate alerts under real concurrency.
    """
    ensure_health_incidents_table(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE health_incidents SET last_notified_at=?"
            " WHERE manager_key=? AND signature=? AND status='open' AND last_notified_at=''",
            (now, mk, sig),
        )
        if cur.rowcount > 0:
            con.commit()
            return "open"

        cutoff = (_utc_now() - timedelta(seconds=int(repeat_after_sec or 0))).replace(microsecond=0).isoformat()
        cur = con.execute(
            "UPDATE health_incidents SET last_notified_at=?, reminder_count=reminder_count+1"
            " WHERE manager_key=? AND signature=? AND status='open'"
            " AND last_notified_at<>'' AND last_notified_at<=?",
            (now, mk, sig, cutoff),
        )
        con.commit()
        return "reminder" if cur.rowcount > 0 else None
    finally:
        con.close()


def health_incident_resolve_all_for_manager(manager_key, *, db_path=None, return_notified=False):
    # type: (str, ..., Optional[str], bool) -> Any
    """TPILOT HEALTH RECOVERY HYBRID-D CORRECTIVE 20260723c (finding H3):
    resolves every OPEN Telegram-health incident for manager_key -- and ONLY
    Telegram-health incidents. A genuine Telegram-health recovery (an ordinary
    LIMITED/BLOCKED write clearing via allow_recover=True) should close every
    unresolved Telegram-health incident the manager currently has -- not just
    the one whose signature happens to match the last error state stored on
    manager_telegram_health -- otherwise an incident opened under an earlier
    restriction (e.g. LIMITED/peerflood) that was later superseded by a
    different one (e.g. BLOCKED overwriting the same health row) is orphaned:
    it never resolves, and the next genuine recurrence of that earlier
    restriction is silently treated as "still open" (suppressed until the
    reminder interval) instead of alerting immediately.

    health_incidents is a SHARED table: besides Telegram-health
    (_tp_hg_incident_signature, main.py -- signatures always start with
    "limited:" or "blocked:", the only two statuses that ever open a
    Telegram-health incident), it also holds unrelated per-manager incidents
    from other subsystems using the SAME manager_key namespace
    (registry_normalize_manager_key) -- e.g. "auth_action_required" (manager
    relogin/auth recovery), "recovery_flap" / "no_health_data" (the health
    aggregator), "proxy_terminal_review" (proxy lifecycle). An earlier version
    of this function resolved EVERY open incident for manager_key regardless
    of signature, which silently closed those unrelated incidents too --
    triggering a spurious Telegram-health recovery notification AND a
    duplicate re-open notification from the affected subsystem on its next
    tick (exactly the alert-spam the incident lifecycle exists to prevent).
    The signature-prefix filter below is collision-safe: none of the other
    subsystems' signatures start with "limited:" or "blocked:".

    Returns the number of Telegram-health incidents resolved (0 is a valid,
    expected result, e.g. the first healthy tick after startup with nothing
    open, or when only foreign-subsystem incidents are open for this
    manager).

    PEERFLOOD RECOVERY 20260812 (finding M5, additive): return_notified=False
    (default -- prior behavior, unaffected for existing callers) keeps the
    original int-count return. When True, returns (resolved_count,
    any_was_notified) -- any_was_notified is True only if at least one of the
    incidents just resolved had already been delivered to the owner
    (last_notified_at != ''), so callers can skip sending a recovery
    notification for a problem the owner was never actually told about."""
    ensure_health_incidents_table(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    con = _bsl_connect(db_path)
    try:
        any_was_notified = False
        if return_notified:
            pending = con.execute(
                "SELECT last_notified_at FROM health_incidents"
                " WHERE manager_key=? AND status='open'"
                " AND (signature LIKE 'limited:%' OR signature LIKE 'blocked:%')",
                (mk,),
            ).fetchall()
            any_was_notified = any(str(r["last_notified_at"] or "").strip() for r in pending)
        cur = con.execute(
            "UPDATE health_incidents SET status='resolved', resolved_at=?"
            " WHERE manager_key=? AND status='open'"
            " AND (signature LIKE 'limited:%' OR signature LIKE 'blocked:%')",
            (now, mk),
        )
        con.commit()
        resolved_count = int(cur.rowcount or 0)
        if return_notified:
            return resolved_count, any_was_notified
        return resolved_count
    finally:
        con.close()


def health_incident_resolve(manager_key, signature, *, db_path=None):
    # type: (str, str, ..., Optional[str]) -> bool
    """Resolves an OPEN incident (CAS on status='open'). Returns True only
    if a row was actually resolved -- callers use this to decide whether a
    recovery notification is warranted (never send one if nothing was open,
    e.g. the very first healthy tick after controller startup)."""
    ensure_health_incidents_table(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE health_incidents SET status='resolved', resolved_at=?"
            " WHERE manager_key=? AND signature=? AND status='open'",
            (now, mk, sig),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def health_incident_get(manager_key, signature, *, db_path=None):
    # type: (str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    ensure_health_incidents_table(db_path)
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM health_incidents WHERE manager_key=? AND signature=?", (mk, sig),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()

# --- TPILOT HEALTH INCIDENT LIFECYCLE 20260718 END ---


# --- TPILOT HEALTH NOTIFICATION V2 20260807 START ---
# Additive storage primitives for the Health Notification System V2 root-cause
# classifier + persistent incident state machine (main.py, appended block
# near __main__). Reuses the existing health_incidents table (additive
# columns only) rather than a parallel table, and adds ONE new table
# (health_notify_quota) for the per-(incident, Kyiv calendar day) rate limit
# -- see the approved plan section E/K for the full rationale. All DDL is
# idempotent and additive; no existing column/table/index is altered or
# dropped. New status values ('observed', 'recovery_pending') are plain TEXT
# writes -- health_incidents.status has no CHECK constraint (see the DDL in
# ensure_health_incidents_table above), so no schema change is needed for
# them.

def ensure_health_incidents_v2_columns(db_path=None):
    # type: (Optional[str]) -> None
    """Idempotent additive migration: adds the HNV2 columns to
    health_incidents if missing. Same _table_columns()+ALTER-if-missing
    convention as main.py's manager_telegram_health.cooldown_until migration
    (main.py, _tp_hg_ensure_tables). Never destructive, never raises."""
    ensure_health_incidents_table(db_path)
    con = _bsl_connect(db_path)
    try:
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(health_incidents)").fetchall()]
        except Exception:
            cols = []
        for col_name, col_ddl in (
            ("observed_first_at", "observed_first_at TEXT NOT NULL DEFAULT ''"),
            ("observed_count", "observed_count INTEGER NOT NULL DEFAULT 0"),
            ("confirm_due_at", "confirm_due_at TEXT NOT NULL DEFAULT ''"),
            ("recovery_pending_since", "recovery_pending_since TEXT NOT NULL DEFAULT ''"),
            ("rearm_watermark", "rearm_watermark TEXT NOT NULL DEFAULT ''"),
            ("family", "family TEXT NOT NULL DEFAULT ''"),
            ("detail", "detail TEXT NOT NULL DEFAULT ''"),
        ):
            if col_name not in cols:
                try:
                    con.execute(f"ALTER TABLE health_incidents ADD COLUMN {col_ddl}")
                except Exception:
                    pass
        con.commit()
    finally:
        con.close()


def ensure_health_notify_quota_table(db_path=None):
    # type: (Optional[str]) -> None
    """Per-(manager_key, signature, kyiv_date) durable problem-notification
    counter. A separate table, not columns on health_incidents, because the
    grain is (incident x calendar day) -- a 1:N relation across the
    incident's lifetime; columns would force a non-atomic read-compare-reset
    dance on day rollover."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS health_notify_quota(
                manager_key   TEXT NOT NULL,
                signature     TEXT NOT NULL,
                kyiv_date     TEXT NOT NULL,
                sent_count    INTEGER NOT NULL DEFAULT 0,
                first_sent_at TEXT NOT NULL DEFAULT '',
                last_sent_at  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(manager_key, signature, kyiv_date)
            );
            CREATE INDEX IF NOT EXISTS ix_health_notify_quota_date ON health_notify_quota(kyiv_date);
        """)
        con.commit()
    finally:
        con.close()


def health_notify_quota_claim(manager_key, signature, kyiv_date, *, max_per_day=3, db_path=None):
    # type: (str, str, str, ..., int, Optional[str]) -> Tuple[bool, int]
    """Atomically claims ONE problem-notification slot for
    (manager_key, signature, kyiv_date). Same arbitration principle as
    health_incident_claim_notify above: the UPDATE's own WHERE clause IS the
    decision -- never a prior SELECT that could race. Two statements
    (INSERT OR IGNORE, then a filtered UPDATE) inside one BEGIN IMMEDIATE,
    deliberately not a single `ON CONFLICT ... DO UPDATE ... WHERE` --
    cur.rowcount for a filtered upsert is ambiguous across sqlite3 builds,
    while this two-statement form has unambiguous rowcount semantics
    everywhere. BEGIN IMMEDIATE takes the write lock before the read,
    serializing concurrent loops/processes (same technique as
    panel_bot._take_panel_notifications). Returns (granted,
    sent_count_after). Recovery notifications must NOT call this -- they are
    exempt from the daily cap by construction (the recovery path in main.py
    never calls this function)."""
    ensure_health_notify_quota_table(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    day = str(kyiv_date or "").strip()
    cap = int(max_per_day or 0)
    if not mk or not sig or not day or cap <= 0:
        return (False, -1)
    con = _bsl_connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "INSERT OR IGNORE INTO health_notify_quota"
            "(manager_key,signature,kyiv_date,sent_count,first_sent_at,last_sent_at)"
            " VALUES(?,?,?,0,'','')",
            (mk, sig, day),
        )
        cur = con.execute(
            "UPDATE health_notify_quota SET"
            "   sent_count = sent_count + 1,"
            "   first_sent_at = CASE WHEN first_sent_at='' THEN ? ELSE first_sent_at END,"
            "   last_sent_at = ?"
            " WHERE manager_key=? AND signature=? AND kyiv_date=? AND sent_count < ?",
            (now, now, mk, sig, day, cap),
        )
        granted = cur.rowcount > 0
        row = con.execute(
            "SELECT sent_count FROM health_notify_quota"
            " WHERE manager_key=? AND signature=? AND kyiv_date=?",
            (mk, sig, day),
        ).fetchone()
        con.commit()
        return (bool(granted), int((row["sent_count"] if row else 0) or 0))
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return (False, -1)
    finally:
        con.close()


def health_notify_quota_get_readonly(manager_key, signature, kyiv_date, *, db_path=None):
    # type: (str, str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    """STRICT READ-ONLY variant for /hnv2_diag (HNV2 N-1 correction,
    post-independent-review): never calls ensure_health_notify_quota_table
    (CREATE TABLE IF NOT EXISTS + commit()). Checks sqlite_master directly;
    on a fresh/pre-migration DB (table absent) returns the sentinel
    {'_schema_missing': True} rather than creating the table."""
    con = _bsl_connect(db_path)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_notify_quota'"
        ).fetchone()
        if not exists:
            return {"_schema_missing": True}
        mk = str(manager_key or "").strip()
        sig = str(signature or "").strip()
        day = str(kyiv_date or "").strip()
        row = con.execute(
            "SELECT * FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
            (mk, sig, day),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def health_notify_quota_get(manager_key, signature, kyiv_date, *, db_path=None):
    # type: (str, str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    """Pure read -- never claims, never mutates. NOTE: /hnv2_diag now uses
    health_notify_quota_get_readonly (above) instead, which additionally
    never calls ensure_health_notify_quota_table (HNV2 N-1 correction). This
    function is kept for any other read-only caller that is fine with
    lazy-migrating the table on first read."""
    ensure_health_notify_quota_table(db_path)
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    day = str(kyiv_date or "").strip()
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
            (mk, sig, day),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def health_notify_quota_refund(manager_key, signature, kyiv_date, *, db_path=None):
    # type: (str, str, str, ..., Optional[str]) -> bool
    """Returns an unused claim -- used when the quota CAS won but the
    subsequent health_incident_claim_notify CAS lost the race (nothing was
    actually delivered). Never drops below zero. A crash between the two
    calls loses one quota unit; documented tradeoff, errs toward fewer
    notifications, never more."""
    ensure_health_notify_quota_table(db_path)
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    day = str(kyiv_date or "").strip()
    if not mk or not sig or not day:
        return False
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE health_notify_quota SET sent_count = sent_count - 1"
            " WHERE manager_key=? AND signature=? AND kyiv_date=? AND sent_count > 0",
            (mk, sig, day),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def health_incident_v2_get(manager_key, signature, *, db_path=None):
    # type: (str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    """Same contract as health_incident_get, but ensures the HNV2 columns
    exist first so callers always see them (empty-string/0 defaults on a
    freshly migrated row)."""
    ensure_health_incidents_v2_columns(db_path)
    return health_incident_get(manager_key, signature, db_path=db_path)


_HNV2_INCIDENTS_V2_COLUMNS = (
    "observed_first_at", "observed_count", "confirm_due_at",
    "recovery_pending_since", "rearm_watermark", "family", "detail",
)


def health_incident_v2_get_readonly(manager_key, signature, *, db_path=None):
    # type: (str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    """STRICT READ-ONLY variant for /hnv2_diag (HNV2 N-1 correction,
    post-independent-review): never calls ensure_health_incidents_v2_columns
    or any other ensure_*/migration helper -- those perform CREATE TABLE IF
    NOT EXISTS / ALTER TABLE ADD COLUMN / commit(), which violates the
    "diagnostic command never mutates" contract even when the DDL happens to
    be a no-op against an already-migrated DB. Reads sqlite_master + PRAGMA
    table_info to check the schema is present WITHOUT creating anything; on
    a fresh/pre-migration DB (schema absent), returns the sentinel
    {'_schema_missing': True} instead of a row, so the caller can print a
    diagnostic message rather than silently creating the schema."""
    con = _bsl_connect(db_path)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='health_incidents'"
        ).fetchone()
        if not exists:
            return {"_schema_missing": True}
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(health_incidents)").fetchall()}
        except Exception:
            cols = set()
        if not set(_HNV2_INCIDENTS_V2_COLUMNS).issubset(cols):
            return {"_schema_missing": True}
        mk = str(manager_key or "").strip()
        sig = str(signature or "").strip()
        row = con.execute(
            "SELECT * FROM health_incidents WHERE manager_key=? AND signature=?", (mk, sig)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def health_incident_v2_resolve_from_recovery_pending(manager_key, signature, *, db_path=None):
    # type: (str, str, ..., Optional[str]) -> bool
    """Resolves a RECOVERY_PENDING incident (CAS on status='recovery_pending').
    The pre-existing health_incident_resolve (storage.py, above) CAS-guards
    on status='open' -- correct for its own OPEN->RESOLVED callers, but
    WRONG for HNV2's OPEN->RECOVERY_PENDING->RESOLVED state machine, where
    by the time the stability window has elapsed the row's status is
    'recovery_pending', not 'open'; calling the 'open'-gated function here
    would silently match zero rows. Returns True only if a row was
    actually resolved."""
    ensure_health_incidents_v2_columns(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE health_incidents SET status='resolved', resolved_at=?"
            " WHERE manager_key=? AND signature=? AND status='recovery_pending'",
            (now, mk, sig),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def health_incident_v2_observe(manager_key, signature, *, family, detail, confirm_sec, db_path=None):
    # type: (str, str, ..., str, str, int, Optional[str]) -> Dict[str, Any]
    """OBSERVED state transition (approved plan section D). First tick for a
    signature: opens a fresh 'observed' row. Same family observed again
    before confirm_due_at: bumps observed_count/detail in place, WITHOUT
    resetting observed_first_at/confirm_due_at (that would let a flapping
    classifier push confirmation out indefinitely). A different, unrelated
    signature is a different primary-key row and is handled by a separate
    call -- this function only ever touches the one (manager_key, signature)
    row named by its arguments. Never sends a notification -- this is the
    silent accumulation phase before OPEN. Returns the row after the write."""
    ensure_health_incidents_v2_columns(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    fam = str(family or "").strip()
    det = str(detail or "")[:500]
    due = (_utc_now() + timedelta(seconds=int(confirm_sec or 0))).replace(microsecond=0).isoformat()
    con = _bsl_connect(db_path)
    try:
        existing = con.execute(
            "SELECT * FROM health_incidents WHERE manager_key=? AND signature=?", (mk, sig),
        ).fetchone()
        is_fresh_observation = (
            existing is None
            or str(existing["status"]) not in ("observed",)
            or str(existing["family"] or "") != fam
        )
        if is_fresh_observation:
            con.execute(
                "INSERT INTO health_incidents"
                "(manager_key,signature,status,opened_at,last_notified_at,reminder_count,resolved_at,"
                " observed_first_at,observed_count,confirm_due_at,recovery_pending_since,rearm_watermark,family,detail)"
                " VALUES(?,?,'observed','','',0,'',?,1,?,'',"
                "   COALESCE((SELECT rearm_watermark FROM health_incidents WHERE manager_key=? AND signature=?), ''),"
                "   ?,?)"
                " ON CONFLICT(manager_key,signature) DO UPDATE SET"
                "   status='observed', observed_first_at=excluded.observed_first_at,"
                "   observed_count=1, confirm_due_at=excluded.confirm_due_at,"
                "   recovery_pending_since='', family=excluded.family, detail=excluded.detail",
                (mk, sig, now, due, mk, sig, fam, det),
            )
        else:
            con.execute(
                "UPDATE health_incidents SET observed_count=observed_count+1, detail=?"
                " WHERE manager_key=? AND signature=? AND status='observed'",
                (det, mk, sig),
            )
        con.commit()
        row = con.execute(
            "SELECT * FROM health_incidents WHERE manager_key=? AND signature=?", (mk, sig),
        ).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def health_incident_v2_close_silent(manager_key, signature, *, db_path=None):
    # type: (str, str, ..., Optional[str]) -> bool
    """Silently closes an OBSERVED (never-yet-opened/never-notified) row --
    used when the family changes or clears before confirm_due_at. CAS on
    status='observed' only, so an already-OPEN incident is never touched by
    this path (that must go through the normal recovery/resolve flow so a
    recovery notification is sent). Returns True only if a row was
    actually closed."""
    ensure_health_incidents_v2_columns(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE health_incidents SET status='resolved', resolved_at=?"
            " WHERE manager_key=? AND signature=? AND status='observed'",
            (now, mk, sig),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def health_incident_v2_set_state(manager_key, signature, *, status, db_path=None, **fields):
    # type: (str, str, ..., str, Optional[str], Any) -> bool
    """Generic CAS-free state/field writer for HNV2 transitions that do not
    need the open/observe-specific logic above (e.g. OPEN->RECOVERY_PENDING,
    RECOVERY_PENDING->OPEN). `fields` may include any of: recovery_pending_since,
    family, detail. `status` is written unconditionally on an existing row
    (callers are responsible for calling this only from a code path that
    already confirmed the current status, per the approved state machine --
    this is intentionally not a CAS because every caller already holds the
    single-threaded aggregator tick as its concurrency boundary, unlike the
    notify-claim paths which must be safe under concurrent processes).
    Never creates a new row. Returns True if a row was updated."""
    ensure_health_incidents_v2_columns(db_path)
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    st = str(status or "").strip()
    if not mk or not sig or not st:
        return False
    allowed_fields = {"recovery_pending_since", "family", "detail", "reminder_count"}
    set_parts = ["status=?"]
    params: list = [st]
    for k, v in fields.items():
        if k not in allowed_fields:
            continue
        set_parts.append(f"{k}=?")
        params.append(v)
    params.extend([mk, sig])
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            f"UPDATE health_incidents SET {', '.join(set_parts)} WHERE manager_key=? AND signature=?",
            tuple(params),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def health_incident_v2_set_watermark(manager_key, signature, watermark, *, db_path=None):
    # type: (str, str, str, ..., Optional[str]) -> None
    """Writes rearm_watermark (a JSON string {"ts":..., "n":...}, opaque to
    storage.py -- main.py owns the format). Upserts a bootstrap row (status
    'resolved') if none exists yet, so the watermark always has a durable
    home before any incident for this signature is ever opened -- see the
    approved plan's bootstrap step for hv2:recovery_flap:flap."""
    ensure_health_incidents_v2_columns(db_path)
    now = _now_iso()
    mk = str(manager_key or "").strip()
    sig = str(signature or "").strip()
    wm = str(watermark or "")
    if not mk or not sig:
        return
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE health_incidents SET rearm_watermark=? WHERE manager_key=? AND signature=?",
            (wm, mk, sig),
        )
        if cur.rowcount == 0:
            con.execute(
                "INSERT INTO health_incidents"
                "(manager_key,signature,status,opened_at,last_notified_at,reminder_count,resolved_at,"
                " observed_first_at,observed_count,confirm_due_at,recovery_pending_since,rearm_watermark,family,detail)"
                " VALUES(?,?,'resolved','','',0,?,'',0,'','',?,'','')"
                " ON CONFLICT(manager_key,signature) DO UPDATE SET rearm_watermark=excluded.rearm_watermark",
                (mk, sig, now, wm),
            )
        con.commit()
    finally:
        con.close()


def health_incident_any_open_v2(manager_key, *, db_path=None):
    # type: (str, ..., Optional[str]) -> bool
    """True if this manager has any HNV2 (signature prefix 'hv2:') incident
    currently in status 'open'. Used by the AdminBot callback handler to
    tell an operator "this incident is already closed" on a stale click,
    without needing the exact signature."""
    ensure_health_incidents_v2_columns(db_path)
    mk = str(manager_key or "").strip()
    if not mk:
        return False
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM health_incidents WHERE manager_key=? AND signature LIKE 'hv2:%' AND status='open' LIMIT 1",
            (mk,),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def health_incident_v2_list_active(manager_key, *, db_path=None):
    # type: (str, ..., Optional[str]) -> List[Dict[str, Any]]
    """All hv2:* incidents for manager_key currently in status 'open' or
    'recovery_pending'. Used by the Recovery Ownership Matrix (approved
    plan section I.2.2): the aggregator's PASS 2 independently re-evaluates
    EVERY active incident against its OWN family's authoritative recovery
    evidence every tick, regardless of what the CURRENT tick's classifier
    reports -- a family that is no longer the top-priority cause this tick
    must not be silently abandoned; it stays open until its own evidence
    proves recovery."""
    ensure_health_incidents_v2_columns(db_path)
    mk = str(manager_key or "").strip()
    if not mk:
        return []
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM health_incidents WHERE manager_key=? AND signature LIKE 'hv2:%' AND status IN ('open','recovery_pending')",
            (mk,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

# --- TPILOT HEALTH NOTIFICATION V2 20260807 END ---


def replacement_create(
    operation_id, old_manager_key, *,
    source_key="", old_display_name="", old_username="",
    created_by_user_id=None, target_date="", db_path=None,
):
    # type: (str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    """Start a new replacement operation for old_manager_key (status='draft',
    required_links defaults to 15 via the column DEFAULT).

    Idempotent on operation_id: a retry with the SAME operation_id returns the
    existing row unchanged (never raises, never duplicates). Returns None when
    ANOTHER operation_id already holds the one active (non-terminal) replacement
    slot for this old_manager_key -- ux_repl_active_old is the single source of
    truth for "one active replacement per primary manager", enforced here."""
    op = str(operation_id or "").strip()
    ok = _rsv_norm_key(old_manager_key)
    if not op or not ok:
        raise ValueError("operation_id and old_manager_key are required")
    ensure_replacement_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        existing_by_op = con.execute(
            "SELECT * FROM manager_replacements WHERE operation_id=?", (op,)
        ).fetchone()
        if existing_by_op:
            return dict(existing_by_op)
        try:
            con.execute(
                "INSERT INTO manager_replacements"
                "(operation_id, status, stage, source_key, old_manager_key,"
                " old_display_name, old_username, target_date, created_by_user_id,"
                " created_at, updated_at)"
                " VALUES(?,'draft','',?,?,?,?,?,?,?,?)",
                (op, _rsv_norm_key(source_key), ok, str(old_display_name or ""),
                 str(old_username or ""), str(target_date or ""), created_by_user_id, now, now),
            )
            con.commit()
        except _bsl_sqlite3.IntegrityError:
            con.rollback()
            return None
        row = con.execute(
            "SELECT * FROM manager_replacements WHERE operation_id=?", (op,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def replacement_get(operation_id, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    op = str(operation_id or "").strip()
    if not op:
        return None
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM manager_replacements WHERE operation_id=?", (op,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def replacement_get_by_id(replacement_id, db_path=None):
    # type: (int, Optional[str]) -> Optional[Dict[str, Any]]
    """Exact match on the numeric id. Invalid/non-positive id returns None."""
    rid = int(replacement_id or 0)
    if rid <= 0:
        return None
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute("SELECT * FROM manager_replacements WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def replacement_get_by_new_key(new_manager_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    """Exact match on new_manager_key -- no prefix matching, no active-only
    filtering (a historical 'done' row must remain retrievable forever, since
    ux_repl_new_key blocks that key from ever being reused). If more than one
    row ever shares this key (a failed/cancelled attempt released it, then a
    later attempt reused it -- allowed by ux_repl_new_key), the successful
    'done' row is preferred; otherwise the most recent attempt wins."""
    nk = _rsv_norm_key(new_manager_key)
    if not nk:
        return None
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM manager_replacements WHERE new_manager_key=?"
            " ORDER BY (status='done') DESC, id DESC LIMIT 1",
            (nk,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def replacement_get_active_for_old_key(old_manager_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    """The one non-terminal replacement for old_manager_key, if any. Read-only
    mirror of the ux_repl_active_old guarantee -- lets a caller show a clean
    "replacement already in progress" message before even attempting
    replacement_create()."""
    ok = _rsv_norm_key(old_manager_key)
    if not ok:
        return None
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM manager_replacements WHERE old_manager_key=?"
            " AND status NOT IN ('done','failed','cancelled')",
            (ok,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def replacement_advance(operation_id, from_status, to_status, *, stage="", fields=None, db_path=None):
    # type: (str, str, str, ..., Optional[Dict[str, Any]], Optional[str]) -> bool
    """Compare-and-set forward transition: UPDATE ... WHERE operation_id=? AND
    status=from_status, only ever moving along the edges declared in
    _REPLACEMENT_TRANSITIONS (a caller bug -- e.g. skipping a stage -- raises
    ValueError immediately, before touching the DB; it is never silently
    allowed). Returns False (not an error) when the CAS predicate matches no
    row -- either a stale/duplicate call (status already moved on) or an
    unknown operation_id; this is the normal double-click/retry signal, exactly
    like reserve_activation_finish's rowcount-based guard.

    to_status='done' is ALWAYS rejected here (ValueError, checked before the
    transition table) -- only the guarded replacement_finalize() may perform
    notified -> done, since that transition must atomically validate every
    completion prerequisite (new_manager_key, new_display_name, link
    readiness). A generic caller must never be able to bypass those checks.

    fields (optional dict) may set any of _REPLACEMENT_MUTABLE_FIELDS in the
    SAME statement as the transition (e.g. new_manager_key at ready_commit) --
    column names are whitelisted here, never taken from caller-supplied
    strings verbatim into SQL. links_ready_at and completed_at are NOT in the
    whitelist -- see replacement_update_links()/replacement_finalize(). Raises
    ReplacementConflict if fields includes a new_manager_key that collides
    with another non-terminal operation's new_manager_key (ux_repl_new_key) --
    never a silent overwrite."""
    op = str(operation_id or "").strip()
    if not op:
        raise ValueError("operation_id is required")
    if str(to_status or "") == "done":
        raise ValueError(
            "replacement_advance() cannot transition to 'done' -- "
            "use the guarded replacement_finalize() instead"
        )
    valid_targets = _REPLACEMENT_TRANSITIONS.get(str(from_status or ""), ())
    if str(to_status or "") not in valid_targets:
        raise ValueError(
            f"invalid replacement transition {from_status!r} -> {to_status!r} "
            f"(allowed from {from_status!r}: {valid_targets!r})"
        )
    fields = dict(fields or {})
    unknown = set(fields) - _REPLACEMENT_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown/forbidden replacement field(s): {sorted(unknown)!r}")
    ensure_replacement_tables(db_path)
    now = _now_iso()
    set_cols = ["status=?", "stage=?", "updated_at=?"]
    params = [str(to_status), str(stage or ""), now]
    for col in _REPLACEMENT_MUTABLE_FIELDS:
        if col in fields:
            set_cols.append(f"{col}=?")
            params.append(fields[col])
    params.extend([op, str(from_status or "")])
    con = _bsl_connect(db_path)
    try:
        try:
            cur = con.execute(
                f"UPDATE manager_replacements SET {', '.join(set_cols)}"
                " WHERE operation_id=? AND status=?",
                tuple(params),
            )
            con.commit()
        except _bsl_sqlite3.IntegrityError:
            con.rollback()
            # new_manager_key and new_tg_user_id are never set together in the
            # same fields dict by any caller (they belong to different
            # transitions), so which key is present in fields tells apart
            # which uniqueness guard actually fired.
            if "new_tg_user_id" in fields:
                raise ReplacementConflict(
                    f"new_tg_user_id in fields already claimed by another active replacement: "
                    f"{fields.get('new_tg_user_id')!r}"
                )
            raise ReplacementConflict(
                f"new_manager_key in fields already claimed by another active replacement: "
                f"{fields.get('new_manager_key')!r}"
            )
        return cur.rowcount > 0
    finally:
        con.close()


def replacement_update_links(operation_id, ready_links, *, required_links=None, links_ready_at=None, db_path=None):
    # type: (str, int, ..., Optional[int], Optional[str], Optional[str]) -> Optional[Dict[str, Any]]
    """Update link-readiness progress for operation_id. Runs inside a single
    short BEGIN IMMEDIATE transaction -- both the preserve-first
    links_ready_at rule and the clamp-to-required_links rule depend on the
    row's CURRENT state, not just the caller's new values, so this cannot be
    a plain UPDATE without a race between two concurrent callers.

    Validation (raises ValueError, never silently coerces):
      - ready_links must be a non-negative int (bool rejected -- bool is an
        int subclass in Python but is always a caller bug here);
      - required_links, if supplied, must be a positive int.

    Behavior:
      - required_links, if supplied, REPLACES the stored value; omitting it
        keeps the row's current required_links (default 15).
      - ready_links is clamped to [0, required_links] -- 16/15 is stored as
        15/15, never above required_links.
      - links_ready_at is set (to now(), or the supplied deterministic
        timestamp for tests) ONLY the first time ready_links reaches
        required_links (required_links > 0) AND no links_ready_at is set yet.
        Once set, it is NEVER overwritten or cleared by a later call --
        including a regression back below required_links -- historical
        readiness is permanent (0/15 after 15/15 keeps the original
        timestamp).
      - This helper never changes status/stage -- the caller drives the
        actual state-machine transition (links_pending -> links_ready) via
        replacement_advance() separately once it observes readiness.

    Returns the updated row, or None if the operation does not exist or is
    already failed/cancelled (fail-closed, not a raised error -- a caller
    polling link progress on a dead operation is a normal race, not a bug)."""
    op = str(operation_id or "").strip()
    if not op:
        raise ValueError("operation_id is required")
    if isinstance(ready_links, bool) or not isinstance(ready_links, int) or ready_links < 0:
        raise ValueError("ready_links must be a non-negative integer")
    if required_links is not None:
        if isinstance(required_links, bool) or not isinstance(required_links, int) or required_links <= 0:
            raise ValueError("required_links must be a positive integer when supplied")
    ensure_replacement_tables(db_path)
    now = str(links_ready_at or _now_iso())
    con = _bsl_connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            row = con.execute(
                "SELECT * FROM manager_replacements WHERE operation_id=?", (op,)
            ).fetchone()
            if not row or str(row["status"]) in ("failed", "cancelled"):
                con.rollback()
                return None
            req = int(required_links) if required_links is not None else int(row["required_links"] or 0)
            clamped_ready = min(int(ready_links), req) if req > 0 else int(ready_links)
            existing_ready_at = str(row["links_ready_at"] or "")
            new_ready_at = existing_ready_at
            if not existing_ready_at and req > 0 and clamped_ready >= req:
                new_ready_at = now
            con.execute(
                "UPDATE manager_replacements SET required_links=?, ready_links=?,"
                " links_ready_at=?, updated_at=? WHERE operation_id=?",
                (req, clamped_ready, new_ready_at, _now_iso(), op),
            )
            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            raise
        row2 = con.execute(
            "SELECT * FROM manager_replacements WHERE operation_id=?", (op,)
        ).fetchone()
        return dict(row2) if row2 else None
    finally:
        con.close()


# --- TPILOT MANAGER REPLACEMENT STAGE4 COMMIT ENGINE 20260716 START ---

def replacement_set_stage(operation_id, stage, *, expected_statuses=None, db_path=None):
    # type: (str, str, ..., Optional[Any], Optional[str]) -> bool
    """Narrow, additive commit-substage marker. Stage 4's restart-safe commit
    engine needs to record fine-grained progress (e.g. 'session_promoted',
    'manager_created') WITHOUT advancing the durable status column, which
    replacement_advance() cannot do -- every one of its CAS transitions
    requires from_status != to_status per _REPLACEMENT_TRANSITIONS. Rather
    than add a new column, this reuses the EXISTING 'stage' free-text column
    (present since Stage 1) via its own narrow guarded UPDATE.

    Purely a diagnostic/observability marker for restart recovery and
    progress reporting -- Stage 4's actual safety comes from every commit
    substep independently re-verifying live DB/filesystem state before
    acting (idempotent primitives), never from trusting this string to skip
    a real check. Safe to call repeatedly with the same value.

    expected_statuses, if supplied, restricts the write to rows whose
    CURRENT status is in that iterable (e.g. only while status is still
    'committing') -- a stale/late-arriving write from an old, already-
    superseded attempt (op moved on to a later status, or was
    cancelled/failed) is silently ignored (returns False), never overwritten
    onto a row that has since progressed past it. Returns True only when a
    row was actually matched and updated."""
    op = str(operation_id or "").strip()
    if not op:
        raise ValueError("operation_id is required")
    stage_clean = str(stage or "").strip()[:_REPLACEMENT_ERROR_STAGE_MAX]
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        if expected_statuses:
            statuses = [str(s) for s in expected_statuses]
            placeholders = ",".join("?" for _ in statuses)
            cur = con.execute(
                f"UPDATE manager_replacements SET stage=?, updated_at=?"
                f" WHERE operation_id=? AND status IN ({placeholders})",
                tuple([stage_clean, _now_iso(), op] + statuses),
            )
        else:
            cur = con.execute(
                "UPDATE manager_replacements SET stage=?, updated_at=? WHERE operation_id=?",
                (stage_clean, _now_iso(), op),
            )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def reserve_pairs_reassign_primary(old_primary_key, new_primary_key, *, db_path=None):
    # type: (str, str, ..., Optional[str]) -> int
    """Repoints every 'linked' manager_reserve_pairs row from old_primary_key
    to new_primary_key -- the account-replacement analog of
    reserve_pair_release_for_primary (which retires pairs on full deletion;
    this instead hands them to the manager taking over). Only 'linked' pairs
    move -- an already-'retired' pair for the old key is left alone (its
    primary is gone for good, not being replaced). Idempotent: calling this
    twice for the same (old, new) pair finds zero remaining 'linked' rows on
    old_primary_key the second time and simply returns 0 -- never a error,
    never a duplicate move. Returns the number of pairs actually reassigned."""
    old_key = _rsv_norm_key(old_primary_key)
    new_key = _rsv_norm_key(new_primary_key)
    if not old_key or not new_key:
        raise ValueError("old_primary_key and new_primary_key are required")
    ensure_reserve_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        now = _now_iso()
        cur = con.execute(
            "UPDATE manager_reserve_pairs SET primary_key=?, updated_at=?"
            " WHERE primary_key=? AND status='linked'",
            (new_key, now, old_key),
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()

# --- TPILOT MANAGER REPLACEMENT STAGE4 COMMIT ENGINE 20260716 END ---


def replacement_finalize(operation_id, *, expected_status="notified", completed_at=None, db_path=None):
    # type: (str, ..., Optional[str], Optional[str]) -> bool
    """Guarded, atomic transition to 'done' -- the ONLY way an operation may
    reach 'done' (generic replacement_advance() always rejects to_status=
    'done'). Succeeds ONLY when ALL completion prerequisites hold in the SAME
    WHERE clause as the CAS status check (never a separate read-then-write):
      - status == expected_status (default 'notified' -- the state
        immediately before 'done' in the approved ordering);
      - new_manager_key non-empty;
      - new_display_name non-empty;
      - required_links > 0 AND ready_links >= required_links;
      - links_ready_at non-empty.
    completed_at is written only on the transition that actually flips
    notified -> done; a retry after a successful finalize is a safe,
    idempotent no-op that returns True without touching completed_at again
    (the WHERE clause no longer matches once status='done', so the UPDATE
    branch naturally can't re-fire -- the idempotent-retry check below
    confirms status is already 'done' and reports success instead of a
    false failure).

    Returns False (not an error) on any prerequisite miss, wrong status, or
    unknown operation_id."""
    op = str(operation_id or "").strip()
    if not op:
        raise ValueError("operation_id is required")
    exp = str(expected_status or "notified")
    ensure_replacement_tables(db_path)
    now = str(completed_at or _now_iso())
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_replacements SET status='done', stage='finalized',"
            " completed_at=?, updated_at=?"
            " WHERE operation_id=? AND status=?"
            " AND new_manager_key<>'' AND new_display_name<>''"
            " AND required_links>0 AND ready_links>=required_links"
            " AND links_ready_at<>''",
            (now, _now_iso(), op, exp),
        )
        con.commit()
        if cur.rowcount > 0:
            return True
        row = con.execute(
            "SELECT status FROM manager_replacements WHERE operation_id=?", (op,)
        ).fetchone()
        return bool(row) and str(row["status"]) == "done"
    finally:
        con.close()


def replacement_cancel(operation_id, *, db_path=None):
    # type: (str, ..., Optional[str]) -> bool
    """CAS to 'cancelled' -- ONLY from a pre-commit status (draft..ready_commit).
    Once 'committing' has begun the operation is durable and user-cancellation
    is no longer offered (only replacement_fail() applies from there on) --
    matches the audit's cutover-ordering requirement that a partially-committed
    replacement is never silently discarded. Returns False when the row is
    missing or already past ready_commit/terminal (the normal stale-click
    signal, not an error)."""
    op = str(operation_id or "").strip()
    if not op:
        raise ValueError("operation_id is required")
    ensure_replacement_tables(db_path)
    now = _now_iso()
    placeholders = ",".join("?" for _ in _REPLACEMENT_PRE_COMMIT_STATUSES)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            f"UPDATE manager_replacements SET status='cancelled', updated_at=?"
            f" WHERE operation_id=? AND status IN ({placeholders})",
            tuple([now, op] + list(_REPLACEMENT_PRE_COMMIT_STATUSES)),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def replacement_fail(operation_id, stage, error_text, *, rollback_stage="", db_path=None):
    # type: (str, str, str, ..., str, Optional[str]) -> bool
    """CAS to 'failed' from any non-terminal status. Records error_stage/
    error_text for diagnosis (never a traceback/secret -- caller's
    responsibility, mirrors reserve_activation_finish's last_error convention).
    Both are trimmed then bounded (_REPLACEMENT_ERROR_STAGE_MAX=64 chars,
    _REPLACEMENT_ERROR_TEXT_MAX=500 chars) -- an over-long value is truncated,
    never rejected. An empty stage normalizes to 'unknown' rather than being
    stored blank. Returns False when the row is missing or already terminal
    (done/failed/cancelled cannot be downgraded/overwritten).

    TPILOT REPLACEMENT ROLLBACK 20260718: optional rollback_stage records
    how far the automatic compensating-rollback algorithm (main.py
    _repl4_rollback) got, alongside rollback_at=now -- both blank (default)
    when this is a plain pre-commit auth-stage failure with nothing to
    compensate. Safe to call repeatedly (idempotent CAS -- a second call
    against an already-'failed' row is simply a no-op, matching the
    existing semantics)."""
    op = str(operation_id or "").strip()
    if not op:
        raise ValueError("operation_id is required")
    ensure_replacement_tables(db_path)
    now = _now_iso()
    stage_clean = str(stage or "").strip()[:_REPLACEMENT_ERROR_STAGE_MAX] or "unknown"
    text_clean = str(error_text or "").strip()[:_REPLACEMENT_ERROR_TEXT_MAX]
    rollback_stage_clean = str(rollback_stage or "").strip()[:_REPLACEMENT_ERROR_STAGE_MAX]
    rollback_at_value = now if rollback_stage_clean else ""
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_replacements SET status='failed', error_stage=?,"
            " error_text=?, rollback_stage=?, rollback_at=?, updated_at=?"
            " WHERE operation_id=? AND status NOT IN ('done','failed','cancelled')",
            (stage_clean, text_clean, rollback_stage_clean, rollback_at_value, now, op),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def replacement_list_for_source(source_key, *, statuses=None, db_path=None):
    # type: (str, ..., Optional[Any], Optional[str]) -> List[Dict[str, Any]]
    """All replacements for source_key, optionally filtered to a status subset
    (e.g. REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES). Ordered oldest-first by id --
    callers needing "unread" filtering do so via replacement_ack_* below."""
    sk = _rsv_norm_key(source_key)
    if not sk:
        return []
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        if statuses:
            sts = [str(s) for s in statuses]
            placeholders = ",".join("?" for _ in sts)
            rows = con.execute(
                f"SELECT * FROM manager_replacements WHERE source_key=?"
                f" AND status IN ({placeholders}) ORDER BY id ASC",
                tuple([sk] + sts),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM manager_replacements WHERE source_key=? ORDER BY id ASC",
                (sk,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def replacement_list_commit_phase_past_deadline(now_iso, *, db_path=None):
    # type: (str, Optional[str]) -> List[Dict[str, Any]]
    """TPILOT C/F 20260718: replacement rows still in a pre-cutover commit
    phase (committing/links_pending/links_ready) whose commit_deadline_at has
    already passed -- the "commit-deadline expiry" automatic-rollback
    trigger (plan §16-C), consumed by a controller sweeper loop. Rows with an
    empty commit_deadline_at (pre-existing/ancient rows from before this
    column existed) are never matched -- nothing to expire without a
    deadline. TPILOT FIX-4 20260718b: rows whose stage has already reached
    the durable cutover point-of-no-return ('cutover_started' /
    'old_manager_retired' -- see main.py's _REPL_CUTOVER_IRREVERSIBLE_STAGES)
    are EXCLUDED even though status is still 'links_ready' -- this is the
    SQL-level half of the fix for the sweeper/cutover race the independent
    audit found (a sweep firing exactly as cutover begins must never trigger
    rollback of an already-irreversible operation); main.py's
    _repl4_rollback also independently refuses on the same condition."""
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM manager_replacements WHERE status IN ('committing','links_pending','links_ready')"
            " AND COALESCE(commit_deadline_at,'')<>'' AND commit_deadline_at<=?"
            " AND COALESCE(stage,'') NOT IN ('cutover_started','old_manager_retired') ORDER BY id ASC",
            (str(now_iso),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def replacement_list_notify_eligible_for_source(source_key, *, db_path=None):
    # type: (str, ..., Optional[str]) -> List[Dict[str, Any]]
    """Shortcut for replacement_list_for_source(..., statuses=
    REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES) -- the candidate set a future
    PartnerBot notify sweep filters down to "unread for this user" via
    replacement_ack_unacked_ids()."""
    return replacement_list_for_source(
        source_key, statuses=REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES, db_path=db_path
    )


def replacement_ack_insert(user_id, replacement_id, source_key, *, db_path=None):
    # type: (int, int, str, ..., Optional[str]) -> bool
    """Idempotent, per-user, source-and-status-validated acknowledgement.

    Uses a single guarded INSERT ... SELECT (never a separate read-then-write)
    so the source/status validation and the insert happen atomically: the row
    is inserted ONLY when manager_replacements.id=replacement_id actually has
    source_key == the supplied (normalized) source_key AND its status is in
    REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES (cutover_done/notified/done) --
    wrong source, wrong/pending status, or a missing replacement id all
    insert nothing and return False. This means a failed/cancelled/draft/
    auth_*/committing/links_pending/links_ready replacement can NEVER be
    acknowledged -- only a genuinely completed-cutover one.

    Acknowledging the SAME (user_id, replacement_id) again (with the SAME
    source_key it was originally acknowledged under) is always safe and
    returns True (idempotent) -- acknowledging it again with a WRONG
    source_key returns False without disturbing the existing ack row."""
    uid = int(user_id or 0)
    rid = int(replacement_id or 0)
    sk = _rsv_norm_key(source_key)
    if uid <= 0 or rid <= 0 or not sk:
        return False
    ensure_replacement_tables(db_path)
    now = _now_iso()
    placeholders = ",".join("?" for _ in REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO replacement_notify_ack"
            "(user_id, replacement_id, source_key, acknowledged_at, created_at)"
            f" SELECT ?, id, source_key, ?, ? FROM manager_replacements"
            f" WHERE id=? AND source_key=? AND status IN ({placeholders})",
            (uid, now, now, rid, sk) + tuple(REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES),
        )
        con.commit()
        if cur.rowcount > 0:
            return True
        row = con.execute(
            "SELECT 1 FROM replacement_notify_ack WHERE user_id=? AND replacement_id=? AND source_key=?",
            (uid, rid, sk),
        ).fetchone()
        return bool(row)
    finally:
        con.close()


def replacement_ack_exists(user_id, replacement_id, db_path=None):
    # type: (int, int, Optional[str]) -> bool
    uid = int(user_id or 0)
    rid = int(replacement_id or 0)
    if uid <= 0 or rid <= 0:
        return False
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM replacement_notify_ack WHERE user_id=? AND replacement_id=?",
            (uid, rid),
        ).fetchone()
        return bool(row)
    finally:
        con.close()


def replacement_ack_unacked_ids(user_id, replacement_ids, db_path=None):
    # type: (int, Any, Optional[str]) -> List[int]
    """Given a candidate list of replacement ids, return only the ones this
    user has NOT yet acknowledged -- one query instead of N, for building a
    per-user "unread" set. Order of the input is preserved; duplicates and
    non-positive ids are dropped."""
    uid = int(user_id or 0)
    ids = [int(i) for i in (replacement_ids or []) if int(i or 0) > 0]
    if uid <= 0 or not ids:
        return []
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = con.execute(
            f"SELECT replacement_id FROM replacement_notify_ack"
            f" WHERE user_id=? AND replacement_id IN ({placeholders})",
            tuple([uid] + ids),
        ).fetchall()
        acked = {int(r[0]) for r in rows}
        seen = set()
        out = []
        for i in ids:
            if i not in acked and i not in seen:
                seen.add(i)
                out.append(i)
        return out
    finally:
        con.close()


def replacement_ack_prune(cutoff_iso, *, db_path=None):
    # type: (str, ..., Optional[str]) -> int
    """Delete replacement_notify_ack rows with acknowledged_at < cutoff_iso.
    The caller supplies the cutoff (no internal now()/timedelta dependency)
    for deterministic tests and to keep this a pure, callable-anytime helper
    -- Stage 1 adds no background scheduler for this. Touches ONLY
    replacement_notify_ack -- manager_replacements and every other table are
    untouched. Boundary: strictly-less-than only (acknowledged_at == cutoff
    or > cutoff are kept). Returns the number of rows deleted. An empty/
    blank cutoff deletes nothing (fails closed, never an accidental
    full-table wipe)."""
    cutoff = str(cutoff_iso or "").strip()
    if not cutoff:
        return 0
    ensure_replacement_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "DELETE FROM replacement_notify_ack WHERE acknowledged_at < ?", (cutoff,)
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()
# --- TPILOT MANAGER REPLACEMENT STAGE1 20260715 END (fix round 20260715b) ---


# --- TPILOT M2.13D-2 BIZLINK DISPLAY HELPERS 20260609 START ---


def bizlink_template_count_available(db_path=None):
    # type: (Optional[str]) -> int
    """Count all templates in bizlink_templates. Max 15 due to CHECK(slot_no BETWEEN 1 AND 15)."""
    try:
        ensure_bizlink_tables(db_path)
        con = _bsl_connect(db_path)
        try:
            row = con.execute("SELECT COUNT(*) FROM bizlink_templates").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            con.close()
    except Exception:
        return _BIZLINK_SLOT_COUNT


def bizlink_clamp_count(requested_count, db_path=None):
    # type: (int, Optional[str]) -> Tuple[int, int]
    """Return (clamped_count, available_count). Clamps requested to [1, available].
    Falls back to _BIZLINK_SLOT_COUNT (15) if table read fails."""
    available = bizlink_template_count_available(db_path)
    if available < 1:
        available = _BIZLINK_SLOT_COUNT
    requested = max(1, int(requested_count or 1))
    clamped = min(requested, available)
    return clamped, available


def format_bizlinks_grouped(target_date_iso, rows, label_map, expected_count=None, expected_manager_keys=None):
    # type: (str, List[Dict[str, Any]], Dict[str, str], Optional[int], Optional[Any]) -> str
    """Pure formatting helper — clean grouped business links display.

    Header: "🔗 Бизнес-ссылки на DD-MM-YYYY"
    Per manager block: blank + label + blank + created link URLs (no numbering, no slots).
    Footer: overall status line.

    No slot numbers. No manager_key unless last-resort fallback.
    label_map: {manager_key -> display_label, e.g. '@username' or display_name}
    expected_count: expected links per manager (None = use count of rows per manager)

    expected_manager_keys: optional iterable of manager_key values the caller
    considers relevant for target_date_iso (e.g. scheduled/source-linked
    managers, including activated reserves). When given:
      - A manager NOT in this set whose rows for this date are ALL
        deleted/ghost (no active link) is dropped entirely from both the
        displayed body and the readiness denominator -- a manager's own
        historical deleted-only rows for an unrelated date must never
        inflate another date's footer.
      - A manager NOT in this set but with at least one active link is still
        shown (never silently hides real, currently-live links), but does
        NOT count toward the footer ratio -- extras never push the ratio
        past 100% or make it meaningless.
      - A manager IN this set is ALWAYS shown and ALWAYS counted, even when
        it has zero bizlinks rows at all for this date (never attempted) --
        expected-per-manager slot count defaults to 15 in that case unless
        expected_count overrides it.
    None (default) disables this filter entirely -- fully backward
    compatible with every existing caller that does not pass it (per-manager
    expected count then falls back to len(rows-for-that-manager), exactly
    the original behavior).

    Per-manager display:
      15/15 (or n_exp/n_exp) -> just the link list.
      0 active               -> "(ссылки ещё не созданы)".
      1..n_exp-1 active      -> link list + "⚠️ Не готово: X/N, не хватает Y".
    Footer: "✅ Готово X/Y" when X==Y>0, else "⚠️ Готово X/Y" followed by a
    per-manager incomplete-list ("• label — X/N, не хватает Y") for every
    expected manager below its own expected count.
    """
    try:
        p = str(target_date_iso or "").split("-")
        date_display = "{}-{}-{}".format(p[2], p[1], p[0])
    except Exception:
        date_display = str(target_date_iso or "")

    lines = [
        "\U0001f517 Бизнес-ссылки на {}".format(date_display)
    ]

    if not rows:
        lines += ["", "Ссылок нет."]
        return "\n".join(lines)

    expected_keys_norm = None
    if expected_manager_keys is not None:
        expected_keys_norm = {str(k or "").strip().lower() for k in expected_manager_keys}

    # Group by manager_key preserving insertion order
    mgr_order = []  # type: List[str]
    mgr_links = {}  # type: Dict[str, List[Dict[str, Any]]]
    for lnk in rows:
        mk = str(lnk.get("manager_key") or "")
        if mk not in mgr_links:
            mgr_order.append(mk)
            mgr_links[mk] = []
        mgr_links[mk].append(lnk)

    # An expected manager with literally zero bizlinks rows for this date (never
    # even attempted) must still appear as "ещё не созданы" -- append any such
    # keys, ordered by display label for a deterministic, readable list.
    if expected_keys_norm is not None:
        present_norm = {mk.strip().lower() for mk in mgr_order}
        missing_expected = [k for k in expected_keys_norm if k not in present_norm]
        for k in sorted(missing_expected, key=lambda k: str(label_map.get(k) or k)):
            mgr_order.append(k)
            mgr_links[k] = []

    total_created = 0
    total_expected = 0
    any_manager_rendered = False
    incomplete_expected = []  # type: List[Tuple[str, int, int]]

    for mk in mgr_order:
        lnks = mgr_links.get(mk) or []
        created = [
            lnk for lnk in lnks
            if str(lnk.get("status") or "") == "created"
            and str(lnk.get("link_url") or "").strip()
            and not str(lnk.get("deleted_at") or "").strip()
        ]
        is_expected = expected_keys_norm is not None and mk.strip().lower() in expected_keys_norm

        if expected_keys_norm is not None and not created and not is_expected:
            # Not expected for this date and has no active links -- this manager's
            # rows here are purely historical/deleted/ghost noise for this date;
            # exclude from display and from the readiness denominator.
            continue

        label = str(label_map.get(mk) or label_map.get(mk.lower()) or mk)
        if expected_count is not None:
            n_exp = expected_count
        elif expected_keys_norm is not None:
            n_exp = 15  # standard per-manager slot allocation under schedule-aware filtering
        else:
            n_exp = len(lnks)  # legacy behavior, unchanged when the filter is not used at all

        lines.append("")
        lines.append(label)
        lines.append("")

        n_active = len(created)
        if created:
            for lnk in sorted(created, key=lambda x: int(x.get("slot_no") or 0)):
                lines.append(str(lnk.get("link_url") or "").strip())
        else:
            lines.append(
                "(ссылки ещё "
                "не созданы)"
            )

        if is_expected and 0 < n_active < n_exp:
            lines.append(
                "⚠️ Не готово: {}/{}, не хватает {}".format(n_active, n_exp, n_exp - n_active)
            )

        if expected_keys_norm is None or is_expected:
            # Extras (real active links from a non-expected manager) are shown
            # above but never counted -- keeps the ratio meaningful and <=100%.
            total_created += n_active
            total_expected += n_exp
            if is_expected and n_active < n_exp:
                incomplete_expected.append((label, n_active, n_exp))

        any_manager_rendered = True

    lines.append("")
    if total_expected > 0:
        if total_created >= total_expected:
            lines.append(
                "✅ Готово {}/{}".format(
                    total_created, total_expected
                )
            )
        else:
            lines.append(
                "⚠️ Готово {}/{}".format(
                    total_created, total_expected
                )
            )
            for label, n_active, n_exp in incomplete_expected:
                lines.append(
                    "• {} — {}/{}, не хватает {}".format(label, n_active, n_exp, n_exp - n_active)
                )
    elif not any_manager_rendered:
        lines.append("Ссылок нет.")

    return "\n".join(lines)


# --- TPILOT M2.13D-2 BIZLINK DISPLAY HELPERS 20260609 END ---

# --- TPILOT M2.13B BIZLINK CREATE STORAGE 20260609 END ---


# --- TPILOT M2.13D-3A BIZLINK DELETE STORAGE 20260609 START ---

import uuid as _m213d3a_uuid  # for preview_id generation (stdlib, always available)


def ensure_bizlink_delete_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Additive migration: soft-delete columns on bizlinks + audit/preview tables.

    Safe to call multiple times (idempotent). Never raises.
    """
    try:
        con = _bsl_connect(db_path)
        try:
            # 1. Add soft-delete columns to bizlinks if missing
            cols = [str(r[1]) for r in con.execute("PRAGMA table_info(bizlinks)").fetchall()]
            if "deleted_at" not in cols:
                con.execute(
                    "ALTER TABLE bizlinks ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''"
                )
            if "deleted_by_user_id" not in cols:
                con.execute(
                    "ALTER TABLE bizlinks ADD COLUMN deleted_by_user_id INTEGER NOT NULL DEFAULT 0"
                )
            if "delete_error" not in cols:
                con.execute(
                    "ALTER TABLE bizlinks ADD COLUMN delete_error TEXT NOT NULL DEFAULT ''"
                )
            con.commit()

            # 2. Audit table — one row per delete operation
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS bizlink_delete_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manager_key TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    target_date TEXT NOT NULL DEFAULT '',
                    requested_count INTEGER NOT NULL DEFAULT 0,
                    deleted_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    slugs_json TEXT NOT NULL DEFAULT '',
                    link_urls_json TEXT NOT NULL DEFAULT '',
                    requested_by_user_id INTEGER NOT NULL DEFAULT 0,
                    requested_by TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'tpilot',
                    result_ok INTEGER NOT NULL DEFAULT 0,
                    error_class TEXT NOT NULL DEFAULT '',
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )

            # 3. Preview table — single-use confirmation tokens
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS bizlink_delete_preview(
                    preview_id TEXT PRIMARY KEY,
                    manager_key TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    target_date TEXT NOT NULL DEFAULT '',
                    slugs_json TEXT NOT NULL,
                    link_urls_json TEXT NOT NULL,
                    exact_count INTEGER NOT NULL,
                    requested_by_user_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def bizlinks_select_delete_candidates(
    manager_key,
    mode,
    target_date=None,
    count=None,
    db_path=None,
    before_target_date=None,
):
    # type: (str, str, Optional[str], Optional[int], Optional[str], Optional[str]) -> List[Dict[str, Any]]
    """Return bizlinks rows suitable for TPilot-managed deletion.

    Modes:
      by_date  — status='created', manager+date, slug not empty, order slot_no ASC
      last_n   — status='created', manager, slug not empty,
                 order created_at DESC, id DESC, LIMIT count
      oldest_n — status='created', manager, slug not empty,
                 order target_date ASC, created_at ASC, id ASC, LIMIT count

    Rows with empty slug are never selected (cannot be deleted without a slug).

    before_target_date (RUNTIME READINESS RC 20260805, wave L4) applies ONLY to
    oldest_n: when given, the eligibility rule `target_date < before_target_date`
    is evaluated INSIDE the SQL, before LIMIT. It previously lived in main.py and
    ran on the already-truncated result, so a window filled with ineligible rows
    (a batch created early for a future date, e.g. vinchi/2026-08-31 created on
    2026-08-04) returned zero candidates and hid genuinely deletable history
    behind it -- the caller re-issues the identical query with no OFFSET, so it
    could never page past them. Rows with an empty target_date are excluded here
    too: they carry no provable business date and must never be auto-deleted.
    by_date and last_n are untouched.

    Ordering for oldest_n is likewise corrected to lead with target_date. The
    business meaning of "oldest" is the business date the links were made for,
    not the row's insert time; created_at and id remain as tie-breakers so the
    order stays total and deterministic. An empty created_at now sorts FIRST
    (oldest) rather than last -- the old COALESCE(NULLIF(created_at,''),'9')
    sentinel sorted undated rows after every real timestamp, i.e. treated the
    least-known rows as the newest.
    """
    try:
        ensure_bizlink_delete_tables(db_path)
        con = _bsl_connect(db_path)
        try:
            mk = str(manager_key or "")
            m = str(mode or "")
            if m == "by_date":
                td = str(target_date or "")
                cur = con.execute(
                    "SELECT * FROM bizlinks "
                    "WHERE manager_key=? AND target_date=? "
                    "  AND status='created' AND COALESCE(slug, '')<>'' "
                    "ORDER BY slot_no ASC",
                    (mk, td),
                )
            elif m == "last_n":
                n = max(1, int(count or 1))
                cur = con.execute(
                    "SELECT * FROM bizlinks "
                    "WHERE manager_key=? AND status='created' "
                    "  AND COALESCE(slug, '')<>'' "
                    "ORDER BY COALESCE(NULLIF(created_at,''), '0') DESC, id DESC "
                    "LIMIT ?",
                    (mk, n),
                )
            elif m == "oldest_n":
                n = max(1, int(count or 1))
                btd = str(before_target_date or "").strip()
                if btd:
                    cur = con.execute(
                        "SELECT * FROM bizlinks "
                        "WHERE manager_key=? AND status='created' "
                        "  AND COALESCE(slug, '')<>'' "
                        "  AND COALESCE(target_date, '')<>'' "
                        "  AND target_date < ? "
                        "ORDER BY target_date ASC, "
                        "         COALESCE(NULLIF(created_at,''), '') ASC, id ASC "
                        "LIMIT ?",
                        (mk, btd, n),
                    )
                else:
                    cur = con.execute(
                        "SELECT * FROM bizlinks "
                        "WHERE manager_key=? AND status='created' "
                        "  AND COALESCE(slug, '')<>'' "
                        "ORDER BY target_date ASC, "
                        "         COALESCE(NULLIF(created_at,''), '') ASC, id ASC "
                        "LIMIT ?",
                        (mk, n),
                    )
            else:
                return []
            return [dict(r) for r in cur.fetchall()]
        finally:
            con.close()
    except Exception:
        return []


def bizlink_delete_preview_create(
    manager_key,
    mode,
    candidates,
    *,
    target_date="",
    requested_by_user_id=0,
    ttl_seconds=300,
    db_path=None,
):
    # type: (str, str, List[Dict[str, Any]], ...) -> str
    """Create a preview/confirm token row from candidate rows. Returns preview_id (UUID4)."""
    import json as _bsd3a_json
    ensure_bizlink_delete_tables(db_path)
    now = _utc_now().replace(microsecond=0).isoformat()
    expires = (
        _utc_now()
        + timedelta(seconds=max(60, int(ttl_seconds or 300)))
    ).replace(microsecond=0).isoformat()
    preview_id = str(_m213d3a_uuid.uuid4())
    slugs = [str(r.get("slug") or "") for r in candidates
             if str(r.get("slug") or "").strip()]
    links = [str(r.get("link_url") or "") for r in candidates]
    slugs_json = _bsd3a_json.dumps(slugs, ensure_ascii=False)
    link_urls_json = _bsd3a_json.dumps(links, ensure_ascii=False)
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO bizlink_delete_preview
                (preview_id, manager_key, mode, target_date, slugs_json, link_urls_json,
                 exact_count, requested_by_user_id, created_at, expires_at, consumed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                preview_id,
                str(manager_key or ""),
                str(mode or ""),
                str(target_date or ""),
                slugs_json,
                link_urls_json,
                len(slugs),
                int(requested_by_user_id or 0),
                now,
                expires,
            ),
        )
        con.commit()
    finally:
        con.close()
    return preview_id


def bizlink_delete_preview_get(preview_id, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    """Return the preview row dict, or None if not found."""
    try:
        ensure_bizlink_delete_tables(db_path)
        con = _bsl_connect(db_path)
        try:
            cur = con.execute(
                "SELECT * FROM bizlink_delete_preview WHERE preview_id=?",
                (str(preview_id or ""),),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            con.close()
    except Exception:
        return None


def bizlink_delete_preview_consume(preview_id, db_path=None):
    # type: (str, Optional[str]) -> bool
    """Atomically mark preview consumed if not already consumed and not expired.

    Returns True if successfully consumed (exactly 1 row updated), False otherwise.
    This is the single-use guard — subsequent calls always return False.
    """
    try:
        ensure_bizlink_delete_tables(db_path)
        now_iso = _utc_now().replace(microsecond=0).isoformat()
        con = _bsl_connect(db_path)
        try:
            cur = con.execute(
                "UPDATE bizlink_delete_preview SET consumed=1 "
                "WHERE preview_id=? AND consumed=0 AND expires_at>?",
                (str(preview_id or ""), now_iso),
            )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()
    except Exception:
        return False


def bizlink_mark_deleted(
    manager_key,
    slug,
    *,
    deleted_by_user_id=0,
    db_path=None,
):
    # type: (str, str, ...) -> None
    """Soft-delete a bizlinks row: set status='deleted', deleted_at, deleted_by_user_id.

    Matches on (manager_key, slug). Only updates rows where status is not already 'deleted'.
    Never raises.
    """
    try:
        ensure_bizlink_delete_tables(db_path)
        now = _utc_now().replace(microsecond=0).isoformat()
        con = _bsl_connect(db_path)
        try:
            con.execute(
                """
                UPDATE bizlinks
                   SET status='deleted',
                       deleted_at=?,
                       deleted_by_user_id=?,
                       delete_error='',
                       updated_at=?
                 WHERE manager_key=? AND slug=? AND status<>'deleted'
                """,
                (
                    now,
                    int(deleted_by_user_id or 0),
                    now,
                    str(manager_key or ""),
                    str(slug or ""),
                ),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def bizlink_mark_delete_failed(manager_key, slug, error_text, db_path=None):
    # type: (str, str, str, Optional[str]) -> None
    """Record a delete failure on a bizlinks row. Keeps status='created', sets delete_error.

    Never raises.
    """
    try:
        ensure_bizlink_delete_tables(db_path)
        now = _utc_now().replace(microsecond=0).isoformat()
        con = _bsl_connect(db_path)
        try:
            con.execute(
                "UPDATE bizlinks SET delete_error=?, updated_at=? "
                "WHERE manager_key=? AND slug=?",
                (
                    str(error_text or "")[:500],
                    now,
                    str(manager_key or ""),
                    str(slug or ""),
                ),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def bizlink_delete_audit_add(
    manager_key,
    mode,
    *,
    target_date="",
    requested_count=0,
    deleted_count=0,
    failed_count=0,
    slugs_json="",
    link_urls_json="",
    requested_by_user_id=0,
    requested_by="",
    source="tpilot",
    result_ok=False,
    error_class="",
    error_text="",
    created_at="",
    finished_at="",
    db_path=None,
):
    # type: (...) -> None
    """Append one row to bizlink_delete_audit. Never raises."""
    try:
        ensure_bizlink_delete_tables(db_path)
        con = _bsl_connect(db_path)
        try:
            now = _utc_now().replace(microsecond=0).isoformat()
            con.execute(
                """
                INSERT INTO bizlink_delete_audit
                    (manager_key, mode, target_date,
                     requested_count, deleted_count, failed_count,
                     slugs_json, link_urls_json,
                     requested_by_user_id, requested_by, source,
                     result_ok, error_class, error_text,
                     created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(manager_key or ""),
                    str(mode or ""),
                    str(target_date or ""),
                    int(requested_count or 0),
                    int(deleted_count or 0),
                    int(failed_count or 0),
                    str(slugs_json or ""),
                    str(link_urls_json or ""),
                    int(requested_by_user_id or 0),
                    str(requested_by or ""),
                    str(source or "tpilot"),
                    1 if bool(result_ok) else 0,
                    str(error_class or ""),
                    str(error_text or "")[:500],
                    str(created_at or now),
                    str(finished_at or now),
                ),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


# --- TPILOT M2.13D-3A BIZLINK DELETE STORAGE 20260609 END ---


# --- TPILOT M2.13D-3B BIZLINK GLOBAL DELETE STORAGE 20260610 START ---


def ensure_bizlink_global_delete_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Additive migration for Patch 3B.

    1. Adds columns source, foreign_count, known_count, total_listed, meta_json
       to bizlink_delete_preview if missing.
    2. Creates bizlink_global_delete_audit table if not exists.

    Safe to call multiple times. Never raises.
    """
    try:
        # Ensure base 3A tables exist before adding columns
        ensure_bizlink_delete_tables(db_path)
        con = _bsl_connect(db_path)
        try:
            # 1. Add new columns to bizlink_delete_preview if missing
            preview_cols = [
                str(r[1])
                for r in con.execute(
                    "PRAGMA table_info(bizlink_delete_preview)"
                ).fetchall()
            ]
            for col, defn in (
                ("source", "TEXT NOT NULL DEFAULT 'tpilot'"),
                ("foreign_count", "INTEGER NOT NULL DEFAULT 0"),
                ("known_count", "INTEGER NOT NULL DEFAULT 0"),
                ("total_listed", "INTEGER NOT NULL DEFAULT 0"),
                ("meta_json", "TEXT NOT NULL DEFAULT ''"),
            ):
                if col not in preview_cols:
                    con.execute(
                        "ALTER TABLE bizlink_delete_preview ADD COLUMN {} {}".format(
                            col, defn
                        )
                    )
            con.commit()

            # 2. Global audit table — one row per global delete operation
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS bizlink_global_delete_audit(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manager_key TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'foreign_all',
                    total_listed INTEGER NOT NULL DEFAULT 0,
                    known_count INTEGER NOT NULL DEFAULT 0,
                    foreign_count INTEGER NOT NULL DEFAULT 0,
                    requested_count INTEGER NOT NULL DEFAULT 0,
                    deleted_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    deleted_json TEXT NOT NULL DEFAULT '',
                    failed_json TEXT NOT NULL DEFAULT '',
                    requested_by_user_id INTEGER NOT NULL DEFAULT 0,
                    requested_by TEXT NOT NULL DEFAULT '',
                    result_ok INTEGER NOT NULL DEFAULT 0,
                    error_class TEXT NOT NULL DEFAULT '',
                    error_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def bizlink_known_slugs_for_manager(manager_key, db_path=None):
    # type: (str, Optional[str]) -> set
    """Return the set of all non-empty slugs in bizlinks for manager_key (any status).

    Used controller-side for TPilot-known vs foreign classification.
    """
    try:
        con = _bsl_connect(db_path)
        try:
            cur = con.execute(
                "SELECT slug FROM bizlinks "
                "WHERE manager_key=? AND COALESCE(slug, '')<>''",
                (str(manager_key or ""),),
            )
            return {str(r[0]) for r in cur.fetchall() if r[0]}
        finally:
            con.close()
    except Exception:
        return set()


def bizlink_global_preview_create(
    manager_key,
    mode,
    delete_items,
    *,
    total_listed=0,
    known_count=0,
    foreign_count=0,
    requested_by_user_id=0,
    ttl_seconds=300,
    db_path=None,
):
    # type: (str, str, List[Dict[str, Any]], ...) -> str
    """Create a preview/confirm token for global Telegram link deletion.

    delete_items: list of dicts {url, slug, title, message, views} — the
    foreign/to-delete set only.  Uses the existing bizlink_delete_preview
    table with source='global'.  Returns preview_id (UUID4).
    """
    import json as _bsd3b_json
    ensure_bizlink_global_delete_tables(db_path)
    now = _utc_now().replace(microsecond=0).isoformat()
    expires = (
        _utc_now()
        + timedelta(seconds=max(60, int(ttl_seconds or 300)))
    ).replace(microsecond=0).isoformat()
    preview_id = str(_m213d3a_uuid.uuid4())
    slugs = [
        str(it.get("slug") or "").strip()
        for it in delete_items
        if str(it.get("slug") or "").strip()
    ]
    links = [str(it.get("url") or "") for it in delete_items]
    meta = [
        {
            "url": str(it.get("url") or ""),
            "slug": str(it.get("slug") or ""),
            "title": str(it.get("title") or ""),
            "message": str(it.get("message") or "")[:100],
            "views": int(it.get("views") or 0),
        }
        for it in delete_items
    ]
    slugs_json = _bsd3b_json.dumps(slugs, ensure_ascii=False)
    link_urls_json = _bsd3b_json.dumps(links, ensure_ascii=False)
    meta_json = _bsd3b_json.dumps(meta, ensure_ascii=False)
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO bizlink_delete_preview
                (preview_id, manager_key, mode, target_date,
                 slugs_json, link_urls_json, exact_count,
                 requested_by_user_id, created_at, expires_at, consumed,
                 source, foreign_count, known_count, total_listed, meta_json)
            VALUES (?, ?, ?, '', ?, ?, ?, ?, ?, ?, 0, 'global', ?, ?, ?, ?)
            """,
            (
                preview_id,
                str(manager_key or ""),
                str(mode or "foreign_all"),
                slugs_json,
                link_urls_json,
                len(slugs),
                int(requested_by_user_id or 0),
                now,
                expires,
                int(foreign_count or 0),
                int(known_count or 0),
                int(total_listed or 0),
                meta_json,
            ),
        )
        con.commit()
    finally:
        con.close()
    return preview_id


def bizlink_global_audit_add(
    manager_key,
    mode,
    *,
    total_listed=0,
    known_count=0,
    foreign_count=0,
    requested_count=0,
    deleted_count=0,
    failed_count=0,
    deleted_json="",
    failed_json="",
    requested_by_user_id=0,
    requested_by="",
    result_ok=False,
    error_class="",
    error_text="",
    created_at="",
    finished_at="",
    db_path=None,
):
    # type: (...) -> None
    """Append one row to bizlink_global_delete_audit. Never raises."""
    try:
        ensure_bizlink_global_delete_tables(db_path)
        con = _bsl_connect(db_path)
        try:
            now = _utc_now().replace(microsecond=0).isoformat()
            con.execute(
                """
                INSERT INTO bizlink_global_delete_audit
                    (manager_key, mode,
                     total_listed, known_count, foreign_count,
                     requested_count, deleted_count, failed_count,
                     deleted_json, failed_json,
                     requested_by_user_id, requested_by,
                     result_ok, error_class, error_text,
                     created_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(manager_key or ""),
                    str(mode or "foreign_all"),
                    int(total_listed or 0),
                    int(known_count or 0),
                    int(foreign_count or 0),
                    int(requested_count or 0),
                    int(deleted_count or 0),
                    int(failed_count or 0),
                    str(deleted_json or ""),
                    str(failed_json or ""),
                    int(requested_by_user_id or 0),
                    str(requested_by or ""),
                    1 if bool(result_ok) else 0,
                    str(error_class or ""),
                    str(error_text or "")[:500],
                    str(created_at or now),
                    str(finished_at or now),
                ),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


# --- TPILOT M2.13D-3B BIZLINK GLOBAL DELETE STORAGE 20260610 END ---


# --- TPILOT TRANSFERS STAGE1 M2.7A START ---
# Additive-only foundation for lead transfers (manager -> closer handoffs).
# No existing table is altered beyond the single additive managers.role column.
# All new tables/columns are created lazily (IF NOT EXISTS / try-except ALTER),
# mirroring the ensure_manager_schedule_tables / ensure_source_work_schedule pattern.

def normalize_manager_role(raw):
    # type: (Any) -> str
    """Only two roles exist: 'manager' (default) and 'closer'. Anything else -> 'manager'."""
    r = str(raw or "").strip().lower()
    return "closer" if r == "closer" else "manager"


def manager_row_is_closer(row):
    # type: (Dict[str, Any]) -> bool
    return normalize_manager_role((row or {}).get("role")) == "closer"


def ensure_manager_role_column(db_path=None):
    # type: (Optional[str]) -> None
    """Additive: managers.role TEXT NOT NULL DEFAULT 'manager'. Existing rows keep 'manager'."""
    con = _bsl_connect(db_path)
    try:
        try:
            con.execute("ALTER TABLE managers ADD COLUMN role TEXT NOT NULL DEFAULT 'manager'")
            con.commit()
        except Exception:
            pass
    finally:
        con.close()


def manager_set_role(manager_key, role, *, db_path=None):
    # type: (str, str, ...) -> bool
    """Set managers.role for one manager_key. Returns True if a row was updated.
    Never allows an empty manager_key."""
    mk = str(manager_key or "").strip()
    if not mk:
        return False
    ensure_manager_role_column(db_path)
    r = normalize_manager_role(role)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE managers SET role=?, updated_at=? WHERE manager_key=?",
            (r, now, mk),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def manager_list_closers(db_path=None):
    # type: (Optional[str]) -> List[Dict[str, Any]]
    """All managers rows with role='closer' (any status), for AdminBot closer management."""
    ensure_manager_role_column(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM managers WHERE COALESCE(role,'manager')='closer' ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows or []]
    finally:
        con.close()


def ensure_transfer_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Create lead_transfers / transfer_config / transfer_drafts / transfer_audit if missing."""
    ensure_manager_role_column(db_path)
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS lead_transfers(
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id            TEXT NOT NULL DEFAULT '',
                manager_key         TEXT NOT NULL DEFAULT '',
                closer_key          TEXT NOT NULL DEFAULT '',
                lead_chat_id        INTEGER NOT NULL DEFAULT 0,
                known_contact_id    INTEGER NOT NULL DEFAULT 0,
                lead_date           TEXT NOT NULL DEFAULT '',
                full_name           TEXT NOT NULL DEFAULT '',
                username            TEXT NOT NULL DEFAULT '',
                phone               TEXT NOT NULL DEFAULT '',
                age                 TEXT NOT NULL DEFAULT '',
                city                TEXT NOT NULL DEFAULT '',
                car_license         TEXT NOT NULL DEFAULT '',
                comment             TEXT NOT NULL DEFAULT '',
                raw_text            TEXT NOT NULL DEFAULT '',
                status              TEXT NOT NULL DEFAULT 'created',
                created_by_uid      INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL DEFAULT '',
                confirmed_at        TEXT NOT NULL DEFAULT '',
                confirmed_chat_id   INTEGER NOT NULL DEFAULT 0,
                updated_at          TEXT NOT NULL DEFAULT '',
                UNIQUE(closer_key, lead_chat_id, lead_date)
            );
            CREATE INDEX IF NOT EXISTS lead_transfers_closer_anchor_idx
                ON lead_transfers(closer_key, lead_chat_id, status);
            CREATE INDEX IF NOT EXISTS lead_transfers_closer_kc_idx
                ON lead_transfers(closer_key, known_contact_id, status);
            CREATE INDEX IF NOT EXISTS lead_transfers_mgr_date_idx
                ON lead_transfers(manager_key, lead_date);
            CREATE INDEX IF NOT EXISTS lead_transfers_batch_idx
                ON lead_transfers(batch_id);

            CREATE TABLE IF NOT EXISTS transfer_config(
                manager_key             TEXT PRIMARY KEY,
                transfer_enabled        INTEGER NOT NULL DEFAULT 0,
                transfer_group_chat_id  INTEGER NOT NULL DEFAULT 0,
                default_closers         TEXT NOT NULL DEFAULT '',
                updated_at              TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS transfer_drafts(
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key          TEXT NOT NULL DEFAULT '',
                tg_user_id           INTEGER NOT NULL DEFAULT 0,
                lead_chat_id         INTEGER NOT NULL DEFAULT 0,
                known_contact_id     INTEGER NOT NULL DEFAULT 0,
                lead_date            TEXT NOT NULL DEFAULT '',
                card_chat_id         INTEGER NOT NULL DEFAULT 0,
                card_message_id      INTEGER NOT NULL DEFAULT 0,
                state                TEXT NOT NULL DEFAULT 'await_text',
                request_chat_id      INTEGER NOT NULL DEFAULT 0,
                request_message_id  INTEGER NOT NULL DEFAULT 0,
                form_text            TEXT NOT NULL DEFAULT '',
                chosen_closers       TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL DEFAULT '',
                updated_at           TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS transfer_drafts_reply_idx
                ON transfer_drafts(request_chat_id, request_message_id);
            CREATE INDEX IF NOT EXISTS transfer_drafts_user_idx
                ON transfer_drafts(tg_user_id, state);

            CREATE TABLE IF NOT EXISTS transfer_audit(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                transfer_id  INTEGER NOT NULL DEFAULT 0,
                action       TEXT NOT NULL DEFAULT '',
                actor        TEXT NOT NULL DEFAULT '',
                detail       TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL DEFAULT ''
            );
        """)
        con.commit()
    finally:
        con.close()


def transfer_config_get(manager_key, db_path=None):
    # type: (str, Optional[str]) -> Dict[str, Any]
    ensure_transfer_tables(db_path)
    mk = str(manager_key or "").strip()
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM transfer_config WHERE manager_key=?", (mk,)
        ).fetchone()
        if row:
            return dict(row)
        return {
            "manager_key": mk, "transfer_enabled": 0, "transfer_group_chat_id": 0,
            "default_closers": "", "updated_at": "",
        }
    finally:
        con.close()


def transfer_config_set_enabled(manager_key, enabled, db_path=None):
    # type: (str, bool, Optional[str]) -> None
    ensure_transfer_tables(db_path)
    mk = str(manager_key or "").strip()
    if not mk:
        return
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO transfer_config(manager_key, transfer_enabled, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(manager_key) DO UPDATE SET
                transfer_enabled=excluded.transfer_enabled, updated_at=excluded.updated_at
            """,
            (mk, 1 if enabled else 0, now),
        )
        con.commit()
    finally:
        con.close()


def transfer_config_set_default_closers(manager_key, closer_keys_csv, db_path=None):
    # type: (str, str, Optional[str]) -> None
    ensure_transfer_tables(db_path)
    mk = str(manager_key or "").strip()
    if not mk:
        return
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO transfer_config(manager_key, default_closers, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(manager_key) DO UPDATE SET
                default_closers=excluded.default_closers, updated_at=excluded.updated_at
            """,
            (mk, str(closer_keys_csv or ""), now),
        )
        con.commit()
    finally:
        con.close()


def transfer_config_set_group_chat(manager_key, chat_id, db_path=None):
    # type: (str, int, Optional[str]) -> None
    """Set (or clear with chat_id=0) transfer_config.transfer_group_chat_id for manager_key."""
    ensure_transfer_tables(db_path)
    mk = str(manager_key or "").strip()
    if not mk:
        return
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO transfer_config(manager_key, transfer_group_chat_id, updated_at)
            VALUES(?,?,?)
            ON CONFLICT(manager_key) DO UPDATE SET
                transfer_group_chat_id=excluded.transfer_group_chat_id, updated_at=excluded.updated_at
            """,
            (mk, int(chat_id or 0), now),
        )
        con.commit()
    finally:
        con.close()

# --- TPILOT TRANSFERS STAGE1 M2.7A END ---


# --- TPILOT PROXY LEASES STAGE2/3 FOUNDATION START ---
# Additive, idempotent schema + CRUD for provider-purchased proxies
# (Proxy-Seller Stage 4+ buy flow will write here). No provider/network code
# lives in this module -- proxy_provider.py owns that. Existing manual-proxy
# columns on `managers` (proxy_host/proxy_port/proxy_username/...) are left
# completely untouched; a lease is only ever *linked* to a manager via the
# new nullable managers.proxy_lease_id column, never merged into the old
# columns automatically. Every helper accepts an explicit db_path override
# (falls back to the module DB_PATH) so callers (main.py's TPILOT_DB_PATH,
# or an isolated test's temp file) are never forced onto the wrong file.
#
# SECURITY: none of these helpers ever print/log a login or password value.
# Callers are responsible for using proxy_parser.mask_proxy() before putting
# any lease data into a Telegram message or a log line.

async def _proxy_leases_table_ready(db: "aiosqlite.Connection") -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_leases(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_key TEXT,
            provider_type TEXT NOT NULL,
            provider_order_id TEXT,
            provider_order_number TEXT,
            provider_proxy_id TEXT,
            proxy_type TEXT DEFAULT 'ipv4',
            scheme TEXT DEFAULT 'socks5',
            host TEXT NOT NULL,
            port INTEGER NOT NULL,
            login TEXT,
            password TEXT,
            expires_at TEXT,
            auto_renew_enabled INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            last_check_at TEXT,
            last_check_ok INTEGER,
            last_check_status TEXT,
            last_renew_attempt_at TEXT,
            last_renew_status TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    for ddl in (
        "CREATE INDEX IF NOT EXISTS proxy_leases_manager_idx ON proxy_leases(manager_key)",
        "CREATE INDEX IF NOT EXISTS proxy_leases_status_idx ON proxy_leases(status)",
        "CREATE INDEX IF NOT EXISTS proxy_leases_expires_idx ON proxy_leases(expires_at)",
        "CREATE INDEX IF NOT EXISTS proxy_leases_provider_proxy_idx ON proxy_leases(provider_type, provider_proxy_id)",
    ):
        try:
            await db.execute(ddl)
        except Exception:
            pass
    # PROXY LIFECYCLE SYNC 20260721: additive bidirectional-reconciliation
    # columns. Each ALTER is guarded/swallowed (idempotent on an already-
    # migrated DB), matching every other lazy migration in this file. These
    # are an INDEPENDENT axis from the legacy `status` column (which the pool
    # UI still derives its free/occupied/assigned labels from); lifecycle_status
    # tracks the desired-vs-observed provider auto_renew reconciliation only.
    #   desired_provider_auto_renew  : 'Y'/'N' computed each sync from health+usage
    #   observed_provider_auto_renew : 'Y'/'N'/'' last value read from proxy/list
    # lifecycle_status vocabulary (see proxy_lifecycle.derive_action):
    #   active_confirmed_on | enable_required | disable_required |
    #   released_off_confirmed | expired
    for ddl in (
        "ALTER TABLE proxy_leases ADD COLUMN lifecycle_status TEXT DEFAULT 'active_confirmed_on'",
        "ALTER TABLE proxy_leases ADD COLUMN desired_provider_auto_renew TEXT",
        "ALTER TABLE proxy_leases ADD COLUMN observed_provider_auto_renew TEXT",
        "ALTER TABLE proxy_leases ADD COLUMN provider_sync_at TEXT",
        "ALTER TABLE proxy_leases ADD COLUMN released_at TEXT",
        "ALTER TABLE proxy_leases ADD COLUMN release_reason TEXT",
    ):
        try:
            await db.execute(ddl)
        except Exception:
            pass
    # Append-only lifecycle audit trail. One row per transition/action; never
    # updated or deleted, so a released proxy's full reconciliation history
    # survives even after it drops out of the active pool. No secrets stored
    # (host/port live on the lease row already; login/password never copied).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_lifecycle_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lease_id INTEGER,
            provider_proxy_id TEXT,
            event TEXT NOT NULL,
            actor TEXT,
            desired TEXT,
            observed TEXT,
            detail TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    try:
        await db.execute("CREATE INDEX IF NOT EXISTS proxy_lifecycle_events_lease_idx ON proxy_lifecycle_events(lease_id)")
    except Exception:
        pass
    # Additive link column on the existing managers table. Nullable, no
    # default meaning required, never rewrites existing manager rows. If the
    # managers table doesn't exist yet in this DB (e.g. a bare test DB),
    # this simply fails and is swallowed -- exactly like every other
    # idempotent ALTER TABLE in this file.
    try:
        await db.execute("ALTER TABLE managers ADD COLUMN proxy_lease_id INTEGER")
    except Exception:
        pass
    try:
        await db.commit()
    except Exception:
        pass


async def proxy_lease_create(
    *,
    provider_type: str,
    host: str,
    port: int,
    manager_key: Optional[str] = None,
    provider_order_id: Optional[str] = None,
    provider_order_number: Optional[str] = None,
    provider_proxy_id: Optional[str] = None,
    proxy_type: str = "ipv4",
    scheme: str = "socks5",
    login: Optional[str] = None,
    password: Optional[str] = None,
    expires_at: Optional[str] = None,
    auto_renew_enabled: bool = False,
    status: str = "active",
    db_path: Optional[str] = None,
) -> int:
    """Insert one proxy lease row. Returns the new lease id. Caller-supplied
    login/password are stored as-is (same trust level as the existing
    manager proxy_username/proxy_password columns) -- never logged here."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        cur = await db.execute(
            "INSERT INTO proxy_leases("
            "manager_key, provider_type, provider_order_id, provider_order_number, provider_proxy_id, "
            "proxy_type, scheme, host, port, login, password, expires_at, auto_renew_enabled, status, "
            "created_at, updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(manager_key).strip() if manager_key else None,
                str(provider_type),
                str(provider_order_id) if provider_order_id else None,
                str(provider_order_number) if provider_order_number else None,
                str(provider_proxy_id) if provider_proxy_id else None,
                str(proxy_type or "ipv4"),
                str(scheme or "socks5"),
                str(host),
                int(port),
                str(login) if login else None,
                str(password) if password else None,
                str(expires_at) if expires_at else None,
                1 if auto_renew_enabled else 0,
                str(status or "active"),
                now, now,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def proxy_lease_get(lease_id: int, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM proxy_leases WHERE id=?", (int(lease_id),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def proxy_lease_get_for_manager(manager_key: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return the manager's currently-linked lease via managers.proxy_lease_id.
    Falls back to the most recent status='active' lease with a matching
    manager_key if the link column/row isn't reachable (e.g. managers table
    absent in an isolated test) -- never raises."""
    dbp = db_path or DB_PATH
    key = str(manager_key or "").strip()
    if not key:
        return None
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        lease_id: Optional[int] = None
        try:
            cur = await db.execute("SELECT proxy_lease_id FROM managers WHERE manager_key=?", (key,))
            row = await cur.fetchone()
            if row and row["proxy_lease_id"]:
                lease_id = int(row["proxy_lease_id"])
        except Exception:
            lease_id = None
        if lease_id:
            cur = await db.execute("SELECT * FROM proxy_leases WHERE id=?", (lease_id,))
            row = await cur.fetchone()
            if row:
                return dict(row)
        cur = await db.execute(
            "SELECT * FROM proxy_leases WHERE manager_key=? AND status='active' ORDER BY id DESC LIMIT 1",
            (key,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def proxy_lease_assign_to_manager(lease_id: int, manager_key: str, *, db_path: Optional[str] = None) -> bool:
    """Link an existing lease to a manager: sets proxy_leases.manager_key
    AND (best-effort) managers.proxy_lease_id. Does NOT touch the existing
    manual proxy_host/proxy_port/... columns -- that mapping is a deliberate
    later step (proxy_lease_to_manager_proxy_fields + a caller decision),
    not an automatic side effect of assignment. Returns False if the lease
    id doesn't exist; the managers-side UPDATE is best-effort (0 rows
    affected if no manager row exists yet) and never raises."""
    dbp = db_path or DB_PATH
    key = str(manager_key or "").strip()
    if not key or not lease_id:
        return False
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        cur = await db.execute("SELECT id FROM proxy_leases WHERE id=?", (int(lease_id),))
        if not await cur.fetchone():
            return False
        await db.execute(
            "UPDATE proxy_leases SET manager_key=?, updated_at=? WHERE id=?",
            (key, now, int(lease_id)),
        )
        try:
            await db.execute(
                "UPDATE managers SET proxy_lease_id=? WHERE manager_key=?",
                (int(lease_id), key),
            )
        except Exception:
            pass
        await db.commit()
        return True


async def proxy_lease_update_check(
    lease_id: int,
    *,
    ok: bool,
    status_text: str = "",
    db_path: Optional[str] = None,
) -> None:
    """Record a read-only liveness check result. `status_text` must already
    be a safe, non-credential string chosen by the caller (this function
    does not scrub it) -- see proxy_provider.py / proxy_parser.mask_proxy
    for building that string safely."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        await db.execute(
            "UPDATE proxy_leases SET last_check_at=?, last_check_ok=?, last_check_status=?, updated_at=? WHERE id=?",
            (now, 1 if ok else 0, str(status_text or "")[:500], now, int(lease_id)),
        )
        await db.commit()


async def proxy_lease_update_renew(
    lease_id: int,
    *,
    status_text: str,
    new_expires_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Record a renewal attempt result. Only bumps expires_at when the
    caller explicitly provides a confirmed new value (never guesses)."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        if new_expires_at:
            await db.execute(
                "UPDATE proxy_leases SET last_renew_attempt_at=?, last_renew_status=?, expires_at=?, updated_at=? WHERE id=?",
                (now, str(status_text or "")[:500], str(new_expires_at), now, int(lease_id)),
            )
        else:
            await db.execute(
                "UPDATE proxy_leases SET last_renew_attempt_at=?, last_renew_status=?, updated_at=? WHERE id=?",
                (now, str(status_text or "")[:500], now, int(lease_id)),
            )
        await db.commit()


# --- PROXY RENEWAL RELIABILITY R2 (verify-after-renew) 20260808 START ---
async def proxy_lease_update_provider_identity(
    lease_id: int,
    *,
    provider_proxy_id: str,
    provider_order_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Resync a lease's provider-assigned identity IN PLACE after the
    provider rotates provider_proxy_id on renewal (observed Proxy-Seller
    behavior). Updates ONLY the provider-identity fields on the EXISTING
    lease row identified by our own internal id -- never touches manager_key
    /status/host/port/credentials/expires_at, and never creates a second
    row (unlike proxy_lease_upsert_from_provider, which is keyed by the NEW
    identity and would insert an orphaned duplicate if used for this)."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        if provider_order_id:
            await db.execute(
                "UPDATE proxy_leases SET provider_proxy_id=?, provider_order_id=?, updated_at=? WHERE id=?",
                (str(provider_proxy_id), str(provider_order_id), now, int(lease_id)),
            )
        else:
            await db.execute(
                "UPDATE proxy_leases SET provider_proxy_id=?, updated_at=? WHERE id=?",
                (str(provider_proxy_id), now, int(lease_id)),
            )
        await db.commit()
# --- PROXY RENEWAL RELIABILITY R2 (verify-after-renew) 20260808 END ---


async def proxy_lease_list_expiring(
    before: str,
    *,
    only_active: bool = True,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List leases with a non-empty expires_at <= `before` (an ISO string in
    the same format as created_at/updated_at -- caller picks the horizon,
    e.g. now+24h, and does the timezone math)."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        sql = "SELECT * FROM proxy_leases WHERE COALESCE(expires_at,'') != '' AND expires_at <= ?"
        params: List[Any] = [str(before)]
        if only_active:
            sql += " AND status='active'"
        sql += " ORDER BY expires_at ASC"
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def proxy_lease_set_status(lease_id: int, status: str, *, db_path: Optional[str] = None) -> None:
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        await db.execute(
            "UPDATE proxy_leases SET status=?, updated_at=? WHERE id=?",
            (str(status or ""), now, int(lease_id)),
        )
        await db.commit()


# --- TPILOT PROXY POOL STAGE6 P1 (storage helpers only) START ---
# Additive, idempotent. No schema changes -- free/assigned/orphaned/expired
# status is derived on read by the caller (main.py), not stored here.

async def proxy_lease_list_all(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return every lease row, oldest first. Raw storage helper -- no
    masking here (same convention as proxy_lease_list_expiring); UI/
    controller callers are responsible for masking secrets before any
    Telegram message or log line."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM proxy_leases ORDER BY id ASC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def proxy_lease_get_by_provider_proxy_id(
    provider_type: str,
    provider_proxy_id: str,
    *,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Look up a lease by provider identity (provider_type +
    provider_proxy_id) -- the correct key for sync/upsert against the
    provider's own proxy list. Deliberately NOT the internal lease id.
    Returns None for an empty provider_proxy_id (never matches on an
    empty string)."""
    dbp = db_path or DB_PATH
    ptype = str(provider_type or "").strip()
    pid = str(provider_proxy_id or "").strip()
    if not pid:
        return None
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM proxy_leases WHERE provider_type=? AND provider_proxy_id=? LIMIT 1",
            (ptype, pid),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def proxy_lease_unassign(lease_id: int, *, db_path: Optional[str] = None) -> bool:
    """Detach a lease from its manager: clears proxy_leases.manager_key
    and (best-effort) the corresponding managers.proxy_lease_id link. The
    lease row is NEVER deleted, and provider_order_id/provider_proxy_id/
    host/port/login/password are NEVER touched -- the proxy stays in the
    pool, ready to be reassigned; the provider-side proxy is never
    touched either. Disabling the manager's own proxy_host/proxy_password
    fields is deliberately out of scope here (P3). Returns False if the
    lease id doesn't exist."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM proxy_leases WHERE id=?", (int(lease_id),))
        row = await cur.fetchone()
        if not row:
            return False
        old_manager_key = str(row["manager_key"] or "").strip()
        await db.execute(
            "UPDATE proxy_leases SET manager_key=NULL, updated_at=? WHERE id=?",
            (now, int(lease_id)),
        )
        try:
            if old_manager_key:
                await db.execute(
                    "UPDATE managers SET proxy_lease_id=NULL WHERE proxy_lease_id=? OR manager_key=?",
                    (int(lease_id), old_manager_key),
                )
            else:
                await db.execute(
                    "UPDATE managers SET proxy_lease_id=NULL WHERE proxy_lease_id=?",
                    (int(lease_id),),
                )
        except Exception:
            pass
        await db.commit()
        return True


async def proxy_lease_upsert_from_provider(
    *,
    provider_type: str,
    provider_proxy_id: str,
    host: str,
    port: int,
    login: Optional[str] = None,
    password: Optional[str] = None,
    scheme: str = "socks5",
    proxy_type: str = "ipv4",
    provider_order_id: Optional[str] = None,
    provider_order_number: Optional[str] = None,
    expires_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Idempotent sync of one proxy from the provider's own list into the
    local pool, keyed by (provider_type, provider_proxy_id) -- NEVER the
    internal lease id. If a lease already exists for this provider
    identity, only the provider-sourced fields (host/port/login/password/
    scheme/proxy_type/provider_order_id/provider_order_number/expires_at)
    are refreshed; manager_key and status are local pool state and are
    NEVER overwritten by a sync. If no lease exists yet, a new one is
    created with manager_key=NULL, status='active' (unassigned/free).
    Never deletes anything -- a local lease absent from a given provider
    response is simply left untouched by the caller's sync loop."""
    dbp = db_path or DB_PATH
    ptype = str(provider_type or "").strip()
    pid = str(provider_proxy_id or "").strip()
    if not pid:
        raise ValueError("provider_proxy_id is required")
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        cur = await db.execute(
            "SELECT id, login, password, expires_at FROM proxy_leases WHERE provider_type=? AND provider_proxy_id=? LIMIT 1",
            (ptype, pid),
        )
        existing = await cur.fetchone()
        if existing:
            lease_id = int(existing[0])
            # NB-1 fix (Proxy Renewal Reliability review, 20260808): a
            # periodic provider list-refresh must NEVER blank out already-
            # known credentials just because THIS particular list response
            # happened to omit login/password -- an empty/None value from
            # the provider means "unknown this round", not "credentials
            # were removed". Only a genuinely non-empty new value replaces
            # the existing one; manager_key/status/assignment are untouched
            # here regardless (this UPDATE never sets them).
            new_login = str(login) if login else None
            new_password = str(password) if password else None
            final_login = new_login if new_login else existing[1]
            final_password = new_password if new_password else existing[2]
            # NB-1 follow-up fix (independent re-review correction, 20260809):
            # the SAME "empty this round means unknown, not removed" rule now
            # also applies to expires_at -- a provider list response that
            # omits (or blanks) the expiry must never erase an already-known
            # one, since that silently drops the lease out of warn/autorenew
            # eligibility (_ppool_parse_expires_at(None) -> None -> "unknown
            # expiry, skip", the same fail-safe convention used throughout
            # this subsystem). Only a genuinely non-empty new value replaces
            # the existing one; never guessed/fabricated.
            new_expires_at = str(expires_at).strip() if expires_at and str(expires_at).strip() else None
            final_expires_at = new_expires_at if new_expires_at else existing[3]
            await db.execute(
                "UPDATE proxy_leases SET host=?, port=?, login=?, password=?, scheme=?, proxy_type=?, "
                "provider_order_id=?, provider_order_number=?, expires_at=?, updated_at=? WHERE id=?",
                (
                    str(host), int(port),
                    final_login,
                    final_password,
                    str(scheme or "socks5"), str(proxy_type or "ipv4"),
                    str(provider_order_id) if provider_order_id else None,
                    str(provider_order_number) if provider_order_number else None,
                    final_expires_at,
                    now, lease_id,
                ),
            )
            await db.commit()
            return lease_id
        cur = await db.execute(
            "INSERT INTO proxy_leases("
            "manager_key, provider_type, provider_order_id, provider_order_number, provider_proxy_id, "
            "proxy_type, scheme, host, port, login, password, expires_at, auto_renew_enabled, status, "
            "created_at, updated_at"
            ") VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,0,'active',?,?)",
            (
                ptype,
                str(provider_order_id) if provider_order_id else None,
                str(provider_order_number) if provider_order_number else None,
                pid,
                str(proxy_type or "ipv4"), str(scheme or "socks5"),
                str(host), int(port),
                str(login) if login else None,
                str(password) if password else None,
                str(expires_at) if expires_at else None,
                now, now,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)

# --- TPILOT PROXY POOL STAGE6 P1 (storage helpers only) END ---


# --- TPILOT PROXY POOL STAGE6.1 (renew notify dedupe + auto-renew toggle) START ---
# Additive, idempotent. No schema changes to proxy_leases beyond reusing
# the EXISTING auto_renew_enabled column (already present since Stage2/3).
# The new proxy_renew_notify_log table exists purely to dedupe admin
# notifications (warnings + auto-renew attempts) per lease/date/slot --
# it is NOT a renewal-attempt record (that stays last_renew_attempt_at/
# last_renew_status on proxy_leases itself).

async def _proxy_renew_notify_log_table_ready(db: "aiosqlite.Connection") -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_renew_notify_log(
            lease_id INTEGER NOT NULL,
            notify_date TEXT NOT NULL,
            slot TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY(lease_id, notify_date, slot)
        )
        """
    )
    try:
        await db.commit()
    except Exception:
        pass


async def proxy_renew_notify_log_ready(*, db_path: Optional[str] = None) -> None:
    """Idempotent schema init for proxy_renew_notify_log -- callable on
    its own (mirrors _proxy_leases_table_ready's public-wrapper style)."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renew_notify_log_table_ready(db)


async def proxy_renew_notify_mark_once(
    lease_id: int,
    notify_date: str,
    slot: str,
    *,
    db_path: Optional[str] = None,
) -> bool:
    """Record that a notification for this (lease_id, notify_date, slot)
    is about to be sent. Returns True only the FIRST time for that exact
    triple (INSERT succeeds); returns False if it was already recorded
    (PRIMARY KEY conflict) -- the caller must treat False as "already
    notified, do not send again". Never raises.

    R5d (Proxy Renewal Reliability, 20260808): the return value stays
    False either way (never send on uncertainty -- the existing convention
    throughout this subsystem, e.g. _ppool_parse_expires_at returning None
    rather than guessing), but a genuine sqlite3.IntegrityError (the
    expected/common "already sent" case) is now distinguished from any
    OTHER failure (locked DB, disk I/O, ...), which is logged instead of
    being silently swallowed -- a real DB problem should be visible, even
    though it still safely suppresses the send rather than risking a
    duplicate."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_renew_notify_log_table_ready(db)
        try:
            await db.execute(
                "INSERT INTO proxy_renew_notify_log(lease_id, notify_date, slot, sent_at) VALUES(?,?,?,?)",
                (int(lease_id), str(notify_date or ""), str(slot or ""), now),
            )
            await db.commit()
            return True
        except sqlite3.IntegrityError:
            # Expected/common case: this (lease_id, notify_date, slot) was
            # already marked -- do not send a duplicate.
            return False
        except Exception as exc:
            try:
                print(f"[storage] proxy_renew_notify_mark_once: unexpected DB error (suppressing send, not a normal dedupe hit): {exc!r}")
            except Exception:
                pass
            return False


async def proxy_renew_notify_purge_old(before_date: str, *, db_path: Optional[str] = None) -> int:
    """Housekeeping: delete notify-log rows older than `before_date`
    (an ISO YYYY-MM-DD string, exclusive comparison via <). Returns the
    number of rows deleted. Never raises."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renew_notify_log_table_ready(db)
        try:
            cur = await db.execute(
                "DELETE FROM proxy_renew_notify_log WHERE notify_date < ?",
                (str(before_date or ""),),
            )
            await db.commit()
            return int(cur.rowcount or 0)
        except Exception:
            return 0


async def proxy_lease_set_auto_renew(lease_id: int, enabled: bool, *, db_path: Optional[str] = None) -> bool:
    """Toggle proxy_leases.auto_renew_enabled for one lease. Pure storage
    write -- never calls the provider, never spends. Returns False if the
    lease id doesn't exist."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        cur = await db.execute("SELECT id FROM proxy_leases WHERE id=?", (int(lease_id),))
        if not await cur.fetchone():
            return False
        await db.execute(
            "UPDATE proxy_leases SET auto_renew_enabled=?, updated_at=? WHERE id=?",
            (1 if enabled else 0, now, int(lease_id)),
        )
        await db.commit()
        return True

# --- TPILOT PROXY POOL STAGE6.1 END ---


# --- TPILOT PROXY LIFECYCLE SYNC 20260721 BEGIN ---
# Storage layer for the bidirectional desired-vs-observed provider auto_renew
# reconciliation. All writes are short, single-statement, and NEVER call the
# provider or spend. lifecycle_status is a CAS axis independent of the legacy
# `status` column. proxy_lifecycle_events is append-only audit.

_PROXY_LIFECYCLE_STATES = (
    "active_confirmed_on",   # desired Y, observed Y
    "enable_required",       # desired Y, observed N -> owner must ENABLE in cabinet
    "disable_required",      # desired N, observed Y -> owner must DISABLE in cabinet
    "released_off_confirmed",  # desired N, observed N -> dropped from active pool
    "expired",               # released and past expires_at
)


async def proxy_lifecycle_event_add(
    lease_id: Optional[int],
    event: str,
    *,
    provider_proxy_id: Optional[str] = None,
    actor: str = "",
    desired: Optional[str] = None,
    observed: Optional[str] = None,
    detail: str = "",
    db_path: Optional[str] = None,
) -> int:
    """Append one row to the proxy_lifecycle_events audit trail. Never
    updates/deletes; never stores secrets. Returns the new event id."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        cur = await db.execute(
            "INSERT INTO proxy_lifecycle_events("
            "lease_id, provider_proxy_id, event, actor, desired, observed, detail, created_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                int(lease_id) if lease_id is not None else None,
                str(provider_proxy_id) if provider_proxy_id else None,
                str(event or ""),
                str(actor or ""),
                str(desired) if desired else None,
                str(observed) if observed else None,
                str(detail or ""),
                now,
            ),
        )
        await db.commit()
        return int(cur.lastrowid)


async def proxy_lease_set_provider_state(
    lease_id: int,
    *,
    desired: Optional[str] = None,
    observed: Optional[str] = None,
    provider_sync_at: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """Write the last-computed desired and/or last-observed provider auto_renew
    values (and provider_sync_at) for one lease. Pure storage write; no
    provider call. Only the arguments that are not None are updated. Returns
    False if the lease id doesn't exist."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    sets: List[str] = []
    vals: List[Any] = []
    if desired is not None:
        sets.append("desired_provider_auto_renew=?")
        vals.append(str(desired))
    if observed is not None:
        sets.append("observed_provider_auto_renew=?")
        vals.append(str(observed))
    if provider_sync_at is not None:
        sets.append("provider_sync_at=?")
        vals.append(str(provider_sync_at))
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(now)
    vals.append(int(lease_id))
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        cur = await db.execute("SELECT id FROM proxy_leases WHERE id=?", (int(lease_id),))
        if not await cur.fetchone():
            return False
        await db.execute(
            "UPDATE proxy_leases SET " + ", ".join(sets) + " WHERE id=?",
            tuple(vals),
        )
        await db.commit()
        return True


async def proxy_lease_set_lifecycle(
    lease_id: int,
    from_state: Optional[str],
    to_state: str,
    *,
    desired: Optional[str] = None,
    observed: Optional[str] = None,
    released_at: Optional[str] = None,
    release_reason: Optional[str] = None,
    db_path: Optional[str] = None,
) -> bool:
    """CAS transition of proxy_leases.lifecycle_status. When from_state is not
    None the UPDATE only applies if the row is still in that state (rowcount==1
    proves this connection won the transition) -- so a duplicate/racing call is
    a safe no-op returning False. from_state=None forces an unconditional set.
    Also writes desired/observed/released_at/release_reason when supplied.
    Never calls the provider, never spends."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    sets = ["lifecycle_status=?"]
    vals: List[Any] = [str(to_state)]
    if desired is not None:
        sets.append("desired_provider_auto_renew=?")
        vals.append(str(desired))
    if observed is not None:
        sets.append("observed_provider_auto_renew=?")
        vals.append(str(observed))
    if released_at is not None:
        sets.append("released_at=?")
        vals.append(str(released_at))
    if release_reason is not None:
        sets.append("release_reason=?")
        vals.append(str(release_reason))
    sets.append("updated_at=?")
    vals.append(now)
    sql = "UPDATE proxy_leases SET " + ", ".join(sets) + " WHERE id=?"
    vals.append(int(lease_id))
    if from_state is not None:
        sql += " AND lifecycle_status=?"
        vals.append(str(from_state))
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        cur = await db.execute(sql, tuple(vals))
        await db.commit()
        return int(cur.rowcount or 0) == 1


async def proxy_lifecycle_list_actionable(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return every lease currently in an actionable mismatch state
    (enable_required or disable_required), oldest first. Read-only."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM proxy_leases WHERE lifecycle_status IN ('enable_required','disable_required') "
            "ORDER BY id ASC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def proxy_lifecycle_events_for_lease(lease_id: int, *, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return the append-only audit history for one lease, oldest first."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_leases_table_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM proxy_lifecycle_events WHERE lease_id=? ORDER BY id ASC",
            (int(lease_id),),
        )
        return [dict(r) for r in await cur.fetchall()]

# --- TPILOT PROXY LIFECYCLE SYNC 20260721 END ---


# --- TPILOT PROXY RENEWAL (TPilot-managed prolong) 20260721 BEGIN ---
# Durable per-attempt audit + idempotency for TPilot-managed renewal
# (prolong/make). Website provider Auto-renewal stays OFF permanently; TPilot
# renews the proxies it needs itself. This layer NEVER calls the provider and
# NEVER spends -- it only records the state machine around the single existing
# spend executor (main.py _prenew_execute_renewal). One row per renewal
# attempt; a partial-unique index enforces "one active op per lease" so a
# scheduler double-fire or a restart can never double-spend. proxy_renewal_config
# is a single-row global automation state (default automation_enabled=0 -- the
# loop does nothing until an explicit one-time admin consent).

_PROXY_RENEWAL_OP_STATES = (
    "pending", "calc_done", "confirmed", "make_pending", "success", "failed", "skipped",
)
_PROXY_RENEWAL_ACTIVE_STATES = ("pending", "calc_done", "confirmed", "make_pending")


async def _proxy_renewal_tables_ready(db: "aiosqlite.Connection") -> None:
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_renewal_ops(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lease_id INTEGER,
            provider_proxy_id TEXT,
            idempotency_key TEXT,
            source TEXT NOT NULL DEFAULT 'manual',
            status TEXT NOT NULL DEFAULT 'pending',
            period_id TEXT,
            calc_price TEXT,
            calc_total TEXT,
            currency TEXT,
            calc_at TEXT,
            make_at TEXT,
            expires_before TEXT,
            expires_after TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            actor_user_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    for ddl in (
        # idempotency_key is unique across ALL ops -- (provider_proxy_id:expires_before:period_id)
        # so a restart or double-tick that recomputes the SAME renewal target
        # cannot create a second op (INSERT raises IntegrityError, caught by the
        # CAS-create helper as "already in flight" -> skip).
        "CREATE UNIQUE INDEX IF NOT EXISTS proxy_renewal_ops_idem_idx ON proxy_renewal_ops(idempotency_key)",
        # one ACTIVE op per lease (partial unique) -- a second attempt while one
        # is still in flight is rejected, preventing concurrent prolong/make.
        "CREATE UNIQUE INDEX IF NOT EXISTS proxy_renewal_ops_active_lease_idx "
        "ON proxy_renewal_ops(lease_id) WHERE status IN ('pending','calc_done','confirmed','make_pending')",
        "CREATE INDEX IF NOT EXISTS proxy_renewal_ops_status_idx ON proxy_renewal_ops(status)",
        "CREATE INDEX IF NOT EXISTS proxy_renewal_ops_lease_idx ON proxy_renewal_ops(lease_id)",
    ):
        try:
            await db.execute(ddl)
        except Exception:
            pass
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_renewal_config(
            id INTEGER PRIMARY KEY CHECK (id = 1),
            automation_enabled INTEGER NOT NULL DEFAULT 0,
            enabled_by_user_id INTEGER,
            enabled_at TEXT,
            paused INTEGER NOT NULL DEFAULT 0,
            emergency_stop INTEGER NOT NULL DEFAULT 0,
            max_amount_per_op TEXT,
            max_daily_spend TEXT,
            min_balance_warn TEXT,
            renewal_period_id TEXT NOT NULL DEFAULT '1m',
            lead_days INTEGER NOT NULL DEFAULT 2,
            updated_at TEXT
        )
        """
    )
    # Seed the single config row exactly once (automation OFF by default).
    await db.execute(
        "INSERT OR IGNORE INTO proxy_renewal_config(id, automation_enabled, paused, emergency_stop, "
        "renewal_period_id, lead_days, updated_at) VALUES(1,0,0,0,'1m',2,?)",
        (_now_iso(),),
    )
    # One-time migration (2026-07-21): the seeded default changed 7 -> 2, but
    # INSERT OR IGNORE above never touches an already-seeded row, so a
    # pre-existing production row stays at the OLD default (7) forever unless
    # explicitly flipped once. lead_days_migrated_7to2 gates this so it fires
    # EXACTLY once per DB and never re-asserts itself if an admin deliberately
    # sets lead_days back to 7 afterwards; any genuinely custom value (1, 3,
    # ...) never matches "lead_days=7" and is left untouched.
    try:
        await db.execute("ALTER TABLE proxy_renewal_config ADD COLUMN lead_days_migrated_7to2 INTEGER")
    except Exception:
        pass
    await db.execute(
        "UPDATE proxy_renewal_config SET lead_days=2, lead_days_migrated_7to2=1, updated_at=? "
        "WHERE id=1 AND lead_days=7 AND (lead_days_migrated_7to2 IS NULL OR lead_days_migrated_7to2=0)",
        (_now_iso(),),
    )
    # Single-row dedup state for the low-balance AdminBot alert (2026-07-21).
    # below_threshold + last_alert_at implement "alert immediately on crossing,
    # then at most once per 24h while still below, reset once recovered" --
    # purely local bookkeeping, never touches the provider or spends.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_balance_alert_state(
            id INTEGER PRIMARY KEY CHECK (id = 1),
            below_threshold INTEGER NOT NULL DEFAULT 0,
            last_alert_at TEXT,
            updated_at TEXT
        )
        """
    )
    await db.execute(
        "INSERT OR IGNORE INTO proxy_balance_alert_state(id, below_threshold, last_alert_at, updated_at) "
        "VALUES(1,0,NULL,?)",
        (_now_iso(),),
    )
    # Additive migration (forward-fix P2, post-incident review 2026-07-26):
    # persists the last-KNOWN-GOOD Proxy-Seller balance so the AdminBot root
    # header can show it across a process restart without a fresh provider
    # call and without requiring the operator to open the Proxy screen first
    # -- previously the balance lived ONLY in an in-memory panel_bot.py
    # cache that reset on every restart. Same idempotent try/except ADD
    # COLUMN pattern as lead_days_migrated_7to2 above (SQLite has no ADD
    # COLUMN IF NOT EXISTS). NULL on every pre-existing row until the next
    # successful fetch -- read side (_pb_cached_proxy_balance_str) already
    # treats a missing/non-numeric value as the existing honest "—".
    for _col, _decl in (
        ("balance_usd", "REAL"),
        ("balance_fetched_at", "TEXT"),
        ("balance_changed_at", "TEXT"),
    ):
        try:
            await db.execute(f"ALTER TABLE proxy_balance_alert_state ADD COLUMN {_col} {_decl}")
        except Exception:
            pass
    try:
        await db.commit()
    except Exception:
        pass


def _proxy_renewal_canonical_date(value: Any) -> str:
    """NB-2 fix (Proxy Renewal Reliability review, 20260808): normalize the
    DATE portion of an expires_at-like value to canonical YYYY-MM-DD, so the
    three formats this project's expires_at has ever been observed in --
    'YYYY-MM-DD', 'YYYY-MM-DDTHH:MM:SS', and 'DD.MM.YYYY' -- all produce the
    SAME idempotency-key date for the SAME calendar date. Without this, a
    provider/UI textual-format wobble around the SAME renewal cycle could
    silently mint a DIFFERENT idempotency key and bypass the one-spend-per-
    cycle guard. Fail-safe: an unrecognized format is returned unchanged
    (stripped) rather than guessed at -- still fully deterministic for the
    SAME raw input (repeat calls with the identical unparseable string still
    collide), it simply isn't normalized across formats it can't confidently
    parse, which is safer than risking a wrong guess."""
    import re
    s = str(value or "").strip()
    if not s:
        return s
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return s


def proxy_renewal_idempotency_key(provider_proxy_id: Any, expires_before: Any, period_id: Any) -> str:
    """Canonical idempotency key for one renewal target. The SAME proxy renewed
    for the SAME period from the SAME pre-renewal expiry is one logical op --
    a restart/double-tick that recomputes it collides on the UNIQUE index and
    is skipped, so prolong/make is never called twice for it. Uses only the
    canonical DATE part of expires_before (see _proxy_renewal_canonical_date)
    so neither an intra-day time component NOR a different textual date
    format (NB-2) can split one logical renewal cycle into two keys."""
    exp_date = _proxy_renewal_canonical_date(expires_before)
    return f"{str(provider_proxy_id or '')}:{exp_date}:{str(period_id or '')}"


async def proxy_renewal_op_create(
    *,
    lease_id: int,
    provider_proxy_id: Any,
    idempotency_key: str,
    source: str = "manual",
    status: str = "pending",
    period_id: Any = None,
    expires_before: Any = None,
    actor_user_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[int]:
    """CAS-create one renewal op. Returns the new op id, or None if creation
    was rejected because (a) the idempotency_key already exists, or (b) the
    lease already has an ACTIVE op (partial-unique index) -- either way the
    caller must treat None as 'already in flight, do NOT spend'. Pure storage;
    never calls the provider."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        try:
            cur = await db.execute(
                "INSERT INTO proxy_renewal_ops("
                "lease_id, provider_proxy_id, idempotency_key, source, status, period_id, "
                "expires_before, actor_user_id, created_at, updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    int(lease_id),
                    str(provider_proxy_id) if provider_proxy_id is not None else None,
                    str(idempotency_key),
                    str(source or "manual"),
                    str(status or "pending"),
                    str(period_id) if period_id is not None else None,
                    str(expires_before) if expires_before else None,
                    int(actor_user_id) if actor_user_id is not None else None,
                    now, now,
                ),
            )
            await db.commit()
            return int(cur.lastrowid)
        except Exception:
            # IntegrityError from either unique index -> already in flight.
            return None


async def proxy_renewal_op_advance(
    op_id: int,
    to_status: str,
    *,
    from_status: Optional[str] = None,
    calc_price: Any = None,
    calc_total: Any = None,
    currency: Any = None,
    calc_at: Any = None,
    make_at: Any = None,
    expires_after: Any = None,
    last_error: Any = None,
    bump_retry: bool = False,
    db_path: Optional[str] = None,
) -> bool:
    """CAS status advance for one renewal op. When from_status is given the
    UPDATE only applies if the op is still in that state (rowcount==1 -> this
    caller won). Writes only the supplied fields. Pure storage; no provider
    call, no spend."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    sets = ["status=?"]
    vals: List[Any] = [str(to_status)]
    if calc_price is not None:
        sets.append("calc_price=?"); vals.append(str(calc_price))
    if calc_total is not None:
        sets.append("calc_total=?"); vals.append(str(calc_total))
    if currency is not None:
        sets.append("currency=?"); vals.append(str(currency))
    if calc_at is not None:
        sets.append("calc_at=?"); vals.append(str(calc_at))
    if make_at is not None:
        sets.append("make_at=?"); vals.append(str(make_at))
    if expires_after is not None:
        sets.append("expires_after=?"); vals.append(str(expires_after))
    if last_error is not None:
        sets.append("last_error=?"); vals.append(str(last_error))
    if bump_retry:
        sets.append("retry_count=retry_count+1")
    sets.append("updated_at=?"); vals.append(now)
    sql = "UPDATE proxy_renewal_ops SET " + ", ".join(sets) + " WHERE id=?"
    vals.append(int(op_id))
    if from_status is not None:
        sql += " AND status=?"; vals.append(str(from_status))
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        cur = await db.execute(sql, tuple(vals))
        await db.commit()
        return int(cur.rowcount or 0) == 1


async def proxy_renewal_op_delete_pending(op_id: int, *, db_path: Optional[str] = None) -> bool:
    """Crash-window fix (independent re-review correction, 20260809): free
    the idempotency_key of an op that is PROVABLY pre-spend -- CAS-guarded
    DELETE that only ever removes a row still in status='pending' (the
    RESERVED stage written by proxy_renewal_op_create before the caller has
    durably marked SPEND_STARTED via proxy_renewal_op_advance(...,
    'make_pending', from_status='pending')). Since the ONLY writer that ever
    moves a row OUT of 'pending' is that one atomic marker write, a row still
    found in 'pending' -- whether right after a same-process spend-marker
    write failure or after a restart that finds it orphaned -- is guaranteed
    to have never reached prolong_make. proxy_renewal_ops_idem_idx is a
    GLOBAL unique index (not scoped to active statuses), so this DELETE is
    the only way to release that idempotency_key for a genuine pre-spend
    reservation; callers must write an audit trail (proxy_lifecycle_events)
    BEFORE calling this, since the row itself is gone afterward. Never
    touches a 'make_pending' (or later) row -- those are potentially-spent
    and must go through the ordinary provider-refresh reconcile path
    instead, never a delete. Returns True only if a row was actually
    deleted."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        cur = await db.execute(
            "DELETE FROM proxy_renewal_ops WHERE id=? AND status='pending'", (int(op_id),),
        )
        await db.commit()
        return int(cur.rowcount or 0) == 1


async def proxy_renewal_op_get(op_id: int, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM proxy_renewal_ops WHERE id=?", (int(op_id),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def proxy_renewal_op_active_for_lease(lease_id: int, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The lease's currently-active op (if any), used for restart reconcile."""
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM proxy_renewal_ops WHERE lease_id=? AND status IN "
            "('pending','calc_done','confirmed','make_pending') ORDER BY id DESC LIMIT 1",
            (int(lease_id),),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def proxy_renewal_ops_by_status(statuses: Sequence[str], *, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    dbp = db_path or DB_PATH
    st = [str(s) for s in (statuses or []) if s]
    if not st:
        return []
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        db.row_factory = aiosqlite.Row
        placeholders = ",".join("?" for _ in st)
        cur = await db.execute(
            f"SELECT * FROM proxy_renewal_ops WHERE status IN ({placeholders}) ORDER BY id DESC",
            tuple(st),
        )
        return [dict(r) for r in await cur.fetchall()]


async def proxy_renewal_daily_spend_total(day_prefix: str, *, db_path: Optional[str] = None) -> float:
    """Sum of calc_total for SUCCESSFUL ops whose make_at falls on the given
    YYYY-MM-DD Kyiv day -- the running daily-spend guard input. Best-effort
    float parse; unparseable totals count as 0."""
    dbp = db_path or DB_PATH
    prefix = str(day_prefix or "")[:10]
    if not prefix:
        return 0.0
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT calc_total FROM proxy_renewal_ops WHERE status='success' AND substr(COALESCE(make_at,''),1,10)=?",
            (prefix,),
        )
        rows = await cur.fetchall()
    total = 0.0
    for r in rows:
        try:
            total += float(str(r["calc_total"]).replace(",", ".").strip())
        except Exception:
            pass
    return total


async def proxy_renewal_config_get(*, db_path: Optional[str] = None) -> Dict[str, Any]:
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM proxy_renewal_config WHERE id=1")
        row = await cur.fetchone()
        return dict(row) if row else {}


async def proxy_renewal_config_set(*, db_path: Optional[str] = None, **fields: Any) -> bool:
    """Update the single config row. Only the passed keys are written. Never
    calls the provider, never spends -- config only."""
    allowed = {
        "automation_enabled", "enabled_by_user_id", "enabled_at", "paused", "emergency_stop",
        "max_amount_per_op", "max_daily_spend", "min_balance_warn", "renewal_period_id", "lead_days",
    }
    sets: List[str] = []
    vals: List[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(_now_iso())
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        await db.execute("UPDATE proxy_renewal_config SET " + ", ".join(sets) + " WHERE id=1", tuple(vals))
        await db.commit()
        return True


async def proxy_balance_alert_state_get(*, db_path: Optional[str] = None) -> Dict[str, Any]:
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM proxy_balance_alert_state WHERE id=1")
        row = await cur.fetchone()
        return dict(row) if row else {}


async def proxy_balance_alert_state_set(*, db_path: Optional[str] = None, **fields: Any) -> bool:
    """Update the single low-balance-alert dedup row. Only the passed keys are
    written. Local bookkeeping only -- never calls the provider, never spends."""
    allowed = {"below_threshold", "last_alert_at"}
    sets: List[str] = []
    vals: List[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(_now_iso())
    dbp = db_path or DB_PATH
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        await db.execute("UPDATE proxy_balance_alert_state SET " + ", ".join(sets) + " WHERE id=1", tuple(vals))
        await db.commit()
        return True


async def proxy_balance_snapshot_write(*, value: float, db_path: Optional[str] = None) -> bool:
    """Forward-fix P2 (post-incident review 2026-07-26): persists the last
    SUCCESSFULLY-fetched Proxy-Seller balance so the AdminBot root header
    can display it across a process restart, without the operator needing
    to open the Proxy screen first and without a second provider-polling
    timer -- the ONLY caller is main.py's existing, already-scheduled
    _renewal_balance_alert_tick, on a fetch that returned a real number.
    A failed/timed-out/unparseable fetch must simply never call this
    function -- the previous row is untouched by construction, so a
    provider outage can never erase the last-known-good value.

    One connection, one SELECT, one UPDATE, one commit -- a single
    transaction; no other writer touches these three columns, so no
    additional lock is needed beyond SQLite's own transaction. fetched_at
    always advances on every successful call; changed_at advances ONLY
    when the new value differs (rounded to cents) from the previously
    stored one -- an unchanged balance across many ticks keeps its original
    changed_at, a genuinely different balance gets a fresh one."""
    dbp = db_path or DB_PATH
    now = _now_iso()
    rounded = round(float(value), 2)
    async with _db_conn(dbp) as db:
        await _proxy_renewal_tables_ready(db)
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT balance_usd, balance_changed_at FROM proxy_balance_alert_state WHERE id=1"
        )
        row = await cur.fetchone()
        prev_value = row["balance_usd"] if row else None
        prev_changed_at = row["balance_changed_at"] if row else None
        same_value = prev_value is not None and round(float(prev_value), 2) == rounded
        changed_at = prev_changed_at if (same_value and prev_changed_at) else now
        await db.execute(
            "UPDATE proxy_balance_alert_state SET balance_usd=?, balance_fetched_at=?, "
            "balance_changed_at=?, updated_at=? WHERE id=1",
            (rounded, now, changed_at, now),
        )
        await db.commit()
        return True

# --- TPILOT PROXY RENEWAL (TPilot-managed prolong) 20260721 END ---


def proxy_lease_to_manager_proxy_fields(lease: Dict[str, Any]) -> Dict[str, Any]:
    """Pure transform (no DB access): convert a proxy_leases row into the
    field-name shape the EXISTING manual manager proxy columns use
    (proxy_type/proxy_host/proxy_port/proxy_username/proxy_password/
    proxy_enabled/proxy_mode -- see main.py _build_telethon_proxy_from_row).
    Does not write anything and is not called from anywhere yet in this
    stage; a later stage decides when/how to apply this via
    manager_set_fields()/_tpag_registry_set_fields()."""
    if not lease:
        return {}
    port_raw = lease.get("port")
    try:
        port_i = int(port_raw) if port_raw not in (None, "") else None
    except Exception:
        port_i = None
    return {
        "proxy_type": str(lease.get("scheme") or "socks5"),
        "proxy_host": str(lease.get("host") or ""),
        "proxy_port": port_i,
        "proxy_username": lease.get("login") or "",
        "proxy_password": lease.get("password") or "",
        "proxy_enabled": 1,
        "proxy_mode": "proxy",
    }

# --- TPILOT PROXY LEASES STAGE2/3 FOUNDATION END ---


# --- TPILOT TDATA/SESSION IMPORT (durable operation) BEGIN ---
#
# Durable state machine for the "upload tdata/session archive" third AdminBot
# authorization method. Mirrors the manager_replacements CAS model above:
# synchronous sqlite3 on the shared QUEUE_DB_PATH (via _bsl_connect), exactly
# one row per operation_id, forward-only stage transitions performed as a
# compare-and-set UPDATE. status is the coarse lifecycle; stage is the detailed
# pipeline. A partial unique index enforces "one active operation per manager".
#
# SECURITY: this table stores ONLY safe metadata. It MUST NEVER hold an
# auth_key, a session-database blob, a 2FA password, a local Telegram passcode,
# an API hash, a full phone number, or a proxy password. Callers are
# responsible for masking (masked_phone) and for keeping error_text generic.
#
# This block intentionally does NOT import the tdata_import package (the
# package's service layer imports storage, not the other way round) -- it stays
# a self-contained low-level layer.

_TDIMPORT_STAGES = (
    "created", "manager_prepared", "source_assigned", "proxy_assigned",
    "proxy_verified", "archive_uploaded", "archive_validated", "session_detected",
    "session_selected", "tdata_converted", "session_validated", "identity_checking",
    "identity_verified", "session_installing", "session_installed",
    "runtime_starting", "runtime_running", "completed",
)

# Forward-only edges. session_detected forks to the ready-session path or the
# tdata-conversion path, which re-merge at session_validated. Kept in sync with
# tdata_import/models.py:Stage.TRANSITIONS.
_TDIMPORT_STAGE_TRANSITIONS = {
    "created": ("manager_prepared",),
    "manager_prepared": ("source_assigned",),
    "source_assigned": ("proxy_assigned",),
    "proxy_assigned": ("proxy_verified",),
    "proxy_verified": ("archive_uploaded",),
    "archive_uploaded": ("archive_validated",),
    "archive_validated": ("session_detected",),
    "session_detected": ("session_selected", "tdata_converted"),
    "session_selected": ("session_validated",),
    "tdata_converted": ("session_validated",),
    "session_validated": ("identity_checking",),
    "identity_checking": ("identity_verified",),
    "identity_verified": ("session_installing",),
    "session_installing": ("session_installed",),
    "session_installed": ("runtime_starting",),
    "runtime_starting": ("runtime_running",),
    "runtime_running": ("completed",),
    "completed": (),
}

# Columns a transition/update may set. Whitelisted -- never taken from
# caller-supplied strings into SQL. status/stage/updated_at/created_at and the
# terminal error columns are handled by the dedicated functions, not here.
_TDIMPORT_MUTABLE_FIELDS = frozenset({
    "source_key", "proxy_lease_id", "proxy_ref", "proxy_verified_ip",
    "import_method", "tg_user_id", "masked_phone", "username", "display_name",
    "identity_verified_at", "worker_key", "available_at", "expires_at",
    "started_at", "finished_at", "result_json", "cleanup_done",
})

_TDIMPORT_ACTIVE_STATUSES = ("created", "processing")


def _tdimport_norm_key(raw):
    # type: (Any) -> str
    """Defensive manager-key normalization identical to
    manager_registry.normalize_manager_key (strip ALL whitespace, casefold,
    'ё'->'е'), implemented without `re` (not imported in storage.py). Idempotent
    on an already-normalized key, so it can never mismatch a managers.manager_key
    while still protecting the one-active-per-manager unique index if a caller
    forgets to normalize first."""
    return "".join(str(raw or "").split()).casefold().replace("ё", "е")


def ensure_tdata_import_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Idempotent, additive create of tdata_import_ops (+ indexes). Safe to
    call at the top of every accessor -- mirrors _proxy_leases_table_ready /
    ensure_replacement_tables."""
    con = _bsl_connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS tdata_import_ops(
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id         TEXT UNIQUE NOT NULL,
                manager_key          TEXT NOT NULL,
                owner_user_id        INTEGER,
                worker_key           TEXT NOT NULL DEFAULT '',
                status               TEXT NOT NULL DEFAULT 'created',
                stage                TEXT NOT NULL DEFAULT 'created',
                source_key           TEXT NOT NULL DEFAULT '',
                proxy_lease_id       INTEGER,
                proxy_ref            TEXT NOT NULL DEFAULT '',
                proxy_verified_ip    TEXT NOT NULL DEFAULT '',
                import_method        TEXT NOT NULL DEFAULT '',
                tg_user_id           INTEGER,
                masked_phone         TEXT NOT NULL DEFAULT '',
                username             TEXT NOT NULL DEFAULT '',
                display_name         TEXT NOT NULL DEFAULT '',
                identity_verified_at TEXT NOT NULL DEFAULT '',
                available_at         TEXT NOT NULL DEFAULT '',
                expires_at           TEXT NOT NULL DEFAULT '',
                created_at           TEXT,
                started_at           TEXT NOT NULL DEFAULT '',
                finished_at          TEXT NOT NULL DEFAULT '',
                updated_at           TEXT,
                result_json          TEXT NOT NULL DEFAULT '',
                error_class          TEXT NOT NULL DEFAULT '',
                error_text           TEXT NOT NULL DEFAULT '',
                cleanup_done         INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_tdimport_active_manager
                ON tdata_import_ops(manager_key)
                WHERE status IN ('created','processing');
            CREATE INDEX IF NOT EXISTS ix_tdimport_manager_status
                ON tdata_import_ops(manager_key, status);
            CREATE INDEX IF NOT EXISTS ix_tdimport_status_expires
                ON tdata_import_ops(status, expires_at);
            """
        )
        con.commit()
    finally:
        con.close()


def tdata_import_create(operation_id, manager_key, *, owner_user_id=None,
                        source_key="", expires_at="", db_path=None):
    # type: (str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    """Start a new import operation (status='created', stage='created').

    Idempotent on operation_id: a retry with the SAME operation_id returns the
    existing row unchanged. Returns None when ANOTHER operation_id already holds
    the one active (non-terminal) slot for this manager_key --
    ux_tdimport_active_manager is the single source of truth for "one active
    import per manager", enforced at INSERT."""
    op = str(operation_id or "").strip()
    mk = _tdimport_norm_key(manager_key)
    if not op or not mk:
        raise ValueError("operation_id and manager_key are required")
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        existing = con.execute(
            "SELECT * FROM tdata_import_ops WHERE operation_id=?", (op,)
        ).fetchone()
        if existing:
            return dict(existing)
        try:
            con.execute(
                "INSERT INTO tdata_import_ops"
                "(operation_id, manager_key, owner_user_id, status, stage,"
                " source_key, expires_at, created_at, updated_at)"
                " VALUES(?,?,?,'created','created',?,?,?,?)",
                (op, mk, owner_user_id, str(source_key or "").strip(),
                 str(expires_at or ""), now, now),
            )
            con.commit()
        except _bsl_sqlite3.IntegrityError:
            con.rollback()
            return None
        row = con.execute(
            "SELECT * FROM tdata_import_ops WHERE operation_id=?", (op,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def tdata_import_get(operation_id, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    op = str(operation_id or "").strip()
    if not op:
        return None
    ensure_tdata_import_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM tdata_import_ops WHERE operation_id=?", (op,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def tdata_import_get_active_for_manager(manager_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    mk = _tdimport_norm_key(manager_key)
    if not mk:
        return None
    ensure_tdata_import_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM tdata_import_ops"
            " WHERE manager_key=? AND status IN ('created','processing')"
            " ORDER BY id DESC LIMIT 1",
            (mk,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def tdata_import_advance_stage(operation_id, from_stage, to_stage, *, fields=None, db_path=None):
    # type: (str, str, str, ..., Optional[Dict[str, Any]], Optional[str]) -> bool
    """Compare-and-set forward stage transition. Only edges declared in
    _TDIMPORT_STAGE_TRANSITIONS are allowed (a caller bug raises ValueError
    before touching the DB). Also bumps status 'created'->'processing'.

    Returns False (not an error) when the CAS predicate matches no row -- a
    stale/duplicate call, an unknown operation_id, or an operation that already
    moved to a terminal status. This is the normal retry/double-fire signal,
    exactly like replacement_advance."""
    op = str(operation_id or "").strip()
    if not op:
        raise ValueError("operation_id is required")
    fs = str(from_stage or "")
    ts = str(to_stage or "")
    valid = _TDIMPORT_STAGE_TRANSITIONS.get(fs)
    if valid is None:
        raise ValueError(f"unknown tdata-import from_stage {fs!r}")
    if ts not in valid:
        raise ValueError(
            f"invalid tdata-import stage transition {fs!r} -> {ts!r} "
            f"(allowed from {fs!r}: {valid!r})"
        )
    fields = dict(fields or {})
    unknown = set(fields) - _TDIMPORT_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown/forbidden tdata-import field(s): {sorted(unknown)!r}")
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    set_cols = ["stage=?", "status='processing'", "updated_at=?"]
    params = [ts, now]
    for col in _TDIMPORT_MUTABLE_FIELDS:
        if col in fields:
            set_cols.append(f"{col}=?")
            params.append(fields[col])
    params.extend([op, fs])
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            f"UPDATE tdata_import_ops SET {', '.join(set_cols)}"
            " WHERE operation_id=? AND stage=? AND status IN ('created','processing')",
            tuple(params),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def tdata_import_set_fields(operation_id, fields, *, db_path=None):
    # type: (str, Dict[str, Any], Optional[str]) -> bool
    """Set whitelisted mutable fields WITHOUT a stage change (e.g. record
    proxy_verified_ip, worker_key). Only applies while the op is still active."""
    op = str(operation_id or "").strip()
    fields = dict(fields or {})
    if not op or not fields:
        return False
    unknown = set(fields) - _TDIMPORT_MUTABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown/forbidden tdata-import field(s): {sorted(unknown)!r}")
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    set_cols = ["updated_at=?"]
    params = [now]
    for col in _TDIMPORT_MUTABLE_FIELDS:
        if col in fields:
            set_cols.append(f"{col}=?")
            params.append(fields[col])
    params.append(op)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            f"UPDATE tdata_import_ops SET {', '.join(set_cols)}"
            " WHERE operation_id=? AND status IN ('created','processing')",
            tuple(params),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def tdata_import_claim(operation_id, worker_key, *, db_path=None):
    # type: (str, str, Optional[str]) -> bool
    """Take ownership of an active operation (sets worker_key; started_at once).
    Succeeds only if the op is unowned or already owned by this worker_key."""
    op = str(operation_id or "").strip()
    wk = str(worker_key or "").strip()
    if not op or not wk:
        return False
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE tdata_import_ops"
            " SET worker_key=?,"
            "     started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,"
            "     updated_at=?"
            " WHERE operation_id=? AND status IN ('created','processing')"
            "       AND (worker_key='' OR worker_key=?)",
            (wk, now, now, op, wk),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def tdata_import_complete(operation_id, *, result_json="", db_path=None):
    # type: (str, ..., Optional[str]) -> bool
    """Guarded processing->done. Only permitted once the runtime is confirmed
    (stage in runtime_running/completed). Sets stage='completed'."""
    op = str(operation_id or "").strip()
    if not op:
        return False
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE tdata_import_ops"
            " SET status='done', stage='completed', finished_at=?, updated_at=?,"
            "     result_json=CASE WHEN ?<>'' THEN ? ELSE result_json END"
            " WHERE operation_id=? AND status='processing'"
            "       AND stage IN ('runtime_running','completed')",
            (now, now, str(result_json or ""), str(result_json or ""), op),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def tdata_import_fail(operation_id, *, error_class="", error_text="", stage=None, db_path=None):
    # type: (str, ..., Optional[str], Optional[str]) -> bool
    """Move an active operation to status='error'. error_text must already be
    safe/generic (no credential material)."""
    op = str(operation_id or "").strip()
    if not op:
        return False
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    set_cols = ["status='error'", "error_class=?", "error_text=?", "finished_at=?", "updated_at=?"]
    params = [str(error_class or ""), str(error_text or ""), now, now]
    if stage is not None:
        set_cols.append("stage=?")
        params.append(str(stage))
    params.append(op)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            f"UPDATE tdata_import_ops SET {', '.join(set_cols)}"
            " WHERE operation_id=? AND status IN ('created','processing')",
            tuple(params),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def tdata_import_cancel(operation_id, *, db_path=None):
    # type: (str, Optional[str]) -> bool
    """Move an active operation to status='cancelled'."""
    op = str(operation_id or "").strip()
    if not op:
        return False
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE tdata_import_ops"
            " SET status='cancelled', finished_at=?, updated_at=?"
            " WHERE operation_id=? AND status IN ('created','processing')",
            (now, now, op),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def tdata_import_mark_cleanup(operation_id, db_path=None):
    # type: (str, Optional[str]) -> bool
    op = str(operation_id or "").strip()
    if not op:
        return False
    ensure_tdata_import_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE tdata_import_ops SET cleanup_done=1, updated_at=? WHERE operation_id=?",
            (now, op),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def tdata_import_list_active(db_path=None):
    # type: (Optional[str]) -> List[Dict[str, Any]]
    ensure_tdata_import_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM tdata_import_ops"
            " WHERE status IN ('created','processing') ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def tdata_import_tg_user_conflict(tg_user_id, *, db_path=None):
    # type: (int, Optional[str]) -> Optional[str]
    """Read-only duplicate-account guard: is `tg_user_id` already the
    identity of another non-archived manager? Mirrors main.py's
    `_replacement_tg_user_conflict` query exactly (same semantics, same SQL),
    reimplemented here rather than imported -- main.py cannot be imported
    standalone (Telethon/env side effects at import time) -- so the
    tdata-import flow gets the SAME dedup guarantee the replacement/relogin
    flows already have, against the SAME `managers` table. Never raises: a
    bare test DB without a `managers` table returns None."""
    tid = int(tg_user_id or 0)
    if tid <= 0:
        return None
    con = _bsl_connect(db_path)
    try:
        try:
            row = con.execute(
                "SELECT manager_key FROM managers WHERE tg_user_id=? AND COALESCE(status,'')<>'archived' LIMIT 1",
                (tid,),
            ).fetchone()
        except _bsl_sqlite3.OperationalError:
            return None
        return str(row["manager_key"]) if row else None
    finally:
        con.close()


def tdata_import_tgid_reserved_by_other(tg_user_id, exclude_operation_id, *, db_path=None):
    # type: (int, str, Optional[str]) -> Optional[str]
    """Read-only: is `tg_user_id` already claimed by ANOTHER active
    (non-terminal) tdata-import operation? Mirrors
    `_replacement_tgid_reserved_by_other`'s purpose for this feature's own
    durable-operation table -- catches a race between two concurrent imports
    before either one has written to `managers` yet."""
    tid = int(tg_user_id or 0)
    if tid <= 0:
        return None
    ensure_tdata_import_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT operation_id FROM tdata_import_ops"
            " WHERE tg_user_id=? AND status IN ('created','processing') AND operation_id<>?"
            " LIMIT 1",
            (tid, str(exclude_operation_id or "")),
        ).fetchone()
        return str(row["operation_id"]) if row else None
    finally:
        con.close()


def tdata_import_list_stale(now_iso, db_path=None):
    # type: (str, Optional[str]) -> List[Dict[str, Any]]
    """Active operations whose expires_at (non-empty ISO string) is strictly in
    the past relative to now_iso. Lexicographic compare on ISO-8601 UTC."""
    cutoff = str(now_iso or "").strip()
    if not cutoff:
        return []
    ensure_tdata_import_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM tdata_import_ops"
            " WHERE status IN ('created','processing')"
            "       AND expires_at <> '' AND expires_at < ?"
            " ORDER BY id ASC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

# --- TPILOT TDATA/SESSION IMPORT (durable operation) END ---


# --- TPILOT DEVICE-LOGIN (connect Telegram on another device) BEGIN ---
# Durable request state for the "🔐 Подключить Telegram на устройстве" feature.
# Model mirrors tdata_import_ops: synchronous sqlite3 on the shared QUEUE_DB_PATH
# via _bsl_connect, one row per request_id, forward-only status transitions as
# compare-and-set UPDATEs, a partial unique index enforcing one active request
# per manager.
#
# SECURITY -- this table stores ONLY safe metadata. It MUST NEVER contain the OTP
# login code, the plaintext capability token, or the service-message text. The
# OTP is genuinely zero-durable inside TPilot server infrastructure: the manager
# runtime re-reads the 777000 message on demand and returns the parsed code
# straight over a 127.0.0.1 loopback socket to AdminBot RAM (see main.py's
# device-login bridge). Only token_hash (SHA-256 of a short-lived one-shot
# capability token, useless without its RAM-only preimage) and bridge_host/port
# are persisted here.
_DEVLOGIN_STATUSES = ("waiting", "received", "delivered", "consumed", "expired", "cancelled", "error")
_DEVLOGIN_ACTIVE_STATUSES = ("waiting", "received", "delivered")

# Whitelisted mutable columns for devlogin_set_fields. status/timestamps/error are
# handled by dedicated CAS functions and are NOT in this set. The OTP/plaintext
# token are absent by construction (they are never columns at all).
_DEVLOGIN_MUTABLE_FIELDS = frozenset({
    "telegram_message_id", "bridge_host", "bridge_port", "token_hash",
    "received_at", "bridge_claimed_at", "fetch_attempts",
})


def ensure_devlogin_tables(db_path=None):
    # type: (Optional[str]) -> None
    """Idempotent, additive create of manager_device_login_ops (+ dedup table +
    indexes). Safe to call at the top of every accessor -- mirrors
    ensure_tdata_import_tables."""
    con = _bsl_connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS manager_device_login_ops(
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id           TEXT UNIQUE NOT NULL,
                manager_key          TEXT NOT NULL,
                requested_by_user_id INTEGER,
                status               TEXT NOT NULL DEFAULT 'waiting',
                telegram_message_id  INTEGER,
                bridge_host          TEXT NOT NULL DEFAULT '',
                bridge_port          INTEGER,
                token_hash           TEXT NOT NULL DEFAULT '',
                fetch_attempts       INTEGER NOT NULL DEFAULT 0,
                started_at           TEXT NOT NULL DEFAULT '',
                expires_at           TEXT NOT NULL DEFAULT '',
                received_at          TEXT NOT NULL DEFAULT '',
                bridge_claimed_at    TEXT NOT NULL DEFAULT '',
                consumed_at          TEXT NOT NULL DEFAULT '',
                created_at           TEXT,
                updated_at           TEXT,
                error_class          TEXT NOT NULL DEFAULT '',
                error_text           TEXT NOT NULL DEFAULT ''
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_devlogin_active_manager
                ON manager_device_login_ops(manager_key)
                WHERE status IN ('waiting','received','delivered');
            CREATE INDEX IF NOT EXISTS ix_devlogin_status_expires
                ON manager_device_login_ops(status, expires_at);
            CREATE INDEX IF NOT EXISTS ix_devlogin_manager_status
                ON manager_device_login_ops(manager_key, status);
            CREATE TABLE IF NOT EXISTS devlogin_seen_msg(
                request_id  TEXT NOT NULL,
                message_id  INTEGER NOT NULL,
                seen_at     TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(request_id, message_id)
            );
            """
        )
        con.commit()
    finally:
        con.close()


def devlogin_create(request_id, manager_key, *, requested_by_user_id=None,
                    token_hash="", started_at="", expires_at="", db_path=None):
    # type: (str, str, ..., Optional[str]) -> Optional[Dict[str, Any]]
    """Create a new device-login request (status='waiting').

    Idempotent on request_id (a retry returns the existing row). Returns None
    when ANOTHER request_id already holds the one active slot for this
    manager_key (ux_devlogin_active_manager). NEVER accepts a code/plaintext
    token -- only the SHA-256 token_hash (safe metadata)."""
    rid = str(request_id or "").strip()
    mk = _tdimport_norm_key(manager_key)
    if not rid or not mk:
        raise ValueError("request_id and manager_key are required")
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        existing = con.execute(
            "SELECT * FROM manager_device_login_ops WHERE request_id=?", (rid,)
        ).fetchone()
        if existing:
            return dict(existing)
        try:
            con.execute(
                "INSERT INTO manager_device_login_ops"
                "(request_id, manager_key, requested_by_user_id, status,"
                " token_hash, started_at, expires_at, created_at, updated_at)"
                " VALUES(?,?,?,'waiting',?,?,?,?,?)",
                (rid, mk, requested_by_user_id, str(token_hash or ""),
                 str(started_at or now), str(expires_at or ""), now, now),
            )
            con.commit()
        except _bsl_sqlite3.IntegrityError:
            con.rollback()
            return None
        row = con.execute(
            "SELECT * FROM manager_device_login_ops WHERE request_id=?", (rid,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


_DEVLOGIN_TERMINAL_STATUSES = ("consumed", "cancelled", "expired", "error")
_DEVLOGIN_DEFAULT_COOLDOWN_SEC = 30


def devlogin_create_with_cooldown(request_id, manager_key, *, requested_by_user_id=None,
                                  token_hash="", started_at="", expires_at="",
                                  cooldown_sec=_DEVLOGIN_DEFAULT_COOLDOWN_SEC, db_path=None):
    # type: (str, str, ..., int, Optional[str]) -> Dict[str, Any]
    """Race-safe create: under a single BEGIN IMMEDIATE transaction (the SAME
    upfront-write-lock pattern manager_queue_take_next/take_next_panel_command
    already use elsewhere in this project), atomically enforces BOTH:

      1. one-active-request-per-manager (mirrors ux_devlogin_active_manager,
         checked explicitly here so it shares the SAME lock as #2 below --
         the plain unique index alone only protects the INSERT itself, not
         the cooldown read that must happen in the same atomic window);
      2. a minimum `cooldown_sec` since this manager's most recent TERMINAL
         request (status in consumed/cancelled/expired/error), measured from
         that row's `updated_at` -- the one column every terminal transition
         (devlogin_transition/_cancel/_expire/_fail) already stamps, so no
         new per-status "finished_at" column is needed.

    Returns {"ok": True, "row": {...}} on success, or on rejection
    {"ok": False, "reason": "active"|"cooldown", "retry_after_sec": int} --
    retry_after_sec is the ONLY detail ever returned for a cooldown block
    (safe to show an admin verbatim; never a secret).

    NEVER touches an OTP/plaintext token -- only the SHA-256 token_hash the
    caller already computed, exactly like devlogin_create above."""
    rid = str(request_id or "").strip()
    mk = _tdimport_norm_key(manager_key)
    if not rid or not mk:
        raise ValueError("request_id and manager_key are required")
    cooldown_sec = int(cooldown_sec or 0)
    ensure_devlogin_tables(db_path)
    now_dt = _utc_now()
    now = now_dt.replace(microsecond=0).isoformat()
    cooldown_cutoff = (now_dt - timedelta(seconds=cooldown_sec)).replace(microsecond=0).isoformat()
    con = _bsl_connect(db_path)
    tx = None
    try:
        tx = _p5_begin_immediate_sync(
            con, source="storage.py", function="devlogin_create_with_cooldown",
            db_path=db_path, manager_key=mk,
        )
        try:
            active = con.execute(
                "SELECT 1 FROM manager_device_login_ops"
                " WHERE manager_key=? AND status IN ('waiting','received','delivered') LIMIT 1",
                (mk,),
            ).fetchone()
            if active:
                _p5_rollback_sync(tx, con)
                return {"ok": False, "reason": "active", "retry_after_sec": 0}

            last_terminal = con.execute(
                "SELECT updated_at FROM manager_device_login_ops"
                " WHERE manager_key=? AND status IN (?,?,?,?)"
                " ORDER BY updated_at DESC LIMIT 1",
                (mk,) + _DEVLOGIN_TERMINAL_STATUSES,
            ).fetchone()
            if cooldown_sec > 0 and last_terminal and str(last_terminal[0] or "") > cooldown_cutoff:
                try:
                    elapsed = (now_dt - datetime.fromisoformat(str(last_terminal[0]))).total_seconds()
                except ValueError:
                    elapsed = 0.0
                remaining = max(1, int(round(cooldown_sec - elapsed)))
                _p5_rollback_sync(tx, con)
                return {"ok": False, "reason": "cooldown", "retry_after_sec": remaining}

            try:
                con.execute(
                    "INSERT INTO manager_device_login_ops"
                    "(request_id, manager_key, requested_by_user_id, status,"
                    " token_hash, started_at, expires_at, created_at, updated_at)"
                    " VALUES(?,?,?,'waiting',?,?,?,?,?)",
                    (rid, mk, requested_by_user_id, str(token_hash or ""),
                     str(started_at or now), str(expires_at or ""), now, now),
                )
            except _bsl_sqlite3.IntegrityError:
                _p5_rollback_sync(tx, con)
                return {"ok": False, "reason": "active", "retry_after_sec": 0}
            _p5_commit_sync(tx, con)
        except Exception as _p5_exc:
            try:
                _p5_rollback_sync(tx, con, error=_p5_exc)
            except Exception:
                pass
            raise
        row = con.execute(
            "SELECT * FROM manager_device_login_ops WHERE request_id=?", (rid,)
        ).fetchone()
        return {"ok": True, "row": dict(row) if row else None}
    finally:
        con.close()


def devlogin_get(request_id, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    rid = str(request_id or "").strip()
    if not rid:
        return None
    ensure_devlogin_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM manager_device_login_ops WHERE request_id=?", (rid,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def devlogin_get_active_for_manager(manager_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    mk = _tdimport_norm_key(manager_key)
    if not mk:
        return None
    ensure_devlogin_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute(
            "SELECT * FROM manager_device_login_ops"
            " WHERE manager_key=? AND status IN ('waiting','received','delivered')"
            " ORDER BY id DESC LIMIT 1",
            (mk,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def devlogin_set_fields(request_id, fields, *, db_path=None):
    # type: (str, Dict[str, Any], Optional[str]) -> bool
    """Update whitelisted safe-metadata columns WITHOUT a status change (only
    while the request is still active). Rejects any non-whitelisted key -- so a
    caller can never smuggle an OTP/plaintext token into a column (there are no
    such columns anyway)."""
    rid = str(request_id or "").strip()
    if not rid:
        raise ValueError("request_id is required")
    fields = dict(fields or {})
    unknown = set(fields) - _DEVLOGIN_MUTABLE_FIELDS
    if unknown:
        raise ValueError("unknown/forbidden devlogin field(s): %r" % (sorted(unknown),))
    if not fields:
        return False
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    set_cols = ["updated_at=?"]
    params = [now]
    for col in _DEVLOGIN_MUTABLE_FIELDS:
        if col in fields:
            set_cols.append("%s=?" % col)
            params.append(fields[col])
    params.append(rid)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_device_login_ops SET %s"
            " WHERE request_id=? AND status IN ('waiting','received','delivered')"
            % ", ".join(set_cols),
            tuple(params),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def devlogin_transition(request_id, from_status, to_status, *, fields=None, db_path=None):
    # type: (str, str, str, ..., Optional[str]) -> bool
    """Compare-and-set status transition. Only advances a row whose current
    status == from_status (returns False, not an error, otherwise -- the normal
    stale/double-fire/lost-race signal). Optionally sets whitelisted metadata
    columns and known timestamp columns (received_at/bridge_claimed_at/
    consumed_at) atomically with the flip. NEVER writes an OTP."""
    rid = str(request_id or "").strip()
    fs = str(from_status or "")
    ts = str(to_status or "")
    if not rid:
        raise ValueError("request_id is required")
    if ts not in _DEVLOGIN_STATUSES:
        raise ValueError("unknown devlogin status %r" % ts)
    fields = dict(fields or {})
    # timestamp columns settable during a transition (not free-form metadata)
    ts_cols = {"received_at", "bridge_claimed_at", "consumed_at", "error_class", "error_text"}
    unknown = set(fields) - (_DEVLOGIN_MUTABLE_FIELDS | ts_cols)
    if unknown:
        raise ValueError("unknown/forbidden devlogin transition field(s): %r" % (sorted(unknown),))
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    set_cols = ["status=?", "updated_at=?"]
    params = [ts, now]
    for col in sorted(_DEVLOGIN_MUTABLE_FIELDS | ts_cols):
        if col in fields:
            set_cols.append("%s=?" % col)
            params.append(fields[col])
    params.extend([rid, fs])
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_device_login_ops SET %s WHERE request_id=? AND status=?"
            % ", ".join(set_cols),
            tuple(params),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def devlogin_claim_for_delivery(request_id, *, db_path=None):
    # type: (str, Optional[str]) -> bool
    """ATOMIC one-shot claim: received -> delivered, bumping fetch_attempts and
    stamping bridge_claimed_at. Returns True for EXACTLY ONE caller (rowcount==1);
    any concurrent second fetch sees rowcount==0 and must return 'unavailable'.
    This is the single gate that guarantees at-most-once OTP delivery -- the
    runtime performs it BEFORE re-reading/returning the code."""
    rid = str(request_id or "").strip()
    if not rid:
        raise ValueError("request_id is required")
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_device_login_ops"
            " SET status='delivered', bridge_claimed_at=?, fetch_attempts=fetch_attempts+1, updated_at=?"
            " WHERE request_id=? AND status='received'",
            (now, now, rid),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def devlogin_cancel(request_id, *, db_path=None):
    # type: (str, Optional[str]) -> bool
    rid = str(request_id or "").strip()
    if not rid:
        raise ValueError("request_id is required")
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_device_login_ops"
            " SET status='cancelled', updated_at=?"
            " WHERE request_id=? AND status IN ('waiting','received','delivered')",
            (now, rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def devlogin_fail(request_id, *, error_class="", error_text="", db_path=None):
    # type: (str, ..., Optional[str]) -> bool
    rid = str(request_id or "").strip()
    if not rid:
        raise ValueError("request_id is required")
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_device_login_ops"
            " SET status='error', error_class=?, error_text=?, updated_at=?"
            " WHERE request_id=? AND status IN ('waiting','received','delivered')",
            (str(error_class or ""), str(error_text or ""), now, rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def devlogin_list_stale(now_iso, db_path=None):
    # type: (str, Optional[str]) -> List[Dict[str, Any]]
    """Active requests whose expires_at is strictly in the past (lexicographic
    ISO-8601 UTC). Used by the controller sweeper to move abandoned requests to
    'expired' and release the one-active-per-manager slot. No OTP is involved."""
    cutoff = str(now_iso or "").strip()
    if not cutoff:
        return []
    ensure_devlogin_tables(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM manager_device_login_ops"
            " WHERE status IN ('waiting','received','delivered')"
            "       AND expires_at <> '' AND expires_at < ?"
            " ORDER BY id ASC",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def devlogin_expire(request_id, *, db_path=None):
    # type: (str, Optional[str]) -> bool
    rid = str(request_id or "").strip()
    if not rid:
        raise ValueError("request_id is required")
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE manager_device_login_ops"
            " SET status='expired', updated_at=?"
            " WHERE request_id=? AND status IN ('waiting','received','delivered')",
            (now, rid),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def devlogin_seen_mark_once(request_id, message_id, *, db_path=None):
    # type: (str, int, Optional[str]) -> bool
    """At-most-once dedup for a (request_id, message_id) pair -- INSERT-or-catch
    like proxy_renew_notify_mark_once. Returns True the FIRST time this exact
    777000 message is processed for this request, False on any repeat (so a
    catch-up rescan can never re-open an already-handled message). Stores only
    ids, never the code."""
    rid = str(request_id or "").strip()
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return False
    if not rid:
        return False
    ensure_devlogin_tables(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            "INSERT INTO devlogin_seen_msg(request_id, message_id, seen_at) VALUES(?,?,?)",
            (rid, mid, now),
        )
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()
# --- TPILOT DEVICE-LOGIN (connect Telegram on another device) END ---


# --- TPILOT MANAGERBOT ACCESS AUTO-GRANT (canonical) 20260727 START ---
# Canonical, idempotent grant of ManagerBot BASE access for an active TPilot
# manager. Single source of truth for "manager connected -> gets ManagerBot
# access" -- called from every confirmed onboarding/reconnect path in
# main.py, and as a self-heal from manager_bot.py's /start.
#
# Deliberately narrow: writes ONLY missing rows in access_users,
# access_targets, manager_bot_access. Never touches event_cutoff_id on an
# EXISTING manager_bot_access row (a past round's bug in this exact area
# once destroyed 30 undelivered lead cards by recomputing cutoff on every
# call -- 0 can legitimately mean "granted before any events existed", not
# "needs repair"). Never enables sub-permissions (can_view_stats,
# stats_format, allow_custom_period) and never resets scope_mode/access_level
# on an existing access_users row. All reads+writes happen inside one
# BEGIN IMMEDIATE transaction with a fresh re-check immediately before each
# write, closing the TOCTOU window against a concurrent admin action
# (disable/delete/revoke) between the initial lookup and the write.
def _mbgrant_norm_key(raw):
    # type: (Any) -> str
    """Same normalization as _tdimport_norm_key / manager_registry.normalize_manager_key
    (strip ALL whitespace, casefold, 'ё'->'е')."""
    return "".join(str(raw or "").split()).casefold().replace("ё", "е")


def _mbgrant_ensure_tables(con) -> None:
    """manager_bot_access/manager_bot_events are normally created by
    manager_bot.py's own init_schema, which may not have run yet if this is
    called from the controller before the ManagerBot process has ever
    started. Defensive CREATE TABLE IF NOT EXISTS, schema copied verbatim
    from manager_bot.py's init_schema (that file remains the authoritative
    definition) -- never DROPs or ALTERs an existing table."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS manager_bot_access (
            tg_user_id          INTEGER NOT NULL,
            manager_key         TEXT    NOT NULL DEFAULT '',
            can_receive_cards   INTEGER NOT NULL DEFAULT 0,
            can_set_status      INTEGER NOT NULL DEFAULT 0,
            can_view_stats      INTEGER NOT NULL DEFAULT 0,
            stats_format        TEXT    NOT NULL DEFAULT 'light',
            allow_custom_period INTEGER NOT NULL DEFAULT 0,
            auto_granted        INTEGER NOT NULL DEFAULT 0,
            granted_by          INTEGER DEFAULT 0,
            granted_at          TEXT    NOT NULL DEFAULT '',
            revoked             INTEGER NOT NULL DEFAULT 0,
            revoked_by          INTEGER DEFAULT 0,
            revoked_at          TEXT    NOT NULL DEFAULT '',
            event_cutoff_id     INTEGER NOT NULL DEFAULT 0,
            created_at          TEXT    NOT NULL DEFAULT '',
            updated_at          TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (tg_user_id, manager_key)
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_manager_bot_access_manager ON manager_bot_access(manager_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_manager_bot_access_revoked ON manager_bot_access(revoked)")
    con.execute(
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
        )
        """
    )


def manager_bot_access_ensure_sync(*, tg_user_id=None, manager_key=None,
                                    display_name="", username="",
                                    db_path=None):
    # type: (Optional[int], Optional[str], str, str, Optional[str]) -> Dict[str, Any]
    """Canonical, idempotent ManagerBot base-access grant -- see the module
    banner above for the full contract. Accepts EITHER identifier and
    resolves the other via the SAME "exactly one active manager" rule
    manager_bot.py's _find_active_manager_by_tg_user_id/_by_username use
    (status='active' AND is_enabled=1 AND manual_stopped=0); ambiguous or
    absent -> not_eligible/identity_conflict, never a write.

    Returns a dict:
      status: "granted_now" | "already_granted" | "not_eligible"
              | "identity_conflict" | "disabled" | "error"
      tg_user_id, manager_key, display_name, username: resolved values for
        the caller's UI (best available; "" if unknown -- never None, never
        a raw DB row, never an internal id beyond tg_user_id/manager_key
        themselves).

    "not_eligible" / "identity_conflict" mean no active manager could be
    resolved for this identity at all (genuinely unknown, or ambiguous) --
    the caller's UI is expected to show this uid so it can be handed to an
    admin. "disabled" means the OPPOSITE: an eligible, already-known manager
    was found, but their own access_users row is disabled or their
    manager_bot_access permission is revoked -- the admin already knows who
    this is, so the caller's UI should NOT repeat the uid there.
    """
    uid = int(tg_user_id or 0) or None
    mk_in = _mbgrant_norm_key(manager_key) if manager_key else ""

    if not uid and not mk_in:
        return {"status": "not_eligible", "tg_user_id": 0, "manager_key": "",
                "display_name": "", "username": ""}

    con = None
    try:
        con = _bsl_connect(db_path)
        _mbgrant_ensure_tables(con)

        if uid:
            rows = con.execute(
                """
                SELECT manager_key, display_name, tg_user_id, telegram_username
                FROM managers
                WHERE tg_user_id=?
                  AND COALESCE(is_enabled,0)=1
                  AND COALESCE(manual_stopped,0)=0
                  AND COALESCE(status,'')='active'
                  AND COALESCE(manager_key,'')<>''
                ORDER BY manager_key ASC
                """,
                (uid,),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT manager_key, display_name, tg_user_id, telegram_username
                FROM managers
                WHERE manager_key=?
                  AND COALESCE(is_enabled,0)=1
                  AND COALESCE(manual_stopped,0)=0
                  AND COALESCE(status,'')='active'
                """,
                (mk_in,),
            ).fetchall()

        if len(rows) == 0:
            return {"status": "not_eligible", "tg_user_id": uid or 0, "manager_key": mk_in,
                    "display_name": "", "username": ""}
        if len(rows) > 1:
            return {"status": "identity_conflict", "tg_user_id": uid or 0, "manager_key": mk_in,
                    "display_name": "", "username": ""}

        row = dict(rows[0])
        mk = str(row.get("manager_key") or "")
        row_uid = int(row.get("tg_user_id") or 0)
        if uid and row_uid and uid != row_uid:
            # manager_key input resolved to a DIFFERENT tg_user_id than the
            # caller expected -- fail closed rather than silently granting
            # to a mismatched account.
            return {"status": "identity_conflict", "tg_user_id": uid, "manager_key": mk,
                    "display_name": "", "username": ""}
        resolved_uid = uid or row_uid
        if not resolved_uid or not mk:
            return {"status": "not_eligible", "tg_user_id": resolved_uid or 0, "manager_key": mk,
                    "display_name": "", "username": ""}

        resolved_display_name = str(display_name or row.get("display_name") or "").strip()
        resolved_username = str(username or row.get("telegram_username") or "").strip().lstrip("@")

        now = _now_iso()
        created_any = False

        con.execute("BEGIN IMMEDIATE")
        try:
            # Re-check eligibility fresh, inside the transaction, immediately
            # before any write -- closes the race window against a
            # concurrent admin disable/delete/revoke between the read above
            # and here.
            mgr_row = con.execute(
                """
                SELECT 1 FROM managers
                WHERE tg_user_id=? AND manager_key=?
                  AND COALESCE(is_enabled,0)=1
                  AND COALESCE(manual_stopped,0)=0
                  AND COALESCE(status,'')='active'
                """,
                (resolved_uid, mk),
            ).fetchone()
            if not mgr_row:
                con.rollback()
                return {"status": "not_eligible", "tg_user_id": resolved_uid, "manager_key": mk,
                        "display_name": resolved_display_name, "username": resolved_username}

            user_row = con.execute(
                "SELECT is_enabled, scope_mode FROM access_users WHERE tg_user_id=?",
                (resolved_uid,),
            ).fetchone()
            blanket_scope = False
            if user_row is None:
                con.execute(
                    """
                    INSERT INTO access_users(
                        tg_user_id, display_name, username, access_level,
                        scope_mode, is_enabled, created_by, created_at, updated_at
                    )
                    VALUES (?, ?, ?, 1, 'selected', 1, 0, ?, ?)
                    """,
                    (resolved_uid, resolved_display_name, resolved_username, now, now),
                )
                created_any = True
            elif int(user_row["is_enabled"] or 0) != 1:
                con.rollback()
                return {"status": "disabled", "tg_user_id": resolved_uid, "manager_key": mk,
                        "display_name": resolved_display_name, "username": resolved_username}
            else:
                blanket_scope = str(user_row["scope_mode"] or "selected").strip().lower() == "all"

            # A blanket-scope ('all') user already reaches EVERY manager via
            # manager_bot.py's _linked_manager_keys -> _all_manager_keys(),
            # which ignores access_targets entirely. A per-key row would add
            # nothing today AND would silently survive a later switch to
            # scope_mode='selected', leaving this one key granted. The
            # removed _mbaccess_restore_access had exactly this guard --
            # keep the contract identical.
            if not blanket_scope:
                target_row = con.execute(
                    "SELECT 1 FROM access_targets WHERE tg_user_id=? AND manager_key=?",
                    (resolved_uid, mk),
                ).fetchone()
                if target_row is None:
                    con.execute(
                        "INSERT OR IGNORE INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?, ?, ?)",
                        (resolved_uid, mk, now),
                    )
                    created_any = True

            mba_row = con.execute(
                "SELECT revoked FROM manager_bot_access WHERE tg_user_id=? AND manager_key=?",
                (resolved_uid, mk),
            ).fetchone()
            if mba_row is None:
                cutoff_row = con.execute(
                    "SELECT COALESCE(MAX(id), 0) FROM manager_bot_events WHERE manager_key=?",
                    (mk,),
                ).fetchone()
                event_cutoff_id = int(cutoff_row[0] or 0) if cutoff_row else 0
                con.execute(
                    """
                    INSERT INTO manager_bot_access(
                        tg_user_id, manager_key,
                        can_receive_cards, can_set_status, can_view_stats,
                        stats_format, allow_custom_period,
                        auto_granted, granted_by, granted_at,
                        revoked, revoked_by, revoked_at,
                        event_cutoff_id, created_at, updated_at
                    )
                    VALUES (?, ?, 1, 1, 0, 'light', 0, 1, 0, ?, 0, 0, '', ?, ?, ?)
                    """,
                    (resolved_uid, mk, now, event_cutoff_id, now, now),
                )
                created_any = True
            elif int(mba_row["revoked"] or 0) != 0:
                con.rollback()
                return {"status": "disabled", "tg_user_id": resolved_uid, "manager_key": mk,
                        "display_name": resolved_display_name, "username": resolved_username}

            con.commit()
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            raise

        return {
            "status": "granted_now" if created_any else "already_granted",
            "tg_user_id": resolved_uid, "manager_key": mk,
            "display_name": resolved_display_name, "username": resolved_username,
        }
    except Exception as exc:
        return {"status": "error", "tg_user_id": uid or 0, "manager_key": mk_in,
                "display_name": "", "username": "", "error": repr(exc)}
    finally:
        if con is not None:
            con.close()
# --- TPILOT MANAGERBOT ACCESS AUTO-GRANT (canonical) 20260727 END ---


# --- TPILOT W2 ACCESS & DELIVERY 20260729 START ---
# W2 (frozen master plan wave "Access and Delivery"): a single explicit read
# authority for ManagerBot card-delivery eligibility (D-02/B19), divergence
# observability between manager_bot_access and access_targets (I-05), an
# idempotent/explicit backlog-repair helper that never touches revoked/
# is_enabled (B7/I-36), the manager_lead_cards lead_date identity migration
# (D-06/B1/I-01/I-02), cutoff observability columns + snapshot (D-24/D-03),
# and a structured delivery-failure log (error classification, Phase D).
# Every function here is read-only or additive/idempotent; nothing here
# deletes a row or mutates revoked/is_enabled. db_path=None resolves to the
# same QUEUE_DB_PATH every other storage.py helper in this module uses.
import hashlib as _w2_hashlib


def _w2_pseudonymize(namespace, raw_id):
    # type: (str, Any) -> str
    """Same construction as manager_bot.py's W1 _w1_myaccess_actor_ref
    (SHA256(namespace + ':' + str(raw_id))[:16]) -- never the raw id, never
    Python's unstable per-process hash(). Re-implemented here (not imported
    from manager_bot.py) because manager_bot.py cannot be imported at
    module load time (Telethon/env side effects) and storage.py must stay
    importable standalone."""
    digest = _w2_hashlib.sha256(f"{namespace}:{raw_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def w2_resolve_delivery_manager_keys(tg_user_id, db_path=None):
    # type: (int, Optional[str]) -> List[str]
    """THE single explicit read authority for ManagerBot delivery
    eligibility (W2-B, B19): manager_bot_access is authoritative, never
    access_targets. Returns manager_keys where this tg_user_id currently
    has an active, non-revoked can_receive_cards=1 row. Read-only.
    Missing/absent rows never auto-grant (fail-closed, B5) -- an empty
    result is the correct answer for an unknown or fully-revoked user,
    never an exception."""
    uid = int(tg_user_id or 0)
    if not uid:
        return []
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT manager_key FROM manager_bot_access
            WHERE tg_user_id=? AND COALESCE(can_receive_cards,0)=1 AND COALESCE(revoked,0)=0
            ORDER BY manager_key ASC
            """,
            (uid,),
        ).fetchall()
        return [str(r["manager_key"]) for r in rows if str(r["manager_key"] or "").strip()]
    except Exception:
        return []
    finally:
        con.close()


def w2_access_decision(tg_user_id, manager_key=None, db_path=None):
    # type: (int, Optional[str], Optional[str]) -> Dict[str, Any]
    """W2 REVISION (Blocker 2): THE single resolved access-authority
    decision for a ManagerBot uid, optionally scoped to one manager_key.
    This -- not a union of independently-called lookups -- is what
    manager_bot.py's _poll_loop must consume for its delivery candidate
    keys (see the "delivery_manager_keys" field below).

    Returns a dict:
      status: "allowed" | "disabled" | "revoked" | "missing" | "conflict"
              | "deleted_manager" | "no_manager_association"
      delivery_manager_keys: manager_bot_access-grounded (can_receive_cards=1,
        revoked=0) -- ALWAYS the authority for card delivery, regardless of
        access_targets. This is exactly w2_resolve_delivery_manager_keys's
        own result, computed via the SAME function (single source, no
        second copy of the same query logic).
      ui_manager_keys: access_targets-grounded for scope_mode='selected',
        or every manager_key for scope_mode='all' -- the SAME set
        _linked_manager_keys already computes for its 8 existing UI call
        sites (manager_bot.py's own version is UNCHANGED; this field lets
        a caller reach the same information through the resolver without
        requiring every existing UI call site to migrate).
      reason_code: short, non-sensitive machine string.
      provenance: which table(s) the decision was grounded in.
      conflict: True if manager_bot_access and access_targets disagree for
        this uid (either direction) -- OBSERVABLE, never auto-repaired by
        this function (it performs zero writes).
      actor_ref: pseudonymized uid (SHA256, namespaced) -- never the raw
        tg_user_id, in any field, in any status.

    Rules (Blocker 2 contract):
      1. Explicit revocation wins -- a revoked manager_bot_access row
         always yields status='revoked', delivery_manager_keys=[].
      2. Disabled stays disabled -- access_users.is_enabled=0 always yields
         status='disabled', delivery_manager_keys=[], REGARDLESS of any
         manager_bot_access rows.
      3. Missing access fails closed -- no access_users row at all ->
         status='missing', delivery_manager_keys=[].
      4. A manager association (managers.tg_user_id) alone is NOT read by
         this function at all -- it only ever reads access_users/
         manager_bot_access/access_targets/managers(by manager_key), never
         grants from a manager identity match.
      5. access_targets ALONE never appears in delivery_manager_keys --
         only manager_bot_access does.
      6. A scope_mode='selected' access_targets-only association (no
         manager_bot_access row) is representable via
         status='no_manager_association' when queried with that
         manager_key -- ui_manager_keys still includes it (the legacy UI
         scenario keeps working), delivery_manager_keys does not.
      9. This function performs NO writes -- conflict is observable, never
         auto-repaired (see w2_manager_bot_access_repair_backlog for the
         separate, explicit, dry-run-by-default repair path)."""
    uid = int(tg_user_id or 0)
    mk = _mbgrant_norm_key(manager_key) if manager_key else ""
    actor_ref = _w2_pseudonymize("w2-decision", uid)
    empty = {"status": "missing", "delivery_manager_keys": [], "ui_manager_keys": [],
             "reason_code": "no_uid", "provenance": "none", "conflict": False, "actor_ref": actor_ref}
    if not uid:
        return empty

    con = _bsl_connect(db_path)
    try:
        au = con.execute(
            "SELECT scope_mode, is_enabled FROM access_users WHERE tg_user_id=?", (uid,)
        ).fetchone()
        if au is None:
            return {**empty, "reason_code": "no_access_users_row"}
        if int(au["is_enabled"] or 0) != 1:
            return {"status": "disabled", "delivery_manager_keys": [], "ui_manager_keys": [],
                    "reason_code": "access_users_disabled", "provenance": "access_users",
                    "conflict": False, "actor_ref": actor_ref}
        scope_all = str(au["scope_mode"] or "selected").strip().lower() == "all"

        mba_rows = con.execute(
            "SELECT manager_key, revoked, can_receive_cards FROM manager_bot_access WHERE tg_user_id=?", (uid,)
        ).fetchall()
        mba_active = {str(r["manager_key"]) for r in mba_rows
                      if int(r["can_receive_cards"] or 0) == 1 and int(r["revoked"] or 0) == 0}
        mba_revoked = {str(r["manager_key"]) for r in mba_rows if int(r["revoked"] or 0) == 1}

        at_rows = con.execute("SELECT manager_key FROM access_targets WHERE tg_user_id=?", (uid,)).fetchall()
        at_keys = {str(r["manager_key"]) for r in at_rows}

        if scope_all:
            all_keys = {str(r["manager_key"]) for r in con.execute(
                "SELECT manager_key FROM managers WHERE COALESCE(manager_key,'')<>''"
            ).fetchall()}
            ui_keys = sorted(all_keys)
            provenance = "access_users.scope_mode=all"
            conflict = False
        else:
            ui_keys = sorted(at_keys)
            provenance = "access_targets"
            conflict = bool((mba_active - at_keys) or (at_keys - mba_active - mba_revoked))

        delivery_keys = sorted(mba_active)

        if mk:
            if mk in mba_revoked:
                return {"status": "revoked", "delivery_manager_keys": [], "ui_manager_keys": ui_keys,
                        "reason_code": "manager_bot_access_revoked", "provenance": "manager_bot_access",
                        "conflict": conflict, "actor_ref": actor_ref}
            if mk not in mba_active:
                mgr_row = con.execute(
                    "SELECT status, is_enabled FROM managers WHERE manager_key=?", (mk,)
                ).fetchone()
                if mgr_row is None or str(mgr_row["status"] or "") != "active" or int(mgr_row["is_enabled"] or 0) != 1:
                    return {"status": "deleted_manager", "delivery_manager_keys": [], "ui_manager_keys": ui_keys,
                            "reason_code": "manager_row_absent_or_inactive", "provenance": "managers",
                            "conflict": conflict, "actor_ref": actor_ref}
                return {"status": "no_manager_association", "delivery_manager_keys": [], "ui_manager_keys": ui_keys,
                        "reason_code": "no_manager_bot_access_row", "provenance": "manager_bot_access",
                        "conflict": conflict, "actor_ref": actor_ref}
            return {"status": "allowed", "delivery_manager_keys": [mk], "ui_manager_keys": ui_keys,
                    "reason_code": "resolved", "provenance": "manager_bot_access",
                    "conflict": conflict, "actor_ref": actor_ref}

        if delivery_keys:
            status = "allowed"
        elif ui_keys:
            status = "no_manager_association"
        else:
            status = "missing"
        return {"status": status, "delivery_manager_keys": delivery_keys, "ui_manager_keys": ui_keys,
                "reason_code": "resolved", "provenance": provenance, "conflict": conflict, "actor_ref": actor_ref}
    except Exception:
        return {**empty, "status": "missing", "reason_code": "read_error"}
    finally:
        con.close()


def w2_detect_access_divergence(db_path=None, sample_limit=25):
    # type: (Optional[str], int) -> Dict[str, Any]
    """Read-only divergence detector (D-02 step (a), I-05): every
    manager_bot_access <-> access_targets mismatch is observable on demand,
    without ever writing anything. Two classes, matching the frozen master
    plan's own terminology:
      - mba_without_access_target: an ACTIVE (can_receive_cards=1,
        revoked=0), scope_mode='selected' grant with no matching
        access_targets row -- the harmful class (D-02): the candidate-key
        resolver used to read only access_targets, so these rows never
        even reached the delivery query. Gated by scope_mode='selected'
        because a scope_mode='all' user does not need an access_targets
        row at all (see manager_bot.py's _linked_manager_keys and this
        module's own manager_bot_access_ensure_sync comment on this exact
        point).
      - access_target_without_mba: an access_targets row with no matching
        active manager_bot_access row -- harmless for delivery (the JOIN
        in _fetch_unsent_events_for_user already requires an active
        manager_bot_access row), but still an observable inconsistency.
    tg_user_id values are never returned raw -- only a pseudonymized
    actor_ref (see _w2_pseudonymize), matching the W1 no-raw-uid logging
    discipline this project already established for access diagnostics."""
    con = _bsl_connect(db_path)
    try:
        mba_missing = con.execute(
            """
            SELECT mba.tg_user_id AS uid, mba.manager_key AS mk
            FROM manager_bot_access mba
            JOIN access_users au ON au.tg_user_id = mba.tg_user_id
            LEFT JOIN access_targets at
              ON at.tg_user_id = mba.tg_user_id AND at.manager_key = mba.manager_key
            WHERE COALESCE(mba.can_receive_cards,0)=1
              AND COALESCE(mba.revoked,0)=0
              AND COALESCE(au.is_enabled,0)=1
              AND COALESCE(au.scope_mode,'selected')='selected'
              AND at.manager_key IS NULL
            ORDER BY mba.tg_user_id, mba.manager_key
            """
        ).fetchall()
        at_missing = con.execute(
            """
            SELECT at.tg_user_id AS uid, at.manager_key AS mk
            FROM access_targets at
            LEFT JOIN manager_bot_access mba
              ON mba.tg_user_id = at.tg_user_id AND mba.manager_key = at.manager_key
                 AND COALESCE(mba.can_receive_cards,0)=1 AND COALESCE(mba.revoked,0)=0
            WHERE mba.manager_key IS NULL
            ORDER BY at.tg_user_id, at.manager_key
            """
        ).fetchall()
    except Exception as exc:
        return {"status": "error", "error_class": type(exc).__name__}
    finally:
        con.close()

    def _sample(rows):
        return [
            {"actor_ref": _w2_pseudonymize("w2-divergence", int(r["uid"])), "manager_key": str(r["mk"])}
            for r in rows[:sample_limit]
        ]

    return {
        "status": "ok",
        "mba_without_access_target_count": len(mba_missing),
        "access_target_without_mba_count": len(at_missing),
        "mba_without_access_target_sample": _sample(mba_missing),
        "access_target_without_mba_sample": _sample(at_missing),
        "sample_limit": sample_limit,
    }


def w2_manager_bot_access_repair_backlog(db_path=None, dry_run=True, actor_tag="w2_backlog_repair"):
    # type: (Optional[str], bool, str) -> Dict[str, Any]
    """Idempotent, explicit, additive-only repair for the D-02 backlog class
    (mba_without_access_target): creates the MISSING access_targets row for
    every (tg_user_id, manager_key) pair where manager_bot_access already
    says revoked=0 AND can_receive_cards=1 (I-36). Never touches `revoked`,
    never touches `access_users.is_enabled`, never deletes anything -- pure
    INSERT OR IGNORE. dry_run=True (the default) computes and returns the
    exact set that WOULD be inserted without writing; dry_run=False
    performs the same INSERT OR IGNORE inside one transaction and
    self-verifies the STOP conditions (10.1/I-36) before committing:
    revoked-row count and disabled-row count must be byte-for-byte
    unchanged, and the access_targets row count may only increase, never
    decrease. Any violation aborts with a rollback and
    status='aborted_stop_condition' -- this function never commits a state
    that fails its own invariant check."""
    con = _bsl_connect(db_path)
    try:
        before_revoked = con.execute("SELECT COUNT(*) FROM manager_bot_access WHERE revoked=1").fetchone()[0]
        before_disabled = con.execute("SELECT COUNT(*) FROM access_users WHERE COALESCE(is_enabled,1)=0").fetchone()[0]
        before_targets = con.execute("SELECT COUNT(*) FROM access_targets").fetchone()[0]

        candidates = con.execute(
            """
            SELECT mba.tg_user_id AS uid, mba.manager_key AS mk
            FROM manager_bot_access mba
            JOIN access_users au ON au.tg_user_id = mba.tg_user_id
            LEFT JOIN access_targets at
              ON at.tg_user_id = mba.tg_user_id AND at.manager_key = mba.manager_key
            WHERE COALESCE(mba.can_receive_cards,0)=1
              AND COALESCE(mba.revoked,0)=0
              AND COALESCE(au.is_enabled,0)=1
              AND COALESCE(au.scope_mode,'selected')='selected'
              AND at.manager_key IS NULL
            """
        ).fetchall()
        pairs = [(int(r["uid"]), str(r["mk"])) for r in candidates]

        if dry_run or not pairs:
            return {
                "status": "dry_run" if dry_run else "no_op",
                "candidate_count": len(pairs),
                "candidates_sample": [
                    {"actor_ref": _w2_pseudonymize("w2-repair", u), "manager_key": m}
                    for u, m in pairs[:25]
                ],
                "before_revoked": before_revoked, "before_disabled": before_disabled,
                "before_access_targets": before_targets,
            }

        now = _now_iso()
        con.execute("BEGIN IMMEDIATE")
        try:
            for uid, mk in pairs:
                con.execute(
                    "INSERT OR IGNORE INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,?)",
                    (uid, mk, now),
                )
            after_revoked = con.execute("SELECT COUNT(*) FROM manager_bot_access WHERE revoked=1").fetchone()[0]
            after_disabled = con.execute("SELECT COUNT(*) FROM access_users WHERE COALESCE(is_enabled,1)=0").fetchone()[0]
            after_targets = con.execute("SELECT COUNT(*) FROM access_targets").fetchone()[0]

            if after_revoked != before_revoked or after_disabled != before_disabled or after_targets < before_targets:
                con.rollback()
                return {
                    "status": "aborted_stop_condition",
                    "before_revoked": before_revoked, "after_revoked": after_revoked,
                    "before_disabled": before_disabled, "after_disabled": after_disabled,
                    "before_access_targets": before_targets, "after_access_targets": after_targets,
                }
            con.commit()
        except Exception:
            con.rollback()
            raise

        return {
            "status": "repaired",
            "actor_tag": actor_tag,
            "repaired_count": len(pairs),
            "before_access_targets": before_targets,
            "after_access_targets": after_targets,
            "before_revoked": before_revoked, "after_revoked": after_revoked,
            "before_disabled": before_disabled, "after_disabled": after_disabled,
        }
    finally:
        con.close()


class W2SchemaCompatibilityError(RuntimeError):
    """Raised by w2_assert_lead_cards_schema_compatible when manager_lead_cards
    exists in its pre-W2 (narrow-key) form. Deliberately NOT caught by
    manager_bot.py's init_schema() -- see the REVISION note on
    w2_manager_lead_cards_schema_status for why silently continuing here
    was the actual Blocker 5 defect."""


def w2_manager_lead_cards_schema_status(db_path=None):
    # type: (Optional[str]) -> Dict[str, Any]
    """Read-only compatibility check (W2 REVISION, Blocker 5). Returns
    {"status": "not_created" | "current" | "legacy_narrow_key" | "unknown_columns", ...}.
    Never writes anything -- this is the function startup is allowed to call
    unconditionally; the actual rebuild migration
    (w2_manager_lead_cards_add_date_identity, below) is NOT called from
    here and must never be reached from ordinary process startup."""
    con = _bsl_connect(db_path)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_lead_cards'"
        ).fetchone()
        if not exists:
            return {"status": "not_created"}

        idx_rows = con.execute("PRAGMA index_list(manager_lead_cards)").fetchall()
        for idx in idx_rows:
            if not int(idx["unique"] or 0):
                continue
            idx_cols = {r["name"] for r in con.execute(f"PRAGMA index_info({idx['name']})").fetchall()}
            if idx_cols == {"tg_user_id", "chat_id", "manager_key", "lead_date"}:
                return {"status": "current"}

        col_names = [str(c["name"]) for c in con.execute("PRAGMA table_info(manager_lead_cards)").fetchall()]
        base_cols = {"id", "tg_user_id", "bot_chat_id", "message_id", "chat_id", "manager_key",
                     "lead_date", "last_status_shown", "last_bucket_shown", "last_manual_flag",
                     "last_action_at", "created_at", "updated_at"}
        expected_extra = {"status_set_at", "last_status_reminder_at", "last_status_reminder_message_id"}
        unknown = set(col_names) - base_cols - expected_extra
        if unknown:
            return {"status": "unknown_columns", "unknown_columns": sorted(unknown)}

        row_count = con.execute("SELECT COUNT(*) FROM manager_lead_cards").fetchone()[0]
        return {"status": "legacy_narrow_key", "row_count": row_count}
    finally:
        con.close()


def w2_assert_lead_cards_schema_compatible(db_path=None):
    # type: (Optional[str]) -> Dict[str, Any]
    """W2 REVISION (Blocker 5): the ONLY manager_lead_cards check
    manager_bot.py's init_schema() is allowed to call at ordinary process
    startup. "not_created" (fresh DB -- init_schema's own CREATE TABLE IF
    NOT EXISTS already declares the CURRENT wide-key UNIQUE, so a fresh
    table is compatible the instant it's created) and "current" both pass
    silently. "legacy_narrow_key" (a real pre-W2 table -- the actual
    at-risk case: card-identity bookkeeping would silently misbehave
    against the narrower key) and "unknown_columns" (a schema this code
    does not recognize -- refuse to guess) both RAISE
    W2SchemaCompatibilityError, uncaught, so the process fails to start
    rather than silently running delivery code against an incompatible
    schema. This function performs NO writes and NO migration -- fixing a
    "legacy_narrow_key" DB requires an explicit, operator-invoked run of
    tools/w2_lead_date_migration_runner.py (never automatic, never from
    startup)."""
    status = w2_manager_lead_cards_schema_status(db_path)
    if status["status"] in ("not_created", "current"):
        return status
    if status["status"] == "legacy_narrow_key":
        raise W2SchemaCompatibilityError(
            "manager_lead_cards exists with the pre-W2 UNIQUE(tg_user_id,chat_id,manager_key) "
            "key (row_count=%s). Card-identity bookkeeping (D-06) requires the widened "
            "UNIQUE(tg_user_id,chat_id,manager_key,lead_date) key. Run "
            "'python tools\\w2_lead_date_migration_runner.py <db_path>' explicitly before "
            "starting ManagerBot against this database. Startup refuses to continue against "
            "an incompatible schema (Blocker 5 fail-closed requirement)." % status.get("row_count")
        )
    raise W2SchemaCompatibilityError(
        "manager_lead_cards has unrecognized columns %r -- refusing to guess a migration path. "
        "Startup refuses to continue." % status.get("unknown_columns")
    )


def w2_manager_lead_cards_add_date_identity(db_path=None):
    # type: (Optional[str]) -> Dict[str, Any]
    """THE ACTUAL REBUILD MIGRATION ENGINE (W2 REVISION, Blocker 5): this
    function must be called ONLY from tools/w2_lead_date_migration_runner.py
    (an explicit, operator-invoked, controlled migration mode) or from a
    test. It is deliberately NOT wired into manager_bot.py's init_schema()
    -- ordinary process startup calls w2_assert_lead_cards_schema_compatible
    (read-only, above) instead, and fails closed on a legacy schema rather
    than silently attempting (or silently skipping) this rebuild every time
    the process starts.

    D-06/B1/I-01/I-02: widen manager_lead_cards' identity from
    (tg_user_id, chat_id, manager_key) to (tg_user_id, chat_id, manager_key,
    lead_date), so the SAME peer contacting again on a genuinely different
    business date gets its own physical card instead of overwriting the
    prior date's card. Idempotent (checks the live schema first) and
    additive in effect: every existing row keeps its `id` (I-02, so
    already-delivered inline buttons keep working) and its existing
    `lead_date` value untouched (10.4 STOP condition); the widened key can
    only ADMIT rows a narrower key would have rejected, so no existing row
    can ever violate it. Table missing entirely (ManagerBot schema not yet
    initialized on this DB) -> no-op, status='table_missing' (manager_bot.py's
    own init_schema is what creates it first, and calls this function right
    after).

    Implementation note: SQLite has no ALTER TABLE ... DROP/ADD CONSTRAINT.
    The existing UNIQUE(tg_user_id, chat_id, manager_key) is a table-level
    constraint from the original CREATE TABLE, not a droppable standalone
    index -- keeping it "alongside" a new, WIDER unique key is not just
    unnecessary but self-defeating: a narrower UNIQUE constraint would keep
    rejecting the very same-peer/different-date inserts this migration
    exists to admit. This function therefore performs the standard SQLite
    rebuild (create the new table with the widened UNIQUE, copy every row
    verbatim INCLUDING id, verify the row count matches exactly, then swap
    names) inside one transaction, entirely rolled back on any mismatch or
    on any column this function does not recognize. The two secondary
    (non-unique) lookup indexes are recreated identically afterward. This
    is documented here and in 05_CARD_IDEMPOTENCY.md as an explicit,
    reasoned deviation from a literal "index alongside index" reading of
    the frozen plan text -- the SAFETY property the plan is actually
    protecting (no existing row lost, no id renumbered, fully
    transactional/reversible-on-failure) is preserved exactly.

    REVISION (2026-07-29, Blocker 5 follow-up): the old table is RENAMED
    aside (manager_lead_cards_prew2_legacy) rather than removed outright.
    Two reasons: (1) this project's own tools/reserve_release_selftest.py
    enforces a project-wide invariant that storage.py contains no
    schema-removing SQL of that kind at all, predating this migration --
    that invariant is worth keeping, not carving an exception into; (2) it
    is simply safer: the pre-migration data stays queryable inside the
    SAME database file indefinitely, on top of (not instead of) the
    migration runner's own separate file-level backup copy."""
    con = _bsl_connect(db_path)
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_lead_cards'"
        ).fetchone()
        if not exists:
            return {"status": "table_missing"}

        idx_rows = con.execute("PRAGMA index_list(manager_lead_cards)").fetchall()
        for idx in idx_rows:
            if not int(idx["unique"] or 0):
                continue
            idx_cols = {r["name"] for r in con.execute(f"PRAGMA index_info({idx['name']})").fetchall()}
            if idx_cols == {"tg_user_id", "chat_id", "manager_key", "lead_date"}:
                return {"status": "already_migrated"}

        legacy_exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_lead_cards_prew2_legacy'"
        ).fetchone()
        if legacy_exists:
            return {"status": "aborted_legacy_name_collision"}

        before_count = con.execute("SELECT COUNT(*) FROM manager_lead_cards").fetchone()[0]
        col_names = [str(c["name"]) for c in con.execute("PRAGMA table_info(manager_lead_cards)").fetchall()]

        base_cols = {"id", "tg_user_id", "bot_chat_id", "message_id", "chat_id", "manager_key",
                     "lead_date", "last_status_shown", "last_bucket_shown", "last_manual_flag",
                     "last_action_at", "created_at", "updated_at"}
        expected_extra = {"status_set_at", "last_status_reminder_at", "last_status_reminder_message_id"}
        unknown = set(col_names) - base_cols - expected_extra
        if unknown:
            return {"status": "aborted_unknown_columns", "unknown_columns": sorted(unknown)}

        target_cols = [c for c in col_names if c in base_cols or c in expected_extra]

        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """
                CREATE TABLE manager_lead_cards_w2new(
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
                    status_set_at TEXT NOT NULL DEFAULT '',
                    last_status_reminder_at TEXT NOT NULL DEFAULT '',
                    last_status_reminder_message_id INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(tg_user_id, chat_id, manager_key, lead_date)
                )
                """
            )
            con.execute(
                f"INSERT INTO manager_lead_cards_w2new ({','.join(target_cols)}) "
                f"SELECT {','.join(target_cols)} FROM manager_lead_cards"
            )
            after_count = con.execute("SELECT COUNT(*) FROM manager_lead_cards_w2new").fetchone()[0]
            if after_count != before_count:
                con.rollback()
                return {"status": "aborted_row_count_mismatch", "before": before_count, "after": after_count}

            con.execute("ALTER TABLE manager_lead_cards RENAME TO manager_lead_cards_prew2_legacy")
            con.execute("ALTER TABLE manager_lead_cards_w2new RENAME TO manager_lead_cards")
            con.execute("CREATE INDEX IF NOT EXISTS manager_lead_cards_msg_idx ON manager_lead_cards(bot_chat_id, message_id)")
            con.execute("CREATE INDEX IF NOT EXISTS manager_lead_cards_lead_idx ON manager_lead_cards(chat_id, manager_key)")
            con.commit()
        except Exception:
            con.rollback()
            raise

        return {"status": "migrated", "row_count": before_count}
    finally:
        con.close()


def w2_cutoff_columns_ensure(db_path=None):
    # type: (Optional[str]) -> Dict[str, Any]
    """D-24/D-03: additive columns on manager_bot_access recording WHY and
    WHEN a cutoff was set (never WHETHER -- event_cutoff_id itself is
    untouched by this function), plus a append-only observability log
    table. Idempotent (PRAGMA table_info guard, same idiom as every other
    migration in this project)."""
    con = _bsl_connect(db_path)
    try:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(manager_bot_access)").fetchall()]
        added = []
        for col, decl in (("cutoff_reason", "TEXT NOT NULL DEFAULT ''"),
                           ("cutoff_set_at", "TEXT NOT NULL DEFAULT ''")):
            if col not in cols:
                con.execute(f"ALTER TABLE manager_bot_access ADD COLUMN {col} {decl}")
                added.append(col)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS manager_bot_cutoff_log(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_user_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL DEFAULT '',
                cutoff_id INTEGER NOT NULL DEFAULT 0,
                suppressed_count INTEGER NOT NULL DEFAULT 0,
                manager_state TEXT NOT NULL DEFAULT '',
                computed_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_mb_cutoff_log_pair ON manager_bot_cutoff_log(tg_user_id, manager_key)")
        con.commit()
        return {"status": "ok", "added_columns": added}
    finally:
        con.close()


def w2_cutoff_observability_snapshot(db_path=None, persist=True):
    # type: (Optional[str], bool) -> List[Dict[str, Any]]
    """Read-only computation of currently cutoff-suppressed events per
    (tg_user_id, manager_key) -- an event with id <= event_cutoff_id that is
    otherwise fully eligible (can_receive_cards=1, revoked=0). Does NOT
    change event_cutoff_id, does NOT re-deliver anything (D-24's explicit
    prohibition on raising cutoff as a "fix") -- purely additive
    observability. When persist=True (the default), each non-zero snapshot
    row is appended to manager_bot_cutoff_log for a historical record;
    persist=False is used by reports/selftests that only want the computed
    numbers without writing."""
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT mba.tg_user_id AS uid, mba.manager_key AS mk, mba.event_cutoff_id AS cutoff,
                   (SELECT COUNT(*) FROM manager_bot_events e
                     WHERE e.manager_key = mba.manager_key AND e.id <= mba.event_cutoff_id) AS suppressed,
                   (SELECT COALESCE(status,'') || ':' || COALESCE(CAST(is_enabled AS TEXT),'')
                      FROM managers m WHERE m.manager_key = mba.manager_key LIMIT 1) AS manager_state
            FROM manager_bot_access mba
            WHERE COALESCE(mba.can_receive_cards,0)=1 AND COALESCE(mba.revoked,0)=0
              AND COALESCE(mba.event_cutoff_id,0) > 0
            """
        ).fetchall()
        now = _now_iso()
        out = []
        for r in rows:
            entry = {
                "actor_ref": _w2_pseudonymize("w2-cutoff", int(r["uid"])),
                "manager_key": str(r["mk"]),
                "cutoff_id": int(r["cutoff"] or 0),
                "suppressed_count": int(r["suppressed"] or 0),
                "manager_state": str(r["manager_state"] or "unknown"),
                "computed_at": now,
            }
            out.append(entry)
            if persist and entry["suppressed_count"] > 0:
                con.execute(
                    "INSERT INTO manager_bot_cutoff_log(tg_user_id, manager_key, cutoff_id, suppressed_count, manager_state, computed_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (int(r["uid"]), str(r["mk"]), int(r["cutoff"] or 0), int(r["suppressed"] or 0),
                     str(r["manager_state"] or "unknown"), now),
                )
        if persist:
            con.commit()
        return out
    except Exception:
        return []
    finally:
        con.close()


def w2_persist_cutoff_suppressed_events(db_path=None):
    # type: (Optional[str]) -> Dict[str, Any]
    """W2 REVISION (Blocker 3): CUTOFF_SUPPRESSED as a REAL, persisted,
    per-EVENT outcome (not just the aggregate count
    w2_cutoff_observability_snapshot already computes). For every active
    (can_receive_cards=1, revoked=0, event_cutoff_id>0) manager_bot_access
    row, every manager_bot_events row with id<=cutoff gets an idempotent
    INSERT OR IGNORE into manager_bot_sent with send_status=
    'cutoff_suppressed'.

    Safety: this does NOT change event_cutoff_id (D-24's prohibition is
    untouched) and does NOT change what _fetch_unsent_events_for_user
    selects -- that function's own `e.id > COALESCE(mba.event_cutoff_id,0)`
    condition ALREADY excludes these events unconditionally, regardless of
    whether a manager_bot_sent row exists for them. This function exists
    purely so the outcome is OBSERVABLE per-event (query manager_bot_sent
    directly) rather than only as an aggregate count. INSERT OR IGNORE
    means an event that already has ANY manager_bot_sent row (e.g. it was
    genuinely sent before the cutoff was raised at grant time, or is
    already in some other state) is left completely untouched -- this
    function never overwrites an existing row."""
    con = _bsl_connect(db_path)
    try:
        pairs = con.execute(
            """
            SELECT tg_user_id, manager_key, event_cutoff_id FROM manager_bot_access
            WHERE COALESCE(can_receive_cards,0)=1 AND COALESCE(revoked,0)=0
              AND COALESCE(event_cutoff_id,0) > 0
            """
        ).fetchall()
        inserted = 0
        for p in pairs:
            uid, mk, cutoff = int(p["tg_user_id"]), str(p["manager_key"]), int(p["event_cutoff_id"] or 0)
            event_ids = [r["id"] for r in con.execute(
                "SELECT id FROM manager_bot_events WHERE manager_key=? AND id<=?", (mk, cutoff)
            ).fetchall()]
            for eid in event_ids:
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO manager_bot_sent(
                        tg_user_id, event_id, sent_at, attempts, next_attempt_at,
                        send_status, last_error_class, fallback_used
                    ) VALUES (?, ?, '', 0, '', 'cutoff_suppressed', 'event_cutoff_id', 0)
                    """,
                    (uid, int(eid)),
                )
                inserted += cur.rowcount
        con.commit()
        return {"status": "ok", "pairs_checked": len(pairs), "rows_inserted": inserted}
    except Exception as exc:
        return {"status": "error", "error_class": type(exc).__name__}
    finally:
        con.close()


def w2_terminal_events_snapshot(db_path=None):
    # type: (Optional[str]) -> Dict[str, int]
    """W2 REVISION (Blocker 3): read-only consumer/report for the 4
    non-attempt persisted outcomes -- counts per state, for reporting."""
    con = _bsl_connect(db_path)
    try:
        out = {}
        for state in ("terminal_access_denied", "terminal_manager_deleted",
                      "terminal_invalid_event", "cutoff_suppressed"):
            out[state] = con.execute(
                "SELECT COUNT(*) FROM manager_bot_sent WHERE send_status=?", (state,)
            ).fetchone()[0]
        return out
    except Exception:
        return {}
    finally:
        con.close()


def w2_delivery_failures_ensure_table(db_path=None):
    # type: (Optional[str]) -> None
    con = _bsl_connect(db_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS manager_bot_delivery_failures(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc TEXT NOT NULL DEFAULT '',
                ts_kyiv TEXT NOT NULL DEFAULT '',
                event_ref INTEGER NOT NULL DEFAULT 0,
                manager_key TEXT NOT NULL DEFAULT '',
                recipient_ref TEXT NOT NULL DEFAULT '',
                failure_class TEXT NOT NULL DEFAULT '',
                retryable INTEGER NOT NULL DEFAULT 1,
                attempt_number INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT NOT NULL DEFAULT '',
                terminal_action TEXT NOT NULL DEFAULT '',
                correlation_id TEXT NOT NULL DEFAULT ''
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_mb_delivery_failures_event ON manager_bot_delivery_failures(event_ref)")
        con.commit()
    finally:
        con.close()


def w2_log_delivery_failure(db_path=None, event_id=0, manager_key="", tg_user_id=0,
                             failure_class="", retryable=True, attempt_number=0,
                             next_retry_at="", terminal_action="", correlation_id=""):
    # type: (Optional[str], int, str, int, str, bool, int, str, str, str) -> None
    """Structured delivery-failure record (Phase D). No raw tg_user_id, no
    exception str()/repr() -- only an already-classified failure_class (a
    class NAME, e.g. 'InputUserDeactivatedError'), a pseudonymized
    recipient_ref, and non-sensitive scalars. Best-effort: a logging
    failure here must never interrupt the caller's own retry/dead-letter
    bookkeeping (that state lives in manager_bot_sent, written separately)
    -- so this function swallows its own exceptions."""
    try:
        from zoneinfo import ZoneInfo
        w2_delivery_failures_ensure_table(db_path)
        con = _bsl_connect(db_path)
        try:
            now = _now_iso()
            try:
                kyiv = _utc_now().replace(microsecond=0, tzinfo=ZoneInfo("UTC")).astimezone(
                    ZoneInfo("Europe/Kyiv")).replace(tzinfo=None).isoformat()
            except Exception:
                kyiv = now
            cid = str(correlation_id or "") or os.urandom(6).hex()
            con.execute(
                """
                INSERT INTO manager_bot_delivery_failures(
                    ts_utc, ts_kyiv, event_ref, manager_key, recipient_ref,
                    failure_class, retryable, attempt_number, next_retry_at,
                    terminal_action, correlation_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (now, kyiv, int(event_id or 0), str(manager_key or ""),
                 _w2_pseudonymize("w2-recipient", int(tg_user_id or 0)),
                 str(failure_class or "")[:120], 1 if retryable else 0, int(attempt_number or 0),
                 str(next_retry_at or ""), str(terminal_action or ""), cid),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def w2_stale_sending_events(db_path=None, stale_after_seconds=None):
    # type: (Optional[str], Optional[int]) -> List[Dict[str, Any]]
    """Read-only observability (Blocker 1 revision): every manager_bot_sent
    row currently send_status='sending', annotated with whether its lease
    has already expired (`lease_expired`) -- i.e. whether the NEXT poll
    cycle's manager_bot.py::_mb_recover_stale_claims pass will pick it up.
    This function itself NEVER writes anything (the actual recovery -- the
    only thing allowed to transition a row out of 'sending' -- lives in
    manager_bot.py, since it needs the real jittered-backoff/attempts-bound
    logic that function already owns; duplicating that logic here would
    risk the two drifting apart). `stale_after_seconds`, if given, overrides
    what counts as "expired" for reporting purposes only (defaults to the
    row's own lease_expires_at, i.e. the SAME rule recovery itself uses);
    pass it to ask "how many are already X seconds old" for a report,
    without changing recovery's own bound. Returns [] (not an error) if
    the lease_expires_at column does not exist yet (pre-revision DB)."""
    con = _bsl_connect(db_path)
    try:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(manager_bot_sent)").fetchall()]
        if "lease_expires_at" not in cols:
            return []
        now = _utc_now().replace(microsecond=0).isoformat()
        rows = con.execute(
            "SELECT tg_user_id, event_id, claimed_at, lease_expires_at, claim_worker_ref FROM manager_bot_sent "
            "WHERE send_status='sending'"
        ).fetchall()
        out = []
        for r in rows:
            lease_expires_at = str(r["lease_expires_at"] or "")
            expired = bool(lease_expires_at) and lease_expires_at <= now
            if stale_after_seconds is not None:
                cutoff = (_utc_now() - timedelta(seconds=stale_after_seconds)).replace(microsecond=0).isoformat()
                claimed_at = str(r["claimed_at"] or "")
                expired = bool(claimed_at) and claimed_at <= cutoff
            out.append({
                "actor_ref": _w2_pseudonymize("w2-stale", int(r["tg_user_id"])),
                "event_id": int(r["event_id"]),
                "claimed_at": str(r["claimed_at"] or ""),
                "lease_expires_at": lease_expires_at,
                "lease_expired": expired,
                "claim_worker_ref": str(r["claim_worker_ref"] or ""),
            })
        return out
    except Exception:
        return []
    finally:
        con.close()


def w2_deleted_manager_orphan_snapshot(db_path=None):
    # type: (Optional[str]) -> List[Dict[str, Any]]
    """Read-only classification (10.2/10.9 style): manager_lead_cards rows
    whose manager_key no longer resolves to an active row in `managers` are
    'orphaned' -- historical, NEVER deleted, NEVER modified by this
    function. Cross-referenced against manager_stats_tombstones so a
    report can say whether the identity is still resolvable for stats."""
    con = _bsl_connect(db_path)
    try:
        rows = con.execute(
            """
            SELECT mlc.manager_key AS mk, COUNT(*) AS card_count,
                   (SELECT 1 FROM manager_stats_tombstones t WHERE t.manager_key = mlc.manager_key) AS has_tombstone
            FROM manager_lead_cards mlc
            LEFT JOIN managers m ON m.manager_key = mlc.manager_key
              AND COALESCE(m.status,'')='active' AND COALESCE(m.is_enabled,0)=1
            WHERE m.manager_key IS NULL AND COALESCE(mlc.manager_key,'')<>''
            GROUP BY mlc.manager_key
            ORDER BY card_count DESC
            """
        ).fetchall()
        return [
            {"manager_key": str(r["mk"]), "card_count": int(r["card_count"]),
             "has_tombstone": bool(r["has_tombstone"])}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        con.close()
# --- TPILOT W2 ACCESS & DELIVERY 20260729 END ---


# --- TPILOT W3.1 SCHEDULE RESOLVER FOUNDATION + DUPLICATE STATUS GATE 20260729 START ---
# W3.1 (frozen W3 Plan Freeze, storage.py-only sub-wave -- see
# C:\ALM_TPilot_AUDIT\20260729\W3_PLAN_FREEZE\05_CANONICAL_RESOLVER_CONTRACT.md,
# 06_VERSIONING_AND_HISTORY_SCHEMA.md, 16_DUPLICATE_STATUS_OWNER_ADDENDUM.md):
# additive effective-dated schedule-versioning schema, a canonical READ-side resolver
# foundation, writer-helper foundations reserved for future W3.6 routing, and the pure
# duplicate-status eligibility gate. W3.1 is FULLY INERT:
#   - with every w3_* table empty, w3_resolve_schedule reduces bit-identically to
#     _manager_effective_is_working_row (2586), _tp_gq_get_schedule (main.py:19908) and
#     source_work_window_get (2339) -- proven by tools/w3_resolver_foundation_selftest.py;
#   - no existing reader or writer anywhere in the project is redirected to any function
#     in this block; nothing here is called at module import or process start;
#   - w3_duplicate_status_eligibility is a pure function (no I/O, no writes, no logging,
#     no clock read) that nothing calls yet -- its write/render boundary is wave W3.5-A,
#     its statistics boundary is wave W3.5-B.
# Placement rationale (identical to W2's, see W2 banner above): all five processes
# already import storage.py, and it has zero module-level side effects, so selftests use
# a REAL import rather than the AST-surgery technique main.py/panel_bot.py require.
import json as _w3_json
import re as _w3_re
from zoneinfo import ZoneInfo as _W3_ZoneInfo
from datetime import timezone as _w3_timezone_mod

_w3_utc_tz = _w3_timezone_mod.utc

W3_TZ_NAME = "Europe/Kyiv"
W3_SCHEDULE_SCHEMA_VERSION = 1
# Bumped 2026-07-29 (independent-review finding F-1 correction, owner decision LOCKED):
# countable_allowed/countable now derive from authoritative_duplicate (tier 1: S-1 OR
# S-2), not from the raw S-1 `duplicate` flag alone -- an event_type='duplicate_card'
# row with duplicate==0/None was previously reported countable=True while
# is_duplicate=True, an internal contradiction. New output keys added (additive,
# nothing removed): authoritative_duplicate, countable_allowed,
# ordinary_status_write_allowed, ordinary_status_render_allowed,
# ordinary_status_callback_allowed, display_state, duplicate_history_required.
W3_DUP_SCHEMA_VERSION = 2

_W3_DATE_RE = _w3_re.compile(r"^\d{4}-\d{2}-\d{2}$")
_W3_HHMM_RE = _w3_re.compile(r"^\d{2}:\d{2}$")
_W3_TZ_CACHE: Dict[str, Any] = {}
_W3_MESSAGE_DEFAULT = ("08:00", "17:00", "17:00", "08:00")  # day_start, day_end, night_start, night_end


class W3TimezoneError(Exception):
    """Raised when the requested IANA timezone cannot be loaded. Never falls back to
    the OS-local zone -- a silent OS-local fallback was inventory's #1 risk finding
    (05_CANONICAL_RESOLVER_CONTRACT.md 5.4); a loud failure here is recoverable, a quiet
    date shift downstream is not."""


class W3NaiveDatetimeError(Exception):
    """Raised when at_instant is a naive datetime. Coercion is exactly how the OS-local
    timezone bug spreads silently between callers; existing call sites already pass
    aware datetimes (e.g. main.py's _kyiv_now()) and are unaffected."""


def w3_tz(tz_name=W3_TZ_NAME):
    # type: (str) -> Any
    """Cached ZoneInfo lookup. RAISES W3TimezoneError on failure -- never returns a
    fallback zone."""
    name = str(tz_name or W3_TZ_NAME)
    cached = _W3_TZ_CACHE.get(name)
    if cached is not None:
        return cached
    try:
        zi = _W3_ZoneInfo(name)
    except Exception as exc:
        raise W3TimezoneError(f"w3: cannot load timezone {name!r}: {exc}") from exc
    _W3_TZ_CACHE[name] = zi
    return zi


def w3_now(tz_name=W3_TZ_NAME):
    # type: (str) -> datetime
    """Always an aware datetime. No fallback."""
    return datetime.now(w3_tz(tz_name))


def _w3_require_aware(at_instant):
    if at_instant.tzinfo is None or at_instant.utcoffset() is None:
        raise W3NaiveDatetimeError("w3: at_instant must be an aware datetime")


def w3_business_date(tz_name=W3_TZ_NAME, at_instant=None):
    # type: (str, Optional[datetime]) -> str
    if at_instant is not None:
        _w3_require_aware(at_instant)
        local = at_instant.astimezone(w3_tz(tz_name))
    else:
        local = w3_now(tz_name)
    return local.strftime("%Y-%m-%d")


def _w3_classify_local(naive, tz):
    # type: (datetime, Any) -> Tuple[bool, bool]
    """Classify a naive wall-clock datetime against `tz`'s DST rules without ever
    hardcoding an offset -- always asks ZoneInfo (02_S9_TIMEZONE_CONTRACT.md 2.6).
    Returns (is_gap, is_ambiguous). Method: construct both fold candidates, convert
    each to UTC, then convert back to `tz` (UTC->local is never ambiguous) and compare
    the round-tripped wall clock to the original. A genuine nonexistent (gap) local time
    fails the round-trip under BOTH folds; a genuine ambiguous (fall-back) local time
    round-trips correctly under both (it really did occur, twice); an ordinary
    unambiguous time yields identical UTC instants for both folds."""
    u0 = naive.replace(tzinfo=tz, fold=0).astimezone(_w3_utc_tz)
    u1 = naive.replace(tzinfo=tz, fold=1).astimezone(_w3_utc_tz)
    if u0 == u1:
        return False, False
    key = (naive.hour, naive.minute, naive.second)
    back0 = u0.astimezone(tz)
    back1 = u1.astimezone(tz)
    valid0 = (back0.hour, back0.minute, back0.second) == key
    valid1 = (back1.hour, back1.minute, back1.second) == key
    if not valid0 and not valid1:
        return True, False
    return False, True


def _w3_snap_dst_gap(naive, tz):
    # type: (datetime, Any) -> datetime
    """`naive` falls inside a spring-forward gap (nonexistent local time). Snap FORWARD
    to the exact transition instant (02_S9_TIMEZONE_CONTRACT.md 2.6: "03:30 -> 04:00"),
    found by binary search on the UTC axis for the boundary where `tz`'s utcoffset
    changes -- never by adding a hardcoded gap size, so a tzdata rule change (different
    gap length, different transition rule) cannot silently desync this from reality."""
    u_a = naive.replace(tzinfo=tz, fold=0).astimezone(_w3_utc_tz)
    u_b = naive.replace(tzinfo=tz, fold=1).astimezone(_w3_utc_tz)
    lo, hi = (u_a, u_b) if u_a < u_b else (u_b, u_a)
    off_lo = lo.astimezone(tz).utcoffset()
    for _ in range(40):
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == off_lo:
            lo = mid
        else:
            hi = mid
    return hi.astimezone(tz).replace(microsecond=0)


def w3_local_at(business_date, minute, *, which="start", tz_name=W3_TZ_NAME, degraded=None):
    # type: (str, int, ..., Optional[List[str]]) -> datetime
    """Localize a (business_date, minute-of-day) pair to an aware datetime in tz_name.
    which='start' uses fold=0 (first occurrence of an ambiguous local time), which='end'
    uses fold=1 (second occurrence) -- the DST-fold convention frozen in
    02_S9_TIMEZONE_CONTRACT.md 2.6. If the requested wall-clock time does not exist
    (spring-forward gap), the result is snapped FORWARD to the exact transition instant
    and, if `degraded` (a list) is passed, "dst_gap_snapped" is appended. If the
    requested time is genuinely ambiguous (fall-back) the fold above already resolves it
    per contract; "dst_fold_ambiguous" is appended to `degraded` for observability only
    -- the returned instant does not change. Signature is unchanged for every existing
    caller: `degraded` is optional and defaults to not being consulted."""
    d = datetime.strptime(str(business_date), "%Y-%m-%d").date()
    m = int(minute) % 1440
    naive = datetime(d.year, d.month, d.day, m // 60, m % 60)
    fold = 1 if which == "end" else 0
    tz = w3_tz(tz_name)
    is_gap, is_ambiguous = _w3_classify_local(naive, tz)
    if is_gap:
        if degraded is not None:
            degraded.append("dst_gap_snapped")
        return _w3_snap_dst_gap(naive, tz)
    if is_ambiguous and degraded is not None:
        degraded.append("dst_fold_ambiguous")
    return naive.replace(tzinfo=tz, fold=fold)


def w3_parse_utc_to_local(s, tz_name=W3_TZ_NAME):
    # type: (str, str) -> Optional[datetime]
    """Tolerates a naive 'YYYY-MM-DDTHH:MM:SS' (assumed UTC, storage._now_iso() form), a
    trailing 'Z', or an explicit offset. Returns None (not a raise) on unparseable input
    -- a best-effort convenience parser, not one of the resolver's two hard-fail paths."""
    txt = str(s or "").strip()
    if not txt:
        return None
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_W3_ZoneInfo("UTC"))
        return dt.astimezone(w3_tz(tz_name))
    except Exception:
        return None


def _w3_json_dumps(obj):
    # type: (Any) -> str
    """Deterministic JSON encoding for w3_schedule_audit.old_json/new_json (06 6.6)."""
    return _w3_json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


# --- W3.1-B: additive schema (06_VERSIONING_AND_HISTORY_SCHEMA.md) ---------------------
# Every new table carries the w3_ prefix so "no legacy table was altered" is statically
# provable. Version interval is HALF-OPEN [effective_from, effective_to); exception
# interval is INCLUSIVE [date_from, date_to] -- deliberately different column names so
# the two semantics cannot be confused (06 6.1). DELETE is never applied to version
# history; reset/unlink/reassignment CLOSE an interval instead.

_W3_OVERLAP_TRIGGER_DDL = (
    """
    CREATE TRIGGER IF NOT EXISTS w3_ssv_no_overlap_ins
    BEFORE INSERT ON w3_source_schedule_version
    FOR EACH ROW WHEN EXISTS (
        SELECT 1 FROM w3_source_schedule_version v
         WHERE v.source_key = NEW.source_key AND v.dimension = NEW.dimension
           AND (NEW.effective_to IS NULL OR v.effective_from < NEW.effective_to)
           AND (v.effective_to IS NULL OR NEW.effective_from < v.effective_to)
    ) BEGIN SELECT RAISE(ABORT, 'w3: overlapping source schedule version'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS w3_ssv_no_overlap_upd
    BEFORE UPDATE OF effective_from, effective_to, source_key, dimension
    ON w3_source_schedule_version
    FOR EACH ROW WHEN EXISTS (
        SELECT 1 FROM w3_source_schedule_version v
         WHERE v.id <> NEW.id AND v.source_key = NEW.source_key AND v.dimension = NEW.dimension
           AND (NEW.effective_to IS NULL OR v.effective_from < NEW.effective_to)
           AND (v.effective_to IS NULL OR NEW.effective_from < v.effective_to)
    ) BEGIN SELECT RAISE(ABORT, 'w3: overlapping source schedule version'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS w3_msv_no_overlap_ins
    BEFORE INSERT ON w3_manager_schedule_version
    FOR EACH ROW WHEN EXISTS (
        SELECT 1 FROM w3_manager_schedule_version v
         WHERE v.manager_key = NEW.manager_key AND v.dimension = NEW.dimension
           AND (NEW.effective_to IS NULL OR v.effective_from < NEW.effective_to)
           AND (v.effective_to IS NULL OR NEW.effective_from < v.effective_to)
    ) BEGIN SELECT RAISE(ABORT, 'w3: overlapping manager schedule version'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS w3_msv_no_overlap_upd
    BEFORE UPDATE OF effective_from, effective_to, manager_key, dimension
    ON w3_manager_schedule_version
    FOR EACH ROW WHEN EXISTS (
        SELECT 1 FROM w3_manager_schedule_version v
         WHERE v.id <> NEW.id AND v.manager_key = NEW.manager_key AND v.dimension = NEW.dimension
           AND (NEW.effective_to IS NULL OR v.effective_from < NEW.effective_to)
           AND (v.effective_to IS NULL OR NEW.effective_from < v.effective_to)
    ) BEGIN SELECT RAISE(ABORT, 'w3: overlapping manager schedule version'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS w3_msh_no_overlap_ins
    BEFORE INSERT ON w3_manager_source_history
    FOR EACH ROW WHEN EXISTS (
        SELECT 1 FROM w3_manager_source_history v
         WHERE v.manager_key = NEW.manager_key
           AND (NEW.effective_to IS NULL OR v.effective_from < NEW.effective_to)
           AND (v.effective_to IS NULL OR NEW.effective_from < v.effective_to)
    ) BEGIN SELECT RAISE(ABORT, 'w3: overlapping manager source history'); END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS w3_msh_no_overlap_upd
    BEFORE UPDATE OF effective_from, effective_to, manager_key ON w3_manager_source_history
    FOR EACH ROW WHEN EXISTS (
        SELECT 1 FROM w3_manager_source_history v
         WHERE v.id <> NEW.id AND v.manager_key = NEW.manager_key
           AND (NEW.effective_to IS NULL OR v.effective_from < NEW.effective_to)
           AND (v.effective_to IS NULL OR NEW.effective_from < v.effective_to)
    ) BEGIN SELECT RAISE(ABORT, 'w3: overlapping manager source history'); END;
    """,
)


def ensure_w3_schedule_versioning(db_path=None):
    # type: (Optional[str]) -> None
    """Create the five additive w3_* schedule-versioning tables, their lookup/uniqueness
    indexes, and their overlap-prevention triggers (layers 1+2 of the three-layer
    defense in 06 6.7; layer 3 is the writer helpers below). Idempotent
    (CREATE ... IF NOT EXISTS throughout). Never touches any legacy table. Callers
    invoke this explicitly -- nothing in this module calls it at import time, and no
    process-startup path in the project calls it either (scope guard: see
    tools/w3_1_scope_guard_selftest.py)."""
    con = _bsl_connect(db_path)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS w3_source_schedule_version(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL,
                dimension TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
                enabled INTEGER NOT NULL DEFAULT 1,
                mon INTEGER, tue INTEGER, wed INTEGER, thu INTEGER,
                fri INTEGER, sat INTEGER, sun INTEGER,
                day_start_min INTEGER, day_end_min INTEGER,
                night_start_min INTEGER, night_end_min INTEGER,
                stats_day_start_min INTEGER, stats_day_end_min INTEGER,
                light_include_dolyoty INTEGER, pro_include_dolyoty INTEGER,
                note TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER,
                created_by_role TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                close_reason TEXT NOT NULL DEFAULT '',
                superseded_by_id INTEGER,
                CHECK (dimension IN ('work','message','stats')),
                CHECK (length(effective_from) = 10 AND effective_from LIKE '____-__-__'),
                CHECK (effective_to IS NULL OR (length(effective_to) = 10 AND effective_to > effective_from)),
                CHECK (day_start_min IS NULL OR (day_start_min BETWEEN 0 AND 1439)),
                CHECK (day_end_min IS NULL OR (day_end_min BETWEEN 0 AND 1439)),
                CHECK (night_start_min IS NULL OR (night_start_min BETWEEN 0 AND 1439)),
                CHECK (night_end_min IS NULL OR (night_end_min BETWEEN 0 AND 1439)),
                CHECK (stats_day_start_min IS NULL OR stats_day_end_min IS NULL
                       OR (0 <= stats_day_start_min AND stats_day_start_min < stats_day_end_min
                           AND stats_day_end_min <= 1439))
            );
            CREATE INDEX IF NOT EXISTS w3_ssv_lookup_idx
                ON w3_source_schedule_version(source_key, dimension, effective_from DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS w3_ssv_one_open_idx
                ON w3_source_schedule_version(source_key, dimension) WHERE effective_to IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS w3_ssv_from_uniq_idx
                ON w3_source_schedule_version(source_key, dimension, effective_from);

            CREATE TABLE IF NOT EXISTS w3_manager_schedule_version(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT NOT NULL,
                dimension TEXT NOT NULL,
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                timezone TEXT NOT NULL DEFAULT 'Europe/Kyiv',
                enabled INTEGER NOT NULL DEFAULT 1,
                mon INTEGER, tue INTEGER, wed INTEGER, thu INTEGER,
                fri INTEGER, sat INTEGER, sun INTEGER,
                day_start_min INTEGER, day_end_min INTEGER,
                night_start_min INTEGER, night_end_min INTEGER,
                stats_day_start_min INTEGER, stats_day_end_min INTEGER,
                light_include_dolyoty INTEGER, pro_include_dolyoty INTEGER,
                source_link_version_id INTEGER,
                close_reason TEXT NOT NULL DEFAULT '',
                superseded_by_id INTEGER,
                note TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER,
                created_by_role TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                CHECK (dimension IN ('work','message','stats')),
                CHECK (length(effective_from) = 10 AND effective_from LIKE '____-__-__'),
                CHECK (effective_to IS NULL OR (length(effective_to) = 10 AND effective_to > effective_from)),
                CHECK (day_start_min IS NULL OR (day_start_min BETWEEN 0 AND 1439)),
                CHECK (day_end_min IS NULL OR (day_end_min BETWEEN 0 AND 1439)),
                CHECK (night_start_min IS NULL OR (night_start_min BETWEEN 0 AND 1439)),
                CHECK (night_end_min IS NULL OR (night_end_min BETWEEN 0 AND 1439))
            );
            CREATE INDEX IF NOT EXISTS w3_msv_lookup_idx
                ON w3_manager_schedule_version(manager_key, dimension, effective_from DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS w3_msv_one_open_idx
                ON w3_manager_schedule_version(manager_key, dimension) WHERE effective_to IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS w3_msv_from_uniq_idx
                ON w3_manager_schedule_version(manager_key, dimension, effective_from);

            CREATE TABLE IF NOT EXISTS w3_manager_source_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT NOT NULL,
                source_key TEXT NOT NULL DEFAULT '',
                effective_from TEXT NOT NULL,
                effective_to TEXT,
                change_kind TEXT NOT NULL DEFAULT '',
                prev_source_key TEXT NOT NULL DEFAULT '',
                override_decision TEXT NOT NULL DEFAULT 'none',
                closed_override_count INTEGER NOT NULL DEFAULT 0,
                decided_by_user_id INTEGER,
                created_by_user_id INTEGER,
                created_by_role TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                CHECK (length(effective_from) = 10),
                CHECK (effective_to IS NULL OR effective_to > effective_from),
                CHECK (override_decision IN ('close_default','preserve_explicit','none'))
            );
            CREATE INDEX IF NOT EXISTS w3_msh_lookup_idx
                ON w3_manager_source_history(manager_key, effective_from DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS w3_msh_one_open_idx
                ON w3_manager_source_history(manager_key) WHERE effective_to IS NULL;
            CREATE UNIQUE INDEX IF NOT EXISTS w3_msh_from_uniq_idx
                ON w3_manager_source_history(manager_key, effective_from);

            CREATE TABLE IF NOT EXISTS w3_schedule_exception(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                scope_key TEXT NOT NULL DEFAULT '',
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                exception_type TEXT NOT NULL,
                is_working INTEGER,
                day_start_min INTEGER, day_end_min INTEGER,
                night_start_min INTEGER, night_end_min INTEGER,
                priority INTEGER NOT NULL DEFAULT 100,
                revoked INTEGER NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created_by_user_id INTEGER,
                created_by_role TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                revoked_at TEXT NOT NULL DEFAULT '',
                CHECK (scope IN ('manager','source','global')),
                CHECK (exception_type IN ('off','on','holiday','custom_hours')),
                CHECK (date_to >= date_from),
                CHECK (length(date_from) = 10 AND length(date_to) = 10)
            );
            CREATE INDEX IF NOT EXISTS w3_exc_lookup_idx
                ON w3_schedule_exception(scope, scope_key, date_from, date_to) WHERE revoked = 0;

            CREATE TABLE IF NOT EXISTS w3_schedule_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity TEXT NOT NULL,
                entity_id INTEGER,
                scope_key TEXT NOT NULL DEFAULT '',
                dimension TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                old_json TEXT NOT NULL DEFAULT '',
                new_json TEXT NOT NULL DEFAULT '',
                effective_from TEXT NOT NULL DEFAULT '',
                effective_to TEXT,
                decision TEXT NOT NULL DEFAULT '',
                prev_source_key TEXT NOT NULL DEFAULT '',
                new_source_key TEXT NOT NULL DEFAULT '',
                prev_history_id INTEGER,
                new_history_id INTEGER,
                updated_by_user_id INTEGER,
                updated_by_role TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS w3_audit_entity_idx ON w3_schedule_audit(entity, entity_id, id);
        """)
        con.commit()
        for stmt in _W3_OVERLAP_TRIGGER_DDL:
            con.execute(stmt)
        con.commit()
    finally:
        con.close()


# --- W3.1-C: writer-helper foundation (reserved for future W3.6 routing) ---------------
# Tested only against synthetic databases in W3.1. Never routed from any existing
# writer. All idempotent (identical payload -> changed=False, no new version/audit row),
# one BEGIN IMMEDIATE transaction per call, no DELETE of version history ever, no network
# or Telegram I/O inside a transaction.

_W3_VERSION_CONFIG_COLUMNS = (
    "timezone", "enabled", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "day_start_min", "day_end_min", "night_start_min", "night_end_min",
    "stats_day_start_min", "stats_day_end_min",
    "light_include_dolyoty", "pro_include_dolyoty",
)


class _W3VersionUnchanged(Exception):
    """Internal control-flow signal: the requested config is byte-identical to the
    currently open version, so no version/audit row is written (idempotency contract)."""
    def __init__(self, payload):
        super().__init__("w3: version unchanged")
        self.payload = payload


def _w3_normalize_config(config):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    cfg = dict(config or {})
    out = {"timezone": str(cfg.get("timezone") or W3_TZ_NAME),
           "enabled": 1 if int(cfg.get("enabled", 1) or 0) == 1 else 0}
    for c in _W3_VERSION_CONFIG_COLUMNS:
        if c in ("timezone", "enabled"):
            continue
        v = cfg.get(c)
        out[c] = None if v is None else int(v)
    return out


def _w3_version_row_config(row):
    return {c: row.get(c) for c in _W3_VERSION_CONFIG_COLUMNS}


def _w3_version_upsert_body(con, table, key_col, key_val, dimension, config, *,
                             effective_from, actor_user_id=None, actor_role="", note="",
                             extra_columns=None):
    # type: (...) -> Dict[str, Any]
    """Close-then-insert body sharing the CALLER's already-open transaction (layer 3 of
    06 6.7's overlap defense). Raises _W3VersionUnchanged (not a return value) when the
    open version's config already matches, so the caller can decide whether to ROLLBACK
    a no-op or fold this into a larger transaction (e.g. w3_message_schedule_write's
    dual write) without a nested connection/transaction ever being opened."""
    if not _W3_DATE_RE.match(str(effective_from or "")):
        raise ValueError("invalid effective_from")
    old = con.execute(
        f"SELECT * FROM {table} WHERE {key_col}=? AND dimension=? AND effective_to IS NULL",
        (key_val, dimension),
    ).fetchone()
    old_dict = dict(old) if old is not None else None
    if old_dict is not None and _w3_version_row_config(old_dict) == {
        k: config.get(k) for k in _W3_VERSION_CONFIG_COLUMNS
    }:
        raise _W3VersionUnchanged({"reason": "unchanged", "id": old_dict["id"], "row": old_dict})
    now = _now_iso()
    cols = [key_col, "dimension", "effective_from", "effective_to", "note",
            "created_by_user_id", "created_by_role", "created_at",
            "close_reason", "superseded_by_id"] + list(_W3_VERSION_CONFIG_COLUMNS)
    vals = [key_val, dimension, str(effective_from), None, str(note or ""),
            actor_user_id, str(actor_role or ""), now, "", None] + [
        config.get(c) for c in _W3_VERSION_CONFIG_COLUMNS
    ]
    if extra_columns:
        for k, v in extra_columns.items():
            cols.append(k)
            vals.append(v)
    placeholders = ",".join("?" for _ in cols)
    # Close the OLD open version BEFORE inserting the new one: inserting first would
    # leave two effective_to-IS-NULL rows for the same (key, dimension) simultaneously
    # visible, which the overlap trigger correctly rejects as an overlap (a half-open
    # interval closing exactly where the next one opens is NOT an overlap, but two
    # concurrently-open rows always are).
    if old_dict is not None:
        con.execute(
            f"UPDATE {table} SET effective_to=?, close_reason='superseded' WHERE id=?",
            (str(effective_from), old_dict["id"]),
        )
    cur = con.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", vals)
    new_id = cur.lastrowid
    if old_dict is not None:
        con.execute(
            f"UPDATE {table} SET superseded_by_id=? WHERE id=?",
            (new_id, old_dict["id"]),
        )
    new_row = dict(con.execute(f"SELECT * FROM {table} WHERE id=?", (new_id,)).fetchone())
    con.execute(
        "INSERT INTO w3_schedule_audit"
        "(entity, entity_id, scope_key, dimension, action, old_json, new_json,"
        " effective_from, effective_to, updated_by_user_id, updated_by_role, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "source_version" if table == "w3_source_schedule_version" else "manager_version",
            new_id, str(key_val), str(dimension),
            "supersede" if old_dict is not None else "create",
            _w3_json_dumps(old_dict) if old_dict else "", _w3_json_dumps(new_row),
            str(effective_from), None, actor_user_id, str(actor_role or ""), now,
        ),
    )
    return {"id": new_id, "row": new_row, "closed_id": (old_dict["id"] if old_dict else None)}


def _w3_version_upsert(table, key_col, key_val, dimension, config, *, effective_from,
                        actor_user_id=None, actor_role="", note="", extra_columns=None,
                        db_path=None):
    # type: (...) -> Tuple[bool, Dict[str, Any]]
    ensure_w3_schedule_versioning(db_path)
    con = _bsl_connect(db_path)
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            result = _w3_version_upsert_body(
                con, table, key_col, key_val, dimension, config,
                effective_from=effective_from, actor_user_id=actor_user_id,
                actor_role=actor_role, note=note, extra_columns=extra_columns,
            )
        except _W3VersionUnchanged as exc:
            con.execute("ROLLBACK")
            return False, exc.payload
        except _bsl_sqlite3.IntegrityError as exc:
            con.execute("ROLLBACK")
            if "w3: overlapping" in str(exc):
                return False, {"reason": "overlap", "error": str(exc)}
            raise
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
        return True, result
    finally:
        con.close()


def w3_source_version_set(source_key, dimension, config, *, effective_from,
                           actor_user_id=None, actor_role="", note="", db_path=None):
    # type: (...) -> Tuple[bool, Dict[str, Any]]
    """Close the current open (source_key, dimension) version and insert a new one, in
    one transaction, with an audit row. Idempotent. Never DELETEs."""
    sk = str(source_key or "").strip()
    if not sk:
        raise ValueError("source_key is required")
    if dimension not in ("work", "message", "stats"):
        raise ValueError("dimension must be one of work/message/stats")
    return _w3_version_upsert(
        "w3_source_schedule_version", "source_key", sk, dimension, _w3_normalize_config(config),
        effective_from=effective_from, actor_user_id=actor_user_id, actor_role=actor_role,
        note=note, db_path=db_path,
    )


def w3_manager_version_set(manager_key, dimension, config, *, effective_from,
                            source_link_version_id=None, actor_user_id=None,
                            actor_role="", note="", db_path=None):
    # type: (...) -> Tuple[bool, Dict[str, Any]]
    """Same contract as w3_source_version_set, keyed by manager_key, plus
    source_link_version_id -- binds the override to the source assignment it was
    authored under, which is what makes the source-reassignment closing contract
    (06 6.9) enforceable rather than merely documented."""
    mk = str(manager_key or "").strip()
    if not mk:
        raise ValueError("manager_key is required")
    if dimension not in ("work", "message", "stats"):
        raise ValueError("dimension must be one of work/message/stats")
    return _w3_version_upsert(
        "w3_manager_schedule_version", "manager_key", mk, dimension, _w3_normalize_config(config),
        effective_from=effective_from, actor_user_id=actor_user_id, actor_role=actor_role,
        note=note, extra_columns={"source_link_version_id": source_link_version_id},
        db_path=db_path,
    )


def w3_source_history_record(manager_key, source_key, *, effective_from, change_kind,
                              override_decision="none", prev_source_key=None,
                              decided_by_user_id=None, actor_user_id=None, actor_role="",
                              db_path=None):
    # type: (...) -> Tuple[bool, Dict[str, Any]]
    """Append-only source-assignment history writer (06 6.4/6.9). Closes the current
    open interval, opens a new one, and -- only when override_decision='close_default'
    -- closes any w3_manager_schedule_version rows bound (via source_link_version_id) to
    the interval being closed, per the source-reassignment closing contract. 'preserve'
    is the caller's job: open a new manager version bound to the NEW history id via
    w3_manager_version_set. Absence of a decision is never treated as preservation
    (override_decision defaults to 'none', which closes nothing)."""
    mk = str(manager_key or "").strip()
    if not mk:
        raise ValueError("manager_key is required")
    if not _W3_DATE_RE.match(str(effective_from or "")):
        return False, {"reason": "invalid_effective_from"}
    sk = str(source_key or "")
    ck = str(change_kind or "")
    if ck not in ("link", "relink", "unlink", "seed", "source_deleted", "manager_deleted",
                  "reserve_activation", "reserve_link", "key_replacement", "replacement_rollback"):
        raise ValueError("invalid change_kind")
    if override_decision not in ("close_default", "preserve_explicit", "none"):
        raise ValueError("invalid override_decision")
    ensure_w3_schedule_versioning(db_path)
    con = _bsl_connect(db_path)
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            old = con.execute(
                "SELECT * FROM w3_manager_source_history WHERE manager_key=? AND effective_to IS NULL",
                (mk,),
            ).fetchone()
            old_dict = dict(old) if old is not None else None
            if old_dict is not None and str(old_dict["source_key"] or "") == sk:
                con.execute("ROLLBACK")
                return False, {"reason": "unchanged", "id": old_dict["id"], "row": old_dict}
            now = _now_iso()
            prev_sk = str(prev_source_key if prev_source_key is not None
                           else (old_dict["source_key"] if old_dict else ""))
            # Close the OLD open interval BEFORE inserting the new one -- same ordering
            # fix as _w3_version_upsert_body: two simultaneously-open rows for the same
            # manager_key trip the overlap trigger even though a half-open interval
            # closing exactly where the next one opens is not a real overlap.
            if old_dict is not None:
                con.execute(
                    "UPDATE w3_manager_source_history SET effective_to=? WHERE id=?",
                    (str(effective_from), old_dict["id"]),
                )
            cur = con.execute(
                "INSERT INTO w3_manager_source_history"
                "(manager_key, source_key, effective_from, effective_to, change_kind,"
                " prev_source_key, override_decision, closed_override_count,"
                " decided_by_user_id, created_by_user_id, created_by_role, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (mk, sk, str(effective_from), None, ck, prev_sk, override_decision, 0,
                 decided_by_user_id, actor_user_id, str(actor_role or ""), now),
            )
            new_id = cur.lastrowid
            closed_count = 0
            if override_decision == "close_default" and old_dict is not None:
                open_versions = con.execute(
                    "SELECT id FROM w3_manager_schedule_version"
                    " WHERE manager_key=? AND effective_to IS NULL AND source_link_version_id=?",
                    (mk, old_dict["id"]),
                ).fetchall()
                for v in open_versions:
                    con.execute(
                        "UPDATE w3_manager_schedule_version"
                        " SET effective_to=?, close_reason='source_reassignment' WHERE id=?",
                        (str(effective_from), v["id"]),
                    )
                    closed_count += 1
                if closed_count:
                    con.execute(
                        "UPDATE w3_manager_source_history SET closed_override_count=? WHERE id=?",
                        (closed_count, new_id),
                    )
            new_row = dict(con.execute(
                "SELECT * FROM w3_manager_source_history WHERE id=?", (new_id,)
            ).fetchone())
            con.execute(
                "INSERT INTO w3_schedule_audit"
                "(entity, entity_id, scope_key, dimension, action, old_json, new_json,"
                " effective_from, effective_to, decision, prev_source_key, new_source_key,"
                " prev_history_id, new_history_id, updated_by_user_id, updated_by_role, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "source_history", new_id, mk, "",
                    "supersede" if old_dict is not None else "create",
                    _w3_json_dumps(old_dict) if old_dict else "", _w3_json_dumps(new_row),
                    str(effective_from), None, override_decision, prev_sk, sk,
                    (old_dict["id"] if old_dict else None), new_id,
                    actor_user_id, str(actor_role or ""), now,
                ),
            )
            con.execute("COMMIT")
            return True, {"id": new_id, "row": new_row, "closed_override_count": closed_count}
        except _bsl_sqlite3.IntegrityError as exc:
            con.execute("ROLLBACK")
            if "w3: overlapping" in str(exc):
                return False, {"reason": "overlap", "error": str(exc)}
            raise
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


def w3_exception_add(scope, scope_key, date_from, date_to, exception_type, *,
                      is_working=None, day_start_min=None, day_end_min=None,
                      night_start_min=None, night_end_min=None, priority=100,
                      note="", actor_user_id=None, actor_role="", db_path=None):
    # type: (...) -> Tuple[bool, Dict[str, Any]]
    """Insert one dated exception row. Never overlap-constrained by design (multiple
    exceptions may cover the same date; the resolver picks ORDER BY priority ASC, id
    DESC and records every considered candidate in provenance -- 06 6.5)."""
    if scope not in ("manager", "source", "global"):
        raise ValueError("invalid scope")
    if exception_type not in ("off", "on", "holiday", "custom_hours"):
        raise ValueError("invalid exception_type")
    df, dt_ = str(date_from or ""), str(date_to or "")
    if not (_W3_DATE_RE.match(df) and _W3_DATE_RE.match(dt_)) or dt_ < df:
        raise ValueError("invalid date range")
    ensure_w3_schedule_versioning(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "INSERT INTO w3_schedule_exception"
            "(scope, scope_key, date_from, date_to, exception_type, is_working,"
            " day_start_min, day_end_min, night_start_min, night_end_min, priority,"
            " revoked, note, created_by_user_id, created_by_role, created_at, revoked_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,'')",
            (scope, str(scope_key or ""), df, dt_, exception_type,
             (None if is_working is None else int(is_working)),
             day_start_min, day_end_min, night_start_min, night_end_min, int(priority or 100),
             str(note or ""), actor_user_id, str(actor_role or ""), now),
        )
        new_id = cur.lastrowid
        new_row = dict(con.execute("SELECT * FROM w3_schedule_exception WHERE id=?", (new_id,)).fetchone())
        con.execute(
            "INSERT INTO w3_schedule_audit"
            "(entity, entity_id, scope_key, dimension, action, old_json, new_json,"
            " effective_from, effective_to, updated_by_user_id, updated_by_role, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("exception", new_id, str(scope_key or ""), "", "create", "",
             _w3_json_dumps(new_row), df, dt_, actor_user_id, str(actor_role or ""), now),
        )
        con.commit()
        return True, {"id": new_id, "row": new_row}
    finally:
        con.close()


def w3_exception_revoke(exception_id, *, reason="", actor_user_id=None, actor_role="", db_path=None):
    # type: (...) -> Tuple[bool, Dict[str, Any]]
    """Soft-delete: sets revoked=1/revoked_at. DELETE is never used on this table
    either (06 6.1 principle 6 applies uniformly)."""
    eid = int(exception_id)
    ensure_w3_schedule_versioning(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        old = con.execute("SELECT * FROM w3_schedule_exception WHERE id=?", (eid,)).fetchone()
        if old is None:
            return False, {"reason": "not_found"}
        old_dict = dict(old)
        if int(old_dict["revoked"] or 0) == 1:
            return False, {"reason": "already_revoked", "row": old_dict}
        con.execute(
            "UPDATE w3_schedule_exception SET revoked=1, revoked_at=?, note=? WHERE id=?",
            (now, (str(reason) if reason else old_dict["note"]), eid),
        )
        new_row = dict(con.execute("SELECT * FROM w3_schedule_exception WHERE id=?", (eid,)).fetchone())
        con.execute(
            "INSERT INTO w3_schedule_audit"
            "(entity, entity_id, scope_key, dimension, action, old_json, new_json,"
            " effective_from, effective_to, updated_by_user_id, updated_by_role, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("exception", eid, str(old_dict["scope_key"] or ""), "", "revoke",
             _w3_json_dumps(old_dict), _w3_json_dumps(new_row),
             str(old_dict["date_from"] or ""), str(old_dict["date_to"] or ""),
             actor_user_id, str(actor_role or ""), now),
        )
        con.commit()
        return True, {"id": eid, "row": new_row}
    finally:
        con.close()


def _w3_ensure_manager_client_message_schedule(con):
    """Defensive CREATE TABLE IF NOT EXISTS mirroring main.py's DDL (19788/22505) --
    main.py stays the DDL owner; this only guards a brand-new DB where storage.py runs
    first, exactly as ensure_source_work_schedule already does for manager_source_links."""
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS manager_client_message_schedule(
            manager_key TEXT PRIMARY KEY,
            day_start   TEXT NOT NULL DEFAULT '08:00',
            day_end     TEXT NOT NULL DEFAULT '17:00',
            night_start TEXT NOT NULL DEFAULT '17:00',
            night_end   TEXT NOT NULL DEFAULT '08:00',
            updated_by_user_id INTEGER,
            updated_at  TEXT
        )
        """
    )


def ensure_w3_manager_client_message_schedule_table(db_path=None):
    # type: (Optional[str]) -> None
    """Self-connecting wrapper around _w3_ensure_manager_client_message_schedule, for
    callers (the resolver's own setup block, the batch resolver) that need the table to
    exist before a plain read -- main.py's async DDL path creates this table today, but
    a synthetic/fresh DB opened through storage.py first would not have it yet."""
    con = _bsl_connect(db_path)
    try:
        _w3_ensure_manager_client_message_schedule(con)
        con.commit()
    finally:
        con.close()


def w3_message_schedule_write(manager_key, day_start, day_end, night_start, night_end, *,
                               actor_user_id=None, actor_role="", effective_from=None,
                               db_path=None):
    # type: (str, str, str, str, str, ...) -> Tuple[bool, Dict[str, Any]]
    """The single controlled writer reserved for the per-manager client-message
    (greeting/away) override, for future W6/W7/W8 routing (06 6.10). W3.1 does not wire
    this into any panel_bot/manager_bot call site -- it exists and is proven here so a
    later wave routes through ONE writer instead of the raw-SQL duplication the resolver
    exists to dissolve (05 5.1). Dual-writes, in ONE transaction (single connection,
    single BEGIN IMMEDIATE -- no nested transaction is ever opened): the legacy
    manager_client_message_schedule row (byte-compatible with the shape
    _tp_gq_get_schedule reads) AND a w3_manager_schedule_version(dimension='message')
    entry. Idempotent against both sides independently."""
    mk = str(manager_key or "").strip()
    if not mk:
        raise ValueError("manager_key is required")
    times = {"day_start": str(day_start or ""), "day_end": str(day_end or ""),
             "night_start": str(night_start or ""), "night_end": str(night_end or "")}
    for label, v in times.items():
        if not _W3_HHMM_RE.match(v):
            raise ValueError(f"{label} must be HH:MM")
    eff = str(effective_from or w3_business_date())
    if not _W3_DATE_RE.match(eff):
        raise ValueError("invalid effective_from")

    def _hhmm_to_min(v):
        h, m = v.split(":")
        return int(h) * 60 + int(m)

    new_config = _w3_normalize_config({
        "day_start_min": _hhmm_to_min(times["day_start"]),
        "day_end_min": _hhmm_to_min(times["day_end"]),
        "night_start_min": _hhmm_to_min(times["night_start"]),
        "night_end_min": _hhmm_to_min(times["night_end"]),
    })
    ensure_w3_schedule_versioning(db_path)
    con = _bsl_connect(db_path)
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            _w3_ensure_manager_client_message_schedule(con)
            legacy_old = con.execute(
                "SELECT day_start, day_end, night_start, night_end"
                " FROM manager_client_message_schedule WHERE manager_key=?",
                (mk,),
            ).fetchone()
            legacy_unchanged = (
                legacy_old is not None
                and str(legacy_old["day_start"]) == times["day_start"]
                and str(legacy_old["day_end"]) == times["day_end"]
                and str(legacy_old["night_start"]) == times["night_start"]
                and str(legacy_old["night_end"]) == times["night_end"]
            )
            version_changed = True
            version_result = None
            try:
                version_result = _w3_version_upsert_body(
                    con, "w3_manager_schedule_version", "manager_key", mk, "message", new_config,
                    effective_from=eff, actor_user_id=actor_user_id, actor_role=actor_role,
                    note="w3_message_schedule_write",
                )
            except _W3VersionUnchanged:
                version_changed = False
            if legacy_unchanged and not version_changed:
                con.execute("ROLLBACK")
                return False, {"reason": "unchanged"}
            now = _now_iso()
            con.execute(
                "INSERT INTO manager_client_message_schedule"
                "(manager_key, day_start, day_end, night_start, night_end, updated_by_user_id, updated_at)"
                " VALUES(?,?,?,?,?,?,?)"
                " ON CONFLICT(manager_key) DO UPDATE SET"
                " day_start=excluded.day_start, day_end=excluded.day_end,"
                " night_start=excluded.night_start, night_end=excluded.night_end,"
                " updated_by_user_id=excluded.updated_by_user_id, updated_at=excluded.updated_at",
                (mk, times["day_start"], times["day_end"], times["night_start"], times["night_end"],
                 actor_user_id, now),
            )
            con.execute("COMMIT")
            return True, {"legacy_written": not legacy_unchanged, "version": version_result}
        except _bsl_sqlite3.IntegrityError as exc:
            con.execute("ROLLBACK")
            if "w3: overlapping" in str(exc):
                return False, {"reason": "overlap", "error": str(exc)}
            raise
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()


# --- W3.1-A: canonical resolver foundation (05_CANONICAL_RESOLVER_CONTRACT.md) --------

def _w3_weekday_idx(business_date):
    try:
        return datetime.strptime(business_date, "%Y-%m-%d").date().weekday()
    except Exception:
        return None


def _w3_min_to_hhmm(m):
    if m is None:
        return None
    m = int(m) % 1440
    return "{:02d}:{:02d}".format(m // 60, m % 60)


def _w3_hhmm_to_min(s):
    try:
        h, mm = str(s).split(":")
        return int(h) * 60 + int(mm)
    except Exception:
        return None


def _w3_active_exceptions(con, scope, scope_key, business_date):
    rows = con.execute(
        "SELECT * FROM w3_schedule_exception"
        " WHERE scope=? AND scope_key=? AND date_from<=? AND date_to>=? AND revoked=0"
        " ORDER BY priority ASC, id DESC",
        (scope, str(scope_key or ""), business_date, business_date),
    ).fetchall()
    return [dict(r) for r in rows]


def _w3_open_version(con, table, key_col, key_val, dimension, business_date):
    row = con.execute(
        f"SELECT * FROM {table} WHERE {key_col}=? AND dimension=? AND enabled=1"
        f" AND effective_from<=? AND (effective_to IS NULL OR ?<effective_to)"
        f" ORDER BY effective_from DESC LIMIT 1",
        (key_val, dimension, business_date, business_date),
    ).fetchone()
    return dict(row) if row is not None else None


def _w3_historical_source_key(con, manager_key, business_date):
    # type: (Any, str, str) -> Tuple[str, Optional[int]]
    """The source_key w3_manager_source_history says was in force on business_date, NOT
    the current manager_source_links row (06 6.8/I-44) -- falls back to the current
    link when no history row covers the date, which is unconditionally true while the
    table is empty (day-one inertness)."""
    row = con.execute(
        "SELECT id, source_key FROM w3_manager_source_history"
        " WHERE manager_key=? AND effective_from<=? AND (effective_to IS NULL OR ?<effective_to)"
        " ORDER BY effective_from DESC LIMIT 1",
        (manager_key, business_date, business_date),
    ).fetchone()
    if row is not None:
        return str(row[1] or ""), int(row[0])
    link = con.execute(
        "SELECT source_key FROM manager_source_links WHERE manager_key=?", (manager_key,),
    ).fetchone()
    return (str(link[0] or "") if link is not None else ""), None


def _w3_resolve_work(con, manager_key, source_key, business_date, provenance):
    # type: (...) -> Tuple[bool, str, Optional[int]]
    """Precedence per 05 5.6: manager exception > legacy explicit day (ON or OFF) >
    source exception > global exception > manager persistent version (source-bound) >
    source persistent version > legacy source weekly (enabled=1) > False. Inertness
    proof (all w3_* empty): reduces to tier2 -> tier7 -> False, bit-identical to
    _manager_effective_is_working_row."""
    def _exc_tier(scope, key, tier_name):
        for exc in _w3_active_exceptions(con, scope, key, business_date):
            entry = {"dimension": "work", "tier": tier_name, "table": "w3_schedule_exception",
                      "row_id": exc["id"], "effective_from": exc["date_from"],
                      "effective_to": exc["date_to"], "decided": False}
            provenance.append(entry)
            if exc["exception_type"] in ("off", "on", "holiday"):
                entry["decided"] = True
                iw = exc["is_working"]
                if iw is None:
                    iw = 0 if exc["exception_type"] in ("off", "holiday") else 1
                return int(iw) == 1
        return None

    hit = _exc_tier("manager", manager_key, "exception_manager")
    if hit is not None:
        return hit, "manager_exception", None

    row = con.execute(
        "SELECT is_working FROM manager_work_schedule_days WHERE manager_key=? AND work_date=?",
        (manager_key, business_date),
    ).fetchone()
    if row is not None:
        provenance.append({"dimension": "work", "tier": "legacy_manager_day",
                            "table": "manager_work_schedule_days", "row_id": None,
                            "effective_from": business_date, "effective_to": None, "decided": True})
        return int(row[0] or 0) == 1, "legacy_manager_day", None

    if source_key:
        hit = _exc_tier("source", source_key, "exception_source")
        if hit is not None:
            return hit, "source_exception", None

    hit = _exc_tier("global", "", "exception_global")
    if hit is not None:
        return hit, "global_exception", None

    mv = _w3_open_version(con, "w3_manager_schedule_version", "manager_key", manager_key, "work", business_date)
    if mv is not None:
        entry = {"dimension": "work", "tier": "manager_version", "table": "w3_manager_schedule_version",
                  "row_id": mv["id"], "effective_from": mv["effective_from"],
                  "effective_to": mv["effective_to"], "decided": False}
        provenance.append(entry)
        link_id = mv.get("source_link_version_id")
        matches = True
        if link_id is not None:
            _, hist_id = _w3_historical_source_key(con, manager_key, business_date)
            matches = hist_id is not None and int(hist_id) == int(link_id)
        if matches:
            entry["decided"] = True
            idx = _w3_weekday_idx(business_date)
            col = _SWS_WEEKDAY_COLUMNS[idx] if idx is not None else None
            return (bool(mv.get(col)) if col else False), "manager_version", mv["id"]

    if source_key:
        sv = _w3_open_version(con, "w3_source_schedule_version", "source_key", source_key, "work", business_date)
        if sv is not None:
            provenance.append({"dimension": "work", "tier": "source_version",
                                "table": "w3_source_schedule_version", "row_id": sv["id"],
                                "effective_from": sv["effective_from"], "effective_to": sv["effective_to"],
                                "decided": True})
            idx = _w3_weekday_idx(business_date)
            col = _SWS_WEEKDAY_COLUMNS[idx] if idx is not None else None
            return (bool(sv.get(col)) if col else False), "source_version", sv["id"]

        sched = con.execute(
            "SELECT mon,tue,wed,thu,fri,sat,sun,enabled FROM source_work_schedule WHERE source_key=?",
            (source_key,),
        ).fetchone()
        if sched is not None and int(sched[7] or 0) == 1:
            idx = _w3_weekday_idx(business_date)
            provenance.append({"dimension": "work", "tier": "legacy_source_weekly",
                                "table": "source_work_schedule", "row_id": None,
                                "effective_from": business_date, "effective_to": None,
                                "decided": idx is not None})
            if idx is not None:
                return int(sched[idx] or 0) == 1, "legacy_source_weekly", None

    provenance.append({"dimension": "work", "tier": "fail_closed", "table": "", "row_id": None,
                        "effective_from": business_date, "effective_to": None, "decided": True})
    return False, "none", None


def _w3_resolve_message(con, manager_key, source_key, business_date, provenance):
    # type: (...) -> Tuple[Dict[str, str], str, Optional[int], Optional[int], Optional[int]]
    """Precedence per 05 5.7: exception custom_hours (manager->source->global) >
    manager persistent version > legacy per-manager override row (presence IS the
    signal) > source persistent version > legacy source_message_schedule (via
    historical link) > hardcoded default. Inertness: reduces to tier3 -> tier5 -> tier6,
    bit-identical to _tp_gq_get_schedule. Returns (times, tier, exception_id,
    source_version_id, manager_version_id)."""
    def _exc_custom(scope, key):
        for exc in _w3_active_exceptions(con, scope, key, business_date):
            entry = {"dimension": "message", "tier": f"exception_{scope}", "table": "w3_schedule_exception",
                      "row_id": exc["id"], "effective_from": exc["date_from"],
                      "effective_to": exc["date_to"], "decided": False}
            provenance.append(entry)
            if exc["exception_type"] == "custom_hours":
                entry["decided"] = True
                return {
                    "day_start": _w3_min_to_hhmm(exc["day_start_min"]) or _W3_MESSAGE_DEFAULT[0],
                    "day_end": _w3_min_to_hhmm(exc["day_end_min"]) or _W3_MESSAGE_DEFAULT[1],
                    "night_start": _w3_min_to_hhmm(exc["night_start_min"]) or _W3_MESSAGE_DEFAULT[2],
                    "night_end": _w3_min_to_hhmm(exc["night_end_min"]) or _W3_MESSAGE_DEFAULT[3],
                }, exc["id"]
        return None, None

    for scope, key in (("manager", manager_key), ("source", source_key or ""), ("global", "")):
        if scope == "source" and not source_key:
            continue
        hit, exc_id = _exc_custom(scope, key)
        if hit is not None:
            return hit, f"exception_{scope}", exc_id, None, None

    mv = _w3_open_version(con, "w3_manager_schedule_version", "manager_key", manager_key, "message", business_date)
    if mv is not None:
        provenance.append({"dimension": "message", "tier": "manager_version",
                            "table": "w3_manager_schedule_version", "row_id": mv["id"],
                            "effective_from": mv["effective_from"], "effective_to": mv["effective_to"],
                            "decided": True})
        return {
            "day_start": _w3_min_to_hhmm(mv["day_start_min"]) or _W3_MESSAGE_DEFAULT[0],
            "day_end": _w3_min_to_hhmm(mv["day_end_min"]) or _W3_MESSAGE_DEFAULT[1],
            "night_start": _w3_min_to_hhmm(mv["night_start_min"]) or _W3_MESSAGE_DEFAULT[2],
            "night_end": _w3_min_to_hhmm(mv["night_end_min"]) or _W3_MESSAGE_DEFAULT[3],
        }, "manager_version", None, None, mv["id"]

    row = con.execute(
        "SELECT day_start, day_end, night_start, night_end"
        " FROM manager_client_message_schedule WHERE manager_key=?",
        (manager_key,),
    ).fetchone()
    if row is not None:
        provenance.append({"dimension": "message", "tier": "legacy_manager_override",
                            "table": "manager_client_message_schedule", "row_id": None,
                            "effective_from": business_date, "effective_to": None, "decided": True})
        return {
            "day_start": str(row[0] or _W3_MESSAGE_DEFAULT[0]), "day_end": str(row[1] or _W3_MESSAGE_DEFAULT[1]),
            "night_start": str(row[2] or _W3_MESSAGE_DEFAULT[2]), "night_end": str(row[3] or _W3_MESSAGE_DEFAULT[3]),
        }, "legacy_manager_override", None, None, None

    if source_key:
        sv = _w3_open_version(con, "w3_source_schedule_version", "source_key", source_key, "message", business_date)
        if sv is not None:
            provenance.append({"dimension": "message", "tier": "source_version",
                                "table": "w3_source_schedule_version", "row_id": sv["id"],
                                "effective_from": sv["effective_from"], "effective_to": sv["effective_to"],
                                "decided": True})
            return {
                "day_start": _w3_min_to_hhmm(sv["day_start_min"]) or _W3_MESSAGE_DEFAULT[0],
                "day_end": _w3_min_to_hhmm(sv["day_end_min"]) or _W3_MESSAGE_DEFAULT[1],
                "night_start": _w3_min_to_hhmm(sv["night_start_min"]) or _W3_MESSAGE_DEFAULT[2],
                "night_end": _w3_min_to_hhmm(sv["night_end_min"]) or _W3_MESSAGE_DEFAULT[3],
            }, "source_version", None, sv["id"], None

        src_row = con.execute(
            "SELECT day_start, day_end, night_start, night_end, enabled"
            " FROM source_message_schedule WHERE source_key=?",
            (source_key,),
        ).fetchone()
        if src_row is not None and int(src_row[4] or 0) == 1:
            provenance.append({"dimension": "message", "tier": "legacy_source_message",
                                "table": "source_message_schedule", "row_id": None,
                                "effective_from": business_date, "effective_to": None, "decided": True})
            return {
                "day_start": str(src_row[0] or _W3_MESSAGE_DEFAULT[0]),
                "day_end": str(src_row[1] or _W3_MESSAGE_DEFAULT[1]),
                "night_start": str(src_row[2] or _W3_MESSAGE_DEFAULT[2]),
                "night_end": str(src_row[3] or _W3_MESSAGE_DEFAULT[3]),
            }, "legacy_source_message", None, None, None

    provenance.append({"dimension": "message", "tier": "default", "table": "", "row_id": None,
                        "effective_from": business_date, "effective_to": None, "decided": True})
    return {
        "day_start": _W3_MESSAGE_DEFAULT[0], "day_end": _W3_MESSAGE_DEFAULT[1],
        "night_start": _W3_MESSAGE_DEFAULT[2], "night_end": _W3_MESSAGE_DEFAULT[3],
    }, "default", None, None, None


def _w3_resolve_stats(con, manager_key, source_key, business_date, provenance):
    # type: (...) -> Tuple[int, int, bool, int, int, str, Optional[int]]
    """Precedence per 05 5.8: manager persistent version > source persistent version >
    legacy source_work_windows (enabled=1 AND 0<=day_start<day_end<=1439, i.e.
    source_work_window_get's own malformed-row rejection) > default 480/1020,
    configured=False. Night is ALWAYS derived, never stored (05 5.8 / se_flight_window)."""
    mv = _w3_open_version(con, "w3_manager_schedule_version", "manager_key", manager_key, "stats", business_date)
    if mv is not None and mv.get("stats_day_start_min") is not None and mv.get("stats_day_end_min") is not None:
        provenance.append({"dimension": "stats", "tier": "manager_version",
                            "table": "w3_manager_schedule_version", "row_id": mv["id"],
                            "effective_from": mv["effective_from"], "effective_to": mv["effective_to"],
                            "decided": True})
        return (int(mv["stats_day_start_min"]), int(mv["stats_day_end_min"]), True,
                (1 if mv.get("light_include_dolyoty") else 0),
                (1 if mv.get("pro_include_dolyoty") in (None,) else (1 if mv.get("pro_include_dolyoty") else 0)),
                "manager_version", mv["id"])
    if source_key:
        sv = _w3_open_version(con, "w3_source_schedule_version", "source_key", source_key, "stats", business_date)
        if sv is not None and sv.get("stats_day_start_min") is not None and sv.get("stats_day_end_min") is not None:
            provenance.append({"dimension": "stats", "tier": "source_version",
                                "table": "w3_source_schedule_version", "row_id": sv["id"],
                                "effective_from": sv["effective_from"], "effective_to": sv["effective_to"],
                                "decided": True})
            return (int(sv["stats_day_start_min"]), int(sv["stats_day_end_min"]), True,
                    (1 if sv.get("light_include_dolyoty") else 0),
                    (1 if sv.get("pro_include_dolyoty") in (None,) else (1 if sv.get("pro_include_dolyoty") else 0)),
                    "source_version", sv["id"])
        legacy = con.execute(
            "SELECT day_start, day_end, enabled, light_include_dolyoty, pro_include_dolyoty"
            " FROM source_work_windows WHERE source_key=?",
            (source_key,),
        ).fetchone()
        if legacy is not None and int(legacy[2] or 0) == 1:
            ds, de = int(legacy[0]), int(legacy[1])
            if 0 <= ds < de <= 1439:
                provenance.append({"dimension": "stats", "tier": "legacy_source_window",
                                    "table": "source_work_windows", "row_id": None,
                                    "effective_from": business_date, "effective_to": None, "decided": True})
                return (ds, de, True, (1 if int(legacy[3] or 0) == 1 else 0),
                        (1 if int(legacy[4] or 0) == 1 else 0), "legacy_source_window", None)
    provenance.append({"dimension": "stats", "tier": "default", "table": "", "row_id": None,
                        "effective_from": business_date, "effective_to": None, "decided": True})
    return (SOURCE_WINDOW_DEFAULT_DAY_START, SOURCE_WINDOW_DEFAULT_DAY_END, False,
            SOURCE_WINDOW_DEFAULT_LIGHT_INCLUDE_DOLYOTY, SOURCE_WINDOW_DEFAULT_PRO_INCLUDE_DOLYOTY,
            "default", None)


def _w3_window_state(cur_min, day_start, day_end, night_start, night_end):
    if None in (cur_min, day_start, day_end, night_start, night_end):
        return "gap"

    def _in_window(m, start, end):
        if start == end:
            return False
        if start < end:
            return start <= m < end
        return m >= start or m < end

    in_day = _in_window(cur_min, day_start, day_end)
    in_night = _in_window(cur_min, night_start, night_end)
    if in_day and in_night:
        return "overlap"
    if in_day:
        return "day"
    if in_night:
        return "night"
    return "gap"


def _w3_stats_attribution(cur_min, day_start, day_end):
    if cur_min is None or day_start is None or day_end is None or day_start == day_end:
        return "outside"
    return "day" if day_start <= cur_min < day_end else "night"


def w3_resolve_schedule(manager_key, business_date=None, *, at_instant=None,
                         tz_name=W3_TZ_NAME, db_path=None, source_key=None, _con=None,
                         activation_mode="legacy_authoritative"):
    # type: (...) -> Dict[str, Any]
    """THE canonical schedule resolver foundation (05_CANONICAL_RESOLVER_CONTRACT.md).
    W3.1: fully inert -- with every w3_* table empty this reduces bit-identically to
    _manager_effective_is_working_row (work), _tp_gq_get_schedule (message), and
    source_work_window_get (stats). Nothing in main.py/panel_bot.py/manager_bot.py/
    partner_stat_bot.py calls this yet (no reader/writer is redirected in W3.1).
    activation_mode is accepted/echoed for forward compatibility with the W3.4+ cutover
    switch; only 'legacy_authoritative' behavior is meaningful while every w3_* table is
    empty, so W3.1 does not branch on it.

    Deviations from the literal 05_CANONICAL_RESOLVER_CONTRACT.md/resolver_contract.json
    key list, documented per the task's own "document any deviation" allowance:
      - both `reason_code` (frozen contract, single string) AND `reason_codes` (this
        task's literal ask, a list) are populated -- the list form is currently either
        [] or a single-element echo of reason_code;
      - both the three flat `legacy_flight_lookback_*` keys (frozen contract) AND a
        bundled `legacy_flight_lookback` dict (this task's literal ask) are populated.
    Every key from both sources is present on every path, including error paths.
    """
    tz_name = str(tz_name or W3_TZ_NAME)
    resolved_at_utc = _now_iso()
    mk = str(manager_key or "").strip()
    invalid_fields = []
    degraded = []

    tz = w3_tz(tz_name)  # raises W3TimezoneError -- never caught, per the frozen contract

    if at_instant is not None:
        _w3_require_aware(at_instant)  # raises W3NaiveDatetimeError -- never caught
        at_instant_local = at_instant.astimezone(tz)
    else:
        at_instant_local = datetime.now(tz)

    if business_date is None:
        bd = at_instant_local.strftime("%Y-%m-%d")
    elif hasattr(business_date, "strftime") and not isinstance(business_date, str):
        bd = business_date.strftime("%Y-%m-%d")
    else:
        bd = str(business_date)

    bad_date = not _W3_DATE_RE.match(bd)
    if bad_date:
        invalid_fields.append("business_date")
    if not mk:
        invalid_fields.append("manager_key")

    provenance = []
    reason_code = "no_manager_key" if not mk else ("bad_business_date" if bad_date else "ok")
    result = {
        "schema_version": W3_SCHEDULE_SCHEMA_VERSION,
        "manager_key": mk,
        "business_date": bd,
        "timezone": tz_name,
        "resolved_at_utc": resolved_at_utc,
        "at_instant": at_instant_local.isoformat(),
        "activation_mode": str(activation_mode or "legacy_authoritative"),
        "source_key": "",
        "source_link_version_id": None,
        "schedule_source": "none",
        "work_version_id": None, "message_version_id": None, "window_version_id": None,
        "exception_id": None, "exception_type": "",
        "provenance": provenance,
        "audit_ref": "",
        "is_working_day": False,
        "is_working_day_reason": reason_code if reason_code != "ok" else "",
        "non_working_classification": "",
        "working_intervals": [],
        "message": {
            "manager_key": mk, "source_key": None,
            "day_start": _W3_MESSAGE_DEFAULT[0], "day_end": _W3_MESSAGE_DEFAULT[1],
            "night_start": _W3_MESSAGE_DEFAULT[2], "night_end": _W3_MESSAGE_DEFAULT[3],
            "day_start_min": SOURCE_WINDOW_DEFAULT_DAY_START, "day_end_min": SOURCE_WINDOW_DEFAULT_DAY_END,
            "night_start_min": SOURCE_WINDOW_DEFAULT_DAY_END, "night_end_min": SOURCE_WINDOW_DEFAULT_DAY_START,
            "day_crosses_midnight": False, "night_crosses_midnight": True,
            "is_default": 1, "is_inherited": 0,
        },
        "message_window_at_instant": "gap",
        "message_window_label": "—",
        "night_key": "",
        "stats": {
            "configured": False,
            "day_start_min": SOURCE_WINDOW_DEFAULT_DAY_START, "day_end_min": SOURCE_WINDOW_DEFAULT_DAY_END,
            "day_start_local": "", "day_end_local": "",
            "night_start_min": SOURCE_WINDOW_DEFAULT_DAY_END, "night_end_min": SOURCE_WINDOW_DEFAULT_DAY_START,
            "night_start_local": "", "night_end_local": "",
            "night_anchor_date": "", "night_crosses_midnight": True,
            "light_include_dolyoty": SOURCE_WINDOW_DEFAULT_LIGHT_INCLUDE_DOLYOTY,
            "pro_include_dolyoty": SOURCE_WINDOW_DEFAULT_PRO_INCLUDE_DOLYOTY,
            "window_cfg": {"day_start_min": SOURCE_WINDOW_DEFAULT_DAY_START,
                            "day_end_min": SOURCE_WINDOW_DEFAULT_DAY_END},
        },
        "stats_attribution_at_instant": "outside",
        "legacy_flight_lookback_is_working": False,
        "legacy_flight_lookback_source": "legacy_default_past",
        "legacy_flight_lookback_differs": False,
        "legacy_flight_lookback": {"is_working": False, "source": "legacy_default_past", "differs": False},
        "parity_legacy_is_working": False,
        "parity_mismatch": False,
        "is_working_at_instant": False,
        "fallback_used": False,
        "invalid": bool(invalid_fields),
        "invalid_fields": invalid_fields,
        "degraded": degraded,
        "reason_code": reason_code,
        "reason_codes": [] if reason_code == "ok" else [reason_code],
    }

    if not mk or bad_date:
        return result

    owns_con = _con is None
    con = _con if _con is not None else _bsl_connect(db_path)
    try:
        if owns_con:
            ensure_manager_schedule_tables(db_path)
            ensure_source_work_schedule(db_path)
            ensure_source_work_windows(db_path)
            ensure_source_message_schedule(db_path)
            ensure_w3_manager_client_message_schedule_table(db_path)
            ensure_w3_schedule_versioning(db_path)

        if source_key is not None:
            resolved_source_key = str(source_key).strip()
            source_link_version_id = None
        else:
            resolved_source_key, source_link_version_id = _w3_historical_source_key(con, mk, bd)
        result["source_key"] = resolved_source_key or ""
        result["source_link_version_id"] = source_link_version_id

        work_prov = []
        is_working, work_tier, work_version_id = _w3_resolve_work(con, mk, resolved_source_key, bd, work_prov)
        provenance.extend(work_prov)
        result["is_working_day"] = is_working
        result["is_working_day_reason"] = work_tier
        result["work_version_id"] = work_version_id
        if not is_working and work_tier in ("manager_exception", "source_exception", "global_exception"):
            result["non_working_classification"] = "scheduled_off"
        elif not is_working and work_tier == "none":
            result["non_working_classification"] = "no_schedule"

        msg_prov = []
        msg, msg_tier, exc_id, source_version_id, manager_version_id = _w3_resolve_message(
            con, mk, resolved_source_key, bd, msg_prov,
        )
        provenance.extend(msg_prov)
        day_start_min = _w3_hhmm_to_min(msg["day_start"])
        day_end_min = _w3_hhmm_to_min(msg["day_end"])
        night_start_min = _w3_hhmm_to_min(msg["night_start"])
        night_end_min = _w3_hhmm_to_min(msg["night_end"])
        is_default = 1 if msg_tier == "default" else 0
        is_inherited = 1 if msg_tier == "legacy_source_message" else 0
        result["message"] = {
            "manager_key": mk,
            "source_key": (resolved_source_key if (is_inherited and resolved_source_key) else None),
            "day_start": msg["day_start"], "day_end": msg["day_end"],
            "night_start": msg["night_start"], "night_end": msg["night_end"],
            "day_start_min": day_start_min, "day_end_min": day_end_min,
            "night_start_min": night_start_min, "night_end_min": night_end_min,
            "day_crosses_midnight": bool(day_start_min is not None and day_end_min is not None
                                          and day_end_min <= day_start_min),
            "night_crosses_midnight": bool(night_start_min is not None and night_end_min is not None
                                            and night_end_min <= night_start_min),
            "is_default": is_default, "is_inherited": is_inherited,
        }
        result["message_version_id"] = manager_version_id or source_version_id
        if exc_id is not None:
            result["exception_id"] = exc_id
            result["exception_type"] = "custom_hours"

        stats_prov = []
        (sday_start, sday_end, configured, light_inc, pro_inc, stats_tier,
         stats_version_id) = _w3_resolve_stats(con, mk, resolved_source_key, bd, stats_prov)
        provenance.extend(stats_prov)
        result["window_version_id"] = stats_version_id
        night_anchor = (datetime.strptime(bd, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
        try:
            # W3.2 structural tiling (06_DST_BOUNDARY_IMPLEMENTATION.md / M32-6): night_start
            # is NEVER independently recomputed -- it is literally the previous business
            # date's resolved day-end instant, and night_end is literally today's resolved
            # day-start instant. Reusing the same w3_local_at(...) call/fold for both readings
            # of "the same clock edge" makes a DST-fold mismatch (a 1-hour tiling gap/overlap
            # across the autumn ambiguous hour) structurally impossible rather than merely
            # untested.
            day_start_dt = w3_local_at(bd, sday_start, which="start", tz_name=tz_name, degraded=degraded)
            day_end_dt = w3_local_at(bd, sday_end, which="end", tz_name=tz_name, degraded=degraded)
            prev_day_end_dt = w3_local_at(night_anchor, sday_end, which="end", tz_name=tz_name, degraded=degraded)
            night_start_dt = prev_day_end_dt
            night_end_dt = day_start_dt
            day_start_local = day_start_dt.isoformat()
            day_end_local = day_end_dt.isoformat()
            night_start_local = night_start_dt.isoformat()
            night_end_local = night_end_dt.isoformat()
        except Exception:
            day_start_local = day_end_local = night_start_local = night_end_local = ""
            degraded.append("stats_local_times")
        if degraded:
            deduped_degraded = []
            for _d in degraded:
                if _d not in deduped_degraded:
                    deduped_degraded.append(_d)
            degraded[:] = deduped_degraded
        result["stats"] = {
            "configured": configured,
            "day_start_min": sday_start, "day_end_min": sday_end,
            "day_start_local": day_start_local, "day_end_local": day_end_local,
            "night_start_min": sday_end, "night_end_min": sday_start,
            "night_start_local": night_start_local, "night_end_local": night_end_local,
            "night_anchor_date": night_anchor, "night_crosses_midnight": True,
            "light_include_dolyoty": light_inc, "pro_include_dolyoty": pro_inc,
            "window_cfg": {"day_start_min": sday_start, "day_end_min": sday_end},
        }

        cur_min = at_instant_local.hour * 60 + at_instant_local.minute
        result["message_window_at_instant"] = _w3_window_state(
            cur_min, day_start_min, day_end_min, night_start_min, night_end_min,
        )
        result["message_window_label"] = {
            "day": "Дневное", "night": "Ночное", "overlap": "Пересечение", "gap": "—",
        }[result["message_window_at_instant"]]
        if result["message_window_at_instant"] == "night":
            result["night_key"] = f"{mk}:night:{bd}:{msg['night_start']}-{msg['night_end']}"
        result["stats_attribution_at_instant"] = _w3_stats_attribution(cur_min, sday_start, sday_end)

        # legacy_flight_lookback_* -- the SEPARATE main.py:5528 _manager_day_is_working
        # question (05 5.5), deliberately NOT merged into is_working_day. manager_work_days
        # lives in the same central DB, so a read-only SELECT on this same sync
        # connection is safe; if the table does not exist (fresh/synthetic DB) this stays
        # at its documented safe default.
        try:
            mwd_row = con.execute(
                "SELECT 1 FROM manager_work_days WHERE manager_key=? AND work_date=?", (mk, bd),
            ).fetchone()
            legacy_flight_is_working = mwd_row is not None
            legacy_flight_source = "manager_work_days" if mwd_row is not None else "legacy_default_past"
        except Exception:
            legacy_flight_is_working = False
            legacy_flight_source = "legacy_default_past"
        result["legacy_flight_lookback_is_working"] = legacy_flight_is_working
        result["legacy_flight_lookback_source"] = legacy_flight_source
        result["legacy_flight_lookback_differs"] = (legacy_flight_is_working != is_working)
        result["legacy_flight_lookback"] = {
            "is_working": legacy_flight_is_working, "source": legacy_flight_source,
            "differs": result["legacy_flight_lookback_differs"],
        }

        result["parity_legacy_is_working"] = _manager_effective_is_working_row(con, mk, bd)
        result["parity_mismatch"] = (result["parity_legacy_is_working"] != is_working)

        result["is_working_at_instant"] = bool(is_working and result["message_window_at_instant"] == "day")
        result["schedule_source"] = work_tier if work_tier != "none" else msg_tier
        result["audit_ref"] = (
            f"w3:mk={mk}:bd={bd}:wv={work_version_id}:mv={manager_version_id}:"
            f"ww={stats_version_id}:ex={exc_id}"
        )
        return result
    except (W3TimezoneError, W3NaiveDatetimeError):
        raise
    except Exception:
        result["fallback_used"] = True
        result["degraded"].append("resolver_exception")
        result["reason_code"] = "db_unavailable"
        result["reason_codes"] = ["db_unavailable"]
        return result
    finally:
        if owns_con:
            con.close()


def w3_resolve_schedule_batch(manager_keys, business_date=None, **kw):
    # type: (List[str], ...) -> Dict[str, Dict[str, Any]]
    """One shared connection for N managers -- for _schedule_summary_loop
    (main.py:27239) and _bizlink_autocreate_loop (main.py:27747), so a full manager scan
    does not open 16xN connections (05 5.3/5.10). Returns {manager_key: result},
    deduplicated, input order preserved."""
    db_path = kw.get("db_path")
    ensure_manager_schedule_tables(db_path)
    ensure_source_work_schedule(db_path)
    ensure_source_work_windows(db_path)
    ensure_source_message_schedule(db_path)
    ensure_w3_manager_client_message_schedule_table(db_path)
    ensure_w3_schedule_versioning(db_path)
    con = _bsl_connect(db_path)
    out = {}
    try:
        seen = set()
        for mk_raw in (manager_keys or []):
            mk = str(mk_raw or "").strip()
            if not mk or mk in seen:
                continue
            seen.add(mk)
            call_kw = dict(kw)
            call_kw["_con"] = con
            out[mk] = w3_resolve_schedule(mk, business_date, **call_kw)
        return out
    finally:
        con.close()


# --- TPILOT WAVE W3.3-A C1 WORK-DAY READERS 20260803 START ---
# Clock-free C1 (work-day dimension) entry point, reusing the reviewed W3.1 resolver
# tier functions (_w3_historical_source_key + _w3_resolve_work) without the message
# dimension, the stats dimension, or any call to w3_tz()/w3_local_at() -- the work-day
# decision consumes only a caller-supplied business_date string and needs neither zone
# nor instant (05_CANONICAL_RESOLVER_CONTRACT.md 5.4/5.6). Equivalence to the full
# resolver's is_working_day is asserted by tools/w3_3_c1_reader_parity_selftest.py over
# the whole scenario matrix, so this can never silently drift into a second
# implementation. See the W3.3 plan freeze S9 for the exact degradation and
# ensure-memo contracts these three functions implement.

_W3_C1_ENSURE_TABLES = (
    "source_work_windows", "source_work_windows_audit", "source_message_schedule",
    "manager_client_message_schedule", "w3_source_schedule_version",
    "w3_manager_schedule_version", "w3_manager_source_history",
    "w3_schedule_exception", "w3_schedule_audit",
)

# Module-level, keyed by the resolved db_path (S9.3): "the four C1-only ensures have
# run for this path in this process". Never trusted blindly -- see
# _w3_c1_ensure_validated. Holds only booleans; no connection, cursor or handle.
_W3_C1_ENSURE_MEMO = {}


def _w3_c1_ensure_validated(con, db_path):
    # type: (Any, Optional[str]) -> None
    """Presence-validated ensure-memo for the four C1-only table groups (S9.3 of the
    W3.3 plan freeze). A cached "ensured" entry for this resolved db_path is never
    honoured blindly: every call re-checks, on the connection already open for this
    call (no extra connection), that all nine tables the four memoized ensures create
    are present. A mismatch -- or the validation query itself raising -- drops the
    memo entry and re-runs the four ensures, then re-validates once more. This makes a
    replaced/restored/truncated SQLite file at the same path self-heal on the very
    next C1 call instead of being served from stale in-process state. Raises on
    failure so the two C1 callers can degrade to their pre-W3 body; never partially
    trusts an uninitialized database."""
    key = _bsl_db_path(db_path)

    def _present_count():
        placeholders = ",".join("?" for _ in _W3_C1_ENSURE_TABLES)
        row = con.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ({})".format(placeholders),
            _W3_C1_ENSURE_TABLES,
        ).fetchone()
        return int(row[0] or 0) if row is not None else 0

    if _W3_C1_ENSURE_MEMO.get(key):
        try:
            if _present_count() == len(_W3_C1_ENSURE_TABLES):
                return
        except Exception:
            pass
        _W3_C1_ENSURE_MEMO.pop(key, None)

    ensure_source_work_windows(db_path)
    ensure_source_message_schedule(db_path)
    ensure_w3_manager_client_message_schedule_table(db_path)
    ensure_w3_schedule_versioning(db_path)

    if _present_count() == len(_W3_C1_ENSURE_TABLES):
        _W3_C1_ENSURE_MEMO[key] = True
    else:
        _W3_C1_ENSURE_MEMO.pop(key, None)
        raise RuntimeError("_w3_c1_ensure_validated: required table(s) missing after ensure")


def _w3_c1_is_working(con, manager_key, business_date):
    # type: (Any, str, str) -> Tuple[bool, str, list]
    """The C1 work-dimension decision for one (manager_key, business_date), on an
    already-open connection: resolve the historically-in-force source_key, then walk
    the same tier chain w3_resolve_schedule uses for its is_working_day field (manager
    exception > legacy explicit day (ON or OFF) > source exception > global exception
    > manager version > source version > legacy source weekly enabled=1 > False).
    Returns (is_working, tier, provenance)."""
    mk = str(manager_key or "").strip()
    bd = str(business_date or "")
    source_key, _link_version_id = _w3_historical_source_key(con, mk, bd)
    provenance = []
    is_working, tier, _version_id = _w3_resolve_work(con, mk, source_key, bd, provenance)
    return bool(is_working), tier, provenance


def _c1_legacy_list_working_on_date(work_date, db_path=None):
    # type: (str, Optional[str]) -> List[str]
    """The pre-W3.3 body of manager_schedule_list_working_on_date, relocated verbatim
    under the W3.3 plan freeze S9.2.1 normalized semantic-preservation contract: only
    this function's name, parameter-list rendering, docstring and the indentation of
    the relocated statements differ from the pre-edit active definition -- no
    statement reorder, no renamed local, no changed literal, no added/removed branch,
    no added except, no changed return shape. Proof: tools/w3_3_c1_reader_parity_selftest.py
    P1 (normalized AST equality), P2 (exact SQL-string multiset equality), P3
    (branch/return-shape inventory) and P4 (behavioral parity), all run against the
    pre-edit definition extracted from the external backup. This is also the sole
    whole-function degradation target for the new public body above and the single
    place the legacy R1 SQL/decision logic exists in this module."""
    wd = str(work_date or "").strip()
    if not wd:
        return []
    ensure_manager_schedule_tables(db_path)
    ensure_source_work_schedule(db_path)
    try:
        weekday_idx = datetime.strptime(wd, "%Y-%m-%d").date().weekday()
    except Exception:
        weekday_idx = None
    weekday_col = _SWS_WEEKDAY_COLUMNS[weekday_idx] if weekday_idx is not None else None
    con = _bsl_connect(db_path)
    try:
        explicit_rows = con.execute(
            "SELECT manager_key FROM manager_work_schedule_days "
            "WHERE work_date=? AND is_working=1",
            (wd,),
        ).fetchall()
        result = {str(r[0] or "") for r in explicit_rows if r[0]}
        if weekday_col:
            inherited_rows = con.execute(
                "SELECT msl.manager_key FROM manager_source_links msl "
                "JOIN source_work_schedule sws ON sws.source_key = msl.source_key AND sws.enabled = 1 "
                "WHERE COALESCE(msl.source_key,'') <> '' AND sws.{} = 1 "
                "AND NOT EXISTS (SELECT 1 FROM manager_work_schedule_days d "
                "WHERE d.manager_key = msl.manager_key AND d.work_date = ?)".format(weekday_col),
                (wd,),
            ).fetchall()
            for r in inherited_rows:
                mk = str(r[0] or "")
                if mk:
                    result.add(mk)
        return sorted(result)
    finally:
        con.close()
# --- TPILOT WAVE W3.3-A C1 WORK-DAY READERS 20260803 END ---


# --- W3.1-D: central duplicate-status eligibility gate (16_DUPLICATE_STATUS_OWNER_ADDENDUM.md) ---

_W3_DUP_PENDING_CONTACT_KINDS = ("duplicate", "returning", "old_baseline")


def w3_duplicate_status_eligibility(lead_row, *, requested_status=None):
    # type: (Dict[str, Any], Optional[str]) -> Dict[str, Any]
    """THE single authority on whether a lead may carry an ordinary workflow status.
    PURE function: no I/O, no writes, no logging side effects, no clock read,
    deterministic on identical input (test J14 in the offline selftest asserts
    byte-identical repeated calls). W3.1 only DEFINES this -- nothing calls it yet
    (write/render boundary is wave W3.5-A; statistics boundary is wave W3.5-B).

    Two-tier predicate, frozen 2026-07-29 (owner correction: NOT a flat five-signal OR):
      tier 1 -- AUTHORITATIVE, drives render/count/callbacks/card state:
        S-1 duplicate==1 (DUP_AUTHORITATIVE_FLAG)
        S-2 event_type=='duplicate_card' (DUP_AUTHORITATIVE_EVENT)
      tier 2 -- ONLY while duplicate_checked==0 (the D-33 async-detection lag window),
        ONLY blocks a status WRITE, NEVER classifies/counts/renders as duplicate:
        S-3 lead_countable==0 (DUP_PENDING_NOT_COUNTABLE)
        S-4 contact_kind in (duplicate,returning,old_baseline) (DUP_PENDING_CONTACT_KIND)
        S-5 'other_manager' in dedupe_reason (DUP_PENDING_DEDUPE_REASON)
      P5: once duplicate_checked==1, tier 2 is ignored ENTIRELY -- false-positive
      protection, since tier-2 signals are known to diverge from duplicate in both
      directions (the 106-row C-11 population).

    Countability -- SECOND owner correction, 2026-07-29, LOCKED (independent-review
    finding F-1): the authoritative tier (S-1 OR S-2) controls countability, not the
    raw S-1 flag alone. The original C-11 formula `countable = (duplicate!=1) AND
    <existing lead_countable logic>` remains the PERSISTED-ROW rule for daily_leads
    rows (where duplicate==1 IS is_duplicate==1, so the two formulas agree), but a
    'duplicate_card' event row (S-2) may exist with no daily_leads row at all
    (16_DUPLICATE_STATUS_OWNER_ADDENDUM.md S-2 rationale) -- for that case the general
    eligibility contract must ALSO deny countability. The corrected formula:
        countable_allowed = (not is_duplicate) AND <existing lead_countable logic>
    where is_duplicate == authoritative_duplicate == (S-1 OR S-2). is_duplicate is
    read FIRST, unconditionally -- the D-35 defect (lead_countable checked INSTEAD of
    the authoritative signal) must never recur here. Tier-2 (provisional) signals
    NEVER participate in this formula (P2): countable_allowed is governed only by
    is_duplicate and the raw lead_countable field, never by contact_kind/dedupe_reason.

    Additional explicit keys (additive, schema_version 2): authoritative_duplicate
    (alias of is_duplicate, named per the owner's locked correction),
    countable_allowed (alias of countable, now correctly denying S-2-only rows),
    ordinary_status_write_allowed (alias of allows_ordinary_status -- write is blocked
    by BOTH the authoritative tier and a tier-2 pending_block, per A1/A8),
    ordinary_status_render_allowed / ordinary_status_callback_allowed (blocked ONLY by
    the authoritative tier, per P3/P7 -- a tier-2 pending_block never touches
    render/callback, only the write), display_state (alias of forced_state),
    duplicate_history_required (alias of is_duplicate; the gate receives no history
    data, so 'where history exists' is necessarily a render-layer decision in W3.5-A).
    """
    row = lead_row or {}

    def _flag(v):
        try:
            return int(v or 0) == 1
        except Exception:
            return False

    duplicate = _flag(row.get("duplicate"))
    duplicate_checked = _flag(row.get("duplicate_checked"))
    event_type = str(row.get("event_type") or "")
    lead_countable_raw = row.get("lead_countable")
    contact_kind = str(row.get("contact_kind") or "")
    dedupe_reason = str(row.get("dedupe_reason") or "")

    is_duplicate = False
    authoritative_signal = ""
    if duplicate:
        is_duplicate = True
        authoritative_signal = "DUP_AUTHORITATIVE_FLAG"
    elif event_type == "duplicate_card":
        is_duplicate = True
        authoritative_signal = "DUP_AUTHORITATIVE_EVENT"

    # is_duplicate (tier 1: S-1 OR S-2) is consulted FIRST, unconditionally -- this is
    # the D-35 short-circuit fix (test J14) PLUS the F-1 independent-review correction
    # (2026-07-29, owner LOCKED): countability must deny an S-2-only 'duplicate_card'
    # row too, not just a raw duplicate==1 row. lead_countable is read too, but never
    # INSTEAD of is_duplicate, and tier-2 (provisional) signals never participate here.
    lead_countable_zero = (lead_countable_raw is not None and int(lead_countable_raw or 0) == 0)
    countable = (not is_duplicate) and (not lead_countable_zero)

    pending_signals = []
    pending_block = False
    if not is_duplicate and not duplicate_checked:
        if lead_countable_zero:
            pending_signals.append("DUP_PENDING_NOT_COUNTABLE")
        if contact_kind in _W3_DUP_PENDING_CONTACT_KINDS:
            pending_signals.append("DUP_PENDING_CONTACT_KIND")
        if "other_manager" in dedupe_reason:
            pending_signals.append("DUP_PENDING_DEDUPE_REASON")
        pending_block = bool(pending_signals)

    allows_ordinary_status = (not is_duplicate) and (not pending_block)
    forced_state = "duplicate" if is_duplicate else ""

    requested = None if requested_status is None else str(requested_status)
    stale_fields = [
        f for f in ("status", "manual_status_override", "quality_status", "quality_bucket")
        if row.get(f) not in (None, "", 0)
    ]
    stale_ordinary_status = is_duplicate and bool(stale_fields)
    violation = stale_ordinary_status

    if is_duplicate and requested is not None:
        violation_code = "DUP_ORDINARY_STATUS_WRITE_REJECTED"
    elif is_duplicate and stale_ordinary_status:
        violation_code = "DUP_STALE_ORDINARY_STATUS"
    elif pending_block and requested is not None:
        violation_code = "DUP_PENDING_STATUS_WRITE_BLOCKED"
    else:
        violation_code = ""

    if is_duplicate:
        reason_code = authoritative_signal
    elif pending_block:
        reason_code = "+".join(pending_signals)
    else:
        reason_code = "ok"

    actor_raw = row.get("tg_user_id") if row.get("tg_user_id") is not None else row.get("chat_id")
    actor_ref = _w2_pseudonymize("w3-dup", actor_raw) if actor_raw is not None else ""

    return {
        "schema_version": W3_DUP_SCHEMA_VERSION,
        "is_duplicate": is_duplicate,
        "authoritative_duplicate": is_duplicate,
        "authoritative_signal": authoritative_signal,
        "pending_block": pending_block,
        "pending_signals": pending_signals,
        "countable": countable,
        "countable_allowed": countable,
        "allows_ordinary_status": allows_ordinary_status,
        "ordinary_status_write_allowed": allows_ordinary_status,
        "ordinary_status_render_allowed": not is_duplicate,
        "ordinary_status_callback_allowed": not is_duplicate,
        "forced_state": forced_state,
        "display_state": forced_state,
        "duplicate_history_required": is_duplicate,
        "stale_ordinary_status": stale_ordinary_status,
        "stale_status_fields": stale_fields,
        "violation": violation,
        "violation_code": violation_code,
        "reason_code": reason_code,
        "actor_ref": actor_ref,
    }


def w3_countable(lead_row):
    # type: (Dict[str, Any]) -> bool
    """Shared countability contract, exposed standalone for future W3.5-B call sites
    that only need the boolean: countable_allowed = (not is_duplicate) AND
    <existing lead_countable logic>, is_duplicate (tier 1: S-1 OR S-2) checked first
    (F-1 independent-review correction, 2026-07-29, owner LOCKED). Unused by any
    current report in W3.1 (item E of the task spec: 'remains unused by current
    reports')."""
    return w3_duplicate_status_eligibility(lead_row)["countable"]


# --- TPILOT PREPARED ACCOUNTS (feature: "💾Подготовленные аккаунты") PHASE 2 BEGIN ---
# Minimal additive side table for the prepared-accounts feature
# (features/prepared_accounts/). This table is NEVER the lifecycle
# authority for a prepared account -- managers.status is, and every
# start/scheduler/supervisor/ManagerBot/statistics gate continues to read
# ONLY managers.status/is_enabled/manual_stopped (unchanged by this block).
# This table exists solely to hold feature metadata (auth_source, prepare/
# activation timestamps, verify outcome, an activation claim) and to define
# VISIBILITY in the "💾Подготовленные аккаунты" screen: a manager_key is a
# visible prepared account only when a row here AND a managers row with
# status='prepared' both exist for it (the repository layer in
# features/prepared_accounts/repository.py computes that join; this module
# never joins against `managers` itself).
#
# Sync helpers via _bsl_connect (same idiom as the neighboring
# tdata_import_* block above), so both the controller (main.py, async) and
# the panel process can call them directly without an event loop. Reuses
# _tdimport_norm_key for manager_key normalization instead of adding a
# third private copy of the same logic.
#
# DDL is additive-only: CREATE TABLE IF NOT EXISTS, no ALTER, no DROP, no
# destructive migration. The `managers` table schema is not touched by this
# block.

def ensure_prepared_accounts_table(db_path=None):
    # type: (Optional[str]) -> None
    """Idempotent, additive create of prepared_accounts. Safe to call at the
    top of every accessor -- mirrors ensure_tdata_import_tables /
    _proxy_leases_table_ready."""
    con = _bsl_connect(db_path)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS prepared_accounts(
                manager_key         TEXT PRIMARY KEY,
                auth_source         TEXT NOT NULL DEFAULT '',
                prepared_by_user_id INTEGER,
                prepared_at         TEXT NOT NULL DEFAULT '',
                activated_at        TEXT NOT NULL DEFAULT '',
                activating_at       TEXT NOT NULL DEFAULT '',
                last_verify_at      TEXT NOT NULL DEFAULT '',
                last_verify_ok      INTEGER NOT NULL DEFAULT 0,
                last_verify_error   TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL DEFAULT '',
                updated_at          TEXT NOT NULL DEFAULT ''
            );
            """
        )
        con.commit()
    finally:
        con.close()


def prepared_account_upsert(manager_key, *, auth_source="", prepared_by_user_id=None, db_path=None):
    # type: (str, ..., Optional[int], Optional[str]) -> Optional[Dict[str, Any]]
    """INSERT ... ON CONFLICT(manager_key) DO UPDATE.

    The FIRST upsert for a manager_key sets prepared_at/created_at. Every
    later call (e.g. the operator switches auth_source qr->phone before
    finishing authorization, or an offline Session/TData import commits its
    side-row) only updates auth_source/prepared_by_user_id/updated_at --
    prepared_at and created_at are NEVER overwritten by a repeat upsert. An
    empty auth_source on a repeat call intentionally leaves the existing
    value alone (an empty string is not a real choice of method); likewise
    a None prepared_by_user_id on a repeat call leaves the existing value
    alone. auth_source here is feature metadata ONLY -- it is never read to
    choose a Telegram API app/device profile (that authority is
    managers.auth_profile, set/read entirely outside this table)."""
    key = _tdimport_norm_key(manager_key)
    if not key:
        raise ValueError("manager_key is required")
    ensure_prepared_accounts_table(db_path)
    now = _now_iso()
    src = str(auth_source or "").strip().lower()
    con = _bsl_connect(db_path)
    try:
        con.execute(
            "INSERT INTO prepared_accounts"
            "(manager_key, auth_source, prepared_by_user_id, prepared_at, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(manager_key) DO UPDATE SET"
            "   auth_source=CASE WHEN excluded.auth_source<>'' THEN excluded.auth_source ELSE prepared_accounts.auth_source END,"
            "   prepared_by_user_id=CASE WHEN excluded.prepared_by_user_id IS NOT NULL THEN excluded.prepared_by_user_id ELSE prepared_accounts.prepared_by_user_id END,"
            "   updated_at=excluded.updated_at",
            (key, src, prepared_by_user_id, now, now, now),
        )
        con.commit()
        row = con.execute("SELECT * FROM prepared_accounts WHERE manager_key=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def prepared_account_get(manager_key, db_path=None):
    # type: (str, Optional[str]) -> Optional[Dict[str, Any]]
    key = _tdimport_norm_key(manager_key)
    if not key:
        return None
    ensure_prepared_accounts_table(db_path)
    con = _bsl_connect(db_path)
    try:
        row = con.execute("SELECT * FROM prepared_accounts WHERE manager_key=?", (key,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def prepared_account_list(db_path=None):
    # type: (Optional[str]) -> List[Dict[str, Any]]
    """Only prepared_accounts rows -- no join against `managers`. The
    repository layer (features/prepared_accounts/repository.py) is the one
    place that computes visibility by joining this list against live
    managers rows; this function's ordering (manager_key ASC) exists only
    to make its own output stable/deterministic for callers that don't
    re-sort."""
    ensure_prepared_accounts_table(db_path)
    con = _bsl_connect(db_path)
    try:
        rows = con.execute("SELECT * FROM prepared_accounts ORDER BY manager_key ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def prepared_account_delete(manager_key, db_path=None):
    # type: (str, Optional[str]) -> bool
    """Deletes only the prepared_accounts side-row for this manager_key.
    Never touches the managers row -- callers that also need to remove the
    manager itself (e.g. main.py's _manager_delete_full_core, wired in a
    later phase) call both primitives explicitly."""
    key = _tdimport_norm_key(manager_key)
    if not key:
        return False
    ensure_prepared_accounts_table(db_path)
    con = _bsl_connect(db_path)
    try:
        cur = con.execute("DELETE FROM prepared_accounts WHERE manager_key=?", (key,))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def prepared_account_mark_verify(manager_key, ok, error="", db_path=None):
    # type: (str, bool, str, Optional[str]) -> bool
    """Records a live-verification outcome (activation's authorization
    re-check, or a manual "🔄 Проверить аккаунт"). Always clears
    activating_at: a verify attempt -- success or failure -- means this
    account is no longer mid-activation-claim. On success the caller
    proceeds to prepared_account_mark_activated (which sets activating_at
    again, harmlessly, to the same empty value); on failure this is the
    only write, so clearing the claim here is what lets a NEW activation
    attempt be claimed later without waiting out the TTL."""
    key = _tdimport_norm_key(manager_key)
    if not key:
        return False
    ensure_prepared_accounts_table(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE prepared_accounts"
            " SET last_verify_at=?, last_verify_ok=?, last_verify_error=?, activating_at='', updated_at=?"
            " WHERE manager_key=?",
            (now, 1 if ok else 0, str(error or ""), now, key),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def prepared_account_mark_activated(manager_key, db_path=None):
    # type: (str, Optional[str]) -> bool
    key = _tdimport_norm_key(manager_key)
    if not key:
        return False
    ensure_prepared_accounts_table(db_path)
    now = _now_iso()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE prepared_accounts SET activated_at=?, activating_at='', updated_at=?"
            " WHERE manager_key=?",
            (now, now, key),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def prepared_account_claim_activating(manager_key, *, ttl_seconds=300, db_path=None):
    # type: (str, ..., int, Optional[str]) -> bool
    """Atomic claim against two admins activating the same prepared account
    at once. Single UPDATE ... WHERE -- no separate lock table: SQLite
    serializes writers on this one statement, so a second concurrent caller
    can only run its UPDATE after this one has committed, and by then
    activating_at already holds the freshly-claimed (non-stale) timestamp,
    so the second caller's WHERE clause matches zero rows and it returns
    False. Succeeds (returns True) only when activating_at is currently
    empty OR older than ttl_seconds -- a claim that is never released by a
    matching prepared_account_mark_verify/mark_activated call (e.g. the
    controller crashed mid-activation) self-heals once it goes stale,
    without needing a separate release/reset helper."""
    key = _tdimport_norm_key(manager_key)
    if not key:
        return False
    ensure_prepared_accounts_table(db_path)
    now = _now_iso()
    cutoff = (_utc_now() - timedelta(seconds=max(0, int(ttl_seconds)))).replace(microsecond=0).isoformat()
    con = _bsl_connect(db_path)
    try:
        cur = con.execute(
            "UPDATE prepared_accounts SET activating_at=?, updated_at=?"
            " WHERE manager_key=? AND (COALESCE(activating_at,'')='' OR activating_at<=?)",
            (now, now, key, cutoff),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()
# --- TPILOT PREPARED ACCOUNTS PHASE 2 END ---

# --- TPILOT W3.1 SCHEDULE RESOLVER FOUNDATION + DUPLICATE STATUS GATE 20260729 END ---
