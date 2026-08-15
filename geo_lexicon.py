# -*- coding: utf-8 -*-
"""
TPilot offline geo lexicon — Stage D (corrected).

Primary sources of truth (imported fail-open at module load):
  liquid_ru_locations.RU_LOCATIONS   — all Russian settlements: key->(canonical,(region,"России"))
  non_liquid_locations.PLACES_POPULAR, COUNTRIES — non-liquid world geo
  ua_locations.UA_LOCATIONS          — set of Ukrainian location tokens

Built-in alias layer (tiny; only abbreviations / typos not in source files):
  МО     -> Московская область, liquid_ru  (no city unless paired with a city name)
  РФ / рашка -> Russia signal, liquid_ru
  МСК / масква -> redirect lookup to "москва" in RU_LOCATIONS
  СПБ / Питер  -> redirect lookup to "санкт петербург" in RU_LOCATIONS
  НН           -> redirect lookup to "нижний новгород" in RU_LOCATIONS
  нижний       -> candidate for Нижний Новгород (ambiguous when alone)

Country abbreviations (укр, кз, рб, by, ua …) are already in COUNTRIES via
_POST_SOVIET_COUNTRY_SYNONYMS merge — no alias needed.

Output keys (all optional except normalized_text, geo_class, confidence):
  normalized_text   str        — normalised input
  geo_class         str        — "liquid_ru" | "non_liquid_geo" | "ua_geo" | "unknown"
  country           str        — canonical country name
  region            str        — region / oblast
  city              str        — canonical city name
  matched_terms     list[str]  — which terms triggered a match
  candidates        list[str]  — ambiguous / secondary matches
  source            str        — first source that matched
  notes             list[str]  — debug notes for shadow log

No network, no DB writes, no Telegram. All functions fail-open (never raise).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

# ── Normalisation ──────────────────────────────────────────────────────────

_YO_TABLE = str.maketrans("ёЁ", "еЕ")
# Hyphens -> space: RU_LOCATIONS has both "санкт-петербург" and "санкт петербург",
# so converting hyphens to spaces is safe and simplifies token splitting.
_PUNCT_RE = re.compile(r"[,\.!?;:«»\"'()\[\]{}—\-]+")
_SPACE_RE = re.compile(r"\s+")


def normalize_geo_text(text: str) -> str:
    """Lowercase, ё->е, strip punctuation (hyphens->space), collapse whitespace."""
    t = str(text or "").translate(_YO_TABLE).lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _SPACE_RE.sub(" ", t).strip()
    return t


# ── Source loading (fail-open) ─────────────────────────────────────────────

_RU_LOCATIONS: Dict[str, Any] = {}
_NL_POPULAR: Dict[str, Any] = {}
_NL_COUNTRIES: Dict[str, Any] = {}
_UA_LOCATIONS: Set[str] = set()
_source_status: Dict[str, str] = {}


def _load_sources() -> None:
    global _RU_LOCATIONS, _NL_POPULAR, _NL_COUNTRIES, _UA_LOCATIONS
    try:
        from liquid_ru_locations import RU_LOCATIONS
        _RU_LOCATIONS = RU_LOCATIONS
        _source_status["liquid_ru"] = "ok"
    except Exception as exc:
        _source_status["liquid_ru"] = f"fail:{str(exc)[:80]}"
    try:
        from non_liquid_locations import PLACES_POPULAR, COUNTRIES
        _NL_POPULAR = PLACES_POPULAR
        _NL_COUNTRIES = COUNTRIES
        _source_status["non_liquid"] = "ok"
    except Exception as exc:
        _source_status["non_liquid"] = f"fail:{str(exc)[:80]}"
    try:
        from ua_locations import UA_LOCATIONS
        _UA_LOCATIONS = UA_LOCATIONS
        _source_status["ua"] = "ok"
    except Exception as exc:
        _source_status["ua"] = f"fail:{str(exc)[:80]}"


_load_sources()


def get_source_status() -> Dict[str, str]:
    """Return load status of each source file (for diagnostics)."""
    return dict(_source_status)


# ── Alias layer (abbreviations / typos not resolvable via source files) ────

# Resolve to region/country without a specific city
_REGION_ALIASES: Dict[str, Dict[str, str]] = {
    "мо":    {"region": "Московская область", "country": "Россия", "geo_class": "liquid_ru"},
    "рф":    {"country": "Россия", "geo_class": "liquid_ru"},
    "рашка": {"country": "Россия", "geo_class": "liquid_ru"},
    "россия": {"country": "Россия", "geo_class": "liquid_ru"},
}

# Redirect to a normalized RU_LOCATIONS key; candidate=True = ambiguous alone
_CITY_ALIASES: Dict[str, Dict[str, Any]] = {
    "мск":    {"lookup": "москва",          "candidate": False},
    "масква": {"lookup": "москва",          "candidate": False},
    "спб":    {"lookup": "санкт петербург", "candidate": False},
    "питер":  {"lookup": "санкт петербург", "candidate": False},
    "нн":     {"lookup": "нижний новгород", "candidate": False},
    "нижний": {"lookup": "нижний новгород", "candidate": True},
}

_NOISE_TOKENS: frozenset = frozenset([
    "да", "нет", "ок", "ладно", "хорошо", "понял", "понятно",
    "привет", "здравствуйте", "добрый", "день", "вечер", "утро",
    "спасибо", "пожалуйста", "конечно", "работа", "работать",
    "вы", "я", "мы", "не", "но", "и", "а", "в", "на", "из",
    "мне", "лет", "год", "это", "так", "там", "вот", "ну",
])


def _is_likely_age(tok: str) -> bool:
    return tok.isdigit() and 14 <= int(tok) <= 90


def _skip_token(tok: str) -> bool:
    return _is_likely_age(tok) or tok in _NOISE_TOKENS or len(tok) < 2


# ── Single-phrase lookup (used for 1–3 token ngrams) ──────────────────────

def _try_match_phrase(
    phrase: str,
    result: Dict[str, Any],
    notes: List[str],
    candidate_mode: bool = False,
) -> bool:
    """Try to match a normalised phrase against all sources. Updates result in-place.
    Returns True if any source matched."""

    # ── 1. Region/country aliases ─────────────────────────────────────────
    if phrase in _REGION_ALIASES:
        ae = _REGION_ALIASES[phrase]
        notes.append(f"region_alias:{phrase}")
        result.setdefault("matched_terms", []).append(phrase)
        if "geo_class" not in result:
            result["geo_class"] = ae["geo_class"]
        if "country" not in result and "country" in ae:
            result["country"] = ae["country"]
        if "region" not in result and "region" in ae:
            result["region"] = ae["region"]
        result.setdefault("source", "alias")
        return True

    # ── 2. City aliases -> RU_LOCATIONS lookup ────────────────────────────
    if phrase in _CITY_ALIASES:
        ae = _CITY_ALIASES[phrase]
        lookup_key = ae["lookup"]
        is_cand = ae["candidate"] or candidate_mode
        rv = _RU_LOCATIONS.get(lookup_key)
        if rv is not None:
            canonical, (region, _) = rv
            notes.append(f"city_alias:{phrase}->{canonical}")
            result.setdefault("matched_terms", []).append(phrase)
            if is_cand:
                result.setdefault("candidates", []).append(canonical)
            else:
                if "city" not in result:
                    result["city"] = canonical
                if "region" not in result:
                    result["region"] = region
            if "country" not in result:
                result["country"] = "Россия"
            if "geo_class" not in result:
                result["geo_class"] = "liquid_ru"
            result.setdefault("source", "alias")
            return True

    # ── 3. UA_LOCATIONS set (Ukraine signal) ─────────────────────────────
    if phrase in _UA_LOCATIONS:
        notes.append(f"ua_set:{phrase}")
        result.setdefault("matched_terms", []).append(phrase)
        if "country" not in result:
            result["country"] = "Украина"
        if "geo_class" not in result:
            result["geo_class"] = "ua_geo"
        result.setdefault("source", "ua_locations")
        return True

    # ── 4. RU_LOCATIONS (primary Russia source) ───────────────────────────
    rv = _RU_LOCATIONS.get(phrase)
    if rv is not None:
        canonical, (region, _) = rv
        # Skip entries that look like dates, numbers, or very short artefacts
        if canonical and not canonical[0].isdigit() and len(canonical) > 1:
            # Conflict guard: if NL_POPULAR has this phrase with a non-Russian country
            # (e.g. "минск" exists as a minor Russian village but also as Minsk/Belarus
            # in PLACES_POPULAR), the world-known city takes priority.
            _pv_conflict = _NL_POPULAR.get(phrase)
            if _pv_conflict is not None and _pv_conflict[1] not in ("Россия", "Russia", "РФ"):
                pass  # fall through to NL_POPULAR at step 5
            else:
                notes.append(f"ru:{phrase}->{canonical}")
                result.setdefault("matched_terms", []).append(phrase)
                if candidate_mode:
                    result.setdefault("candidates", []).append(canonical)
                else:
                    if "city" not in result:
                        result["city"] = canonical
                    if "region" not in result:
                        result["region"] = region
                if "country" not in result:
                    result["country"] = "Россия"
                if "geo_class" not in result:
                    result["geo_class"] = "liquid_ru"
                result.setdefault("source", "ru_locations")
                return True

    # ── 5. PLACES_POPULAR (non-liquid cities / world places) ─────────────
    pv = _NL_POPULAR.get(phrase)
    if pv is not None:
        city_name, country_name = pv[0], pv[1]
        notes.append(f"nl_popular:{phrase}->{city_name}/{country_name}")
        result.setdefault("matched_terms", []).append(phrase)
        if candidate_mode:
            result.setdefault("candidates", []).append(city_name)
        else:
            if "city" not in result:
                result["city"] = city_name
        if "country" not in result:
            result["country"] = country_name
        geo_cls = "ua_geo" if country_name == "Украина" else "non_liquid_geo"
        if "geo_class" not in result:
            result["geo_class"] = geo_cls
        result.setdefault("source", "nl_popular")
        return True

    # ── 6. COUNTRIES (country-level signal) ──────────────────────────────
    cv = _NL_COUNTRIES.get(phrase)
    if cv is not None:
        country_name = cv[0]  # canonical country name (first element)
        notes.append(f"nl_country:{phrase}->{country_name}")
        result.setdefault("matched_terms", []).append(phrase)
        if "country" not in result:
            result["country"] = country_name
        geo_cls = "ua_geo" if country_name == "Украина" else "non_liquid_geo"
        if "geo_class" not in result:
            result["geo_class"] = geo_cls
        result.setdefault("source", "nl_countries")
        return True

    return False


# ── Core extraction ────────────────────────────────────────────────────────

def extract_geo_hints(text: str) -> Dict[str, Any]:
    """Extract geo hints from a single text string. Never raises."""
    try:
        return _extract_impl(text)
    except Exception:
        return {
            "normalized_text": "", "geo_class": "unknown", "confidence": "low",
            "notes": ["extraction_error"],
        }


def _extract_impl(text: str) -> Dict[str, Any]:
    norm = normalize_geo_text(text)
    toks = norm.split() if norm else []

    # geo_class is NOT initialized here — _try_match_phrase uses "not in result" guard.
    # It is set to "unknown" only at the end via setdefault().
    result: Dict[str, Any] = {"normalized_text": norm}
    notes: List[str] = []

    if not toks:
        result["geo_class"] = "unknown"
        result["confidence"] = "low"
        result["notes"] = ["empty"]
        return result

    # Positions consumed by a longer (bigram/trigram) match
    consumed: Set[int] = set()
    n = len(toks)

    # ── Pass 1: trigrams ──────────────────────────────────────────────────
    for i in range(n - 2):
        if i in consumed or (i + 1) in consumed or (i + 2) in consumed:
            continue
        phrase = f"{toks[i]} {toks[i+1]} {toks[i+2]}"
        if _try_match_phrase(phrase, result, notes):
            consumed |= {i, i + 1, i + 2}

    # ── Pass 2: bigrams ───────────────────────────────────────────────────
    for i in range(n - 1):
        if i in consumed or (i + 1) in consumed:
            continue
        phrase = f"{toks[i]} {toks[i+1]}"
        if _try_match_phrase(phrase, result, notes):
            consumed |= {i, i + 1}

    # ── Pass 3: single tokens ─────────────────────────────────────────────
    for i, tok in enumerate(toks):
        if i in consumed or _skip_token(tok):
            continue
        _try_match_phrase(tok, result, notes)

    # ── Confidence ────────────────────────────────────────────────────────
    geo_class = result.get("geo_class", "unknown")
    city = result.get("city")
    region = result.get("region")
    country = result.get("country")

    if geo_class == "unknown":
        confidence = "low"
    elif city and (country or region):
        confidence = "high"
    elif city or (region and geo_class == "liquid_ru"):
        confidence = "medium"
    elif geo_class in ("liquid_ru", "ua_geo", "non_liquid_geo") and (country or region):
        confidence = "low"
    else:
        confidence = "low"

    result.setdefault("geo_class", "unknown")
    result["confidence"] = confidence

    if not result.get("matched_terms"):
        result.pop("matched_terms", None)
    if not result.get("candidates"):
        result.pop("candidates", None)

    if notes:
        result["notes"] = notes

    return result


def extract_geo_hints_from_messages(messages: List[str]) -> Dict[str, Any]:
    """Extract geo hints from a burst of messages. Never raises."""
    try:
        return _extract_from_messages_impl(messages)
    except Exception:
        return {
            "normalized_text": "", "geo_class": "unknown", "confidence": "low",
            "notes": ["extraction_error"],
        }


def _extract_from_messages_impl(messages: List[str]) -> Dict[str, Any]:
    if not messages:
        return {
            "normalized_text": "", "geo_class": "unknown", "confidence": "low",
            "notes": ["no_messages"],
        }

    if len(messages) == 1:
        return extract_geo_hints(messages[0])

    # Primary: analyze combined text (handles cross-message: "МО" + "Дубна" -> Дубна/МО)
    combined = " ".join(str(m) for m in messages if m)
    merged = extract_geo_hints(combined)

    # Secondary: fill any gaps from per-message analysis
    for msg in messages:
        if not msg:
            continue
        r = extract_geo_hints(str(msg))
        if merged.get("geo_class") == "unknown" and r.get("geo_class") != "unknown":
            merged["geo_class"] = r["geo_class"]
        if "city" not in merged and "city" in r:
            merged["city"] = r["city"]
        if "region" not in merged and "region" in r:
            merged["region"] = r["region"]
        if "country" not in merged and "country" in r:
            merged["country"] = r["country"]

    # Recalculate confidence after merge
    geo_class = merged.get("geo_class", "unknown")
    city = merged.get("city")
    region = merged.get("region")
    country = merged.get("country")
    if geo_class == "unknown":
        merged["confidence"] = "low"
    elif city and (country or region):
        merged["confidence"] = "high"
    elif city or (region and geo_class == "liquid_ru"):
        merged["confidence"] = "medium"
    elif geo_class in ("liquid_ru", "ua_geo", "non_liquid_geo") and (country or region):
        merged["confidence"] = "low"
    else:
        merged["confidence"] = "low"

    return merged
