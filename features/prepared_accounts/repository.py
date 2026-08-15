# -*- coding: utf-8 -*-
"""Feature-facing boundary over the existing storage API for "Prepared
accounts" (Подготовленные аккаунты).

No SQL lives here -- every read/write delegates to storage.py's
prepared_account_* primitives (added in Phase 2) or to manager_registry's
existing sync manager-row reader. This module's only real job is the
INNER JOIN visibility rule described in the approved plan: a manager_key is
a visible prepared account only when BOTH a prepared_accounts side-row AND
a managers row with status == model.PREPARED_STATUS exist for it.

IMPORTANT (import hygiene): `storage` is a DB-touching module and is
imported lazily (inside functions, never at module level) so that this
file can be imported -- directly, or via the package's get_repository() --
without pulling storage's aiosqlite/telethon-adjacent import surface into a
caller that only wanted the pure `model`/`texts` modules. `manager_registry`
is imported at module level: it is a light, mostly-stdlib module (plus
python-dotenv) with no telethon/main/panel_bot/storage import of its own,
so importing it here does not compromise that isolation contract -- see
prepared_accounts/__init__.py's docstring for the exact contract this file
must keep.

Substate classification (DRAFT/VERIFIED/STORED) is never computed here --
it is delegated to model.derive_substate, the single source of truth for
that logic.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import manager_registry

from . import model

__all__ = [
    "get_prepared",
    "list_prepared",
    "upsert_metadata",
    "mark_verify",
    "mark_activated",
    "claim_activation",
    "delete_metadata",
]


def _storage_module():
    """Lazy import boundary -- see this module's docstring."""
    import storage  # noqa: WPS433 (deferred on purpose)

    return storage


def _resolve_db_path(db_path: Optional[str]) -> str:
    if db_path:
        return db_path
    return _storage_module().QUEUE_DB_PATH


# Managers-row fields exposed to the view model. Deliberately excludes
# proxy_username/proxy_password (raw proxy credentials never leave
# storage.py through this layer, matching the project's existing
# password-hygiene rule for every other manager-facing screen) and
# anything not actually present as a managers column.
_MANAGER_VIEW_FIELDS = (
    "status",
    "is_enabled",
    "manual_stopped",
    "tg_user_id",
    "phone",
    "telegram_username",
    "first_name",
    "last_name",
    "display_name",
    "session_path",
    "proxy_type",
    "proxy_host",
    "proxy_port",
    "proxy_enabled",
    "proxy_lease_id",
    "auth_profile",
    "owner_user_id",
)

# prepared_accounts side-row fields exposed to the view model (mirrors the
# schema created by storage.ensure_prepared_accounts_table in Phase 2).
_PREPARED_VIEW_FIELDS = (
    "auth_source",
    "prepared_by_user_id",
    "prepared_at",
    "activated_at",
    "activating_at",
    "last_verify_at",
    "last_verify_ok",
    "last_verify_error",
)


def _build_view_model(manager_row: Dict[str, Any], prepared_row: Dict[str, Any]) -> Dict[str, Any]:
    view: Dict[str, Any] = {"manager_key": manager_registry.normalize_manager_key(manager_row.get("manager_key") or "")}
    for field in _PREPARED_VIEW_FIELDS:
        view[field] = prepared_row.get(field)
    for field in _MANAGER_VIEW_FIELDS:
        view[field] = manager_row.get(field)
    view["substate"] = model.derive_substate(manager_row, prepared_row)
    return view


def get_prepared(manager_key: str, *, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Returns the joined view model, or None if this manager_key is not a
    VISIBLE prepared account (either half of the join is missing)."""
    key = manager_registry.normalize_manager_key(manager_key or "")
    if not key:
        return None
    resolved_db_path = _resolve_db_path(db_path)
    storage = _storage_module()

    prepared_row = storage.prepared_account_get(key, db_path=resolved_db_path)
    if not prepared_row:
        return None

    manager_row = manager_registry.get_manager_row_from_db_sync(resolved_db_path, key)
    if not manager_row:
        return None
    if not model.is_prepared_row(manager_row):
        return None

    return _build_view_model(manager_row, prepared_row)


def list_prepared(*, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """All VISIBLE prepared accounts, sorted by manager_key ascending
    (stable/deterministic regardless of insertion order in either table).

    Visibility is the INNER JOIN described in the module docstring: a
    prepared_accounts side-row with no matching managers row (e.g. a failed
    offline Session/TData import that never committed its side-row, or a
    manager deleted after being prepared) is skipped. A managers row with
    status == 'prepared' but no side-row (should not normally happen, but
    is not treated as a safety violation -- this table is metadata, not a
    gate) is also skipped, exactly as the approved plan requires for the
    Session/TData transient-import case."""
    resolved_db_path = _resolve_db_path(db_path)
    storage = _storage_module()

    prepared_rows = storage.prepared_account_list(db_path=resolved_db_path)
    if not prepared_rows:
        return []

    out: List[Dict[str, Any]] = []
    for prepared_row in prepared_rows:
        key = manager_registry.normalize_manager_key(prepared_row.get("manager_key") or "")
        if not key:
            continue
        manager_row = manager_registry.get_manager_row_from_db_sync(resolved_db_path, key)
        if not manager_row:
            continue
        if not model.is_prepared_row(manager_row):
            continue
        out.append(_build_view_model(manager_row, prepared_row))

    out.sort(key=lambda v: v["manager_key"])
    return out


def upsert_metadata(
    manager_key: str,
    *,
    auth_source: str = "",
    prepared_by_user_id: Optional[int] = None,
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    storage = _storage_module()
    return storage.prepared_account_upsert(
        manager_key,
        auth_source=auth_source,
        prepared_by_user_id=prepared_by_user_id,
        db_path=_resolve_db_path(db_path),
    )


def mark_verify(manager_key: str, ok: bool, error: str = "", *, db_path: Optional[str] = None) -> bool:
    storage = _storage_module()
    return storage.prepared_account_mark_verify(manager_key, ok, error, db_path=_resolve_db_path(db_path))


def mark_activated(manager_key: str, *, db_path: Optional[str] = None) -> bool:
    storage = _storage_module()
    return storage.prepared_account_mark_activated(manager_key, db_path=_resolve_db_path(db_path))


def claim_activation(manager_key: str, *, ttl_seconds: int = 300, db_path: Optional[str] = None) -> bool:
    storage = _storage_module()
    return storage.prepared_account_claim_activating(
        manager_key, ttl_seconds=ttl_seconds, db_path=_resolve_db_path(db_path)
    )


def delete_metadata(manager_key: str, *, db_path: Optional[str] = None) -> bool:
    """Deletes only the prepared_accounts side-row. Never touches the
    managers row -- see storage.prepared_account_delete's docstring."""
    storage = _storage_module()
    return storage.prepared_account_delete(manager_key, db_path=_resolve_db_path(db_path))
