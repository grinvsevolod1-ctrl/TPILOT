# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telethon import Button, TelegramClient, events

import storage

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env.TPilot"
load_dotenv(ENV_PATH, override=True)

API_ID = int(os.getenv("API_ID") or "0")
API_HASH = (os.getenv("API_HASH") or "").strip()
PARTNER_BOT_TOKEN = (os.getenv("PARTNER_BOT_TOKEN") or os.getenv("PARTNER_STAT_BOT_TOKEN") or "").strip()
PARTNER_SESSION_FILE = (os.getenv("PARTNER_BOT_SESSION_FILE") or "sessions/session_partner_stat_bot").strip()
TPILOT_DB_PATH = (os.getenv("DB_PATH") or str(BASE_DIR / "db" / "data_tpilot.db")).strip()
EXPORT_DIR = (os.getenv("EXPORT_DIR") or "exports").strip()
PARTNER_POLL_SEC = int((os.getenv("PARTNER_BOT_POLL_SEC") or "5").strip() or "5")

if not os.path.isabs(PARTNER_SESSION_FILE):
    PARTNER_SESSION_FILE = str(BASE_DIR / PARTNER_SESSION_FILE)
if not os.path.isabs(TPILOT_DB_PATH):
    TPILOT_DB_PATH = str((BASE_DIR / TPILOT_DB_PATH).resolve())
if not os.path.isabs(EXPORT_DIR):
    EXPORT_DIR = str((BASE_DIR / EXPORT_DIR).resolve())

TZ = storage.w3_tz()  # W3.3-B redirect: canonical storage timezone (was ZoneInfo("Europe/Kyiv"))

if API_ID <= 0 or not API_HASH:
    raise RuntimeError("API_ID/API_HASH are not set in .env.TPilot")
if not PARTNER_BOT_TOKEN:
    raise RuntimeError("PARTNER_BOT_TOKEN is not set in .env.TPilot")

client = TelegramClient(PARTNER_SESSION_FILE, API_ID, API_HASH)


def _now_iso() -> str:
    return datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat()


def _kyiv_now() -> datetime:
    return storage.w3_now()


def _connect() -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(TPILOT_DB_PATH))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(TPILOT_DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return con


def _norm_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", str(raw or "").strip().lower().replace("ё", "е"))


def _ensure_tables() -> None:
    con = _connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS partner_buyers(
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                source_key TEXT DEFAULT '',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                can_view_stats INTEGER NOT NULL DEFAULT 1,
                can_live_leads INTEGER NOT NULL DEFAULT 0,
                can_view_contacts INTEGER NOT NULL DEFAULT 0,
                can_excel INTEGER NOT NULL DEFAULT 0,
                stat_format TEXT NOT NULL DEFAULT 'pro',
                live_enabled_at TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                last_seen_at TEXT DEFAULT ''
            );
        """)
        try:
            cols = [str(r[1]) for r in con.execute("PRAGMA table_info(partner_buyers)").fetchall()]
            if "stat_format" not in cols:
                con.execute("ALTER TABLE partner_buyers ADD COLUMN stat_format TEXT NOT NULL DEFAULT 'pro'")
            # M2.9A: additive only. Default off. No PartnerBot UI in this patch.
            if "can_view_screenshots" not in cols:
                con.execute("ALTER TABLE partner_buyers ADD COLUMN can_view_screenshots INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass
        con.execute("""
            CREATE TABLE IF NOT EXISTS partner_lead_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL DEFAULT '',
                source_key TEXT NOT NULL DEFAULT '',
                manager_key TEXT NOT NULL DEFAULT '',
                manager_display_name TEXT DEFAULT '',
                manager_username TEXT DEFAULT '',
                lead_id INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL DEFAULT 0,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                first_seen_utc TEXT NOT NULL DEFAULT '',
                duplicate INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS partner_sent_events(
                user_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                sent_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, event_id)
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS partner_lead_events_source_idx ON partner_lead_events(source_key, id)")
        con.execute("CREATE INDEX IF NOT EXISTS partner_sent_events_user_idx ON partner_sent_events(user_id, event_id)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS partner_access_requests(
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                last_name TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                requested_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS partner_sent_leads(
                user_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL DEFAULT '',
                lead_id INTEGER NOT NULL DEFAULT 0,
                sent_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(user_id, manager_key, lead_id)
            );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS partner_buyers_source_idx ON partner_buyers(source_key, is_enabled)")
        con.commit()
    finally:
        con.close()


def _insert_panel_notification(title: str, body: str) -> None:
    try:
        con = _connect()
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS panel_notifications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    body TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'new',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                );
            """)
            now = _now_iso()
            con.execute("INSERT INTO panel_notifications(kind,title,body,status,created_at,updated_at) VALUES(?,?,?,?,?,?)", ("partner_request", title, body, "new", now, now))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _buyer(user_id: int) -> Dict[str, Any]:
    _ensure_tables()
    con = _connect()
    try:
        row = con.execute("SELECT * FROM partner_buyers WHERE user_id=?", (int(user_id),)).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _enabled_buyers() -> List[Dict[str, Any]]:
    _ensure_tables()
    con = _connect()
    try:
        rows = con.execute("SELECT * FROM partner_buyers WHERE is_enabled=1 AND COALESCE(source_key,'')<>'' ORDER BY user_id ASC").fetchall()
        return [dict(r) for r in rows or []]
    finally:
        con.close()


def _touch_buyer(user_id: int) -> None:
    try:
        con = _connect()
        try:
            con.execute("UPDATE partner_buyers SET last_seen_at=? WHERE user_id=?", (_now_iso(), int(user_id)))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _register_request(sender) -> None:
    _ensure_tables()
    uid = int(getattr(sender, "id", 0) or 0)
    username = str(getattr(sender, "username", "") or "")
    first = str(getattr(sender, "first_name", "") or "")
    last = str(getattr(sender, "last_name", "") or "")
    now = _now_iso()
    con = _connect()
    try:
        con.execute("""
            INSERT INTO partner_access_requests(user_id, username, first_name, last_name, status, requested_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                status=CASE WHEN partner_access_requests.status='approved' THEN 'approved' ELSE 'new' END,
                updated_at=excluded.updated_at
        """, (uid, username, first, last, "new", now, now))
        con.commit()
    finally:
        con.close()
    title = "🤝 Новая заявка от байера"
    body = "\n".join([title, "", f"Пользователь: @{username}" if username else f"Пользователь: {first} {last}".strip(), f"user_id: {uid}"]).rstrip()
    _insert_panel_notification(title, body)


def _source_links() -> Dict[str, str]:
    con = _connect()
    try:
        rows = con.execute("SELECT manager_key, source_key FROM manager_source_links WHERE COALESCE(source_key,'')<>''").fetchall()
        return {_norm_key(r["manager_key"]): _norm_key(r["source_key"]) for r in rows or []}
    except Exception:
        return {}
    finally:
        con.close()


def _source_name(source_key: str) -> str:
    con = _connect()
    try:
        row = con.execute("SELECT name FROM traffic_sources WHERE source_key=?", (_norm_key(source_key),)).fetchone()
        return str(row[0] or source_key) if row else source_key
    except Exception:
        return source_key
    finally:
        con.close()


def _tp_pstat_tombstone_eligible_for_period(
    t: Dict[str, Any], sk: str, period_start: str, period_end: str, con: sqlite3.Connection
) -> bool:
    # DELETED MANAGER STATS RETENTION 20260711 (period-filter correction): mirrors
    # main.py's _tp_mgrret_tombstone_eligible_for_period. Retention (60 days) is a
    # ceiling on visibility, not a reason to include a deleted manager in every
    # report -- the deleted manager surfaces only if the requested [period_start,
    # period_end] range actually intersects real data (or a working schedule day)
    # on or before their own deletion date. deleted_at is a HARD UPPER BOUND: a day
    # after deletion is never eligible even if schedule rows exist for it.
    deleted_at = str(t.get("deleted_at") or "")
    deleted_date = deleted_at[:10]
    p_start = str(period_start or "")[:10]
    p_end = str(period_end or "")[:10]
    if not deleted_date or not p_start or not p_end:
        return False
    if p_start > deleted_date:
        return False  # requested period is fully after the deletion date
    check_start = p_start
    check_end = min(p_end, deleted_date)
    if check_start > check_end:
        return False
    mk = _norm_key(t.get("manager_key") or "")
    if not mk:
        return False
    # 1) backup per-manager DB daily_leads, opened read-only.
    db_path = str(t.get("db_path") or "")
    if db_path and not os.path.isabs(db_path):
        db_path = str((BASE_DIR / db_path).resolve())
    if db_path and os.path.exists(db_path):
        try:
            bcon = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                cur = bcon.execute(
                    "SELECT 1 FROM daily_leads WHERE lead_date BETWEEN ? AND ? LIMIT 1",
                    (check_start, check_end),
                )
                if cur.fetchone():
                    return True
            finally:
                bcon.close()
        except Exception:
            pass
    # 2) central partner_lead_events snapshot for the same manager+source, dated
    # via first_seen_utc (this table has no separate lead_date column). Compare
    # source_key via _norm_key in Python, not a raw SQL '=': stored casing may
    # differ from the caller's already-normalized sk (same case-sensitivity
    # pitfall fixed earlier in _manager_rows_for_source's own fallback match).
    try:
        cand = con.execute(
            "SELECT source_key, first_seen_utc FROM partner_lead_events WHERE manager_key=?",
            (mk,),
        ).fetchall()
        for cand_row in cand or []:
            if _norm_key(cand_row[0] or "") != sk:
                continue
            ev_date = str(cand_row[1] or "")[:10]
            if ev_date and check_start <= ev_date <= check_end:
                return True
    except Exception:
        pass
    # 3) schedule/working fallback -- manager_work_schedule_days survives hard-delete.
    try:
        from storage import manager_schedule_is_working as _sched_is_working
        cur_date = datetime.strptime(check_start, "%Y-%m-%d").date()
        end_date = datetime.strptime(check_end, "%Y-%m-%d").date()
        days_checked = 0
        while cur_date <= end_date and days_checked < 366:
            if _sched_is_working(mk, cur_date.isoformat(), db_path=TPILOT_DB_PATH):
                return True
            cur_date += timedelta(days=1)
            days_checked += 1
    except Exception:
        pass
    return False


def _manager_rows_for_source(
    source_key: str, period_start: str = "", period_end: str = ""
) -> List[Dict[str, Any]]:
    # --- TPILOT HISTSTATS M2.6H START ---
    # Includes archived managers: source link check is the privacy fence, not status.
    sk = _norm_key(source_key)
    links = _source_links()
    con = _connect()
    try:
        rows = con.execute("SELECT * FROM managers ORDER BY id ASC").fetchall()
        out = []
        seen_keys = set()
        for r in rows or []:
            d = dict(r)
            mk = _norm_key(d.get("manager_key") or "")
            if links.get(mk) == sk:
                out.append(d)
                seen_keys.add(mk)

        # DELETED MANAGER STATS RETENTION 20260711 (period-filter correction): a
        # tombstoned manager is unioned in ONLY when the caller passed a concrete
        # [period_start, period_end] AND _tp_pstat_tombstone_eligible_for_period
        # confirms real data (or a working schedule day) in that range up to their
        # deletion date. No period args -> fail closed, live-only (unchanged
        # behavior for every non-report/operational caller of this function).
        if period_start and period_end:
            try:
                tombstones = storage.manager_stats_tombstone_list_active(retention_days=60, db_path=TPILOT_DB_PATH)
            except Exception:
                tombstones = []
            for t in tombstones or []:
                mk = _norm_key(t.get("manager_key") or "")
                if not mk or mk in seen_keys:
                    continue
                t_source = _norm_key(t.get("source_key") or "")
                if t_source:
                    matched = t_source == sk
                else:
                    # Compare via _norm_key in Python, not a raw SQL '=': partner_lead_events
                    # may have stored source_key with different casing than the caller's
                    # normalized sk, and _norm_key lowercases + strips -- a naive SQL
                    # equality would silently miss real matches.
                    try:
                        cand = con.execute(
                            "SELECT DISTINCT source_key FROM partner_lead_events WHERE manager_key=? LIMIT 20", (mk,)
                        ).fetchall()
                        matched = any(_norm_key(c[0] or "") == sk for c in cand or [])
                    except Exception:
                        matched = False
                if not matched:
                    continue
                if not _tp_pstat_tombstone_eligible_for_period(t, sk, period_start, period_end, con):
                    continue
                out.append({
                    "manager_key": mk,
                    "display_name": str(t.get("display_name") or mk),
                    "telegram_username": str(t.get("telegram_username") or ""),
                    "db_path": str(t.get("db_path") or ""),
                    "status": "deleted",  # renders " (удалён)"/" (аккаунт удалён)" via
                                           # se_manager_label / other label builders
                    "is_enabled": 0,
                })
                seen_keys.add(mk)
        return out
    finally:
        con.close()
    # --- TPILOT HISTSTATS M2.6H END ---


def _manager_db_path(row: Dict[str, Any]) -> str:
    dbp = str((row or {}).get("db_path") or "").strip()
    if not dbp:
        key = _norm_key((row or {}).get("manager_key") or "")
        dbp = str(BASE_DIR / "runtime" / "managers" / key / f"{key}.db")
    if not os.path.isabs(dbp):
        dbp = str((BASE_DIR / dbp).resolve())
    return dbp




def _parse_date_token(token: str = "") -> Tuple[date, date, str]:
    t = str(token or "today").strip().lower()
    today = _kyiv_now().date()

    def _one(raw: str):
        s = str(raw or "").strip().lower()
        if s in ("today", "сегодня", ""):
            return today
        if s in ("yesterday", "вчера"):
            return today - timedelta(days=1)
        for fmt in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d", "%d-%m-%y", "%d-%m-%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except Exception:
                pass
        return None

    if t in ("today", "сегодня", ""):
        return today, today, today.strftime("%d.%m.%Y")
    if t in ("yesterday", "вчера"):
        d = today - timedelta(days=1)
        return d, d, d.strftime("%d.%m.%Y")
    if t in ("week", "7d", "неделя"):
        return today - timedelta(days=6), today, f"{(today - timedelta(days=6)).strftime('%d.%m.%Y')} - {today.strftime('%d.%m.%Y')}"
    if t in ("month", "30d", "31d", "месяц"):
        return today - timedelta(days=30), today, f"{(today - timedelta(days=30)).strftime('%d.%m.%Y')} - {today.strftime('%d.%m.%Y')}"

    parts = [p for p in re.split(r"[\s,;]+", t) if p]
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
        min_day = today - timedelta(days=30)
        if end > today:
            end = today
        if start < min_day:
            start = min_day
        if (end - start).days > 30:
            start = end - timedelta(days=30)
        return start, end, f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}"

    d = _one(t)
    if d:
        return d, d, d.strftime("%d.%m.%Y")
    return today, today, today.strftime("%d.%m.%Y")




def _date_range_list(start: date, end: date) -> List[str]:
    out = []
    d = start
    while d <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _list_leads_for_manager(row: Dict[str, Any], start: date, end: date) -> List[Dict[str, Any]]:
    dbp = _manager_db_path(row)
    if not dbp or not os.path.exists(dbp):
        return []
    dates = _date_range_list(start, end)
    q = ",".join(["?"] * len(dates))
    con = sqlite3.connect(dbp, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(f"SELECT * FROM daily_leads WHERE lead_date IN ({q}) ORDER BY first_seen_utc ASC, id ASC", tuple(dates)).fetchall()
        out = []
        for r in rows or []:
            d = dict(r)
            d["manager_key"] = _norm_key(row.get("manager_key") or d.get("manager_key") or "")
            d["manager_username"] = str(row.get("telegram_username") or d.get("manager_username") or "")
            # --- TPILOT HISTSTATS M2.6H START ---
            _base_name = str(row.get("display_name") or d.get("manager_key") or "")
            _status = str(row.get("status") or "").strip()
            _suffix = " (архив)" if _status == "archived" else ""
            d["manager_display_name"] = _base_name + _suffix
            # --- TPILOT HISTSTATS M2.6H END ---
            out.append(d)
        return out
    except Exception:
        return []
    finally:
        con.close()


def _collect_source_leads(source_key: str, start: date, end: date) -> List[Dict[str, Any]]:
    # --- TPILOT HISTSTATS M2.6H START ---
    # Includes archived managers; dedup by (chat_id, lead_date) — non-archived rows win.
    all_rows = _manager_rows_for_source(source_key)
    active_rows = [r for r in all_rows if str(r.get("status") or "") != "archived"]
    archived_rows = [r for r in all_rows if str(r.get("status") or "") == "archived"]
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for row in active_rows:
        for lead in _list_leads_for_manager(row, start, end):
            key = (str(lead.get("chat_id") or ""), str(lead.get("lead_date") or ""))
            seen.add(key)
            out.append(lead)
    for row in archived_rows:
        for lead in _list_leads_for_manager(row, start, end):
            key = (str(lead.get("chat_id") or ""), str(lead.get("lead_date") or ""))
            if key not in seen:
                seen.add(key)
                out.append(lead)
    out.sort(key=lambda x: (str(x.get("first_seen_utc") or ""), str(x.get("manager_key") or ""), int(x.get("id") or 0)))
    return out


def _manager_account_label(row: Dict[str, Any], fallback_key: str = "") -> str:
    # MANAGER_PROFILE_SYNC_20260711: was username-only, so a manager's display_name
    # (incl. a live Telegram rename now synced at startup by
    # storage.manager_sync_telegram_profile_in_db) never showed up in partner-facing
    # stats headers. Now: "Display | @username" when both exist and differ, "@username"
    # username-only, "Display" display_name-only, else the manager_key fallback.
    display = str((row or {}).get("display_name") or "").strip()
    username = str((row or {}).get("telegram_username") or (row or {}).get("manager_username") or "").strip().lstrip("@")
    key = _norm_key(fallback_key or (row or {}).get("manager_key") or "_") or "_"
    if display and username and display != username:
        return f"{display} | @{username}"
    if username:
        return "@" + username
    if display:
        return display
    return key


def _country_code(country: str, reason: str = "") -> str:
    raw = (str(country or "").strip() or str(reason or "").strip()).lower()
    mapping = {
        "казахстан": "KZ", "kz": "KZ", "қазақстан": "KZ",
        "азербайджан": "AZ", "azerbaijan": "AZ", "az": "AZ",
        "беларусь": "BY", "белоруссия": "BY", "by": "BY",
        "украина": "UA", "ukraine": "UA", "ua": "UA",
        "узбекистан": "UZ", "uz": "UZ",
        "таджикистан": "TJ", "tj": "TJ",
        "кыргызстан": "KG", "киргизия": "KG", "kg": "KG",
        "молдова": "MD", "md": "MD",
        "армения": "AM", "am": "AM",
        "грузия": "GE", "georgia": "GE", "ge": "GE",
        "турция": "TR", "turkey": "TR", "tr": "TR",
    }
    for key, code in mapping.items():
        if key in raw:
            return code
    cleaned = re.sub(r"[^A-Za-zА-Яа-я0-9]+", " ", str(country or reason or "")).strip()
    return cleaned[:20] if cleaned else "не указано"


def _format_stats_for_buyer(user_id: int, token: str = "today") -> str:
    buyer = _buyer(user_id)
    mode = str((buyer or {}).get("stat_format") or "pro").strip().lower()
    if mode in ("light", "lite"):
        return _format_stats_light_for_buyer(user_id, token)
    if mode in ("both", "light+pro", "all"):
        return _format_stats_light_for_buyer(user_id, token) + "\n\n" + _format_stats_pro_for_buyer(user_id, token)
    return _format_stats_pro_for_buyer(user_id, token)


def _lead_dt(row: Dict[str, Any]) -> datetime:
    raw = str(row.get("first_seen_utc") or "")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ)
    except Exception:
        return _kyiv_now()



def _event_sent_exists(user_id: int, event_id: int) -> bool:
    con = _connect()
    try:
        row = con.execute("SELECT 1 FROM partner_sent_events WHERE user_id=? AND event_id=?", (int(user_id), int(event_id))).fetchone()
        return bool(row)
    finally:
        con.close()


def _mark_event_sent(user_id: int, event_id: int) -> None:
    con = _connect()
    try:
        con.execute("INSERT OR IGNORE INTO partner_sent_events(user_id, event_id, sent_at) VALUES(?,?,?)", (int(user_id), int(event_id), _now_iso()))
        con.commit()
    finally:
        con.close()


def _mark_old_events_before_live(user_id: int, source_key: str, live_from: str) -> int:
    """Mark pre-enable live events as seen for this buyer without sending them.

    This preserves the existing live_enabled_at gate while preventing old events from
    starving newer unsent events when a source has a large backlog.
    """
    uid = int(user_id or 0)
    sk = _norm_key(source_key or "")
    cutoff = str(live_from or "").strip()
    if not uid or not sk or not cutoff:
        return 0
    _ensure_tables()
    con = _connect()
    try:
        cur = con.execute(
            """
            INSERT OR IGNORE INTO partner_sent_events(user_id, event_id, sent_at)
            SELECT ?, e.id, ?
            FROM partner_lead_events e
            WHERE e.source_key=?
              AND COALESCE(e.lead_countable, CASE WHEN COALESCE(e.duplicate,0)=1 THEN 0 ELSE 1 END)=1
              AND COALESCE(NULLIF(e.first_seen_utc,''), NULLIF(e.created_at,''), '') < ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM partner_sent_events s
                    WHERE s.user_id=?
                      AND s.event_id=e.id
              )
            """,
            (uid, _now_iso(), sk, cutoff, uid),
        )
        con.commit()
        return int(cur.rowcount or 0)
    finally:
        con.close()


def _pending_events_for_source(source_key: str, limit: int = 200, user_id: int = 0) -> List[Dict[str, Any]]:
    _ensure_tables()
    sk = _norm_key(source_key or "")
    uid = int(user_id or 0)
    con = _connect()
    try:
        if uid:
            rows = con.execute(
                """
                SELECT e.*
                FROM partner_lead_events e
                WHERE e.source_key=?
                  AND COALESCE(e.lead_countable, CASE WHEN COALESCE(e.duplicate,0)=1 THEN 0 ELSE 1 END)=1
                  AND NOT EXISTS (
                        SELECT 1
                        FROM partner_sent_events s
                        WHERE s.user_id=?
                          AND s.event_id=e.id
                  )
                ORDER BY e.id ASC
                LIMIT ?
                """,
                (sk, uid, int(limit)),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM partner_lead_events
                WHERE source_key=?
                  AND COALESCE(lead_countable, CASE WHEN COALESCE(duplicate,0)=1 THEN 0 ELSE 1 END)=1
                ORDER BY id ASC
                LIMIT ?
                """,
                (sk, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows or []]
    finally:
        con.close()


def _sent_exists(user_id: int, manager_key: str, lead_id: int) -> bool:
    con = _connect()
    try:
        row = con.execute("SELECT 1 FROM partner_sent_leads WHERE user_id=? AND manager_key=? AND lead_id=?", (int(user_id), _norm_key(manager_key), int(lead_id))).fetchone()
        return bool(row)
    finally:
        con.close()


def _mark_sent(user_id: int, manager_key: str, lead_id: int) -> None:
    con = _connect()
    try:
        con.execute("INSERT OR IGNORE INTO partner_sent_leads(user_id, manager_key, lead_id, sent_at) VALUES(?,?,?,?)", (int(user_id), _norm_key(manager_key), int(lead_id), _now_iso()))
        con.commit()
    finally:
        con.close()


def _parse_utc(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(raw or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)




async def _notify_live_leads_once() -> None:
    buyers = [b for b in _enabled_buyers() if int(b.get("can_live_leads") or 0) == 1]
    if not buyers:
        return
    for buyer in buyers:
        uid = int(buyer.get("user_id") or 0)
        sk = _norm_key(buyer.get("source_key") or "")
        if not uid or not sk:
            continue
        live_from = str(buyer.get("live_enabled_at") or "").strip()
        if not live_from:
            live_from = _now_iso()
            con = _connect()
            try:
                con.execute("UPDATE partner_buyers SET live_enabled_at=?, updated_at=? WHERE user_id=?", (live_from, live_from, uid))
                con.commit()
            finally:
                con.close()
        min_dt = _parse_utc(live_from)
        old_marked = _mark_old_events_before_live(uid, sk, live_from)
        if old_marked:
            print(f"partner live pre-enable events marked user_id={uid} source={sk} count={old_marked}")
        # R1A2: eligibility (no state row, or 'retry' row that's due) is now
        # fully decided by _pending_events_for_source's query -- an extra
        # _event_sent_exists() check here would be WRONG once 'retry' rows
        # exist (the row exists but the event genuinely is still eligible).
        for event_row in _pending_events_for_source(sk, limit=300, user_id=uid):
            event_id = int(event_row.get("id") or 0)
            if event_id <= 0:
                continue
            if _parse_utc(str(event_row.get("first_seen_utc") or event_row.get("created_at") or "")) < min_dt:
                _pse_mark_sent(uid, event_id)
                print(f"partner live pre-enable event skipped user_id={uid} source={sk} event_id={event_id}")
                continue
            if not _buyer_show_duplicates(buyer) and _psf3_is_buyer_duplicate(event_row):
                _pse_mark_sent(uid, event_id)
                continue
            try:
                _msg, fallback_used = await _pse_send_notification(uid, event_row, buyer)
            except Exception as e:
                # R1A2/F-31: classify + bounded retry/dead-letter instead of an
                # unconditional silent retry-forever -- a deterministic
                # formatting failure (already survived the plain-text
                # fallback inside _pse_send_notification) or a permanently
                # invalid recipient now reaches 'dead' instead of retrying
                # every ~5s forever, and one poison event can never block
                # the events that follow it in this same for-loop.
                _pse_record_failure(uid, event_id, e)
                continue
            # F-33: persistence happens OUTSIDE the send's own try/except --
            # the message is already delivered at this point, so a failure
            # here must never be classified as a send failure or trigger a
            # resend (_pse_mark_sent has its own bounded retry + degraded
            # logging, never raises).
            _pse_mark_sent(uid, event_id)
            print(f"partner live sent user_id={uid} source={sk} event_id={event_id} fallback={fallback_used}")
            await asyncio.sleep(0.2)
        # Full menu is not sent automatically after live leads.
        # Each live-lead card has a compact “Панель” button.





async def _live_loop() -> None:
    while True:
        try:
            await _notify_live_leads_once()
            await asyncio.sleep(max(3, int(PARTNER_POLL_SEC or 5)))
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"partner live loop error: {e!r}")
            await asyncio.sleep(10)



def _main_buttons(buyer: Dict[str, Any] | None = None):
    rows = [
        [Button.inline("📊 Сегодня", b"stats:today"), Button.inline("📅 Вчера", b"stats:yesterday")],
        [Button.inline("📈 Неделя", b"stats:week"), Button.inline("🗓 Месяц", b"stats:month")],
        [Button.inline("📆 Выбрать дату", b"help:date"), Button.inline("📆 Период", b"help:period")],
    ]
    if buyer and int(buyer.get("can_excel") or 0) == 1:
        rows.extend([
            [Button.inline("📦 Excel сегодня", b"excel:today"), Button.inline("📦 Excel вчера", b"excel:yesterday")],
            [Button.inline("📦 Excel неделя", b"excel:week"), Button.inline("📦 Excel месяц", b"excel:month")],
            [Button.inline("📦 Excel за дату", b"help:excel_date"), Button.inline("📦 Excel за период", b"help:excel_period")],
        ])
    rows.append([Button.inline("🔄 Обновить", b"stats:today")])
    return rows



def _live_lead_buttons():
    # Do not duplicate the full menu after every live lead.
    # The buyer can open it only when needed.
    return [[Button.inline("🤝 Панель", b"menu:main")]]


async def _send_menu(chat_id: int, user_id: int) -> None:
    buyer = _buyer(user_id)
    if not buyer or int(buyer.get("is_enabled") or 0) != 1:
        await client.send_message(chat_id, "Доступ пока не выдан. Заявка уже отправлена администратору. Ожидайте подтверждения.")
        return
    _touch_buyer(user_id)
    src = _source_name(str(buyer.get("source_key") or ""))
    text = "\n".join(["🤝 Партнёрская панель статистики", "", f"Ваш источник: {src}", "Выберите отчёт:"]).rstrip()
    await client.send_message(chat_id, text, buttons=_main_buttons(buyer))


def _export_xlsx_for_buyer(user_id: int, token: str) -> str:
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
        from openpyxl.utils import get_column_letter
    except Exception as e:
        raise RuntimeError("openpyxl не установлен") from e

    buyer = _buyer(user_id)
    if not buyer or int(buyer.get("can_excel") or 0) != 1:
        raise RuntimeError("Excel для вашего доступа выключен")

    sk = _norm_key(buyer.get("source_key") or "")
    if not sk:
        raise RuntimeError("Источник для вашего доступа ещё не назначен")

    start, end, label = _parse_date_token(token)
    leads = _collect_source_leads(sk, start, end)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", label).strip("_") or "period"
    out = os.path.join(EXPORT_DIR, f"partner_{sk}_{safe_label}.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "leads"

    headers = [
        "№", "Дата", "Время", "Источник", "manager_key", "Аккаунт менеджера",
        "chat_id", "username", "Имя аккаунта", "Телефон", "Дубликат",
        "Возраст", "Город", "Регион", "Страна", "Статус", "Причина неликвида",
        "Источник гео", "Точность", "Пометка", "Режим менеджера", "Вопрос отправлен",
        "Offline-сообщение", "UA текст", "Менеджер ответил",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")

    can_contacts = int(buyer.get("can_view_contacts") or 0) == 1
    source_display = _source_name(sk)

    for idx, r in enumerate(leads, 1):
        dt = _lead_dt(r)
        manager_username = str(r.get("manager_username") or "").strip()
        manager_key = str(r.get("manager_key") or "").strip()
        manager_account = "@" + manager_username.lstrip("@") if manager_username else (manager_key or "_")
        status_raw = str(r.get("status") or "").strip()
        status_disp = "ликвид" if status_raw == "liquid" else ("неликвид" if status_raw == "nonliquid" else (status_raw or "не определено"))
        username = str(r.get("username") or "").strip()
        ws.append([
            idx,
            dt.strftime("%d.%m.%Y"),
            dt.strftime("%H:%M:%S"),
            source_display,
            manager_key,
            manager_account,
            int(r.get("chat_id") or 0) if can_contacts else "_",
            (username if username.startswith("@") else ("@" + username if username else "_")) if can_contacts else "_",
            (str(r.get("full_name") or "").strip() or "_") if can_contacts else "_",
            (str(r.get("phone") or "").strip() or "_") if can_contacts else "_",
            "да" if int(r.get("duplicate") or 0) == 1 else "нет",
            str(r.get("age") if r.get("age") is not None else "_").strip() or "_",
            str(r.get("city") or "").strip() or "_",
            str(r.get("region") or "").strip() or "_",
            str(r.get("country") or "").strip() or "_",
            status_disp,
            str(r.get("nonliquid_reason") or "").strip() or "_",
            str(r.get("geo_source") or "").strip() or "_",
            str(r.get("geo_confidence") or "").strip() or "_",
            str(r.get("geo_note") or "").strip() or "_",
            str(r.get("manager_work_status") or "").strip() or "_",
            "да" if int(r.get("profile_question_sent") or 0) == 1 else "нет",
            "да" if int(r.get("offline_notice_sent") or 0) == 1 else "нет",
            "да" if int(r.get("ua_text_sent") or 0) == 1 else "нет",
            "да" if int(r.get("manager_replied") or 0) == 1 else "нет",
        ])

    widths = [6, 14, 13, 22, 16, 24, 18, 22, 28, 18, 12, 10, 22, 26, 18, 16, 24, 18, 14, 30, 18, 18, 18, 12, 18]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    wb.save(out)
    return out




# --- TPILOT PARTNER PERIOD WIZARD / DUPLICATES PANEL UPDATE 20260507 START ---
_TPILOT_PARTNER_PERIOD_STATE: Dict[int, str] = {}


def _partner_period_cancel_buttons():
    return [[Button.inline("🤝 Панель", b"menu:main"), Button.inline("✖️ Отмена", b"period:cancel")]]


async def _partner_start_period_wizard(event, kind: str) -> None:
    uid = int(getattr(event, "sender_id", 0) or 0)
    if kind not in ("data", "excel", "dup", "dup_excel"):
        await event.answer("Неизвестный период", alert=True)
        return
    _TPILOT_PARTNER_PERIOD_STATE[uid] = kind
    try:
        await event.edit(_partner_period_prompt(kind), buttons=_partner_period_cancel_buttons())
    except Exception:
        await client.send_message(int(event.chat_id), _partner_period_prompt(kind), buttons=_partner_period_cancel_buttons())
    try:
        await event.answer("Введите даты сообщением")
    except Exception:
        pass


def _partner_extract_two_dates(raw: str) -> str:
    s = str(raw or "").strip()
    # Accept only two date-like tokens. This keeps wizard safe and predictable.
    dates = re.findall(r"\b\d{1,2}[.\-]\d{1,2}[.\-](?:\d{2}|\d{4})\b", s)
    if len(dates) < 2:
        return ""
    return f"range {dates[0]} {dates[1]}"


def _partner_event_time_bounds(start: date, end: date) -> Tuple[str, str]:
    # partner_lead_events stores UTC-ish ISO strings. Date-level filtering is enough for panel reports.
    start_s = datetime(start.year, start.month, start.day).replace(microsecond=0).isoformat()
    next_day = end + timedelta(days=1)
    end_s = datetime(next_day.year, next_day.month, next_day.day).replace(microsecond=0).isoformat()
    return start_s, end_s


def _tp_partner_dup_is_identity_duplicate(row: Dict[str, Any]) -> bool:
    """M2.11B: identity-safe duplicate/repeat predicate for display labels.
    Mirrors the SQL predicate used to select duplicate groups below — must NOT
    rely on the legacy `duplicate` column as canonical truth (production has
    106 false-positive rows with duplicate=1, contact_kind='new', lead_countable=1
    that must never be displayed as duplicates)."""
    try:
        lead_countable = int(row.get("lead_countable") if row.get("lead_countable") is not None else 1)
    except Exception:
        lead_countable = 1
    kind = str(row.get("contact_kind") or "").strip().lower()
    return lead_countable == 0 and kind in ("duplicate", "returning", "old_baseline")


def _partner_duplicate_groups(user_id: int, token: str = "31d", *, max_groups: int = 20, max_items: int = 5) -> Tuple[List[Dict[str, Any]], str, bool, str]:
    buyer = _buyer(int(user_id))
    if not buyer or int(buyer.get("is_enabled") or 0) != 1:
        return [], "", False, "Доступ не выдан."
    sk = _norm_key(str(buyer.get("source_key") or ""))
    if not sk:
        return [], "", False, "Источник не назначен."
    start, end, label = _parse_date_token(token or "31d")
    start_s, end_s = _partner_event_time_bounds(start, end)
    can_contacts = int(buyer.get("can_view_contacts") or 0) == 1
    con = _connect()
    try:
        # M2.11B: identity-safe predicate (NOT the legacy `duplicate` column —
        # see _tp_partner_dup_is_identity_duplicate for why). Selects only the
        # M2.11A non-countable identity-mirror rows: duplicate / returning /
        # old_baseline contacts that do not count toward paid stats.
        dup_rows = con.execute(
            """
            SELECT * FROM partner_lead_events
            WHERE source_key=?
              AND COALESCE(lead_countable, CASE WHEN COALESCE(duplicate,0)=1 THEN 0 ELSE 1 END)=0
              AND lower(COALESCE(contact_kind,'')) IN ('duplicate','returning','old_baseline')
              AND COALESCE(first_seen_utc, created_at, '')>=?
              AND COALESCE(first_seen_utc, created_at, '')<?
            ORDER BY COALESCE(first_seen_utc, created_at, '') DESC, id DESC
            LIMIT ?
            """,
            (sk, start_s, end_s, int(max_groups)),
        ).fetchall()
        groups: List[Dict[str, Any]] = []
        seen_chat_ids: set[int] = set()
        for row in dup_rows or []:
            d = dict(row)
            chat_id = int(d.get("chat_id") or 0)
            if not chat_id or chat_id in seen_chat_ids:
                continue
            seen_chat_ids.add(chat_id)
            items = [dict(x) for x in con.execute(
                """
                SELECT * FROM partner_lead_events
                WHERE source_key=? AND chat_id=?
                ORDER BY COALESCE(first_seen_utc, created_at, '') ASC, id ASC
                LIMIT ?
                """,
                (sk, chat_id, int(max_items)),
            ).fetchall() or []]
            total = int(con.execute(
                "SELECT COUNT(*) FROM partner_lead_events WHERE source_key=? AND chat_id=?",
                (sk, chat_id),
            ).fetchone()[0] or 0)
            # M2.11B same-source previous-contact lookup — both queries are
            # scoped by source_key AND chat_id, so a buyer can never see a
            # previous contact that belongs to another source_key. If neither
            # row exists, the formatter must fall back to the generic
            # "Ранее уже писал в систему" line and must not resolve
            # first_seen_global_manager_key (that may point to a foreign source).
            cur_seen = str(d.get("first_seen_utc") or d.get("created_at") or "")
            first_prev_row = con.execute(
                """
                SELECT * FROM partner_lead_events
                WHERE source_key=? AND chat_id=? AND COALESCE(first_seen_utc, created_at, '') < ?
                ORDER BY COALESCE(first_seen_utc, created_at, '') ASC, id ASC
                LIMIT 1
                """,
                (sk, chat_id, cur_seen),
            ).fetchone()
            latest_prev_row = con.execute(
                """
                SELECT * FROM partner_lead_events
                WHERE source_key=? AND chat_id=? AND COALESCE(first_seen_utc, created_at, '') < ?
                ORDER BY COALESCE(first_seen_utc, created_at, '') DESC, id DESC
                LIMIT 1
                """,
                (sk, chat_id, cur_seen),
            ).fetchone()
            groups.append({
                "chat_id": chat_id,
                "items": items,
                "total": total,
                "current": d,
                "first_prev": dict(first_prev_row) if first_prev_row else None,
                "latest_prev": dict(latest_prev_row) if latest_prev_row else None,
            })
        return groups, label, can_contacts, ""
    finally:
        con.close()


# TPILOT IDENTITY RECONCILE 20260809: partner_lead_events.manager_display_name/
# manager_username are a HISTORICAL snapshot, frozen at event-insert time -- the only
# UPDATE that ever touches them (main.py's _create_partner_lead_event_from_daily)
# stops firing once a lead is marked sent, and there is no rename-propagation job.
# PartnerBot's own stats surfaces (_manager_account_label et al.) already read the
# LIVE `managers` table; live lead/duplicate cards did not, so a Telegram rename
# never showed up there even though the same rename was already visible in stats.
# This resolver fixes that WITHOUT ever mutating partner_lead_events: it is a
# read-time-only current-identity lookup (same never-raises pattern as the existing
# _rsvan_manager_display), TTL-cached so a burst of card renders doesn't do one
# SELECT per row, with the snapshot columns kept as the fallback for a manager_key
# that no longer has a `managers` row at all (deleted manager -- see tombstone
# handling elsewhere in this file for the analogous historical case).
_CURRENT_MGR_IDENTITY_CACHE: Dict[str, Tuple[float, bool, str, str]] = {}
_CURRENT_MGR_IDENTITY_TTL_SEC = 60.0


def _current_manager_identity(manager_key: str) -> Optional[Tuple[str, str]]:
    """Returns (display_name, telegram_username) for manager_key from the LIVE
    `managers` table, TTL-cached ~60s. Returns None when the manager row does not
    exist (or on any DB error) -- callers must fall back to their own historical/
    snapshot values in that case; None is deliberately distinct from a found-but-
    blank row, which is returned as ("", "")."""
    mk = _norm_key(manager_key)
    if not mk:
        return None
    now = time.monotonic()
    cached = _CURRENT_MGR_IDENTITY_CACHE.get(mk)
    if cached is not None and (now - cached[0]) < _CURRENT_MGR_IDENTITY_TTL_SEC:
        return (cached[2], cached[3]) if cached[1] else None
    try:
        con = sqlite3.connect(TPILOT_DB_PATH)
        try:
            row = con.execute(
                "SELECT display_name, telegram_username FROM managers WHERE manager_key=?", (mk,)
            ).fetchone()
        finally:
            con.close()
    except Exception as e:
        print(f"[identity-sync] current manager identity lookup failed key={mk}: {e!r}")
        return None
    if not row:
        _CURRENT_MGR_IDENTITY_CACHE[mk] = (now, False, "", "")
        return None
    name = str(row[0] or "")
    uname = str(row[1] or "")
    _CURRENT_MGR_IDENTITY_CACHE[mk] = (now, True, name, uname)
    return name, uname


def _partner_current_or_snapshot_identity(manager_key: str, snapshot_display: str, snapshot_username: str) -> Tuple[str, str]:
    """Resolve (display_name, telegram_username) preferring the LIVE managers row
    over the historical snapshot passed in; falls back to the snapshot only when the
    manager row itself does not exist. Never mutates partner_lead_events -- read-time
    resolution only."""
    current = _current_manager_identity(manager_key) if manager_key else None
    if current is not None:
        name, uname = current
        return name.strip(), uname.strip().lstrip("@")
    return str(snapshot_display or "").strip(), str(snapshot_username or "").strip().lstrip("@")


def _partner_manager_label_from_event(row: Dict[str, Any]) -> str:
    key = str(row.get("manager_key") or "").strip()
    name, uname = _partner_current_or_snapshot_identity(
        key, str(row.get("manager_display_name") or ""), str(row.get("manager_username") or "")
    )
    if name and uname:
        return f"{name} | @{uname}"
    if uname:
        return f"@{uname}"
    return name or key or "_"


def _partner_duplicate_item_lines(row: Dict[str, Any], idx: int, *, can_contacts: bool) -> List[str]:
    dt = _lead_dt(row).strftime("%d.%m.%y %H:%M")
    lines = [f"{idx}) {dt}", f"Аккаунт: {_partner_manager_label_from_event(row)}"]
    if can_contacts:
        lines.append(f"chat_id: {int(row.get('chat_id') or 0)}")
        username = str(row.get("username") or "").strip().lstrip("@")
        full_name = str(row.get("full_name") or "").strip()
        phone = str(row.get("phone") or "").strip()
        if username:
            lines.append(f"username: @{username}")
        if full_name:
            lines.append(f"Имя: {full_name}")
        if phone:
            lines.append(f"Телефон: {phone}")
    else:
        lines.append("Данные лида: скрыты")
    # M2.11B: identity-based status, NOT the legacy `duplicate` column.
    if _tp_partner_dup_is_identity_duplicate(row):
        lines.append("Статус: повторный контакт (не считается в оплату)")
    return lines


def _format_partner_duplicates_for_buyer(user_id: int, token: str = "31d") -> str:
    groups, label, can_contacts, err = _partner_duplicate_groups(int(user_id), token or "31d", max_groups=10, max_items=5)
    buyer = _buyer(int(user_id))
    src = _source_name(str((buyer or {}).get("source_key") or ""))
    if err:
        return f"⚠️ {err}"
    if not groups:
        return "\n".join([
            "🔁 Дубликаты",
            "",
            f"Источник: {src}",
            f"Период: {label}",
            "",
            "Пока нет дублей за выбранный период.",
        ]).rstrip()
    lines = ["🔁 Дубликаты", "", f"Источник: {src}", f"Период: {label}", ""]
    for n, g in enumerate(groups, 1):
        chat_id = int(g.get("chat_id") or 0)
        total = int(g.get("total") or 0)
        cur = dict(g.get("current") or {})
        # M2.11B: same-source previous-contact data only (both rows already
        # scoped by source_key AND chat_id inside _partner_duplicate_groups).
        first_prev = g.get("first_prev")
        latest_prev = g.get("latest_prev")
        cur_seen = _lead_dt(cur).strftime("%d.%m.%y %H:%M") if cur else "_"
        lines.append(f"Дубль #{n}")
        lines.append("🔁 Дубликат / повторный лид")
        if can_contacts:
            lines.append(f"chat_id: {chat_id}")
        else:
            lines.append("Контакт: скрыт")
        if first_prev or latest_prev:
            # Same-source previous contact found → show manager + timestamps.
            cur_label = _partner_manager_label_from_event(cur) if cur else "_"
            prev_src = dict(latest_prev or first_prev)
            prev_label = _partner_manager_label_from_event(prev_src)
            first_seen = _lead_dt(dict(first_prev)).strftime("%d.%m.%y %H:%M") if first_prev else "_"
            prev_seen = _lead_dt(dict(latest_prev)).strftime("%d.%m.%y %H:%M") if latest_prev else "_"
            lines.append(f"Текущий аккаунт: {cur_label}")
            lines.append(f"Дата сейчас: {cur_seen}")
            lines.append("")
            lines.append("Ранее писал:")
            lines.append(prev_label)
            lines.append(f"Первый контакт: {first_seen}")
            lines.append(f"Последний предыдущий контакт: {prev_seen}")
        else:
            # No same-source previous row → generic, privacy-safe message only.
            # Never resolve first_seen_global_manager_key (may be foreign source).
            lines.append("Ранее уже писал в систему")
            lines.append(f"Дата сейчас: {cur_seen}")
        lines.append("")
        lines.append("Статус: не считается в оплату")
        if total > 1:
            lines.append(f"Всего обращений по источнику: {total}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _export_partner_duplicates_xlsx(user_id: int, token: str = "31d") -> str:
    buyer = _buyer(int(user_id))
    if not buyer or int(buyer.get("is_enabled") or 0) != 1:
        raise RuntimeError("Доступ не выдан.")
    if int(buyer.get("can_excel") or 0) != 1:
        raise RuntimeError("Excel не разрешён для вашего доступа.")
    groups, label, can_contacts, err = _partner_duplicate_groups(int(user_id), token or "31d", max_groups=500, max_items=100)
    if err:
        raise RuntimeError(err)
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
        from openpyxl.utils import get_column_letter
    except Exception as e:
        raise RuntimeError("openpyxl не установлен") from e

    os.makedirs(EXPORT_DIR, exist_ok=True)
    safe_label = re.sub(r"[^0-9A-Za-zА-Яа-я_.-]+", "_", str(label or "period"))[:80]
    out = os.path.join(EXPORT_DIR, f"partner_duplicates_{int(user_id)}_{safe_label}.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "duplicates"
    headers = ["Блок", "№ обращения", "Время", "manager_key", "Аккаунт менеджера", "chat_id", "username", "Имя", "Телефон", "Дубликат", "Комментарий"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    block_no = 0
    for g in groups:
        block_no += 1
        chat_id = int(g.get("chat_id") or 0)
        items = list(g.get("items") or [])
        if not items:
            cur = dict(g.get("current") or {})
            items = [cur] if cur else []
        if not items:
            ws.append([block_no, "", "", "", "", chat_id if can_contacts else "_", "", "", "", "да", "контакт уже встречался ранее"])
            continue
        for idx, row in enumerate(items, 1):
            username = str(row.get("username") or "").strip().lstrip("@")
            ws.append([
                block_no,
                idx,
                _lead_dt(row).strftime("%d.%m.%Y %H:%M:%S"),
                str(row.get("manager_key") or ""),
                _partner_manager_label_from_event(row),
                int(row.get("chat_id") or 0) if can_contacts else "_",
                ("@" + username) if (can_contacts and username) else "_",
                str(row.get("full_name") or "").strip() if can_contacts else "_",
                str(row.get("phone") or "").strip() if can_contacts else "_",
                "да" if _tp_partner_dup_is_identity_duplicate(row) else "нет",
                "",
            ])
        if int(g.get("total") or 0) > len(items):
            ws.append([block_no, "", "", "", "", chat_id if can_contacts else "_", "", "", "", "да", f"Показано {len(items)} из {int(g.get('total') or 0)} обращений"])
    for i, width in enumerate([8, 14, 20, 18, 30, 18, 24, 30, 20, 12, 40], 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    wb.save(out)
    return out


_TPILOT_PARTNER_PREV_MAIN_BUTTONS = globals().get("_main_buttons")

def _main_buttons(buyer: Dict[str, Any] | None = None):
    rows = [
        [Button.inline("📊 Сегодня", b"stats:today"), Button.inline("📅 Вчера", b"stats:yesterday")],
        [Button.inline("📈 Неделя", b"stats:week"), Button.inline("🗓 Месяц", b"stats:month")],
        [Button.inline("📆 Выбрать дату", b"help:date"), Button.inline("📆 Период", b"help:period")],
        [Button.inline("🔁 Дубли 31 день", b"dup:31d"), Button.inline("📆 Дубли период", b"help:dup_period")],
    ]
    if buyer and int(buyer.get("can_excel") or 0) == 1:
        rows.extend([
            [Button.inline("📦 Excel сегодня", b"excel:today"), Button.inline("📦 Excel вчера", b"excel:yesterday")],
            [Button.inline("📦 Excel неделя", b"excel:week"), Button.inline("📦 Excel месяц", b"excel:month")],
            [Button.inline("📦 Excel за дату", b"help:excel_date"), Button.inline("📦 Excel за период", b"help:excel_period")],
            [Button.inline("📦 Excel дублей", b"dup_excel:31d"), Button.inline("📦 Excel дублей период", b"help:dup_excel_period")],
        ])
    rows.append([Button.inline("🔄 Обновить", b"stats:today")])
    return rows


async def _partner_handle_period_wizard(event, uid: int, text: str) -> bool:
    kind = str(_TPILOT_PARTNER_PERIOD_STATE.get(int(uid)) or "")
    if not kind:
        return False
    raw = str(text or "").strip()
    if not raw:
        return True
    if raw.startswith("/"):
        _TPILOT_PARTNER_PERIOD_STATE.pop(int(uid), None)
        return False
    token = _partner_extract_two_dates(raw)
    if not token:
        await client.send_message(int(event.chat_id), "⚠️ Введите две даты одним сообщением.\n\nПример: `01.05.26 07.05.26`", buttons=_partner_period_cancel_buttons())
        return True
    _TPILOT_PARTNER_PERIOD_STATE.pop(int(uid), None)
    buyer = _buyer(int(uid))
    try:
        if kind == "data":
            await client.send_message(int(event.chat_id), _format_stats_for_buyer(int(uid), token), buttons=_main_buttons(buyer))
            return True
        if kind == "excel":
            path = _export_xlsx_for_buyer(int(uid), token)
            await client.send_file(int(event.chat_id), path, caption="📦 Excel по вашему источнику", buttons=_main_buttons(buyer))
            return True
        if kind == "dup":
            await client.send_message(int(event.chat_id), _format_partner_duplicates_for_buyer(int(uid), token), buttons=_main_buttons(buyer))
            return True
        if kind == "dup_excel":
            path = _export_partner_duplicates_xlsx(int(uid), token)
            await client.send_file(int(event.chat_id), path, caption="📦 Excel дублей по вашему источнику", buttons=_main_buttons(buyer))
            return True
    except Exception as e:
        await client.send_message(int(event.chat_id), f"⚠️ {e}", buttons=_main_buttons(buyer))
        return True
    return True

# --- TPILOT PARTNER PERIOD WIZARD / DUPLICATES PANEL UPDATE 20260507 END ---

async def _handle_text(event) -> None:
    sender = await event.get_sender()
    uid = int(getattr(sender, "id", 0) or 0)
    text = str(event.raw_text or "").strip()
    if await _partner_handle_period_wizard(event, uid, text):
        return

    parts = text.split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/start":
        if not _buyer(uid):
            _register_request(sender)
            await client.send_message(event.chat_id, "Заявка на доступ отправлена администратору. Ожидайте подтверждения.")
            return
        await _send_menu(event.chat_id, uid)
        return

    if cmd == "/id":
        await client.send_message(event.chat_id, f"Ваш user_id: {uid}\nchat_id: {int(event.chat_id)}")
        return

    if cmd in ("/date", "/data", "/stat", "/stats"):
        token = arg or "today"
        await client.send_message(event.chat_id, _format_stats_for_buyer(uid, token), buttons=_main_buttons(_buyer(uid)), parse_mode='html')
        return

    if cmd in ("/excel", "/export"):
        token = arg or "today"
        try:
            path = _export_xlsx_for_buyer(uid, token)
            await client.send_file(int(event.chat_id), path, caption="📦 Excel по вашему источнику")
            await _send_menu(event.chat_id, uid)
        except Exception as e:
            await client.send_message(event.chat_id, _partner_xlsx_err_text(e), buttons=_main_buttons(_buyer(uid)))
        return

    if cmd == "/help":
        await client.send_message(
            event.chat_id,
            "Команды:\n/start - открыть меню\n/date 05.05.26 - отчёт за дату\n/date range 01.05.26 06.05.26 - отчёт за период\n/data range 01.05.26 06.05.26 - то же самое\n/excel 05.05.26 - Excel за дату, если разрешён\n/excel range 01.05.26 06.05.26 - Excel за период\n\nЧерез кнопки панели можно выбрать период и отправить только две даты: 01.05.26 07.05.26\n/id - показать ваш user_id",
        )
        return

    await _send_menu(event.chat_id, uid)



@client.on(events.NewMessage)
async def on_message(event):
    try:
        await _handle_text(event)
    except Exception as e:
        await client.send_message(event.chat_id, f"Ошибка: {e!r}")


@client.on(events.CallbackQuery)

async def on_callback(event):
    try:
        uid = int(event.sender_id or 0)
        data = bytes(event.data or b"").decode("utf-8", errors="ignore")
        buyer = _buyer(uid)
        if data in ("menu:main", "panel:main"):
            await event.answer("Панель")
            await _partner_edit_or_replace_menu(event, uid)
            return
        if data.startswith("stats:"):
            token = data.split(":", 1)[1]
            await event.edit(_format_stats_for_buyer(uid, token), buttons=_main_buttons(buyer), parse_mode='html')
            return
        if data.startswith("excel:"):
            token = data.split(":", 1)[1]
            try:
                path = _export_xlsx_for_buyer(uid, token)
                await client.send_file(int(event.chat_id), path, caption="📦 Excel по вашему источнику")
                await event.answer("Excel отправлен")
                await _partner_edit_or_replace_menu(event, uid)
            except Exception as e:
                await event.answer(str(e), alert=True)
            return
        if data == "period:cancel":
            _TPILOT_PARTNER_PERIOD_STATE.pop(int(uid), None)
            await event.answer("Отменено")
            await _partner_edit_or_replace_menu(event, uid)
            return
        if data == "help:dup_period":
            if not _buyer_show_duplicates(buyer):
                await event.answer("Показ дублей отключён администратором", alert=True)
                return
            await _partner_start_period_wizard(event, "dup")
            return
        if data == "help:dup_excel_period":
            if not _buyer_show_duplicates(buyer):
                await event.answer("Показ дублей отключён администратором", alert=True)
                return
            await _partner_start_period_wizard(event, "dup_excel")
            return
        if data.startswith("dup:"):
            if not _buyer_show_duplicates(buyer):
                await event.answer("Показ дублей отключён администратором", alert=True)
                return
            token = data.split(":", 1)[1] or "31d"
            await event.edit(_format_partner_duplicates_for_buyer(uid, token), buttons=_main_buttons(buyer))
            return
        if data.startswith("dup_excel:"):
            token = data.split(":", 1)[1] or "31d"
            try:
                path = _export_partner_duplicates_xlsx(uid, token)
                await client.send_file(int(event.chat_id), path, caption="📦 Excel дублей по вашему источнику")
                await event.answer("Excel дублей отправлен")
                await _partner_edit_or_replace_menu(event, uid)
            except Exception as e:
                await event.answer(str(e), alert=True)
            return
        if data == "help:date":
            await event.answer("Напишите: /date 05.05.26", alert=True)
            return
        if data == "help:period":
            await _partner_start_period_wizard(event, "data")
            return
        if data == "help:excel_date":
            await event.answer("Напишите: /excel 05.05.26", alert=True)
            return
        if data == "help:excel_period":
            await _partner_start_period_wizard(event, "excel")
            return
    except Exception as e:
        try:
            await event.answer(f"Ошибка: {e!r}", alert=True)
        except Exception:
            pass





# --- TPILOT PARTNER DUPLICATES CLEAN STATS UPDATE 20260507 START ---
_TPILOT_PARTNER_ORIG_LIST_LEADS = globals().get("_list_leads_for_manager")
_TPILOT_PARTNER_ORIG_FORMAT_STATS = globals().get("_format_stats_for_buyer")


def _list_leads_for_manager(row: Dict[str, Any], start: date, end: date) -> List[Dict[str, Any]]:
    leads = _TPILOT_PARTNER_ORIG_LIST_LEADS(row, start, end) if callable(_TPILOT_PARTNER_ORIG_LIST_LEADS) else []
    return [x for x in leads if int((x or {}).get("duplicate") or 0) != 1]


def _partner_strip_duplicate_lines(text: str) -> str:
    out = []
    for line in str(text or "").splitlines():
        s = line.strip()
        if s.startswith("Дубликаты:") or s.startswith("Дубли:"):
            continue
        if " | Дубли:" in line:
            line = re.sub(r"\s*\|\s*Дубли:\s*\d+", "", line)
        out.append(line)
    return "\n".join(out).rstrip()


def _format_stats_for_buyer(user_id: int, token: str = "today") -> str:
    if callable(_TPILOT_PARTNER_ORIG_FORMAT_STATS):
        return _partner_strip_duplicate_lines(_TPILOT_PARTNER_ORIG_FORMAT_STATS(user_id, token))
    return "Статистика недоступна."

# --- TPILOT PARTNER DUPLICATES CLEAN STATS UPDATE 20260507 END ---



# --- TPILOT PARTNER BOT EXCEL MENU / TODAY STATS UX UPDATE 20260507 START ---
_TPILOT_PARTNER_PREV_MAIN_BUTTONS_FOR_EXCEL_MENU = globals().get("_main_buttons")
_TPILOT_PARTNER_PREV_SEND_MENU_FOR_TODAY_STATS = globals().get("_send_menu")


def _main_buttons(buyer: Dict[str, Any] | None = None):
    rows = [
        [Button.inline("📊 Сегодня", b"stats:today"), Button.inline("📅 Вчера", b"stats:yesterday")],
        [Button.inline("📈 Неделя", b"stats:week"), Button.inline("🗓 Месяц", b"stats:month")],
        [Button.inline("📆 Период", b"help:period"), Button.inline("🔁 Дубликаты", b"partner_dup:31d")],
    ]
    if buyer and int((buyer or {}).get("can_excel") or 0) == 1:
        rows.append([Button.inline("📦 Excel", b"excel_menu:main")])
    rows.append([Button.inline("🔄 Обновить", b"menu:main")])
    return rows


async def _send_menu(chat_id: int, user_id: int) -> None:
    buyer = _buyer(int(user_id))
    if not buyer or int((buyer or {}).get("is_enabled") or 0) != 1:
        await client.send_message(chat_id, "Доступ пока не выдан. Заявка уже отправлена администратору. Ожидайте подтверждения.")
        return
    _touch_buyer(int(user_id))
    src = _source_name(str((buyer or {}).get("source_key") or ""))
    stats_text = _format_stats_for_buyer(int(user_id), "today")
    text = "\n".join([
        "🤝 Партнёрская панель статистики",
        "",
        f"Ваш источник: {src}",
        "",
        stats_text,
        "",
        "Выберите действие:",
    ]).rstrip()
    await client.send_message(int(chat_id), text, buttons=_main_buttons(buyer))


async def _partner_try_start_period_wizard(event, kind: str) -> None:
    starter = globals().get("_partner_start_period_wizard")
    if callable(starter):
        await starter(event, kind)
        return
    example = "`01.05.26 07.05.26`"
    titles = {
        "data": "📅 Статистика за период",
        "excel": "📦 Excel за период",
        "dup": "🔁 Дубликаты за период",
        "dup_excel": "📦 Excel дублей за период",
    }
    text = "\n".join([
        titles.get(str(kind or ""), "📅 Период"),
        "",
        "Введите даты одним сообщением:",
        "",
        example,
    ]).rstrip()
    try:
        await event.edit(text, buttons=[[Button.inline("🤝 Панель", b"menu:main")]])
    except Exception:
        await client.send_message(int(event.chat_id), text, buttons=[[Button.inline("🤝 Панель", b"menu:main")]])


@client.on(events.CallbackQuery)
async def _tpilot_partner_excel_menu_callback(event):
    try:
        uid = int(event.sender_id or 0)
        data = bytes(event.data or b"").decode("utf-8", errors="ignore")
        buyer = _buyer(uid)

        if data == "excel_menu:main":
            if not buyer or int((buyer or {}).get("can_excel") or 0) != 1:
                await event.answer("Excel для вашего доступа выключен", alert=True)
                return
            await event.edit(_partner_excel_menu_text(uid), buttons=_partner_excel_menu_buttons(buyer=buyer))
            return

        if data == "excel_menu:period":
            await _partner_try_start_period_wizard(event, "excel")
            return

        if data == "excel_menu:dup_period":
            if not _buyer_show_duplicates(buyer):
                await event.answer("Показ дублей отключён администратором", alert=True)
                return
            await _partner_try_start_period_wizard(event, "dup_excel")
            return

        if data == "partner_dup:31d":
            if not _buyer_show_duplicates(buyer):
                await event.answer("Показ дублей отключён администратором", alert=True)
                return
            formatter = globals().get("_format_partner_duplicates_for_buyer")
            if callable(formatter):
                await event.edit(formatter(uid, "31d"), buttons=_main_buttons(buyer))
            else:
                await event.answer("Раздел дублей ещё не установлен", alert=True)
            return

        if data == "partner_dup_excel:31d":
            if not _buyer_show_duplicates(buyer):
                await event.answer("Показ дублей отключён администратором", alert=True)
                return
            exporter = globals().get("_export_partner_duplicates_xlsx")
            if not callable(exporter):
                await event.answer("Excel дублей ещё не установлен", alert=True)
                return
            try:
                path = exporter(uid, "31d")
                await client.send_file(int(event.chat_id), path, caption="📦 Excel дублей по вашему источнику")
                await event.answer("Excel дублей отправлен")
                await _partner_edit_or_replace_menu(event, uid)
            except Exception as e:
                await event.answer(str(e), alert=True)
            return
    except Exception as e:
        try:
            await event.answer(f"Ошибка: {e!r}", alert=True)
        except Exception:
            pass
# --- TPILOT PARTNER BOT EXCEL MENU / TODAY STATS UX UPDATE 20260507 END ---


# --- TPILOT PARTNER PANEL EDIT-NOT-SEND HOTFIX 20260507 START ---
# Keeps Partner Bot panel UX clean: callback buttons edit the current panel instead
# of creating a new panel message. If Telegram refuses edit, the old panel is
# deleted and replaced by one fresh panel.
_TPILOT_PARTNER_PREV_SEND_MENU_FOR_EDIT_HOTFIX = globals().get("_send_menu")
_PARTNER_ACTIVE_PANEL_BY_CHAT: Dict[int, int] = {}


async def _partner_delete_active_panel(chat_id: int, *, except_message_id: int = 0) -> None:
    try:
        cid = int(chat_id)
        old_mid = int(_PARTNER_ACTIVE_PANEL_BY_CHAT.get(cid) or 0)
        if not old_mid or (except_message_id and old_mid == int(except_message_id)):
            return
        try:
            await client.delete_messages(cid, [old_mid])
        except Exception:
            pass
        finally:
            if int(_PARTNER_ACTIVE_PANEL_BY_CHAT.get(cid) or 0) == old_mid:
                _PARTNER_ACTIVE_PANEL_BY_CHAT.pop(cid, None)
    except Exception:
        pass
# --- TPILOT PARTNER PANEL EDIT-NOT-SEND HOTFIX 20260507 END ---


# --- TPILOT PARTNER TODAY TIME AND TITLE HOTFIX 20260507 START ---
# Final visible Partner panel title and report update time with seconds.
_PARTNER_PANEL_TITLE = "🤝 Партнёрская панель статистики"
_TPILOT_PARTNER_PREV_FORMAT_STATS_FOR_TIME_TITLE = globals().get("_format_stats_for_buyer")


def _partner_update_time_hms() -> str:
    try:
        return _kyiv_now().strftime("%H:%M:%S")
    except Exception:
        return datetime.now().strftime("%H:%M:%S")


def _partner_add_update_time_to_stats_title(text: str) -> str:
    raw = str(text or "")
    if not raw.strip():
        return raw
    hms = _partner_update_time_hms()

    def repl_light(match):
        return f"{match.group(1)} {hms}"

    # LIGHT title: "📊 Статистика за 07.05.26".
    raw = re.sub(
        r"(📊\s*Статистика\s+за\s+\d{2}\.\d{2}\.\d{2})(?:\s+\d{2}:\d{2}:\d{2})?",
        repl_light,
        raw,
        count=1,
    )

    # PRO title can be just "07.05.26" on one of the first lines.
    lines = raw.splitlines()
    for i, line in enumerate(lines[:4]):
        s = str(line or "").strip()
        m = re.fullmatch(r"(\d{2}\.\d{2}\.\d{2})(?:\s+\d{2}:\d{2}:\d{2})?", s)
        if m:
            lines[i] = f"{m.group(1)} {hms}"
            raw = "\n".join(lines)
            break
    return raw
# --- TPILOT PARTNER TODAY TIME AND TITLE HOTFIX 20260507 END ---





async def main() -> None:
    _ensure_tables()
    await client.start(bot_token=PARTNER_BOT_TOKEN)
    me = await client.get_me()
    print(f"Partner Stat Bot started: @{getattr(me, 'username', '')}")
    asyncio.create_task(_live_loop())
    asyncio.create_task(_preflight_partner_morning_loop())  # TPILOT PREFLIGHT (PARTNER, MORNING+EVENING)
    asyncio.create_task(_preflight_partner_evening_loop())  # TPILOT PREFLIGHT (PARTNER, MORNING+EVENING)
    asyncio.create_task(_arn_sweep_loop())  # TPILOT MANAGER REPLACEMENT STAGE5 PARTNER NOTIFY
    asyncio.create_task(_rsvan_sweep_loop())  # TPILOT RESERVE ACTIVATION PARTNER NOTIFY 20260717
    await client.run_until_disconnected()

# --- TPILOT PARTNER CONTACT IDENTITY PATCH V1 20260510 START ---
# Partner Stat Bot must count and notify only real new contacts:
# lead_countable=1. Returning, duplicate and old_baseline are excluded from normal stats/live.

_TP_PARTNER_CI_ORIG_ENSURE_TABLES = globals().get("_ensure_tables")
_TP_PARTNER_CI_ORIG_PENDING_EVENTS_FOR_SOURCE = globals().get("_pending_events_for_source")
_TP_PARTNER_CI_ORIG_EXPORT_XLSX_FOR_BUYER = globals().get("_export_xlsx_for_buyer")


def _tp_partner_ci_cols(con: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _ensure_tables() -> None:  # type: ignore[override]
    if callable(_TP_PARTNER_CI_ORIG_ENSURE_TABLES):
        _TP_PARTNER_CI_ORIG_ENSURE_TABLES()
    con = _connect()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS partner_lead_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL DEFAULT '',
                source_key TEXT NOT NULL DEFAULT '',
                manager_key TEXT NOT NULL DEFAULT '',
                manager_display_name TEXT DEFAULT '',
                manager_username TEXT DEFAULT '',
                lead_id INTEGER NOT NULL DEFAULT 0,
                chat_id INTEGER NOT NULL DEFAULT 0,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                first_seen_utc TEXT NOT NULL DEFAULT '',
                duplicate INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
        """)
        cols = _tp_partner_ci_cols(con, "partner_lead_events")
        migrations = [
            ("known_contact_id", "ALTER TABLE partner_lead_events ADD COLUMN known_contact_id INTEGER NOT NULL DEFAULT 0"),
            ("contact_kind", "ALTER TABLE partner_lead_events ADD COLUMN contact_kind TEXT NOT NULL DEFAULT 'new'"),
            ("lead_countable", "ALTER TABLE partner_lead_events ADD COLUMN lead_countable INTEGER NOT NULL DEFAULT 1"),
            ("dedupe_reason", "ALTER TABLE partner_lead_events ADD COLUMN dedupe_reason TEXT NOT NULL DEFAULT ''"),
            ("first_seen_global_at", "ALTER TABLE partner_lead_events ADD COLUMN first_seen_global_at TEXT NOT NULL DEFAULT ''"),
            ("first_seen_global_manager_key", "ALTER TABLE partner_lead_events ADD COLUMN first_seen_global_manager_key TEXT NOT NULL DEFAULT ''"),
        ]
        for col, ddl in migrations:
            if col not in cols:
                try:
                    con.execute(ddl)
                except Exception:
                    pass
        con.execute("CREATE INDEX IF NOT EXISTS partner_lead_events_countable_idx ON partner_lead_events(source_key, lead_countable, id)")
        con.execute("CREATE INDEX IF NOT EXISTS partner_lead_events_kind_idx ON partner_lead_events(source_key, contact_kind, id)")

        # R1A2 (2026-08-12, plan section 23-D): additive, idempotent bounded-retry
        # state machine for partner_sent_events. DEFAULT 'sent' on send_status is
        # deliberate -- every EXISTING row already means "handled" (either a real
        # delivery via _mark_event_sent, or an intentional suppression via
        # _mark_old_events_before_live), so no backfill pass is needed: legacy rows
        # classify themselves correctly for free. See plan section 23-D.1.
        sent_cols = _tp_partner_ci_cols(con, "partner_sent_events")
        sent_migrations = [
            ("send_status", "TEXT NOT NULL DEFAULT 'sent'"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_attempt_at", "TEXT NOT NULL DEFAULT ''"),
            ("last_error_class", "TEXT NOT NULL DEFAULT ''"),
        ]
        for _col, _decl in sent_migrations:
            if _col not in sent_cols:
                try:
                    con.execute(f"ALTER TABLE partner_sent_events ADD COLUMN {_col} {_decl}")
                except Exception:
                    pass
        con.execute("CREATE INDEX IF NOT EXISTS partner_sent_events_retry_idx ON partner_sent_events(user_id, send_status, next_attempt_at)")
        con.commit()
    finally:
        con.close()


def _tp_partner_ci_is_countable(row: Dict[str, Any]) -> bool:
    try:
        if "lead_countable" in (row or {}):
            return int((row or {}).get("lead_countable") or 0) == 1
    except Exception:
        pass
    try:
        if int((row or {}).get("duplicate") or 0) == 1:
            return False
    except Exception:
        pass
    kind = str((row or {}).get("contact_kind") or "new").strip().lower()
    return kind in ("", "new")


def _partner_nonduplicate_leads(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:  # type: ignore[override]
    return [x for x in list(leads or []) if _tp_partner_ci_is_countable(x)]


def _partner_duplicate_count(leads: List[Dict[str, Any]]) -> int:  # type: ignore[override]
    # Count all non-countable contacts as technical duplicates for audit counters,
    # but they are excluded from new leads and ordinary Excel.
    return sum(1 for x in list(leads or []) if not _tp_partner_ci_is_countable(x))


def _pending_events_for_source(source_key: str, limit: int = 200, user_id: int = 0) -> List[Dict[str, Any]]:  # type: ignore[override]
    _ensure_tables()
    sk = _norm_key(source_key or "")
    uid = int(user_id or 0)
    con = _connect()
    try:
        if uid:
            # R1A2 (plan section 23-D.3): a plain NOT EXISTS would make the very
            # FIRST recorded attempt (send_status='retry') permanently suppress
            # the event -- a row now legitimately exists. Selectable iff no state
            # row exists yet, OR the row is 'retry' and due. 'sent' and 'dead'
            # never match either branch -- excluded structurally, not by an
            # extra filter, so a future column typo can't silently re-open them.
            rows = con.execute(
                """
                SELECT e.*
                FROM partner_lead_events e
                LEFT JOIN partner_sent_events s
                    ON s.user_id=? AND s.event_id=e.id
                WHERE e.source_key=?
                  AND COALESCE(e.lead_countable, CASE WHEN COALESCE(e.duplicate,0)=1 THEN 0 ELSE 1 END)=1
                  AND ( s.event_id IS NULL
                     OR ( s.send_status = 'retry'
                          AND (COALESCE(s.next_attempt_at,'') = '' OR s.next_attempt_at <= ?) ) )
                ORDER BY e.id ASC
                LIMIT ?
                """,
                (uid, sk, _now_iso(), int(limit)),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT * FROM partner_lead_events
                WHERE source_key=?
                  AND COALESCE(lead_countable, CASE WHEN COALESCE(duplicate,0)=1 THEN 0 ELSE 1 END)=1
                ORDER BY id ASC
                LIMIT ?
                """,
                (sk, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows or []]
    finally:
        con.close()


def _fmt_lead_notification(row: Dict[str, Any], buyer: Dict[str, Any]) -> str:  # type: ignore[override]
    # Live cards are only countable new leads after this patch.
    dt = _lead_dt(row).strftime("%d.%m.%y %H:%M")
    # TPILOT IDENTITY RECONCILE 20260809: was a direct read of the frozen
    # partner_lead_events snapshot columns -- now prefers the live managers row via
    # _partner_current_or_snapshot_identity (falls back to the snapshot only when the
    # manager row itself is gone). See the resolver's own docstring above
    # _partner_manager_label_from_event for why. partner_lead_events is never written
    # here.
    manager_name, manager_username = _partner_current_or_snapshot_identity(
        str(row.get("manager_key") or "").strip(),
        str(row.get("manager_display_name") or ""),
        str(row.get("manager_username") or ""),
    )
    account = f"{manager_name} | @{manager_username}" if manager_username and manager_name else (f"@{manager_username}" if manager_username else (manager_name or "_"))
    lines = ["🆕 Новый лид", "", f"Аккаунт: {account}", f"Время: {dt}"]
    if int(buyer.get("can_view_contacts") or 0) == 1:
        lines.append(f"chat_id: {int(row.get('chat_id') or 0)}")
        username = str(row.get("username") or "").strip().lstrip("@")
        full_name = str(row.get("full_name") or "").strip()
        phone = str(row.get("phone") or "").strip()
        if username:
            lines.append(f"username: @{username}")
        if full_name:
            lines.append(f"Имя: {full_name}")
        if phone:
            lines.append(f"Телефон: {phone}")
    else:
        lines.append("Данные лида: скрыты")
    lines.append("Дубликат: нет")
    return "\n".join(lines).rstrip()


# --- TPILOT R1A2 PARTNER DELIVERY RELIABILITY 20260812 START ---
# Closes F-31 (poison EntityBoundsInvalidError event retried >1000x, never
# terminal) and F-33 (send succeeds, _mark_event_sent falls in the same try,
# a locked DB on the write can silently resend). Plan: plans/model-opus-5-
# mode-playful-dolphin.md section 23 (authoritative), parts A/D/F.
#
# Root cause of the poison event: _fmt_lead_notification's plain text is sent
# with no explicit parse_mode, so Telethon's default Markdown parser applies
# and interprets raw '_'/'*'/'`'/'[' from manager/contact display fields as
# formatting syntax. The fix is explicit parse_mode='html' with every dynamic
# field html-escaped for the primary attempt, and a plain parse_mode=None
# fallback (using the existing unescaped _fmt_lead_notification text) if the
# formatted attempt fails for an entity/formatting reason specifically.

_PSE_FORMATTING_ERROR_CLASS_NAMES = frozenset({
    "EntityBoundsInvalidError", "EntitiesTooLongError",
    "MessageEntitiesTooLongError", "MessageEmptyError",
})
_PSE_FORMATTING_ERROR_TEXT_MARKERS = ("ENTITY_BOUNDS_INVALID", "ENTITIES_TOO_LONG", "MESSAGE_EMPTY")

# Recipient-permanent failures: no number of retries will ever change the
# outcome, so these dead-letter immediately instead of consuming the bounded
# retry budget. Same convention (class-name string match, no telethon.errors
# import needed -- keeps this testable via a plain Exception subclass) as
# manager_bot.py's _mb_classify_delivery_error / _MB_TERMINAL_*_CLASS_NAMES.
_PSE_TERMINAL_RECIPIENT_CLASS_NAMES = frozenset({
    "InputUserDeactivatedError", "UserDeactivatedError", "UserDeactivatedBanError",
    "UserIsBlockedError", "UserBlockedError", "UserBannedInChannelError",
    "PeerIdInvalidError", "UserIdInvalidError", "ChannelPrivateError", "ChatWriteForbiddenError",
})
_PSE_FLOODWAIT_CLASS_NAMES = frozenset({"FloodWaitError", "FloodError", "SlowModeWaitError"})
_PSE_TRANSIENT_DB_MARKERS = ("DATABASE IS LOCKED", "DATABASE IS BUSY")

# Bounded retry budget for a genuinely transient failure (network/db-busy/
# unclassified). Env-overridable, same shape as MANAGER_BOT_SEND_MAX_ATTEMPTS.
PARTNER_SENT_MAX_ATTEMPTS = max(1, int((os.getenv("PARTNER_BOT_SEND_MAX_ATTEMPTS") or "8").strip() or "8"))


def _pse_is_formatting_error(exc: BaseException) -> bool:
    if type(exc).__name__ in _PSE_FORMATTING_ERROR_CLASS_NAMES:
        return True
    text = str(exc).upper()
    return any(marker in text for marker in _PSE_FORMATTING_ERROR_TEXT_MARKERS)


def _pse_classify_error(exc: BaseException) -> Dict[str, Any]:
    """Returns {"category", "terminal", "cooldown_seconds"}. "terminal"=True
    means the caller must dead-letter this event IMMEDIATELY regardless of
    remaining attempt budget (plan section 23-D.2: a deterministic formatting
    failure that survives the plain-text fallback, or a permanently-invalid
    recipient, will never succeed no matter how many times it is retried)."""
    cls_name = type(exc).__name__
    text_upper = str(exc).upper()
    if _pse_is_formatting_error(exc):
        return {"category": "poison_formatting", "terminal": True, "cooldown_seconds": None}
    if cls_name in _PSE_TERMINAL_RECIPIENT_CLASS_NAMES:
        return {"category": "terminal_recipient", "terminal": True, "cooldown_seconds": None}
    if cls_name in _PSE_FLOODWAIT_CLASS_NAMES:
        seconds = getattr(exc, "seconds", None)
        try:
            seconds = int(seconds) if seconds is not None else None
        except Exception:
            seconds = None
        return {"category": "transient_floodwait", "terminal": False, "cooldown_seconds": seconds}
    if isinstance(exc, sqlite3.OperationalError) and any(m in text_upper for m in _PSE_TRANSIENT_DB_MARKERS):
        return {"category": "transient_db_busy", "terminal": False, "cooldown_seconds": None}
    return {"category": "unexpected_internal", "terminal": False, "cooldown_seconds": None}


def _pse_backoff_seconds(attempts: int) -> int:
    """Exponential backoff capped at 1h: 60, 120, 240, 480, ... Same curve as
    manager_bot.py's _mb_backoff_seconds (kept independent/duplicated, not
    imported -- importing manager_bot triggers Telethon client construction
    at module import time, unsafe from partner_stat_bot.py)."""
    return min(60 * (2 ** max(0, attempts - 1)), 3600)


def _pse_backoff_seconds_with_jitter(attempts: int) -> float:
    return _pse_backoff_seconds(attempts) * random.uniform(0.8, 1.2)


def _pse_fmt_lead_notification_html(row: Dict[str, Any], buyer: Dict[str, Any]) -> str:
    """Same content/layout as _fmt_lead_notification, but every dynamic field
    is html.escape'd and the text is meant to be sent with parse_mode='html'.
    Structural text (labels, emoji, punctuation) is not escaped -- it is
    static and contains no user-controlled characters."""
    esc = _html.escape
    dt = esc(_lead_dt(row).strftime("%d.%m.%y %H:%M"))
    manager_name, manager_username = _partner_current_or_snapshot_identity(
        str(row.get("manager_key") or "").strip(),
        str(row.get("manager_display_name") or ""),
        str(row.get("manager_username") or ""),
    )
    manager_name = esc(manager_name)
    manager_username = esc(manager_username)
    account = f"{manager_name} | @{manager_username}" if manager_username and manager_name else (f"@{manager_username}" if manager_username else (manager_name or "_"))
    lines = ["🆕 Новый лид", "", f"Аккаунт: {account}", f"Время: {dt}"]
    if int(buyer.get("can_view_contacts") or 0) == 1:
        lines.append(f"chat_id: {int(row.get('chat_id') or 0)}")
        username = esc(str(row.get("username") or "").strip().lstrip("@"))
        full_name = esc(str(row.get("full_name") or "").strip())
        phone = esc(str(row.get("phone") or "").strip())
        if username:
            lines.append(f"username: @{username}")
        if full_name:
            lines.append(f"Имя: {full_name}")
        if phone:
            lines.append(f"Телефон: {phone}")
    else:
        lines.append("Данные лида: скрыты")
    lines.append("Дубликат: нет")
    return "\n".join(lines).rstrip()


async def _pse_send_notification(uid: int, row: Dict[str, Any], buyer: Dict[str, Any]):
    """Primary attempt: parse_mode='html' with every dynamic field escaped.
    On a formatting/entity-specific failure only, one bounded fallback to the
    existing plain _fmt_lead_notification text with parse_mode=None -- same
    delivery-preserving shape as manager_bot.py's _mb_send_with_fallback. Any
    other exception, or a second failure on the plain-text retry, propagates
    to the caller unchanged. Returns (message, fallback_used)."""
    html_text = _pse_fmt_lead_notification_html(row, buyer)
    try:
        msg = await client.send_message(uid, html_text, buttons=_live_lead_buttons(), parse_mode="html")
        return msg, False
    except Exception as exc:
        if not _pse_is_formatting_error(exc):
            raise
        plain_text = _fmt_lead_notification(row, buyer)
        msg = await client.send_message(uid, plain_text, buttons=_live_lead_buttons(), parse_mode=None)
        return msg, True


def _pse_mark_sent(uid: int, event_id: int) -> bool:
    """Persists a successful delivery. Bounded retry (3 attempts, small
    jitter) against a transiently locked DB -- called AFTER the Telegram send
    already succeeded (F-33: this must never be inside the send's own try),
    so a write failure here must never be treated as a send failure and must
    never trigger a resend. UPSERT (not the old INSERT OR IGNORE) is required:
    once a 'retry' row can exist for an event, a later successful send has to
    be able to flip send_status back to 'sent', which INSERT OR IGNORE could
    never do once the row already exists. 'dead' is excluded from the WHERE
    (plan 23-D.2: dead -> * is forbidden) -- a dead event is never reselected
    for send in the first place, so this is defense in depth, not a live path.

    Residual risk, accepted and documented (mirrors plan section 15 F1a-proof
    for ManagerBot): if the process dies between the Telegram send and this
    write succeeding, delivery is unknown and the event will be retried on
    the normal bounded schedule -- not worse than the pre-fix behavior, and
    no unbounded resend is introduced."""
    now = _now_iso()
    last_exc: Optional[BaseException] = None
    for _attempt in range(3):
        con = _connect()
        try:
            con.execute(
                """
                INSERT INTO partner_sent_events(user_id, event_id, sent_at, attempts, next_attempt_at, send_status, last_error_class)
                VALUES (?, ?, ?, 0, '', 'sent', '')
                ON CONFLICT(user_id, event_id) DO UPDATE SET
                    sent_at=excluded.sent_at,
                    send_status='sent',
                    next_attempt_at=''
                WHERE partner_sent_events.send_status <> 'dead'
                """,
                (int(uid), int(event_id), now),
            )
            con.commit()
            return True
        except sqlite3.OperationalError as exc:
            last_exc = exc
            time.sleep(random.uniform(0.05, 0.15))
            continue
        finally:
            con.close()
    print(f"partner live sent-state persistence degraded user_id={uid} event_id={event_id} error={last_exc!r}")
    return False


def _pse_record_failure(uid: int, event_id: int, exc: BaseException) -> None:
    """Records one failed delivery attempt. Bounded: once attempts reaches
    PARTNER_SENT_MAX_ATTEMPTS, or the failure is classified terminal (dead
    recipient / formatting error that survived the plain-text fallback), the
    row is marked 'dead' -- excluded from _pending_events_for_source forever,
    but never deleted (audit trail preserved, same convention as
    manager_bot.py's manager_bot_sent). Guarded against regressing an
    already-'sent' or already-'dead' row (plan 23-D.2: both are terminal)."""
    if not event_id or not uid:
        return
    con = _connect()
    try:
        row = con.execute(
            "SELECT attempts, send_status FROM partner_sent_events WHERE user_id=? AND event_id=?",
            (int(uid), int(event_id)),
        ).fetchone()
        if row is not None and str(row["send_status"] if row["send_status"] is not None else row[1] or "") in ("sent", "dead"):
            return
        prev_attempts = int(row["attempts"] if row["attempts"] is not None else (row[0] or 0)) if row else 0
        attempts = prev_attempts + 1

        classification = _pse_classify_error(exc)
        terminal = bool(classification["terminal"])
        dead = terminal or attempts >= PARTNER_SENT_MAX_ATTEMPTS
        status = "dead" if dead else "retry"
        if dead:
            next_attempt_at = ""
        elif classification.get("cooldown_seconds"):
            next_attempt_at = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=max(1, int(classification["cooldown_seconds"])))).replace(microsecond=0).isoformat()
        else:
            next_attempt_at = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) + timedelta(
                seconds=_pse_backoff_seconds_with_jitter(attempts))).replace(microsecond=0).isoformat()
        err_cls = type(exc).__name__[:120]
        con.execute(
            """
            INSERT INTO partner_sent_events(user_id, event_id, sent_at, attempts, next_attempt_at, send_status, last_error_class)
            VALUES (?, ?, '', ?, ?, ?, ?)
            ON CONFLICT(user_id, event_id) DO UPDATE SET
                attempts=excluded.attempts,
                next_attempt_at=excluded.next_attempt_at,
                send_status=excluded.send_status,
                last_error_class=excluded.last_error_class
            WHERE partner_sent_events.send_status NOT IN ('sent', 'dead')
            """,
            (int(uid), int(event_id), attempts, next_attempt_at, status, err_cls),
        )
        con.commit()
        print(
            f"partner live send failed user_id={uid} event_id={event_id} attempt={attempts} "
            f"error_class={err_cls} category={classification['category']} action={status}"
        )
    except Exception as db_exc:
        print(f"partner live record failure itself failed user_id={uid} event_id={event_id} error_class={type(db_exc).__name__}")
    finally:
        con.close()
# --- TPILOT R1A2 PARTNER DELIVERY RELIABILITY 20260812 END ---


def _export_xlsx_for_buyer(user_id: int, token: str) -> str:  # type: ignore[override]
    # The original exporter already uses _collect_source_leads and/or
    # _list_leads_for_manager. Those functions are filtered by countability through
    # _partner_nonduplicate_leads in reports. To keep Excel strict, rebuild leads here.
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
        from openpyxl.utils import get_column_letter
    except Exception as e:
        raise RuntimeError("openpyxl не установлен") from e

    buyer = _buyer(user_id)
    if not buyer or int(buyer.get("can_excel") or 0) != 1:
        raise RuntimeError("Excel для вашего доступа выключен")

    sk = _norm_key(buyer.get("source_key") or "")
    if not sk:
        raise RuntimeError("Источник для вашего доступа ещё не назначен")

    start, end, label = _parse_date_token(token)
    leads = _partner_nonduplicate_leads(_collect_source_leads(sk, start, end))
    os.makedirs(EXPORT_DIR, exist_ok=True)
    safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", label).strip("_") or "period"
    out = os.path.join(EXPORT_DIR, f"partner_{sk}_{safe_label}.xlsx")

    wb = Workbook()
    ws = wb.active
    ws.title = "leads"
    headers = [
        "№", "Дата", "Время", "Источник", "manager_key", "Аккаунт менеджера",
        "chat_id", "username", "Имя аккаунта", "Телефон", "Дубликат",
        "Возраст", "Город", "Регион", "Страна", "Статус", "Причина неликвида",
        "Источник гео", "Точность", "Пометка", "Режим менеджера", "Вопрос отправлен",
        "Offline-сообщение", "UA текст", "Менеджер ответил",
        "Тип контакта", "Считать как новый", "Причина"
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    can_contacts = int(buyer.get("can_view_contacts") or 0) == 1
    source_display = _source_name(sk)
    for idx, r in enumerate(leads, 1):
        dt = _lead_dt(r)
        manager_username = str(r.get("manager_username") or "").strip()
        manager_key = str(r.get("manager_key") or "").strip()
        manager_account = "@" + manager_username.lstrip("@") if manager_username else (manager_key or "_")
        status_raw = str(r.get("status") or "").strip()
        status_disp = "ликвид" if status_raw == "liquid" else ("неликвид" if status_raw == "nonliquid" else (status_raw or "не определено"))
        username = str(r.get("username") or "").strip()
        ws.append([
            idx, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S"), source_display, manager_key, manager_account,
            int(r.get("chat_id") or 0) if can_contacts else "_",
            (username if username.startswith("@") else ("@" + username if username else "_")) if can_contacts else "_",
            (str(r.get("full_name") or "").strip() or "_") if can_contacts else "_",
            (str(r.get("phone") or "").strip() or "_") if can_contacts else "_",
            "да" if int(r.get("duplicate") or 0) == 1 else "нет",
            str(r.get("age") if r.get("age") is not None else "_").strip() or "_",
            str(r.get("city") or "").strip() or "_",
            str(r.get("region") or "").strip() or "_",
            str(r.get("country") or "").strip() or "_",
            status_disp,
            str(r.get("nonliquid_reason") or "").strip() or "_",
            str(r.get("geo_source") or "").strip() or "_",
            str(r.get("geo_confidence") or "").strip() or "_",
            str(r.get("geo_note") or "").strip() or "_",
            str(r.get("manager_work_status") or "").strip() or "_",
            "да" if int(r.get("profile_question_sent") or 0) == 1 else "нет",
            "да" if int(r.get("offline_notice_sent") or 0) == 1 else "нет",
            "да" if int(r.get("ua_text_sent") or 0) == 1 else "нет",
            "да" if int(r.get("manager_replied") or 0) == 1 else "нет",
            str(r.get("contact_kind") or "new"),
            "да" if _tp_partner_ci_is_countable(r) else "нет",
            str(r.get("dedupe_reason") or "new_contact"),
        ])
    widths = [6,14,13,22,16,24,18,22,28,18,12,10,22,26,18,16,24,18,14,30,18,18,18,12,18,16,18,24]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    wb.save(out)
    return out
# --- TPILOT PARTNER CONTACT IDENTITY PATCH V1 20260510 END ---

# --- TPILOT PARTNER QUALITY STATUS ENGINE V1 20260510 START ---
# Partner statistics now prefer quality_bucket over legacy status/nonliquid_reason.

_TP_PARTNER_QS_VERSION = "partner_quality_status_v1_20260510"


def _tp_partner_qs_text(raw: Any) -> str:
    return str(raw or "").strip()


def _tp_partner_qs_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except Exception:
        return int(default)


def _tp_partner_qs_age(raw: Any):
    try:
        if raw is None or str(raw).strip() == "":
            return None
        return int(float(str(raw).strip()))
    except Exception:
        return None
# --- TPILOT PARTNER QUALITY STATUS ENGINE V1 20260510 END ---

# --- TPILOT PARTNER REPORT CONSISTENCY HOTFIX V3 20260510 START ---
# Final payment-safe partner reporting:
# OTPISOK = LIQUID + NONLIQUID
# NONLIQUID = GEO + -18 + NA + TRASH
# "-18" includes both explicit under 18 and age not confirmed.

_TP_PARTNER_REPORT_V3_VERSION = "partner_report_consistency_v3_20260510"


def _tp_pr_v3_text(raw: Any) -> str:
    return str(raw or "").strip()


def _tp_pr_v3_low(raw: Any) -> str:
    return _tp_pr_v3_text(raw).lower().replace("ё", "е")


def _tp_pr_v3_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except Exception:
        return int(default)


def _tp_pr_v3_age(raw: Any):
    try:
        if raw is None or str(raw).strip() == "":
            return None
        return int(float(str(raw).strip()))
    except Exception:
        return None


def _tp_pr_v3_country_title(raw: Any) -> str:
    s = _tp_pr_v3_text(raw)
    if not s:
        return ""
    mapping = {
        "украина": "Украина", "узбекистан": "Узбекистан", "казахстан": "Казахстан",
        "кыргызстан": "Кыргызстан", "киргизия": "Кыргызстан", "беларусь": "Беларусь",
        "белоруссия": "Беларусь", "молдова": "Молдова", "грузия": "Грузия",
        "германия": "Германия", "индия": "Индия", "гана": "Гана", "таиланд": "Таиланд",
        "таджикистан": "Таджикистан", "азербайджан": "Азербайджан", "армения": "Армения",
    }
    low = _tp_pr_v3_low(s).replace("_", " ")
    return mapping.get(low, s[:1].upper() + s[1:])


def _tp_pr_v3_reason_ru(reason: Any, *, bucket: str = "", country: Any = "") -> str:
    raw = _tp_pr_v3_text(reason)
    low = _tp_pr_v3_low(raw).replace("-", "_")
    if low.startswith("country_"):
        return "гео: " + _tp_pr_v3_country_title(raw.split("_", 1)[1].replace("_", " "))
    mapping = {
        "age_and_geo_missing": "нет города и возраста",
        "age_missing": "нет возраста",
        "geo_missing": "нет города",
        "under18": "несовершеннолетний",
        "trash_text": "🗑 trash",
        "trash": "🗑 trash",
        "send_failed": "чат недоступен / отправка не прошла",
        "blocked": "клиент заблокировал",
        "ru_18_plus": "Россия, 18+",
        "ru_18_plus_city_unknown": "Россия, 18+, город не определён",
    }
    if low in mapping:
        return mapping[low]
    c = _tp_pr_v3_country_title(country)
    if c and bucket == "geo":
        return "гео: " + c
    return raw or "_"
# --- TPILOT PARTNER REPORT CONSISTENCY HOTFIX V3 20260510 END ---

# --- TPILOT PARTNER REPORT CONSISTENCY HOTFIX V5 20260510 START ---
# Final payment-safe partner reporting:
# OTPISOK = LIQUID + NONLIQUID
# NONLIQUID = GEO + -18 + NA + TRASH
# "-18" includes both explicit under 18 and age not confirmed.

_TP_PARTNER_REPORT_V5_VERSION = "partner_report_consistency_v5_20260510"


def _tp_pr_v5_text(raw: Any) -> str:
    return str(raw or "").strip()


def _tp_pr_v5_low(raw: Any) -> str:
    return _tp_pr_v5_text(raw).lower().replace("ё", "е")


def _tp_pr_v5_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except Exception:
        return int(default)


def _tp_pr_v5_age(raw: Any):
    # --- TPILOT AGE0 FIX M2.6I START ---
    # age=0 is a technical placeholder for "unknown", never a real age — normalize to None.
    try:
        if raw is None or str(raw).strip() == "":
            return None
        v = int(float(str(raw).strip()))
        return v if v != 0 else None
    except Exception:
        return None
    # --- TPILOT AGE0 FIX M2.6I END ---


def _tp_pr_v5_country_title(raw: Any) -> str:
    s = _tp_pr_v5_text(raw)
    if not s:
        return ""
    mapping = {
        "украина": "Украина", "узбекистан": "Узбекистан", "казахстан": "Казахстан",
        "кыргызстан": "Кыргызстан", "киргизия": "Кыргызстан", "беларусь": "Беларусь",
        "белоруссия": "Беларусь", "молдова": "Молдова", "грузия": "Грузия",
        "германия": "Германия", "индия": "Индия", "гана": "Гана", "таиланд": "Таиланд",
        "таджикистан": "Таджикистан", "азербайджан": "Азербайджан", "армения": "Армения",
    }
    low = _tp_pr_v5_low(s).replace("_", " ")
    return mapping.get(low, s[:1].upper() + s[1:])


def _tp_pr_v5_reason_ru(reason: Any, *, bucket: str = "", country: Any = "") -> str:
    raw = _tp_pr_v5_text(reason)
    low = _tp_pr_v5_low(raw).replace("-", "_")
    if low.startswith("country_"):
        return "гео: " + _tp_pr_v5_country_title(raw.split("_", 1)[1].replace("_", " "))
    mapping = {
        "age_and_geo_missing": "нет города и возраста",
        "age_missing": "нет возраста",
        "geo_missing": "нет города",
        "under18": "несовершеннолетний",
        "trash_text": "🗑 trash",
        "trash": "🗑 trash",
        "send_failed": "чат недоступен / отправка не прошла",
        "blocked": "клиент заблокировал",
        "ru_18_plus": "Россия, 18+",
        "ru_18_plus_city_unknown": "Россия, 18+, город не определён",
    }
    if low in mapping:
        return mapping[low]
    c = _tp_pr_v5_country_title(country)
    if c and bucket == "geo":
        return "гео: " + c
    return raw or "_"


def _tp_pr_v5_under18_reason(lead: Dict[str, Any]) -> str:
    age = _tp_pr_v5_age((lead or {}).get("age"))
    if age is not None and age < 18:
        return f"{age} лет"
    return ""


def _tp_pr_v5_lead_reason(lead: Dict[str, Any]) -> str:
    bucket = _tp_partner_qs_bucket(lead)
    if bucket == "under18":
        return _tp_pr_v5_under18_reason(lead)
    if bucket == "geo":
        return _tp_pr_v5_reason_ru((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason"), bucket="geo", country=(lead or {}).get("country"))
    if bucket == "trash":
        return _tp_pr_v5_reason_ru((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "trash", bucket="trash")
    if bucket == "na":
        return _tp_pr_v5_reason_ru((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "age_and_geo_missing", bucket="na")
    return _tp_pr_v5_reason_ru((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason"), bucket=bucket, country=(lead or {}).get("country"))


def _tp_partner_qs_bucket(lead: Dict[str, Any]) -> str:  # type: ignore[override]
    bucket = _tp_pr_v5_text((lead or {}).get("quality_bucket"))
    if bucket == "age_missing":
        return "under18"
    if bucket == "geo_missing":
        return "na"
    if bucket in ("liquid", "geo", "under18", "na", "trash"):
        return bucket
    status = _tp_pr_v5_low((lead or {}).get("status"))
    reason = _tp_pr_v5_low((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason"))
    country = _tp_pr_v5_text((lead or {}).get("country"))
    city = _tp_pr_v5_text((lead or {}).get("city"))
    age = _tp_pr_v5_age((lead or {}).get("age"))
    if _tp_pr_v5_int((lead or {}).get("trash")) == 1 or status == "trash" or "trash" in reason or "send_failed" in reason or "недоступ" in reason or "blocked" in reason:
        return "trash"
    if age is not None and age < 18:
        return "under18"
    if country and country != "Россия":
        return "geo"
    if country == "Россия" and age is not None and age >= 18:
        return "liquid"
    if age is None and (country or city):
        return "under18"
    return "na"


def _bucket_empty() -> Dict[str, int]:  # type: ignore[override]
    return {"otpisok": 0, "nonliquid": 0, "geo": 0, "under18": 0, "na": 0, "trash": 0, "liquid": 0}


def _bucket_add(b: Dict[str, int], lead: Dict[str, Any]) -> None:  # type: ignore[override]
    bucket = _tp_partner_qs_bucket(lead)
    if bucket == "liquid":
        b["liquid"] = int(b.get("liquid") or 0) + 1
    elif bucket == "geo":
        b["geo"] = int(b.get("geo") or 0) + 1
    elif bucket == "under18":
        b["under18"] = int(b.get("under18") or 0) + 1
    elif bucket == "trash":
        b["trash"] = int(b.get("trash") or 0) + 1
    else:
        b["na"] = int(b.get("na") or 0) + 1
    b["nonliquid"] = int(b.get("geo") or 0) + int(b.get("under18") or 0) + int(b.get("na") or 0) + int(b.get("trash") or 0)
    b["otpisok"] = int(b.get("liquid") or 0) + int(b.get("nonliquid") or 0)


def _format_bucket_lines(b: Dict[str, int]) -> List[str]:  # type: ignore[override]
    geo = int(b.get("geo") or 0)
    under18 = int(b.get("under18") or 0)
    na = int(b.get("na") or 0)
    trash = int(b.get("trash") or 0)
    liquid = int(b.get("liquid") or 0)
    nonliquid = geo + under18 + na + trash
    otpisok = liquid + nonliquid
    return [
        f"ОТПИСОК: {otpisok}",
        f"НЕЛИКВИД: {nonliquid}",
        f"ГЕО: {geo}",
        f"-18: {under18}",
        f"NA: {na}",
        f"TRASH: {trash}",
        f"ЛИКВИД: {liquid}",
    ]


def _geo_reason_counts(leads: List[Dict[str, Any]]) -> Dict[str, int]:  # type: ignore[override]
    out: Dict[str, int] = {}
    for lead in leads or []:
        if _tp_partner_qs_bucket(lead) != "geo":
            continue
        country = _tp_pr_v5_text((lead or {}).get("country"))
        reason = _tp_pr_v5_text((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason"))
        label = _tp_pr_v5_reason_ru(reason, bucket="geo", country=country)
        out[label] = out.get(label, 0) + 1
    return out


def _tp_pr_v5_detail_counts(leads: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    out: Dict[str, Dict[str, int]] = {"geo": {}, "under18": {}, "na": {}, "trash": {}}
    for lead in leads or []:
        bucket = _tp_partner_qs_bucket(lead)
        if bucket not in out:
            continue
        reason = _tp_pr_v5_lead_reason(lead)
        if not reason or reason == "_":
            continue
        if bucket == "geo" and reason.startswith("гео:"):
            reason = reason.split(":", 1)[1].strip() or "не определено"
        out[bucket][reason] = int(out[bucket].get(reason, 0) or 0) + 1
    return out


def _tp_pr_v5_append_details(lines: List[str], leads: List[Dict[str, Any]]) -> None:
    details = _tp_pr_v5_detail_counts(leads)
    if not any(details.values()):
        return
    lines.append("")
    lines.append("Детализация неликвида")
    if details.get("geo"):
        lines.append("ГЕО")
        for label, cnt in sorted(details["geo"].items(), key=lambda x: (-int(x[1]), str(x[0]))):
            if int(cnt) > 0:
                lines.append(f"{label}: {int(cnt)}")
    if details.get("under18"):
        lines.append("-18")
        def _age_sort_key(item: Tuple[str, int]) -> Tuple[int, int, str]:
            label, cnt = item
            import re
            m = re.search(r"возраст\s+(\d+)", str(label))
            age = int(m.group(1)) if m else 999
            return (age, -int(cnt), str(label))
        for label, cnt in sorted(details["under18"].items(), key=_age_sort_key):
            if int(cnt) > 0:
                lines.append(f"{label}: {int(cnt)}")
    if details.get("na"):
        lines.append("NA")
        for label, cnt in sorted(details["na"].items(), key=lambda x: (-int(x[1]), str(x[0]))):
            if int(cnt) > 0:
                lines.append(f"{label}: {int(cnt)}")
    if details.get("trash"):
        lines.append("TRASH")
        for label, cnt in sorted(details["trash"].items(), key=lambda x: (-int(x[1]), str(x[0]))):
            if int(cnt) > 0:
                lines.append(f"{label}: {int(cnt)}")
# --- TPILOT PARTNER REPORT CONSISTENCY HOTFIX V5 20260510 END ---


# --- TPILOT PARTNER STAT FORMAT HOTFIX V3 20260511 START ---
# Final late override placed before __main__ execution.
# Fixes active _format_stats_for_buyer after previous tail overrides.
# LIGHT: total writers + duplicates only.
# PRO: payment report. Formula: ОТПИСОК = ЛИКВИД + НЕЛИКВИД; НЕЛИКВИД = ГЕО + -18 + NA + TRASH.


def _psf3_now_hms():
    try:
        return _kyiv_now().strftime("%H:%M:%S")
    except Exception:
        try:
            return datetime.now().strftime("%H:%M:%S")
        except Exception:
            return "00:00:00"


def _psf3_date_title(start, end, label):
    try:
        if start == end:
            return f"{start.strftime('%d.%m.%Y')} {_psf3_now_hms()}"
    except Exception:
        pass
    raw = str(label or "").strip()
    return raw or f"{_kyiv_now().date().strftime('%d.%m.%Y')} {_psf3_now_hms()}"


def _psf3_date_list(start, end):
    try:
        return _date_range_list(start, end)
    except Exception:
        out = []
        cur = start
        while cur <= end:
            out.append(cur.isoformat())
            cur = cur + timedelta(days=1)
        return out


def _psf3_manager_label(row):
    try:
        return _manager_account_label(row)
    except Exception:
        username = str((row or {}).get("telegram_username") or (row or {}).get("manager_username") or "").strip().lstrip("@")
        name = str((row or {}).get("display_name") or (row or {}).get("manager_key") or "").strip()
        if username and name and name.lower() != username.lower():
            return f"{name} | @{username}"
        if username:
            return f"@{username}"
        return name or str((row or {}).get("manager_key") or "_")


# --- TPILOT M2.10A FINAL-BUCKET OVERRIDE OVERLAY (read-only) START ---
# Brings PartnerBot's bucketing in line with ManagerBot: when a manual
# override exists in lead_status_overrides for (manager_key, chat_id), it is
# authoritative over daily_leads.quality_bucket. The overlay only mutates the
# in-memory lead dicts returned by _psf3_leads_for_manager BEFORE the existing
# (unchanged) _psf3_bucket/_psf3_bucket_add functions run; it never writes to
# any DB and never touches LIGHT/PRO/flight routing or duplicate display.
_PSF3_OVERRIDE_BUCKET_CANON = ("liquid", "geo", "under18", "na", "trash")

_PSF3_OVERRIDE_STATUS_FOR_BUCKET = {
    "liquid": "liquid",
    "geo": "nonliquid",
    "under18": "nonliquid",
    "trash": "trash",
    "na": "na",
}


def _psf3_override_bucket_value(raw):
    v = str(raw or "").strip().lower()
    if v in _PSF3_OVERRIDE_BUCKET_CANON:
        return v
    if v == "age_missing":
        return "under18"
    if v == "geo_missing":
        return "na"
    return ""


def _psf3_table_exists(con, table):
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
        ).fetchone())
    except Exception:
        return False


def _psf3_overrides_for_manager(row, con):
    """Load lead_status_overrides once per manager (reuses the open connection
    already used for daily_leads -> no extra connection, no per-lead queries).

    Returns {chat_id: {"bucket": canon, "reason": str, "status": str}}.
    Fail-safe: missing table/columns/errors -> {}.
    """
    mk = _norm_key((row or {}).get("manager_key") or "")
    per_chat = {}
    if not mk or con is None:
        return per_chat
    try:
        if not _psf3_table_exists(con, "lead_status_overrides"):
            return per_chat
        for r in con.execute(
            "SELECT chat_id, status, bucket, reason FROM lead_status_overrides WHERE manager_key=?",
            (mk,),
        ).fetchall():
            try:
                chat_id = int(r["chat_id"] or 0)
            except Exception:
                continue
            if chat_id <= 0:
                continue
            norm = _psf3_override_bucket_value(r["bucket"])
            if not norm:
                norm = _psf3_override_bucket_value(r["status"])
            if not norm:
                continue
            per_chat[chat_id] = {
                "bucket": norm,
                "reason": str(r["reason"] or ""),
                "status": str(r["status"] or ""),
            }
    except Exception:
        return {}
    return per_chat


def _psf3_apply_override_overlay(leads, overrides):
    """Mutate in-memory lead entries (in place) so existing unchanged bucket
    functions (_psf3_bucket, which reads quality_bucket/quality_status first)
    resolve to the manual override. Does not touch windows, countable filters,
    duplicate logic, nonliquid formula, or any DB row."""
    if not leads or not overrides:
        return
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        try:
            chat_id = int((lead or {}).get("chat_id") or 0)
        except Exception:
            continue
        if chat_id <= 0:
            continue
        ov = overrides.get(chat_id)
        if not ov:
            continue
        bucket = ov.get("bucket")
        if bucket not in _PSF3_OVERRIDE_BUCKET_CANON:
            continue
        lead["quality_bucket"] = bucket
        lead["quality_status"] = _PSF3_OVERRIDE_STATUS_FOR_BUCKET.get(bucket, "na")
        if ov.get("reason"):
            lead["quality_reason"] = ov.get("reason")
        lead["_manual_override_overlay"] = 1
# --- TPILOT M2.10A FINAL-BUCKET OVERRIDE OVERLAY (read-only) END ---


def _psf3_manager_db_path(row):
    p = str((row or {}).get("db_path") or "").strip()
    try:
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    try:
        return _manager_db_path(row)
    except Exception:
        mk = str((row or {}).get("manager_key") or "").strip()
        if not mk:
            return ""
        return os.path.join(str(BASE_DIR), "runtime", "managers", mk, f"{mk}.db")


def _psf3_leads_for_manager(row, start, end):
    dbp = _psf3_manager_db_path(row)
    try:
        if not dbp or not os.path.exists(dbp):
            return []
    except Exception:
        return []
    dates = _psf3_date_list(start, end)
    if not dates:
        return []
    q = ",".join(["?"] * len(dates))
    con = sqlite3.connect(dbp, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT * FROM daily_leads WHERE lead_date IN ({q}) ORDER BY first_seen_utc ASC, id ASC",
            tuple(dates),
        ).fetchall()
        out = []
        for r in rows or []:
            d = dict(r)
            d["manager_key"] = _norm_key((row or {}).get("manager_key") or d.get("manager_key") or "")
            d["manager_username"] = str((row or {}).get("telegram_username") or d.get("manager_username") or "")
            d["manager_display_name"] = str((row or {}).get("display_name") or d.get("manager_key") or "")
            out.append(d)
        try:
            overrides = _psf3_overrides_for_manager(row, con)
            _psf3_apply_override_overlay(out, overrides)
        except Exception:
            pass
        return out
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _psf3_countable(lead):
    try:
        if "lead_countable" in (lead or {}):
            return int((lead or {}).get("lead_countable") or 0) == 1
    except Exception:
        return False
    try:
        return int((lead or {}).get("duplicate") or 0) != 1
    except Exception:
        return True


def _psf3_duplicate_for_light(lead):
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


def _buyer_show_duplicates(buyer) -> bool:
    """Return True (show duplicates) unless buyer explicitly has show_duplicates=0."""
    val = (buyer or {}).get("show_duplicates")
    if val is None:
        return True
    try:
        return int(val) != 0
    except Exception:
        return True


def _psf3_is_buyer_duplicate(lead) -> bool:
    """Canonical buyer-duplicate predicate for buyer-facing PartnerBot stat filtering.
    Returns True for any lead that should be invisible when show_duplicates=0."""
    kind = str((lead or {}).get("contact_kind") or "").strip().lower()
    reason = str((lead or {}).get("dedupe_reason") or "").strip().lower()
    try:
        if int((lead or {}).get("duplicate") or 0) == 1:
            return True
    except Exception:
        pass
    if kind in ("duplicate", "returning", "old_baseline"):
        return True
    if "other_manager" in reason or "other manager" in reason:
        return True
    try:
        if _tp_partner_dup_is_identity_duplicate(lead):
            return True
    except Exception:
        pass
    return False


def _psf3_age(lead):
    # --- TPILOT AGE0 FIX M2.6I START ---
    # age=0 is a technical placeholder for "unknown", never a real age — normalize to None.
    try:
        raw = (lead or {}).get("age")
        if raw is None or str(raw).strip() == "":
            return None
        v = int(raw)
        return v if v != 0 else None
    except Exception:
        return None
    # --- TPILOT AGE0 FIX M2.6I END ---


def _psf3_bucket_empty():
    return {"otpisok": 0, "nonliquid": 0, "geo": 0, "under18": 0, "na": 0, "trash": 0, "liquid": 0}


def _psf3_bucket(lead):
    qb = str((lead or {}).get("quality_bucket") or "").strip().lower()
    qs = str((lead or {}).get("quality_status") or "").strip().lower()
    reason = str((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "").strip().lower()
    status = str((lead or {}).get("status") or "").strip().lower()
    country = str((lead or {}).get("country") or "").strip()
    age = _psf3_age(lead)

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


def _psf3_bucket_add(b, lead):
    bucket = _psf3_bucket(lead)
    b["otpisok"] += 1
    if bucket == "liquid":
        b["liquid"] += 1
    elif bucket == "geo":
        b["geo"] += 1
    elif bucket == "under18":
        b["under18"] += 1
    elif bucket == "trash":
        b["trash"] += 1
    else:
        b["na"] += 1
    b["nonliquid"] = b["geo"] + b["under18"] + b["na"] + b["trash"]


def _psf3_bucket_lines(b):
    return [
        f"ОТПИСОК: {int(b.get('otpisok') or 0)}",
        f"НЕЛИКВИД: {int(b.get('nonliquid') or 0)}",
        f"ГЕО: {int(b.get('geo') or 0)}",
        f"-18: {int(b.get('under18') or 0)}",
        f"NA: {int(b.get('na') or 0)}",
        f"TRASH: {int(b.get('trash') or 0)}",
        f"ЛИКВИД: {int(b.get('liquid') or 0)}",
    ]


def _psf3_country_title(country, reason=""):
    raw = str(country or "").strip()
    low = (raw or str(reason or "")).strip().lower()
    if low.startswith("country_"):
        low = low.split("country_", 1)[1].strip()
    low = re.sub(r"[^a-zа-яёіїєґ\s_-]+", " ", low, flags=re.IGNORECASE).strip()
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


def _psf3_na_reason_ru(reason):
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


def _psf3_trash_reason_ru(reason):
    low = str(reason or "").strip().lower()
    if "send_failed" in low or "blocked" in low or "inaccessible" in low or "недоступ" in low:
        return "чат недоступен / отправка не прошла"
    return "🗑 trash"


def _psf3_details(leads):
    geo = {}
    under18 = {}
    na = {}
    trash = {}
    for lead in list(leads or []):
        bucket = _psf3_bucket(lead)
        reason = str((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "").strip()
        if bucket == "geo":
            if str((lead or {}).get("country") or "").strip() not in ("Россия", "РФ", "Российская Федерация"):
                title = _psf3_country_title(str((lead or {}).get("country") or ""), reason)
                geo[title] = geo.get(title, 0) + 1
        elif bucket == "under18":
            age = _psf3_age(lead)
            if age is not None and age < 18:
                title = f"{age} лет"
                under18[title] = under18.get(title, 0) + 1
        elif bucket == "na":
            title = _psf3_na_reason_ru(reason)
            na[title] = na.get(title, 0) + 1
        elif bucket == "trash":
            title = _psf3_trash_reason_ru(reason)
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


def _psf3_buyer_context(user_id, token):
    buyer = _buyer(int(user_id))
    if not buyer or int((buyer or {}).get("is_enabled") or 0) != 1:
        return None, [], None, None, "Доступ не выдан или выключен. Напишите /start, чтобы отправить заявку."
    if int((buyer or {}).get("can_view_stats") or 0) != 1:
        return None, [], None, None, "Статистика для вашего доступа выключена администратором."
    sk = _norm_key((buyer or {}).get("source_key") or "")
    if not sk:
        return buyer, [], None, None, "Источник для вашего доступа ещё не назначен."
    start, end, label = _parse_date_token(token)
    # DELETED MANAGER STATS RETENTION 20260711 (period-filter correction): this is
    # the single active funnel for light/pro/both text stats -- pass the resolved
    # [start, end] range so a tombstoned manager surfaces only if it intersects
    # their real data (or schedule) up to their own deletion date.
    managers = _manager_rows_for_source(sk, start.isoformat(), end.isoformat())
    if not managers:
        return buyer, [], start, end, f"Источник { _source_name(sk) } пока не привязан к менеджерам."
    # Stage A: schedule-aware visible managers. Single-day views only (start == end);
    # multi-day ranges stay unfiltered. Fail-open: a day with zero schedule rows (set empty)
    # or any error keeps the full source list. Mirrors the bizlinks view filter (_psbl_text).
    if start == end and callable(_psbl_sched_working_keys):
        try:
            working_keys = {
                _norm_key(str(k or ""))
                for k in _psbl_sched_working_keys(start.isoformat(), TPILOT_DB_PATH)
            }
            if working_keys:
                managers = [
                    r for r in managers
                    if _norm_key(str(r.get("manager_key") or "")) in working_keys
                ]
        except Exception:
            pass  # fail-open: keep original managers list
    # STAGE D R3B: activated reserves must be visible regardless of normal schedule.
    if start == end:
        managers = _rsva_merge_activated_reserves(managers, sk, start.isoformat())
    return buyer, managers, start, end, ""


def _psf3_mode(user_id):
    buyer = _buyer(int(user_id))
    raw = str((buyer or {}).get("stat_format") or "pro").strip().lower().replace(" ", "")
    raw = raw.replace("лёгкий", "light").replace("легкий", "light").replace("лайт", "light").replace("про", "pro").replace("оба", "both")
    if raw in ("both", "all", "lightpro", "light+pro", "litepro", "lite+pro", "light_pro"):
        return "both"
    if "light" in raw and "pro" in raw:
        return "both"
    if raw in ("light", "lite") or ("light" in raw and "pro" not in raw):
        return "light"
    return "pro"


def _psf3_light_body(managers, start, end, drop_duplicates=False):
    lines = []
    total_all = 0
    dup_all = 0
    for row in managers:
        leads_all = _psf3_leads_for_manager(row, start, end)
        if drop_duplicates:
            leads_all = [lead for lead in leads_all if not _psf3_is_buyer_duplicate(lead)]
        total = len(leads_all)
        dup = 0 if drop_duplicates else sum(1 for lead in leads_all if _psf3_duplicate_for_light(lead))
        total_all += total
        dup_all += dup
        lines.append(_psf3_manager_label(row))
        lines.append(f"Всего написавших: {total}")
        if not drop_duplicates:
            lines.append(f"Дубликаты: {dup}")
        lines.append("")
    lines.append("ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ")
    lines.append(f"Всего написавших: {total_all}")
    if not drop_duplicates:
        lines.append(f"Дубликаты: {dup_all}")
    return "\n".join(lines).rstrip()


def _format_stats_light_for_buyer(user_id, token="today"):
    buyer, managers, start, end, err = _psf3_buyer_context(int(user_id), token)
    if err:
        return err
    _s, _e, label = _parse_date_token(token)
    drop = not _buyer_show_duplicates(buyer)
    return "\n".join([_psf3_date_title(start, end, label), "", _psf3_light_body(managers, start, end, drop_duplicates=drop)]).rstrip()


def _partner_menu_payload(user_id):
    buyer = _buyer(int(user_id))
    if not buyer or int((buyer or {}).get("is_enabled") or 0) != 1:
        return "Доступ пока не выдан. Заявка уже отправлена администратору. Ожидайте подтверждения.", None
    _touch_buyer(int(user_id))
    src = _source_name(str((buyer or {}).get("source_key") or ""))
    try:
        stats_text = _format_stats_for_buyer(int(user_id), "today")
    except Exception as e:
        stats_text = f"Статистика временно недоступна: {e!r}"
    text = "\n".join([
        "🤝 Партнёрская панель статистики",
        "",
        f"Ваш источник: {src}",
        "",
        stats_text,
        "",
        "Выберите действие:",
    ]).rstrip()
    return text, _main_buttons(buyer)

# --- TPILOT PARTNER STAT FORMAT HOTFIX V3 20260511 END ---



# --- TPILOT PARTNER DAY/FLIGHT PRO SEPARATION V1 20260512 START ---
# Partner LIGHT stays calendar-day based. Partner PRO gets day 08:00-17:00 and flights 17:00-08:00 separately.
_TP_PDF_VERSION = "partner_day_flight_pro_v1_20260512"
_TP_PDF_ORIG_MAIN_BUTTONS = globals().get("_main_buttons")
_TP_PDF_ORIG_EXPORT_XLSX_FOR_BUYER = globals().get("_export_xlsx_for_buyer")
_TP_PDF_ORIG_PARTNER_START_PERIOD_WIZARD = globals().get("_partner_start_period_wizard")
_TP_PDF_ORIG_PARTNER_HANDLE_PERIOD_WIZARD = globals().get("_partner_handle_period_wizard")


def _tp_pdf_local_dt(lead):
    raw_local = str((lead or {}).get("first_seen_kyiv") or "").strip()
    if raw_local:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw_local[:19], fmt).replace(tzinfo=TZ)
            except Exception:
                pass
    raw_utc = str((lead or {}).get("first_seen_utc") or "").strip()
    if raw_utc:
        try:
            dt = datetime.fromisoformat(raw_utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(TZ)
        except Exception:
            pass
    return None


def _tp_pdf_date_iter(start, end):
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)


def _tp_pdf_windows(start, end, kind="day", window_cfg=None):
    kind = str(kind or "day").lower()
    ds_min, de_min = _psf3c3_minutes(window_cfg)
    for d in _tp_pdf_date_iter(start, end):
        base = datetime(d.year, d.month, d.day, tzinfo=TZ)
        if kind == "flight":
            # Date is morning/closing date. Night = exact complement of the day
            # window so day+night tile with no overlap/gap (mirrors se_flight_window).
            yield (base - timedelta(days=1)) + timedelta(minutes=de_min), base + timedelta(minutes=ds_min)
        else:
            yield base + timedelta(minutes=ds_min), base + timedelta(minutes=de_min)


def _tp_pdf_in_windows(lead, start, end, kind="day", window_cfg=None):
    dt = _tp_pdf_local_dt(lead)
    if not dt:
        return False
    for ws, we in _tp_pdf_windows(start, end, kind, window_cfg=window_cfg):
        if ws <= dt < we:
            return True
    return False


def _tp_pdf_label(start, end, label, kind="day", *, window_cfg=None, period_mode=None):
    if start == end:
        title = start.strftime("%d.%m.%Y")
    else:
        title = str(label or f"{start.strftime('%d.%m.%Y')} - {end.strftime('%d.%m.%Y')}")
    if str(kind).lower() == "flight":
        return f"🌙 Долёты за {title}\nОкно: {_psf3c3_flight_window_text(window_cfg)}"
    return f"📊 День за {title}\nОкно: {_psf3c3_day_window_text(window_cfg, period_mode)}"


def _psf3_pro_body(managers, start, end, drop_duplicates=False, window_cfg=None, period_mode=None):  # type: ignore[override]
    lines = []
    total_bucket = _psf3_bucket_empty()
    all_countable = []
    for row in managers:
        leads_all = _tp_pdf_windowed_leads_for_manager(row, start, end, "day")
        if drop_duplicates:
            leads_all = [lead for lead in leads_all if not _psf3_is_buyer_duplicate(lead)]
        leads = [lead for lead in leads_all if _psf3_countable(lead)]
        b = _psf3_bucket_empty()
        for lead in leads:
            _psf3_bucket_add(b, lead)
            _psf3_bucket_add(total_bucket, lead)
        all_countable.extend(leads)
        lines.append(_psf3_manager_label(row))
        lines.extend(_psf3_bucket_lines(b))
        lines.append("")
    lines.append("ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ")
    lines.extend(_psf3_bucket_lines(total_bucket))
    details = _psf3_details(all_countable)
    if details:
        lines.append("")
        lines.extend(details)
    return "\n".join(lines).rstrip()


def _tp_pdf_flight_body(managers, start, end, drop_duplicates=False, window_cfg=None):
    lines = []
    total_bucket = _psf3_bucket_empty()
    all_countable = []
    for row in managers:
        leads_all = _tp_pdf_windowed_leads_for_manager(row, start, end, "flight")
        if drop_duplicates:
            leads_all = [lead for lead in leads_all if not _psf3_is_buyer_duplicate(lead)]
        leads = [lead for lead in leads_all if _psf3_countable(lead)]
        b = _psf3_bucket_empty()
        for lead in leads:
            _psf3_bucket_add(b, lead)
            _psf3_bucket_add(total_bucket, lead)
        all_countable.extend(leads)
        lines.append(_psf3_manager_label(row))
        lines.extend(_psf3_bucket_lines(b))
        lines.append("")
    lines.append("ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ")
    lines.extend(_psf3_bucket_lines(total_bucket))
    details = _psf3_details(all_countable)
    if details:
        lines.append("")
        lines.extend(details)
    return "\n".join(lines).rstrip()


# --- TPILOT STAGE C3: Resolve source-level window/period config 20260621 START ---
# Source-scoped period behavior (Stage C1/C-add-1 storage). Resolved once per
# render from the buyer's source_key; never per-lead/per-manager. No row, no
# source, disabled row, or any error -> (None, None) i.e. current defaults
# (work-window 08:00-17:00 + dolyoty). Exports (_tp_pdf_export_xlsx) are out of
# scope here (Stage C4).
try:
    from storage import source_work_window_get as _psf3c3_window_get
except Exception:
    _psf3c3_window_get = None  # type: ignore[assignment]


def _psf3c3_source_cfg(buyer):
    try:
        sk = _norm_key((buyer or {}).get("source_key") or "")
        if not sk or not callable(_psf3c3_window_get):
            return None
        return _psf3c3_window_get(sk, TPILOT_DB_PATH)
    except Exception:
        return None


def _psf3c3_window_cfg(cfg):
    if not cfg:
        return None
    try:
        return {"day_start_min": int(cfg.get("day_start")), "day_end_min": int(cfg.get("day_end"))}
    except Exception:
        return None


def _psf3c3_pro_period_mode(cfg):
    if cfg and int(cfg.get("pro_include_dolyoty") or 0) == 0:
        return "calendar_day"
    return None
# --- TPILOT STAGE C3: Resolve source-level window/period config 20260621 END ---


# --- TPILOT STAGE C4: Export window/label helpers 20260621 START ---
# Mirrors stats_engine._se_window_minutes narrowly (not imported, so exports stay
# safe even if stats_engine fails to import). None/invalid/overnight window_cfg ->
# default 480/1020 (08:00/17:00) -> byte-identical to pre-Stage-C export windows.
def _psf3c3_minutes(window_cfg):
    if isinstance(window_cfg, dict):
        try:
            ds = int(window_cfg.get("day_start_min"))
            de = int(window_cfg.get("day_end_min"))
            if 0 <= ds < de <= 1439:
                return ds, de
        except Exception:
            pass
    return 480, 1020


def _psf3c3_hhmm(total_minutes):
    h, m = divmod(int(total_minutes) % 1440, 60)
    return f"{h:02d}:{m:02d}"
# --- TPILOT STAGE C4: Export window/label helpers 20260621 END ---


# --- TPILOT STAGE C-add-3: On-screen label text helpers 20260621 START ---
# Screen-only presentation text (Pro day / flights labels, Both-mode header).
# Reuses C4's _psf3c3_minutes/_psf3c3_hhmm so screen text matches export text.
# None/missing args -> current defaults (08:00-17:00 / 17:00-08:00), byte-identical.
def _psf3c3_day_window_text(window_cfg=None, period_mode=None):
    if str(period_mode or "") == "calendar_day":
        return "00:00-00:00"
    ds, de = _psf3c3_minutes(window_cfg)
    return f"{_psf3c3_hhmm(ds)}-{_psf3c3_hhmm(de)}"


def _psf3c3_flight_window_text(window_cfg=None):
    ds, de = _psf3c3_minutes(window_cfg)
    return f"{_psf3c3_hhmm(de)}-{_psf3c3_hhmm(ds)}"
# --- TPILOT STAGE C-add-3: On-screen label text helpers 20260621 END ---


def _format_stats_pro_for_buyer(user_id, token="today"):  # type: ignore[override]
    buyer, managers, start, end, err = _psf3_buyer_context(int(user_id), token)
    if err:
        return err
    _s, _e, label = _parse_date_token(token)
    drop = not _buyer_show_duplicates(buyer)
    cfg = _psf3c3_source_cfg(buyer)
    body = _psf3_pro_body(
        managers, start, end, drop_duplicates=drop,
        window_cfg=_psf3c3_window_cfg(cfg), period_mode=_psf3c3_pro_period_mode(cfg),
    )
    return "\n".join([
        _tp_pdf_label(start, end, label, "day", window_cfg=_psf3c3_window_cfg(cfg), period_mode=_psf3c3_pro_period_mode(cfg)),
        "", body,
    ]).rstrip()


def _format_flights_pro_for_buyer(user_id, token="today"):
    mode = _psf3_mode(int(user_id))
    if mode == "light":
        return "🌙 Долёты доступны только в PRO-формате."
    buyer, managers, start, end, err = _psf3_buyer_context(int(user_id), token)
    if err:
        return err
    cfg = _psf3c3_source_cfg(buyer)
    if cfg and int(cfg.get("pro_include_dolyoty") or 0) == 0:
        return "🌙 Долёты отключены для вашего источника."
    _s, _e, label = _parse_date_token(token)
    drop = not _buyer_show_duplicates(buyer)
    return "\n".join([
        _tp_pdf_label(start, end, label, "flight", window_cfg=_psf3c3_window_cfg(cfg)), "",
        _tp_pdf_flight_body(managers, start, end, drop_duplicates=drop, window_cfg=_psf3c3_window_cfg(cfg)),
    ]).rstrip()


def _format_stats_for_buyer(user_id, token="today"):  # type: ignore[override]
    mode = _psf3_mode(int(user_id))
    if mode == "light":
        return _format_stats_light_for_buyer(int(user_id), token)
    if mode == "both":
        buyer, managers, start, end, err = _psf3_buyer_context(int(user_id), token)
        if err:
            return err
        _s, _e, label = _parse_date_token(token)
        drop = not _buyer_show_duplicates(buyer)
        cfg = _psf3c3_source_cfg(buyer)
        return "\n".join([
            _psf3_date_title(start, end, label),
            "",
            "LIGHT, сутки",
            "",
            _psf3_light_body(managers, start, end, drop_duplicates=drop),
            "",
            f"PRO, день {_psf3c3_day_window_text(_psf3c3_window_cfg(cfg), _psf3c3_pro_period_mode(cfg))}",
            "",
            _psf3_pro_body(
                managers, start, end, drop_duplicates=drop,
                window_cfg=_psf3c3_window_cfg(cfg), period_mode=_psf3c3_pro_period_mode(cfg),
            ),
        ]).rstrip()
    return _format_stats_pro_for_buyer(int(user_id), token)


def _tp_pdf_collect_source_windowed(sk, start, end, kind="day", window_cfg=None):
    out = []
    for row in _manager_rows_for_source(sk):
        out.extend(_tp_pdf_windowed_leads_for_manager(row, start, end, kind, window_cfg=window_cfg))
    out.sort(key=lambda x: (str(x.get("first_seen_utc") or ""), str(x.get("manager_key") or ""), int(x.get("id") or 0)))
    return out


# --- TPILOT PARTNER EXCEL DUPLICATE FILTER (Option C+) 20260713 START ------
# _tp_pdf_export_xlsx previously exported ALL countable rows regardless of
# show_duplicates, so a buyer with duplicates hidden could still see
# duplicate-like rows (duplicate=1+lead_countable=1, or contact_kind in
# duplicate/returning/old_baseline, or same-source identity dupes) in the
# Excel file even though the SAME rows are already excluded from that
# buyer's text stats totals (drop_duplicates=not show_duplicates ->
# se_is_buyer_duplicate). This reuses the EXISTING predicates (no new
# duplicate logic) so Excel and totals agree for a given buyer. Only
# PartnerBot Excel is touched -- stats_engine.py/main.py/manager_bot.py/
# panel_bot.py, text stats, the duplicate section, and live push are
# untouched.

def _tp_pdf_leads_for_export(buyer, leads):
    if not _buyer_show_duplicates(buyer):
        return [l for l in (leads or []) if not _psf3_is_buyer_duplicate(l)]
    return list(leads or [])

# --- TPILOT PARTNER EXCEL DUPLICATE FILTER (Option C+) 20260713 END --------


def _tp_pdf_export_xlsx(user_id, token, kind="day"):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment
        from openpyxl.utils import get_column_letter
    except Exception as e:
        raise RuntimeError("openpyxl не установлен") from e
    buyer = _buyer(int(user_id))
    if not buyer or int((buyer or {}).get("can_excel") or 0) != 1:
        raise RuntimeError("Excel для вашего доступа выключен")
    mode = _psf3_mode(int(user_id))
    if str(kind).lower() == "flight" and mode == "light":
        raise RuntimeError("Excel долётов доступен только в PRO-формате")
    sk = _norm_key((buyer or {}).get("source_key") or "")
    if not sk:
        raise RuntimeError("Источник для вашего доступа ещё не назначен")
    start, end, label = _parse_date_token(token)
    kind_l = str(kind).lower()
    cfg = _psf3c3_source_cfg(buyer)
    window_cfg = _psf3c3_window_cfg(cfg)
    if kind_l == "flight" and cfg and int(cfg.get("pro_include_dolyoty") or 0) == 0:
        raise RuntimeError("Долёты отключены для вашего источника — экспорт недоступен")
    period_mode = _psf3c3_pro_period_mode(cfg)
    if mode == "light" and kind_l == "day":
        leads = _partner_nonduplicate_leads(_collect_source_leads(sk, start, end))
        window_label = "сутки"
        prefix = "partner_light"
    elif kind_l == "day" and period_mode == "calendar_day":
        leads = _partner_nonduplicate_leads(_collect_source_leads(sk, start, end))
        window_label = "00:00-00:00"
        prefix = "partner_day"
    else:
        leads = _partner_nonduplicate_leads(_tp_pdf_collect_source_windowed(sk, start, end, kind, window_cfg=window_cfg))
        ds_min, de_min = _psf3c3_minutes(window_cfg)
        window_label = f"{_psf3c3_hhmm(de_min)}-{_psf3c3_hhmm(ds_min)}" if kind_l == "flight" else f"{_psf3c3_hhmm(ds_min)}-{_psf3c3_hhmm(de_min)}"
        prefix = "partner_flights" if kind_l == "flight" else "partner_day"
    leads = _tp_pdf_leads_for_export(buyer, leads)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    safe_label = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(label or "period")).strip("_") or "period"
    out = os.path.join(EXPORT_DIR, f"{prefix}_{sk}_{safe_label}.xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "flights" if str(kind).lower() == "flight" else "leads"
    headers = ["№", "Дата", "Время", "Окно", "Источник", "manager_key", "Аккаунт менеджера", "chat_id", "username", "Имя аккаунта", "Телефон", "Дубликат", "Возраст", "Город", "Регион", "Страна", "Статус", "Причина неликвида", "Источник гео", "Точность", "Пометка", "Режим менеджера", "Вопрос отправлен", "Offline-сообщение", "UA текст", "Менеджер ответил", "Тип контакта", "Считать как новый", "Причина"]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center")
    can_contacts = int((buyer or {}).get("can_view_contacts") or 0) == 1
    source_display = _source_name(sk)
    for idx, r in enumerate(leads, 1):
        dt = _tp_pdf_local_dt(r) or _lead_dt(r)
        manager_username = str(r.get("manager_username") or "").strip()
        manager_key = str(r.get("manager_key") or "").strip()
        manager_account = "@" + manager_username.lstrip("@") if manager_username else (manager_key or "_")
        status_raw = str(r.get("status") or "").strip()
        status_disp = "ликвид" if status_raw == "liquid" else ("неликвид" if status_raw == "nonliquid" else (status_raw or "не определено"))
        username = str(r.get("username") or "").strip()
        ws.append([
            idx, dt.strftime("%d.%m.%Y"), dt.strftime("%H:%M:%S"), window_label, source_display, manager_key, manager_account,
            int(r.get("chat_id") or 0) if can_contacts else "_",
            (username if username.startswith("@") else ("@" + username if username else "_")) if can_contacts else "_",
            (str(r.get("full_name") or "").strip() or "_") if can_contacts else "_",
            (str(r.get("phone") or "").strip() or "_") if can_contacts else "_",
            "да" if int(r.get("duplicate") or 0) == 1 else "нет",
            str(r.get("age") if r.get("age") is not None else "_").strip() or "_",
            str(r.get("city") or "").strip() or "_",
            str(r.get("region") or "").strip() or "_",
            str(r.get("country") or "").strip() or "_",
            status_disp, str(r.get("nonliquid_reason") or "").strip() or "_",
            str(r.get("geo_source") or "").strip() or "_",
            str(r.get("geo_confidence") or "").strip() or "_",
            str(r.get("geo_note") or "").strip() or "_",
            str(r.get("manager_work_status") or "").strip() or "_",
            "да" if int(r.get("profile_question_sent") or 0) == 1 else "нет",
            "да" if int(r.get("offline_notice_sent") or 0) == 1 else "нет",
            "да" if int(r.get("ua_text_sent") or 0) == 1 else "нет",
            "да" if int(r.get("manager_replied") or 0) == 1 else "нет",
            str(r.get("contact_kind") or "new"), "да" if _tp_partner_ci_is_countable(r) else "нет", str(r.get("dedupe_reason") or "new_contact"),
        ])
    widths = [6,14,13,14,22,16,24,18,22,28,18,12,10,22,26,18,16,24,18,14,30,18,18,18,12,18,16,18,24]
    for i, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    wb.save(out)
    return out


def _export_xlsx_for_buyer(user_id: int, token: str) -> str:  # type: ignore[override]
    return _tp_pdf_export_xlsx(int(user_id), token, "day")


def _export_flights_xlsx_for_buyer(user_id: int, token: str = "today") -> str:
    return _tp_pdf_export_xlsx(int(user_id), token, "flight")


def _main_buttons(buyer: Dict[str, Any] | None = None):  # type: ignore[override]
    mode = "light"
    try:
        if buyer and int((buyer or {}).get("is_enabled") or 0) == 1:
            mode = _psf3_mode(int((buyer or {}).get("user_id") or 0))
    except Exception:
        mode = "pro"
    if mode == "light":
        rows = [
            [Button.inline("📊 Статистика", b"stats:today"), Button.inline("📅 Вчера", b"stats:yesterday")],
            [Button.inline("📈 Неделя", b"stats:week"), Button.inline("🗓 Месяц", b"stats:month")],
            [Button.inline("📆 Период", b"help:period"), Button.inline("🔁 Дубликаты", b"partner_dup:31d")],
        ]
        if buyer and int((buyer or {}).get("can_excel") or 0) == 1:
            rows.append([Button.inline("📦 Excel", b"excel_menu:main")])
        rows.append([Button.inline("🔄 Обновить", b"menu:main")])
        return rows
    rows = [
        [Button.inline("📊 День", b"stats:today"), Button.inline("🌙 Долёты", b"flights:today")],
        [Button.inline("📅 День вчера", b"stats:yesterday"), Button.inline("🌙 Долёты вчера", b"flights:yesterday")],
        [Button.inline("📈 День 7 дней", b"stats:week"), Button.inline("🌙 Долёты 7 дней", b"flights:week")],
        [Button.inline("🗓 День 31 день", b"stats:month"), Button.inline("🌙 Долёты 31 день", b"flights:month")],
        [Button.inline("📆 День период", b"help:period"), Button.inline("🌙 Долёты период", b"help:flight_period")],
        [Button.inline("🔁 Дубликаты", b"partner_dup:31d")],
    ]
    if buyer and int((buyer or {}).get("can_excel") or 0) == 1:
        rows.append([Button.inline("📦 Excel день", b"excel_menu:main"), Button.inline("🌙 Excel долёты", b"excel_menu:flight")])
    rows.append([Button.inline("🔄 Обновить", b"menu:main")])
    return rows


def _partner_excel_menu_buttons():  # type: ignore[override]
    return [
        [Button.inline("📦 Excel день сегодня", b"excel:today"), Button.inline("📅 Excel день вчера", b"excel:yesterday")],
        [Button.inline("📈 Excel день 7 дней", b"excel:week"), Button.inline("🗓 Excel день 31 день", b"excel:month")],
        [Button.inline("📅 Excel день период", b"excel_menu:period")],
        [Button.inline("🌙 Excel долёты сегодня", b"excel_flight:today"), Button.inline("🌙 Excel долёты вчера", b"excel_flight:yesterday")],
        [Button.inline("🌙 Долёты 7 дней", b"excel_flight:week"), Button.inline("🌙 Долёты 31 день", b"excel_flight:month")],
        [Button.inline("📅 Долёты период", b"excel_menu:flight_period")],
        [Button.inline("🔁 Дубли 31 день", b"partner_dup_excel:31d"), Button.inline("📅 Дубли за период", b"excel_menu:dup_period")],
        [Button.inline("⬅️ Назад", b"menu:main")],
    ]


def _partner_excel_menu_text(user_id: int) -> str:  # type: ignore[override]
    buyer = _buyer(int(user_id))
    src = _source_name(str((buyer or {}).get("source_key") or ""))
    mode = _psf3_mode(int(user_id))
    extra = "LIGHT: Excel считается за сутки." if mode == "light" else "PRO: Excel день 08:00-17:00, Excel долёты 17:00-08:00."
    return "\n".join(["📦 Excel", "", f"Источник: {src}", "", extra, "Обычный Excel содержит только новые уникальные контакты.", "Дубли вынесены отдельно."]).rstrip()


def _partner_period_prompt(kind: str) -> str:  # type: ignore[override]
    titles = {
        "data": "📅 День за период",
        "excel": "📦 Excel день за период",
        "flight": "🌙 Долёты за период",
        "flight_excel": "🌙 Excel долёты за период",
        "dup": "🔁 Дубликаты за период",
        "dup_excel": "📦 Excel дублей за период",
    }
    return "\n".join([titles.get(str(kind or ""), "📅 Период"), "", "Введите даты одним сообщением:", "", "`01.05.26 07.05.26`", "", "Для долётов дата означает утро закрытия окна."]).rstrip()


async def _partner_start_period_wizard(event, kind: str) -> None:  # type: ignore[override]
    uid = int(getattr(event, "sender_id", 0) or 0)
    if kind not in ("data", "excel", "flight", "flight_excel", "dup", "dup_excel"):
        await event.answer("Неизвестный период", alert=True)
        return
    if kind in ("flight", "flight_excel") and _psf3_mode(uid) == "light":
        await event.answer("Долёты доступны только в PRO-формате", alert=True)
        return
    _TPILOT_PARTNER_PERIOD_STATE[uid] = kind
    try:
        await event.edit(_partner_period_prompt(kind), buttons=_partner_period_cancel_buttons())
    except Exception:
        await client.send_message(int(event.chat_id), _partner_period_prompt(kind), buttons=_partner_period_cancel_buttons())
    try:
        await event.answer("Введите даты сообщением")
    except Exception:
        pass


async def _partner_handle_period_wizard(event, uid: int, text: str) -> bool:  # type: ignore[override]
    kind = str(_TPILOT_PARTNER_PERIOD_STATE.get(int(uid)) or "")
    if not kind:
        return False
    raw = str(text or "").strip()
    if not raw:
        return True
    if raw.startswith("/"):
        _TPILOT_PARTNER_PERIOD_STATE.pop(int(uid), None)
        return False
    token = _partner_extract_two_dates(raw)
    if not token:
        await client.send_message(int(event.chat_id), "⚠️ Введите две даты одним сообщением.\n\nПример: `01.05.26 07.05.26`", buttons=_partner_period_cancel_buttons())
        return True
    _TPILOT_PARTNER_PERIOD_STATE.pop(int(uid), None)
    buyer = _buyer(int(uid))
    try:
        if kind == "data":
            await client.send_message(int(event.chat_id), _format_stats_for_buyer(int(uid), token), buttons=_main_buttons(buyer), parse_mode='html')
            return True
        if kind == "flight":
            await client.send_message(int(event.chat_id), _format_flights_pro_for_buyer(int(uid), token), buttons=_main_buttons(buyer))
            return True
        if kind == "excel":
            path = _export_xlsx_for_buyer(int(uid), token)
            await client.send_file(int(event.chat_id), path, caption="📦 Excel день по вашему источнику", buttons=_main_buttons(buyer))
            return True
        if kind == "flight_excel":
            path = _export_flights_xlsx_for_buyer(int(uid), token)
            await client.send_file(int(event.chat_id), path, caption="🌙 Excel долёты по вашему источнику", buttons=_main_buttons(buyer))
            return True
        if kind == "dup":
            await client.send_message(int(event.chat_id), _format_partner_duplicates_for_buyer(int(uid), token), buttons=_main_buttons(buyer))
            return True
        if kind == "dup_excel":
            path = _export_partner_duplicates_xlsx(int(uid), token)
            await client.send_file(int(event.chat_id), path, caption="📦 Excel дублей по вашему источнику", buttons=_main_buttons(buyer))
            return True
    except Exception as e:
        await client.send_message(int(event.chat_id), _partner_xlsx_err_text(e), buttons=_main_buttons(buyer))
        return True
    return True


@client.on(events.CallbackQuery)
async def _tp_pdf_partner_day_flight_callback(event):
    try:
        uid = int(event.sender_id or 0)
        data = bytes(event.data or b"").decode("utf-8", errors="ignore")
        buyer = _buyer(uid)
        if data.startswith("flights:"):
            token = data.split(":", 1)[1] or "today"
            await event.edit(_format_flights_pro_for_buyer(uid, token), buttons=_main_buttons(buyer))
            return
        if data == "help:flight_period":
            await _partner_start_period_wizard(event, "flight")
            return
        if data == "excel_menu:flight":
            if not buyer or int((buyer or {}).get("can_excel") or 0) != 1:
                await event.answer("Excel для вашего доступа выключен", alert=True)
                return
            await event.edit(_partner_excel_menu_text(uid), buttons=_partner_excel_menu_buttons(buyer=buyer))
            return
        if data == "excel_menu:flight_period":
            await _partner_start_period_wizard(event, "flight_excel")
            return
        if data.startswith("excel_flight:"):
            token = data.split(":", 1)[1] or "today"
            try:
                path = _export_flights_xlsx_for_buyer(uid, token)
                await client.send_file(int(event.chat_id), path, caption="🌙 Excel долёты по вашему источнику")
                await event.answer("Excel долётов отправлен")
                await _partner_edit_or_replace_menu(event, uid)
            except Exception as e:
                await event.answer(str(e), alert=True)
            return
    except Exception as e:
        try:
            await event.answer(f"Ошибка: {e!r}", alert=True)
        except Exception:
            pass

# --- TPILOT PARTNER DAY/FLIGHT PRO SEPARATION V1 20260512 END ---

# --- TPILOT STATUS BADGE V1 START ---
# Adds a live TPilot 🟢/🔴 badge to partner-facing messages.
# Source: runtime/soft_status.json (written by soft_watchdog_pinger.py every INTERVAL_SEC).
# Green only when: file exists, ok=true, time_utc within 180 seconds.
# Never exposes reasons, duplicate_warnings, process counts or internal diagnostics.

_TPILOT_BADGE_VERSION = "tpilot_status_badge_v1"

# Guard: set True while _partner_menu_payload wrapper is composing its text so that
# the inner stats/flights/excel wrappers skip the badge (it is already at the top).
_TP_BADGE_MENU_ACTIVE = False


def _tpilot_status_badge() -> str:
    """Return 'TPilot \U0001f7e2' (green) or 'TPilot \U0001f534' (red).

    Reads runtime/soft_status.json written by soft_watchdog_pinger.py.
    Returns green only when: file exists, ok=true, time_utc within 180 seconds.
    Never raises; any read/parse/clock failure returns red.
    """
    try:
        import json as _json
        from datetime import datetime, timezone
        _p = BASE_DIR / "runtime" / "soft_status.json"
        if not _p.exists():
            return "TPilot \U0001f534"
        _data = _json.loads(_p.read_text(encoding="utf-8", errors="ignore"))
        if not isinstance(_data, dict) or not _data.get("ok"):
            return "TPilot \U0001f534"
        _raw_ts = str(_data.get("time_utc") or "")
        if not _raw_ts:
            return "TPilot \U0001f534"
        _ts = datetime.fromisoformat(_raw_ts.replace("Z", "+00:00"))
        if _ts.tzinfo is None:
            _ts = _ts.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - _ts).total_seconds() > 180:
            return "TPilot \U0001f534"
        return "TPilot \U0001f7e2"
    except Exception:
        return "TPilot \U0001f534"


def _partner_xlsx_err_text(exc) -> str:
    """Persistent Excel error message with status badge prepended."""
    return f"{_tpilot_status_badge()}\n\n\u26a0\ufe0f {exc}"


_TP_BADGE_ORIG_PARTNER_MENU_PAYLOAD = globals().get("_partner_menu_payload")
_TP_BADGE_ORIG_FORMAT_STATS = globals().get("_format_stats_for_buyer")
_TP_BADGE_ORIG_FORMAT_FLIGHTS = globals().get("_format_flights_pro_for_buyer")
_TP_BADGE_ORIG_EXCEL_MENU_TEXT = globals().get("_partner_excel_menu_text")


def _partner_menu_payload(user_id):  # type: ignore[override]
    """Wrap _partner_menu_payload to prepend TPilot status badge at the top.

    Badge is added only when the original returned a real menu (buttons is not None).
    Access-denied responses (buttons=None) are returned unchanged.
    """
    global _TP_BADGE_MENU_ACTIVE
    _TP_BADGE_MENU_ACTIVE = True
    try:
        _result = (
            _TP_BADGE_ORIG_PARTNER_MENU_PAYLOAD(user_id)
            if callable(_TP_BADGE_ORIG_PARTNER_MENU_PAYLOAD)
            else ("", None)
        )
    finally:
        _TP_BADGE_MENU_ACTIVE = False
    _text, _buttons = _result if isinstance(_result, tuple) else (_result, None)
    if _buttons is None:
        # Access denied or error path — return without badge.
        return _text, _buttons
    _badge = _tpilot_status_badge()
    _text = f"{_badge}\n\n{_text}" if _text else _badge
    return _text, _buttons


def _format_stats_for_buyer(user_id, token="today"):  # type: ignore[override]
    """Wrap _format_stats_for_buyer to prepend badge; skip when composing menu."""
    _fn = _TP_BADGE_ORIG_FORMAT_STATS
    _result = _fn(user_id, token) if callable(_fn) else ""
    if _TP_BADGE_MENU_ACTIVE:
        return _result
    _badge = _tpilot_status_badge()
    return f"{_badge}\n\n{_result}" if _result else _badge


def _format_flights_pro_for_buyer(user_id, token="today"):  # type: ignore[override]
    """Wrap _format_flights_pro_for_buyer to prepend badge; skip when composing menu."""
    _fn = _TP_BADGE_ORIG_FORMAT_FLIGHTS
    _result = _fn(user_id, token) if callable(_fn) else ""
    if _TP_BADGE_MENU_ACTIVE:
        return _result
    _badge = _tpilot_status_badge()
    return f"{_badge}\n\n{_result}" if _result else _badge


def _partner_excel_menu_text(user_id: int) -> str:  # type: ignore[override]
    """Wrap _partner_excel_menu_text to prepend badge; skip when composing menu."""
    _fn = _TP_BADGE_ORIG_EXCEL_MENU_TEXT
    _result = _fn(int(user_id)) if callable(_fn) else ""
    if _TP_BADGE_MENU_ACTIVE:
        return _result
    _badge = _tpilot_status_badge()
    return f"{_badge}\n\n{_result}" if _result else _badge

# --- TPILOT STATUS BADGE V1 END ---

# --- TPILOT EXTERNAL WATCHDOG STATUS BADGE V1 START ---
# Prefer EXTERNAL_WATCHDOG_STATUS_URL when set; otherwise keep Stage 1.3 local soft_status logic.

_TP_EXT_BADGE_ORIG_TPILOT_STATUS_BADGE = globals().get("_tpilot_status_badge")


def _read_external_watchdog_status() -> bool | None:
    """Return True/False when external checker responds; None => use local soft_status badge."""
    import urllib.request

    url = (os.getenv("EXTERNAL_WATCHDOG_STATUS_URL") or "").strip()
    if not url:
        return None
    token = (os.getenv("EXTERNAL_WATCHDOG_STATUS_TOKEN") or "").strip()
    try:
        timeout = float((os.getenv("EXTERNAL_WATCHDOG_TIMEOUT_SEC") or "1.5").strip() or "1.5")
    except Exception:
        timeout = 1.5
    try:
        req = urllib.request.Request(url, method="GET")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = (resp.read() or b"").decode("utf-8", errors="ignore")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            return None
        if data.get("ok") is True:
            return True
        if data.get("ok") is False:
            return False
        return None
    except Exception:
        return None


def _tpilot_status_badge() -> str:  # type: ignore[override]
    ext = _read_external_watchdog_status()
    if ext is True:
        return "TPilot \U0001f7e2"
    if ext is False:
        return "TPilot \U0001f534"
    fn = _TP_EXT_BADGE_ORIG_TPILOT_STATUS_BADGE
    return fn() if callable(fn) else "TPilot \U0001f534"

# --- TPILOT EXTERNAL WATCHDOG STATUS BADGE V1 END ---


# --- TPILOT M2.9B2 PARTNER SCREENSHOT VIEWER START ---
# PartnerBot screenshot viewer gated on can_view_screenshots=1.
# Scoped strictly to buyer.source_key. Re-checks buyer on every callback.
# Callbacks: psv:dates, psv:date:{date}, psv:mgr:{date}:{mk},
#            psv:show:{date}:{all|mk}, psv:zip:{date}:{all|mk}

_PSV_ROOT = (BASE_DIR / "runtime" / "screenshots").resolve()
_PSV_EXPORT_ROOT = BASE_DIR / "runtime" / "screenshot_exports"
_PSV_MAX_SHOW = 15  # max files to send in-chat before telling user to ZIP


def _psv_path_is_safe(path: str) -> bool:
    """Return True iff path is non-empty and resolves inside _PSV_ROOT."""
    try:
        if not path:
            return False
        Path(path).resolve().relative_to(_PSV_ROOT)
        return True
    except Exception:
        return False


def _psv_date_label(date_iso: str) -> str:
    """Convert YYYY-MM-DD → DD.MM.YYYY for display. Never shows timezone name."""
    try:
        return date.fromisoformat(date_iso).strftime("%d.%m.%Y")
    except Exception:
        return date_iso


def _psv_upload_day(row: dict) -> "str | None":
    """Return Kyiv local date (YYYY-MM-DD) for a screenshot row, or None."""
    uploaded_at = str(row.get("uploaded_at") or "")
    if uploaded_at:
        try:
            dt_utc = datetime.fromisoformat(uploaded_at).replace(tzinfo=timezone.utc)
            return dt_utc.astimezone(TZ).date().isoformat()
        except Exception:
            pass
    status_date = str(row.get("status_date") or "")
    if status_date:
        try:
            date.fromisoformat(status_date)  # validate
            return status_date
        except Exception:
            pass
    return None


def _psv_last_7_days() -> "list[str]":
    """Return last 7 calendar days (today first) as YYYY-MM-DD strings."""
    today = datetime.now(TZ).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(7)]


def _psv_check_buyer(uid: int) -> "dict | None":
    """Fail-closed buyer check for screenshot access.
    Returns buyer dict if allowed, None otherwise."""
    buyer = _buyer(int(uid))
    if not buyer:
        return None
    if int(buyer.get("is_enabled") or 0) != 1:
        return None
    if int(buyer.get("can_view_screenshots") or 0) != 1:
        return None
    if not str(buyer.get("source_key") or "").strip():
        return None
    return buyer


def _psv_rows_for_date(source_key: str, date_iso: str, manager_key: str = "") -> "list[dict]":
    """Return uploaded screenshot rows for buyer's source_key + Kyiv local date.
    Optionally filter by manager_key. Only safe paths included.
    source_key filter is always applied server-side — never trust callback alone."""
    sk = _norm_key(source_key)
    mk_filter = _norm_key(manager_key)
    if not sk:
        return []
    con = _connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM manager_screenshot_requests "
            "WHERE state='uploaded' "
            "  AND source_key=? "
            "  AND COALESCE(screenshot_path,'') <> '' "
            "  AND COALESCE(file_deleted_at,'') = '' "
            "ORDER BY id ASC",
            (sk,)
        ).fetchall()]
    finally:
        con.close()
    result = []
    for row in rows:
        if _psv_upload_day(row) != date_iso:
            continue
        if mk_filter and _norm_key(row.get("manager_key") or "") != mk_filter:
            continue
        if not _psv_path_is_safe(str(row.get("screenshot_path") or "")):
            continue
        result.append(row)
    return result


def _psv_date_list_text_and_buttons(source_key: str) -> "tuple[str, list]":
    """Build (text, buttons) for the PartnerBot screenshot date list."""
    sk = _norm_key(source_key)
    dates = _psv_last_7_days()
    dates_set = set(dates)
    con = _connect()
    try:
        rows_meta = [dict(r) for r in con.execute(
            "SELECT manager_key, uploaded_at, status_date FROM manager_screenshot_requests "
            "WHERE state='uploaded' AND source_key=? AND COALESCE(screenshot_path,'') <> '' "
            "  AND COALESCE(file_deleted_at,'') = ''",
            (sk,)
        ).fetchall()]
    finally:
        con.close()
    day_counts: dict = {}
    for row in rows_meta:
        day = _psv_upload_day(row)
        if day and day in dates_set:
            day_counts[day] = day_counts.get(day, 0) + 1
    lines = ["📸 Скриншоты", ""]
    for d in dates:
        cnt = day_counts.get(d, 0)
        lines.append(f"📅 {_psv_date_label(d)}: {cnt} шт.")
    text = "\n".join(lines).rstrip()
    buttons = []
    for d in dates:
        buttons.append([Button.inline(f"📅 {_psv_date_label(d)}", f"psv:date:{d}".encode("utf-8"))])
    buttons.append([Button.inline("⬅️ Панель", b"menu:main")])
    return text, buttons


def _psv_date_detail_text_and_buttons(source_key: str, date_iso: str) -> "tuple[str, list]":
    """Build (text, buttons) for the PartnerBot date detail view."""
    label = _psv_date_label(date_iso)
    rows = _psv_rows_for_date(source_key, date_iso)
    by_manager: dict = {}
    for row in rows:
        mk = _norm_key(row.get("manager_key") or "")
        if mk:
            by_manager[mk] = by_manager.get(mk, 0) + 1
    lines = [f"📸 Скриншоты за {label}", "", f"Всего: {len(rows)}"]
    if by_manager:
        lines.append("")
        for mk in sorted(by_manager):
            lines.append(f"{mk}: {by_manager[mk]}")
    text = "\n".join(lines).rstrip()
    buttons = []
    buttons.append([Button.inline("\U0001f5bc Показать все за дату", f"psv:show:{date_iso}:all".encode("utf-8"))])
    buttons.append([Button.inline("\U0001f4e6 Скачать ZIP за дату", f"psv:zip:{date_iso}:all".encode("utf-8"))])
    for mk in sorted(by_manager):
        buttons.append([Button.inline(f"\U0001f464 {mk}", f"psv:mgr:{date_iso}:{mk}".encode("utf-8"))])
    buttons.append([Button.inline("⬅️ Назад", b"psv:dates")])
    return text, buttons


def _psv_manager_detail_text_and_buttons(source_key: str, date_iso: str, manager_key: str) -> "tuple[str, list]":
    """Build (text, buttons) for the PartnerBot per-manager detail view."""
    label = _psv_date_label(date_iso)
    mk = _norm_key(manager_key)
    rows = _psv_rows_for_date(source_key, date_iso, mk)
    lines = [f"📸 Скриншоты за {label}", "", f"Менеджер: {mk}", f"Файлов: {len(rows)}"]
    text = "\n".join(lines).rstrip()
    buttons = [
        [Button.inline(f"\U0001f5bc Показать скрины {mk}", f"psv:show:{date_iso}:{mk}".encode("utf-8"))],
        [Button.inline(f"\U0001f4e6 Скачать ZIP {mk}", f"psv:zip:{date_iso}:{mk}".encode("utf-8"))],
        [Button.inline("⬅️ Назад", f"psv:date:{date_iso}".encode("utf-8"))],
    ]
    return text, buttons


async def _psv_send_files(event, source_key: str, date_iso: str, manager_key: str) -> None:
    """Send screenshot files for a buyer's source+date (optionally manager-filtered)."""
    mk = _norm_key(manager_key)
    rows = _psv_rows_for_date(source_key, date_iso, mk)
    label = _psv_date_label(date_iso)
    if not rows:
        await event.answer("Файлов не найдено", alert=True)
        return
    if len(rows) > _PSV_MAX_SHOW:
        await event.answer(f"Файлов: {len(rows)} — используйте ZIP", alert=True)
        return
    await event.answer()
    cid = int(event.chat_id or 0)
    sent = 0
    for row in rows:
        path = str(row.get("screenshot_path") or "")
        if not path or not _psv_path_is_safe(path) or not os.path.exists(path):
            continue
        row_mk = str(row.get("manager_key") or "")
        rid = int(row.get("id") or 0)
        try:
            await client.send_file(cid, path, caption=f"📸 {label} | {row_mk} | #{rid}")
            sent += 1
        except Exception as exc:
            print(f"psv send file error row_id={rid}: {exc!r}")
    if sent == 0:
        await client.send_message(cid, "❌ Файлы не найдены или удалены")


def _psv_create_zip(source_key: str, date_iso: str, manager_key: str) -> str:
    """Create a ZIP of screenshot files for a buyer's source+date.
    Returns ZIP path. Raises RuntimeError if no files found."""
    import zipfile
    sk = _norm_key(source_key)
    mk = _norm_key(manager_key)
    rows = _psv_rows_for_date(sk, date_iso, mk)
    if not rows:
        raise RuntimeError("Файлов не найдено")
    _PSV_EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    mk_part = f"_{mk}" if mk else "_all"
    sk_safe = re.sub(r"[^a-z0-9_-]+", "", sk)
    zip_name = f"screenshots_partner_{sk_safe}_{date_iso}{mk_part}.zip"
    zip_path = _PSV_EXPORT_ROOT / zip_name
    added = 0
    with zipfile.ZipFile(str(zip_path), "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for i, row in enumerate(rows, 1):
            path = str(row.get("screenshot_path") or "")
            if not path or not _psv_path_is_safe(path) or not os.path.exists(path):
                continue
            ext = Path(path).suffix.lower() or ".jpg"
            mk_safe = _norm_key(str(row.get("manager_key") or "unknown"))
            arcname = f"{mk_safe}_{i:04d}{ext}"
            zf.write(path, arcname)
            added += 1
    if added == 0:
        try:
            os.remove(str(zip_path))
        except Exception:
            pass
        raise RuntimeError("Все файлы удалены или недоступны")
    return str(zip_path)


_PSV_PREV_MAIN_BUTTONS = globals().get("_main_buttons")


def _main_buttons(buyer: "Dict[str, Any] | None" = None):  # type: ignore[override]
    """Override _main_buttons to add 📸 Скриншоты when can_view_screenshots=1."""
    prev = _PSV_PREV_MAIN_BUTTONS
    rows = list(prev(buyer)) if callable(prev) else []
    if buyer and int(buyer.get("can_view_screenshots") or 0) == 1:
        # Insert 📸 Скриншоты before the last row (🔄 Обновить).
        if rows:
            last = rows[-1]
            rows = rows[:-1] + [[Button.inline("📸 Скриншоты", b"psv:dates")]] + [last]
        else:
            rows.append([Button.inline("📸 Скриншоты", b"psv:dates")])
    return rows


@client.on(events.CallbackQuery)
async def _psv_callback(event):
    """PartnerBot screenshot viewer callback handler (psv: prefix).
    Re-checks buyer permissions on every callback (fail-closed)."""
    try:
        uid = int(event.sender_id or 0)
        data = bytes(event.data or b"").decode("utf-8", errors="ignore")
        if not data.startswith("psv:"):
            return

        # Fail-closed buyer check on every callback.
        buyer = _psv_check_buyer(uid)
        if not buyer:
            await event.answer("Доступ к скриншотам не разрешён", alert=True)
            return
        source_key = _norm_key(str(buyer.get("source_key") or ""))
        if not source_key:
            await event.answer("Источник не назначен", alert=True)
            return

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "dates":
            text, buttons = _psv_date_list_text_and_buttons(source_key)
            await event.answer()
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=buttons)
            return

        if action == "date":
            date_iso = parts[2] if len(parts) > 2 else ""
            if not date_iso:
                await event.answer("Пустая дата", alert=True)
                return
            text, buttons = _psv_date_detail_text_and_buttons(source_key, date_iso)
            await event.answer()
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=buttons)
            return

        if action == "mgr":
            date_iso = parts[2] if len(parts) > 2 else ""
            mk_raw = _norm_key(parts[3] if len(parts) > 3 else "")
            if not date_iso or not mk_raw:
                await event.answer("Ошибка параметров", alert=True)
                return
            text, buttons = _psv_manager_detail_text_and_buttons(source_key, date_iso, mk_raw)
            await event.answer()
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=buttons)
            return

        if action == "show":
            date_iso = parts[2] if len(parts) > 2 else ""
            mk_raw = parts[3] if len(parts) > 3 else "all"
            if not date_iso:
                await event.answer("Пустая дата", alert=True)
                return
            manager_key = "" if mk_raw == "all" else _norm_key(mk_raw)
            await _psv_send_files(event, source_key, date_iso, manager_key)
            return

        if action == "zip":
            date_iso = parts[2] if len(parts) > 2 else ""
            mk_raw = parts[3] if len(parts) > 3 else "all"
            if not date_iso:
                await event.answer("Пустая дата", alert=True)
                return
            manager_key = "" if mk_raw == "all" else _norm_key(mk_raw)
            await event.answer("Создаю ZIP...")
            cid = int(event.chat_id or 0)
            zip_path = None
            try:
                zip_path = _psv_create_zip(source_key, date_iso, manager_key)
                label = _psv_date_label(date_iso)
                caption = f"📸 Скриншоты {label}" + (f" | {manager_key}" if manager_key else "")
                await client.send_file(cid, zip_path, caption=caption)
            except RuntimeError as exc:
                await client.send_message(cid, f"❌ {exc}")
            except Exception as exc:
                print(f"psv zip error date={date_iso}: {exc!r}")
                await client.send_message(cid, "❌ Ошибка создания ZIP")
            finally:
                if zip_path:
                    try:
                        if os.path.exists(zip_path):
                            os.remove(zip_path)
                    except Exception as exc:
                        print(f"psv zip cleanup warning: {exc!r}")
            return

        await event.answer()
    except Exception as exc:
        print(f"psv callback error: {exc!r}")
        try:
            await event.answer("Ошибка", alert=True)
        except Exception:
            pass
# --- TPILOT M2.9B2 PARTNER SCREENSHOT VIEWER END ---


# --- TPILOT M2.13D-2 PARTNER BIZLINKS VIEW 20260609 START ---
# Buyer sees business links for managers in their source only (no cross-source leak).
# Access: buyer must be enabled and have a source_key (same gate as stats).
# No new DB column added. Display uses format_bizlinks_grouped from storage.

try:
    from storage import (
        bizlinks_list_for_manager_date as _psbl_links_mgr_date,
        format_bizlinks_grouped as _psbl_fmt_grouped,
        manager_schedule_list_working_on_date as _psbl_sched_working_keys,
        manager_schedule_set_day as _psbl_sched_set_day,
    )
    _PSBL_STORAGE_OK = True
except Exception as _psbl_imp_err:
    _psbl_links_mgr_date = None  # type: ignore[assignment]
    _psbl_fmt_grouped = None  # type: ignore[assignment]
    _psbl_sched_working_keys = None  # type: ignore[assignment]
    _psbl_sched_set_day = None  # type: ignore[assignment]
    _PSBL_STORAGE_OK = False


def _psbl_check_buyer(user_id: int) -> Optional[Dict[str, Any]]:
    """Return buyer dict if enabled with source_key; None otherwise."""
    buyer = _buyer(int(user_id))
    if not buyer:
        return None
    if int(buyer.get("is_enabled") or 0) != 1:
        return None
    if not _norm_key(str(buyer.get("source_key") or "")):
        return None
    return buyer


def _psbl_build_label_map(managers: List[Dict[str, Any]]) -> Dict[str, str]:
    """Build {manager_key -> display_label} for the given manager rows."""
    label_map: Dict[str, str] = {}
    for row in managers:
        mk = _norm_key(str(row.get("manager_key") or ""))
        uname = str(row.get("telegram_username") or "").strip()
        dname = str(row.get("display_name") or "").strip()
        if uname:
            label = "@" + uname.lstrip("@")
        elif dname:
            label = dname
        else:
            label = mk
        if mk:
            label_map[mk] = label
    return label_map


def _psbl_text(user_id: int, target_date_iso: str) -> str:
    """Format business links for this buyer's source managers on target_date_iso."""
    buyer = _psbl_check_buyer(user_id)
    if not buyer:
        return "Доступ не выдан или источник не назначен."
    source_key = _norm_key(str(buyer.get("source_key") or ""))
    managers = _manager_rows_for_source(source_key)
    if not managers:
        return "Источник {} пока не привязан к менеджерам.".format(
            _source_name(source_key)
        )
    # Stage 1: filter by schedule for this date (fail-open on exception)
    if callable(_psbl_sched_working_keys):
        try:
            working_keys = {_norm_key(str(k or "")) for k in _psbl_sched_working_keys(target_date_iso, TPILOT_DB_PATH)}
            managers = [r for r in managers if _norm_key(str(r.get("manager_key") or "")) in working_keys]
        except Exception:
            pass  # fail-open: keep original managers list
    # STAGE D R3B: activated reserves must appear regardless of normal schedule. Merge
    # before the emptiness check below so a schedule-empty day with an active reserve
    # still renders that reserve's links instead of the "no working managers" message.
    managers = _rsva_merge_activated_reserves(managers, source_key, target_date_iso)
    if not managers:
        try:
            date_label = date.fromisoformat(target_date_iso).strftime("%d.%m.%Y")
        except Exception:
            date_label = target_date_iso
        return "\U0001f4c5 На {} по графику нет рабочих менеджеров для источника {}.".format(
            date_label, _source_name(source_key)
        )
    all_rows: List[Dict[str, Any]] = []
    if callable(_psbl_links_mgr_date):
        for row in managers:
            mk = _norm_key(str(row.get("manager_key") or ""))
            if not mk:
                continue
            try:
                rows = _psbl_links_mgr_date(mk, target_date_iso, TPILOT_DB_PATH)
                all_rows.extend(rows)
            except Exception:
                pass
    label_map = _psbl_build_label_map(managers)
    if callable(_psbl_fmt_grouped):
        # Defense-in-depth consistency with the AdminBot readiness-denominator fix:
        # `managers` here is already schedule/source/reserve-filtered above, so this
        # changes nothing observable today (every manager in all_rows is already in
        # this set) -- it just makes the shared formatter's exclusion rule apply
        # identically on both surfaces instead of relying solely on pre-filtering.
        expected_keys = {_norm_key(str(r.get("manager_key") or "")) for r in managers}
        return _psbl_fmt_grouped(target_date_iso, all_rows, label_map, expected_manager_keys=expected_keys)
    return "\U0001f517 Бизнес-ссылки\nОшибка загрузки модуля."


def _psbl_kyiv_tomorrow_iso() -> str:
    return (_kyiv_now() + timedelta(days=1)).date().isoformat()


def _psbl_kyiv_day_after_tomorrow_iso() -> str:
    return (_kyiv_now() + timedelta(days=2)).date().isoformat()


def _psbl_kyiv_today_iso() -> str:
    return _kyiv_now().date().isoformat()


def _psbl_buttons(target_date_iso: str, source_key: str = "") -> list:
    tomorrow = _psbl_kyiv_tomorrow_iso()
    day_after_tomorrow = _psbl_kyiv_day_after_tomorrow_iso()
    today = _psbl_kyiv_today_iso()
    rows = []
    rows.append([
        Button.inline(
            "\U0001f4c5 Завтра",
            "psbl:date:{}".format(tomorrow).encode("utf-8"),
        ),
        Button.inline(
            "\U0001f4c6 Сегодня",
            "psbl:date:{}".format(today).encode("utf-8"),
        ),
    ])
    rows.append([
        Button.inline(
            "\U0001f4c5 Послезавтра",
            "psbl:date:{}".format(day_after_tomorrow).encode("utf-8"),
        ),
    ])
    if source_key and _rsva_linked_pairs(source_key):
        rows.append([
            Button.inline(
                "\U0001f9e9 Активировать резерв",
                "rsva:list:{}".format(target_date_iso).encode("utf-8"),
            )
        ])
    rows.append([Button.inline("⬅️ Назад", b"menu:main")])
    return rows


# M2.13D-2: stacked _main_buttons to add 🔗 Бизнес-ссылки
_PSBL_PREV_MAIN_BUTTONS = globals().get("_main_buttons")


def _main_buttons(buyer: "Dict[str, Any] | None" = None):  # type: ignore[override]
    prev = _PSBL_PREV_MAIN_BUTTONS
    rows = list(prev(buyer)) if callable(prev) else []
    if buyer and int(buyer.get("is_enabled") or 0) == 1 and _norm_key(
        str(buyer.get("source_key") or "")
    ):
        # Insert 🔗 Бизнес-ссылки before the last row (🔄 Обновить)
        btn = [Button.inline("\U0001f517 Бизнес-ссылки", b"psbl:tomorrow")]
        if rows:
            rows = rows[:-1] + [btn] + [rows[-1]]
        else:
            rows.append(btn)
    return rows


@client.on(events.CallbackQuery)
async def _psbl_bizlinks_callback(event):
    """Handle psbl: callbacks for PartnerBot business links view."""
    try:
        uid = int(event.sender_id or 0)
        data = bytes(event.data or b"").decode("utf-8", errors="ignore")
        if not data.startswith("psbl:"):
            return

        buyer = _psbl_check_buyer(uid)
        if not buyer:
            await event.answer("Доступ не разрешён или источник не назначен.", alert=True)
            return

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "tomorrow":
            target = _psbl_kyiv_tomorrow_iso()
        elif action == "today":
            target = _psbl_kyiv_today_iso()
        elif action == "date" and len(parts) > 2:
            target = parts[2]
        else:
            await event.answer()
            return

        text = _psbl_text(uid, target)
        buttons = _psbl_buttons(target, str(buyer.get("source_key") or ""))
        await event.answer()
        try:
            await event.edit(text, buttons=buttons)
        except Exception as e:
            if "not modified" not in str(e).lower():
                await client.send_message(int(event.chat_id), text, buttons=buttons)

    except Exception as exc:
        print(f"psbl_bizlinks_callback error: {exc!r}")
        try:
            await event.answer("Ошибка", alert=True)
        except Exception:
            pass

# --- TPILOT M2.13D-2 PARTNER BIZLINKS VIEW 20260609 END ---


# --- TPILOT STAGE D R2: PARTNER RESERVE ACTIVATION 20260621 START ---
# Lets a buyer activate an admin-prepared reserve manager (Stage D R1 pairing) for their
# own source, from inside the existing business-links view above. PartnerBot never
# creates links itself: it only writes a reserve_activation_events row and then
# polls/reads the resulting bizlinks rows once main.py's _reserve_activation_loop has
# run. No new Telethon logic here; no changes to onboarding, sessions, or stats_engine.

try:
    from storage import (
        reserve_pair_list_for_source as _rsva_pairs_for_source,
        reserve_activation_request as _rsva_request,
        reserve_activation_get as _rsva_get,
        reserve_activation_finish as _rsva_finish,
        reserve_activation_cancel as _rsva_cancel,
        BIZLINKS_DELAY_BETWEEN_LINKS_SEC as _rsva_link_delay_sec,
    )
    _RSVA_STORAGE_OK = True
except Exception as _rsva_imp_err:
    _rsva_pairs_for_source = None  # type: ignore[assignment]
    _rsva_cancel = None  # type: ignore[assignment]
    _rsva_request = None  # type: ignore[assignment]
    _rsva_get = None  # type: ignore[assignment]
    _rsva_finish = None  # type: ignore[assignment]
    _rsva_link_delay_sec = 10  # type: ignore[assignment]
    _RSVA_STORAGE_OK = False


def _rsva_linked_pairs(source_key: str) -> List[Dict[str, Any]]:
    """Return 'linked' reserve pairs for this source (excludes retired ones)."""
    if not callable(_rsva_pairs_for_source):
        return []
    try:
        rows = _rsva_pairs_for_source(_norm_key(source_key), TPILOT_DB_PATH)
    except Exception:
        return []
    return [r for r in (rows or []) if str(r.get("status") or "") == "linked"]


def _rsva_manager_label(manager_key: str) -> str:
    """Resolve a manager_key to @username / display_name / key, same fallback order as
    _psbl_build_label_map, but for a single key that may not be in the buyer's visible
    manager list (the reserve only gets bizlinks rows after activation)."""
    mk = _norm_key(str(manager_key or ""))
    if not mk:
        return manager_key
    con = _connect()
    try:
        row = con.execute(
            "SELECT telegram_username, display_name FROM managers WHERE manager_key=?", (mk,)
        ).fetchone()
    except Exception:
        row = None
    finally:
        con.close()
    if row:
        uname = str(row["telegram_username"] or "").strip()
        dname = str(row["display_name"] or "").strip()
        if uname:
            return "@" + uname.lstrip("@")
        if dname:
            return dname
    return mk


def _rsva_date_label(target_date_iso: str) -> str:
    try:
        return date.fromisoformat(str(target_date_iso)).strftime("%d.%m.%Y")
    except Exception:
        return str(target_date_iso or "")


def _rsva_find_pair(source_key: str, primary_key: str, reserve_key: str) -> Optional[Dict[str, Any]]:
    pk = _norm_key(primary_key)
    rk = _norm_key(reserve_key)
    for row in _rsva_linked_pairs(source_key):
        if _norm_key(str(row.get("primary_key") or "")) == pk and _norm_key(str(row.get("reserve_key") or "")) == rk:
            return row
    return None


def _rsva_active_reserve_keys_for_source_date(source_key: str, date_iso: str) -> set:
    """STAGE D R3B: reserve keys whose activation for this source/date is in-flight or
    done, regardless of normal work schedule. Authority is the activation event + the
    linked pair, not manager_work_schedule_days."""
    sk = _norm_key(str(source_key or ""))
    if not sk or not date_iso:
        return set()
    con = _connect()
    try:
        rows = con.execute(
            """
            SELECT p.reserve_key AS reserve_key
            FROM reserve_activation_events e
            JOIN manager_reserve_pairs p ON p.reserve_key = e.reserve_key
            WHERE e.source_key = ?
              AND e.target_date = ?
              AND e.status IN ('requested', 'creating', 'done')
              AND p.status = 'linked'
            """,
            (sk, str(date_iso)),
        ).fetchall()
    except Exception:
        return set()
    finally:
        con.close()
    return {_norm_key(str(r["reserve_key"] or "")) for r in (rows or [])}


def _rsva_manager_row_for_key(manager_key: str) -> Optional[Dict[str, Any]]:
    mk = _norm_key(str(manager_key or ""))
    if not mk:
        return None
    con = _connect()
    try:
        row = con.execute(
            "SELECT * FROM managers WHERE manager_key=? AND COALESCE(status,'')!='archived'",
            (mk,),
        ).fetchone()
    except Exception:
        return None
    finally:
        con.close()
    return dict(row) if row else None


def _rsva_merge_activated_reserves(managers: List[Dict[str, Any]], source_key: str, date_iso: str) -> List[Dict[str, Any]]:
    """Append activated reserves missing from `managers` without removing/mutating anything
    already present. Schedule-independent: visibility comes from the activation event."""
    active_keys = _rsva_active_reserve_keys_for_source_date(source_key, date_iso)
    if not active_keys:
        return managers
    present_keys = {_norm_key(str(r.get("manager_key") or "")) for r in managers}
    merged = list(managers)
    for rk in active_keys:
        if rk in present_keys:
            continue
        row = _rsva_manager_row_for_key(rk)
        if row:
            merged.append(row)
    return merged


def _rsva_datepick_screen(source_key: str, primary_key: str, reserve_key: str, target_date: str) -> Tuple[str, list]:
    """STAGE D R3C: render the datepick screen as a toggle. Shows '✅ Активировать резерв'
    when this reserve/date is not currently active, or '❌ Убрать резерв на эту дату' when
    it is (per R3B's own active-keys definition, so the toggle always matches what is
    actually visible in stats/business links)."""
    rk_norm = _norm_key(reserve_key)
    try:
        active_keys = _rsva_active_reserve_keys_for_source_date(source_key, target_date)
    except Exception:
        active_keys = set()
    is_active = rk_norm in active_keys
    text = (
        "\U0001f9e9 Активация резерва\n"
        "Заменяется: {}\n"
        "Резерв: {}\n"
        "Дата: {}"
    ).format(
        _rsva_manager_label(primary_key), _rsva_manager_label(reserve_key),
        _rsva_date_label(target_date),
    )
    if is_active:
        action_btn = Button.inline(
            "❌ Убрать резерв на эту дату",
            "rsva:cancel:{}:{}:{}".format(primary_key, reserve_key, target_date).encode("utf-8"),
        )
    else:
        action_btn = Button.inline(
            "✅ Активировать резерв",
            "rsva:confirm:{}:{}:{}".format(primary_key, reserve_key, target_date).encode("utf-8"),
        )
    buttons = [
        [action_btn],
        [Button.inline("⬅️ Назад", "rsva:date:{}:{}".format(primary_key, reserve_key).encode("utf-8"))],
    ]
    return text, buttons


def _rsva_eta_seconds(reserve_key: str, target_date: str) -> int:
    """R4: read-only ETA estimate using the existing per-link delay constant, no new
    counter/timer. Counts created slots out of 15, multiplies the remainder by the
    same BIZLINKS_DELAY_BETWEEN_LINKS_SEC the creation loop already sleeps, and folds
    in any stored flood_wait_seconds. Never writes the DB; falls back to a full
    15-slot estimate if rows can't be read."""
    delay = int(_rsva_link_delay_sec or 10)
    full_estimate = 15 * delay
    if not callable(_psbl_links_mgr_date):
        return full_estimate
    try:
        rows = _psbl_links_mgr_date(reserve_key, target_date, TPILOT_DB_PATH) or []
    except Exception:
        return full_estimate
    created_count = sum(
        1 for r in rows
        if str(r.get("status") or "") == "created" and str(r.get("link_url") or "").strip()
    )
    missing = max(0, 15 - created_count)
    if missing == 0:
        return 0
    base_eta = missing * delay
    flood_eta = 0
    for r in rows:
        try:
            flood_eta = max(flood_eta, int(r.get("flood_wait_seconds") or 0))
        except Exception:
            pass
    return base_eta + flood_eta


def _rsva_fmt_eta(seconds: int) -> str:
    """R4: short Russian ETA label, e.g. 'около 40 сек' / 'около 2 мин 20 сек'."""
    sec = max(0, int(seconds or 0))
    if sec < 60:
        return "около {} сек".format(sec)
    minutes, rem = divmod(sec, 60)
    if rem:
        return "около {} мин {} сек".format(minutes, rem)
    return "около {} мин".format(minutes)


def _rsva_render_result(event: Dict[str, Any]) -> Tuple[str, list]:
    """Shared renderer for the manual check button and the background poll task."""
    status = str(event.get("status") or "")
    reserve_key = _norm_key(str(event.get("reserve_key") or ""))
    target_date = str(event.get("target_date") or "")
    back_btn = [Button.inline("⬅️ Назад", "psbl:date:{}".format(target_date).encode("utf-8"))]

    # STAGE D R3: delivery hardening — if 15 created links already exist (e.g. the
    # controller loop produced them but the event is stuck in requested/creating), render
    # them directly instead of showing "still creating" forever. Read-only check, never
    # queues/creates links here.
    if status != "done" and reserve_key and target_date:
        fallback_rows: List[Dict[str, Any]] = []
        if callable(_psbl_links_mgr_date):
            try:
                fallback_rows = _psbl_links_mgr_date(reserve_key, target_date, TPILOT_DB_PATH)
            except Exception:
                fallback_rows = []
        created_rows = [
            r for r in fallback_rows
            if str(r.get("status") or "") == "created" and str(r.get("link_url") or "").strip()
        ]
        if len(created_rows) >= 15:
            event_id = int(event.get("id") or 0)
            if event_id and callable(_rsva_finish):
                try:
                    _rsva_finish(event_id, "done", db_path=TPILOT_DB_PATH)
                except Exception:
                    pass
            label_map = {reserve_key: _rsva_manager_label(reserve_key)}
            if callable(_psbl_fmt_grouped):
                text = _psbl_fmt_grouped(target_date, fallback_rows, label_map)
            else:
                text = "\U0001f517 Бизнес-ссылки\nОшибка загрузки модуля."
            return text, [back_btn]

    if status == "done":
        rows: List[Dict[str, Any]] = []
        if callable(_psbl_links_mgr_date):
            try:
                rows = _psbl_links_mgr_date(reserve_key, target_date, TPILOT_DB_PATH)
            except Exception:
                rows = []
        label_map = {reserve_key: _rsva_manager_label(reserve_key)}
        if callable(_psbl_fmt_grouped):
            text = _psbl_fmt_grouped(target_date, rows, label_map)
        else:
            text = "\U0001f517 Бизнес-ссылки\nОшибка загрузки модуля."
        return text, [back_btn]

    if status == "failed":
        err = str(event.get("last_error") or "неизвестная ошибка")
        text = "❌ Не удалось создать резервные ссылки: {}".format(err)
        retry_btn = [Button.inline(
            "\U0001f504 Повторить",
            "rsva:datepick:{}:{}:{}".format(
                _norm_key(str(event.get("primary_key") or "")), reserve_key, target_date,
            ).encode("utf-8"),
        )]
        return text, [retry_btn, back_btn]

    # requested / creating
    eta_sec = _rsva_eta_seconds(reserve_key, target_date)
    text = (
        "⏳ Ссылки создаются.\n"
        "Осталось примерно: {}\n"
        "Я пришлю ссылки сюда автоматически, когда они будут готовы."
    ).format(_rsva_fmt_eta(eta_sec))
    check_btn = [Button.inline(
        "\U0001f504 Проверить готовность",
        "rsva:check:{}".format(int(event.get("id") or 0)).encode("utf-8"),
    )]
    return text, [check_btn, back_btn]


async def _rsva_wait_and_update(chat_id: int, message_id: int, event_id: int) -> None:
    """R4: bounded background poll sized to the real link-creation ETA (interval ~6s, total
    capped at 210s — enough for a fresh 15-link batch at the existing per-link delay) so the
    partner does not need to tap the manual check button. Safe to run alongside a manual
    check on the same message — worst case is a harmless duplicate edit. Never raises; on
    reaching the bound without completion, leaves the message with an updated ETA and the
    manual '🔄 Проверить готовность' button as the fallback. Always terminates (fixed bound)."""
    if not callable(_rsva_get):
        return
    interval_sec = 6
    bound_sec = 210
    elapsed = 0
    event: Optional[Dict[str, Any]] = None
    while elapsed < bound_sec:
        await asyncio.sleep(interval_sec)
        elapsed += interval_sec
        try:
            event = _rsva_get(int(event_id))
        except Exception:
            event = None
        if not event:
            return
        status = str(event.get("status") or "")
        reserve_key = _norm_key(str(event.get("reserve_key") or ""))
        target_date = str(event.get("target_date") or "")
        ready = status in ("done", "failed")
        if not ready and reserve_key and target_date:
            ready = _rsva_eta_seconds(reserve_key, target_date) == 0
        if ready:
            text, buttons = _rsva_render_result(event)
            try:
                await client.edit_message(chat_id, message_id, text, buttons=buttons)
            except Exception:
                pass
            return
    # Bound reached without completion: refresh the message with an updated ETA so the
    # partner sees current progress instead of a stale "creating" message forever.
    if event:
        text, buttons = _rsva_render_result(event)
        try:
            await client.edit_message(chat_id, message_id, text, buttons=buttons)
        except Exception:
            pass


@client.on(events.CallbackQuery)
async def _rsva_callback(event):
    """Handle rsva: callbacks for PartnerBot reserve activation (list/pick/confirm/check)."""
    try:
        uid = int(event.sender_id or 0)
        data = bytes(event.data or b"").decode("utf-8", errors="ignore")
        if not data.startswith("rsva:"):
            return

        buyer = _psbl_check_buyer(uid)
        if not buyer:
            await event.answer("Доступ не разрешён или источник не назначен.", alert=True)
            return
        source_key = _norm_key(str(buyer.get("source_key") or ""))

        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "list":
            target_date = parts[2] if len(parts) > 2 else _psbl_kyiv_tomorrow_iso()
            pairs = _rsva_linked_pairs(source_key)
            if not pairs:
                await event.answer("Резервы для вашего источника не настроены.", alert=True)
                return
            rows = []
            for pr in pairs:
                pk = _norm_key(str(pr.get("primary_key") or ""))
                rk = _norm_key(str(pr.get("reserve_key") or ""))
                label = "{} → {}".format(_rsva_manager_label(pk), _rsva_manager_label(rk))
                rows.append([Button.inline(
                    label, "rsva:date:{}:{}".format(pk, rk).encode("utf-8"),
                )])
            rows.append([Button.inline("⬅️ Назад", "psbl:date:{}".format(target_date).encode("utf-8"))])
            text = "\U0001f9e9 Активация резерва\n\nВыберите пару для активации:"
            await event.answer()
            try:
                await event.edit(text, buttons=rows)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=rows)
            return

        # STAGE D R3: explicit date-choice step (Сегодня / Завтра), inserted between pair
        # pick and confirm. The previously inherited bizlinks-view date is no longer used
        # for activation — the partner must choose it here.
        if action == "date":
            if len(parts) < 4:
                await event.answer("Ошибка данных.", alert=True)
                return
            primary_key, reserve_key = parts[2], parts[3]
            pair = _rsva_find_pair(source_key, primary_key, reserve_key)
            if not pair:
                await event.answer("Эта пара резерва недоступна для вашего источника.", alert=True)
                return
            today_iso = _psbl_kyiv_today_iso()
            tomorrow_iso = _psbl_kyiv_tomorrow_iso()
            text = (
                "\U0001f4c5 На какую дату активировать резерв?\n"
                "Заменяется: {}\n"
                "Резерв: {}"
            ).format(_rsva_manager_label(primary_key), _rsva_manager_label(reserve_key))
            buttons = [
                [Button.inline(
                    "\U0001f4c6 Сегодня",
                    "rsva:datepick:{}:{}:{}".format(primary_key, reserve_key, today_iso).encode("utf-8"),
                )],
                [Button.inline(
                    "\U0001f4c5 Завтра",
                    "rsva:datepick:{}:{}:{}".format(primary_key, reserve_key, tomorrow_iso).encode("utf-8"),
                )],
                [Button.inline("⬅️ Назад", "rsva:list:{}".format(today_iso).encode("utf-8"))],
            ]
            await event.answer()
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=buttons)
            return

        if action == "datepick":
            if len(parts) < 5:
                await event.answer("Ошибка данных.", alert=True)
                return
            primary_key, reserve_key, target_date = parts[2], parts[3], parts[4]
            pair = _rsva_find_pair(source_key, primary_key, reserve_key)
            if not pair:
                await event.answer("Эта пара резерва недоступна для вашего источника.", alert=True)
                return
            text, buttons = _rsva_datepick_screen(source_key, primary_key, reserve_key, target_date)
            await event.answer()
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=buttons)
            return

        # STAGE D R3C: deactivate an active reserve activation for this source/reserve/date.
        # Does not touch the pair, bizlinks rows, schedule, or stats/leads — only flips the
        # activation event's status so R3B's visibility union stops including it.
        if action == "cancel":
            if len(parts) < 5:
                await event.answer("Ошибка данных.", alert=True)
                return
            primary_key, reserve_key, target_date = parts[2], parts[3], parts[4]
            pair = _rsva_find_pair(source_key, primary_key, reserve_key)
            if not pair:
                await event.answer("Эта пара резерва недоступна для вашего источника.", alert=True)
                return
            if callable(_rsva_cancel):
                try:
                    cancelled = _rsva_cancel(source_key, reserve_key, target_date, db_path=TPILOT_DB_PATH)
                except Exception:
                    cancelled = False
            else:
                cancelled = False
            # R3C.1 hotfix: also unset the reserve's schedule checkbox for this date, so it
            # disappears from stats/links regardless of whether the event row found anything to
            # cancel above — this self-heals any row left stuck at is_working=1 by R3C.
            if callable(_psbl_sched_set_day):
                try:
                    _psbl_sched_set_day(
                        reserve_key,
                        target_date,
                        0,
                        source="reserve_activation_cancel",
                        updated_by_user_id=uid,
                        updated_by_role="partner_reserve_cancel",
                        db_path=TPILOT_DB_PATH,
                    )
                except Exception:
                    pass
            if cancelled:
                await event.answer("Резерв убран на {}".format(_rsva_date_label(target_date)))
                # PATCH B2: enqueue deactivation notifications (only on actual cancel)
                try:
                    import json as _rsvdeact_j
                    _fn = str(buyer.get("first_name") or "").strip()
                    _ln = str(buyer.get("last_name") or "").strip()
                    _un = str(buyer.get("username") or "").strip()
                    _pname = " ".join([x for x in (_fn, _ln) if x]).strip()
                    if not _pname and _un:
                        _pname = "@" + _un
                    _who = (_pname + " / " + str(uid)) if _pname else str(uid)
                    _time_str = datetime.now(tz=TZ).replace(microsecond=0).strftime("%d.%m.%Y %H:%M")
                    _ekey = "reserve_deactivated:" + reserve_key + ":" + target_date
                    _payload = _rsvdeact_j.dumps({
                        "primary": primary_key,
                        "reserve": reserve_key,
                        "source_key": source_key,
                        "target_date": target_date,
                        "partner_id": uid,
                        "partner_name": _pname,
                        "action": "deactivated",
                    }, ensure_ascii=False)
                    _now = _now_iso()
                    _adm_kind = "reserve_deactivated"
                    _adm_title = "⏹ " + "Резервный аккаунт отключён"
                    _adm_body = (
                        "⏹ " + "Резервный"
                        " аккаунт"
                        " отключён" + "\n\n"
                        + "Основной аккаунт: " + primary_key + "\n"
                        + "Резервный аккаунт: " + reserve_key + "\n"
                        + "Кто отключил: " + _who + "\n"
                        + "Источник действия: PartnerBot / " + (source_key or "—") + "\n"
                        + "Время: " + _time_str
                    )
                    _con = _connect()
                    try:
                        _con.execute("""
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
                        """)
                        _con.execute(
                            "INSERT OR IGNORE INTO manager_bot_events"
                            "(event_key, event_type, manager_key, chat_id, lead_date, daily_lead_id,"
                            " old_status, new_status, payload_json, source, created_at)"
                            " VALUES(?,?,?,0,?,0,'','',?,?,?)",
                            (_ekey, "reserve_deactivated", primary_key, target_date,
                             _payload, "reserve_deactivation", _now),
                        )
                        _con.execute("""
                            CREATE TABLE IF NOT EXISTS panel_notifications(
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                kind TEXT NOT NULL DEFAULT '',
                                title TEXT NOT NULL DEFAULT '',
                                body TEXT NOT NULL DEFAULT '',
                                status TEXT NOT NULL DEFAULT 'new',
                                created_at TEXT NOT NULL DEFAULT '',
                                updated_at TEXT NOT NULL DEFAULT ''
                            )
                        """)
                        _ex = _con.execute(
                            "SELECT 1 FROM panel_notifications WHERE kind=? AND body=?"
                            " AND status IN ('new','running') LIMIT 1",
                            (_adm_kind, _adm_body),
                        ).fetchone()
                        if not _ex:
                            _con.execute(
                                "INSERT INTO panel_notifications"
                                "(kind, title, body, status, created_at, updated_at)"
                                " VALUES(?,?,?,?,?,?)",
                                (_adm_kind, _adm_title, _adm_body, "new", _now, _now),
                            )
                        _con.commit()
                    except Exception as _ne:
                        print("[rsvdeact_notify] db error: " + repr(_ne))
                    finally:
                        _con.close()
                except Exception as _nexc:
                    print("[rsvdeact_notify] notification error: " + repr(_nexc))
                # END PATCH B2
            else:
                await event.answer("Активной активации на эту дату не найдено.", alert=True)
            text, buttons = _rsva_datepick_screen(source_key, primary_key, reserve_key, target_date)
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=buttons)
            return

        if action == "confirm":
            if len(parts) < 5:
                await event.answer("Ошибка данных.", alert=True)
                return
            primary_key, reserve_key, target_date = parts[2], parts[3], parts[4]
            pair = _rsva_find_pair(source_key, primary_key, reserve_key)
            if not pair:
                await event.answer("Эта пара резерва недоступна для вашего источника.", alert=True)
                return
            if not callable(_rsva_request):
                await event.answer("Модуль активации резерва недоступен.", alert=True)
                return
            chat_id = int(event.chat_id)
            try:
                ev_row = _rsva_request(
                    source_key, primary_key, reserve_key, target_date,
                    uid, response_chat_id=chat_id, db_path=TPILOT_DB_PATH,
                )
            except Exception as exc:
                await event.answer("Ошибка создания заявки: {}".format(exc), alert=True)
                return
            event_id = int((ev_row or {}).get("id") or 0)
            ev_status = str((ev_row or {}).get("status") or "")
            eta_sec = _rsva_eta_seconds(reserve_key, target_date)
            if ev_status in ("done", "failed") or eta_sec == 0:
                # R4: links already exist (re-activation / pre-created by an admin batch,
                # or a fast-fail) — render the real result immediately, no "creating" text.
                # _rsva_render_result also auto-finishes the event when >=15 are created.
                text, buttons = _rsva_render_result(ev_row or {})
            else:
                text = (
                    "⏳ TPilot создаёт ссылки.\n"
                    "Осталось примерно: {}\n"
                    "Я пришлю ссылки сюда автоматически, когда они будут готовы."
                ).format(_rsva_fmt_eta(eta_sec))
                buttons = [[Button.inline(
                    "\U0001f504 Проверить готовность",
                    "rsva:check:{}".format(event_id).encode("utf-8"),
                )]]
            await event.answer()
            message_id = None
            try:
                msg = await event.edit(text, buttons=buttons)
                message_id = getattr(msg, "id", None) or getattr(event, "message_id", None)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    sent = await client.send_message(chat_id, text, buttons=buttons)
                    message_id = getattr(sent, "id", None)
            if event_id and message_id and ev_status not in ("done", "failed") and eta_sec > 0:
                try:
                    client.loop.create_task(_rsva_wait_and_update(chat_id, int(message_id), event_id))
                except Exception:
                    pass
            return

        if action == "check":
            if len(parts) < 3:
                await event.answer("Ошибка данных.", alert=True)
                return
            try:
                event_id = int(parts[2])
            except Exception:
                event_id = 0
            ev_row = _rsva_get(event_id) if callable(_rsva_get) else None
            if not ev_row or int(ev_row.get("requested_by_user_id") or 0) != uid:
                await event.answer("Заявка не найдена.", alert=True)
                return
            text, buttons = _rsva_render_result(ev_row)
            await event.answer()
            try:
                await event.edit(text, buttons=buttons)
            except Exception as e:
                if "not modified" not in str(e).lower():
                    await client.send_message(int(event.chat_id), text, buttons=buttons)
            return

        await event.answer()

    except Exception as exc:
        print(f"rsva_callback error: {exc!r}")
        try:
            await event.answer("Ошибка", alert=True)
        except Exception:
            pass
# --- TPILOT STAGE D R2: PARTNER RESERVE ACTIVATION 20260621 END ---


# --- TPILOT M2.14 BUYER DUPLICATE VISIBILITY TOGGLE START ---
# Buyer-level show_duplicates=0: duplicates are invisible in all buyer-facing surfaces.
# Admin/internal stats, ManagerBot, dedupe logic, lead_countable: all untouched.

_M214_DUP_DISABLED_MSG = "\U0001f501 Дубликаты\n\nПоказ дублей отключён администратором."

_M214_ORIG_FORMAT_DUPS = globals().get("_format_partner_duplicates_for_buyer")
_M214_ORIG_EXPORT_DUPS = globals().get("_export_partner_duplicates_xlsx")
_M214_ORIG_MAIN_BUTTONS = globals().get("_main_buttons")
_M214_ORIG_EXCEL_MENU_BUTTONS = globals().get("_partner_excel_menu_buttons")


def _format_partner_duplicates_for_buyer(user_id: int, token: str = "31d") -> str:  # type: ignore[override]
    buyer = _buyer(int(user_id))
    if not _buyer_show_duplicates(buyer):
        return _M214_DUP_DISABLED_MSG
    fn = _M214_ORIG_FORMAT_DUPS
    return fn(int(user_id), token) if callable(fn) else "Раздел дублей недоступен."


def _export_partner_duplicates_xlsx(user_id: int, token: str = "31d") -> str:  # type: ignore[override]
    buyer = _buyer(int(user_id))
    if not _buyer_show_duplicates(buyer):
        raise RuntimeError("Показ дублей отключён администратором.")
    fn = _M214_ORIG_EXPORT_DUPS
    if not callable(fn):
        raise RuntimeError("Excel дублей недоступен.")
    return fn(int(user_id), token)


def _main_buttons(buyer=None):  # type: ignore[override]
    fn = _M214_ORIG_MAIN_BUTTONS
    rows = fn(buyer) if callable(fn) else []
    if _buyer_show_duplicates(buyer):
        return rows
    filtered = []
    for row in (rows or []):
        new_row = []
        for btn in (row or []):
            data = getattr(btn, "data", None) or b""
            if b"partner_dup" not in data and b"dup:" not in data:
                new_row.append(btn)
        if new_row:
            filtered.append(new_row)
    return filtered


def _partner_excel_menu_buttons(buyer=None):  # type: ignore[override]
    fn = _M214_ORIG_EXCEL_MENU_BUTTONS
    rows = fn() if callable(fn) else []
    if _buyer_show_duplicates(buyer):
        return rows
    filtered = []
    for row in (rows or []):
        new_row = []
        for btn in (row or []):
            data = getattr(btn, "data", None) or b""
            if b"partner_dup" not in data and b"dup_period" not in data and b"dup_excel" not in data and b"dup:" not in data:
                new_row.append(btn)
        if new_row:
            filtered.append(new_row)
    return filtered

# --- TPILOT M2.14 BUYER DUPLICATE VISIBILITY TOGGLE END ---


# --- TPILOT STAGE 4C: Wire PartnerBot stats bodies to stats_engine START ---
# Routes _psf3_light_body / _psf3_pro_body / _tp_pdf_flight_body through the
# canonical stats_engine.  Key effects:
#   - Flight fetch bug fixed: se_window_body("flight") widens lead_date fetch to
#     include D-1, so leads arriving D-1 after 17:00 are no longer silently
#     dropped.  Matches ManagerBot._mbstat_window_body (kind="flight") behaviour.
#   - schedule_aware=False: non-consecutive dolyoty carry is Stage 4F territory.
#   - Buyer identity-dedup hook (_tp_partner_dup_is_identity_duplicate) preserved.
#   - Fail-open: if stats_engine is not importable the original bodies remain.
#   - AdminBot, ManagerBot, Business links, DB schema all untouched.
_SE4C_VERSION = "partner_engine_wire_v1_20260617"

try:
    import stats_engine as _se4c
    _SE4C_OK = True
except Exception:
    _SE4C_OK = False

if _SE4C_OK:
    def _psf3_light_body(managers, start, end, drop_duplicates=False):  # type: ignore[override]
        _idhook = globals().get("_tp_partner_dup_is_identity_duplicate")
        return _se4c.se_light_body(
            managers, start, end,
            drop_duplicates=drop_duplicates,
            identity_hook=_idhook if drop_duplicates and callable(_idhook) else None,
        )

    def _psf3_pro_body(managers, start, end, drop_duplicates=False, window_cfg=None, period_mode=None):  # type: ignore[override]
        _idhook = globals().get("_tp_partner_dup_is_identity_duplicate")
        return _se4c.se_window_body(
            managers, start, end, "day",
            schedule_aware=False,
            drop_duplicates=drop_duplicates,
            identity_hook=_idhook if drop_duplicates and callable(_idhook) else None,
            window_cfg=window_cfg,
            period_mode=period_mode,
        )

    def _tp_pdf_flight_body(managers, start, end, drop_duplicates=False, window_cfg=None):  # type: ignore[override]
        _idhook = globals().get("_tp_partner_dup_is_identity_duplicate")
        return _se4c.se_window_body(
            managers, start, end, "flight",
            schedule_aware=False,
            drop_duplicates=drop_duplicates,
            identity_hook=_idhook if drop_duplicates and callable(_idhook) else None,
            window_cfg=window_cfg,
        )

# --- TPILOT STAGE 4C: Wire PartnerBot stats bodies to stats_engine END ---


# --- TPILOT STAGE 4C+: Fix Excel/export flight fetch (D-1 widening) START ---
# _tp_pdf_windowed_leads_for_manager previously fetched only lead_date=target_date
# for all kinds, causing the flight window [D-1 17:00, D 08:00) to miss leads that
# arrived on D-1 after 17:00.  Stage 4C fixed the UI stats bodies via stats_engine.
# This block fixes the Excel/export path (_tp_pdf_collect_source_windowed) with the
# same D-1 widening logic, matching ManagerBot._mbstat_window_body(kind="flight").
# Day kind behavior is unchanged.  No schedule-aware logic introduced.
_SE4C_PLUS_VERSION = "partner_export_flight_fetch_fix_v1_20260617"


def _tp_pdf_windowed_leads_for_manager(row, start, end, kind="day", window_cfg=None):  # type: ignore[override]
    kind = str(kind or "day").lower()
    fetch_start = (start - timedelta(days=1)) if kind == "flight" else start
    return [lead for lead in _psf3_leads_for_manager(row, fetch_start, end) if _tp_pdf_in_windows(lead, start, end, kind, window_cfg=window_cfg)]

# --- TPILOT STAGE 4C+: Fix Excel/export flight fetch (D-1 widening) END ---


# --- TPILOT STAGE 4E: HTML formatting for PartnerBot PRO daily stats START ---
# Post-processing approach: stats_engine.se_window_body stays plain-text (shared with
# ManagerBot/AdminBot). HTML conversion happens only in partner_stat_bot.py.
# parse_mode='html' added only to stats and menu send/edit calls.
# All dynamic strings escaped via html.escape(s, quote=False).
# Backup: partner_stat_bot.py.bak_htmlfmt_20260625_033702
_PSF3_HTML_FMT_VERSION = "partnerbot_html_fmt_v1_20260625"

import html as _html

_PSF3_DETAIL_HEADINGS = frozenset({"ГЕО", "-18", "NA", "TRASH"})
_PSF3_METRIC_PREFIXES = (
    "ОТПИСОК:", "НЕЛИКВИД:", "ГЕО:", "-18:", "NA:", "TRASH:", "ЛИКВИД:",
)


def _psf3_is_metric_line(s: str) -> bool:
    return any(s.startswith(p) for p in _PSF3_METRIC_PREFIXES)


def _psf3_html_manager_label(label: str) -> str:
    """Bold display name; escape all dynamic parts."""
    label = str(label or "").strip()
    if " | @" in label:
        name_part, rest = label.split(" | @", 1)
        return f"<b>{_html.escape(name_part, quote=False)}</b> | @{_html.escape(rest, quote=False)}"
    if label.startswith("@"):
        return f"<b>{_html.escape(label, quote=False)}</b>"
    return f"<b>{_html.escape(label, quote=False)}</b>"


def _psf3_html_format_header(header_text: str) -> str:
    """📊 День за DD.MM.YYYY → bold date; Окно: ... → italic."""
    lines = str(header_text or "").splitlines()
    out = []
    for line in lines:
        s = line.strip()
        matched = False
        for pfx in ("📊 День за ", "🌙 Долёты за "):
            if s.startswith(pfx):
                date_part = _html.escape(s[len(pfx):], quote=False)
                pfx_safe = _html.escape(pfx, quote=False)
                out.append(f"{pfx_safe}<b>{date_part}</b>")
                matched = True
                break
        if not matched:
            if s.startswith("Окно:"):
                out.append(f"<i>{_html.escape(s, quote=False)}</i>")
            else:
                out.append(_html.escape(s, quote=False))
    return "\n".join(out)


def _psf3_html_format_body(body_text: str) -> str:
    """
    Convert se_window_body plain output to HTML.
    Managers+ИТОГО → wrapped in <blockquote>.
    Manager labels → bold name. ЛИКВИД: N → <u><b>...</b></u>.
    ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ → <b>...</b>.
    Details section → bold headings, blank line before each heading.
    """
    body_text = str(body_text or "").strip()
    detail_marker = "Детализация неликвида"

    if detail_marker in body_text:
        idx = body_text.index(detail_marker)
        managers_raw = body_text[:idx].rstrip()
        details_raw = body_text[idx:]
    else:
        managers_raw = body_text
        details_raw = ""

    # Process manager + total block
    processed_block = []
    for line in managers_raw.splitlines():
        s = line.strip()
        if not s:
            processed_block.append("")
            continue
        if s == "ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ":
            processed_block.append(f"<b>{_html.escape(s, quote=False)}</b>")
            continue
        if s.startswith("ЛИКВИД:"):
            processed_block.append(f"<u><b>{_html.escape(s, quote=False)}</b></u>")
            continue
        if _psf3_is_metric_line(s):
            processed_block.append(_html.escape(s, quote=False))
            continue
        processed_block.append(_psf3_html_manager_label(s))

    block_content = "\n".join(processed_block).strip()
    blockquote = f"<blockquote>{block_content}</blockquote>"

    if not details_raw.strip():
        return blockquote

    # Process details section
    # se_details returns lines without blank lines between sections;
    # blank line before each section heading is added here.
    processed_details: list = []
    for line in details_raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if s == detail_marker:
            processed_details.append(f"<b>{_html.escape(s, quote=False)}</b>")
            continue
        if s in _PSF3_DETAIL_HEADINGS:
            processed_details.append("")
            processed_details.append(f"<b>{_html.escape(s, quote=False)}</b>")
            continue
        processed_details.append(_html.escape(s, quote=False))

    return blockquote + "\n\n" + "\n".join(processed_details)


def _psf3_html_format_pro_report(plain_text: str) -> str:
    """
    Convert _format_stats_pro_for_buyer plain output to HTML.
    Input: "header_lines\\n\\nbody_from_se_window_body"
    Returns HTML string. Falls back to html.escape on any parse error.
    """
    try:
        text = str(plain_text or "").rstrip()
        if not text:
            return text
        parts = text.split("\n\n", 1)
        if len(parts) != 2:
            return _html.escape(text, quote=False)
        html_header = _psf3_html_format_header(parts[0])
        html_body = _psf3_html_format_body(parts[1])
        return f"{html_header}\n\n{html_body}"
    except Exception:
        return _html.escape(str(plain_text or ""), quote=False)


# Override _format_stats_pro_for_buyer to return HTML
_PSF3_HTML_ORIG_FMT_PRO = globals().get("_format_stats_pro_for_buyer")


def _format_stats_pro_for_buyer(user_id, token="today"):  # type: ignore[override]
    fn = _PSF3_HTML_ORIG_FMT_PRO
    plain = fn(user_id, token) if callable(fn) else "Статистика недоступна."
    return _psf3_html_format_pro_report(str(plain or ""))


# Ensure all _format_stats_for_buyer outputs are HTML-safe.
# PRO mode already returns HTML; LIGHT/BOTH/error paths get html.escape.
_PSF3_HTML_ORIG_FMT_STATS = globals().get("_format_stats_for_buyer")


def _format_stats_for_buyer(user_id, token="today"):  # type: ignore[override]
    fn = _PSF3_HTML_ORIG_FMT_STATS
    result = str(fn(user_id, token) if callable(fn) else "")
    if "<blockquote>" in result or "<b>" in result:
        return result  # already HTML (PRO mode)
    return _html.escape(result, quote=False)  # LIGHT/BOTH/error → HTML-safe plain


# HTML-safe _partner_menu_payload: escape source name in assembled text.
_PSF3_HTML_ORIG_MENU_PAYLOAD = globals().get("_partner_menu_payload")


def _partner_menu_payload(user_id):  # type: ignore[override]
    fn = _PSF3_HTML_ORIG_MENU_PAYLOAD
    result = fn(int(user_id)) if callable(fn) else ("", None)
    _text, _buttons = result if isinstance(result, tuple) else (result, None)
    if _buttons is None:
        return _html.escape(str(_text or ""), quote=False), _buttons
    _text = re.sub(
        r"(Ваш источник:\s*)([^\n]*)",
        lambda m: m.group(1) + _html.escape(m.group(2), quote=False),
        str(_text or ""),
        count=1,
    )
    return _text, _buttons


# Override _send_menu to add parse_mode='html'
async def _send_menu(chat_id: int, user_id: int) -> None:  # type: ignore[override]
    """Fresh-send Partner menu with HTML parse mode."""
    cid = int(chat_id)
    await _partner_delete_active_panel(cid)
    text, buttons = _partner_menu_payload(int(user_id))
    msg = await client.send_message(cid, text, buttons=buttons, parse_mode='html')
    try:
        mid = int(getattr(msg, "id", 0) or 0)
        if mid:
            _PARTNER_ACTIVE_PANEL_BY_CHAT[cid] = mid
    except Exception:
        pass


# Override _partner_edit_or_replace_menu to add parse_mode='html'
async def _partner_edit_or_replace_menu(event, user_id: int) -> None:  # type: ignore[override]
    """Edit Partner panel into HTML; replace only if edit fails."""
    cid = int(getattr(event, "chat_id", 0) or 0)
    text, buttons = _partner_menu_payload(int(user_id))
    current_mid = 0
    try:
        current_mid = int(getattr(event, "message_id", 0) or 0)
    except Exception:
        current_mid = 0
    if not current_mid:
        try:
            q = getattr(event, "query", None)
            current_mid = int(getattr(q, "msg_id", 0) or 0)
        except Exception:
            current_mid = 0
    try:
        await event.edit(text, buttons=buttons, parse_mode='html')
        if cid and current_mid:
            _PARTNER_ACTIVE_PANEL_BY_CHAT[cid] = current_mid
        return
    except Exception as e:
        if "MessageNotModified" in type(e).__name__ or "not modified" in str(e).lower():
            try:
                await event.answer("Панель обновлена")
            except Exception:
                pass
            if cid and current_mid:
                _PARTNER_ACTIVE_PANEL_BY_CHAT[cid] = current_mid
            return
    try:
        if cid and current_mid:
            try:
                await client.delete_messages(cid, [current_mid])
            except Exception:
                pass
    finally:
        if cid:
            await _send_menu(cid, int(user_id))

# --- TPILOT STAGE 4E: HTML formatting for PartnerBot PRO daily stats END ---


# --- TPILOT PARTNER STATS UX P1 START ---
# UX/rendering/navigation only. No calculation, window, dolyoty, manager-filtering,
# or DB-schema changes. Every function here is a final additive override that
# captures the previous active definition via globals().get(...) before rebinding
# the name, and chains to it -- exactly the pattern used throughout this file
# (PSV/PSBL/M2.14/Stage 4E). Status badge (_tpilot_status_badge) is untouched;
# only the display of its already-computed text is reformatted.

def _p1_time_hms() -> str:
    fn = globals().get("_psf3_now_hms")
    try:
        if callable(fn):
            return str(fn() or "")
    except Exception:
        pass
    try:
        return _kyiv_now().strftime("%H:%M:%S")
    except Exception:
        return datetime.now().strftime("%H:%M:%S")


# --- 1. Header formatting: bold "TPilot", literal "Partner", preserve the
# runtime-generated 🟢/🔴 indicator exactly as-is. Scoped to the main menu
# payload only (the one screen the target visual describes) so no other
# render path (stats:/flights:/period-wizard messages) is touched.
_P1_BADGE_TITLE_RE = re.compile(
    r"^TPilot\s+(\S+)\n\n🤝 Партнёрская панель статистики\n\n"
)

_P1_ORIG_MENU_PAYLOAD = globals().get("_partner_menu_payload")


def _partner_menu_payload(user_id):  # type: ignore[override]
    fn = _P1_ORIG_MENU_PAYLOAD
    result = fn(int(user_id)) if callable(fn) else ("", None)
    text, buttons = result if isinstance(result, tuple) else (result, None)
    if buttons is None:
        return text, buttons  # access-denied path, unchanged
    text = str(text or "")
    m = _P1_BADGE_TITLE_RE.match(text)
    if m:
        text = _P1_BADGE_TITLE_RE.sub(f"<b>TPilot</b> Partner {m.group(1)}\n\n", text, count=1)
    return text, buttons


# --- 2. Refresh time HH:MM:SS on PRO reports.
_P1_ORIG_FORMAT_STATS_PRO = globals().get("_format_stats_pro_for_buyer")


def _p1_inject_refresh_after_window(text: str) -> str:
    s = str(text or "")
    if not s:
        return s
    header_block = s.split("\n\n", 1)[0]
    if "Обновлено:" in header_block:
        return s  # already present, do not duplicate
    lines = s.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("Окно:") or line.strip().startswith("<i>Окно:"):
            lines.insert(i + 1, f"Обновлено: {_p1_time_hms()}")
            return "\n".join(lines)
    return s  # no window line found; leave unchanged (fail-safe)


def _format_stats_pro_for_buyer(user_id, token="today"):  # type: ignore[override]
    fn = _P1_ORIG_FORMAT_STATS_PRO
    text = str(fn(user_id, token) if callable(fn) else "")
    return _p1_inject_refresh_after_window(text)


_P1_ORIG_FORMAT_FLIGHTS_PRO = globals().get("_format_flights_pro_for_buyer")


def _format_flights_pro_for_buyer(user_id, token="today"):  # type: ignore[override]
    fn = _P1_ORIG_FORMAT_FLIGHTS_PRO
    text = str(fn(user_id, token) if callable(fn) else "")
    if not text:
        return text
    hms = _p1_time_hms()
    token_norm = str(token or "today").strip().lower()
    lines = text.split("\n")
    if token_norm in ("today", "сегодня", ""):
        for i, line in enumerate(lines):
            if line.strip().startswith("🌙 Долёты за"):
                lines[i] = f"🌙 Долёты со вчера на сегодня {hms}"
                return "\n".join(lines)
    return _p1_inject_refresh_after_window(text)


# --- 3. Window line italics: already produced by Stage 4E's HTML formatter for
# PRO day stats (_psf3_html_format_header wraps "Окно:" lines in <i>...</i>).
# Flights render without parse_mode='html' (plain edit, see
# _tp_pdf_partner_day_flight_callback), so italics cannot be applied there
# without switching flights to HTML across every call site (period wizard,
# Excel captions) -- out of scope for this additive UX patch. Skipped safely.

# --- 4. Bold "ЛИКВИД: N": already implemented by Stage 4E's
# _psf3_html_format_body (wraps ЛИКВИД lines in <u><b>...</b></u>) for PRO day
# stats. No change needed. Flights stay plain text for the same reason as
# item 3 above -- skipped safely, no formatter change made here.


# --- 5. Main buttons cleanup: strip old quick-stat rows, add one refresh
# action + dolyoty/history row, keep every optional row (Excel, Duplicates,
# Screenshots, Business links) exactly as currently assembled.
_P1_REMOVED_CALLBACKS = frozenset({
    b"stats:today", b"stats:yesterday", b"stats:week", b"stats:month",
    b"flights:today", b"flights:yesterday", b"flights:week", b"flights:month",
    b"help:period", b"help:flight_period",
    b"menu:main",  # old standalone "🔄 Обновить" row; replaced by the new top button
})

_P1_ORIG_MAIN_BUTTONS = globals().get("_main_buttons")


def _p1_dolyoty_applicable(buyer) -> bool:
    try:
        if not buyer:
            return False
        uid = int((buyer or {}).get("user_id") or 0)
        mode_fn = globals().get("_psf3_mode")
        if callable(mode_fn) and str(mode_fn(uid) or "") == "light":
            return False
        cfg_fn = globals().get("_psf3c3_source_cfg")
        if callable(cfg_fn):
            cfg = cfg_fn(buyer)
            if cfg and int(cfg.get("pro_include_dolyoty") or 0) == 0:
                return False
        return True
    except Exception:
        return False


def _main_buttons(buyer: "Dict[str, Any] | None" = None):  # type: ignore[override]
    prev = _P1_ORIG_MAIN_BUTTONS
    rows = list(prev(buyer)) if callable(prev) else []
    kept_rows = []
    for row in rows:
        new_row = [
            btn for btn in (row or [])
            if bytes(getattr(btn, "data", None) or b"") not in _P1_REMOVED_CALLBACKS
        ]
        if new_row:
            kept_rows.append(new_row)

    primary_rows = [[Button.inline("📊 Статистика за сегодня", b"menu:main")]]
    if _p1_dolyoty_applicable(buyer):
        primary_rows.append([Button.inline("🌙 Долёты со вчера на сегодня", b"flights:today")])
    primary_rows.append([Button.inline("📅 История статистики", b"psc:open")])

    return primary_rows + kept_rows


# --- 6. Temporary history placeholder removed here. It answered "psc:open"
# with a "coming soon" alert; the real calendar now lives in the P2 CALENDAR
# block below and handles "psc:open" itself. Telethon calls every registered
# @client.on(events.CallbackQuery) handler for a matching event (no implicit
# first-match-wins, and this placeholder was registered earlier than the P2
# handler), so leaving it in place would have fired its "coming soon" alert
# on every click alongside the real calendar -- it had to go, not just be
# superseded by name.

# --- TPILOT PARTNER STATS UX P1 END ---


# --- TPILOT PARTNER STATS UX P1 FORMAT FIX START ---
# Corrects P1's day-stats title/refresh formatting: no separate "Обновлено:"
# line; HH:MM:SS lives in the title and only for today; ranges read
# "Период за <b>D1-D2</b>" instead of "День за <b>D1 - D2</b>". Display-only:
# reuses the existing _parse_date_token(token) that already drives the
# underlying stats calculation, so the title always matches the exact date
# range that was actually counted -- no new date logic, no calculation touched.
# Flights/dolyoty keep P1's existing behavior unchanged (out of scope here,
# per explicit priority on day statistics for this fix).

_P1FF_STATS_PRO_HTML_SOURCE = globals().get("_P1_ORIG_FORMAT_STATS_PRO") or globals().get("_format_stats_pro_for_buyer")


def _p1ff_drop_updated_line_from_header(text: str) -> str:
    """Safety net: strip any leftover 'Обновлено:' line from the header block
    (before the first blank line). No-op if absent; blockquote body untouched."""
    parts = text.split("\n\n", 1)
    header_lines = [l for l in parts[0].split("\n") if not l.strip().startswith("Обновлено:")]
    header = "\n".join(header_lines)
    return header if len(parts) == 1 else f"{header}\n\n{parts[1]}"


def _p1ff_day_title_line(start, end) -> str:
    if start != end:
        d1 = start.strftime("%d.%m.%Y")
        d2 = end.strftime("%d.%m.%Y")
        return f"📊 Период за <b>{d1}–{d2}</b>"
    d = start.strftime("%d.%m.%Y")
    try:
        today = _kyiv_now().date()
    except Exception:
        today = None
    if today is not None and start == today:
        return f"📊 День за <b>{d}</b> {_p1_time_hms()}"
    return f"📊 День за <b>{d}</b>"


def _format_stats_pro_for_buyer(user_id, token="today"):  # type: ignore[override]
    fn = _P1FF_STATS_PRO_HTML_SOURCE
    text = str(fn(user_id, token) if callable(fn) else "")
    if not text:
        return text
    text = _p1ff_drop_updated_line_from_header(text)
    lines = text.split("\n")
    if not lines or not lines[0].strip().startswith("📊 День за"):
        return text  # error/access-denied text (not a title line) -- leave untouched
    try:
        start, end, _label = _parse_date_token(token)
    except Exception:
        return text  # fail-safe: keep the original title unchanged
    lines[0] = _p1ff_day_title_line(start, end)
    return "\n".join(lines)

# --- TPILOT PARTNER STATS UX P1 FORMAT FIX END ---


# --- TPILOT PARTNER STATS UX P1 DOLYOTY FORMAT FIX START ---
# Applies the same title principle approved for day statistics to
# flights/dolyoty: no separate "Обновлено:" line; HH:MM:SS only for today;
# range uses an en-dash. Display-only: reuses the existing
# _parse_date_token(token) that already drives the underlying flight
# calculation, so the title always matches the exact date range actually
# counted -- no new date logic, no window recalculation.
#
# HTML tradeoff (audited before writing this block): every render path for
# flights -- the period-wizard branch (client.send_message at the "flight"
# kind) and the flights: callback (event.edit in
# _tp_pdf_partner_day_flight_callback) -- calls send_message/event.edit
# WITHOUT parse_mode='html', and this client (TelegramClient at module load)
# has no client.parse_mode override, so Telethon's default (Markdown) parser
# is what actually processes this text today. Markdown does not interpret
# <b>/<i> tags -- they would render as literal "<b>...</b>" characters, a
# visible regression, not a style upgrade. Enabling parse_mode='html' for
# flights would mean editing two existing call sites outside this additive
# block (touching already-reviewed non-P1 code) and auditing every dynamic
# string reaching them for HTML-escaping (source name, manager labels,
# details) -- real work belonging to its own reviewed patch, not a safe
# drop-in here. Per the explicit fallback allowed for this fix: dolyoty stays
# plain text. Title gets the date/time cleanup; the window line ("Окно:
# HH:MM-HH:MM", the exact value already computed by
# _psf3c3_flight_window_text) is left completely unchanged in content --
# no bold/italic tags, no invented date-qualified window text.

_P1DFF_FLIGHTS_SOURCE = globals().get("_P1_ORIG_FORMAT_FLIGHTS_PRO") or globals().get("_format_flights_pro_for_buyer")


def _p1dff_rewrite_flight_title(text: str, start, end, hms: str) -> str:
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not line.strip().startswith("🌙 Долёты за"):
            continue
        if start != end:
            d1 = start.strftime("%d.%m.%Y")
            d2 = end.strftime("%d.%m.%Y")
            lines[i] = f"🌙 Долёты за {d1}–{d2}"
        else:
            d = start.strftime("%d.%m.%Y")
            is_today = False
            try:
                is_today = (start == _kyiv_now().date())
            except Exception:
                pass
            lines[i] = f"🌙 Долёты за {d} {hms}" if is_today else f"🌙 Долёты за {d}"
        # Defensive: drop a stray "Обновлено:" line between the title and the
        # next blank line (guards the rare fallback path only; the primary
        # source above never inserts one).
        j = i + 1
        while j < len(lines) and lines[j].strip() != "":
            if lines[j].strip().startswith("Обновлено:"):
                lines.pop(j)
                continue
            j += 1
        return "\n".join(lines)
    return text  # disabled/light/error message (no title line) -- unchanged


def _format_flights_pro_for_buyer(user_id, token="today"):  # type: ignore[override]
    fn = _P1DFF_FLIGHTS_SOURCE
    text = str(fn(user_id, token) if callable(fn) else "")
    if not text:
        return text
    try:
        start, end, _label = _parse_date_token(token)
    except Exception:
        return text  # fail-safe: keep the original title unchanged
    return _p1dff_rewrite_flight_title(text, start, end, _p1_time_hms())

# --- TPILOT PARTNER STATS UX P1 DOLYOTY FORMAT FIX END ---


# --- TPILOT PARTNER STATS UX P2 CALENDAR START ---
# "📅 История статистики": a calendar date/range picker for arbitrary periods,
# replacing the old quick-button rows removed in P1. Zero new calculations --
# every report is produced by calling the existing, already-reviewed
# _format_stats_for_buyer / _format_flights_pro_for_buyer with a plain date
# token ("today"/"yesterday"/"week"/"month"/"YYYY-MM-DD"/"YYYY-MM-DD
# YYYY-MM-DD"), exactly the same token grammar _parse_date_token already
# accepts and that already drove the old stats:/flights: quick buttons. State
# is in-memory only (mirrors the existing _TPILOT_PARTNER_PERIOD_STATE
# pattern) -- no DB table, no persistence needed for a navigation widget.

import calendar as _psc_cal_mod

_PSC_STATE: Dict[int, dict] = {}
_PSC_WEEKDAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_PSC_MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


def _psc_fmt_date(iso: str) -> str:
    try:
        return date.fromisoformat(iso).strftime("%d.%m")
    except Exception:
        return iso


def _psc_min_yyyymm() -> str:
    """Earliest selectable month: the one containing today-30 days -- matches
    _parse_date_token's own 30-day range clamp, so the calendar never offers
    a date/range that the existing calculation would silently clamp away."""
    min_date = _kyiv_now().date() - timedelta(days=30)
    return f"{min_date.year:04d}{min_date.month:02d}"


def _psc_dolyoty_applicable(buyer) -> bool:
    fn = globals().get("_p1_dolyoty_applicable")
    try:
        return bool(fn(buyer)) if callable(fn) else False
    except Exception:
        return False


def _psc_text(state: dict) -> str:
    ym = str(state.get("ym") or "")
    try:
        year, month = int(ym[:4]), int(ym[4:6])
    except Exception:
        today0 = _kyiv_now().date()
        year, month = today0.year, today0.month
    month_name = _PSC_MONTH_NAMES[month] if 1 <= month <= 12 else ""
    dates = sorted(state.get("dates") or [])
    if not dates:
        sel_line = "Выбрано: —"
    elif len(dates) == 1:
        sel_line = f"Выбрано: {_psc_fmt_date(dates[0])}"
    else:
        try:
            n = (date.fromisoformat(dates[-1]) - date.fromisoformat(dates[0])).days + 1
        except Exception:
            n = 0
        sel_line = f"Выбрано: {_psc_fmt_date(dates[0])}-{_psc_fmt_date(dates[-1])} · {n} дн."
    styp = str(state.get("styp") or "day")
    type_line = f"Тип отчёта: {'☀️ День' if styp == 'day' else '🌙 Долёты'}"
    return "\n".join([
        "📅 История статистики", "",
        f"Месяц: {month_name} {year}",
        sel_line,
        type_line,
        "",
        "Нажмите на дату (одна = день, две — диапазон; третий клик сбрасывает выбор).",
    ])


def _psc_buttons(buyer, state: dict) -> list:
    ym = str(state.get("ym") or "")
    try:
        year, month = int(ym[:4]), int(ym[4:6])
    except Exception:
        today0 = _kyiv_now().date()
        year, month = today0.year, today0.month
    today = _kyiv_now().date()
    chosen = set(state.get("dates") or [])
    rows = [[Button.inline(d, b"psc:noop") for d in _PSC_WEEKDAY_LABELS]]
    cal = _psc_cal_mod.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(Button.inline(" ", b"psc:noop"))
                continue
            try:
                d_obj = date(year, month, day)
            except Exception:
                row.append(Button.inline(" ", b"psc:noop"))
                continue
            iso = d_obj.isoformat()
            if d_obj > today:
                row.append(Button.inline(".", b"psc:noop"))
            else:
                mark = "✅" if iso in chosen else str(day)
                cb = "psc:d:{:04d}{:02d}{:02d}".format(year, month, day).encode("utf-8")
                row.append(Button.inline(mark, cb))
        rows.append(row)

    prev_y, prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_y, next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    min_ym = _psc_min_yyyymm()
    nav_row = []
    if f"{prev_y:04d}{prev_m:02d}" >= min_ym:
        nav_row.append(Button.inline("⬅️", "psc:cal:{:04d}{:02d}".format(prev_y, prev_m).encode("utf-8")))
    else:
        nav_row.append(Button.inline(".", b"psc:noop"))
    if (year, month) < (today.year, today.month):
        nav_row.append(Button.inline("➡️", "psc:cal:{:04d}{:02d}".format(next_y, next_m).encode("utf-8")))
    else:
        nav_row.append(Button.inline(".", b"psc:noop"))
    rows.append(nav_row)

    styp = str(state.get("styp") or "day")

    if chosen:
        action_emoji = "☀️" if styp == "day" else "🌙"
        rows.append([Button.inline(f"✅ Показать статистику {action_emoji}", b"psc:go")])

    if _psc_dolyoty_applicable(buyer):
        other = "flight" if styp == "day" else "day"
        other_label = "🌙 Долёты" if other == "flight" else "☀️ День"
        rows.append([Button.inline(other_label, f"psc:styp:{other}".encode("utf-8"))])

    if chosen:
        rows.append([Button.inline("♻️ Сбросить", b"psc:reset")])

    rows.append([Button.inline("⬅️ Назад", b"psc:back")])
    return rows


async def _psc_render(event, uid: int, notice: str = "") -> None:
    state = _PSC_STATE.get(uid) or {}
    buyer = _buyer(uid)
    text = _psc_text(state)
    if notice:
        text = f"{notice}\n\n{text}"
    try:
        await event.edit(text, buttons=_psc_buttons(buyer, state))
    except Exception as e:
        if "MessageNotModified" not in type(e).__name__ and "not modified" not in str(e).lower():
            raise


async def _psc_show_report(event, uid: int, token: str, styp: str) -> None:
    buyer = _buyer(uid)
    if styp == "flight" and _psc_dolyoty_applicable(buyer):
        text = _format_flights_pro_for_buyer(uid, token)
        await event.edit(text, buttons=_main_buttons(buyer))
    else:
        text = _format_stats_for_buyer(uid, token)
        await event.edit(text, buttons=_main_buttons(buyer), parse_mode='html')
    # State is intentionally kept (not popped) so a double-click on
    # "✅ Показать статистику" / a preset is idempotent instead of hitting a
    # missing-state alert. State is only cleared on explicit "⬅️ Назад".


@client.on(events.CallbackQuery)
async def _psc_callback(event):
    try:
        data = bytes(event.data or b"").decode("utf-8", errors="ignore")
        if not data.startswith("psc:"):
            return
        uid = int(event.sender_id or 0)
        buyer = _buyer(uid)
        if not buyer or int((buyer or {}).get("is_enabled") or 0) != 1:
            await event.answer("Доступ не выдан или выключен.", alert=True)
            return
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else ""

        if action == "noop":
            await event.answer()
            return

        if action == "open":
            today = _kyiv_now().date()
            _PSC_STATE[uid] = {"ym": f"{today.year:04d}{today.month:02d}", "dates": [], "styp": "day"}
            await event.answer()
            await _psc_render(event, uid)
            return

        state = _PSC_STATE.get(uid)
        if state is None:
            # Self-heal: an old calendar message survived a PartnerBot restart
            # (or the in-memory state otherwise expired) and its buttons are
            # still clickable. Recreate a fresh state instead of dead-ending
            # the user with an error -- any psc:* click just re-opens a clean
            # calendar for the current month.
            today = _kyiv_now().date()
            _PSC_STATE[uid] = {"ym": f"{today.year:04d}{today.month:02d}", "dates": [], "styp": "day"}
            await event.answer()
            await _psc_render(event, uid, notice="Календарь обновлён. Выберите дату.")
            return

        if action == "cal":
            state["ym"] = parts[2] if len(parts) > 2 else state.get("ym", "")
            await event.answer()
            await _psc_render(event, uid)
            return

        if action == "d":
            yyyymmdd = parts[2] if len(parts) > 2 else ""
            try:
                d_obj = datetime.strptime(yyyymmdd, "%Y%m%d").date()
            except Exception:
                await event.answer("Некорректная дата", alert=True)
                return
            if d_obj > _kyiv_now().date():
                await event.answer("Дата ещё не наступила", alert=True)
                return
            iso = d_obj.isoformat()
            dates = list(state.get("dates") or [])
            if iso in dates:
                dates.remove(iso)
            elif len(dates) >= 2:
                dates = [iso]
            else:
                dates.append(iso)
            state["dates"] = dates
            state["ym"] = yyyymmdd[:6]
            await event.answer()
            await _psc_render(event, uid)
            return

        if action == "styp":
            sub = parts[2] if len(parts) > 2 else "day"
            if sub == "flight" and not _psc_dolyoty_applicable(buyer):
                await event.answer("Долёты недоступны для вашего доступа/источника", alert=True)
                return
            state["styp"] = sub if sub in ("day", "flight") else "day"
            await event.answer()
            await _psc_render(event, uid)
            return

        if action == "reset":
            state["dates"] = []
            await event.answer("Выбор очищен")
            await _psc_render(event, uid)
            return

        if action == "go":
            dates = sorted(state.get("dates") or [])
            if not dates:
                await event.answer("Выберите хотя бы одну дату", alert=True)
                return
            token = dates[0] if len(dates) == 1 else f"{dates[0]} {dates[-1]}"
            await event.answer("Формирую отчёт...")
            await _psc_show_report(event, uid, token, str(state.get("styp") or "day"))
            return

        if action == "pre":
            preset = parts[2] if len(parts) > 2 else ""
            if preset not in ("yesterday", "week", "month"):
                await event.answer()
                return
            await event.answer("Формирую отчёт...")
            await _psc_show_report(event, uid, preset, str(state.get("styp") or "day"))
            return

        if action == "back":
            _PSC_STATE.pop(uid, None)
            await event.answer()
            await _partner_edit_or_replace_menu(event, uid)
            return

        await event.answer()
    except Exception as e:
        try:
            await event.answer(f"Ошибка: {e!r}", alert=True)
        except Exception:
            pass

# --- TPILOT PARTNER STATS UX P2 CALENDAR END ---


# --- TPILOT PREFLIGHT (PARTNER, MORNING+EVENING) START ---
# Per-source readiness reports for PartnerBot buyers. Morning (~07:50) checks
# TODAY only; evening (~16:50) checks TOMORROW only. Report content is
# built once per source_key (preflight_check.build_partner_report/
# render_partner_text -- shared with panel_bot.py's admin report, same
# module, no duplicated readiness logic) and sent individually to every
# enabled buyer of that source. A source with nothing relevant for the
# checked date is skipped entirely (report["send_recommended"] is False) --
# no message is sent, per spec ("never send a partner report to a source
# with zero relevant links"). No DB schema changes; the settings table
# already exists (shared central DB) -- _pf_set_setting below only ever
# uses the canonical 2-column (key, value) form, matching panel_bot.py's own
# preflight KV fix, since production's settings table has no updated_at
# column.

try:
    from preflight_check import (
        build_partner_report as _pf_build_partner_report,
        render_partner_text as _pf_render_partner_text,
        apply_deep_results as _pf_apply_deep_results,
        relevant_source_keys_for_date as _pf_relevant_source_keys_for_date,
        report_is_ok as _pf_report_is_ok,
        report_has_recheckable_problem as _pf_report_has_recheckable_problem,
        apply_correction_note as _pf_apply_correction_note,
        CORRECTION_NOTE_PARTNER as _PF_CORRECTION_NOTE_PARTNER,
        CORRECTED_TITLE_ADMIN as _PF_CORRECTED_TITLE,
    )
    _PF_PARTNER_OK = True
except Exception as _pf_partner_import_err:
    _pf_build_partner_report = None  # type: ignore[assignment]
    _pf_render_partner_text = None  # type: ignore[assignment]
    _pf_apply_deep_results = None  # type: ignore[assignment]
    _pf_relevant_source_keys_for_date = None  # type: ignore[assignment]
    _pf_report_is_ok = None  # type: ignore[assignment]
    _pf_report_has_recheckable_problem = None  # type: ignore[assignment]
    _pf_apply_correction_note = None  # type: ignore[assignment]
    _PF_CORRECTION_NOTE_PARTNER = "✅ Готовность перепроверена. Всё готово к работе."
    _PF_CORRECTED_TITLE = "✅ Обновлено: готовность перепроверена"
    _PF_PARTNER_OK = False
    print(f"[preflight-partner] import error: {_pf_partner_import_err!r}")

_PF_MORNING_SEND_HOUR_START = 7
_PF_MORNING_SEND_MINUTE_START = 50
_PF_MORNING_SEND_HOUR_CUTOFF = 12
_PF_EVENING_SEND_HOUR_START = 16
_PF_EVENING_SEND_MINUTE_START = 50
_PF_EVENING_SEND_HOUR_CUTOFF = 20

# 20260713: if the ONLY remaining issue for a source is a transient/pending
# deep-verify result (report_has_recheckable_problem -- timeout/not_checked-
# with-relevant-links, never a real structural problem), defer the partner
# send instead of shipping a technical "не успели пройти проверку" message.
# The 60s tick naturally retries every minute; PanelBot's own auto-recheck
# loop and/or a manual admin check keep refreshing the shared verify blob in
# the meantime. Once the deadline passes, send as-is (report_is_ok's own
# render already uses the neutral partner wording -- see preflight_check.py
# render_partner_text -- so even a still-pending send is never technical).
_PF_MORNING_DEFER_HOUR_DEADLINE = 9
_PF_MORNING_DEFER_MINUTE_DEADLINE = 0
_PF_EVENING_DEFER_HOUR_DEADLINE = 18
_PF_EVENING_DEFER_MINUTE_DEADLINE = 30


def _pf_within_window(now: datetime, hour_start: int, minute_start: int, hour_cutoff: int) -> bool:
    if now.hour < hour_start or (now.hour == hour_start and now.minute < minute_start):
        return False
    if now.hour >= hour_cutoff:
        return False
    return True


def _pf_get_setting(key: str) -> str:
    try:
        con = _connect()
        try:
            row = con.execute("SELECT value FROM settings WHERE key=? LIMIT 1", (str(key),)).fetchone()
            return str(row[0] or "") if row else ""
        finally:
            con.close()
    except Exception:
        return ""


def _pf_set_setting(key: str, value: str) -> None:
    """Preflight-safe KV write: canonical 2-column (key, value) schema only
    (matches panel_bot.py's _pf_set_setting -- production's settings table
    has no updated_at column). No migration -- CREATE TABLE IF NOT EXISTS
    only bootstraps a fresh/local test DB; it is a no-op against the
    production table, which already exists with 2 columns."""
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
        print(f"[preflight-partner] set_setting failed key={key}: {e!r}")


def _pf_verify_blob_key(period: str, target_date_iso: str) -> str:
    return f"preflight_verify_{period}_{target_date_iso}"


def _pf_read_verify_blob(period: str, target_date_iso: str) -> Optional[dict]:
    """Read-only: PartnerBot NEVER enqueues deep verification itself -- it
    only reads the blob PanelBot already wrote (same settings key, same
    canonical 2-column KV). Returns None if absent, unparsable, or for a
    different date (never applies a mismatched date's result)."""
    raw = _pf_get_setting(_pf_verify_blob_key(period, target_date_iso))
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except Exception:
        return None
    if str(blob.get("target_date") or "") != target_date_iso:
        return None
    return blob


# --- AUTO-RECHECK + MESSAGE CORRECTION 20260712 START -----------------------
# PartnerBot NEVER launches deep verification itself (that is PanelBot's
# job) -- it only reacts to whatever blob PanelBot has already (possibly
# freshly) written, on the SAME per-tick rebuild _pf_partner_tick already
# does for every source. If a previously-sent problem report is now OK,
# the stored message for that (period_kind, uid) is corrected in place;
# every key and every message touched is scoped to that single uid, so
# unrelated buyers/sources are never affected.

def _pf_partner_status_key(period_kind: str, date_iso: str, user_id: int) -> str:
    return f"preflight_partner_status_{period_kind}_{date_iso}_{user_id}"


def _pf_partner_resolved_key(period_kind: str, date_iso: str, user_id: int) -> str:
    return f"preflight_partner_resolved_{period_kind}_{date_iso}_{user_id}"


async def _pf_correct_partner_message(user_id: int, prev_mid: int, corrected_text: str, fallback_titled_text: str, buttons) -> tuple:
    """Same edit -> delete+send -> new-message fallback chain as PanelBot's
    _pf_correct_message (panel_bot.py), duplicated here because PartnerBot
    is a separate process/file. Returns (ok, new_mid); new_mid is set only
    when a NEW message was actually sent. Never raises."""
    if prev_mid:
        edited = False
        try:
            await client.edit_message(int(user_id), int(prev_mid), corrected_text, buttons=buttons)
            edited = True
        except Exception as e:
            if e.__class__.__name__ == "MessageNotModifiedError":
                edited = True
            else:
                print(f"[preflight-partner-recheck] edit failed user={user_id}: {e!r}")
        if edited:
            return True, None

        deleted = False
        try:
            await client.delete_messages(int(user_id), [int(prev_mid)])
            deleted = True
        except Exception as e:
            print(f"[preflight-partner-recheck] delete failed user={user_id}: {e!r}")

        if deleted:
            try:
                msg = await client.send_message(int(user_id), corrected_text, buttons=buttons)
                return True, (int(getattr(msg, "id", 0) or 0) or None)
            except Exception as e:
                print(f"[preflight-partner-recheck] send-after-delete failed user={user_id}: {e!r}")
                return False, None

    try:
        msg = await client.send_message(int(user_id), fallback_titled_text, buttons=buttons)
        return True, (int(getattr(msg, "id", 0) or 0) or None)
    except Exception as e:
        print(f"[preflight-partner-recheck] fallback send failed user={user_id}: {e!r}")
        return False, None


async def _pf_partner_maybe_correct(user_id: int, source_key: str, target_date_iso: str, period_kind: str, report: Dict[str, Any]) -> None:
    """Reaction-only correction for one uid. `report` is the SAME source-
    scoped report _pf_partner_tick already rebuilt this tick (fresh deep
    blob applied) -- no extra rebuild here, and no verify is ever
    triggered. Strictly source-scoped: only user_id's own stored message
    and status keys are touched."""
    resolved_key = _pf_partner_resolved_key(period_kind, target_date_iso, user_id)
    status_key = _pf_partner_status_key(period_kind, target_date_iso, user_id)
    if _pf_get_setting(resolved_key) == "1":
        return
    if _pf_get_setting(status_key) != "problem":
        return
    if not (callable(_pf_report_is_ok) and _pf_report_is_ok(report)):
        return  # still not ok -- PartnerBot never retries/forces verify itself

    try:
        text = _pf_render_partner_text(report, period_kind=period_kind)
    except Exception as e:
        print(f"[preflight-partner-recheck] render error source={source_key} user={user_id}: {e!r}")
        return
    corrected = (
        _pf_apply_correction_note(text, _PF_CORRECTION_NOTE_PARTNER)
        if callable(_pf_apply_correction_note) else f"{_PF_CORRECTION_NOTE_PARTNER}\n\n{text}"
    )
    fallback_titled = f"{_PF_CORRECTED_TITLE}\n\n{text}"

    msg_key = f"preflight_msg_partner_{period_kind}_{user_id}"
    prev_ref = _pf_get_setting(msg_key)
    prev_mid = 0
    if prev_ref:
        prev_date, _, prev_mid_s = prev_ref.partition(":")
        if prev_date == target_date_iso and prev_mid_s.strip().isdigit():
            prev_mid = int(prev_mid_s)

    is_evening = period_kind == "tomorrow"
    btn_code = ("pfok:pe" if is_evening else "pfok:pm").encode()
    ok, new_mid = await _pf_correct_partner_message(
        user_id, prev_mid, corrected, fallback_titled,
        [[Button.inline("✅ OK", btn_code)]],
    )
    if not ok:
        return
    if new_mid:
        _pf_set_setting(msg_key, f"{target_date_iso}:{new_mid}")
    _pf_set_setting(status_key, "ok")
    _pf_set_setting(resolved_key, "1")
    print(f"[preflight-partner-recheck] corrected source={source_key} user={user_id}")

# --- AUTO-RECHECK + MESSAGE CORRECTION 20260712 END -------------------------


async def _pf_send_partner_report(user_id: int, target_date_iso: str, period_kind: str, text: str, report: Optional[Dict[str, Any]] = None) -> None:
    """MAX-TWO-READINESS-REPORTS 20260717: retention is SLOT-based
    (period_kind + user_id), never date-based -- the previously stored
    message for THIS slot is always deleted before the new one is sent,
    regardless of whether its stored date matches target_date_iso (a
    same-day re-send must not leave the earlier message orphaned). The
    evening slot (preflight_msg_partner_tomorrow_{user_id}) and the morning
    slot (preflight_msg_partner_today_{user_id}) are independent keys, so a
    morning send can never touch the evening message and vice versa.
    Missing/already-deleted/inaccessible old messages, or any Telegram
    delete error, are swallowed and logged -- never block the new send. The
    slot's stored message_id is overwritten ONLY after the new message is
    confirmed sent (see below); if the send itself fails, the previously
    stored reference is left untouched."""
    is_evening = period_kind == "tomorrow"
    msg_key = f"preflight_msg_partner_{period_kind}_{user_id}"
    prev_ref = _pf_get_setting(msg_key)
    if prev_ref:
        _prev_date, _, prev_mid_s = prev_ref.partition(":")
        if prev_mid_s.strip().isdigit():
            try:
                await client.delete_messages(int(user_id), [int(prev_mid_s)])
            except Exception as e:
                print(f"[preflight-partner] previous {period_kind} message delete failed user={user_id}: {e!r}")

    btn_code = ("pfok:pe" if is_evening else "pfok:pm").encode()
    try:
        msg = await client.send_message(int(user_id), text, buttons=[[Button.inline("✅ OK", btn_code)]])
    except Exception as e:
        print(f"[preflight-partner] send failed user={user_id}: {e!r}")
        return

    try:
        mid = int(getattr(msg, "id", 0) or 0)
        if mid:
            _pf_set_setting(msg_key, f"{target_date_iso}:{mid}")
    except Exception:
        pass
    _pf_set_setting(f"preflight_partner_{period_kind}_sent_{target_date_iso}_{user_id}", "1")
    # AUTO-RECHECK 20260712: record whether this FIRST send was already ok
    # or a problem for this uid, source-scoped via the uid itself (each uid
    # belongs to exactly one source at send time).
    is_ok = bool(callable(_pf_report_is_ok) and report is not None and _pf_report_is_ok(report))
    status_key = _pf_partner_status_key(period_kind, target_date_iso, user_id)
    _pf_set_setting(status_key, "ok" if is_ok else "problem")
    if is_ok:
        _pf_set_setting(_pf_partner_resolved_key(period_kind, target_date_iso, user_id), "1")


async def _pf_partner_tick(period_kind: str) -> None:
    if not _PF_PARTNER_OK:
        return
    now = _kyiv_now()
    if period_kind == "tomorrow":
        target_date_iso = (now.date() + timedelta(days=1)).isoformat()
    else:
        target_date_iso = now.date().isoformat()

    # TPILOT MANAGER REPLACEMENT STAGE5 PARTNER NOTIFY: 7-day ack retention,
    # piggybacked onto the existing morning tick (spec section 7 -- no new
    # high-frequency loop). Self-guarded to run at most once per calendar
    # day; unrelated to the rest of this function's own report-sending flow.
    if period_kind == "today":
        try:
            _arn_run_daily_retention(target_date_iso)
        except Exception as e:
            print(f"[arn] daily retention error: {e!r}")

    try:
        buyers = _enabled_buyers()
    except Exception as e:
        print(f"[preflight-partner] _enabled_buyers error: {e!r}")
        return

    by_source: Dict[str, List[int]] = {}
    for b in buyers:
        sk = _norm_key(str(b.get("source_key") or ""))
        uid = int(b.get("user_id") or 0)
        if sk and uid:
            by_source.setdefault(sk, []).append(uid)

    # Diagnostic-only: sources that ARE active today but have zero enabled
    # buyer targets -- logged so a "why didn't PartnerBot send" question can
    # be answered from logs alone, without touching production data.
    try:
        if callable(_pf_relevant_source_keys_for_date):
            relevant_sks = _pf_relevant_source_keys_for_date(target_date_iso, TPILOT_DB_PATH)
            for sk in sorted(relevant_sks - set(by_source.keys())):
                print(f"[preflight-partner] skip source={sk} reason=no_enabled_buyers")
    except Exception as e:
        print(f"[preflight-partner] relevant_source_keys_for_date error: {e!r}")

    verify_period = "evening" if period_kind == "tomorrow" else "morning"
    for sk, uids in by_source.items():
        try:
            report = _pf_build_partner_report(target_date_iso, TPILOT_DB_PATH, sk)
        except Exception as e:
            print(f"[preflight-partner] build_partner_report error source={sk}: {e!r}")
            continue
        if not report.get("send_recommended"):
            print(f"[preflight-partner] skip source={sk} reason=no_relevant_links")
            continue
        try:
            deep_blob = _pf_read_verify_blob(verify_period, target_date_iso)
            if deep_blob is not None and callable(_pf_apply_deep_results):
                report = _pf_apply_deep_results(report, deep_blob, period_kind=period_kind)
        except Exception as e:
            print(f"[preflight-partner] apply_deep_results error source={sk}: {e!r}")
        try:
            text = _pf_render_partner_text(report, period_kind=period_kind)
        except Exception as e:
            print(f"[preflight-partner] render error source={sk}: {e!r}")
            continue

        # 20260713: if the ONLY remaining issue is a transient/pending deep-
        # verify result (report_has_recheckable_problem -- never a real
        # structural problem), defer the FIRST send for this source until
        # the deadline. Avoids ever showing a not-yet-seen partner a
        # technical "проверка не завершилась" message; the 60s tick retries
        # every minute and PanelBot's own auto-recheck / a manual admin
        # check keep refreshing the shared blob in the meantime. Uids who
        # already have a sent report for this date are NEVER deferred --
        # the existing correction loop below keeps running for them exactly
        # as before, tick by tick, independent of this deadline.
        deferred = False
        if not (callable(_pf_report_is_ok) and _pf_report_is_ok(report)) and callable(_pf_report_has_recheckable_problem) and _pf_report_has_recheckable_problem(report):
            deadline_hour, deadline_minute = (
                (_PF_EVENING_DEFER_HOUR_DEADLINE, _PF_EVENING_DEFER_MINUTE_DEADLINE) if period_kind == "tomorrow"
                else (_PF_MORNING_DEFER_HOUR_DEADLINE, _PF_MORNING_DEFER_MINUTE_DEADLINE)
            )
            deferred = (now.hour < deadline_hour) or (now.hour == deadline_hour and now.minute < deadline_minute)

        for uid in uids:
            try:
                if _pf_get_setting(f"preflight_partner_{period_kind}_sent_{target_date_iso}_{uid}") == "1":
                    # AUTO-RECHECK 20260712: already sent -- react to the
                    # freshly-rebuilt (this tick) source-scoped report if it
                    # is now ok. Never triggers verification itself.
                    await _pf_partner_maybe_correct(uid, sk, target_date_iso, period_kind, report)
                    continue
                if deferred:
                    continue
                await _pf_send_partner_report(uid, target_date_iso, period_kind, text, report)
                print(f"[preflight-partner] send source={sk} target={uid} period={verify_period}")
            except Exception as e:
                print(f"[preflight-partner] send loop error user={uid}: {e!r}")
        if deferred:
            print(f"[preflight-partner] defer source={sk} reason=verify_pending period={verify_period}")


async def _preflight_partner_morning_loop() -> None:
    while True:
        try:
            now = _kyiv_now()
            if _pf_within_window(now, _PF_MORNING_SEND_HOUR_START, _PF_MORNING_SEND_MINUTE_START, _PF_MORNING_SEND_HOUR_CUTOFF):
                await _pf_partner_tick("today")
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[preflight-partner] morning loop error: {e!r}")
        await asyncio.sleep(60)


async def _preflight_partner_evening_loop() -> None:
    while True:
        try:
            now = _kyiv_now()
            if _pf_within_window(now, _PF_EVENING_SEND_HOUR_START, _PF_EVENING_SEND_MINUTE_START, _PF_EVENING_SEND_HOUR_CUTOFF):
                await _pf_partner_tick("tomorrow")
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[preflight-partner] evening loop error: {e!r}")
        await asyncio.sleep(60)


@client.on(events.CallbackQuery)
async def _preflight_partner_ok_callback(event):
    try:
        data = (event.data or b"").decode("utf-8", "ignore")
        if not data.startswith("pfok:"):
            return
        try:
            await event.delete()
        except Exception as e:
            print(f"[preflight-partner] OK-button delete failed: {e!r}")
        is_evening = data.endswith("e")
        try:
            await event.answer("Хорошего вечера ☀️" if is_evening else "Хорошего дня ☀️")
        except Exception:
            pass
    except Exception:
        pass

# --- TPILOT PREFLIGHT (PARTNER, MORNING+EVENING) END ---


# ---------------------------------------------------------------------------
# --- TPILOT MANAGER REPLACEMENT STAGE5 PARTNER NOTIFY 20260716 START ---
#
# PartnerBot-side notification/acknowledgement/finalization for account
# replacement (Stage 1 storage.py schema + guarded helpers, Stage 4 commit
# engine reaching cutover_done). Continues the flow:
#   cutover_done -> notified -> done
# Deliberately entirely self-contained in this file + already-existing
# storage.py Stage 1 primitives (replacement_ack_insert/_exists/
# _unacked_ids/_prune, replacement_list_notify_eligible_for_source,
# replacement_advance, replacement_finalize) -- none of those needed any
# change. No new storage.py schema, no main.py change: main.py's
# replacement_commit() already stops exactly at cutover_done by design: main.py
# is the CONTROLLER process (owns Telethon sign-in for managers); PartnerBot
# is a SEPARATE process with its OWN Telethon client, the one that actually
# has to send these messages -- so the sweep lives here, polling the shared
# SQLite DB, matching the existing _preflight_partner_morning_loop/
# _preflight_partner_evening_loop convention exactly (a plain asyncio loop
# with a fixed sleep interval, not a push from main.py).
#
# Delivery rule (documented per spec section 5, chosen deliberately): a
# replacement's cutover_done -> notified transition requires only AT LEAST
# ONE successful matching PartnerBot delivery, not all eligible users
# delivered. This matches the project's own existing best-effort per-user
# philosophy already established by _pf_send_partner_report/_pf_partner_tick
# ("one failed user send does not block others") -- there is no existing
# "wait for every user" gate anywhere else in this file to mirror instead,
# and requiring ALL users would mean one permanently-unreachable buyer could
# block the operation from ever finalizing. A user who was skipped/failed on
# the transitioning sweep is simply picked up on the NEXT sweep tick
# (idempotent per (replacement_id, user_id) via the arn_sent_* KV flag) --
# their eligibility for late delivery is unaffected by the transition
# already having advanced to notified/done, since REPLACEMENT_NOTIFY_
# ELIGIBLE_STATUSES already includes all three of cutover_done/notified/done.
#
# Done timing (documented per spec section 8, chosen deliberately): notified
# -> done happens immediately via the existing guarded replacement_finalize()
# right after the first successful send advances cutover_done -> notified.
# Per-user unread visibility does NOT depend on the operation's own status
# staying at "notified" -- REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES already
# treats cutover_done/notified/done as equally eligible for (re)send/ack, and
# replacement_ack_insert's own guard already accepts an ack against a 'done'
# row. So finalizing immediately does not hide the notification from anyone
# who hasn't acked yet -- it remains visible/ack-able exactly as long as its
# KV message reference is kept (up to the 7-day retention rule below).
# ---------------------------------------------------------------------------

_ARN_ACK_TEXT = "✅ Отмечено"
_ARN_RETENTION_DAYS = 7


def _arn_safe_at(username) -> str:
    """No bare '@' when username is empty -- renders a safe fallback phrase
    instead. Always returns a short plain-text token, never None."""
    u = str(username or "").strip().lstrip("@")
    return f"@{u}" if u else "(без username)"


def _arn_safe_display(display_name) -> str:
    d = str(display_name or "").strip()
    return d or "Менеджер"


def _arn_utc_iso_to_kyiv_hm(iso_utc: str) -> str:
    """Converts a UTC ISO string in storage.py's own _now_iso() format
    (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat(), naive, no tz
    suffix) to a Kyiv HH:MM display string. Never raises."""
    raw = str(iso_utc or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).strftime("%H:%M")
    except Exception:
        return ""


def _arn_notification_text(row: Dict[str, Any]) -> str:
    """Exact approved Stage 5 notification text. Never includes phone,
    tg_user_id, manager_key, proxy fields, session paths, or the raw
    operation_id -- only display/username/time, all already safe-formatted.
    {display} uses old_display_name -- the persona/name partners already
    know this account by, whose Telegram handle is what actually changed
    (falls back to new_display_name if the old one was somehow blank, so the
    line is never empty)."""
    display = _arn_safe_display(row.get("old_display_name") or row.get("new_display_name"))
    old_at = _arn_safe_at(row.get("old_username"))
    new_at = _arn_safe_at(row.get("new_username"))
    hhmm = _arn_utc_iso_to_kyiv_hm(row.get("links_ready_at")) or _kyiv_now().strftime("%H:%M")
    return (
        "🔄 Замена аккаунта\n\n"
        f"✅ {display} | {old_at} заменена на {new_at}.\n"
        f"Ссылки на сегодня обновлены в {hhmm}.\n\n"
        "Откройте 🔗 Бизнес-ссылки и проверьте новые ссылки."
    )


def _arn_eligible_rows_for_source(source_key: str) -> List[Dict[str, Any]]:
    """Notification-eligibility gate (spec section 2): status already
    constrained to cutover_done/notified/done by replacement_list_notify_
    eligible_for_source itself; the checks below add the remaining required
    conditions (non-empty source, links 15/15, links_ready_at present, old
    AND new manager_key present -- proves an old manager was genuinely
    replaced, not just a fresh onboarding). draft/auth_*/committing/
    links_pending/links_ready/failed/cancelled are excluded structurally --
    they can never reach REPLACEMENT_NOTIFY_ELIGIBLE_STATUSES in the first
    place, so no extra filtering is needed for those here."""
    sk = _norm_key(source_key)
    if not sk:
        return []
    try:
        rows = storage.replacement_list_notify_eligible_for_source(sk, db_path=TPILOT_DB_PATH)
    except Exception as e:
        print(f"[arn] eligible query failed source={sk}: {e!r}")
        return []
    out = []
    for r in rows or []:
        if int(r.get("required_links") or 0) <= 0:
            continue
        if int(r.get("ready_links") or 0) < int(r.get("required_links") or 0):
            continue
        if not str(r.get("links_ready_at") or "").strip():
            continue
        if not str(r.get("old_manager_key") or "").strip() or not str(r.get("new_manager_key") or "").strip():
            continue
        out.append(dict(r))
    return out


async def _arn_send_one(uid: int, row: Dict[str, Any]) -> bool:
    """Sends (or confirms already-sent) the replacement notice to exactly
    one PartnerBot user. Idempotent per (replacement_id, user_id) via the
    arn_sent_ KV flag -- a duplicate sweep never re-sends. Supersedes any
    OTHER (older) active replacement message this user still has pending
    (spec section 4: one active notification per user) -- delete failure of
    the old message is never allowed to block sending the new one."""
    rid = int(row.get("id") or 0)
    if rid <= 0:
        return False
    sent_key = f"arn_sent_{rid}_{uid}"
    if _pf_get_setting(sent_key) == "1":
        return True

    active_key = f"arn_active_msg_{uid}"
    prev_ref = _pf_get_setting(active_key)
    if prev_ref:
        prev_rid_s, _, prev_mid_s = prev_ref.partition(":")
        if prev_rid_s.strip().isdigit() and int(prev_rid_s) != rid and prev_mid_s.strip().isdigit():
            try:
                await client.delete_messages(int(uid), [int(prev_mid_s)])
            except Exception as e:
                print(f"[arn] supersede-delete failed user={uid}: {e!r}")

    text = _arn_notification_text(row)
    try:
        msg = await client.send_message(
            int(uid), text, buttons=[[Button.inline("✅ Ознакомился", f"arn:ack:{rid}".encode())]],
        )
    except Exception as e:
        print(f"[arn] send failed user={uid} rid={rid}: {e!r}")
        return False

    _pf_set_setting(sent_key, "1")
    mid = int(getattr(msg, "id", 0) or 0)
    if mid:
        _pf_set_setting(active_key, f"{rid}:{mid}")
    return True


async def _arn_sweep_source(source_key: str, buyer_user_ids: List[int]) -> Dict[str, Any]:
    """Structured result (spec section 11) for one source's sweep pass.
    Never includes secrets -- only ids/counts/status strings."""
    sk = _norm_key(source_key)
    result: Dict[str, Any] = {
        "ok": True, "replacement_id": None, "source_key": sk,
        "eligible_users": len(buyer_user_ids or []),
        "sent_count": 0, "failed_count": 0, "skipped_count": 0,
        "status_before": "", "status_after": "", "retryable": False, "message": "",
    }
    rows = _arn_eligible_rows_for_source(sk)
    if not rows or not buyer_user_ids:
        result["message"] = "no eligible replacements" if not rows else "no eligible users"
        return result

    for row in rows:
        rid = int(row.get("id") or 0)
        op_id = str(row.get("operation_id") or "")
        status_before = str(row.get("status") or "")
        any_sent = False
        for uid in buyer_user_ids:
            try:
                if storage.replacement_ack_exists(uid, rid, db_path=TPILOT_DB_PATH):
                    result["skipped_count"] += 1
                    continue
            except Exception:
                pass
            ok_send = await _arn_send_one(uid, row)
            if ok_send:
                result["sent_count"] += 1
                any_sent = True
            else:
                result["failed_count"] += 1

        status_after = status_before
        if status_before == "cutover_done" and any_sent:
            try:
                advanced = storage.replacement_advance(
                    op_id, "cutover_done", "notified", stage="partner_notified", db_path=TPILOT_DB_PATH,
                )
            except Exception as e:
                print(f"[arn] advance failed op={op_id}: {e!r}")
                advanced = False
            if advanced:
                status_after = "notified"
                try:
                    if storage.replacement_finalize(op_id, db_path=TPILOT_DB_PATH):
                        status_after = "done"
                except Exception as e:
                    print(f"[arn] finalize failed op={op_id}: {e!r}")
        result["replacement_id"] = rid
        result["status_before"] = status_before
        result["status_after"] = status_after

    result["retryable"] = result["failed_count"] > 0
    result["ok"] = result["failed_count"] == 0 or result["sent_count"] > 0
    return result


async def _arn_sweep_tick() -> None:
    try:
        buyers = _enabled_buyers()
    except Exception as e:
        print(f"[arn] enabled_buyers error: {e!r}")
        return
    by_source: Dict[str, List[int]] = {}
    for b in buyers:
        sk = _norm_key(str(b.get("source_key") or ""))
        uid = int(b.get("user_id") or 0)
        if sk and uid:
            by_source.setdefault(sk, []).append(uid)
    for sk, uids in by_source.items():
        try:
            await _arn_sweep_source(sk, uids)
        except Exception as e:
            print(f"[arn] sweep error source={sk}: {e!r}")


async def _arn_sweep_loop() -> None:
    while True:
        try:
            await _arn_sweep_tick()
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[arn] sweep loop error: {e!r}")
        await asyncio.sleep(90)


def _arn_retention_cutoff_iso() -> str:
    """7-day Kyiv-aware business cutoff, converted to storage.py's own UTC
    ISO format (matches _now_iso()'s exact style, since replacement_notify_
    ack.acknowledged_at is stored that way)."""
    cutoff_kyiv = _kyiv_now() - timedelta(days=_ARN_RETENTION_DAYS)
    cutoff_utc = cutoff_kyiv.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    return cutoff_utc.isoformat()


def _arn_run_daily_retention(target_date_iso: str) -> None:
    """Wired into the existing morning preflight tick (spec section 7: 'wire
    pruning into an existing daily/preflight/notification maintenance path',
    no new high-frequency loop). Runs at most once per calendar day via a KV
    guard. Prunes ACKNOWLEDGED rows older than the cutoff (replacement_ack_
    prune -- touches ONLY replacement_notify_ack, never manager_replacements
    or any unrelated table), and best-effort clears any STILL-UNACKED active
    message reference older than the same cutoff (per spec: 'unacknowledged
    notification older than 7 days is no longer actively shown' / 'active
    message reference may be cleaned up') -- the underlying manager_
    replacements row and its 'done' status are never touched here."""
    guard_key = f"arn_prune_done_{target_date_iso}"
    if _pf_get_setting(guard_key) == "1":
        return
    cutoff_iso = _arn_retention_cutoff_iso()
    try:
        pruned = storage.replacement_ack_prune(cutoff_iso, db_path=TPILOT_DB_PATH)
        print(f"[arn] retention pruned={pruned} cutoff={cutoff_iso}")
    except Exception as e:
        print(f"[arn] retention prune failed: {e!r}")

    try:
        buyers = _enabled_buyers()
    except Exception:
        buyers = []
    for b in buyers:
        uid = int(b.get("user_id") or 0)
        if not uid:
            continue
        active_key = f"arn_active_msg_{uid}"
        prev_ref = _pf_get_setting(active_key)
        if not prev_ref:
            continue
        prev_rid_s, _, prev_mid_s = prev_ref.partition(":")
        if not (prev_rid_s.strip().isdigit() and prev_mid_s.strip().isdigit()):
            continue
        rid = int(prev_rid_s)
        try:
            if storage.replacement_ack_exists(uid, rid, db_path=TPILOT_DB_PATH):
                continue  # already acked -- ack-row retention above already covers it
        except Exception:
            continue
        try:
            row = storage.replacement_get_by_id(rid, db_path=TPILOT_DB_PATH)
        except Exception:
            row = None
        completed_at = str((row or {}).get("completed_at") or "").strip()
        age_ref = completed_at or str((row or {}).get("links_ready_at") or "").strip()
        if not age_ref or age_ref >= cutoff_iso:
            continue  # unknown age -- fail closed, never guess-expire; or genuinely still fresh
        try:
            client.loop.create_task(_arn_cleanup_stale_message(uid, int(prev_mid_s), active_key))
        except Exception as e:
            print(f"[arn] retention cleanup schedule failed user={uid}: {e!r}")
    _pf_set_setting(guard_key, "1")


async def _arn_cleanup_stale_message(uid: int, mid: int, active_key: str) -> None:
    try:
        await client.delete_messages(int(uid), [int(mid)])
    except Exception as e:
        print(f"[arn] retention cleanup delete failed user={uid}: {e!r}")
    _pf_set_setting(active_key, "")


@client.on(events.CallbackQuery)
async def _arn_ack_callback(event):
    """Acknowledgement callback (spec section 6). callback_data is `arn:ack:
    {replacement_id}` -- the replacement_id itself is already a short
    integer (manager_replacements.id, not the long operation_id token), so
    no separate opaque-token mapping layer is needed to stay well under
    Telegram's 64-byte limit. Source/status verification happens entirely
    inside replacement_ack_insert's own atomic guard, using the CALLER's own
    resolved source_key (never a value read back out of the callback data
    itself) -- a forged/guessed replacement_id belonging to another source
    can never be acknowledged this way."""
    try:
        data = (event.data or b"").decode("utf-8", "ignore")
        if not data.startswith("arn:ack:"):
            return
        rid_s = data[len("arn:ack:"):].strip()
        if not rid_s.isdigit():
            try:
                await event.answer()
            except Exception:
                pass
            return
        rid = int(rid_s)
        uid = int(getattr(event, "sender_id", 0) or 0)

        buyer = _buyer(uid)
        sk = _norm_key(str(buyer.get("source_key") or ""))
        ok = False
        if sk and int(buyer.get("is_enabled") or 0) == 1:
            try:
                ok = storage.replacement_ack_insert(uid, rid, sk, db_path=TPILOT_DB_PATH)
            except Exception as e:
                print(f"[arn] ack insert failed user={uid} rid={rid}: {e!r}")
                ok = False

        try:
            await event.delete()
        except Exception as e:
            print(f"[arn] ack delete failed user={uid} rid={rid}: {e!r}")

        # Clear only THIS user's own active-message reference, and only if
        # it still points at the SAME replacement_id being acked -- never
        # touches a newer notification that may have superseded this one,
        # and never touches any other PartnerBot menu/statistics message.
        active_key = f"arn_active_msg_{uid}"
        prev_ref = _pf_get_setting(active_key)
        if prev_ref:
            prev_rid_s, _, _ = prev_ref.partition(":")
            if prev_rid_s.strip().isdigit() and int(prev_rid_s) == rid:
                _pf_set_setting(active_key, "")

        try:
            await event.answer(_ARN_ACK_TEXT if ok else "Не удалось отметить (устарело)")
        except Exception:
            pass
    except Exception:
        pass

# --- TPILOT MANAGER REPLACEMENT STAGE5 PARTNER NOTIFY 20260716 END ---


# ---------------------------------------------------------------------------
# --- TPILOT RESERVE ACTIVATION PARTNER NOTIFY 20260717 START ---
#
# Immediate PartnerBot notification when a reserve-account activation
# actually COMMITS. The commit itself (session/schedule/business-link setup)
# runs entirely in main.py's controller-only _reserve_activation_process_one
# -> storage.reserve_activation_finish, which flips reserve_activation_events
# .status 'creating' -> 'done' -- main.py and panel_bot.py are protected/
# out-of-scope files for this change. That commit is fully observable from
# here, though: reserve_activation_events, manager_reserve_pairs, and
# managers are plain storage.py-native SQLite tables on the same shared DB,
# so no protected-file edit was needed -- this sweep polls the durable
# 'done' state exactly the way _arn_sweep_* already polls manager_
# replacements, reusing that pipeline's own shape (persistent per-(event,
# user) dedup KV flag, source-scoped fan-out, best-effort per-recipient
# send, a plain asyncio sweep loop) instead of a second architecture.
#
# This is a DIFFERENT business event from account replacement and must
# never be confused with it: no "аккаунт заменён" wording, no shared
# dedup/ack state with the _arn_* pipeline (key prefixes rsvan_sent_* vs
# arn_sent_*/arn_active_msg_* never collide), no acknowledgement button
# (not requested for this event type), and it never touches the readiness-
# report message-id slots (preflight_msg_partner_*) or the _arn_active_
# msg_* pointer.
#
# Dedup key: reserve_activation_events.id (durable AUTOINCREMENT primary
# key) + the recipient's user_id, via `rsvan_sent_{event_id}_{uid}` in the
# same settings KV table every other PartnerBot flag already uses --
# survives a process restart, and a later, DIFFERENT reserve activated for
# the same primary gets its own new event id (and therefore its own new
# notification) automatically.
#
# BASELINE GUARD 20260717/18 (forensic review fixes -- two deploy blockers):
#
# Round 1 (20260717) used a pure `id > baseline` cutoff. That closed the
# first-deploy historical-spam hole but opened two new ones the second
# review caught by direct code inspection:
#   1. LATE-ENABLED RECIPIENT BACKLOG -- since the id-baseline never
#      advances and a 'done' row is never removed, a partner enabled months
#      after deploy has no rsvan_sent_* flags for any post-baseline row and
#      would receive the ENTIRE accumulated backlog as "fresh" on their
#      first sweep.
#   2. REUSED/LATE-TRANSITIONING ROW -- storage.reserve_activation_request
#      (storage.py) reuses the SAME row/id for a cancelled-then-re-requested
#      activation, and reserve_activation_finish only ever UPDATEs status IN
#      PLACE. A row whose id happens to be <= the id-baseline (e.g. it was
#      'requested'/'cancelled' at deploy time) but transitions to 'done'
#      LATER would be permanently suppressed by a pure id filter, even
#      though its completion is genuinely new.
#
# Round 2 (20260718) replaces the id cutoff with a TIMESTAMP baseline plus a
# bounded freshness window, using reserve_activation_events.updated_at --
# which reserve_activation_finish always refreshes on every status
# transition (verified directly in storage.py), so a reused row's new
# completion is always visible via updated_at regardless of its old id.
#   - rsvan_baseline_updated_at: the ELIGIBILITY gate. Initialized ONCE to
#     "now" (UTC) and never advanced afterward (still Option A: a fixed
#     cutoff, not a per-event/per-sweep high-water mark -- advancing it
#     would let a failed recipient's retry silently expire the moment the
#     next sweep ran, which is exactly the failure mode the ORIGINAL
#     replacement-notify pipeline's own design notes already warn against).
#   - rsvan_baseline_max_event_id: kept only as a diagnostic/back-compat
#     breadcrumb (still written at init) -- no longer part of the
#     eligibility WHERE clause at all.
#   - _RSVAN_FRESHNESS_HOURS (72h): bounds BOTH the late-enabled-recipient
#     backlog (they can only ever receive the last 72h of activity for
#     their source, never the full post-baseline history) AND the sweep's
#     own candidate-set growth (the SQL WHERE clause itself is bounded by
#     updated_at, so the table is never fully rescanned every 90s). A
#     recipient who stays disabled/unreachable beyond 72h simply stops
#     being retried for that specific old event -- accepted tradeoff per
#     spec, preferable to unbounded backlog/rescan.
# Per-(event,user) rsvan_sent_ dedup and _rsvan_lock's serialization were
# UNCHANGED by round 2 -- but round 2's own claim that dedup was "already
# correct" was wrong, caught by round 3's review below.
#
# Round 3 (20260718b, dedup blocker fix): storage.reserve_activation_cancel
# accepts status 'done' as a valid source state (verified directly in
# storage.py), so a committed reserve_activation_events row can be
# cancelled and then reserve_activation_request revives the SAME row/id
# for a genuinely new activation, which reserve_activation_finish later
# completes again with a NEWER updated_at on the SAME id. The old static
# rsvan_sent_{event_id}_{uid}="1" flag from the FIRST completion would
# permanently suppress the SECOND, real, distinct completion forever.
#   - Fix: the dedup VALUE is now the canonical (_rsvan_canonical_generation)
#     updated_at of the delivered completion, not a static "1" -- i.e. dedup
#     is keyed on (event_id, user_id, done-GENERATION), not just
#     (event_id, user_id). A duplicate sweep or a restart re-observing the
#     SAME completion (identical generation string) still never re-sends;
#     a later completion of the same id (different generation string)
#     always does. Any legacy/malformed stored value (including a
#     pre-round-3 static "1") can never equal a real canonical generation
#     string, so it can never wrongly suppress a genuine new completion --
#     no special-case legacy branch was needed. The key format itself
#     (rsvan_sent_{event_id}_{uid}) and the settings KV table are
#     unchanged -- no schema migration.
# ---------------------------------------------------------------------------

_RSVAN_BASELINE_KEY = "rsvan_baseline_max_event_id"  # diagnostic/back-compat only -- not used for eligibility.
_RSVAN_BASELINE_TS_KEY = "rsvan_baseline_updated_at"
_RSVAN_FRESHNESS_HOURS = 72
_rsvan_lock = asyncio.Lock()


def _rsvan_now_iso() -> str:
    """Same naive-UTC-no-microseconds format storage.py's own _now_iso()
    writes into reserve_activation_events.updated_at (verified directly:
    reserve_activation_request/_finish/_cancel all use that exact
    convention) -- matching it exactly is what makes the SQL-level string
    comparison (updated_at>?/updated_at>=?) correct without a per-row
    normalize-in-Python step for the common case; malformed/foreign-format
    rows are still caught defensively below (see _rsvan_parse_utc)."""
    return datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat()


def _rsvan_parse_utc(raw) -> Optional[datetime]:
    """Normalizes a stored updated_at value to a timezone-aware UTC
    datetime. Accepts the project's own naive-UTC ISO convention (no
    offset), an explicit 'Z' suffix, or an explicit UTC offset like
    '+00:00'. Blank/None/malformed -> None -- NEVER raises, and a value
    that fails to parse is always treated as ineligible/excluded, never as
    "fresh" (a parse failure must not become a permissive default)."""
    s = str(raw or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _rsvan_canonical_generation(raw) -> Optional[str]:
    """Canonical dedup value for one 'done' completion (generation) of a
    reserve_activation_events row -- the project's own naive-UTC-no-
    microseconds ISO form (same convention as _rsvan_now_iso()/storage.py's
    _now_iso()), derived via _rsvan_parse_utc so a Z-suffixed, +00:00-
    offset, or already-naive-UTC input for the SAME instant all normalize
    to the identical string. None on blank/malformed input -- callers must
    never send or store a dedup value for an unparseable generation
    (skip/deny, never treat as fresh)."""
    dt = _rsvan_parse_utc(raw)
    if dt is None:
        return None
    return dt.replace(microsecond=0, tzinfo=None).isoformat()


def _rsvan_get_baseline_ts() -> Optional[str]:
    """Returns the stored baseline timestamp string, or None if it has
    never been initialized OR the stored value is malformed -- both cases
    mean "(re)initialization is required"."""
    raw = _pf_get_setting(_RSVAN_BASELINE_TS_KEY).strip()
    if not raw or _rsvan_parse_utc(raw) is None:
        return None
    return raw


def _rsvan_max_done_event_id() -> int:
    """Read-only: MAX(id) across ALL sources for status='done' rows.
    Diagnostic/back-compat value only (see module docstring above) -- no
    longer consulted for eligibility. Returns 0 if the table is
    missing/empty/on any error."""
    try:
        storage.ensure_reserve_tables(TPILOT_DB_PATH)
        con = sqlite3.connect(TPILOT_DB_PATH)
        try:
            row = con.execute("SELECT MAX(id) FROM reserve_activation_events WHERE status='done'").fetchone()
        finally:
            con.close()
    except Exception as e:
        print(f"[rsvan] baseline max-id query failed: {e!r}")
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _rsvan_ensure_baseline() -> Optional[str]:
    """Idempotent one-time TIMESTAMP baseline initialization. Returns the
    baseline ISO string on success. Returns None if initialization itself
    could not be durably confirmed -- the caller MUST then send nothing
    this pass (never fall through to fan-out with an unknown/unsafe cutoff)
    and let the next sweep retry. Callers must already hold _rsvan_lock
    (this function does not acquire it itself, so it composes safely
    inside a single locked sweep pass without nested-lock risk)."""
    existing = _rsvan_get_baseline_ts()
    if existing is not None:
        return existing
    now_iso = _rsvan_now_iso()
    try:
        _pf_set_setting(_RSVAN_BASELINE_KEY, str(_rsvan_max_done_event_id()))
        _pf_set_setting(_RSVAN_BASELINE_TS_KEY, now_iso)
    except Exception as e:
        print(f"[rsvan] baseline init write failed: {e!r}")
        return None
    confirmed = _rsvan_get_baseline_ts()
    if confirmed != now_iso:
        print("[rsvan] baseline init write did not verify -- will retry next sweep")
        return None
    print(f"[rsvan] baseline initialized at {now_iso}")
    return now_iso

def _rsvan_manager_display(manager_key: str) -> Tuple[str, str]:
    """Returns (display_name, telegram_username) for manager_key from the
    real `managers` table. Never raises -- blank strings on any failure or
    missing row; callers run these through the same _arn_safe_display/
    _arn_safe_at helpers the replacement pipeline already uses, which
    render blanks safely (never a bare '@', never 'None')."""
    mk = _norm_key(manager_key)
    if not mk:
        return "", ""
    try:
        con = sqlite3.connect(TPILOT_DB_PATH)
        try:
            row = con.execute(
                "SELECT display_name, telegram_username FROM managers WHERE manager_key=?", (mk,)
            ).fetchone()
        finally:
            con.close()
    except Exception as e:
        print(f"[rsvan] manager lookup failed key={mk}: {e!r}")
        return "", ""
    if not row:
        return "", ""
    return str(row[0] or ""), str(row[1] or "")


def _rsvan_notification_text(primary_key: str, reserve_key: str) -> str:
    """Deliberately NOT the permanent-replacement wording ('аккаунт
    заменён') -- a reserve activation is a temporary/covering assignment,
    not the Stage 1-5 permanent replacement flow. Never includes
    manager_key/event id/session/proxy/technical state names."""
    p_display, p_username = _rsvan_manager_display(primary_key)
    r_display, r_username = _rsvan_manager_display(reserve_key)
    p_name = _arn_safe_display(p_display)
    r_name = _arn_safe_display(r_display)
    p_at = _arn_safe_at(p_username)
    r_at = _arn_safe_at(r_username)
    return (
        "🟢 Активирован резерв\n\n"
        f"Для аккаунта «{p_name}» {p_at} активирован резерв «{r_name}» {r_at}."
    )


def _rsvan_done_events_for_source(source_key: str, *, baseline_ts: str,
                                   freshness_hours: int = _RSVAN_FRESHNESS_HOURS) -> List[Dict[str, Any]]:
    """Read-only: every reserve_activation_events row with status='done' for
    this source_key, whose updated_at is STRICTLY AFTER baseline_ts (the
    fixed, never-advancing init-time cutoff -- excludes everything that was
    already 'done' before this feature's baseline) AND within the last
    freshness_hours of "now" (bounds late-enabled-recipient backlog AND
    keeps this query itself bounded -- never a full-table scan). The two
    SQL bounds are plain string comparisons against updated_at, which is
    safe here specifically because every row in this table is written
    exclusively via storage.py's own _now_iso() convention (same format
    _rsvan_now_iso() reproduces above) -- this is the "bounded at SQL
    level" filter the design calls for; the Python loop below is a
    defensive SECOND pass (skips anything that doesn't actually parse,
    e.g. a hypothetical foreign-format value) rather than the primary
    growth-limiting mechanism. ORDER BY updated_at ASC, id ASC keeps
    delivery order deterministic even across reused/old-id rows. A
    malformed row is skipped, never aborts the batch, and is never logged
    with its raw content/id (bounded, non-secret diagnostics only).

    The baseline bound is deliberately updated_at>=baseline_ts, NOT a
    strict >. _now_iso() has only whole-second resolution, so a genuinely
    NEW event that completes in the SAME SECOND baseline initialization
    ran would otherwise collide with a strict '>' and be wrongly excluded
    -- exactly the race the design review asked to have reasoned through.
    '>=' is safe from the historical-spam side too: baseline_ts is always
    computed strictly AFTER every row that existed at init time was
    already written, so no pre-existing historical row can ever share that
    exact timestamp -- only a brand-new completion racing the init instant
    can, and that one must be delivered."""
    sk = _norm_key(source_key)
    if not sk or not baseline_ts:
        return []
    cutoff_iso = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0) - timedelta(hours=int(freshness_hours))).isoformat()
    try:
        storage.ensure_reserve_tables(TPILOT_DB_PATH)
        con = sqlite3.connect(TPILOT_DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT * FROM reserve_activation_events"
                " WHERE source_key=? AND status='done' AND updated_at>=? AND updated_at>=?"
                " ORDER BY updated_at ASC, id ASC",
                (sk, baseline_ts, cutoff_iso),
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        print(f"[rsvan] done-events query failed source={sk}: {e!r}")
        return []
    out = []
    for r in rows or []:
        try:
            d = dict(r)
            if int(d.get("id") or 0) <= 0:
                continue
            if _rsvan_parse_utc(d.get("updated_at")) is None:
                print(f"[rsvan] skipping event with unparseable updated_at source={sk}")
                continue
            out.append(d)
        except Exception as e:
            print(f"[rsvan] skipping malformed event row source={sk}: {e!r}")
            continue
    return out


async def _rsvan_send_one(uid: int, event_row: Dict[str, Any]) -> bool:
    """Idempotent per (event_id, user_id, done-GENERATION). The dedup value
    stored under rsvan_sent_{event_id}_{uid} is the canonical updated_at of
    the delivered completion, not a static marker -- storage.py's
    reserve_activation_cancel accepts status 'done' (verified directly),
    so the SAME event_id can be cancelled and re-activated, producing a
    SECOND genuinely new completion with a newer updated_at on the same
    row. A duplicate sweep tick or a restart re-observing the SAME
    completion (same generation string) never re-sends; a later, DIFFERENT
    completion of the same id always does, because its generation string
    differs. A blank/malformed updated_at can never send or be stored (see
    _rsvan_canonical_generation) -- in practice _rsvan_done_events_for_source
    already filters those rows out before this is called. Any legacy/
    malformed stored value (including a pre-generation-aware static "1")
    simply never equals a real canonical generation string, so it can never
    permanently suppress a valid completion."""
    event_id = int(event_row.get("id") or 0)
    if event_id <= 0:
        return False
    generation = _rsvan_canonical_generation(event_row.get("updated_at"))
    if generation is None:
        return False
    sent_key = f"rsvan_sent_{event_id}_{uid}"
    stored = _pf_get_setting(sent_key).strip()
    if stored and stored == generation:
        return True

    text = _rsvan_notification_text(
        str(event_row.get("primary_key") or ""), str(event_row.get("reserve_key") or ""),
    )
    try:
        await client.send_message(int(uid), text)
    except Exception as e:
        print(f"[rsvan] send failed user={uid} event_id={event_id}: {e!r}")
        return False

    _pf_set_setting(sent_key, generation)
    return True


async def _rsvan_sweep_source(source_key: str, buyer_user_ids: List[int]) -> None:
    """One source's sweep pass. Serialized end-to-end by _rsvan_lock (baseline
    check/init through the full per-recipient fan-out) so two overlapping
    calls -- e.g. a duplicate/overlapping sweep tick -- can never both
    initialize the baseline or interleave around the same event's
    check-then-send-then-mark-sent window. If the baseline cannot be
    confirmed this pass, nothing is sent (fail closed) and the next sweep
    retries baseline initialization. A send failure for one recipient is
    fully isolated (caught per-uid) and never blocks the remaining
    recipients, the remaining events, or that recipient's OWN retry on the
    next sweep (rsvan_sent_ is only set after a confirmed successful send)."""
    if not buyer_user_ids:
        return
    async with _rsvan_lock:
        baseline_ts = _rsvan_ensure_baseline()
        if baseline_ts is None:
            return
        rows = _rsvan_done_events_for_source(source_key, baseline_ts=baseline_ts)
        for row in rows:
            for uid in buyer_user_ids:
                try:
                    await _rsvan_send_one(uid, row)
                except Exception as e:
                    print(f"[rsvan] send error user={uid} event_id={row.get('id')}: {e!r}")


async def _rsvan_sweep_tick() -> None:
    try:
        buyers = _enabled_buyers()
    except Exception as e:
        print(f"[rsvan] enabled_buyers error: {e!r}")
        return
    by_source: Dict[str, List[int]] = {}
    for b in buyers:
        sk = _norm_key(str(b.get("source_key") or ""))
        uid = int(b.get("user_id") or 0)
        if sk and uid:
            by_source.setdefault(sk, []).append(uid)
    for sk, uids in by_source.items():
        try:
            await _rsvan_sweep_source(sk, uids)
        except Exception as e:
            print(f"[rsvan] sweep error source={sk}: {e!r}")


async def _rsvan_sweep_loop() -> None:
    while True:
        try:
            await _rsvan_sweep_tick()
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[rsvan] sweep loop error: {e!r}")
        await asyncio.sleep(90)

# --- TPILOT RESERVE ACTIVATION PARTNER NOTIFY 20260717 END ---


if __name__ == "__main__":
    asyncio.run(main())
