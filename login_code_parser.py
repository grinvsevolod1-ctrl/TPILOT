# -*- coding: utf-8 -*-
"""Strict parser for a Telegram login code delivered as a 777000 service message.

Pure, stdlib-only, NO network, NO logging. Extracts a Telegram login code
(exactly 5 digits -- the canonical app/SMS length) ONLY when the surrounding
text carries genuine login-code semantics, in Russian or English. Deliberately
NOT a "first digits in the message" regex: it must reject phone numbers, IP
addresses, dates, times, device/order numbers and URLs.

SECURITY: this module never logs the source text or the parsed code. Callers
must treat the returned code as credential-level and keep it in memory only.
"""
from __future__ import annotations

import re
from typing import Optional

# Hard cap on the text we will even look at (a real 777000 login message is
# short; anything larger is rejected outright, both as a safety bound and to
# keep the scan cheap).
MAX_TEXT_LEN = 512

# Exactly five digits that are not part of a longer digit run. Everything that
# LOOKS like 5 digits but is really part of a phone/IP/date/time/URL is rejected
# separately via _masked_spans overlap (below), so the boundary here only has to
# guard against 6+-digit numbers -- a trailing sentence period must NOT block a
# real code like "Login code: 54321.".
_CODE_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")

# Login-code context words. A candidate 5-digit run is only accepted when at
# least one of these appears in the message (case-insensitive). Russian +
# English cover Telegram's own service-message wording.
_CONTEXT_TERMS = (
    # Russian
    "код", "код для входа", "код входа", "войти", "вход", "никому не сообщайте",
    "не сообщайте", "код подтверждения", "подтверждени",
    # English
    "code", "login code", "login", "log in", "do not give this code",
    "don't give this code", "confirmation code", "verification code",
)

# Strong negative markers -- if a candidate 5-digit run sits inside one of these
# shapes we drop it even when context words are present elsewhere.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
_DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
_PHONE_RE = re.compile(r"\+\d[\d\s\-()]{6,}")


def _has_context(text_lc: str) -> bool:
    return any(term in text_lc for term in _CONTEXT_TERMS)


def _masked_spans(text: str):
    """Yield (start, end) spans occupied by URLs / IPs / times / dates / phones,
    so a 5-digit run overlapping any of them is rejected as not-a-code."""
    spans = []
    for rx in (_URL_RE, _IPV4_RE, _TIME_RE, _DATE_RE, _PHONE_RE):
        for m in rx.finditer(text):
            spans.append((m.start(), m.end()))
    return spans


def _overlaps(a_start: int, a_end: int, spans) -> bool:
    for s, e in spans:
        if a_start < e and s < a_end:
            return True
    return False


def parse_login_code(text: str, *, entities=None) -> Optional[str]:
    """Return the 5-digit login code as a STRING (leading zeros preserved) when
    the text is a genuine login-code service message, else None.

    `entities` (optional) may be a list of Telethon MessageEntity objects; a
    MessageEntityCode/Pre span is treated as a strong candidate location, but we
    never depend on entities exclusively -- the plain-text scan is authoritative.
    """
    if not text:
        return None
    s = str(text)
    if len(s) > MAX_TEXT_LEN:
        return None
    s_lc = s.lower()
    if not _has_context(s_lc):
        return None

    masked = _masked_spans(s)

    # Prefer a code that Telegram itself marked as a code/pre entity, if present
    # and it is exactly 5 digits and not inside a masked span.
    if entities:
        for ent in entities:
            cls = type(ent).__name__
            if cls not in ("MessageEntityCode", "MessageEntityPre"):
                continue
            try:
                off = int(getattr(ent, "offset", -1))
                length = int(getattr(ent, "length", -1))
            except (TypeError, ValueError):
                continue
            if off < 0 or length <= 0 or off + length > len(s):
                continue
            frag = s[off:off + length].strip()
            if re.fullmatch(r"\d{5}", frag) and not _overlaps(off, off + length, masked):
                return frag

    # Plain-text scan: collect all clean 5-digit runs not overlapping a masked
    # shape. Accept only when EXACTLY ONE such candidate exists (ambiguous
    # messages with several distinct 5-digit runs are rejected as unsafe).
    candidates = []
    for m in _CODE_RE.finditer(s):
        if _overlaps(m.start(1), m.end(1), masked):
            continue
        candidates.append(m.group(1))
    # de-duplicate identical runs (the same code repeated is still one code)
    uniq = list(dict.fromkeys(candidates))
    if len(uniq) == 1:
        return uniq[0]
    return None
