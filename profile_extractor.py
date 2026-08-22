# -*- coding: utf-8 -*-
from __future__ import annotations

"""Profile Extraction Engine v2 Base for ALM_TPilot / TPilot.

Fail-closed rule: LIQUID is allowed only when geo is Russia/GEO_OK and
18+ is explicitly confirmed, with no negative age marker.
"""

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import router as _router  # type: ignore
except Exception:
    _router = None  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
VERSION = "profile_extraction_v2_base_20260512"

DEFAULT_CITY_BLACKLIST = [
    "почти", "почта", "работа", "работу", "работы", "условия", "интересно",
    "сотрудничество", "постоянные", "постоянная", "постоянно", "надо", "можно",
    "расскажите", "если", "есть", "лет", "мне", "да", "нет", "хочу", "нужна", "нужно",
    "дай", "дайте", "вакансия", "вакансию", "оплата", "зарплата", "зп",
]

DEFAULT_AGE_NEGATIVE_PHRASES = [
    "почти 18", "почти восемнадцать", "скоро 18", "будет 18", "18 будет",
    "через месяц 18", "через неделю 18", "через пару дней 18", "завтра 18",
    "мне 18 будет", "18 исполнится", "без пяти 18", "нет 18", "не 18",
    "еще нет 18", "ещё нет 18", "до 18", "меньше 18", "нету 18", "не исполнилось 18",
]

DEFAULT_AGE_POSITIVE_PHRASES = [
    "мне 18", "18 лет", "мне уже 18", "есть 18", "мне есть 18", "полных 18",
    "исполнилось 18", "уже исполнилось 18", "совершеннолетний", "совершеннолетняя",
    "совершеннолетние", "есть совершеннолетие",
]

# Business GEO_OK list. Editable in config/profile_geo_ru_special_locations.txt.
# Whole Zaporizhzhia/Kherson oblasts are intentionally NOT included as oblast-level rules.
DEFAULT_GEO_RU_SPECIAL_LOCATIONS = [
    "днр", "донецкая народная республика", "донецк", "донецкая область",
    "макеевка", "горловка", "енакеево", "дебальцево", "шахтерск", "снежное", "торез", "харцызск", "ясиноватая", "докучаевск", "новоазовск", "амвросиевка", "старобешево",
    "лнр", "луганская народная республика", "луганск", "луганская область",
    "алчевск", "стаханов", "кадиевка", "брянка", "краснодон", "сорокино", "ровеньки", "антрацит", "свердловск", "должанск", "красный луч", "хрустальный", "северодонецк", "лисичанск", "рубежное", "попасная",
    "крым", "республика крым", "севастополь", "симферополь", "ялта", "керчь", "евпатория", "феодосия", "бахчисарай", "джанкой", "саки", "алушта", "судак", "армянск", "красноперекопск",
    "мариуполь", "mariupol", "мелитополь", "melitopol", "бердянск", "berdiansk", "энергодар", "enerhodar", "токмак", "tokmak", "геническ", "henichesk", "новая каховка", "nova kakhovka", "скадовск", "skadovsk",
]

WORK_INTEREST_WORDS = {
    "работа", "работу", "работы", "вакансия", "вакансию", "сотрудничество", "условия",
    "интересно", "подробнее", "расскажите", "зарплата", "зп", "оплата", "доход", "18+",
}
AGE_CONTEXT_WORDS = {
    "мне", "возраст", "лет", "года", "год", "полных", "исполнилось", "исполнится",
    "совершеннолетний", "совершеннолетняя", "скоро", "будет", "есть",
}
GEO_CONTEXT_PATTERNS = [
    r"\bсейчас\s+в\b", r"\bнахожусь\s+в\b", r"\bживу\s+в\b", r"\bпроживаю\s+в\b",
    r"\bгород\b", r"\bв\s+городе\b", r"\bя\s+из\b", r"\bсам\s+из\b", r"\bсама\s+из\b", r"\bродом\s+из\b",
]

@dataclass
class RuleSet:
    city_blacklist: List[str]
    age_negative_phrases: List[str]
    age_positive_phrases: List[str]
    geo_ru_special_locations: List[str]

_RULES_CACHE: Optional[RuleSet] = None


def _norm(text: Any) -> str:
    s = str(text or "")
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = s.lower()
    s = re.sub(r"[‐‑‒–—−]", "-", s)
    s = re.sub(r"[\t\r\n]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _key(text: Any) -> str:
    s = _norm(text)
    s = re.sub(r"[^0-9a-zа-яіїєґ\s\-+]+", " ", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(text: Any) -> List[str]:
    return [x for x in re.split(r"\s+", _key(text)) if x]


def _read_lines(path: Path, defaults: Iterable[str]) -> List[str]:
    vals: List[str] = []
    try:
        if path.exists():
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    vals.append(line)
    except Exception:
        vals = []
    if not vals:
        vals = list(defaults)
    seen = set()
    out = []
    for v in vals:
        k = _key(v)
        if k and k not in seen:
            seen.add(k)
            out.append(v.strip())
    return out


def load_rules(*, reload: bool = False) -> RuleSet:
    global _RULES_CACHE
    if _RULES_CACHE is not None and not reload:
        return _RULES_CACHE
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _RULES_CACHE = RuleSet(
        city_blacklist=_read_lines(CONFIG_DIR / "profile_city_blacklist.txt", DEFAULT_CITY_BLACKLIST),
        age_negative_phrases=_read_lines(CONFIG_DIR / "profile_age_negative_phrases.txt", DEFAULT_AGE_NEGATIVE_PHRASES),
        age_positive_phrases=_read_lines(CONFIG_DIR / "profile_age_positive_phrases.txt", DEFAULT_AGE_POSITIVE_PHRASES),
        geo_ru_special_locations=_read_lines(CONFIG_DIR / "profile_geo_ru_special_locations.txt", DEFAULT_GEO_RU_SPECIAL_LOCATIONS),
    )
    return _RULES_CACHE


def reload_rules() -> RuleSet:
    return load_rules(reload=True)


def rules_summary() -> str:
    r = load_rules()
    return "\n".join([
        "📋 Profile rules loaded",
        f"city_blacklist: {len(r.city_blacklist)}",
        f"age_negative_phrases: {len(r.age_negative_phrases)}",
        f"age_positive_phrases: {len(r.age_positive_phrases)}",
        f"geo_ru_special_locations: {len(r.geo_ru_special_locations)}",
    ])


def _contains_phrase(text: str, phrase: str) -> bool:
    """Token-safe phrase matcher. Prevents bad substring hits like не 18 inside мне 18."""
    tt = _tokens(text)
    pp = _tokens(phrase)
    if not pp or len(pp) > len(tt):
        return False
    for i in range(0, len(tt) - len(pp) + 1):
        if tt[i:i + len(pp)] == pp:
            return True
    return False


def _text_has_any(text: str, words: Iterable[str]) -> bool:
    t = _key(text)
    toks = set(t.split())
    for w in words:
        wk = _key(w)
        if not wk:
            continue
        if " " in wk:
            if wk in t:
                return True
        elif wk in toks:
            return True
    return False


def _is_interest_text(text: str) -> bool:
    t = _key(text)
    if not t:
        return False
    return _text_has_any(t, WORK_INTEREST_WORDS)


def _has_geo_context(text: str) -> bool:
    t = _key(text)
    return any(re.search(p, t, flags=re.I) for p in GEO_CONTEXT_PATTERNS)


def _looks_like_only_geo_or_profile(text: str) -> bool:
    toks = _tokens(text)
    if not toks:
        return False
    if len(toks) <= 5 and not _text_has_any(text, WORK_INTEREST_WORDS):
        return True
    return _has_geo_context(text)


def _looks_like_age_message(text: str) -> bool:
    t = _key(text)
    if not t:
        return False
    if re.search(r"(?<!\d)\d{1,2}\s*\+?\s*(?:лет|год|года|полных\s+лет)\b", t):
        return True
    if re.search(r"\b(?:мне|возраст|полных|исполнилось|исполнится|есть|скоро|будет)\b", t) and re.search(r"(?<!\d)\d{1,2}\+?(?!\d)", t):
        return True
    if re.search(r"\bсовершеннолетн\w*\b", t):
        return True
    if re.fullmatch(r"\d{1,2}\+?", t):
        return True
    return False


def _age_negative(text: str, rules: RuleSet) -> Tuple[bool, str, Optional[int], str]:
    t = _key(text)
    for phrase in rules.age_negative_phrases:
        if _contains_phrase(t, phrase):
            # Keep exact underage number when it is explicit, e.g. "17 почти 18".
            found = [int(x) for x in re.findall(r"(?<!\d)(1[4-7])(?!\d)", t)]
            return True, phrase, (found[0] if found else None), "negative_phrase"
    # Strong grammar patterns around 18.
    patterns = [
        r"\bпочти\s+18\b", r"\bскоро\s+18\b", r"\bбудет\s+18\b", r"\b18\s+будет\b",
        r"\b18\s+исполн", r"\bисполн\w*\s+18\b", r"\bбез\s+пяти\s+18\b",
        r"\bнет\s+18\b", r"\bне\s+18\b", r"\bеще\s+нет\s+18\b", r"\bещё\s+нет\s+18\b",
        r"\bдо\s+18\b", r"\bменьше\s+18\b", r"\b18\s+будет\s+\w+\b",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            found = [int(x) for x in re.findall(r"(?<!\d)(1[4-7])(?!\d)", t)]
            return True, m.group(0), (found[0] if found else None), "negative_pattern"
    # Any explicit age below 18.
    m = re.search(r"(?<!\d)(1[4-7])(?!\d)\s*(?:лет|год|года)?", t)
    if m:
        return True, m.group(0), int(m.group(1)), "under18_number"
    return False, "", None, ""


def _extract_age(text: str, rules: RuleSet) -> Dict[str, Any]:
    raw = str(text or "")
    t = _key(raw)
    neg, neg_phrase, neg_age, neg_source = _age_negative(t, rules)
    if neg:
        return {
            "age": neg_age,
            "age_confirmed_18_plus": False,
            "negative_age_marker": neg_phrase or "negative_age",
            "age_evidence_text": raw.strip(),
            "age_confidence": "high",
            "age_source": neg_source,
        }

    # "скоро N": current age is N-1 when N > 18.
    m = re.search(r"\bскоро\s+(\d{2})\b", t)
    if m:
        n = int(m.group(1))
        if n == 18:
            return {"age": None, "age_confirmed_18_plus": False, "negative_age_marker": "скоро 18", "age_evidence_text": raw.strip(), "age_confidence": "high", "age_source": "soon18"}
        if 19 <= n <= 90:
            return {"age": n - 1, "age_confirmed_18_plus": True, "negative_age_marker": "", "age_evidence_text": raw.strip(), "age_confidence": "medium", "age_source": "soon_n_minus_1"}

    # Words that confirm adulthood without exact age.
    if re.search(r"\bсовершеннолетн\w*\b", t):
        return {"age": 18, "age_confirmed_18_plus": True, "negative_age_marker": "", "age_evidence_text": raw.strip(), "age_confidence": "medium", "age_source": "adult_word"}

    # Positive phrases from config.
    for phrase in rules.age_positive_phrases:
        if _contains_phrase(t, phrase):
            nums = [int(x) for x in re.findall(r"(?<!\d)(\d{2})(?!\d)", _key(phrase))]
            age = nums[0] if nums else 18
            if 18 <= age <= 90:
                return {"age": age, "age_confirmed_18_plus": True, "negative_age_marker": "", "age_evidence_text": raw.strip(), "age_confidence": "high" if age != 18 or "соверш" not in _key(phrase) else "medium", "age_source": "positive_phrase"}

    # Passport / official work are explicitly not proof of age.
    if re.search(r"\bпаспорт\b", t) or re.search(r"\bофициальн\w*\b", t):
        return {"age": None, "age_confirmed_18_plus": False, "negative_age_marker": "", "age_evidence_text": "", "age_confidence": "none", "age_source": "not_age_proof"}

    # Work-interest text with 18+ is not client's age unless age context exists.
    if "18+" in t and _is_interest_text(t) and not _text_has_any(t, AGE_CONTEXT_WORDS):
        return {"age": None, "age_confirmed_18_plus": False, "negative_age_marker": "", "age_evidence_text": "", "age_confidence": "none", "age_source": "offer_18_plus_ignored"}

    # Context age patterns.
    patterns = [
        r"\b(?:мне|возраст|полных|есть)\s*(\d{1,2})\+?\b",
        r"\b(\d{1,2})\s*\+?\s*(?:лет|год|года|полных\s+лет)\b",
        r"\bисполнилось\s*(\d{1,2})\b",
    ]
    for p in patterns:
        m = re.search(p, t, flags=re.I)
        if m:
            age = int(m.group(1))
            if 14 <= age <= 90:
                return {"age": age, "age_confirmed_18_plus": age >= 18, "negative_age_marker": "" if age >= 18 else str(age), "age_evidence_text": raw.strip(), "age_confidence": "high", "age_source": "age_context"}

    # Bare age. Allowed after questionnaire gate, but still blocked when text is a work offer line.
    if not _is_interest_text(t):
        nums = [int(x) for x in re.findall(r"(?<!\d)(\d{1,2})\+?(?!\d)", t)]
        plausible = [x for x in nums if 14 <= x <= 90]
        if plausible:
            # Prefer last number in compact profile like "Россия 18".
            age = plausible[-1]
            return {"age": age, "age_confirmed_18_plus": age >= 18, "negative_age_marker": "" if age >= 18 else str(age), "age_evidence_text": raw.strip(), "age_confidence": "high", "age_source": "bare_profile_age"}

    return {"age": None, "age_confirmed_18_plus": False, "negative_age_marker": "", "age_evidence_text": "", "age_confidence": "none", "age_source": ""}


def _special_geo(text: str, rules: RuleSet) -> Optional[Dict[str, Any]]:
    raw = _key(text)
    if not raw:
        return None
    for loc in rules.geo_ru_special_locations:
        lk = _key(loc)
        if not lk:
            continue
        # Exact token/phrase match. Avoid fuzzy for special territories.
        if (" " + lk + " ") in (" " + raw + " ") or (" " in lk and lk in raw):
            city = None
            region = "GEO_OK"
            aliases_no_city = {"днр", "лнр", "донецкая народная республика", "луганская народная республика", "крым", "республика крым", "донецкая область", "луганская область"}
            if lk not in aliases_no_city and "область" not in lk and "республика" not in lk:
                city = loc.strip().capitalize() if loc.strip().isascii() is False else loc.strip()
            return {
                "city": city,
                "region": region,
                "country": "Россия",
                "geo_source": "special_geo_ok",
                "geo_confidence": "high",
                "geo_note": "GEO_OK special location: " + loc.strip(),
                "geo_evidence_text": str(text or "").strip(),
            }
    return None


def _as_bool(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "да", "on"):
        return True
    if s in ("0", "false", "no", "нет", "off"):
        return False
    return None


def _merge_messages(messages: Any) -> List[str]:
    if messages is None:
        return []
    if isinstance(messages, str):
        # profile_raw_text uses line-separated history.
        return [x.strip() for x in re.split(r"[\r\n]+", messages) if x.strip()]
    out = []
    for x in messages:
        sx = str(x or "").strip()
        if sx:
            out.append(sx)
    return out


def _decision(profile: Dict[str, Any]) -> Dict[str, Any]:
    # TPILOT STRICT COMPLETE PROFILE DECISION V3 20260516
    # Final liquid/nonliquid is allowed only when BOTH geo and age are known.
    country = str(profile.get("country") or "").strip()
    age = profile.get("age")
    try:
        age_i = int(age) if age is not None and str(age).strip() != "" else None
    except Exception:
        age_i = None
    age_confirmed = bool(profile.get("age_confirmed_18_plus") is True)
    negative = str(profile.get("negative_age_marker") or "").strip()

    age_known = bool(negative) or age_i is not None or age_confirmed
    geo_known = bool(country)

    if not geo_known or not age_known:
        missing = []
        if not geo_known:
            missing.append("гео")
        if not age_known:
            missing.append("возраст")
        reason = "Не хватает обязательных данных: " + ", ".join(missing)
        return {
            "status": "unknown", "nonliquid_reason": "", "profile_done": 0,
            "quality_status": "pending", "quality_bucket": "na_pending", "quality_reason": reason, "quality_confidence": "low",
            "profile_decision_reason": reason, "needs_review": 0,
        }

    if negative or (age_i is not None and age_i < 18):
        return {
            "status": "nonliquid", "nonliquid_reason": "-18", "profile_done": 1,
            "quality_status": "nonliquid", "quality_bucket": "under18", "quality_reason": "18+ не подтверждён", "quality_confidence": "high",
            "profile_decision_reason": f"Возраст не подходит: {negative or age_i}", "needs_review": 0,
        }
    if country and country != "Россия":
        return {
            "status": "nonliquid", "nonliquid_reason": country, "profile_done": 1,
            "quality_status": "nonliquid", "quality_bucket": "geo", "quality_reason": "ГЕО: " + country, "quality_confidence": "high",
            "profile_decision_reason": "Страна не Россия: " + country, "needs_review": 0,
        }
    if country == "Россия" and age_confirmed and age_i is not None and age_i >= 18:
        conf = str(profile.get("age_confidence") or "high")
        return {
            "status": "liquid", "nonliquid_reason": "", "profile_done": 1,
            "quality_status": "liquid", "quality_bucket": "liquid", "quality_reason": "Россия + 18+ подтверждён", "quality_confidence": conf if conf in ("medium", "high") else "high",
            "profile_decision_reason": "Россия/GEO_OK и 18+ подтверждён", "needs_review": 0,
        }
    return {
        "status": "unknown", "nonliquid_reason": "", "profile_done": 0,
        "quality_status": "pending", "quality_bucket": "na_pending", "quality_reason": "18+ не подтверждён", "quality_confidence": "low",
        "profile_decision_reason": "18+ не подтверждён", "needs_review": 0,
    }


def build_final_na_fields(reason: str = "NA после 3 уточнений") -> Dict[str, Any]:
    return {
        "status": "nonliquid",
        "nonliquid_reason": "NA",
        "profile_done": 1,
        "quality_status": "nonliquid",
        "quality_bucket": "na",
        "quality_reason": reason,
        "quality_confidence": "high",
        "quality_source": "profile_extractor_v2_final_na",
        "quality_version": VERSION,
        "profile_decision_reason": reason,
        "needs_review": 0,
        "extraction_method": "rules_v2",
        "extraction_confidence": "high",
        "profile_extraction_version": VERSION,
    }


def extract_profile_v2(messages: Any, previous_profile: Optional[Dict[str, Any]] = None, *, final_na: bool = False) -> Dict[str, Any]:
    rules = load_rules()
    prev = dict(previous_profile or {})
    msgs = _merge_messages(messages)
    profile: Dict[str, Any] = {
        "age": prev.get("age"),
        "city": str(prev.get("city") or "").strip() or None,
        "region": str(prev.get("region") or "").strip() or None,
        "country": str(prev.get("country") or "").strip() or None,
        "geo_source": str(prev.get("geo_source") or "").strip(),
        "geo_confidence": str(prev.get("geo_confidence") or "").strip() or "none",
        "geo_note": str(prev.get("geo_note") or "").strip(),
        "age_confirmed_18_plus": _as_bool(prev.get("age_confirmed_18_plus")) is True,
        "negative_age_marker": str(prev.get("negative_age_marker") or "").strip(),
        "age_evidence_text": str(prev.get("age_evidence_text") or "").strip(),
        "geo_evidence_text": str(prev.get("geo_evidence_text") or "").strip(),
        "age_confidence": str(prev.get("age_confidence") or "").strip() or "none",
        "country_confidence": str(prev.get("country_confidence") or "").strip() or "none",
        "city_confidence": str(prev.get("city_confidence") or "").strip() or "none",
    }

    for msg in msgs:
        age_res = _extract_age(msg, rules)
        if age_res.get("negative_age_marker"):
            profile["age"] = age_res.get("age")
            profile["age_confirmed_18_plus"] = False
            profile["negative_age_marker"] = age_res.get("negative_age_marker") or "negative_age"
            profile["age_evidence_text"] = age_res.get("age_evidence_text") or msg
            profile["age_confidence"] = age_res.get("age_confidence") or "high"
        elif age_res.get("age_confirmed_18_plus") is True:
            # Do not let older interest-message 18+ override a later negative marker; negative is handled above.
            profile["age"] = age_res.get("age")
            profile["age_confirmed_18_plus"] = True
            profile["age_evidence_text"] = age_res.get("age_evidence_text") or msg
            profile["age_confidence"] = age_res.get("age_confidence") or "high"
        elif age_res.get("age") is not None:
            profile["age"] = age_res.get("age")
            profile["age_confirmed_18_plus"] = False
            profile["negative_age_marker"] = str(age_res.get("negative_age_marker") or age_res.get("age") or "")
            profile["age_evidence_text"] = age_res.get("age_evidence_text") or msg
            profile["age_confidence"] = age_res.get("age_confidence") or "high"

        geo = _parse_geo_safe(msg, rules)
        if geo.get("country"):
            # Strong geo updates. Do not overwrite an existing exact city with a no-city country unless no country exists.
            new_city = str(geo.get("city") or "").strip()
            new_country = str(geo.get("country") or "").strip()
            new_region = str(geo.get("region") or "").strip()
            source = str(geo.get("geo_source") or "")
            if new_city or not profile.get("country") or source == "special_geo_ok":
                profile["city"] = new_city or profile.get("city")
                profile["region"] = new_region or profile.get("region")
                profile["country"] = new_country or profile.get("country")
                profile["geo_source"] = source
                profile["geo_confidence"] = str(geo.get("geo_confidence") or "high")
                profile["geo_note"] = str(geo.get("geo_note") or "") or str(profile.get("geo_note") or "")
                profile["geo_evidence_text"] = str(geo.get("geo_evidence_text") or msg)
                if new_country:
                    profile["country_confidence"] = "high"
                if new_city:
                    profile["city_confidence"] = "high"
            elif new_country and not profile.get("country"):
                profile["country"] = new_country

    if final_na:
        dec = build_final_na_fields()
    else:
        dec = _decision(profile)

    out: Dict[str, Any] = {}
    out.update(profile)
    out.update(dec)
    out["age_confirmed_18_plus"] = 1 if profile.get("age_confirmed_18_plus") else 0
    out["needs_review"] = int(out.get("needs_review") or 0)
    out["profile_extraction_version"] = VERSION
    out["quality_source"] = "profile_extractor_v2"
    out["quality_version"] = VERSION
    out["extraction_method"] = "rules_v2"
    out["extraction_confidence"] = str(out.get("quality_confidence") or "low")
    out["profile_evidence_text"] = "\n".join(msgs)[-4000:]
    out["profile_raw_text"] = "\n".join(msgs)[-4000:]
    out["city_raw"] = out.get("city") or ""
    out["country_raw"] = out.get("country") or ""
    # Normalize None values for DB friendliness.
    for k in ("city", "region", "country", "geo_source", "geo_confidence", "geo_note", "age_evidence_text", "geo_evidence_text", "profile_decision_reason", "negative_age_marker"):
        if out.get(k) is None:
            out[k] = ""
    return out


def _read_test_cases(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    p = path or (BASE_DIR / "profile_parser_test_cases.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def run_profile_tests(*, full: bool = False) -> Dict[str, Any]:
    cases = _read_test_cases()
    if not full:
        cases = cases[:20]
    results = []
    passed = 0
    for case in cases:
        name = str(case.get("name") or "case")
        messages = case.get("messages") or []
        expected = case.get("expected") or {}
        got = extract_profile_v2(messages, {})
        ok = True
        diffs = []
        for key, exp in expected.items():
            val = got.get(key)
            if key == "liquid":
                val = got.get("quality_bucket") == "liquid"
            if exp is None:
                match = val in (None, "")
            else:
                match = val == exp
            if not match:
                ok = False
                diffs.append(f"{key}: expected={exp!r}, got={val!r}")
        if ok:
            passed += 1
        results.append({"name": name, "ok": ok, "diffs": diffs})
    return {"total": len(cases), "passed": passed, "failed": len(cases) - passed, "results": results}


__all__ = [
    "VERSION", "load_rules", "reload_rules", "rules_summary", "extract_profile_v2",
    "build_final_na_fields", "run_profile_tests",
]

# --- TPILOT GEO PRIORITY GUARD V1.2 REPAIR SAFE START ---
# Patch id: geo_priority_guard_v1_2_repair_safe_20260516
# Conservative rule:
# - Direct explicit geo only. No noisy router foreign guesses.
# - Direct Russia aliases win over accidental foreign guesses.
# - Explicit foreign city/country wins when it is actually stated by the client.
# - First-message policy is handled outside: this extractor receives only allowed post-first texts.

GEO_PRIORITY_GUARD_VERSION = "geo_priority_guard_v1_2_repair_safe_20260516"

# Country, city, aliases. Aliases are matched token/phrase safely, not as substrings.
_GP11_FOREIGN = [
    ("Узбекистан", "Ташкент", ["ташкент", "ташкента", "ташкенте", "ташкенту", "тошкент", "тошкенте", "тошкенту"]),
    ("Узбекистан", "Фергана", ["фергана", "ферганы", "фергане", "фергану", "фаргона"]),
    ("Узбекистан", "Ургенч", ["ургенч"]),
    ("Узбекистан", "Карши", ["карши"]),
    ("Узбекистан", "Жиззах", ["жиззах", "джизак"]),
    ("Узбекистан", "Самарканд", ["самарканд"]),
    ("Узбекистан", "Сурхандарья", ["сурхандарья", "сурхандарйо", "сурхандарыйо", "сурхандария", "сурхондаре", "сурхондаре", "сурхондарё", "сурхондарйо", "термез"]),
    ("Узбекистан", "Бухара", ["бухара"]),
    ("Узбекистан", "Андижан", ["андижан"]),
    ("Узбекистан", "Наманган", ["наманган"]),
    ("Узбекистан", None, ["узбекистан", "узбекистане", "узбекистана", "узбек", "узб", "нарпай", "narpay"]),

    ("Казахстан", "Павлодар", ["павлодар"]),
    ("Казахстан", "Кентау", ["кентау"]),
    ("Казахстан", "Алматы", ["алматы", "алма ата", "алма-ата"]),
    ("Казахстан", "Астана", ["астана", "нур султан", "нур-султан"]),
    ("Казахстан", "Караганда", ["караганда"]),
    ("Казахстан", "Шымкент", ["шымкент", "чимкент"]),
    ("Казахстан", None, ["казахстан", "казахстана", "казахстане", "казах"]),

    ("Кыргызстан", "Бишкек", ["бишкек", "бишкеке", "бишкека"]),
    ("Кыргызстан", "Ош", ["ош", "оше"]),
    ("Кыргызстан", None, ["кыргызстан", "киргизия", "кыргызстана", "киргизии", "кыргыз", "киргиз"]),

    ("Украина", "Киев", ["киев", "київ"]),
    ("Украина", "Одесса", ["одесса"]),
    ("Украина", "Чернигов", ["чернигов", "чернігів"]),
    ("Украина", "Волынская область", ["волынская область", "волинська область", "волынь", "волинь"]),
    ("Украина", None, ["украина", "украине", "украины", "україна", "україні"]),

    ("Беларусь", "Минск", ["минск"]),
    ("Беларусь", "Гомель", ["гомель", "гомельская область"]),
    ("Беларусь", "Брест", ["брест"]),
    ("Беларусь", None, ["беларусь", "белоруссия", "беларуси"]),

    ("Молдова", "Тирасполь", ["тирасполь"]),
    ("Молдова", None, ["молдова", "молдове", "молдавия", "молдавии"]),

    ("Польша", "Варшава", ["варшава"]),
    ("Польша", "Щецин", ["щецин", "szczecin"]),
    ("Польша", "Познань", ["познань", "познани"]),
    ("Польша", None, ["польша", "польше", "польши"]),

    ("Южная Корея", None, ["южная корея", "южной корее", "сеул"]),
    ("Болгария", "Варна", ["варна"]),
    ("Египет", None, ["египет", "египте", "египта"]),
    ("Индия", "Раджастхан", ["раджастхан", "rajasthan"]),
    ("Индия", None, ["индия", "индии", "india"]),
    ("Таджикистан", "Душанбе", ["душанбе"]),
    ("Таджикистан", None, ["таджикистан", "таджикистана", "таджикистане"]),
    ("Азербайджан", "Баку", ["баку"]),
    ("Азербайджан", None, ["азербайджан", "азербайджана", "азербайджане"]),
    ("Армения", "Ереван", ["ереван"]),
    ("Армения", None, ["армения", "армении"]),
    ("Грузия", "Тбилиси", ["тбилиси"]),
    ("Грузия", None, ["грузия", "грузии"]),
    ("Турция", "Стамбул", ["стамбул", "истанбул", "istanbul"]),
    ("Турция", "Мерсин", ["мерсин"]),
    ("Турция", None, ["турция", "турции"]),
]

_GP11_RU = [
    ("Москва", ["москва", "москве", "москвы", "мск"]),
    ("Санкт-Петербург", ["санкт петербург", "санкт-петербург", "спб", "питер", "санпетербург", "санпетбург", "saint petersburg", "st petersburg"]),
    ("Казань", ["казань", "казани", "казан"]),
    ("Республика Ингушетия", ["республика ингушетия", "ингушетия", "ингушетии", "сунжа"]),
    ("Грозный", ["грозный"]),
    ("Самара", ["самара"]),
    ("Курск", ["курск"]),
    ("Екатеринбург", ["екатеринбург", "екб"]),
    ("Уфа", ["уфа"]),
    ("Нальчик", ["нальчик"]),
    ("Липецк", ["липецк"]),
    ("Краснодар", ["краснодар"]),
    ("Калининград", ["калининград"]),
    ("Магнитогорск", ["магнитогорск"]),
    ("Омск", ["омск"]),
    ("Нижнеудинск", ["нижнеудинск"]),
    ("Йошкар-Ола", ["йошкар ола", "йошкар-ола"]),
    ("Воронеж", ["воронеж"]),
    ("Абакан", ["абакан"]),
    ("Красногорск", ["красногорск"]),
    ("Красноярск", ["красноярск"]),
    ("Новосибирск", ["новосибирск"]),
    ("Благовещенск", ["благовещенск"]),
    ("Улан-Удэ", ["улан удэ", "улан-удэ"]),
    ("Курган", ["курган", "курганская область", "кургански облис"]),
    ("Смоленск", ["смоленск"]),
    ("Свирск", ["свирск"]),
    ("Киров", ["киров", "кировская область", "котельнич"]),
    ("Московская область", ["московская область", "московский область", "подмосковье", "путилково", "путилкова"]),
    ("Республика Алтай", ["республика алтай", "республики алтай", "маймински район", "майминский район"]),
    ("Орск", ["орск"]),
    ("Пенза", ["пенза"]),
    ("Черкесск", ["черкесск"]),
]

_GP11_ROUTE_OR_AD_HINTS = [
    "актуальные грузы", "йуналишлар", "йўналишлар", "маршрут", "направление", "груз готов",
    "россия разные города", "разные города", "макулатура", "тонна", "тент", "оплата нал",
]

_GP11_WORK_ROUTE_RE = r"\b(?:работал|работала|работаю|работаем|работать|доставка|доставк\w*)\s+(?:на|в)\s+(?:питер|спб|москв\w*)\b"


def _gp11_phrase_in(text_key: str, phrase: str) -> bool:
    pk = _key(phrase)
    if not pk:
        return False
    tt = _tokens(text_key)
    pp = _tokens(pk)
    if not pp:
        return False
    if len(pp) == 1:
        return pp[0] in tt
    for i in range(0, max(0, len(tt) - len(pp) + 1)):
        if tt[i:i + len(pp)] == pp:
            return True
    return False


def _gp11_has_any(text_key: str, phrases: Iterable[str]) -> bool:
    return any(_gp11_phrase_in(text_key, p) for p in phrases)


def _gp11_is_ad_or_route(text_key: str) -> bool:
    return _gp11_has_any(text_key, _GP11_ROUTE_OR_AD_HINTS)


def _gp11_false_ru_work_route(text_key: str) -> bool:
    return bool(re.search(_GP11_WORK_ROUTE_RE, text_key, flags=re.I))


def _gp11_find_ru(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    k = _key(raw)
    if not k:
        return None
    if _gp11_is_ad_or_route(k) or _gp11_false_ru_work_route(k):
        return None
    for city, aliases in _GP11_RU:
        for alias in aliases:
            if _gp11_phrase_in(k, alias):
                return {
                    "city": city,
                    "region": "Россия",
                    "country": "Россия",
                    "geo_source": "geo_priority_ru_alias_safe",
                    "geo_confidence": "high",
                    "geo_note": "RU direct alias: " + alias,
                    "geo_evidence_text": raw,
                }
    if re.search(r"\b(россия|рф|россии|россию|российская\s+федерация)\b", k):
        return {
            "city": None,
            "region": "Россия",
            "country": "Россия",
            "geo_source": "geo_priority_ru_country_safe",
            "geo_confidence": "high",
            "geo_note": "Explicit Russia country",
            "geo_evidence_text": raw,
        }
    return None


def _gp11_find_foreign(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    k = _key(raw)
    if not k:
        return None
    if _gp11_is_ad_or_route(k):
        return None
    matches: List[Tuple[str, Optional[str], str, int]] = []
    for country, city, aliases in _GP11_FOREIGN:
        for alias in aliases:
            if _gp11_phrase_in(k, alias):
                # priority: explicit country aliases > city aliases
                pr = 2 if city is None else 1
                matches.append((country, city, alias, pr))
                break
    if not matches:
        return None
    matches.sort(key=lambda x: x[3], reverse=True)
    country, city, alias, _ = matches[0]
    return {
        "city": city,
        "region": country,
        "country": country,
        "geo_source": "geo_priority_foreign_alias_safe",
        "geo_confidence": "high",
        "geo_note": "Foreign direct alias: " + alias,
        "geo_evidence_text": raw,
    }


def _gp11_current_ru_override(text: str) -> Optional[Dict[str, Any]]:
    raw = str(text or "").strip()
    k = _key(raw)
    ru = _gp11_find_ru(raw)
    if not ru:
        return None
    if re.search(r"\b(?:сейчас|нахожусь|живу|проживаю|в\s+данный\s+момент)\s+в\b", k, flags=re.I):
        return ru
    return None


def _parse_geo_safe(text: str, rules: RuleSet) -> Dict[str, Any]:  # type: ignore[override]
    raw = str(text or "").strip()
    if not raw:
        return {"city": None, "region": None, "country": None, "geo_source": "", "geo_confidence": "none", "geo_note": "", "geo_evidence_text": ""}

    if _gp11_is_ad_or_route(_key(raw)):
        return {"city": None, "region": None, "country": None, "geo_source": "ad_or_route_skip_geo_safe", "geo_confidence": "none", "geo_note": "ad/router text is not current client geo", "geo_evidence_text": ""}

    sp = _special_geo(raw, rules)
    if sp:
        sp["geo_source"] = str(sp.get("geo_source") or "special_geo_ok")
        return sp

    current_ru = _gp11_current_ru_override(raw)
    if current_ru:
        return current_ru

    ru = _gp11_find_ru(raw)
    foreign = _gp11_find_foreign(raw)

    # When both are present in one message, direct RU wins unless the text explicitly says current foreign/origin foreign.
    # Example: "Москва 35 ... Европа" must remain Russia; "я из Казахстана, сейчас в Москве" is handled by current_ru above.
    if ru and not foreign:
        return ru
    if foreign and not ru:
        return foreign
    if ru and foreign:
        k = _key(raw)
        if re.search(r"\b(?:я\s+из|я\s+с|из\s+города|с\s+города|живу\s+в|нахожусь\s+в|в\s+данный\s+момент\s+в)\b", k, flags=re.I):
            # If current/location wording is explicitly attached to a foreign alias, foreign wins.
            return foreign
        return ru

    if _gp11_false_ru_work_route(_key(raw)):
        return {"city": None, "region": None, "country": None, "geo_source": "work_route_skip_ru_safe", "geo_confidence": "none", "geo_note": "work route phrase is not current geo", "geo_evidence_text": ""}

    if _looks_like_age_message(raw) and not _has_geo_context(raw):
        low = _key(raw)
        if not re.search(r"\b(россия|рф|казахстан|украина|беларусь|узбекистан|молдова|таджикистан|азербайджан|кыргызстан|киргизия|польша|южная\s+корея|болгария|турция)\b", low):
            return {"city": None, "region": None, "country": None, "geo_source": "age_message_skip_geo", "geo_confidence": "none", "geo_note": "", "geo_evidence_text": ""}
    if _is_interest_text(raw) and not _has_geo_context(raw) and not re.search(r"\b(россия|рф|казахстан|украина|беларусь|узбекистан|молдова|таджикистан|азербайджан|кыргызстан|киргизия|польша|южная\s+корея|болгария|турция)\b", _key(raw)):
        return {"city": None, "region": None, "country": None, "geo_source": "interest_skip_geo", "geo_confidence": "none", "geo_note": "", "geo_evidence_text": ""}

    if _router is None or not hasattr(_router, "parse_geo"):
        return {"city": None, "region": None, "country": None, "geo_source": "", "geo_confidence": "none", "geo_note": "", "geo_evidence_text": ""}
    got = _router.parse_geo(raw) or {}
    country = got.get("country")
    city = got.get("city")
    if city and _key(city) in {_key(x) for x in rules.city_blacklist}:
        return {"city": None, "region": None, "country": None, "geo_source": "city_blacklist", "geo_confidence": "none", "geo_note": "blocked_city_candidate", "geo_evidence_text": ""}

    if country:
        # Do not trust router foreign guesses unless our direct foreign matcher also saw the foreign geo.
        if str(country).strip() != "Россия":
            foreign2 = _gp11_find_foreign(raw)
            if foreign2:
                return foreign2
            return {"city": None, "region": None, "country": None, "geo_source": "router_foreign_ignored_safe", "geo_confidence": "none", "geo_note": "ignored noisy router foreign guess", "geo_evidence_text": ""}
        return {
            "city": city,
            "region": got.get("region"),
            "country": country,
            "geo_source": got.get("source") or "router",
            "geo_confidence": got.get("confidence") or "medium",
            "geo_note": got.get("note") or "",
            "geo_evidence_text": raw,
        }
    return {"city": None, "region": None, "country": None, "geo_source": got.get("source") or "", "geo_confidence": got.get("confidence") or "none", "geo_note": got.get("note") or "", "geo_evidence_text": ""}


def extract_profile_v2(messages: Any, previous_profile: Optional[Dict[str, Any]] = None, *, final_na: bool = False) -> Dict[str, Any]:  # type: ignore[override]
    rules = load_rules()
    prev = dict(previous_profile or {})
    msgs = _merge_messages(messages)
    profile: Dict[str, Any] = {
        "age": prev.get("age"),
        "city": str(prev.get("city") or "").strip() or None,
        "region": str(prev.get("region") or "").strip() or None,
        "country": str(prev.get("country") or "").strip() or None,
        "geo_source": str(prev.get("geo_source") or "").strip(),
        "geo_confidence": str(prev.get("geo_confidence") or "").strip() or "none",
        "geo_note": str(prev.get("geo_note") or "").strip(),
        "age_confirmed_18_plus": _as_bool(prev.get("age_confirmed_18_plus")) is True,
        "negative_age_marker": str(prev.get("negative_age_marker") or "").strip(),
        "age_evidence_text": str(prev.get("age_evidence_text") or "").strip(),
        "geo_evidence_text": str(prev.get("geo_evidence_text") or "").strip(),
        "age_confidence": str(prev.get("age_confidence") or "").strip() or "none",
        "country_confidence": str(prev.get("country_confidence") or "").strip() or "none",
        "city_confidence": str(prev.get("city_confidence") or "").strip() or "none",
    }

    for msg in msgs:
        age_res = _extract_age(msg, rules)
        if age_res.get("negative_age_marker"):
            profile["age"] = age_res.get("age")
            profile["age_confirmed_18_plus"] = False
            profile["negative_age_marker"] = age_res.get("negative_age_marker") or "negative_age"
            profile["age_evidence_text"] = age_res.get("age_evidence_text") or msg
            profile["age_confidence"] = age_res.get("age_confidence") or "high"
        elif age_res.get("age_confirmed_18_plus") is True:
            profile["age"] = age_res.get("age")
            profile["age_confirmed_18_plus"] = True
            profile["age_evidence_text"] = age_res.get("age_evidence_text") or msg
            profile["age_confidence"] = age_res.get("age_confidence") or "high"
        elif age_res.get("age") is not None:
            profile["age"] = age_res.get("age")
            profile["age_confirmed_18_plus"] = False
            profile["negative_age_marker"] = str(age_res.get("negative_age_marker") or age_res.get("age") or "")
            profile["age_evidence_text"] = age_res.get("age_evidence_text") or msg
            profile["age_confidence"] = age_res.get("age_confidence") or "high"

        geo = _parse_geo_safe(msg, rules)
        if geo.get("country"):
            new_city = str(geo.get("city") or "").strip()
            new_country = str(geo.get("country") or "").strip()
            new_region = str(geo.get("region") or "").strip()
            source = str(geo.get("geo_source") or "")
            current_country = str(profile.get("country") or "").strip()
            current_source = str(profile.get("geo_source") or "")

            should_update = False
            if source == "special_geo_ok":
                should_update = True
            elif not current_country:
                should_update = True
            elif current_country == "Россия" and new_country == "Россия" and new_city:
                should_update = True
            elif current_country != "Россия" and new_country == "Россия":
                # Only explicit current-Russia phrase can override already known foreign geo.
                if _gp11_current_ru_override(str(msg or "")):
                    should_update = True
            elif current_country == "Россия" and new_country != "Россия":
                # Do not let later noisy foreign phrases override direct Russia.
                # Foreign can override only when it is explicit current/origin location phrase in same message.
                k = _key(str(msg or ""))
                if re.search(r"\b(?:я\s+из|я\s+с|из\s+города|с\s+города|живу\s+в|нахожусь\s+в|в\s+данный\s+момент\s+в)\b", k, flags=re.I):
                    should_update = True
            elif current_country != "Россия" and new_country != "Россия":
                # Explicit country/city can refine foreign geo.
                should_update = True

            if should_update:
                profile["city"] = new_city or (None if new_country != "Россия" else profile.get("city"))
                profile["region"] = new_region or profile.get("region")
                profile["country"] = new_country or profile.get("country")
                profile["geo_source"] = source
                profile["geo_confidence"] = str(geo.get("geo_confidence") or "high")
                profile["geo_note"] = str(geo.get("geo_note") or "") or str(profile.get("geo_note") or "")
                profile["geo_evidence_text"] = str(geo.get("geo_evidence_text") or msg)
                if new_country:
                    profile["country_confidence"] = "high"
                if new_city:
                    profile["city_confidence"] = "high"

    dec = build_final_na_fields() if final_na else _decision(profile)

    out: Dict[str, Any] = {}
    out.update(profile)
    out.update(dec)
    out["age_confirmed_18_plus"] = 1 if profile.get("age_confirmed_18_plus") else 0
    out["needs_review"] = int(out.get("needs_review") or 0)
    out["profile_extraction_version"] = GEO_PRIORITY_GUARD_VERSION
    out["quality_source"] = "profile_extractor_geo_priority_guard_safe"
    out["quality_version"] = GEO_PRIORITY_GUARD_VERSION
    out["extraction_method"] = "rules_v2_geo_priority_guard_safe"
    out["extraction_confidence"] = str(out.get("quality_confidence") or "low")
    out["profile_evidence_text"] = "\n".join(msgs)[-4000:]
    out["profile_raw_text"] = "\n".join(msgs)[-4000:]
    out["city_raw"] = out.get("city") or ""
    out["country_raw"] = out.get("country") or ""
    for k in ("city", "region", "country", "geo_source", "geo_confidence", "geo_note", "age_evidence_text", "geo_evidence_text", "profile_decision_reason", "negative_age_marker"):
        if out.get(k) is None:
            out[k] = ""
    return out

# --- TPILOT GEO PRIORITY GUARD V1.2 REPAIR SAFE END ---
