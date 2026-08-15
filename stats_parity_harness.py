#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4B parity harness: compare stats_engine.py vs inlined reference logic.

READ-ONLY.  Never writes to any DB, never imports bot modules, never starts Telegram
clients or event loops.  Safe to run against a copied DB or (with care) against the
production DB.

WHY INLINE REFERENCES?
    partner_stat_bot.py and manager_bot.py both load .env at import time, raise
    RuntimeError when tokens are absent, and construct a TelegramClient at module
    level.  They cannot be imported without a live Telegram session.  The key bucket
    / window / override functions are therefore replicated verbatim here from their
    active definitions:

      REF-Partner: PartnerBot active defs (_tp_pdf_* + _psf3_*)
        - Lite  : _psf3_light_body   (line 3180, partner_stat_bot.py)
        - Pro   : _psf3_pro_body     (line 3364, active override)
        - Flight: _tp_pdf_flight_body (line 3390)
        Fetch range: start..end only (SEE NOTE BELOW)

      REF-Manager: ManagerBot active defs (_mbstat_*)
        - Flight: _mbstat_window_body (line 3831, manager_bot.py)
        Fetch range: start-1..end for flight (explicit widening at line 3840)

KNOWN DISCREPANCY (pre-Stage-4C):
    _tp_pdf_windowed_leads_for_manager calls _psf3_leads_for_manager(row, start, end)
    with the CALENDAR-DAY range of the report.  For flight(today), start=end=today, so
    only lead_date=today rows are fetched.  But the flight window is [D-1 17:00, D 08:00):
    leads arriving D-1 after 17:00 carry lead_date=D-1 and are MISSED by PartnerBot's
    fetch.  ManagerBot and the engine both widen the fetch by 1 day — they are correct.

    Consequence for this harness:
      - Engine vs REF-Manager flight  → expected PASS (same widened fetch, same window).
      - Engine vs REF-Partner flight  → expected MISMATCH when any D-1 17:00+ leads exist.

    The harness reports both comparisons; exit code is based on the engine-vs-manager
    comparison (the correct reference) unless --partner-only is passed.

Usage:
    python stats_parity_harness.py \\
        --db /path/to/data_tpilot.db \\
        --manager te \\
        --date 2026-06-16 \\
        --mode both \\
        --verbose

Exit: 0 = all primary comparisons PASS, 1 = mismatch, 2 = fatal setup error.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Import stats_engine (safe: stdlib only, zero import-time side effects).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

_ENGINE_AVAILABLE = False
_ENGINE_IMPORT_ERR = ""
try:
    import stats_engine as _se
    _ENGINE_AVAILABLE = True
except Exception as _exc:
    _ENGINE_IMPORT_ERR = repr(_exc)

# ---------------------------------------------------------------------------
# Shared timezone
# ---------------------------------------------------------------------------
_TZ = ZoneInfo("Europe/Kyiv")

# ---------------------------------------------------------------------------
# REF: inlined reference helpers (from PartnerBot + ManagerBot active defs)
# ---------------------------------------------------------------------------

def _ref_norm_key(raw: Any) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", str(raw or "").strip().lower().replace("ё", "е"))


def _ref_local_dt(lead: Dict[str, Any]) -> Optional[datetime]:
    raw_local = str((lead or {}).get("first_seen_kyiv") or "").strip()
    if raw_local:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw_local[:19], fmt).replace(tzinfo=_TZ)
            except Exception:
                pass
    raw_utc = str((lead or {}).get("first_seen_utc") or "").strip()
    if raw_utc:
        try:
            dt = datetime.fromisoformat(raw_utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(_TZ)
        except Exception:
            pass
    return None


def _ref_date_list(start: date, end: date) -> List[str]:
    out: List[str] = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


# --- override overlay (PartnerBot _psf3_apply_override_overlay verbatim) ---
_REF_OVERRIDE_CANON = ("liquid", "geo", "under18", "na", "trash")
_REF_OVERRIDE_STATUS = {"liquid": "liquid", "geo": "nonliquid", "under18": "nonliquid",
                        "trash": "trash", "na": "na"}

def _ref_override_bucket_value(raw: Any) -> str:
    v = str(raw or "").strip().lower()
    if v in _REF_OVERRIDE_CANON:
        return v
    if v == "age_missing":
        return "under18"
    if v == "geo_missing":
        return "na"
    return ""

def _ref_table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
        ).fetchone())
    except Exception:
        return False

def _ref_overrides(con: sqlite3.Connection, mk: str) -> Dict[int, Dict[str, Any]]:
    out: Dict[int, Dict[str, Any]] = {}
    if not mk or con is None or not _ref_table_exists(con, "lead_status_overrides"):
        return out
    try:
        for r in con.execute(
            "SELECT chat_id, status, bucket, reason FROM lead_status_overrides WHERE manager_key=?",
            (mk,),
        ).fetchall():
            cid = int(r["chat_id"] or 0) if r["chat_id"] else 0
            if cid <= 0:
                continue
            norm = _ref_override_bucket_value(r["bucket"]) or _ref_override_bucket_value(r["status"])
            if norm:
                out[cid] = {"bucket": norm, "reason": str(r["reason"] or ""), "status": str(r["status"] or "")}
    except Exception:
        pass
    return out

def _ref_apply_overlay(leads: List[Dict[str, Any]], ov: Dict[int, Dict[str, Any]]) -> None:
    if not leads or not ov:
        return
    for lead in leads:
        cid = int((lead or {}).get("chat_id") or 0) if (lead or {}).get("chat_id") else 0
        if cid <= 0:
            continue
        entry = ov.get(cid)
        if not entry or entry.get("bucket") not in _REF_OVERRIDE_CANON:
            continue
        lead["quality_bucket"] = entry["bucket"]
        lead["quality_status"] = _REF_OVERRIDE_STATUS.get(entry["bucket"], "na")
        if entry.get("reason"):
            lead["quality_reason"] = entry["reason"]

def _ref_fetch_leads(db_path: str, mk: str, dates: List[str]) -> List[Dict[str, Any]]:
    """Fetch daily_leads and apply overlay — mirrors _psf3_leads_for_manager."""
    if not db_path or not os.path.exists(db_path) or not dates:
        return []
    q = ",".join(["?"] * len(dates))
    con = sqlite3.connect(db_path, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT * FROM daily_leads WHERE lead_date IN ({q}) ORDER BY first_seen_utc ASC, id ASC",
            tuple(dates),
        ).fetchall()
        out: List[Dict[str, Any]] = [dict(r) for r in rows]
        for d in out:
            d["manager_key"] = _ref_norm_key(d.get("manager_key") or mk)
        _ref_apply_overlay(out, _ref_overrides(con, mk))
        return out
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


# --- window helpers ---
def _ref_day_window(d: date) -> Tuple[datetime, datetime]:
    b = datetime(d.year, d.month, d.day, tzinfo=_TZ)
    return b.replace(hour=8), b.replace(hour=17)

def _ref_flight_window(d: date) -> Tuple[datetime, datetime]:
    b = datetime(d.year, d.month, d.day, tzinfo=_TZ)
    prev = b - timedelta(days=1)
    return prev.replace(hour=17), b.replace(hour=8)


# --- bucket helpers (PartnerBot _psf3_bucket verbatim) ---
def _ref_age(lead: Dict[str, Any]) -> Optional[int]:
    try:
        raw = (lead or {}).get("age")
        return None if (raw is None or str(raw).strip() == "") else int(raw)
    except Exception:
        return None

def _ref_bucket(lead: Dict[str, Any]) -> str:
    qb = str((lead or {}).get("quality_bucket") or "").strip().lower()
    qs = str((lead or {}).get("quality_status") or "").strip().lower()
    reason = str((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or "").strip().lower()
    status = str((lead or {}).get("status") or "").strip().lower()
    country = str((lead or {}).get("country") or "").strip()
    age = _ref_age(lead)
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

def _ref_countable(lead: Dict[str, Any]) -> bool:
    try:
        if "lead_countable" in (lead or {}):
            return int((lead or {}).get("lead_countable") or 0) == 1
    except Exception:
        return False
    try:
        return int((lead or {}).get("duplicate") or 0) != 1
    except Exception:
        return True

def _ref_dup_for_light(lead: Dict[str, Any]) -> bool:
    kind = str((lead or {}).get("contact_kind") or "").strip().lower()
    reason = str((lead or {}).get("dedupe_reason") or "").strip().lower()
    try:
        if int((lead or {}).get("duplicate") or 0) == 1:
            return True
    except Exception:
        pass
    return kind == "duplicate" or "other_manager" in reason or "other manager" in reason

def _ref_bucket_empty() -> Dict[str, int]:
    return {"otpisok": 0, "nonliquid": 0, "geo": 0, "under18": 0, "na": 0, "trash": 0, "liquid": 0}

def _ref_bucket_add(b: Dict[str, int], lead: Dict[str, Any]) -> None:
    bkt = _ref_bucket(lead)
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


# ---------------------------------------------------------------------------
# REF-Admin: inlined AdminBot active counting logic (from main.py active defs)
# ---------------------------------------------------------------------------
# Sources verified in Stage 4E diagnostic (read-only audit of main.py):
#   _tp_ci_is_countable_lead  @ main.py:12842  — countable predicate
#   _tp_report_v5_raw_bucket  @ main.py:14528  — bucket resolver (raw evidence)
#   _tp_report_v5_bucket      @ main.py:14597  — bucket resolver (final)
#
# Differences from _ref_bucket / _ref_countable (PartnerBot / ManagerBot):
#   1. Countable: extra contact_kind ∈ {"","new"} gate when lead_countable absent.
#   2. Russia synonyms: рф/ru/rus/russia treated same as Россия (non-geo).
#   3. Trash: also checks lead["trash"] int column and мусор/трэш keywords.
#   4. Pending quality_bucket falls to raw-evidence resolver (no blanket "na").
#
# NOT REPLICATED:
#   _TP_REPORT_V5_ORIG_TP_QS_ROW_BUCKET — AdminBot captures an older quality-engine
#   function only available at AdminBot runtime.  When quality_bucket is a recognized
#   FINAL value, AdminBot tries this orig-engine first; we skip it and fall straight
#   to quality_bucket.  Leads where the two disagree appear here as
#   reason=bucket_rule:orig_engine_skip (potential false positives).

_ADMIN_FINAL_QB: frozenset = frozenset({
    "liquid", "geo", "under18", "age_missing", "trash", "na",
    "geo_missing", "age_and_geo_missing", "unknown", "unclear",
})
_ADMIN_RUSSIA_SYNS: frozenset = frozenset({"россия", "рф", "ru", "rus", "russia"})


def _admin_countable(lead: Dict[str, Any]) -> bool:
    """Inline of _tp_ci_is_countable_lead @ main.py:12842.

    Extra gate vs _ref_countable: when lead_countable absent and duplicate!=1,
    also requires contact_kind ∈ {"", "new"}.
    """
    try:
        if "lead_countable" in (lead or {}):
            return int((lead or {}).get("lead_countable") or 0) == 1
    except Exception:
        pass
    try:
        if int((lead or {}).get("duplicate") or 0) == 1:
            return False
    except Exception:
        pass
    kind_val = str((lead or {}).get("contact_kind") or "new").strip().lower()
    return kind_val in ("", "new")


def _admin_bucket_raw(lead: Dict[str, Any]) -> str:
    """Inline of _tp_report_v5_raw_bucket @ main.py:14528.

    orig-engine indirection skipped (harness limitation; see section header).
    Key differences vs _ref_bucket: Russia synonyms, trash column, мусор/трэш,
    and pending quality_bucket falls through to raw-evidence resolution.
    """
    qb_norm = str((lead or {}).get("quality_bucket") or "").strip().lower()
    if qb_norm in _ADMIN_FINAL_QB:
        # _TP_REPORT_V5_ORIG_TP_QS_ROW_BUCKET indirection NOT replicated
        if qb_norm in ("age_and_geo_missing", "unknown", "unclear"):
            return "na"
        return qb_norm  # liquid/geo/under18/age_missing/trash/na/geo_missing pass through
    # Pending/non-final quality_bucket: resolve from raw evidence
    status = str((lead or {}).get("status") or "").strip().lower()
    reason = str(
        (lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason") or ""
    ).strip().lower()
    country = str((lead or {}).get("country") or "").strip()
    country_low = country.lower()
    try:
        age_raw = (lead or {}).get("age")
        age: Optional[int] = (
            None if (age_raw is None or str(age_raw).strip() == "") else int(age_raw)
        )
    except Exception:
        age = None
    combo_ts = f"{reason} {status}"
    if (
        int((lead or {}).get("trash") or 0) == 1
        or "trash" in combo_ts
        or "blocked" in combo_ts
        or "send_failed" in combo_ts
        or "inaccessible" in combo_ts
        or "недоступ" in combo_ts
        or "мусор" in combo_ts
        or "трэш" in combo_ts
    ):
        return "trash"
    if age is not None and age < 18:
        return "under18"
    if country and country_low not in _ADMIN_RUSSIA_SYNS:
        return "geo"
    combo = f"{reason} {status}"
    if "age_missing" in combo or "нет 18" in combo or "under18" in combo:
        return "under18"
    return "na"


def _admin_bucket(lead: Dict[str, Any]) -> str:
    """Inline of _tp_report_v5_bucket @ main.py:14597."""
    b = _admin_bucket_raw(lead)
    if b == "age_missing":
        return "under18"
    if b == "geo_missing":
        return "na"
    if b in ("liquid", "geo", "under18", "trash", "na"):
        return b
    return "na"


def _admin_bkt_add(b: Dict[str, int], bkt: str) -> None:
    """Add pre-computed bucket string to a _ref_bucket_empty-format counter."""
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


# ---------------------------------------------------------------------------
# REF counter builders
# ---------------------------------------------------------------------------

def _ref_lite_counters(rows: List[Dict[str, Any]], d: date) -> Dict[str, Any]:
    """REF light: calendar-day total + dups (PartnerBot _psf3_light_body style)."""
    dates = _ref_date_list(d, d)
    result: Dict[str, Any] = {}
    total_all = dup_all = 0
    for row in rows:
        mk = _ref_norm_key(row.get("manager_key") or "")
        leads = _ref_fetch_leads(str(row.get("db_path") or ""), mk, dates)
        total = len(leads)
        dup = sum(1 for l in leads if _ref_dup_for_light(l))
        total_all += total
        dup_all += dup
        result[mk] = {"total": total, "dup": dup}
    result["TOTAL"] = {"total": total_all, "dup": dup_all}
    return result


def _ref_window_counters(
    rows: List[Dict[str, Any]], d: date, kind: str, *, widen_fetch: bool
) -> Dict[str, Any]:
    """REF pro/flight bucket counters.

    widen_fetch=False → PartnerBot style (fetch target_date only; known to miss D-1 leads).
    widen_fetch=True  → ManagerBot style (fetch target_date-1 for flight; correct).
    """
    kind = kind.lower()
    if kind == "flight":
        ws, we = _ref_flight_window(d)
        fetch_start = (d - timedelta(days=1)) if widen_fetch else d
    else:
        ws, we = _ref_day_window(d)
        fetch_start = d
    dates = _ref_date_list(fetch_start, d)
    result: Dict[str, Any] = {}
    total_b = _ref_bucket_empty()
    for row in rows:
        mk = _ref_norm_key(row.get("manager_key") or "")
        leads_all = _ref_fetch_leads(str(row.get("db_path") or ""), mk, dates)
        b = _ref_bucket_empty()
        for lead in leads_all:
            dt = _ref_local_dt(lead)
            if not dt or not (ws <= dt < we):
                continue
            if not _ref_countable(lead):
                continue
            _ref_bucket_add(b, lead)
            _ref_bucket_add(total_b, lead)
        result[mk] = b
    result["TOTAL"] = total_b
    return result


# ---------------------------------------------------------------------------
# ENGINE counter builders
# ---------------------------------------------------------------------------

def _engine_lite_counters(rows: List[Dict[str, Any]], d: date) -> Dict[str, Any]:
    dates = _se.se_date_list(d, d)
    result: Dict[str, Any] = {}
    total_all = dup_all = 0
    for row in rows:
        mk = _se.se_norm_key(row.get("manager_key") or "")
        leads = _se.se_leads_for_manager(row, dates)
        total = len(leads)
        dup = sum(1 for l in leads if _se.se_duplicate_for_light(l))
        total_all += total
        dup_all += dup
        result[mk] = {"total": total, "dup": dup}
    result["TOTAL"] = {"total": total_all, "dup": dup_all}
    return result


def _engine_window_counters(rows: List[Dict[str, Any]], d: date, kind: str) -> Dict[str, Any]:
    kind = kind.lower()
    result: Dict[str, Any] = {}
    total_b = _se.se_bucket_empty()
    for row in rows:
        mk = _se.se_norm_key(row.get("manager_key") or "")
        leads = _se.se_windowed_leads(row, d, d, kind, schedule_aware=False)
        leads = [l for l in leads if _se.se_countable(l)]
        b = _se.se_bucket_empty()
        for lead in leads:
            _se.se_bucket_add(b, lead)
            _se.se_bucket_add(total_b, lead)
        result[mk] = dict(b)
    result["TOTAL"] = dict(total_b)
    return result


# ---------------------------------------------------------------------------
# Admin vs Engine detailed collectors and comparison
# ---------------------------------------------------------------------------

def _admin_ref_collect(
    rows: List[Dict[str, Any]], d: date, kind: str
) -> Tuple[Dict[str, Any], Dict[str, Dict[int, Tuple[Dict[str, Any], str]]]]:
    """REF-Admin: collect window-filtered countable leads with admin buckets.

    Returns:
      counters  : {mk: bucket_dict, "TOTAL": bucket_dict}   (_ref_bucket_empty format)
      leads_map : {mk: {chat_id: (lead_dict, bucket_str)}}

    Overlay applied inside _ref_fetch_leads (per-manager DB); equivalent to
    AdminBot's _tp_apply_override_overlay for single-manager tests.
    """
    kind = kind.lower()
    if kind == "flight":
        ws, we = _ref_flight_window(d)
        fetch_start = d - timedelta(days=1)
    else:
        ws, we = _ref_day_window(d)
        fetch_start = d
    dates = _ref_date_list(fetch_start, d)
    counters: Dict[str, Any] = {}
    leads_map: Dict[str, Dict[int, Tuple[Dict[str, Any], str]]] = {}
    total_b: Dict[str, int] = _ref_bucket_empty()
    for row in rows:
        mk = _ref_norm_key(row.get("manager_key") or "")
        leads_all = _ref_fetch_leads(str(row.get("db_path") or ""), mk, dates)
        b: Dict[str, int] = _ref_bucket_empty()
        mk_map: Dict[int, Tuple[Dict[str, Any], str]] = {}
        for lead in leads_all:
            dt = _ref_local_dt(lead)
            if not dt or not (ws <= dt < we):
                continue
            if not _admin_countable(lead):
                continue
            bkt = _admin_bucket(lead)
            _admin_bkt_add(b, bkt)
            _admin_bkt_add(total_b, bkt)
            cid = int(lead.get("chat_id") or 0)
            if cid > 0:
                mk_map[cid] = (lead, bkt)
        counters[mk] = b
        leads_map[mk] = mk_map
    counters["TOTAL"] = total_b
    return counters, leads_map


def _engine_collect(
    rows: List[Dict[str, Any]], d: date, kind: str
) -> Tuple[Dict[str, Any], Dict[str, Dict[int, Tuple[Dict[str, Any], str]]]]:
    """Engine: collect window-filtered countable leads with engine buckets.

    Returns same structure as _admin_ref_collect for direct diff.
    """
    counters: Dict[str, Any] = {}
    leads_map: Dict[str, Dict[int, Tuple[Dict[str, Any], str]]] = {}
    total_b = _se.se_bucket_empty()
    for row in rows:
        mk = _se.se_norm_key(row.get("manager_key") or "")
        windowed = _se.se_windowed_leads(row, d, d, kind, schedule_aware=False)
        countable = [l for l in windowed if _se.se_countable(l)]
        b = _se.se_bucket_empty()
        mk_map: Dict[int, Tuple[Dict[str, Any], str]] = {}
        for lead in countable:
            _se.se_bucket_add(b, lead)
            _se.se_bucket_add(total_b, lead)
            cid = int(lead.get("chat_id") or 0)
            if cid > 0:
                mk_map[cid] = (lead, _se.se_bucket(lead))
        counters[mk] = dict(b)
        leads_map[mk] = mk_map
    counters["TOTAL"] = dict(total_b)
    return counters, leads_map


def _categorize_diff(
    lead: Dict[str, Any],
    admin_bkt: Optional[str],
    eng_bkt: Optional[str],
    direction: str,
) -> str:
    """Classify a per-lead REF-Admin vs Engine divergence.

    direction: "admin_only" | "engine_only" | "bucket_diff"
    Returns a reason string suitable for aggregation and display.
    """
    if direction == "admin_only":
        # Admin counted it; check why engine wouldn't (engine uses _ref_countable logic)
        if _ENGINE_AVAILABLE and not _se.se_countable(lead):
            return "unknown"  # engine countable also says no — shouldn't reach here
        if "lead_countable" not in lead:
            kind_val = str(lead.get("contact_kind") or "new").strip().lower()
            if kind_val not in ("", "new"):
                return f"countable_rule:contact_kind={kind_val!r}"
        return "countable_rule"
    if direction == "engine_only":
        # Engine counted it; check why admin wouldn't
        if not _admin_countable(lead):
            if "lead_countable" not in lead:
                kind_val = str(lead.get("contact_kind") or "new").strip().lower()
                if int(lead.get("duplicate") or 0) == 0 and kind_val not in ("", "new"):
                    return f"countable_rule:contact_kind={kind_val!r}"
            return "countable_rule"
        return "unknown"
    # direction == "bucket_diff"
    country = str(lead.get("country") or "").strip()
    country_low = country.lower()
    qb = str(lead.get("quality_bucket") or "").strip().lower()
    status_reason = (
        str(lead.get("quality_reason") or "") + " " + str(lead.get("status") or "")
    ).lower()
    if country and country_low in ("рф", "ru", "rus", "russia") and eng_bkt == "geo" and admin_bkt != "geo":
        return "bucket_rule:russia_synonym"
    if int(lead.get("trash") or 0) == 1 and admin_bkt == "trash" and eng_bkt != "trash":
        return "bucket_rule:trash_col"
    if (
        any(kw in status_reason for kw in ("мусор", "трэш"))
        and admin_bkt == "trash"
        and eng_bkt != "trash"
    ):
        return "bucket_rule:trash_ru_synonym"
    if qb and qb not in _ADMIN_FINAL_QB:
        return f"bucket_rule:pending_qb={qb!r}"
    if qb in _ADMIN_FINAL_QB:
        return "bucket_rule:orig_engine_skip"
    return "bucket_rule"


def _compare_admin_vs_engine(
    label: str,
    ref_ctr: Dict[str, Any],
    eng_ctr: Dict[str, Any],
    ref_map: Dict[int, Tuple[Dict[str, Any], str]],
    eng_map: Dict[int, Tuple[Dict[str, Any], str]],
    verbose: bool,
) -> bool:
    """Compare REF-Admin vs Engine for one manager; print per-lead diffs on mismatch."""
    if ref_ctr == eng_ctr:
        print(f"  PASS  {label}")
        if verbose:
            for k, v in sorted(ref_ctr.items()):
                print(f"        {k}: {v}")
        return True
    print(f"  FAIL  {label}")
    for k in sorted(set(ref_ctr) | set(eng_ctr)):
        rv, ev = ref_ctr.get(k), eng_ctr.get(k)
        if rv != ev:
            print(f"    MISMATCH [{k}]  REF-Admin={rv!r}  Engine={ev!r}")
        elif verbose:
            print(f"    MATCH   [{k}]  {rv!r}")
    # Per-lead diagnostics
    ref_ids = set(ref_map.keys())
    eng_ids = set(eng_map.keys())
    diffs: List[Tuple[str, int, Optional[str], Optional[str]]] = []
    for cid in sorted(ref_ids - eng_ids):
        lead, bkt = ref_map[cid]
        diffs.append((_categorize_diff(lead, bkt, None, "admin_only"), cid, bkt, None))
    for cid in sorted(eng_ids - ref_ids):
        lead, bkt = eng_map[cid]
        diffs.append((_categorize_diff(lead, None, bkt, "engine_only"), cid, None, bkt))
    for cid in sorted(ref_ids & eng_ids):
        ref_lead, ref_bkt = ref_map[cid]
        _, eng_bkt = eng_map[cid]
        if ref_bkt == eng_bkt:
            continue
        diffs.append((_categorize_diff(ref_lead, ref_bkt, eng_bkt, "bucket_diff"), cid, ref_bkt, eng_bkt))
    if diffs:
        limit = 30
        print(f"    --- diverging leads (showing up to {limit} of {len(diffs)}) ---")
        for reason, cid, abkt, ebkt in diffs[:limit]:
            print(f"      chat_id={cid}  reason={reason}  admin_bkt={abkt!r}  engine_bkt={ebkt!r}")
        reason_counts: Counter = Counter(r for r, _, _, _ in diffs)
        print("    --- reason summary ---")
        for reason, count in reason_counts.most_common():
            print(f"      {reason}: {count} lead(s)")
    return False


# ---------------------------------------------------------------------------
# Comparison / printing
# ---------------------------------------------------------------------------

def _compare(label: str, ref: Dict, eng: Dict, verbose: bool) -> bool:
    if ref == eng:
        print(f"  PASS  {label}")
        if verbose:
            for k, v in sorted(ref.items()):
                print(f"        {k}: {v}")
        return True
    print(f"  FAIL  {label}")
    for k in sorted(set(ref) | set(eng)):
        rv = ref.get(k)
        ev = eng.get(k)
        if rv != ev:
            print(f"    MISMATCH [{k}]  REF={rv!r}  ENG={ev!r}")
        elif verbose:
            print(f"    MATCH   [{k}]  {rv!r}")
    return False


def _load_manager_row(main_db: str, manager_key: str) -> Optional[Dict[str, Any]]:
    mk = _ref_norm_key(manager_key)
    if not mk:
        return None
    try:
        con = sqlite3.connect(main_db, timeout=20)
        con.row_factory = sqlite3.Row
        try:
            r = con.execute("SELECT * FROM managers WHERE manager_key=? LIMIT 1", (mk,)).fetchone()
            if not r:
                return None
            row = dict(r)
        finally:
            con.close()
    except Exception as exc:
        print(f"[ERROR] Cannot read managers table: {exc!r}", file=sys.stderr)
        return None
    row["manager_key"] = mk
    # Resolve per-manager DB path
    raw = str(row.get("db_path") or "").strip()
    if raw:
        p = Path(raw)
        if not p.is_absolute():
            p = (Path(main_db).resolve().parent.parent / raw).resolve()
        row["db_path"] = str(p)
    else:
        base = Path(main_db).resolve().parent.parent
        row["db_path"] = str(base / "runtime" / "managers" / mk / f"{mk}.db")
    return row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Stage 4B parity harness — read-only comparison of stats_engine vs bot reference logic."
    )
    ap.add_argument("--db", required=True, help="Path to TPILOT central DB (data_tpilot.db)")
    ap.add_argument("--manager", required=True, help="Manager key, e.g. te")
    ap.add_argument("--date", required=True, help="Target date YYYY-MM-DD")
    ap.add_argument("--mode", default="both",
                    choices=["lite", "pro", "flight", "both"],
                    help="lite=light/calendar, pro=day 08-17, flight=night 17-08, both=all three")
    ap.add_argument("--partner-only", action="store_true",
                    help="Use only PartnerBot reference (skip ManagerBot-style flight comparison)")
    ap.add_argument("--schedule-aware", action="store_true",
                    help="Enable schedule-aware dolyoty (Stage 4F — not yet proven for parity)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print all counters, not just mismatches")
    ap.add_argument("--limit", type=int, default=0, help="Reserved; unused in Stage 4B")
    ap.add_argument(
        "--admin", action="store_true",
        help=(
            "Add REF-Admin vs Engine comparison.  Inlines AdminBot active counting logic "
            "(_tp_ci_is_countable_lead, _tp_report_v5_{raw_,}bucket) from main.py audit.  "
            "Requires --mode pro, flight, or both (AdminBot has no Lite mode).  "
            "Reports divergences categorised as: countable_rule, bucket_rule:russia_synonym, "
            "bucket_rule:trash_col, bucket_rule:trash_ru_synonym, bucket_rule:pending_qb, "
            "bucket_rule:orig_engine_skip, unknown."
        ),
    )
    args = ap.parse_args()

    if not _ENGINE_AVAILABLE:
        print(f"[FATAL] stats_engine not importable: {_ENGINE_IMPORT_ERR}", file=sys.stderr)
        return 2

    if args.schedule_aware:
        print("[NOTE] --schedule-aware is Stage 4F territory; parity not yet proven for non-consecutive days.\n")

    try:
        d = date.fromisoformat(args.date)
    except Exception:
        print(f"[FATAL] Invalid date: {args.date!r}", file=sys.stderr)
        return 2

    main_db = os.path.abspath(args.db)
    if not os.path.exists(main_db):
        print(f"[FATAL] DB not found: {main_db}", file=sys.stderr)
        return 2

    row = _load_manager_row(main_db, args.manager)
    if row is None:
        print(f"[FATAL] Manager {args.manager!r} not found in managers table.", file=sys.stderr)
        return 2

    mk = str(row.get("manager_key") or args.manager)
    db_path = str(row.get("db_path") or "")
    db_exists = os.path.exists(db_path)

    print(f"Manager  : {mk}")
    print(f"CentralDB: {main_db}")
    print(f"MgrDB    : {db_path}  {'OK' if db_exists else 'MISSING — all counts will be 0'}")
    print(f"Date     : {d}  (prev day for flight fetch: {d - timedelta(days=1)})")
    print(f"Mode     : {args.mode}")
    print()

    if not db_exists:
        print("[WARN] Per-manager DB not found; all REF and ENGINE counters will be 0 — PASS trivially.", file=sys.stderr)

    mgr_rows = [row]
    admin_pass: Optional[bool] = None   # set only when --admin is requested
    primary_pass = True   # combined gate: manager comparisons + admin (when --admin)
    manager_pass = True   # manager comparisons only (never polluted by admin result)
    partner_pass = True   # engine vs partner-style (informational; FAIL expected for flight if D-1 leads exist)

    # ---- LITE ----------------------------------------------------------------
    if args.mode in ("lite", "both"):
        print("=== LITE (calendar-day total / dups) ===")
        ref_l = _ref_lite_counters(mgr_rows, d)
        eng_l = _engine_lite_counters(mgr_rows, d)
        ok_mk = _compare(f"Lite/{mk}   [REF-Partner vs Engine]", ref_l.get(mk, {}), eng_l.get(mk, {}), args.verbose)
        ok_tot = _compare("Lite/TOTAL [REF-Partner vs Engine]", ref_l.get("TOTAL", {}), eng_l.get("TOTAL", {}), args.verbose)
        primary_pass = primary_pass and ok_mk and ok_tot
        manager_pass = manager_pass and ok_mk and ok_tot
        partner_pass = partner_pass and ok_mk and ok_tot
        print()

    # ---- PRO (day window 08:00-17:00) ----------------------------------------
    if args.mode in ("pro", "both"):
        print("=== PRO / DAY (window 08:00-17:00) ===")
        ref_p = _ref_window_counters(mgr_rows, d, "day", widen_fetch=False)  # fetch=target_date only
        eng_p = _engine_window_counters(mgr_rows, d, "day")
        ok_mk = _compare(f"Pro-day/{mk}   [REF-Partner vs Engine]", ref_p.get(mk, {}), eng_p.get(mk, {}), args.verbose)
        ok_tot = _compare("Pro-day/TOTAL [REF-Partner vs Engine]", ref_p.get("TOTAL", {}), eng_p.get("TOTAL", {}), args.verbose)
        primary_pass = primary_pass and ok_mk and ok_tot
        manager_pass = manager_pass and ok_mk and ok_tot
        partner_pass = partner_pass and ok_mk and ok_tot
        print()

    # ---- FLIGHT (night window D-1 17:00 -> D 08:00) --------------------------
    if args.mode in ("flight", "both"):
        print("=== FLIGHT / NIGHT (window D-1 17:00 -> D 08:00) ===")
        eng_f = _engine_window_counters(mgr_rows, d, "flight")

        # PartnerBot-style: fetch target_date only (may miss D-1 leads — see module docstring)
        ref_partner_f = _ref_window_counters(mgr_rows, d, "flight", widen_fetch=False)
        ok_p_mk = _compare(
            f"Flight/{mk}   [REF-Partner vs Engine]  (fetch=target_date only — may miss D-1 17:00+ leads)",
            ref_partner_f.get(mk, {}), eng_f.get(mk, {}), args.verbose,
        )
        ok_p_tot = _compare(
            "Flight/TOTAL [REF-Partner vs Engine]",
            ref_partner_f.get("TOTAL", {}), eng_f.get("TOTAL", {}), args.verbose,
        )
        partner_pass = partner_pass and ok_p_mk and ok_p_tot

        if not args.partner_only:
            # ManagerBot-style: widen fetch to D-1 (correct for D-1 17:00+ leads)
            ref_mgr_f = _ref_window_counters(mgr_rows, d, "flight", widen_fetch=True)
            ok_m_mk = _compare(
                f"Flight/{mk}   [REF-Manager vs Engine]  (fetch=D-1+D; correct reference)",
                ref_mgr_f.get(mk, {}), eng_f.get(mk, {}), args.verbose,
            )
            ok_m_tot = _compare(
                "Flight/TOTAL [REF-Manager vs Engine]",
                ref_mgr_f.get("TOTAL", {}), eng_f.get("TOTAL", {}), args.verbose,
            )
            primary_pass = primary_pass and ok_m_mk and ok_m_tot
            manager_pass = manager_pass and ok_m_mk and ok_m_tot

            if not (ok_p_mk and ok_p_tot) and (ok_m_mk and ok_m_tot):
                print("  [INFO] REF-Partner FAIL but REF-Manager PASS: PartnerBot flight fetch bug confirmed")
                print("         (PartnerBot misses leads with lead_date=D-1 arriving after 17:00).")
                print("         Stage 4C wiring PartnerBot to engine will FIX this discrepancy.")
        else:
            primary_pass = primary_pass and ok_p_mk and ok_p_tot

        print()

    # ---- ManagerBot override note (informational) ----------------------------
    if not args.partner_only and args.mode in ("pro", "flight", "both"):
        print("--- ManagerBot override note (informational) ---")
        print("  ManagerBot reads lead_status_overrides in a SEPARATE fetch (_mbstat_overrides_for_manager),")
        print("  then resolves bucket via _mbstat_resolve_bucket(lead, ov) — override passed as argument.")
        print("  PartnerBot / engine mutate lead['quality_bucket'] in-place before bucket functions run.")
        print("  For canonical override.bucket values both approaches produce the same bucket string.")
        print("  Full ManagerBot import is SKIPPED (unsafe: loads .env, creates TelegramClient at module level).")
        print()

    # ---- ADMIN comparison (--admin flag) -------------------------------------
    if args.admin:
        print("=== REF-Admin vs Engine (Stage 4E Step 1) ===")
        print("  Inlined: _tp_ci_is_countable_lead @ main.py:12842")
        print("           _tp_report_v5_raw_bucket  @ main.py:14528")
        print("           _tp_report_v5_bucket      @ main.py:14597")
        print("  Window : backward anchor D-1 17:00→D 08:00 (same as active _window_for_kind_date@16859)")
        print("  Overlay: applied via _ref_fetch_leads (per-manager, equivalent for single-manager test)")
        print("  Limitation: _TP_REPORT_V5_ORIG_TP_QS_ROW_BUCKET indirection NOT replicated.")
        print("    Leads where orig-engine disagrees with quality_bucket → reason=bucket_rule:orig_engine_skip")
        print("  Window limitation: _lead_belongs_to_window presence-carry branch (main.py:5315) NOT replicated.")
        print("    REF-Admin uses clean backward anchor only; window-boundary divergences are NOT measured.")
        print()
        admin_pass = True
        if args.mode not in ("pro", "flight", "both"):
            print("  [INFO] AdminBot has no Lite mode — --admin requires --mode pro, flight, or both.")
            admin_pass = None  # not applicable, don't penalise
        else:
            if args.mode in ("pro", "both"):
                print("-- REF-Admin day (08:00-17:00) --")
                ref_a_d, ref_a_d_map = _admin_ref_collect(mgr_rows, d, "day")
                eng_a_d, eng_a_d_map = _engine_collect(mgr_rows, d, "day")
                ok_mk = _compare_admin_vs_engine(
                    f"Admin-day/{mk}",
                    ref_a_d.get(mk, _ref_bucket_empty()),
                    eng_a_d.get(mk, _se.se_bucket_empty()),
                    ref_a_d_map.get(mk, {}),
                    eng_a_d_map.get(mk, {}),
                    args.verbose,
                )
                ok_tot = _compare(
                    "Admin-day/TOTAL [counters only]",
                    ref_a_d.get("TOTAL", _ref_bucket_empty()),
                    eng_a_d.get("TOTAL", _se.se_bucket_empty()),
                    args.verbose,
                )
                admin_pass = admin_pass and ok_mk and ok_tot
                print()

            if args.mode in ("flight", "both"):
                print("-- REF-Admin flight (D-1 17:00 -> D 08:00) --")
                ref_a_f, ref_a_f_map = _admin_ref_collect(mgr_rows, d, "flight")
                eng_a_f, eng_a_f_map = _engine_collect(mgr_rows, d, "flight")
                ok_mk = _compare_admin_vs_engine(
                    f"Admin-flight/{mk}",
                    ref_a_f.get(mk, _ref_bucket_empty()),
                    eng_a_f.get(mk, _se.se_bucket_empty()),
                    ref_a_f_map.get(mk, {}),
                    eng_a_f_map.get(mk, {}),
                    args.verbose,
                )
                ok_tot = _compare(
                    "Admin-flight/TOTAL [counters only]",
                    ref_a_f.get("TOTAL", _ref_bucket_empty()),
                    eng_a_f.get("TOTAL", _se.se_bucket_empty()),
                    args.verbose,
                )
                admin_pass = admin_pass and ok_mk and ok_tot
                print()

        if admin_pass is not None:
            primary_pass = primary_pass and admin_pass

    # ---- Summary --------------------------------------------------------------
    print("=" * 60)
    if args.partner_only:
        verdict = "PASS" if partner_pass else "FAIL"
        print(f"OVERALL (partner-only): {verdict}")
        return 0 if partner_pass else 1
    else:
        p_label = "PASS" if partner_pass else "FAIL"
        m_label = "PASS" if manager_pass else "FAIL"
        print(f"REF-Partner vs Engine : {p_label}  (informational for flight)")
        print(f"REF-Manager  vs Engine: {m_label}  (manager parity gate)")
        if admin_pass is not None:
            a_label = "PASS" if admin_pass else "FAIL"
            print(f"REF-Admin   vs Engine: {a_label}  (--admin; included in overall gate)")
        else:
            print("REF-Admin   vs Engine: SKIP  (pass --admin to enable)")
        gate_parts = "manager" + (" + admin" if admin_pass is not None else "")
        o_label = "PASS" if primary_pass else "FAIL"
        print(f"Overall gate          : {o_label}  ({gate_parts})")
        return 0 if primary_pass else 1


if __name__ == "__main__":
    sys.exit(main())
