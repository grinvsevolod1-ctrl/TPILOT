# -*- coding: utf-8 -*-
"""proxy_parser.py -- TPilot central proxy string parser (Stage 1).

Single source of truth for parsing/normalizing/masking manually-pasted proxy
strings. Pure module: no network calls, no DB access, no Telegram calls, no
logging/printing. Callers (panel_bot.py, main.py) are responsible for
sending/storing results and must never print/log an unmasked proxy string
(use mask_proxy() first).

Supported input formats (default scheme is socks5 when none is given):
  login:password@host:port
  socks5://login:password@host:port
  http://login:password@host:port
  https://login:password@host:port
  host:port:login:password
  host:port@login:password
  host:port
  socks5://host:port

Not a provider integration -- this module knows nothing about Proxy-Seller
or any other provider API. That is a later stage.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple, Union

ALLOWED_SCHEMES: Tuple[str, ...] = ("socks5", "http", "https")
DEFAULT_SCHEME = "socks5"

_SCHEME_RE = re.compile(r"^(socks5|http|https)://", re.IGNORECASE)

# Historical TPilot convention: a literal "-" placeholder in a login/password
# slot means "no credential" (used by the old space-separated command args).
_EMPTY_PLACEHOLDER = "-"


# ---------------------------------------------------------------------------
# Small internal helpers (no I/O, never raise)
# ---------------------------------------------------------------------------

def _to_port(raw: Any) -> Optional[int]:
    try:
        p = int(str(raw).strip())
    except Exception:
        return None
    if 1 <= p <= 65535:
        return p
    return None


def _looks_like_host_port(s: str) -> bool:
    parts = s.split(":")
    if len(parts) != 2:
        return False
    host, port = parts
    return bool(host.strip()) and _to_port(port) is not None


def _clean_cred(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip()
    if not v or v == _EMPTY_PLACEHOLDER:
        return None
    return v


def _split_scheme(raw: str, default_scheme: str) -> Tuple[str, str]:
    m = _SCHEME_RE.match(raw)
    if m:
        return m.group(1).lower(), raw[m.end():].strip()
    return str(default_scheme or DEFAULT_SCHEME).strip().lower() or DEFAULT_SCHEME, raw


# ---------------------------------------------------------------------------
# Masking (format-preserving; only ever hides the password)
# ---------------------------------------------------------------------------

def _mask_body(body: str) -> str:
    """Mask the password inside a scheme-less proxy body. Never raises;
    returns the body unchanged if no password can be confidently located
    (safer than guessing wrong and leaving something unmasked)."""
    if "@" in body:
        left, right = body.rsplit("@", 1)
        if _looks_like_host_port(right):
            if ":" in left:
                login, _pw = left.split(":", 1)
                return f"{login}:***@{right}"
            return body
        if _looks_like_host_port(left):
            if ":" in right:
                login, _pw = right.split(":", 1)
                return f"{left}@{login}:***"
            return body
        return body
    parts = body.split(":", 3)
    if len(parts) == 4:
        host, port, login, _pw = parts
        return f"{host}:{port}:{login}:***"
    return body


def mask_proxy(proxy_or_text: Union[str, Dict[str, Any], None]) -> str:
    """Mask the password in a proxy string OR a parsed proxy dict.
    Never logs/prints anything itself -- callers must use this before any
    logging of proxy data. Multi-line text is masked line by line."""
    if isinstance(proxy_or_text, dict):
        scheme = str(proxy_or_text.get("scheme") or "").strip().lower()
        host = str(proxy_or_text.get("host") or "").strip()
        port = proxy_or_text.get("port")
        login = proxy_or_text.get("login")
        prefix = f"{scheme}://" if scheme else ""
        if login:
            return f"{prefix}{login}:***@{host}:{port}"
        return f"{prefix}{host}:{port}"

    text = str(proxy_or_text or "")
    if not text.strip():
        return text
    if "\n" in text:
        return "\n".join(mask_proxy(line) for line in text.splitlines())

    raw = text.strip()
    m = _SCHEME_RE.match(raw)
    scheme_prefix = raw[:m.end()] if m else ""
    body = raw[m.end():].strip() if m else raw
    return scheme_prefix + _mask_body(body)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_proxy_line(text: str, default_scheme: str = DEFAULT_SCHEME) -> Optional[Dict[str, Any]]:
    """Parse one proxy line into a normalized dict, or None if it cannot be
    confidently parsed. Never raises. Never logs. `raw_masked` is a masked
    version of the ORIGINAL input line (password hidden)."""
    raw = str(text or "").strip()
    if not raw:
        return None

    scheme, body = _split_scheme(raw, default_scheme)
    body = body.strip()
    if not body:
        return None

    host: Optional[str] = None
    port_raw: Any = None
    login: Optional[str] = None
    password: Optional[str] = None
    input_format: Optional[str] = None

    if "@" in body:
        left, right = body.rsplit("@", 1)
        left = left.strip()
        right = right.strip()
        if _looks_like_host_port(right):
            host, port_raw = right.split(":", 1)
            if ":" in left:
                login, password = left.split(":", 1)
            else:
                login = left
            input_format = "userinfo_at_hostport"
        elif _looks_like_host_port(left):
            host, port_raw = left.split(":", 1)
            if ":" in right:
                login, password = right.split(":", 1)
            else:
                login = right
            input_format = "hostport_at_userinfo"
        else:
            return None
    else:
        parts = body.split(":", 3)
        if len(parts) == 2:
            host, port_raw = parts
            input_format = "hostport"
        elif len(parts) == 4:
            host, port_raw, login, password = parts
            input_format = "hostport_colon_userinfo"
        else:
            return None

    host = str(host or "").strip()
    port_i = _to_port(port_raw)
    if not host or not port_i:
        return None

    login = _clean_cred(login)
    password = _clean_cred(password)

    rec: Dict[str, Any] = {
        "scheme": scheme,
        "host": host,
        "port": port_i,
        "login": login,
        "password": password,
        "input_format": input_format,
    }
    rec["raw_masked"] = mask_proxy(raw)
    return rec


def parse_proxy_bulk(text: str, default_scheme: str = DEFAULT_SCHEME) -> List[Dict[str, Any]]:
    """Parse multiple proxy lines. Ignores empty lines and lines that fail
    to parse. Never raises."""
    results: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = parse_proxy_line(line, default_scheme=default_scheme)
        if parsed:
            results.append(parsed)
    return results


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_proxy_fields(
    host: Any,
    port: Any,
    scheme: str = DEFAULT_SCHEME,
    login: Any = None,
    password: Any = None,
) -> Tuple[bool, str]:
    """Basic field validation. Returns (ok, error_message). error_message
    never includes login/password values."""
    host_s = str(host or "").strip()
    if not host_s:
        return False, "host не указан"

    scheme_n = str(scheme or "").strip().lower()
    if scheme_n not in ALLOWED_SCHEMES:
        return False, f"схема прокси не поддерживается: {scheme_n or '(пусто)'} (допустимо: {', '.join(ALLOWED_SCHEMES)})"

    port_i = _to_port(port)
    if port_i is None:
        return False, "port должен быть числом от 1 до 65535"

    return True, ""


# ---------------------------------------------------------------------------
# Telethon conversion (matches the structure TPilot's runtime code already
# expects -- see main.py _build_telethon_proxy_from_row)
# ---------------------------------------------------------------------------

def build_telethon_proxy(parsed_proxy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert a parsed proxy dict into the dict shape Telethon's
    TelegramClient(proxy=...) kwarg expects. Returns None if the record is
    missing a usable host/port. TPilot currently only runs live connections
    through SOCKS5, so proxy_type is always "socks5" here regardless of the
    parsed scheme (matches existing runtime behavior)."""
    if not parsed_proxy:
        return None
    host = str(parsed_proxy.get("host") or "").strip()
    port_i = _to_port(parsed_proxy.get("port"))
    if not host or not port_i:
        return None
    login = parsed_proxy.get("login") or None
    password = parsed_proxy.get("password") or None
    return {
        "proxy_type": "socks5",
        "addr": host,
        "port": int(port_i),
        "username": login,
        "password": password,
        "rdns": True,
    }
