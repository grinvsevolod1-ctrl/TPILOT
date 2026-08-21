# -*- coding: utf-8 -*-
"""
ALM_TPilot / TPilot
Repair: reparse profile fields from saved client messages after the first client message.

Usage:
  python repair_reparse_profile_from_messages_v1.py --manager darias --date 2026-05-15 --dry-run
  python repair_reparse_profile_from_messages_v1.py --manager darias --date 2026-05-15 --apply

Rule:
- first client message is ignored;
- all subsequent incoming client messages are profile evidence regardless of greeting/questionnaire/silence settings.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent
CENTRAL_DB = ROOT / "db" / "data_tpilot.db"
VERSION = "repair_reparse_profile_from_messages_v1_20260516"


def now_iso() -> str:
    # utcnow refactor: naive-UTC seam, byte-identical to the old
    # datetime.utcnow() output and deprecation-free on Python 3.12+.
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def columns(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def s(raw: Any) -> str:
    return str(raw or "").strip()


def b(raw: Any) -> bool:
    return str(raw).strip().lower() in {"1", "true", "yes", "да", "on", "вкл"}


def is_incoming_direction(direction: Any) -> bool:
    d = s(direction).lower()
    if not d:
        return True
    if d in {"out", "outgoing", "manager", "bot", "self", "me", "исходящее"}:
        return False
    if d.startswith("out"):
        return False
    return True


def manager_db_path(manager_key: str) -> Path:
    con = sqlite3.connect(str(CENTRAL_DB))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT db_path FROM managers WHERE manager_key=? LIMIT 1", (manager_key,)).fetchone()
        if not row:
            raise SystemExit(f"MANAGER NOT FOUND: {manager_key}")
        dbp = Path(str(row["db_path"] or ""))
        if not dbp.exists():
            raise SystemExit(f"MANAGER DB MISSING: {dbp}")
        return dbp
    finally:
        con.close()


def collect_client_texts_after_first(con: sqlite3.Connection, chat_id: int) -> List[str]:
    texts: List[str] = []

    if table_exists(con, "inbound_events") and "text" in columns(con, "inbound_events"):
        try:
            for r in con.execute("""
                SELECT text
                FROM inbound_events
                WHERE chat_id=? AND COALESCE(text,'')<>''
                ORDER BY COALESCE(message_date_utc, received_at_utc, created_at, ''), id
            """, (chat_id,)):
                t = s(r[0])
                if t:
                    texts.append(t)
        except Exception:
            texts = []

    if not texts and table_exists(con, "messages"):
        cols = columns(con, "messages")
        try:
            if "direction" in cols:
                for r in con.execute("""
                    SELECT direction, text
                    FROM messages
                    WHERE chat_id=? AND COALESCE(text,'')<>''
                    ORDER BY id
                """, (chat_id,)):
                    if is_incoming_direction(r[0]):
                        t = s(r[1])
                        if t:
                            texts.append(t)
            else:
                for r in con.execute("""
                    SELECT text
                    FROM messages
                    WHERE chat_id=? AND COALESCE(text,'')<>''
                    ORDER BY id
                """, (chat_id,)):
                    t = s(r[0])
                    if t:
                        texts.append(t)
        except Exception:
            pass

    if len(texts) <= 1:
        return []
    return [x for x in texts[1:] if s(x)][-30:]


def age_known(profile: Dict[str, Any]) -> bool:
    if s(profile.get("negative_age_marker")):
        return True
    if profile.get("age") is not None and s(profile.get("age")) != "":
        return True
    if b(profile.get("age_confirmed_18_plus")):
        return True
    return False


def geo_known(profile: Dict[str, Any]) -> bool:
    return bool(s(profile.get("country")))


def pending_fields(reason: str) -> Dict[str, Any]:
    return {
        "status": "unknown",
        "nonliquid_reason": "",
        "profile_done": 0,
        "quality_status": "pending",
        "quality_bucket": "na_pending",
        "quality_reason": reason,
        "quality_confidence": "low",
        "quality_source": VERSION,
        "quality_checked_at": now_iso(),
        "quality_version": VERSION,
        "profile_decision_reason": reason,
        "needs_review": 0,
        "extraction_method": "repair_parse_always_v1",
        "extraction_confidence": "low",
        "profile_extraction_version": VERSION,
    }


def enforce_complete(profile: Dict[str, Any], old_row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    out = dict(profile or {})
    old = dict(old_row or {})

    country = s(out.get("country"))
    old_bucket = s(old.get("quality_bucket"))
    old_country = s(old.get("country"))

    # Foreign geo is final nonliquid even if age is missing.
    # Example: "я из Ташкента" must be GEO, not NA because age is absent.
    if country and country != "Россия":
        return out

    # Do not downgrade an already known foreign GEO to NA when the new parser is unsure.
    # This protects historical repaired rows like Фергана/Бишкек/Египет/Брест until dictionaries are richer.
    if not country and old_bucket == "geo" and old_country and old_country != "Россия":
        keep = dict(old)
        keep["quality_source"] = VERSION
        keep["quality_checked_at"] = now_iso()
        keep["quality_version"] = VERSION
        keep["profile_extraction_version"] = VERSION
        return keep

    missing_age = not age_known(out)
    missing_geo = not geo_known(out)
    if missing_age or missing_geo:
        if missing_age and missing_geo:
            reason = "Не хватает гео и возраста после первого сообщения клиента"
        elif missing_age:
            reason = "Не хватает возраста после первого сообщения клиента"
        else:
            reason = "Не хватает гео после первого сообщения клиента"
        out.update(pending_fields(reason))
        if missing_age:
            out["age"] = None
            out["age_confirmed_18_plus"] = 0
            out["age_evidence_text"] = ""
    return out


def update_daily(con: sqlite3.Connection, row_id: int, fields: Dict[str, Any]) -> None:
    cols = set(columns(con, "daily_leads"))
    clean = {k: v for k, v in fields.items() if k in cols}
    if not clean:
        return
    keys = list(clean.keys())
    vals = [clean[k] for k in keys]
    vals.append(row_id)
    sql = "UPDATE daily_leads SET " + ", ".join([f"{k}=?" for k in keys]) + " WHERE id=?"
    con.execute(sql, vals)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manager", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT))
    import profile_extractor

    dbp = manager_db_path(args.manager)
    con = sqlite3.connect(str(dbp), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=30000")

    try:
        if not table_exists(con, "daily_leads"):
            raise SystemExit("daily_leads not found")

        rows = list(con.execute("""
            SELECT *
            FROM daily_leads
            WHERE lead_date=?
            ORDER BY id
        """, (args.date,)))

        counters = Counter()
        examples = []

        if args.apply:
            con.execute("BEGIN IMMEDIATE")

        for row in rows:
            d = dict(row)
            if int(d.get("manual_status_override") or 0) == 1:
                counters["skipped_manual_override"] += 1
                continue
            if int(d.get("trash") or 0) == 1:
                counters["skipped_trash"] += 1
                continue

            chat_id = int(d.get("chat_id") or 0)
            answer_msgs = collect_client_texts_after_first(con, chat_id)
            answer_joined = "\n".join(answer_msgs)[-4000:]

            if not answer_msgs:
                fields = {
                    "first_message_ignored": 1,
                    "first_message_ignored_at": d.get("first_message_ignored_at") or now_iso(),
                    "profile_answer_texts": "",
                    "profile_raw_text": "",
                    "profile_evidence_text": "",
                    "age_evidence_text": "",
                    "geo_evidence_text": "",
                    "age_confirmed_18_plus": 0,
                }
                fields.update(pending_fields("Нет данных после первого сообщения клиента"))
                new_bucket = fields["quality_bucket"]
                new_reason = fields["quality_reason"]
            else:
                parsed = profile_extractor.extract_profile_v2(answer_msgs, {
                    "manager_key": args.manager,
                    "lead_date": args.date,
                    "chat_id": chat_id,
                })
                parsed = enforce_complete(parsed, d)
                parsed.update({
                    "first_message_ignored": 1,
                    "first_message_ignored_at": d.get("first_message_ignored_at") or now_iso(),
                    "profile_answer_texts": answer_joined,
                    "profile_raw_text": answer_joined,
                    "profile_evidence_text": answer_joined,
                    "profile_answered_at": now_iso(),
                    "profile_extraction_version": VERSION,
                })
                if not s(parsed.get("age_evidence_text")):
                    parsed["age_confirmed_18_plus"] = 0
                fields = parsed
                new_bucket = fields.get("quality_bucket") or fields.get("status") or ""
                new_reason = fields.get("quality_reason") or fields.get("profile_decision_reason") or ""

            old_key = f"{d.get('quality_bucket') or ''}|{d.get('quality_reason') or ''}"
            new_key = f"{new_bucket}|{new_reason}"
            if old_key != new_key or (answer_joined and not s(d.get("profile_answer_texts"))):
                counters["would_change"] += 1
                if len(examples) < 30:
                    examples.append({
                        "id": d.get("id"),
                        "chat_id": chat_id,
                        "username": d.get("username"),
                        "old_bucket": d.get("quality_bucket"),
                        "old_reason": d.get("quality_reason"),
                        "new_bucket": new_bucket,
                        "new_reason": new_reason,
                        "answer_texts": answer_joined[:500],
                    })
                if args.apply:
                    update_daily(con, int(d["id"]), fields)
            else:
                counters["unchanged"] += 1

        if args.apply:
            con.commit()

        print("REPAIR", "APPLY" if args.apply else "DRY-RUN")
        print("manager:", args.manager)
        print("date:", args.date)
        print("db:", dbp)
        print("total_rows:", len(rows))
        print("counters:", dict(counters))
        print("examples:")
        for x in examples:
            print(x)
        return 0
    except Exception:
        if args.apply:
            con.rollback()
        raise
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
