# -*- coding: utf-8 -*-
"""
Pure text/value formatting helpers moved out of main.py (extraction pass
#2, 2026-08-21). No I/O, no DB, no Telethon, no dependency on any
main.py-only global (e.g. TZ_KYIV, _tp_utc_now) -- every function here is a
stdlib-only leaf. main.py re-imports these under their original `_*` names
so every existing call site stays byte-identical.
"""

import re
from datetime import datetime, timezone
from typing import Any, Optional, Tuple


def tp_normalize_phone_plus(raw: Any) -> str:
    """TPILOT PHONE SELF-HEAL 20260719: canonical TPilot phone storage format
    is a leading '+' followed by digits -- the format phone-login already
    stores as-is (admin instructed "PHONE +79991234567"). Telethon's own
    `me.phone` is digits-only (no leading '+'); this adds one WITHOUT
    reformatting an already-'+'-prefixed value or touching digits otherwise,
    so it never creates a second, conflicting stored format. Never logs its
    input/output (pure string transform, no I/O)."""
    s = str(raw or "").strip()
    if not s:
        return ""
    return s if s.startswith("+") else f"+{s}"


def parse_cmd(text: str) -> Tuple[str, str]:
    t = (text or "").strip()
    if not t.startswith("/"):
        return "", ""
    parts = t.split(maxsplit=1)
    return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")


def display_username(username: str) -> str:
    u = str(username or "").strip()
    if not u:
        return "_"
    return u if u.startswith("@") else "@" + u


def full_name(first_name: str = "", last_name: str = "") -> str:
    return " ".join([x.strip() for x in [first_name or "", last_name or ""] if x and x.strip()]).strip()


def manager_label_from_row(row: dict) -> str:
    display_name = str((row or {}).get("display_name") or (row or {}).get("manager_key") or "").strip()
    username = str((row or {}).get("telegram_username") or "").strip()
    label = f"{display_name} | @{username}" if username else f"{display_name} | username:none"
    # DELETED MANAGER STATS RETENTION 20260711: mirrors the existing status=='deleted'
    # convention already present in stats_engine.se_manager_label. Only ever fires for
    # tombstone rows returned by _manager_rows_for_reporting (the only place that sets
    # status='deleted') -- every live managers row uses 'new'/'active'/'archived', so
    # this is a no-op for every other existing caller of this function.
    if str((row or {}).get("status") or "").strip() == "deleted":
        label += " (удалён)"
    return label


def manager_status_label(row: dict) -> str:
    status = str((row or {}).get("status") or "new")
    enabled = int((row or {}).get("is_enabled") or 0)
    manual_stopped = int((row or {}).get("manual_stopped") or 0)
    if not enabled:
        return f"{status}, disabled"
    if manual_stopped:
        return f"{status}, stopped"
    return status


def phone_clean(phone: str) -> str:
    p = str(phone or "").strip()
    if not p:
        return ""
    digits = re.sub(r"\D+", "", p)
    if not digits:
        return ""
    if p.startswith("+"):
        return "+" + digits
    if len(digits) >= 10:
        return "+" + digits
    return digits


def display_unknown(value: Any) -> str:
    v = str(value or "").strip()
    return v if v else "не определено"


# ---------------------------------------------------------------------------
# Naive-UTC datetime parsers + duration formatter (moved from main.py,
# extraction pass #3, 2026-08-22). Pure stdlib datetime/string logic only --
# no TZ_KYIV, no _kyiv_now(), no Telethon types, no main.py globals.
# ---------------------------------------------------------------------------

def parse_utc_naive(raw: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(raw or ""))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.replace(microsecond=0)
    except Exception:
        return None


def parse_utc_naive_dt(raw: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(raw or ""))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.replace(microsecond=0)
    except Exception:
        return None


def tp_utc_parse(raw: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(raw or ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0)
    except Exception:
        return None


def format_wait_duration(value: Any) -> str:
    try:
        n = float(value or 0)
    except Exception:
        n = 0.0
    minutes = int(round(n / 60.0)) if n > 600 else int(round(n))
    if minutes <= 0:
        return "0 мин"
    h, m = divmod(minutes, 60)
    d, h = divmod(h, 24)
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if m or not parts:
        parts.append(f"{m} мин")
    return " ".join(parts)
