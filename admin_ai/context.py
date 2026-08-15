# -*- coding: utf-8 -*-
"""
admin_ai.context — sanitized live-state snapshot for the AI assistant.

The model never queries SQLite (or anything else) directly. panel_bot (in
a later local milestone) injects plain callables ("readers") that return
already-fetched rows/dicts from storage.py's existing read helpers; this
module copies ONLY whitelisted fields out of them into the payload that
would be sent to the model, and hard-fails closed if anything that looks
like a secret survives that whitelist — a backstop against a future
whitelist mistake, not the primary defense.

Pure stdlib. No panel_bot/main/telethon/DB imports — callers pass data in
as plain dicts/callables.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

# Keys that must NEVER appear anywhere in a context payload sent to the
# model, regardless of which whitelist produced the surrounding dict.
# Checked recursively over the ENTIRE built context as a hard backstop.
FORBIDDEN_KEYS = frozenset({
    "phone", "phone_number", "password", "proxy_password", "pin",
    "twofa", "2fa", "two_fa", "api_key", "apikey", "anthropic_api_key",
    "session_path", "session", "session_file", "login_code", "code",
    "auth_code", "secret", "token", "access_token",
    "raw_log", "log_text", "logs", "message_text", "lead_text",
    "dialog_text", "conversation_text", "db_dump", "db_path",
})

MANAGER_ROSTER_FIELDS: Sequence[str] = ("manager_key", "display_name", "username", "status", "is_enabled")
SOURCE_ROSTER_FIELDS: Sequence[str] = ("source_key", "name", "is_active")
PROXY_LEASE_FIELDS: Sequence[str] = ("lease_id", "manager_key", "host", "port", "status", "expires_at", "auto_renew_enabled")
SCHEDULE_REQUEST_FIELDS: Sequence[str] = ("manager_key", "requested_date", "status", "created_at")
BIZLINK_READINESS_FIELDS: Sequence[str] = ("manager_key", "date", "expected_count", "actual_count", "status")
HEALTH_INCIDENT_FIELDS: Sequence[str] = ("kind", "target", "status", "opened_at")

DEFAULT_MAX_ROWS_PER_SECTION = 25


def _whitelist_row(row: Dict[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    return {f: row.get(f) for f in fields if f in row}


def assert_no_forbidden_keys(payload: Any, path: str = "context") -> None:
    """Recursively raise ValueError if any forbidden key is present
    anywhere in payload. Intended to be called on the fully-built context
    before it is ever handed to a provider/prompt module."""
    if isinstance(payload, dict):
        for k, v in payload.items():
            if str(k).strip().lower() in FORBIDDEN_KEYS:
                raise ValueError(f"forbidden key leaked into AI context at {path}.{k}")
            assert_no_forbidden_keys(v, f"{path}.{k}")
    elif isinstance(payload, (list, tuple)):
        for i, v in enumerate(payload):
            assert_no_forbidden_keys(v, f"{path}[{i}]")


def _safe_call(
    fn: Optional[Callable[[], Sequence[Dict[str, Any]]]],
    max_rows: int,
) -> List[Dict[str, Any]]:
    if fn is None:
        return []
    try:
        rows = fn() or []
    except Exception:
        # A misbehaving reader degrades its OWN section to empty rather
        # than failing the whole context build.
        return []
    return list(rows)[:max_rows]


def build_context(
    *,
    manager_rows: Optional[Callable[[], Sequence[Dict[str, Any]]]] = None,
    source_rows: Optional[Callable[[], Sequence[Dict[str, Any]]]] = None,
    expiring_proxy_rows: Optional[Callable[[], Sequence[Dict[str, Any]]]] = None,
    pending_schedule_requests: Optional[Callable[[], Sequence[Dict[str, Any]]]] = None,
    bizlink_readiness_rows: Optional[Callable[[], Sequence[Dict[str, Any]]]] = None,
    open_health_incidents: Optional[Callable[[], Sequence[Dict[str, Any]]]] = None,
    active_wizard_name: Optional[str] = None,
    max_rows_per_section: int = DEFAULT_MAX_ROWS_PER_SECTION,
) -> Dict[str, Any]:
    """Build a sanitized snapshot from injected reader callables. Every
    reader is optional; a raising/missing reader degrades its section to
    an empty list instead of failing context build for the whole request.
    Raises ValueError (never silently drops) if a forbidden key is somehow
    still present after whitelisting — that indicates a bug in a caller's
    row shape and must not reach the model.
    """
    context: Dict[str, Any] = {
        "managers": [_whitelist_row(r, MANAGER_ROSTER_FIELDS) for r in _safe_call(manager_rows, max_rows_per_section)],
        "sources": [_whitelist_row(r, SOURCE_ROSTER_FIELDS) for r in _safe_call(source_rows, max_rows_per_section)],
        "expiring_proxies": [_whitelist_row(r, PROXY_LEASE_FIELDS) for r in _safe_call(expiring_proxy_rows, max_rows_per_section)],
        "pending_schedule_requests": [_whitelist_row(r, SCHEDULE_REQUEST_FIELDS) for r in _safe_call(pending_schedule_requests, max_rows_per_section)],
        "bizlink_readiness": [_whitelist_row(r, BIZLINK_READINESS_FIELDS) for r in _safe_call(bizlink_readiness_rows, max_rows_per_section)],
        "open_health_incidents": [_whitelist_row(r, HEALTH_INCIDENT_FIELDS) for r in _safe_call(open_health_incidents, max_rows_per_section)],
        "active_wizard": str(active_wizard_name) if active_wizard_name else None,
    }
    assert_no_forbidden_keys(context)
    return context
