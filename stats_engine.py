# -*- coding: utf-8 -*-
"""Unified TPilot statistics engine (Stage 4A).

Single source of truth for Lite / Pro / BOTH statistics shared by PartnerBot,
ManagerBot, and AdminBot. Pure and READ-ONLY: it only opens per-manager SQLite
DBs for SELECT, never writes, never loads .env, never constructs a Telegram
client, and has no import-time side effects.

The canonical bucket / overlay / formatting logic is ported verbatim from the
battle-tested PartnerBot `_psf3_*` implementation so that wiring each bot to this
engine produces byte-identical output for the consecutive-day case (see the
parity harness, Stage 4B). Schedule-aware dolyoty is implemented but gated behind
`schedule_aware=False` (default), so this module is inert until Stage 4F flips it
on.

Canonical rules (mirror of the approved plan):
  - Period membership = arrival time (first_seen_kyiv -> first_seen_utc, Kyiv).
  - Bucket counting = current status/quality at report time, via the
    lead_status_overrides overlay.
  - Lite = calendar-day "wrote / duplicates".
  - Pro  = day window 08:00-17:00 buckets + dolyoty night.
  - Dolyoty night for working day D = [P 17:00, D 08:00) where P = previous
    scheduled working day (schedule_aware); else [D-1 17:00, D 08:00).
  - PRO formula: ОТПИСОК = ЛИКВИД + НЕЛИКВИД; НЕЛИКВИД = ГЕО + -18 + NA + TRASH.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# Kyiv everywhere unless a caller proves otherwise.
SE_TZ = ZoneInfo("Europe/Kyiv")

# Default lookback (days) for schedule-aware dolyoty carry. Matches HISTORY_DAYS_LIMIT.
SE_DOLYOT_LOOKBACK_DAYS = 31

BASE_DIR = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Keys / paths
# --------------------------------------------------------------------------- #
def se_norm_key(raw: Any) -> str:
    """Manager-key normalization identical to PartnerBot/ManagerBot `_norm_key`."""
    return re.sub(r"[^a-z0-9_-]+", "", str(raw or "").strip().lower().replace("ё", "е"))


def se_manager_db_path(row: Dict[str, Any]) -> str:
    """Resolve a manager's DB path, mirroring `_psf3_manager_db_path` fallback."""
    p = str((row or {}).get("db_path") or "").strip()
    try:
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    mk = se_norm_key((row or {}).get("manager_key") or "")
    if not mk:
        return p or ""
    cand = os.path.join(str(BASE_DIR), "runtime", "managers", mk, f"{mk}.db")
    return cand


# --------------------------------------------------------------------------- #
# Time / membership (arrival-time anchored)
# --------------------------------------------------------------------------- #
def se_local_dt(lead: Dict[str, Any]) -> Optional[datetime]:
    """Lead arrival time as Kyiv-aware datetime. first_seen_kyiv preferred."""
    raw_local = str((lead or {}).get("first_seen_kyiv") or "").strip()
    if raw_local:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw_local[:19], fmt).replace(tzinfo=SE_TZ)
            except Exception:
                pass
    raw_utc = str((lead or {}).get("first_seen_utc") or "").strip()
    if raw_utc:
        try:
            dt = datetime.fromisoformat(raw_utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(SE_TZ)
        except Exception:
            pass
    return None


def se_date_iter(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur = cur + timedelta(days=1)


def se_date_list(start: date, end: date) -> List[str]:
    """Calendar-day ISO list for the [start, end] inclusive range (for SQL IN)."""
    return [d.isoformat() for d in se_date_iter(start, end)]


def se_prev_working_day(
    manager_key: str,
    before_date_iso: str,
    *,
    db_path: Optional[str] = None,
    lookback_days: int = SE_DOLYOT_LOOKBACK_DAYS,
) -> Optional[str]:
    """Most recent scheduled working day strictly before `before_date_iso`.

    Thin wrapper over storage.manager_schedule_prev_working_day. Returns None on
    any failure (caller then falls back to the single-night window). Import is
    lazy so the engine stays usable even if storage is unavailable.
    """
    try:
        from storage import manager_schedule_prev_working_day as _prev
    except Exception:
        return None
    try:
        return _prev(se_norm_key(manager_key), before_date_iso, lookback_days, db_path)
    except Exception:
        return None


# Default Kyiv day work window as minutes-from-midnight: 08:00-17:00. Mirrors
# storage.SOURCE_WINDOW_DEFAULT_DAY_START/END; duplicated here (not imported) so
# the engine stays usable even when storage is unavailable.
SE_WINDOW_DEFAULT_DAY_START_MIN = 8 * 60   # 480
SE_WINDOW_DEFAULT_DAY_END_MIN = 17 * 60    # 1020


def _se_window_minutes(window_cfg: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    """Resolve (day_start_min, day_end_min) from an optional window_cfg.

    Falls back to the current 08:00-17:00 default when window_cfg is None, not a
    dict, missing a field, or holds an out-of-range/overnight value — this keeps
    every existing caller (which never passes window_cfg) byte-identical.
    """
    default = (SE_WINDOW_DEFAULT_DAY_START_MIN, SE_WINDOW_DEFAULT_DAY_END_MIN)
    if not isinstance(window_cfg, dict):
        return default
    try:
        ds = int(window_cfg.get("day_start_min"))
        de = int(window_cfg.get("day_end_min"))
    except (TypeError, ValueError):
        return default
    if not (0 <= ds < de <= 1439):
        return default
    return (ds, de)


def se_day_window(
    d: date, *, window_cfg: Optional[Dict[str, Any]] = None
) -> Tuple[datetime, datetime]:
    """Day work window: [D day_start, D day_end). Default [D 08:00, D 17:00)."""
    ds_min, de_min = _se_window_minutes(window_cfg)
    base = datetime(d.year, d.month, d.day, tzinfo=SE_TZ)
    return (
        base + timedelta(minutes=ds_min),
        base + timedelta(minutes=de_min),
    )


def se_flight_window(
    d: date,
    *,
    manager_key: str = "",
    schedule_aware: bool = False,
    db_path: Optional[str] = None,
    lookback_days: int = SE_DOLYOT_LOOKBACK_DAYS,
    window_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[datetime, datetime]:
    """Dolyoty night window for working/closing date D.

    Default (schedule_aware=False): [D-1 17:00, D 08:00) — current behavior.
    schedule_aware=True: [P 17:00, D 08:00) where P = previous scheduled working
    day; falls back to D-1 when no previous working day is found within lookback.
    window_cfg (optional): overrides the 08:00/17:00 anchors with
    day_start_min/day_end_min — the night window becomes the exact complement
    [P day_end, D day_start), so day+night still tile with no overlap/gap.
    """
    ds_min, de_min = _se_window_minutes(window_cfg)
    base = datetime(d.year, d.month, d.day, tzinfo=SE_TZ)
    end = base + timedelta(minutes=ds_min)
    start_day = d - timedelta(days=1)
    if schedule_aware and manager_key:
        prev_iso = se_prev_working_day(
            manager_key, d.isoformat(), db_path=db_path, lookback_days=lookback_days
        )
        if prev_iso:
            try:
                start_day = datetime.strptime(prev_iso, "%Y-%m-%d").date()
            except Exception:
                start_day = d - timedelta(days=1)
    start_base = datetime(start_day.year, start_day.month, start_day.day, tzinfo=SE_TZ)
    start = start_base + timedelta(minutes=de_min)
    return (start, end)


def se_in_window(lead: Dict[str, Any], ws: datetime, we: datetime) -> bool:
    dt = se_local_dt(lead)
    if not dt:
        return False
    return ws <= dt < we


# --------------------------------------------------------------------------- #
# Quality buckets (ported verbatim from PartnerBot `_psf3_*`)
# --------------------------------------------------------------------------- #
def se_age(lead: Dict[str, Any]) -> Optional[int]:
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


def se_bucket(lead: Dict[str, Any]) -> str:
    qb = str((lead or {}).get("quality_bucket") or "").strip().lower()
    qs = str((lead or {}).get("quality_status") or "").strip().lower()
    reason = str((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "").strip().lower()
    status = str((lead or {}).get("status") or "").strip().lower()
    country = str((lead or {}).get("country") or "").strip()
    age = se_age(lead)

    # R1A1-F (F-24): a manual override (se_apply_override_overlay, which sets
    # quality_bucket/quality_status and marks _manual_override_overlay=1) must be
    # authoritative -- it must win even when the raw `status` column is stuck at
    # 'liquid' from before the override. Mirrors manager_bot._mbstat_bucket's
    # override-first precedence. Non-override rows fall through unchanged below
    # (byte-identical to before this block existed).
    if int((lead or {}).get("_manual_override_overlay") or 0) == 1:
        if qb == "liquid":
            return "liquid"
        if qb == "geo":
            return "geo"
        if qb in ("under18", "age_missing"):
            return "under18"
        if qb == "trash":
            return "trash"
        if qb in ("na", "geo_missing", "age_and_geo_missing", "unknown", "unclear"):
            return "na"

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


def se_bucket_empty() -> Dict[str, int]:
    return {"otpisok": 0, "nonliquid": 0, "geo": 0, "under18": 0, "na": 0, "trash": 0, "liquid": 0}


def se_bucket_add(b: Dict[str, int], lead: Dict[str, Any]) -> None:
    bucket = se_bucket(lead)
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


def se_bucket_lines(b: Dict[str, int]) -> List[str]:
    return [
        f"ОТПИСОК: {int(b.get('otpisok') or 0)}",
        f"НЕЛИКВИД: {int(b.get('nonliquid') or 0)}",
        f"ГЕО: {int(b.get('geo') or 0)}",
        f"-18: {int(b.get('under18') or 0)}",
        f"NA: {int(b.get('na') or 0)}",
        f"TRASH: {int(b.get('trash') or 0)}",
        f"ЛИКВИД: {int(b.get('liquid') or 0)}",
    ]


def se_country_title(country: Any, reason: Any = "") -> str:
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


def se_na_reason_ru(reason: Any) -> str:
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


def se_trash_reason_ru(reason: Any) -> str:
    low = str(reason or "").strip().lower()
    if "send_failed" in low or "blocked" in low or "inaccessible" in low or "недоступ" in low:
        return "чат недоступен / отправка не прошла"
    return "🗑 trash"


def se_details(leads: List[Dict[str, Any]]) -> List[str]:
    geo: Dict[str, int] = {}
    under18: Dict[str, int] = {}
    na: Dict[str, int] = {}
    trash: Dict[str, int] = {}
    for lead in list(leads or []):
        bucket = se_bucket(lead)
        reason = str((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "").strip()
        if bucket == "geo":
            if str((lead or {}).get("country") or "").strip() not in ("Россия", "РФ", "Российская Федерация"):
                title = se_country_title(str((lead or {}).get("country") or ""), reason)
                geo[title] = geo.get(title, 0) + 1
        elif bucket == "under18":
            age = se_age(lead)
            if age is not None and age < 18:
                title = f"{age} лет"
                under18[title] = under18.get(title, 0) + 1
        elif bucket == "na":
            title = se_na_reason_ru(reason)
            na[title] = na.get(title, 0) + 1
        elif bucket == "trash":
            title = se_trash_reason_ru(reason)
            trash[title] = trash.get(title, 0) + 1
    lines: List[str] = []
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


# --------------------------------------------------------------------------- #
# Countable / duplicate predicates
# --------------------------------------------------------------------------- #
def se_countable(lead: Dict[str, Any]) -> bool:
    try:
        if "lead_countable" in (lead or {}):
            return int((lead or {}).get("lead_countable") or 0) == 1
    except Exception:
        return False
    try:
        return int((lead or {}).get("duplicate") or 0) != 1
    except Exception:
        return True


def se_duplicate_for_light(lead: Dict[str, Any]) -> bool:
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


def se_is_buyer_duplicate(
    lead: Dict[str, Any],
    *,
    identity_hook: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> bool:
    """Canonical buyer-duplicate predicate.

    `identity_hook` is an edge concern (PartnerBot's same-source identity dedup);
    when None (ManagerBot/AdminBot), identity dedup is skipped.
    """
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
    if callable(identity_hook):
        try:
            if identity_hook(lead):
                return True
        except Exception:
            pass
    return False


# --------------------------------------------------------------------------- #
# Override overlay (lead_status_overrides) — late status changes update old stats
# --------------------------------------------------------------------------- #
SE_OVERRIDE_BUCKET_CANON = ("liquid", "geo", "under18", "na", "trash")

SE_OVERRIDE_STATUS_FOR_BUCKET = {
    "liquid": "liquid",
    "geo": "nonliquid",
    "under18": "nonliquid",
    "trash": "trash",
    "na": "na",
}


def se_override_bucket_value(raw: Any) -> str:
    v = str(raw or "").strip().lower()
    if v in SE_OVERRIDE_BUCKET_CANON:
        return v
    if v == "age_missing":
        return "under18"
    if v == "geo_missing":
        return "na"
    return ""


def se_table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
        ).fetchone())
    except Exception:
        return False


def se_overrides_for_manager(con: sqlite3.Connection, manager_key: str) -> Dict[int, Dict[str, Any]]:
    """Load lead_status_overrides for a manager: {chat_id: {bucket,reason,status}}."""
    mk = se_norm_key(manager_key)
    per_chat: Dict[int, Dict[str, Any]] = {}
    if not mk or con is None:
        return per_chat
    try:
        if not se_table_exists(con, "lead_status_overrides"):
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
            norm = se_override_bucket_value(r["bucket"])
            if not norm:
                norm = se_override_bucket_value(r["status"])
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


def se_apply_override_overlay(leads: List[Dict[str, Any]], overrides: Dict[int, Dict[str, Any]]) -> None:
    """In-place overlay so se_bucket resolves to the manual override. Never writes DB."""
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
        if bucket not in SE_OVERRIDE_BUCKET_CANON:
            continue
        lead["quality_bucket"] = bucket
        lead["quality_status"] = SE_OVERRIDE_STATUS_FOR_BUCKET.get(bucket, "na")
        if ov.get("reason"):
            lead["quality_reason"] = ov.get("reason")
        lead["_manual_override_overlay"] = 1


# --------------------------------------------------------------------------- #
# Lead fetch (read-only)
# --------------------------------------------------------------------------- #
class SeLeadsUnavailable(list):
    """R1A1-G (F-25): sentinel meaning "this manager's leads could not be read"
    (locked/corrupt/unreadable DB) -- as opposed to a genuinely empty dataset.
    Subclasses list so every existing caller that just iterates/len()s the
    result keeps working unchanged (degrades to "no leads" behaviorally, same
    as before this fix); code that needs to tell the two apart can check
    `isinstance(result, SeLeadsUnavailable)` instead of silently trusting a
    zero that might actually be a database outage."""
    __slots__ = ()


def se_leads_for_manager(row: Dict[str, Any], dates: List[str]) -> List[Dict[str, Any]]:
    """Read daily_leads for a manager over the given calendar-day ISO list.

    Applies the lead_status_overrides overlay using the same connection. Pure
    read; returns [] only for a genuinely missing/never-created DB or an empty
    `dates` list -- both are legitimate "nothing to read" cases, not failures.
    A read/connect FAILURE (locked, corrupt, permission, etc.) returns
    SeLeadsUnavailable() instead of [] -- see R1A1-G (F-25).
    """
    dbp = se_manager_db_path(row)
    try:
        if not dbp or not os.path.exists(dbp):
            return []
    except Exception:
        return []
    if not dates:
        return []
    q = ",".join(["?"] * len(dates))
    try:
        con = sqlite3.connect(dbp, timeout=20)
        con.row_factory = sqlite3.Row
    except Exception:
        return SeLeadsUnavailable()
    try:
        rows = con.execute(
            f"SELECT * FROM daily_leads WHERE lead_date IN ({q}) ORDER BY first_seen_utc ASC, id ASC",
            tuple(dates),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows or []:
            d = dict(r)
            d["manager_key"] = se_norm_key((row or {}).get("manager_key") or d.get("manager_key") or "")
            d["manager_username"] = str((row or {}).get("telegram_username") or d.get("manager_username") or "")
            d["manager_display_name"] = str((row or {}).get("display_name") or d.get("manager_key") or "")
            out.append(d)
        try:
            overrides = se_overrides_for_manager(con, (row or {}).get("manager_key") or "")
            se_apply_override_overlay(out, overrides)
        except Exception:
            pass
        return out
    except Exception:
        return SeLeadsUnavailable()
    finally:
        try:
            con.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Labels / titles
# --------------------------------------------------------------------------- #
def se_manager_label(row: Dict[str, Any]) -> str:
    username = str((row or {}).get("telegram_username") or (row or {}).get("manager_username") or "").strip().lstrip("@")
    name = str((row or {}).get("display_name") or (row or {}).get("manager_key") or "").strip()
    if username and name and name.lower() != username.lower():
        label = f"{name} | @{username}"
    elif username:
        label = f"@{username}"
    else:
        label = name or str((row or {}).get("manager_key") or "_")
    # --- TPILOT HISTSTATS M2.6H START ---
    status = str((row or {}).get("status") or "").strip()
    is_enabled = int((row or {}).get("is_enabled") if (row or {}).get("is_enabled") is not None else 1)
    if status == "archived":
        label += " (архив)"
    elif status == "deleted":
        label += " (аккаунт удалён)"
    elif is_enabled == 0:
        label += " (отключён)"
    # --- TPILOT HISTSTATS M2.6H END ---
    return label


# --------------------------------------------------------------------------- #
# Body builders (Lite / Pro day / Pro flight) — parity with PartnerBot `_psf3_*`
# --------------------------------------------------------------------------- #
def se_windowed_leads(
    row: Dict[str, Any],
    start: date,
    end: date,
    kind: str,
    *,
    schedule_aware: bool = False,
    leads_provider: Optional[Callable[[Dict[str, Any], List[str]], List[Dict[str, Any]]]] = None,
    window_cfg: Optional[Dict[str, Any]] = None,
    period_mode: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch + window-filter a manager's leads for kind in {"day","flight"}.

    For flight, the fetch range is widened one day back (and, when schedule_aware,
    back to the previous working day) so 17:00+ leads of the prior day are visible;
    the per-day window filter still admits only true night-window leads.

    window_cfg (optional, kind="day"/"flight"): overrides the 08:00/17:00 anchors
    (see _se_window_minutes); None or invalid -> current defaults, unchanged.

    period_mode (optional, kind="day" only): "calendar_day" runs membership over
    the full calendar day [00:00, 24:00) per date instead of the work window —
    window_cfg is ignored in that mode. None or "work_window" -> current
    windowed behavior (default, byte-identical to before this parameter existed).
    Has no effect for kind="flight".
    """
    kind = str(kind or "day").lower()
    provider = leads_provider or se_leads_for_manager
    mk = se_norm_key((row or {}).get("manager_key") or "")
    dbp = se_manager_db_path(row)
    calendar_day = kind == "day" and str(period_mode or "work_window") == "calendar_day"

    fetch_start = start
    windows: List[Tuple[datetime, datetime]] = []
    if kind == "flight":
        earliest = start
        for d in se_date_iter(start, end):
            ws, we = se_flight_window(
                d, manager_key=mk, schedule_aware=schedule_aware, db_path=dbp,
                window_cfg=window_cfg,
            )
            windows.append((ws, we))
            if ws.date() < earliest:
                earliest = ws.date()
        fetch_start = earliest
    elif calendar_day:
        for d in se_date_iter(start, end):
            base = datetime(d.year, d.month, d.day, tzinfo=SE_TZ)
            windows.append((base, base + timedelta(days=1)))
    else:
        for d in se_date_iter(start, end):
            windows.append(se_day_window(d, window_cfg=window_cfg))

    leads = provider(row, se_date_list(fetch_start, end))
    out: List[Dict[str, Any]] = []
    for lead in leads:
        dt = se_local_dt(lead)
        if not dt:
            continue
        for ws, we in windows:
            if ws <= dt < we:
                out.append(lead)
                break
    # R1A1-G (F-25): propagate the read-failure sentinel through the window
    # filter so callers still see "unavailable", not a filtered-down zero.
    if isinstance(leads, SeLeadsUnavailable):
        return SeLeadsUnavailable(out)
    return out


def se_light_body(
    managers: List[Dict[str, Any]],
    start: date,
    end: date,
    *,
    drop_duplicates: bool = False,
    leads_provider: Optional[Callable[[Dict[str, Any], List[str]], List[Dict[str, Any]]]] = None,
    identity_hook: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> str:
    """Lite body: calendar-day "Всего написавших / Дубликаты" per manager + total."""
    provider = leads_provider or se_leads_for_manager
    dates = se_date_list(start, end)
    lines: List[str] = []
    total_all = 0
    dup_all = 0
    for row in managers:
        leads_all = provider(row, dates)
        # R1A1-G (F-25): a read failure for this manager must render as an
        # explicit unavailable marker, never as a silent "Всего написавших: 0"
        # that looks identical to a genuinely quiet day.
        if isinstance(leads_all, SeLeadsUnavailable):
            lines.append(se_manager_label(row))
            lines.append("⚠ Данные недоступны (ошибка чтения БД)")
            lines.append("")
            continue
        if drop_duplicates:
            leads_all = [l for l in leads_all if not se_is_buyer_duplicate(l, identity_hook=identity_hook)]
        total = len(leads_all)
        dup = 0 if drop_duplicates else sum(1 for l in leads_all if se_duplicate_for_light(l))
        total_all += total
        dup_all += dup
        lines.append(se_manager_label(row))
        lines.append(f"Всего написавших: {total}")
        if not drop_duplicates:
            lines.append(f"Дубликаты: {dup}")
        lines.append("")
    lines.append("ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ")
    lines.append(f"Всего написавших: {total_all}")
    if not drop_duplicates:
        lines.append(f"Дубликаты: {dup_all}")
    return "\n".join(lines).rstrip()


def se_window_body(
    managers: List[Dict[str, Any]],
    start: date,
    end: date,
    kind: str,
    *,
    schedule_aware: bool = False,
    drop_duplicates: bool = False,
    leads_provider: Optional[Callable[[Dict[str, Any], List[str]], List[Dict[str, Any]]]] = None,
    identity_hook: Optional[Callable[[Dict[str, Any]], bool]] = None,
    window_cfg: Optional[Dict[str, Any]] = None,
    period_mode: Optional[str] = None,
) -> str:
    """Pro body for kind in {"day","flight"}: per-manager buckets + total + details.

    window_cfg / period_mode are threaded straight through to se_windowed_leads
    (see its docstring); both default to None, which reproduces the current
    work-window 08:00-17:00 / 17:00-08:00 behavior exactly.
    """
    lines: List[str] = []
    total_bucket = se_bucket_empty()
    all_countable: List[Dict[str, Any]] = []
    for row in managers:
        leads_all = se_windowed_leads(
            row, start, end, kind,
            schedule_aware=schedule_aware, leads_provider=leads_provider,
            window_cfg=window_cfg, period_mode=period_mode,
        )
        # R1A1-G (F-25): same rationale as se_light_body above.
        if isinstance(leads_all, SeLeadsUnavailable):
            lines.append(se_manager_label(row))
            lines.append("⚠ Данные недоступны (ошибка чтения БД)")
            lines.append("")
            continue
        if drop_duplicates:
            leads_all = [l for l in leads_all if not se_is_buyer_duplicate(l, identity_hook=identity_hook)]
        leads = [l for l in leads_all if se_countable(l)]
        b = se_bucket_empty()
        for lead in leads:
            se_bucket_add(b, lead)
            se_bucket_add(total_bucket, lead)
        all_countable.extend(leads)
        lines.append(se_manager_label(row))
        lines.extend(se_bucket_lines(b))
        lines.append("")
    lines.append("ИТОГО ПО ВСЕМ МЕНЕДЖЕРАМ")
    lines.extend(se_bucket_lines(total_bucket))
    details = se_details(all_countable)
    if details:
        lines.append("")
        lines.extend(details)
    return "\n".join(lines).rstrip()
