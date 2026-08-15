# -*- coding: utf-8 -*-
"""
admin_ai.entities — deterministic, app-side entity resolution.

The model NEVER resolves an entity itself; it may only supply a raw_text
hint tagged with a slot name (see schema.py). Resolution here always runs
against data the app already trusts (an injected roster snapshot, plain
Python `date` arithmetic) — never against a live DB query the model could
influence. Ambiguous matches are ALWAYS surfaced for the admin to pick
from; nothing here silently guesses.

Pure stdlib (difflib/re/dataclasses/datetime). No panel_bot/main/telethon
imports.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Sequence

_YO_YE = str.maketrans({"ё": "е", "Ё": "Е"})


def _norm(text: Any) -> str:
    return str(text or "").strip().casefold().translate(_YO_YE)


@dataclass
class ResolutionResult:
    status: str  # "matched" | "ambiguous" | "not_found"
    matches: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def single(self) -> Optional[Dict[str, Any]]:
        if self.status == "matched" and len(self.matches) == 1:
            return self.matches[0]
        return None


def _resolve_from_roster(
    query_raw: str,
    roster: Sequence[Dict[str, Any]],
    *,
    key_field: str,
    name_field: str,
    extra_fields: Sequence[str] = (),
    min_score: float,
    max_ambiguous: int = 5,
) -> ResolutionResult:
    query = _norm(query_raw)
    if not query:
        return ResolutionResult(status="not_found")

    exact: List[Dict[str, Any]] = []
    substring: List[Dict[str, Any]] = []
    fuzzy: List[Dict[str, Any]] = []

    for row in roster:
        key = _norm(row.get(key_field, ""))
        name = _norm(row.get(name_field, ""))
        extras = [_norm(row.get(f, "")) for f in extra_fields]

        if query in ([key, name] + extras):
            exact.append(row)
            continue
        if query and (query in name or query in key or any(query in e for e in extras if e)):
            substring.append(row)
            continue
        if name:
            score = difflib.SequenceMatcher(None, query, name).ratio()
            if score >= min_score:
                fuzzy.append(row)

    for bucket in (exact, substring, fuzzy):
        if not bucket:
            continue
        # de-dup by key_field before deciding matched vs ambiguous
        seen: Dict[Any, Dict[str, Any]] = {}
        for row in bucket:
            seen.setdefault(row.get(key_field), row)
        uniq = list(seen.values())
        if len(uniq) == 1:
            return ResolutionResult(status="matched", matches=uniq)
        return ResolutionResult(status="ambiguous", matches=uniq[:max_ambiguous])

    return ResolutionResult(status="not_found")


def resolve_manager(
    raw_text: str,
    roster: Sequence[Dict[str, Any]],
    *,
    min_score: float = 0.75,
) -> ResolutionResult:
    """`roster` entries must carry at least "manager_key" and
    "display_name" (an optional "username" is also matched). Matching
    order: normalized exact hit on key/display_name/username, then
    substring, then difflib similarity on display_name >= min_score.
    Multiple equally-ranked hits at any stage -> ambiguous, never
    auto-picked.
    """
    return _resolve_from_roster(
        raw_text, roster,
        key_field="manager_key", name_field="display_name", extra_fields=("username",),
        min_score=min_score,
    )


def resolve_source(
    raw_text: str,
    roster: Sequence[Dict[str, Any]],
    *,
    min_score: float = 0.75,
) -> ResolutionResult:
    """Same resolution shape as resolve_manager, for traffic sources
    ("source_key" / "name" fields)."""
    return _resolve_from_roster(
        raw_text, roster,
        key_field="source_key", name_field="name", extra_fields=(),
        min_score=min_score,
    )


_RELATIVE_DATE_WORDS: Dict[str, int] = {
    "позавчера": -2,
    "вчера": -1,
    "сегодня": 0,
    "завтра": 1,
    "послезавтра": 2,
}

# DD.MM or DD.MM.YYYY / DD.MM.YY
_DATE_RE = re.compile(r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})(?:\.(?P<y>\d{2,4}))?")


def resolve_date(raw_text: str, *, today: date) -> Optional[date]:
    """Deterministic RU date parsing: сегодня/вчера/завтра/позавчера/
    послезавтра, and DD.MM[.YYYY]. Longer relative words are checked before
    shorter ones ("послезавтра" before "завтра") so substring matches never
    misfire. Returns None if nothing is recognized — the caller must then
    ask for clarification rather than guess.
    """
    text = _norm(raw_text)
    if not text:
        return None

    for word in sorted(_RELATIVE_DATE_WORDS, key=len, reverse=True):
        if word in text:
            return today + timedelta(days=_RELATIVE_DATE_WORDS[word])

    m = _DATE_RE.search(text)
    if m:
        day = int(m.group("d"))
        month = int(m.group("m"))
        year_raw = m.group("y")
        if year_raw:
            year = int(year_raw)
            if year < 100:
                year += 2000
        else:
            year = today.year
        try:
            return date(year, month, day)
        except ValueError:
            return None

    return None
