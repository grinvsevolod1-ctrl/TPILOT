# -*- coding: utf-8 -*-
from __future__ import annotations

"""Profile-dialog decision layer for ALM_TPilot / TPilot v2.

This module does not send Telegram messages. It only decides what to say next
and stores lightweight anti-repeat state in the manager SQLite DB.
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import aiosqlite

from profile_texts import PROFILE_DIALOG_TEMPLATES

CITY_LIMIT_NOTE = "город уточняли 2 раза, клиент не указал"


def _now_iso() -> str:
    # utcnow refactor: naive-UTC seam, byte-identical to the old
    # datetime.utcnow() output and deprecation-free on Python 3.12+.
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


def _norm(text: Any) -> str:
    s = str(text or "").lower().replace("ё", "е")
    s = re.sub(r"[‐‑‒–—−]", "-", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(value)
    except Exception:
        return default


def _nonempty(value: Any) -> str:
    return str(value or "").strip()


def detect_profile_intent(text: str) -> str:
    t = _norm(text)
    if not t:
        return "short_noise"

    if re.search(r"\b(бот|робот|ии|ai|нейросет|автоответ|автомат)\b", t):
        return "bot_question"

    if any(x in t for x in [
        "сколько плат", "оплата", "зарплат", "зп", "деньги", "ставка", "сколько можно", "доход",
    ]):
        return "salary_question"

    if any(x in t for x in [
        "зачем", "для чего", "почему", "для чего это", "зачем возраст", "зачем город", "почему надо",
        "для чего вам", "зачем вам",
    ]):
        return "why_question"

    if any(x in t for x in [
        "что за работа", "какая работа", "что за вакан", "какая вакан", "расскаж", "подробнее",
        "чем заниматься", "что делать", "в чем суть", "какие условия", "что нужно делать",
    ]):
        return "job_question"

    if any(x in t for x in [
        "не хочу", "не буду", "не скажу", "личное", "не дам", "какая разница", "без этого", "не важно",
    ]):
        return "privacy_resistance"

    if any(x in t for x in ["потом", "позже", "занят", "занята", "сейчас не могу", "попозже"]):
        return "later"

    compact = re.sub(r"[^a-zа-я0-9]+", "", t)
    if len(compact) <= 3 or t in {"да", "нет", "ок", "окей", "?", "ага", "угу", "хорошо"}:
        return "short_noise"

    return "general"


def profile_missing_fields(lead: Dict[str, Any]) -> Tuple[bool, bool, bool]:
    age_missing = lead.get("age") is None or str(lead.get("age") or "").strip() == ""
    country = _nonempty(lead.get("country"))
    city = _nonempty(lead.get("city"))
    country_missing = not bool(country)
    city_missing = not bool(city)
    return age_missing, country_missing, city_missing


def _is_russia_adult_without_city(lead: Dict[str, Any]) -> bool:
    country = _nonempty(lead.get("country"))
    city = _nonempty(lead.get("city"))
    age = _as_int(lead.get("age"), -1)
    return country == "Россия" and age >= 18 and not city


def _sent_keys(state: Dict[str, Any]) -> List[str]:
    raw = state.get("sent_template_keys") or "[]"
    try:
        data = json.loads(str(raw))
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
    except Exception:
        pass
    return []


def _choose_template(category: str, state: Dict[str, Any]) -> Tuple[str, str]:
    cat = category if category in PROFILE_DIALOG_TEMPLATES else "general"
    variants = list(PROFILE_DIALOG_TEMPLATES.get(cat) or PROFILE_DIALOG_TEMPLATES.get("general") or [])
    if not variants:
        return "", ""
    sent = set(_sent_keys(state))
    for idx, text in enumerate(variants):
        key = f"{cat}:{idx}"
        if key not in sent:
            return key, text
    # All variants in this category have already been used. Reuse the last one only if needed.
    key = f"{cat}:0"
    return key, variants[0]


def choose_profile_reply(lead: Dict[str, Any], client_text: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Return a dict with reply decision.

    Keys:
      send_text: text to send, or empty string
      template_key: anti-repeat key
      intent: detected intent
      increment_city_attempt: whether city-only counter must be incremented
      city_limit_reached: True when Russia 18+ has already had 2 city asks
      update_fields: fields suggested for daily_leads
    """
    lead = dict(lead or {})
    state = dict(state or {})
    intent = detect_profile_intent(client_text or "")
    age_missing, country_missing, city_missing = profile_missing_fields(lead)

    result = {
        "send_text": "",
        "template_key": "",
        "intent": intent,
        "increment_city_attempt": False,
        "city_limit_reached": False,
        "update_fields": {},
    }

    if _is_russia_adult_without_city(lead):
        city_attempts = _as_int(state.get("city_attempts"), 0)
        if city_attempts >= 2:
            result["city_limit_reached"] = True
            result["update_fields"] = {"geo_note": CITY_LIMIT_NOTE}
            return result
        category = "city_only_first" if city_attempts <= 0 else "city_only_second"
        key, text = _choose_template(category, state)
        result.update({
            "send_text": text,
            "template_key": key,
            "intent": category,
            "increment_city_attempt": True,
        })
        return result

    if not age_missing and not country_missing:
        return result

    if age_missing and (country_missing or city_missing):
        if intent in {"job_question", "why_question", "bot_question", "salary_question", "privacy_resistance", "later", "short_noise"}:
            category = intent
        else:
            category = "missing_both"
    elif age_missing:
        category = "missing_age"
    else:
        if intent in {"job_question", "why_question", "bot_question", "salary_question", "privacy_resistance", "later", "short_noise"}:
            category = intent
        else:
            category = "missing_geo"

    key, text = _choose_template(category, state)
    result.update({"send_text": text, "template_key": key, "intent": category})
    return result


async def ensure_profile_dialog_table(db_path: str) -> None:
    if not db_path:
        return
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS lead_dialog_state(
                chat_id INTEGER PRIMARY KEY,
                sent_template_keys TEXT DEFAULT '[]',
                last_intent TEXT DEFAULT '',
                last_bot_text TEXT DEFAULT '',
                last_client_text TEXT DEFAULT '',
                dialog_step INTEGER NOT NULL DEFAULT 0,
                city_attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        await db.commit()


async def load_profile_dialog_state(db_path: str, chat_id: int) -> Dict[str, Any]:
    await ensure_profile_dialog_table(db_path)
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            "INSERT OR IGNORE INTO lead_dialog_state(chat_id, created_at, updated_at) VALUES(?,?,?)",
            (int(chat_id), now, now),
        )
        cur = await db.execute("SELECT * FROM lead_dialog_state WHERE chat_id=?", (int(chat_id),))
        row = await cur.fetchone()
        await db.commit()
        return dict(row) if row else {"chat_id": int(chat_id)}


async def mark_profile_dialog_sent(
    db_path: str,
    chat_id: int,
    *,
    template_key: str = "",
    intent: str = "",
    bot_text: str = "",
    client_text: str = "",
    increment_city_attempt: bool = False,
) -> None:
    state = await load_profile_dialog_state(db_path, chat_id)
    sent = _sent_keys(state)
    if template_key and template_key not in sent:
        sent.append(str(template_key))
    city_attempts = _as_int(state.get("city_attempts"), 0) + (1 if increment_city_attempt else 0)
    step = _as_int(state.get("dialog_step"), 0) + 1
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            UPDATE lead_dialog_state
            SET sent_template_keys=?, last_intent=?, last_bot_text=?, last_client_text=?,
                dialog_step=?, city_attempts=?, updated_at=?
            WHERE chat_id=?
            """,
            (
                json.dumps(sent[-80:], ensure_ascii=False),
                str(intent or ""),
                str(bot_text or ""),
                str(client_text or ""),
                int(step),
                int(city_attempts),
                now,
                int(chat_id),
            ),
        )
        await db.commit()


__all__ = [
    "CITY_LIMIT_NOTE",
    "detect_profile_intent",
    "profile_missing_fields",
    "choose_profile_reply",
    "ensure_profile_dialog_table",
    "load_profile_dialog_state",
    "mark_profile_dialog_sent",
]
