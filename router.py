# -*- coding: utf-8 -*-
from __future__ import annotations

"""Age + geo detection helpers for ALM_TPilot / TPilot lead tracker.

The module is intentionally offline-first:
- liquid_ru_locations.RU_LOCATIONS for Russia;
- non_liquid_locations dictionaries for non-RU countries/cities;
- ua_locations.UA_LOCATIONS for Ukraine priority detection;
- local aliases for common Russian/CIS latin spellings and abbreviations.
"""

import re
import unicodedata
from typing import Any, Dict, Optional, Tuple

try:
    from liquid_ru_locations import RU_LOCATIONS as _RU_LOCATIONS  # type: ignore
except Exception:
    _RU_LOCATIONS = {}

try:
    import non_liquid_locations as _NLIQ  # type: ignore
except Exception:
    _NLIQ = None

try:
    from ua_locations import UA_LOCATIONS as _UA_LOCATIONS_RAW  # type: ignore
except Exception:
    _UA_LOCATIONS_RAW = set()


def _norm(text: Any) -> str:
    s = str(text or "").strip()
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("ё", "е").replace("Ё", "Е")
    s = s.lower()
    s = re.sub(r"[‐‑‒–—−]", "-", s)
    s = re.sub(r"[\t\r\n]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _key(text: Any) -> str:
    s = _norm(text)
    s = re.sub(r"[^0-9a-zа-яіїєґ\s\-]+", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _compact_key(text: Any) -> str:
    return re.sub(r"[\s\-]+", "", _key(text))


def _title_city(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    parts = re.split(r"([\s\-]+)", s)
    return "".join(p.capitalize() if p and not re.fullmatch(r"[\s\-]+", p) else p for p in parts)


# ----------------------------
# Source indexes
# ----------------------------
RU_LOCATIONS: Dict[str, Any] = {}
for k, v in dict(_RU_LOCATIONS or {}).items():
    kk = _key(k)
    if kk:
        RU_LOCATIONS[kk] = v

UA_LOCATIONS = {_key(x) for x in (_UA_LOCATIONS_RAW or set()) if _key(x)}
UA_COMPACT = {_compact_key(x) for x in UA_LOCATIONS if x}


def _unpack_mapping_value(v: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Return (city_or_name, region, country)."""
    if v is None:
        return None, None, None
    if isinstance(v, str):
        return v.strip() or None, None, None
    if isinstance(v, dict):
        city = v.get("city") or v.get("name") or v.get("place")
        region = v.get("region") or v.get("admin1") or v.get("subject")
        country = v.get("country")
        return (
            str(city).strip() if city else None,
            str(region).strip() if region else None,
            str(country).strip() if country else None,
        )
    if isinstance(v, (tuple, list)):
        if len(v) >= 2 and isinstance(v[1], (tuple, list)):
            city = str(v[0]).strip() if v[0] is not None else None
            region = str(v[1][0]).strip() if len(v[1]) >= 1 and v[1][0] is not None else None
            country = str(v[1][1]).strip() if len(v[1]) >= 2 and v[1][1] is not None else None
            return city, region, country
        if len(v) >= 2:
            city = str(v[0]).strip() if v[0] is not None else None
            country = str(v[1]).strip() if v[1] is not None else None
            return city, None, country
        if len(v) == 1:
            return str(v[0]).strip() if v[0] is not None else None, None, None
    return str(v).strip() or None, None, None


NLIQ_MAP: Dict[str, Tuple[Optional[str], Optional[str], Optional[str]]] = {}
NLIQ_STRINGS: set[str] = set()
if _NLIQ is not None:
    for name in dir(_NLIQ):
        if name.startswith("_"):
            continue
        val = getattr(_NLIQ, name, None)
        if isinstance(val, dict):
            for k, v in val.items():
                if isinstance(k, str):
                    kk = _key(k)
                    if kk:
                        NLIQ_MAP[kk] = _unpack_mapping_value(v)
                if isinstance(v, str):
                    vv = _key(v)
                    if vv:
                        NLIQ_STRINGS.add(vv)
                elif isinstance(v, (tuple, list, set)):
                    for x in v:
                        if isinstance(x, str):
                            xx = _key(x)
                            if xx:
                                NLIQ_STRINGS.add(xx)
        elif isinstance(val, (set, list, tuple)):
            for x in val:
                if isinstance(x, str):
                    xx = _key(x)
                    if xx:
                        NLIQ_STRINGS.add(xx)

NLIQ_KEYS = set(NLIQ_MAP.keys()) | set(NLIQ_STRINGS)


COUNTRY_ALIASES: Dict[str, str] = {
    "россия": "Россия", "рф": "Россия", "ru": "Россия", "rus": "Россия",
    "russia": "Россия", "rossiya": "Россия", "russian federation": "Россия",
    "украина": "Украина", "україна": "Украина", "ua": "Украина", "ukr": "Украина", "ukraine": "Украина",
    "беларусь": "Беларусь", "белоруссия": "Беларусь", "рб": "Беларусь", "бр": "Беларусь", "by": "Беларусь", "belarus": "Беларусь",
    "казахстан": "Казахстан", "кз": "Казахстан", "каз": "Казахстан", "kz": "Казахстан", "kazakhstan": "Казахстан",
    "узбекистан": "Узбекистан", "уз": "Узбекистан", "узб": "Узбекистан", "uz": "Узбекистан", "uzb": "Узбекистан", "uzbekistan": "Узбекистан",
    "таджикистан": "Таджикистан", "тж": "Таджикистан", "тад": "Таджикистан", "tj": "Таджикистан", "tajikistan": "Таджикистан",
    "азербайджан": "Азербайджан", "аз": "Азербайджан", "азе": "Азербайджан", "az": "Азербайджан", "azerbaijan": "Азербайджан",
    "молдова": "Молдова", "мд": "Молдова", "md": "Молдова", "moldova": "Молдова",
    "кыргызстан": "Кыргызстан", "киргизия": "Кыргызстан", "кг": "Кыргызстан", "kg": "Кыргызстан", "kyrgyzstan": "Кыргызстан",
}

# Common inflected forms. These are important for phrases like
# "сам из Москвы, сейчас в Беларуси, 25": current location must win.
COUNTRY_ALIASES.update({
    "россии": "Россия", "рфе": "Россия",
    "украины": "Украина", "україни": "Украина",
    "беларуси": "Беларусь", "белоруссии": "Беларусь",
    "казахстана": "Казахстан",
    "узбекистана": "Узбекистан",
    "таджикистана": "Таджикистан",
    "азербайджана": "Азербайджан",
    "молдовы": "Молдова",
    "кыргызстана": "Кыргызстан", "киргизии": "Кыргызстан",
})

# High-priority aliases. Value: (city, region, country).
ALIASES: Dict[str, Tuple[Optional[str], Optional[str], str]] = {
    "мск": ("Москва", "Москва", "Россия"),
    "msk": ("Москва", "Москва", "Россия"),
    "moscow": ("Москва", "Москва", "Россия"),
    "moskva": ("Москва", "Москва", "Россия"),
    "москва": ("Москва", "Москва", "Россия"),
    "спб": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "spb": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "питер": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "piter": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "peterburg": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "saint petersburg": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "st petersburg": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "санкт петербург": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "санкт-петербург": ("Санкт-Петербург", "Санкт-Петербург", "Россия"),
    "екб": ("Екатеринбург", "Свердловская область", "Россия"),
    "ekb": ("Екатеринбург", "Свердловская область", "Россия"),
    "ekaterinburg": ("Екатеринбург", "Свердловская область", "Россия"),
    "yekaterinburg": ("Екатеринбург", "Свердловская область", "Россия"),
    "екат": ("Екатеринбург", "Свердловская область", "Россия"),
    "екатеринбург": ("Екатеринбург", "Свердловская область", "Россия"),
    "нн": ("Нижний Новгород", "Нижегородская область", "Россия"),
    "nizhny novgorod": ("Нижний Новгород", "Нижегородская область", "Россия"),
    "нижний новгород": ("Нижний Новгород", "Нижегородская область", "Россия"),
    "рнд": ("Ростов-на-Дону", "Ростовская область", "Россия"),
    "rnd": ("Ростов-на-Дону", "Ростовская область", "Россия"),
    "rostov on don": ("Ростов-на-Дону", "Ростовская область", "Россия"),
    "rostov-na-donu": ("Ростов-на-Дону", "Ростовская область", "Россия"),
    "ростов на дону": ("Ростов-на-Дону", "Ростовская область", "Россия"),
    "ростов-на-дону": ("Ростов-на-Дону", "Ростовская область", "Россия"),
    "ростов": ("Ростов-на-Дону", "Ростовская область", "Россия"),
    "уфа": ("Уфа", "Республика Башкортостан", "Россия"),
    "ufa": ("Уфа", "Республика Башкортостан", "Россия"),
    "казань": ("Казань", "Республика Татарстан", "Россия"),
    "kazan": ("Казань", "Республика Татарстан", "Россия"),
    "новосибирск": ("Новосибирск", "Новосибирская область", "Россия"),
    "novosibirsk": ("Новосибирск", "Новосибирская область", "Россия"),
    "омск": ("Омск", "Омская область", "Россия"),
    "omsk": ("Омск", "Омская область", "Россия"),
    "самара": ("Самара", "Самарская область", "Россия"),
    "samara": ("Самара", "Самарская область", "Россия"),
    "челябинск": ("Челябинск", "Челябинская область", "Россия"),
    "chelyabinsk": ("Челябинск", "Челябинская область", "Россия"),
    "краснодар": ("Краснодар", "Краснодарский край", "Россия"),
    "krasnodar": ("Краснодар", "Краснодарский край", "Россия"),
    "сочи": ("Сочи", "Краснодарский край", "Россия"),
    "sochi": ("Сочи", "Краснодарский край", "Россия"),
    "пермь": ("Пермь", "Пермский край", "Россия"),
    "perm": ("Пермь", "Пермский край", "Россия"),
    "тюмень": ("Тюмень", "Тюменская область", "Россия"),
    "tyumen": ("Тюмень", "Тюменская область", "Россия"),
    "красноярск": ("Красноярск", "Красноярский край", "Россия"),
    "krasnoyarsk": ("Красноярск", "Красноярский край", "Россия"),
    "воронеж": ("Воронеж", "Воронежская область", "Россия"),
    "voronezh": ("Воронеж", "Воронежская область", "Россия"),
    "волгоград": ("Волгоград", "Волгоградская область", "Россия"),
    "volgograd": ("Волгоград", "Волгоградская область", "Россия"),
    "саратов": ("Саратов", "Саратовская область", "Россия"),
    "saratov": ("Саратов", "Саратовская область", "Россия"),
    "ярославль": ("Ярославль", "Ярославская область", "Россия"),
    "yaroslavl": ("Ярославль", "Ярославская область", "Россия"),
    "иркутск": ("Иркутск", "Иркутская область", "Россия"),
    "irkutsk": ("Иркутск", "Иркутская область", "Россия"),
    "хабаровск": ("Хабаровск", "Хабаровский край", "Россия"),
    "khabarovsk": ("Хабаровск", "Хабаровский край", "Россия"),
    "владивосток": ("Владивосток", "Приморский край", "Россия"),
    "vladivostok": ("Владивосток", "Приморский край", "Россия"),
    "барнаул": ("Барнаул", "Алтайский край", "Россия"),
    "barnaul": ("Барнаул", "Алтайский край", "Россия"),
    "ижевск": ("Ижевск", "Удмуртская Республика", "Россия"),
    "izhevsk": ("Ижевск", "Удмуртская Республика", "Россия"),
    "томск": ("Томск", "Томская область", "Россия"),
    "tomsk": ("Томск", "Томская область", "Россия"),
    "кемерово": ("Кемерово", "Кемеровская область", "Россия"),
    "kemerovo": ("Кемерово", "Кемеровская область", "Россия"),
    "оренбург": ("Оренбург", "Оренбургская область", "Россия"),
    "orenburg": ("Оренбург", "Оренбургская область", "Россия"),
    "тольятти": ("Тольятти", "Самарская область", "Россия"),
    "tolyatti": ("Тольятти", "Самарская область", "Россия"),

    "kyiv": ("Киев", None, "Украина"), "kiev": ("Киев", None, "Украина"), "киев": ("Киев", None, "Украина"), "київ": ("Киев", None, "Украина"),
    "lviv": ("Львов", None, "Украина"), "lvov": ("Львов", None, "Украина"), "львов": ("Львов", None, "Украина"), "львів": ("Львов", None, "Украина"),
    "odesa": ("Одесса", None, "Украина"), "odessa": ("Одесса", None, "Украина"), "одесса": ("Одесса", None, "Украина"),
    "kharkiv": ("Харьков", None, "Украина"), "kharkov": ("Харьков", None, "Украина"), "харьков": ("Харьков", None, "Украина"), "харків": ("Харьков", None, "Украина"),
    "dnipro": ("Днепр", None, "Украина"), "dnepr": ("Днепр", None, "Украина"), "днепр": ("Днепр", None, "Украина"),

    "almaty": ("Алматы", None, "Казахстан"), "алматы": ("Алматы", None, "Казахстан"),
    "astana": ("Астана", None, "Казахстан"), "астана": ("Астана", None, "Казахстан"), "nursultan": ("Астана", None, "Казахстан"), "nur sultan": ("Астана", None, "Казахстан"),
    "tashkent": ("Ташкент", None, "Узбекистан"), "ташкент": ("Ташкент", None, "Узбекистан"),
    "samarkand": ("Самарканд", None, "Узбекистан"), "самарканд": ("Самарканд", None, "Узбекистан"),
    "dushanbe": ("Душанбе", None, "Таджикистан"), "душанбе": ("Душанбе", None, "Таджикистан"),
    "khujand": ("Худжанд", None, "Таджикистан"), "худжанд": ("Худжанд", None, "Таджикистан"),
    "baku": ("Баку", None, "Азербайджан"), "баку": ("Баку", None, "Азербайджан"),
    "ganja": ("Гянджа", None, "Азербайджан"), "ganca": ("Гянджа", None, "Азербайджан"), "гянджа": ("Гянджа", None, "Азербайджан"),
    "minsk": ("Минск", None, "Беларусь"), "минск": ("Минск", None, "Беларусь"),
    "gomel": ("Гомель", None, "Беларусь"), "гомель": ("Гомель", None, "Беларусь"),
    "brest": ("Брест", None, "Беларусь"), "брест": ("Брест", None, "Беларусь"),
    "chisinau": ("Кишинёв", None, "Молдова"), "kishinev": ("Кишинёв", None, "Молдова"), "кишинев": ("Кишинёв", None, "Молдова"), "кишинёв": ("Кишинёв", None, "Молдова"),
    "tiraspol": ("Тирасполь", None, "Молдова"), "тирасполь": ("Тирасполь", None, "Молдова"),
}
ALIASES_COMPACT = {_compact_key(k): v for k, v in ALIASES.items()}

NOISE_WORDS = {
    "мне", "я", "из", "живу", "проживаю", "город", "г", "в", "во", "с", "со", "лет", "год", "года", "полных",
    "здравствуйте", "привет", "добрый", "день", "вечер", "утро", "работа", "подработка", "ищу", "нужна", "нужно",
    "да", "нет", "ок", "окей", "ага", "угу", "это", "вам", "вас", "для", "по", "на", "и", "а", "но", "или",
}

AGE_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")
AGE_WORD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:лет|год|года|полных\s+лет)\b", re.IGNORECASE)


def extract_age(text: str) -> Optional[int]:
    t = _norm(text)
    m = AGE_WORD_RE.search(t)
    if m:
        age = int(m.group(1))
        return age if 14 <= age <= 90 else None
    m = re.search(r"(?:мне|возраст|полных)\s*(\d{1,2})(?!\d)", t, flags=re.IGNORECASE)
    if m:
        age = int(m.group(1))
        return age if 14 <= age <= 90 else None
    m = re.search(r"(?<!\d)(\d{1,2})(?!\d)\s*$", t)
    if m:
        age = int(m.group(1))
        return age if 14 <= age <= 90 else None
    for m in AGE_RE.finditer(t):
        age = int(m.group(1))
        if 14 <= age <= 90:
            return age
    return None


def _strip_age_and_contacts(text: str) -> str:
    t = _norm(text)
    t = re.sub(r"https?://\S+|t\.me/\S+|wa\.me/\S+", " ", t, flags=re.IGNORECASE)
    t = re.sub(r"\+?\d[\d\s\-()]{6,}\d", " ", t)
    t = AGE_WORD_RE.sub(" ", t)
    t = re.sub(r"(?<!\d)\d{1,2}(?!\d)", " ", t)
    t = re.sub(r"[,.!?:;|/\\()\[\]{}]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _tokens(text: str) -> list[str]:
    return [x for x in _key(text).split() if x and x not in NOISE_WORDS]


def _variants_for_token(tok: str) -> list[str]:
    tok = _key(tok)
    variants = {tok}
    if not tok:
        return []
    if tok.endswith("ы") or tok.endswith("и"):
        variants.add(tok[:-1] + "а")
        variants.add(tok[:-1])
        variants.add(tok[:-1] + "ь")
    if tok.endswith("е"):
        variants.add(tok[:-1])
        variants.add(tok[:-1] + "а")
    if tok.endswith("у") or tok.endswith("ю"):
        variants.add(tok[:-1])
        variants.add(tok[:-1] + "а")
    if tok.endswith("ом") or tok.endswith("ем"):
        variants.add(tok[:-2])
    if tok.endswith("ой") or tok.endswith("ей"):
        variants.add(tok[:-2] + "ая")
        variants.add(tok[:-2])
    # common genitive for Москва -> Москвы
    if tok.endswith("вы"):
        variants.add(tok[:-2] + "ва")
    return [v for v in variants if v]


def _candidate_phrases(text: str) -> list[str]:
    cleaned = _strip_age_and_contacts(text)
    toks = _tokens(cleaned)
    out: list[str] = []
    # First, explicit tails after anchors.
    # Priority matters: the project needs the city where the candidate is now,
    # not where they were born or where they are originally from.
    low = _key(cleaned)
    current_location_patterns = [
        r"\bсейчас\s+в\s+(.+)$",
        r"\bсейчас\s+нахожусь\s+в\s+(.+)$",
        r"\bнахожусь\s+в\s+(.+)$",
        r"\bна\s+данный\s+момент\s+в\s+(.+)$",
        r"\bживу\s+в\s+(.+)$",
        r"\bпроживаю\s+в\s+(.+)$",
        r"\bгород\s+(.+)$",
        r"\bв\s+городе\s+(.+)$",
    ]
    origin_patterns = [
        r"\bя\s+из\s+(.+)$",
        r"\bсам\s+из\s+(.+)$",
        r"\bсама\s+из\s+(.+)$",
        r"\bродом\s+из\s+(.+)$",
        r"\bиз\s+(.+)$",
    ]
    for pat in current_location_patterns + origin_patterns:
        m = re.search(pat, low, flags=re.IGNORECASE)
        if m:
            tail = m.group(1).strip()
            tail = re.split(r"\b(?:и|а|но|мне|возраст|лет|сам|сама|родом|сейчас|нахожусь|живу|проживаю)\b", tail, maxsplit=1)[0].strip()
            if tail and tail not in out:
                out.append(tail)
    # N-grams, longer first.
    for n in range(min(5, len(toks)), 0, -1):
        for i in range(0, len(toks) - n + 1):
            phrase = " ".join(toks[i:i+n]).strip()
            if phrase and phrase not in out:
                out.append(phrase)
    # Single-token de-inflection variants.
    for tok in toks:
        for v in _variants_for_token(tok):
            if v not in out:
                out.append(v)
    return out[:80]


def _normalize_country_name(country: Optional[str]) -> Optional[str]:
    if not country:
        return None
    c = _key(country)
    if c in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[c]
    if c in {"россии", "russian federation"}:
        return "Россия"
    if c in {"украины", "україни"}:
        return "Украина"
    # Values from source files often use genitive "России".
    if c == "россии":
        return "Россия"
    return str(country).strip().capitalize()


def detect_country_explicit(text: str) -> Optional[str]:
    raw = _key(text)
    compact = _compact_key(text)
    if not raw:
        return None
    words = set(raw.split())
    # UA priority, including multi-word locations.
    for ua in UA_LOCATIONS:
        if not ua:
            continue
        if " " in ua:
            if ua in raw:
                return "Украина"
        elif ua in words:
            return "Украина"
    if compact in UA_COMPACT:
        return "Украина"
    # Country aliases.
    for alias, country in COUNTRY_ALIASES.items():
        ak = _key(alias)
        if " " in ak:
            if ak in raw:
                return country
        elif ak in words:
            return country
    # non-liquid country/city exact phrase.
    if raw in NLIQ_MAP:
        _city, _region, country = NLIQ_MAP[raw]
        return _normalize_country_name(country or _region or _city)
    if raw in NLIQ_STRINGS:
        return _title_city(raw)
    return None


def _lookup_country_alias(candidate: str) -> Optional[Dict[str, Any]]:
    checks = [candidate]
    if len(_key(candidate).split()) == 1:
        checks.extend(_variants_for_token(candidate))
    for cand in checks:
        country = detect_country_explicit(cand)
        if country:
            return {
                "city": None,
                "region": None,
                "country": country,
                "source": "country_alias",
                "confidence": "high",
                "note": "город не определен" if country == "Россия" else "",
            }
    return None


def _lookup_alias(candidate: str) -> Optional[Dict[str, Any]]:
    checks = [candidate]
    if len(_key(candidate).split()) == 1:
        checks.extend(_variants_for_token(candidate))
    for cand in checks:
        k = _key(cand)
        ck = _compact_key(cand)
        val = ALIASES.get(k) or ALIASES_COMPACT.get(ck)
        if val:
            city, region, country = val
            return {"city": city, "region": region, "country": country, "source": "alias", "confidence": "high"}
    return None


def _lookup_ru(candidate: str) -> Optional[Dict[str, Any]]:
    for cand in [candidate] + _variants_for_token(candidate):
        k = _key(cand)
        if k in RU_LOCATIONS:
            city, region, country = _unpack_mapping_value(RU_LOCATIONS[k])
            return {
                "city": city or _title_city(k),
                "region": region or "",
                "country": "Россия",
                "source": "ru_locations",
                "confidence": "high",
            }
    return None


def _lookup_ua(candidate: str) -> Optional[Dict[str, Any]]:
    k = _key(candidate)
    ck = _compact_key(candidate)
    if k in UA_LOCATIONS or ck in UA_COMPACT:
        city = ALIASES.get(k, (None, None, ""))[0] if k in ALIASES else None
        return {"city": city or _title_city(k), "region": "", "country": "Украина", "source": "ua_locations", "confidence": "high"}
    return None


def _lookup_non_ru(candidate: str) -> Optional[Dict[str, Any]]:
    k = _key(candidate)
    if k in NLIQ_MAP:
        city, region, country = NLIQ_MAP[k]
        # Many source values are (name, country). Treat second value as country if region is absent.
        country_ru = _normalize_country_name(country or region)
        city_name = city or (None if country_ru and _key(k) in COUNTRY_ALIASES else _title_city(k))
        return {"city": city_name, "region": "", "country": country_ru or _title_city(k), "source": "non_liquid_locations", "confidence": "high"}
    if k in NLIQ_STRINGS:
        return {"city": None, "region": "", "country": _title_city(k), "source": "non_liquid_locations", "confidence": "medium"}
    return None


def geocode_city_country(text: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    res = parse_profile_reply(text)
    return res.get("city"), res.get("country"), None


def parse_geo(text: str) -> Dict[str, Any]:
    raw = _norm(text)
    if not raw:
        return {"city": None, "region": None, "country": None, "source": "", "confidence": "none", "note": ""}

    # Explicit Russia/РФ is enough to mark country, even without city.
    explicit_country = detect_country_explicit(raw)

    for cand in _candidate_phrases(raw):
        for fn in (_lookup_country_alias, _lookup_alias, _lookup_ru, _lookup_ua, _lookup_non_ru):
            got = fn(cand)
            if got and got.get("country"):
                return {**got, "note": got.get("note") or ""}

    if explicit_country:
        return {
            "city": None,
            "region": None,
            "country": explicit_country,
            "source": "explicit_country",
            "confidence": "high",
            "note": "город не определен" if explicit_country == "Россия" else "",
        }

    return {"city": None, "region": None, "country": None, "source": "", "confidence": "none", "note": ""}


def classify_lead(age: Optional[int], country: Optional[str], city_or_raw: Optional[str] = None) -> Tuple[str, Optional[str]]:
    if age is not None and int(age) < 18:
        return "nonliquid", "-18"
    country_ru = _normalize_country_name(country)
    if not country_ru and city_or_raw:
        country_ru = parse_geo(city_or_raw).get("country")
    if country_ru and country_ru != "Россия":
        return "nonliquid", country_ru
    if not country_ru:
        return "unknown", None
    if age is None:
        return "unknown", None
    if country_ru == "Россия":
        return "liquid", None
    return "unknown", None


def parse_profile_reply(text: str) -> Dict[str, Any]:
    age = extract_age(text)
    geo = parse_geo(text)
    country = geo.get("country")
    status, reason = classify_lead(age, country, text)
    return {
        "age": age,
        "city": geo.get("city"),
        "region": geo.get("region"),
        "country": country,
        "status": status,
        "nonliquid_reason": reason,
        "geo_source": geo.get("source") or "",
        "geo_confidence": geo.get("confidence") or "none",
        "geo_note": geo.get("note") or "",
    }


def is_noise_text(text: str) -> bool:
    t = _key(text)
    return not t or t in {"ок", "ok", "да", "нет", "привет", "здравствуйте", "угу", "ага"}


def is_known_city_token(word: str) -> bool:
    k = _key(word)
    return bool(k in RU_LOCATIONS or k in NLIQ_KEYS or k in UA_LOCATIONS or k in ALIASES)


def detect_remote_intent(text: str) -> bool:
    t = _key(text)
    return any(x in t for x in ["удаленно", "удаленка", "удаленная", "онлайн", "дистанционно", "remote"])


__all__ = [
    "extract_age",
    "geocode_city_country",
    "parse_geo",
    "parse_profile_reply",
    "classify_lead",
    "detect_country_explicit",
    "detect_remote_intent",
    "is_noise_text",
    "is_known_city_token",
]


# --- TPILOT ROUTER HOTFIX EN/RF ---
try:
    COUNTRY_ALIASES.update({"rf": "Россия", "russian": "Россия"})
except Exception:
    pass

try:
    _noise_words = {"i", "im", "i'm", "am", "from", "my", "me", "iam"}
    if hasattr(NOISE_WORDS, "update"):
        NOISE_WORDS.update(_noise_words)
    else:
        for _w in _noise_words:
            if _w not in NOISE_WORDS:
                NOISE_WORDS.append(_w)
except Exception:
    pass
# --- END TPILOT ROUTER HOTFIX EN/RF ---

# --- TPILOT ROUTER HOTFIX WORK REQUEST NOISE ---
# These words are common work-request chatter, not geo. Prevents phrases like
# "Работу дай" from being interpreted as a city after the questionnaire is open.
try:
    _work_request_noise = {
        "дай", "дайте", "дать", "давай", "давайте", "предложи", "предложите",
        "работу", "работы", "работе", "работой", "работку", "вакансию", "вакансии",
        "нужен", "нужна", "нужно", "хочу", "можно", "подскажи", "подскажите",
    }
    if hasattr(NOISE_WORDS, "update"):
        NOISE_WORDS.update(_work_request_noise)
    else:
        for _w in _work_request_noise:
            if _w not in NOISE_WORDS:
                NOISE_WORDS.append(_w)
except Exception:
    pass
# --- END TPILOT ROUTER HOTFIX WORK REQUEST NOISE ---
