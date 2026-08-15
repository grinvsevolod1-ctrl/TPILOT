#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 4E parity check: AdminBot detailed-stat bucket vs stats_engine.se_bucket.

READ-ONLY.  Never writes to any DB, never imports bot modules (main.py / panel_bot.py /
manager_bot.py load .env and build a TelegramClient at import time and cannot be imported
without a live session).  Safe to run against a COPIED DB.  Do NOT point it at the live
production DB while bots are running -- copy data_tpilot.db (and the per-manager DBs it
references) to a temp location first.

WHAT IT ANSWERS
    If AdminBot's detailed stats (/stat det, /flight) switched their per-lead bucket label
    from the active classifier `_tp_report_v5_bucket` (main.py:15410) to the engine's
    `se_bucket` (stats_engine.py), would the DISPLAYED numbers change?

    It re-derives both bucket labels for every lead inside the AdminBot day window
    (08:00-17:00) and flight window (D-1 17:00 -> D 08:00, schedule_aware=False, no
    window_cfg), per manager, over a range of recent dates, and reports:
      * line-level aggregate deltas (ОТПИСОК/НЕЛИКВИД/ГЕО/-18/NA/TRASH/ЛИКВИД)
      * the count of leads whose label differs, with sample chat_ids.

    Exit 0 = zero disagreements (safe to flip _SE4E_ENGINE_BUCKET_LIVE).
    Exit 1 = at least one disagreement (review before flipping).
    Exit 2 = fatal setup error.

FAITHFULNESS NOTE
    `_tp_report_v5_raw_bucket` consults the original quality engine
    (_TP_REPORT_V5_ORIG_TP_QS_ROW_BUCKET) for FINAL quality_bucket values.  Per main.py's
    own documented invariant (lines 15343-15344) that chain "itself echoes quality_bucket
    verbatim", so this replica uses the normalized quality_bucket directly for final values.
    If a future change makes the quality engine RE-classify final buckets, this replica
    could diverge from the live classifier; the gate stays conservative because the live
    path remains flag-OFF until a human reviews the result.

Usage:
    python tools/stage4e_bucket_parity.py --db /path/to/COPY/data_tpilot.db --days 7
    python tools/stage4e_bucket_parity.py --db ... --date 2026-06-20 --kind both --verbose
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Import stats_engine (stdlib only, zero import-time side effects).
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for _p in (str(_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import stats_engine as _se
except Exception as _exc:  # pragma: no cover
    print(f"FATAL: cannot import stats_engine: {_exc!r}", file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------- #
# REF: inlined replica of AdminBot's active bucket classifier
#   _tp_report_v5_bucket (main.py:15410) + _tp_report_v5_raw_bucket (main.py:15341)
#   + helpers (main.py:15246-15267).  Replicated verbatim except the orig-quality
#   echo (see FAITHFULNESS NOTE above).
# --------------------------------------------------------------------------- #
def _v5_text(raw: Any) -> str:
    return str(raw or "").strip()


def _v5_low(raw: Any) -> str:
    return _v5_text(raw).lower().replace("ё", "е")  # ё -> е


def _v5_int(raw: Any, default: int = 0) -> int:
    try:
        return int(raw)
    except Exception:
        return int(default)


def _v5_age(raw: Any) -> Optional[int]:
    try:
        if raw is None or str(raw).strip() == "":
            return None
        return int(float(str(raw).strip()))
    except Exception:
        return None


_V5_FINAL_QB = {
    "liquid": "liquid",
    "geo": "geo",
    "under18": "under18",
    "age_missing": "age_missing",
    "trash": "trash",
    "na": "na",
    "geo_missing": "geo_missing",
    "age_and_geo_missing": "na",
    "unknown": "na",
    "unclear": "na",
}


def _v5_raw_bucket(lead: Dict[str, Any]) -> str:
    qb_norm = _v5_low((lead or {}).get("quality_bucket"))
    if qb_norm in _V5_FINAL_QB:
        # Live code calls the orig quality engine here; per its documented invariant the
        # orig echoes quality_bucket verbatim, so we use the cached value directly.
        b = _v5_text((lead or {}).get("quality_bucket"))
        if b:
            return b
    # Pending / non-final quality_bucket: resolve from raw parsed evidence (no liquid here).
    status = _v5_low((lead or {}).get("status"))
    reason = _v5_low((lead or {}).get("quality_reason") or (lead or {}).get("nonliquid_reason"))
    country = _v5_text((lead or {}).get("country"))
    country_low = _v5_low(country)
    age = _v5_age((lead or {}).get("age"))
    combo_ts = f"{reason} {status}"
    if (
        _v5_int((lead or {}).get("trash")) == 1
        or "trash" in combo_ts
        or "blocked" in combo_ts
        or "send_failed" in combo_ts
        or "inaccessible" in combo_ts
        or "недоступ" in combo_ts   # недоступ
        or "мусор" in combo_ts                    # мусор
        or "трэш" in combo_ts                          # трэш
    ):
        return "trash"
    if age is not None and age < 18:
        return "under18"
    if country and country_low not in ("россия", "рф", "ru", "rus", "russia"):
        return "geo"
    combo = f"{reason} {status}"
    if "age_missing" in combo or "нет 18" in combo or "under18" in combo:
        return "under18"
    return "na"


def _v5_bucket(lead: Dict[str, Any]) -> str:
    b = _v5_raw_bucket(lead)
    if b == "age_missing":
        return "under18"
    if b == "geo_missing":
        return "na"
    if b in ("liquid", "geo", "under18", "trash", "na"):
        return b
    return "na"


# --------------------------------------------------------------------------- #
# Bucket accumulation mirroring _tp_report_v5_bucket_add (counts only).
# --------------------------------------------------------------------------- #
_BUCKET_KEYS = ("otpisok", "nonliquid", "geo", "under18", "na", "trash", "liquid")


def _empty_counts() -> Dict[str, int]:
    return {k: 0 for k in _BUCKET_KEYS}


def _add(counts: Dict[str, int], bucket_label: str) -> None:
    if bucket_label == "liquid":
        counts["liquid"] += 1
    elif bucket_label == "geo":
        counts["geo"] += 1
    elif bucket_label == "under18":
        counts["under18"] += 1
    elif bucket_label == "trash":
        counts["trash"] += 1
    else:
        counts["na"] += 1
    counts["nonliquid"] = counts["geo"] + counts["under18"] + counts["na"] + counts["trash"]
    counts["otpisok"] = counts["liquid"] + counts["nonliquid"]


# --------------------------------------------------------------------------- #
# Read-only DB access
# --------------------------------------------------------------------------- #
def _connect_ro(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        row = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _manager_rows(central_db: str) -> List[Dict[str, Any]]:
    con = _connect_ro(central_db)
    try:
        if not _table_exists(con, "managers"):
            return []
        cols = {r[1] for r in con.execute("PRAGMA table_info(managers)").fetchall()}
        sel = "manager_key" + (", db_path" if "db_path" in cols else "") + \
              (", is_enabled" if "is_enabled" in cols else "")
        rows = []
        for r in con.execute(f"SELECT {sel} FROM managers").fetchall():
            d = dict(r)
            if "is_enabled" in d and int(d.get("is_enabled") or 0) != 1:
                continue
            rows.append(d)
        return rows
    finally:
        con.close()


def _resolve_db_path(stored: str, central_db: str) -> str:
    """Best-effort: use the stored db_path; if absent, try it relative to the central DB dir."""
    if stored and os.path.exists(stored):
        return stored
    base = os.path.dirname(os.path.abspath(central_db))
    cand = os.path.join(base, os.path.basename(stored or ""))
    if stored and os.path.exists(cand):
        return cand
    # last resort: <central_dir>/../managers/<key>/... is unknown here; return stored as-is.
    return stored or ""


def _read_window_leads(
    db_path: str, ws: datetime, we: datetime, fetch_widen_days: int
) -> List[Dict[str, Any]]:
    """Read daily_leads whose first_seen falls in [ws, we). fetch_widen_days widens the
    lead_date pre-filter (flight needs D-1)."""
    if not db_path or not os.path.exists(db_path):
        return []
    con = _connect_ro(db_path)
    try:
        if not _table_exists(con, "daily_leads"):
            return []
        rows = con.execute("SELECT * FROM daily_leads WHERE COALESCE(chat_id,0)>0").fetchall()
        out = []
        for r in rows:
            lead = dict(r)
            if _se.se_in_window(lead, ws, we):
                out.append(lead)
        return out
    finally:
        con.close()


def _dates(args) -> List[date]:
    if args.date:
        return [datetime.strptime(args.date, "%Y-%m-%d").date()]
    today = date.today()
    n = max(1, int(args.days))
    return [today - timedelta(days=i) for i in range(n)]


def _windows_for(kind: str, d: date) -> Tuple[datetime, datetime, int]:
    if kind == "flight":
        ws, we = _se.se_flight_window(d, schedule_aware=False)
        return ws, we, 1
    ws, we = _se.se_day_window(d)
    return ws, we, 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage 4E bucket parity (read-only).")
    ap.add_argument("--db", required=True, help="Path to a COPY of the central data_tpilot.db")
    ap.add_argument("--days", type=int, default=7, help="Number of recent days (default 7)")
    ap.add_argument("--date", default="", help="Single date YYYY-MM-DD (overrides --days)")
    ap.add_argument("--kind", choices=("day", "flight", "both"), default="both")
    ap.add_argument("--managers", default="", help="Comma-separated manager_key filter")
    ap.add_argument("--samples", type=int, default=20, help="Max disagreement samples to print")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"FATAL: --db not found: {args.db}", file=sys.stderr)
        return 2

    mgr_filter = {_se.se_norm_key(x) for x in args.managers.split(",") if x.strip()}
    mrows = _manager_rows(args.db)
    if mgr_filter:
        mrows = [m for m in mrows if _se.se_norm_key(m.get("manager_key")) in mgr_filter]
    if not mrows:
        print("No managers found (check --db / --managers).", file=sys.stderr)
        return 2

    kinds = ("day", "flight") if args.kind == "both" else (args.kind,)
    dates = _dates(args)

    grand_disagree = 0
    for kind in kinds:
        v5_counts = _empty_counts()
        eng_counts = _empty_counts()
        disagreements: List[Tuple[str, str, int, str, str]] = []  # (mk, date, chat_id, v5, eng)
        n_leads = 0

        for d in dates:
            ws, we, widen = _windows_for(kind, d)
            for m in mrows:
                mk = _se.se_norm_key(m.get("manager_key"))
                dbp = _resolve_db_path(str(m.get("db_path") or ""), args.db)
                if not dbp or not os.path.exists(dbp):
                    if args.verbose:
                        print(f"  skip {mk}: db not found ({m.get('db_path')!r})")
                    continue
                for lead in _read_window_leads(dbp, ws, we, widen):
                    n_leads += 1
                    v5 = _v5_bucket(lead)
                    eng = _se.se_bucket(lead)
                    _add(v5_counts, v5)
                    _add(eng_counts, eng)
                    if v5 != eng:
                        grand_disagree += 1
                        if len(disagreements) < args.samples:
                            disagreements.append(
                                (mk, d.isoformat(), int(lead.get("chat_id") or 0), v5, eng)
                            )

        label = "DAY 08:00-17:00" if kind == "day" else "FLIGHT D-1 17:00 -> D 08:00"
        print(f"\n=== {kind.upper()} ({label}) | dates={len(dates)} | leads={n_leads} ===")
        print(f"{'line':<10} {'v5':>8} {'engine':>8} {'delta':>8}")
        any_delta = False
        for k in _BUCKET_KEYS:
            dv = eng_counts[k] - v5_counts[k]
            if dv != 0:
                any_delta = True
            print(f"{k:<10} {v5_counts[k]:>8} {eng_counts[k]:>8} {dv:>+8}")
        if not any_delta:
            print("  -> no line-level deltas (displayed numbers identical for this kind)")
        if disagreements:
            print(f"  per-lead disagreements (showing {len(disagreements)}):")
            for mk, ds, cid, v5, eng in disagreements:
                print(f"    {ds} {mk} chat_id={cid}: v5={v5} engine={eng}")

    print(f"\nTOTAL per-lead disagreements: {grand_disagree}")
    if grand_disagree == 0:
        print("PARITY CLEAN -> safe to flip _SE4E_ENGINE_BUCKET_LIVE (after review).")
        return 0
    print("PARITY MISMATCH -> do NOT flip the flag; review the samples above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
