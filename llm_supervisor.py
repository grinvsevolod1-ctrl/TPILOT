# -*- coding: utf-8 -*-
"""
TPilot LLM Questionnaire Supervisor — shadow mode only.

SHADOW MODE: analyzes questionnaire context and logs what the LLM would suggest,
but NEVER modifies any DB state, profile fields, attempt counters, or sends any
Telegram message. All output goes to a JSONL log file only.

Fail-open everywhere:
  - LLM_SUPERVISOR_ENABLED=false (default): logs disabled, returns None.
  - anthropic package absent: logs unavailable, returns None.
  - any API/timeout error: logs error, returns None.
  - any unexpected exception: silently returns None.

TPilot remains the sole sender and decision-maker in all cases.
Do not print or log ANTHROPIC_API_KEY or any secret value.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Geo lexicon — optional, fail-open (Stage D)
# --------------------------------------------------------------------------- #
try:
    from geo_lexicon import extract_geo_hints_from_messages as _geo_extract_msgs
    _geo_lexicon_ok: bool = True
except Exception:
    _geo_extract_msgs = None  # type: ignore[assignment]
    _geo_lexicon_ok = False

# --------------------------------------------------------------------------- #
# Lazy anthropic import — fails open if package not installed
# --------------------------------------------------------------------------- #
_anthropic_mod: Any = None
_anthropic_ok: bool = False
_anthropic_attempted: bool = False


def _load_anthropic() -> bool:
    global _anthropic_mod, _anthropic_ok, _anthropic_attempted
    if _anthropic_attempted:
        return _anthropic_ok
    _anthropic_attempted = True
    try:
        import anthropic as _a
        _anthropic_mod = _a
        _anthropic_ok = True
    except Exception:
        _anthropic_ok = False
    return _anthropic_ok


# --------------------------------------------------------------------------- #
# Config — reads from env already loaded by main.py via load_dotenv
# --------------------------------------------------------------------------- #
_BASE_DIR = Path(__file__).resolve().parent

_CFG_DEFAULTS: Dict[str, str] = {
    "LLM_SUPERVISOR_ENABLED": "false",
    "LLM_SUPERVISOR_MODE": "shadow",
    "ANTHROPIC_MODEL": "claude-sonnet-4-6",
    "LLM_SUPERVISOR_TIMEOUT_SEC": "8",
    "LLM_SUPERVISOR_LOG_PATH": "logs/llm_supervisor_shadow.log",
    "LLM_LIVE_TIMEOUT_SEC": "4",
    "LLM_LIVE_MIN_CONFIDENCE": "high",
}


def _cfg(key: str) -> str:
    return os.environ.get(key, _CFG_DEFAULTS.get(key, "")).strip()


def _cfg_bool(key: str) -> bool:
    return _cfg(key).lower() in ("1", "true", "yes", "on")


def _cfg_int(key: str, default: int) -> int:
    v = _cfg(key)
    try:
        return int(v) if v else default
    except Exception:
        return default


# --------------------------------------------------------------------------- #
# Shadow JSONL logger — no secrets, all fields truncated
# --------------------------------------------------------------------------- #
def _log_path() -> Path:
    raw = _cfg("LLM_SUPERVISOR_LOG_PATH") or "logs/llm_supervisor_shadow.log"
    p = Path(raw)
    return p if p.is_absolute() else _BASE_DIR / p


def _safe_hash(value: Any) -> str:
    try:
        return hashlib.sha256(str(value).encode()).hexdigest()[:12]
    except Exception:
        return "?"


def _write_shadow_log(record: Dict[str, Any]) -> None:
    try:
        lp = _log_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with lp.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass  # shadow log failure must never propagate


# --------------------------------------------------------------------------- #
# JSON response schema — enums for validation and API structured output
# --------------------------------------------------------------------------- #
_ALLOWED_ACTIONS = frozenset(["clarify", "handoff", "no_action", "wait"])
_ALLOWED_HANDOFFS = frozenset(["liquid", "none", "nonliquid_geo", "nonliquid_na", "nonliquid_under18", "ua_redirect"])
_ALLOWED_MESSAGE_MODES = frozenset(["free_text", "none", "template"])
_ALLOWED_CONFIDENCES = frozenset(["high", "low", "medium"])
_ALLOWED_MISSING_FIELDS = frozenset(["age", "city", "country"])
_ALLOWED_PROFILE_PATCH_KEYS = frozenset(["age", "city", "country", "is_adult", "is_russia", "region"])
_ALLOWED_SAFETY_FLAGS = frozenset(["duplicate", "hostile", "needs_human", "objection", "off_topic", "uncertain_geo"])

# Passed to API for structured output constraint
RESPONSE_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "ambiguity", "confidence", "handoff", "message_mode", "profile_patch", "reason"],
    "properties": {
        "action": {"type": "string", "enum": sorted(_ALLOWED_ACTIONS)},
        "ambiguity": {"type": "boolean"},
        "confidence": {"type": "string", "enum": sorted(_ALLOWED_CONFIDENCES)},
        "handoff": {
            "type": "string",
            "enum": sorted(_ALLOWED_HANDOFFS),
        },
        "message_mode": {"type": "string", "enum": sorted(_ALLOWED_MESSAGE_MODES)},
        "message_to_send": {"type": ["string", "null"]},
        "missing_fields": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(_ALLOWED_MISSING_FIELDS)},
        },
        "profile_patch": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "age": {"type": ["integer", "null"]},
                "city": {"type": ["string", "null"]},
                "country": {"type": ["string", "null"]},
                "is_adult": {"type": ["boolean", "null"]},
                "is_russia": {"type": ["boolean", "null"]},
                "region": {"type": ["string", "null"]},
            },
        },
        "reason": {"type": "string"},
        "safety_flags": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(_ALLOWED_SAFETY_FLAGS)},
        },
        "template_id": {"type": ["string", "null"]},
    },
}


# --------------------------------------------------------------------------- #
# Response validator
# --------------------------------------------------------------------------- #
def _validate_response(raw: Any) -> Optional[Dict[str, Any]]:
    """Returns parsed dict if structurally valid, None otherwise."""
    if not isinstance(raw, dict):
        return None
    if raw.get("action") not in _ALLOWED_ACTIONS:
        return None
    if raw.get("confidence") not in _ALLOWED_CONFIDENCES:
        return None
    if raw.get("message_mode") not in _ALLOWED_MESSAGE_MODES:
        return None
    handoff = raw.get("handoff")
    if handoff is not None and handoff not in _ALLOWED_HANDOFFS:
        return None
    # Cross-field: action=handoff requires a real handoff value (not None, not "none", not missing)
    if raw.get("action") == "handoff":
        if handoff is None or handoff == "" or handoff == "none":
            return None
    if not isinstance(raw.get("reason", ""), str):
        return None
    patch = raw.get("profile_patch")
    if patch is not None and not isinstance(patch, dict):
        return None
    if isinstance(patch, dict):
        for k in patch:
            if k not in _ALLOWED_PROFILE_PATCH_KEYS:
                return None
    mf = raw.get("missing_fields")
    if mf is not None and not isinstance(mf, list):
        return None
    sf = raw.get("safety_flags")
    if sf is not None:
        if not isinstance(sf, list):
            return None
        for flag in sf:
            if flag not in _ALLOWED_SAFETY_FLAGS:
                return None
    return raw


# --------------------------------------------------------------------------- #
# Free-text style validator (shadow audit only — never blocks sends)
# --------------------------------------------------------------------------- #
_FREETEXT_MAX_CHARS = 200
_FREETEXT_STOP_WORDS = [
    "http://", "https://", "www.", "@",
    "руб",          # руб
    "грн",          # грн
    "зарплат",   # зарплат
    "оклад",               # оклад
    "доход",               # доход
    "перезвон",  # перезвон
    "позвони",        # позвони
]
_PHONE_RE = re.compile(r"[\+\d][\d\s\-\(\)]{7,}")
_QUESTION_RE = re.compile(r"\?")


def _validate_free_text_style(text: Optional[str]) -> Dict[str, Any]:
    if not text:
        return {"ok": True, "violations": []}
    violations: List[str] = []
    if len(text) > _FREETEXT_MAX_CHARS:
        violations.append(f"too_long:{len(text)}")
    if ":" in text:
        violations.append("colon")
    if "—" in text:
        violations.append("long_dash")
    if any(q in text for q in ('"', "«", "»", "„")):
        violations.append("quotes")
    if len(_QUESTION_RE.findall(text)) > 1:
        violations.append("multiple_questions")
    if _PHONE_RE.search(text):
        violations.append("phone_number")
    for sw in _FREETEXT_STOP_WORDS:
        if sw.lower() in text.lower():
            violations.append(f"stop_word:{sw}")
    return {"ok": len(violations) == 0, "violations": violations}


# --------------------------------------------------------------------------- #
# Shared LLM API call — used by both shadow and live functions
# --------------------------------------------------------------------------- #
async def _call_llm_api(
    payload: Dict[str, Any],
    api_key: str,
    model_id: str,
    timeout_sec: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]]]:
    """Call LLM API once. Returns (llm_parsed, error_info, free_text_check). Never raises."""
    llm_parsed: Optional[Dict[str, Any]] = None
    error_info: Optional[str] = None
    free_text_check: Optional[Dict[str, Any]] = None
    try:
        ac = _anthropic_mod.AsyncAnthropic(api_key=api_key)
        response = await asyncio.wait_for(
            ac.messages.create(
                model=model_id,
                max_tokens=512,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, default=str),
                    }
                ],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": RESPONSE_JSON_SCHEMA,
                    }
                },
            ),
            timeout=timeout_sec,
        )
        raw_text = (response.content[0].text if response.content else "").strip()
        try:
            llm_raw = json.loads(raw_text)
        except Exception:
            llm_raw = raw_text
        llm_parsed = _validate_response(llm_raw if isinstance(llm_raw, dict) else None)
        if llm_parsed and llm_parsed.get("message_mode") == "free_text":
            free_text_check = _validate_free_text_style(llm_parsed.get("message_to_send"))
    except Exception as exc:
        error_info = f"{type(exc).__name__}: {str(exc)[:200]}"
    return llm_parsed, error_info, free_text_check


# --------------------------------------------------------------------------- #
# Context payload builder — redacted / truncated
# --------------------------------------------------------------------------- #
_PROFILE_KEYS = (
    "country", "region", "city", "age", "status", "quality_bucket",
    "profile_done", "clarify_attempts", "profile_question_sent",
    "manual_status_override", "lead_countable",
)


def _build_payload(
    lead_row: Dict[str, Any],
    dialog_state: Dict[str, Any],
    client_text: str,
    recent_messages: Optional[List[str]] = None,
) -> Dict[str, Any]:
    raw = str(lead_row.get("profile_raw_text") or "")
    if len(raw) > 800:
        raw = raw[-800:]
    ds = dialog_state or {}
    clarify_used = int(lead_row.get("clarify_attempts") or 0)
    payload: Dict[str, Any] = {
        "chat_id_hash": _safe_hash(lead_row.get("chat_id")),
        "last_incoming_text": (client_text or "")[:400],
        "profile_raw_text_tail": raw,
        "questionnaire_state": {
            "age_attempts": int(ds.get("age_attempts") or 0),
            "both_attempts": int(ds.get("both_attempts") or 0),
            "city_attempts": int(ds.get("city_attempts") or 0),
            "geo_attempts": int(ds.get("geo_attempts") or 0),
            "na_finalized": int(ds.get("na_finalized") or 0),
        },
        "known_profile": {k: lead_row.get(k) for k in _PROFILE_KEYS},
        "runtime_limits": {
            "clarifications_used": clarify_used,
            "max_clarifications": 3,
        },
    }
    if recent_messages:
        payload["recent_messages"] = [str(m)[:200] for m in recent_messages[:6]]
    if _geo_lexicon_ok and _geo_extract_msgs is not None:
        try:
            _geo_texts = [t for t in ([client_text] + list(recent_messages or [])) if t]
            if _geo_texts:
                _hints = _geo_extract_msgs(_geo_texts)
                if _hints.get("geo_class") not in ("unknown", None):
                    payload["geo_hints"] = _hints
        except Exception:
            pass
    return payload


# --------------------------------------------------------------------------- #
# System prompt — stable across calls, suitable for prompt cache
# --------------------------------------------------------------------------- #
_SYSTEM_PROMPT = """Ты — профессиональный рекрутёр/менеджер по вакансиям. \
Анализируешь диалог с кандидатом и предлагаешь следующий шаг в анкете. \
TPilot (система) является единственным отправителем сообщений — ты только предлагаешь.

ТВОЯ ЦЕЛЬ: заполнить профиль кандидата (город + возраст) коротко и по-человечески, \
без шаблонных повторов. Ты не закрываешь лид самостоятельно — финальное решение принимает детерминированная система.

═══════════════════════════════════
ИЗВЛЕЧЕНИЕ ДАННЫХ (profile_patch)
═══════════════════════════════════
Смотри все сообщения: last_incoming_text И recent_messages (список последних сообщений серии).
Если recent_messages непуст, обрабатывай его как ЕДИНЫЙ ответ кандидата.

Сокращения и аббревиатуры:
- МО → region=Московская область, country=Россия (city=null пока город не назван явно)
- МСК / Мск / Москва → city=Москва, country=Россия
- СПБ / СПб / Питер → city=Санкт-Петербург, country=Россия
- НН / Нижний → city=Нижний Новгород, country=Россия
- «МО Дубна» → city=Дубна, region=Московская область, country=Россия

Если в payload есть ключ geo_hints — результат офлайн-лексикона. \
Поле geo_class: liquid_ru=Россия, ua_geo=Украина, non_liquid_geo=другая страна. \
При confidence=high используй city/region/country для profile_patch. \
Финальный close только детерминированный — не закрывай лид только на основе geo_hints.

Возраст:
- Число 14–90 рядом с локацией или отдельно (если локация уже есть) → age
- «двадцать два» / «двадцать лет» и т.п. → соответствующее число
- Если число явно < 14 или > 90 — не заполняй age, отмети ambiguity=true

Игнорируй имена, приветствия, эмодзи, лишний шум при извлечении.
Не домысливай данные если их нет явно.

Если город неоднозначен или сокращение неизвестно — uncertain_geo в safety_flags, action=clarify.

═══════════════════════════════════
ЛОГИКА ACTION
═══════════════════════════════════
- wait: первое касание, совсем нет данных
- clarify: нужно уточнение (один вопрос!) или обработка возражения
- handoff: все нужные данные собраны, confidence=high
- no_action: диалог завершён, новых данных ждать нет смысла

Не задавай вопрос про поле, которое уже есть в known_profile.
Если city и age уже заполнены → action=handoff, не clarify.
Если заполнено только одно → спрашивай только недостающее.

═══════════════════════════════════
HANDOFF (только при confidence=high)
═══════════════════════════════════
Поле handoff ОБЯЗАТЕЛЬНО в каждом ответе JSON:
- При action=handoff: укажи одно из liquid / nonliquid_geo / nonliquid_under18 / nonliquid_na / ua_redirect
- При action=clarify / wait / no_action: "handoff":"none"
НЕ пиши классификацию только в reason — она ДОЛЖНА быть в поле handoff.
Если в reason написал «Handoff liquid» — в JSON обязательно "handoff":"liquid".

Значения handoff:
- none: используй при clarify/wait/no_action (данных недостаточно или handoff не нужен)
- liquid: ТЕКУЩЕЕ местоположение Россия И age >= 18, оба явно подтверждены
- nonliquid_geo: ТЕКУЩАЯ локация не Россия и не Украина
- ua_redirect: ТЕКУЩАЯ локация Украина
- nonliquid_under18: age < 18
- nonliquid_na: 3+ попыток, лид игнорирует по существу

Текущее местоположение важнее происхождения:
«я из Беларуси, сейчас в Москве» → текущая локация Россия → liquid (если 18+)
Не делай nonliquid_geo только из-за происхождения, если текущая локация Россия.

═══════════════════════════════════
ПРАВИЛА message_to_send
═══════════════════════════════════
- Максимум 200 символов, ровно один вопросительный знак
- Без двоеточия, без длинного тире (—), без кавычек (", «, »)
- Без ссылок, без @, без телефонов, без упоминания зарплаты
- Только русский язык
- Коротко и по-человечески, без канцелярита
- Не повторяй дословно предыдущий вопрос бота (см. profile_raw_text_tail)

═══════════════════════════════════
РАБОТА С ВОЗРАЖЕНИЯМИ (приоритетно!)
═══════════════════════════════════
Распознай возражение ДО попытки извлечь данные. Если кандидат говорит:

«зачем возраст» / «зачем вам это» / «для чего» / «почему надо» / «не понимаю зачем»
→ safety_flags=[objection], action=clarify, message_mode=free_text
→ кратко объясни: возраст нужен чтобы понять подходит ли формат работы по условиям трудоустройства
→ затем задай вопрос снова (одним предложением)
→ пример: «Понимаю. Возраст нужен чтобы подобрать подходящий формат занятости. Сколько вам лет?»

«зачем город» / «какая разница где я»
→ аналогично: объясни — город нужен чтобы предложить ближайший вариант или удалённый формат
→ пример: «Это нужно чтобы предложить удобный для вас вариант. Из какого вы города?»

«что за работа» / «расскажите подробнее» / «чем заниматься»
→ коротко: «Это курьерская/полевая работа с гибким графиком. Чтобы подобрать вариант под вас, уточните...»
→ НЕ описывай детально условия и НЕ называй суммы

«сколько платят» / «какая зарплата» / «доход»
→ safety_flags=[off_topic] (не враждебность), но НЕ называй цифры
→ пример: «Условия обсудим после короткой анкеты. Подскажите...»

«это развод» / «мошенники» / «не верю»
→ спокойно, кратко, без давления: «Понимаю сомнения. Анкета нужна только для подбора.»
→ затем одним вопросом вернись к теме

«не хочу говорить» / «личное» / «не скажу»
→ safety_flags=[objection], уважительно объясни один раз
→ если повторный отказ — action=no_action или handoff=nonliquid_na

«потом» / «я подумаю» / «позже»
→ мягко: «Конечно, не тороплю. Когда будете готовы — напишите.»
→ action=wait или no_action

«уже писал» / «я уже говорил»
→ посмотри profile_raw_text_tail и known_profile
→ если данные уже есть — НЕ переспрашивай, используй их
→ если данные потерялись — извинись и уточни одним вопросом

Грубость / оскорбления
→ safety_flags=[hostile], спокойно и нейтрально, один вежливый вопрос или needs_human

═══════════════════════════════════
ЗАПРЕЩЕНО
═══════════════════════════════════
- Называть зарплату, ставку, доход в цифрах
- Упоминать законы, статистику, возрастные ограничения формально
- Давить, торопить, угрожать
- Упоминать внутренние системы (TPilot, классификатор, JSON, LLM)
- Отправлять более одного вопроса за раз
- Переспрашивать поле, которое уже заполнено

═══════════════════════════════════
ПРИМЕРЫ (точный JSON для каждого случая)
═══════════════════════════════════
{"action":"handoff","handoff":"liquid","message_mode":"none","missing_fields":[]}
{"action":"handoff","handoff":"nonliquid_geo","message_mode":"none","missing_fields":[]}
{"action":"handoff","handoff":"nonliquid_under18","message_mode":"none","missing_fields":[]}
{"action":"clarify","handoff":"none","message_mode":"template","missing_fields":["city"]}
{"action":"clarify","handoff":"none","message_mode":"template","missing_fields":["age"]}
{"action":"clarify","handoff":"none","message_mode":"template","missing_fields":["city","age"]}
{"action":"wait","handoff":"none","message_mode":"none","missing_fields":[]}

reason: кратко на русском, не более 200 символов.
Отвечай только валидным JSON без markdown."""


# --------------------------------------------------------------------------- #
# Main public async function
# --------------------------------------------------------------------------- #
async def llm_supervisor_shadow(
    lead_row: Dict[str, Any],
    dialog_state: Dict[str, Any],
    client_text: str,
    det_decision: Optional[Dict[str, Any]] = None,
    recent_messages: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Shadow call. Never modifies any state. Shadow log behavior unchanged.

    Logs one JSONL record with the LLM suggestion vs deterministic result.
    Returns llm_parsed for F1 audit persistence (may be None). State not mutated.
    recent_messages: optional burst of recent client texts (Stage C debounce, additive).
    """
    ts = datetime.now(timezone.utc).isoformat()
    mode = _cfg("LLM_SUPERVISOR_MODE") or "shadow"
    enabled = _cfg_bool("LLM_SUPERVISOR_ENABLED")
    base: Dict[str, Any] = {
        "ts": ts,
        "mode": mode,
        "chat_id_hash": _safe_hash((lead_row or {}).get("chat_id")),
    }

    if not enabled:
        return

    if mode != "shadow":
        _write_shadow_log({**base, "status": "unsupported_mode"})
        return

    if not _load_anthropic():
        _write_shadow_log({**base, "status": "anthropic_unavailable"})
        return

    api_key = _cfg("ANTHROPIC_API_KEY")
    if not api_key:
        _write_shadow_log({**base, "status": "no_api_key"})
        return

    model_id = _cfg("ANTHROPIC_MODEL") or "claude-sonnet-4-6"
    timeout_sec = float(_cfg_int("LLM_SUPERVISOR_TIMEOUT_SEC", 8))
    payload = _build_payload(lead_row or {}, dialog_state or {}, client_text or "", recent_messages=recent_messages)

    det_summary: Optional[Dict[str, Any]] = None
    if det_decision:
        det_summary = {
            "intent": det_decision.get("intent"),
            "template_key": det_decision.get("template_key"),
            "has_send_text": bool(det_decision.get("send_text")),
        }
        payload["deterministic_decision"] = det_summary

    t0 = time.monotonic()
    llm_parsed, error_info, free_text_check = await _call_llm_api(payload, api_key, model_id, timeout_sec)
    latency_ms = int((time.monotonic() - t0) * 1000)
    record: Dict[str, Any] = {
        **base,
        "status": "ok" if llm_parsed else ("error" if error_info else "invalid_response"),
        "latency_ms": latency_ms,
        "model": model_id,
        "det_summary": det_summary,
        "llm_valid": llm_parsed is not None,
        "llm_action": (llm_parsed or {}).get("action"),
        "llm_ambiguity": (llm_parsed or {}).get("ambiguity"),
        "llm_confidence": (llm_parsed or {}).get("confidence"),
        "llm_handoff": (llm_parsed or {}).get("handoff"),
        "llm_message_mode": (llm_parsed or {}).get("message_mode"),
        "llm_missing_fields": (llm_parsed or {}).get("missing_fields"),
        "llm_safety_flags": (llm_parsed or {}).get("safety_flags"),
        "llm_reason": ((llm_parsed or {}).get("reason") or "")[:200],
        "free_text_check": free_text_check,
    }
    if error_info:
        record["error"] = error_info
    _write_shadow_log(record)
    return llm_parsed  # F1: returned for audit only; shadow log/state unchanged


# --------------------------------------------------------------------------- #
# Live text function — Stage E
# --------------------------------------------------------------------------- #
_LIVE_CONF_RANK: Dict[str, int] = {"low": 0, "medium": 1, "high": 2}


async def llm_supervisor_live_text(
    lead_row: Dict[str, Any],
    dialog_state: Dict[str, Any],
    client_text: str,
    det_decision: Optional[Dict[str, Any]] = None,
    recent_messages: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Live call for allowlisted managers/chats. Returns validated dict if all
    acceptance checks pass, else None. Writes live_used / live_fallback audit entry.
    Never raises to caller.

    Acceptance checks (all must pass to return non-None):
      action==clarify, message_mode==free_text, handoff in (None,"none"),
      confidence>=LLM_LIVE_MIN_CONFIDENCE, message_to_send non-empty+style-ok,
      needs_human not in safety_flags, message_to_send != dialog_state.last_bot_text.
    """
    ts = datetime.now(timezone.utc).isoformat()
    mode = _cfg("LLM_SUPERVISOR_MODE") or "shadow"
    enabled = _cfg_bool("LLM_SUPERVISOR_ENABLED")
    base: Dict[str, Any] = {
        "ts": ts,
        "mode": mode,
        "chat_id_hash": _safe_hash((lead_row or {}).get("chat_id")),
    }
    try:
        if not enabled or mode != "live":
            return None
        if not _load_anthropic():
            _write_shadow_log({**base, "status": "anthropic_unavailable"})
            return None
        api_key = _cfg("ANTHROPIC_API_KEY")
        if not api_key:
            _write_shadow_log({**base, "status": "no_api_key"})
            return None

        model_id = _cfg("ANTHROPIC_MODEL") or "claude-sonnet-4-6"
        timeout_sec = float(_cfg_int("LLM_LIVE_TIMEOUT_SEC", 4))
        payload = _build_payload(
            lead_row or {}, dialog_state or {}, client_text or "",
            recent_messages=recent_messages,
        )

        det_summary: Optional[Dict[str, Any]] = None
        if det_decision:
            det_summary = {
                "intent": det_decision.get("intent"),
                "template_key": det_decision.get("template_key"),
                "has_send_text": bool(det_decision.get("send_text")),
            }
            payload["deterministic_decision"] = det_summary

        t0 = time.monotonic()
        llm_parsed, error_info, free_text_check = await _call_llm_api(payload, api_key, model_id, timeout_sec)
        latency_ms = int((time.monotonic() - t0) * 1000)

        # Acceptance checks — all must pass for live_used
        accepted = False
        reject_reason: Optional[str] = None
        if llm_parsed is None:
            reject_reason = "no_valid_response"
        elif llm_parsed.get("action") != "clarify":
            reject_reason = f"action_not_clarify:{llm_parsed.get('action')}"
        elif llm_parsed.get("message_mode") != "free_text":
            reject_reason = f"mode_not_free_text:{llm_parsed.get('message_mode')}"
        elif llm_parsed.get("handoff") not in (None, "none"):
            reject_reason = f"unexpected_handoff:{llm_parsed.get('handoff')}"
        elif "needs_human" in (llm_parsed.get("safety_flags") or []):
            reject_reason = "needs_human"
        else:
            min_conf = _cfg("LLM_LIVE_MIN_CONFIDENCE") or "high"
            confidence = str(llm_parsed.get("confidence") or "")
            if _LIVE_CONF_RANK.get(confidence, -1) < _LIVE_CONF_RANK.get(min_conf, 2):
                reject_reason = f"confidence_low:{confidence}<{min_conf}"
            elif free_text_check is None or not free_text_check.get("ok"):
                reject_reason = f"style_fail:{(free_text_check or {}).get('violations')}"
            else:
                live_msg = str(llm_parsed.get("message_to_send") or "").strip()
                if not live_msg:
                    reject_reason = "empty_message"
                else:
                    last_bot = str((dialog_state or {}).get("last_bot_text") or "").strip()
                    if live_msg == last_bot:
                        reject_reason = "duplicate_last_bot_text"
                    else:
                        accepted = True

        record: Dict[str, Any] = {
            **base,
            "status": "live_used" if accepted else "live_fallback",
            "latency_ms": latency_ms,
            "model": model_id,
            "det_summary": det_summary,
            "llm_valid": llm_parsed is not None,
            "llm_action": (llm_parsed or {}).get("action"),
            "llm_confidence": (llm_parsed or {}).get("confidence"),
            "llm_handoff": (llm_parsed or {}).get("handoff"),
            "llm_message_mode": (llm_parsed or {}).get("message_mode"),
            "llm_safety_flags": (llm_parsed or {}).get("safety_flags"),
            "llm_reason": ((llm_parsed or {}).get("reason") or "")[:200],
            "free_text_check": free_text_check,
        }
        if reject_reason:
            record["reject_reason"] = reject_reason
        if error_info:
            record["error"] = error_info
        _write_shadow_log(record)

        return llm_parsed if accepted else None
    except Exception:
        return None
