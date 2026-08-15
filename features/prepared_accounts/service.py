# -*- coding: utf-8 -*-
"""Prepare/verify/activate/delete orchestration for "Prepared accounts".

Every heavy dependency (manager row I/O, storage.prepared_account_* calls,
the Telegram client factory, _manager_finalize_login, the transient-manager
delete primitive) is INJECTED by the caller (main.py) as a plain callable --
this module never imports main.py, storage.py, panel_bot.py, or telethon,
and never constructs a TelegramClient, a proxy provider, a runtime process,
or a ManagerBot access row itself. It also never writes SQL: every DB
touchpoint is one of the injected callables.

Result shape is a plain dict: {"ok": bool, "status": <model.ActivationResult
value>, ...}. `status` is the single field Phase 6's UI needs to branch on;
"ok" is a convenience boolean mirroring status == OK.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

import manager_registry

from . import import_offline, model

__all__ = ["prepare_offline_import", "verify", "activate", "delete_prepared"]

_DEFAULT_CLAIM_TTL_SECONDS = 300


async def prepare_offline_import(
    manager_key: str,
    *,
    archive_path: str,
    base_dir: str,
    requested_by: int = 0,
    get_manager_row: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    get_prepared_metadata: Callable[[str], Optional[Dict[str, Any]]],
    mark_auth_profile: Callable[[str], Awaitable[None]],
    upsert_prepared_metadata: Callable[..., Any],
    delete_manager: Callable[..., Awaitable[Any]],
) -> Dict[str, Any]:
    """Session/TData offline prepare. Delegates the whole archive->install
    pipeline to import_offline.prepare_offline (zero network, zero proxy);
    this function's only job is the commit boundary around it:
    manager_set_fields(auth_profile) + prepared_account_upsert (the
    injected callables) are the ONLY things that make the account visible
    in 💾Подготовленные аккаунты (repository.list_prepared's INNER JOIN
    rule) -- see the approved plan's commit-boundary contract.

    Any failure (offline pipeline failure, or a commit-write failure after
    a successful install) removes the transient manager row via the
    injected delete_manager (the SAME canonical _manager_delete_full_core
    Phase 4 already wired to clean up prepared_accounts metadata too) --
    never a new delete path."""
    key = manager_registry.normalize_manager_key(manager_key)
    row = await get_manager_row(key)
    if not row or not model.is_prepared_row(row):
        return {"ok": False, "status": model.ActivationResult.INTERNAL_ERROR, "error_class": "manager_not_prepared", "error_text": f"Менеджер не найден или не в статусе prepared: {key}"}

    existing_meta = get_prepared_metadata(key)
    if existing_meta and str(existing_meta.get("auth_source") or "").strip().lower() in model.AuthSource.REQUIRES_PROXY_BEFORE_AUTH:
        return {"ok": False, "status": model.ActivationResult.INTERNAL_ERROR, "error_class": "wrong_auth_track", "error_text": "Этот аккаунт уже привязан к QR/Phone -- офлайн-импорт недоступен."}

    result = import_offline.prepare_offline(manager_key=key, archive_path=archive_path, base_dir=base_dir)
    if not result.ok:
        try:
            await delete_manager(key, requested_by=requested_by)
        except Exception:
            pass
        return {"ok": False, "status": model.ActivationResult.INTERNAL_ERROR, "error_class": result.error_class, "error_text": result.error_text}

    auth_source = model.auth_source_for_import_method(result.import_method)
    try:
        await mark_auth_profile(key)
        upsert_prepared_metadata(key, auth_source=auth_source, prepared_by_user_id=int(requested_by or 0) or None)
    except Exception as exc:  # noqa: BLE001
        import_offline.rollback_success(result)
        try:
            await delete_manager(key, requested_by=requested_by)
        except Exception:
            pass
        return {"ok": False, "status": model.ActivationResult.INTERNAL_ERROR, "error_class": "internal_error", "error_text": type(exc).__name__}

    import_offline.finalize_success(result)
    return {"ok": True, "status": model.ActivationResult.OK, "import_method": result.import_method, "auth_source": auth_source}


async def _live_verify(
    row: Dict[str, Any],
    substate: Optional[str],
    *,
    build_client: Callable[[str, str, str], Awaitable[Any]],
    check_duplicate_tg_user: Callable[[int], Optional[str]],
    mark_verify: Callable[[str, bool, str], Any],
):
    """Shared core for verify() and activate(): proxy guard -> connect ->
    is_user_authorized -> get_me -> identity/collision check -> disconnect.
    Never activates/finalizes anything -- callers decide what "success"
    means for them. On any failure, records the safe error class via
    mark_verify (which also clears activating_at -- see storage.py's
    Phase 2 contract) before returning. Returns
    (ok: bool, status: model.ActivationResult value, me_obj_or_None)."""
    key = str(row.get("manager_key") or "")

    # TPILOT PREPARED ACCOUNTS PHASE 5: cheap, explicit pre-check BEFORE any
    # client is built -- a manager with no proxy configured must never
    # reach a connect attempt (Telegram network call count stays 0 for this
    # path). build_client itself would ALSO fail closed on a genuinely
    # unusable proxy (see main.py's _resolve_manager_telethon_proxy), but
    # that failure mode is reported as CONNECT_FAILED below, not NEED_PROXY
    # -- the two are deliberately distinct outcomes.
    if not int(row.get("proxy_enabled") or 0):
        mark_verify(key, False, "proxy_required")
        return False, model.ActivationResult.NEED_PROXY, None

    source = model.client_source_for(row)
    client = None
    try:
        client = await build_client(key, str(row.get("session_path") or ""), source)
        await client.connect()
        authorized = await client.is_user_authorized()
        if not authorized:
            mark_verify(key, False, "unauthorized")
            return False, model.ActivationResult.UNAUTHORIZED, None
        me = await client.get_me()
    except Exception:  # noqa: BLE001
        mark_verify(key, False, "connect_failed")
        return False, model.ActivationResult.CONNECT_FAILED, None
    finally:
        # Disconnect on EVERY outcome once a client object exists --
        # including a failed connect() -- never leave a prepared client
        # connected (approved plan, section 12).
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    # TPILOT PREPARED ACCOUNTS PHASE 7B HARDENING (2026-08-11): fail closed
    # if get_me() returned None or an object with no usable positive id --
    # is_user_authorized() already returned True above, so this is not the
    # ordinary UNAUTHORIZED case, but neither the VERIFIED identity check
    # nor the STORED duplicate-guard below is safe to run against a missing/
    # invalid identity (VERIFIED would compare 0 against a real tg_user_id;
    # STORED's duplicate lookup would short-circuit on tid<=0 and silently
    # report "no conflict"). Reuses the existing CONNECT_FAILED result class
    # (no new model.ActivationResult value) -- the mark_verify error text
    # records the more specific reason for diagnostics.
    if me is None or int(getattr(me, "id", 0) or 0) <= 0:
        mark_verify(key, False, "identity_unavailable")
        return False, model.ActivationResult.CONNECT_FAILED, None

    me_id = int(getattr(me, "id", 0) or 0)
    if substate == model.PreparedSubstate.VERIFIED:
        if me_id != int(row.get("tg_user_id") or 0):
            mark_verify(key, False, "identity_mismatch")
            return False, model.ActivationResult.IDENTITY_MISMATCH, None
    else:
        conflict = check_duplicate_tg_user(me_id)
        if conflict:
            mark_verify(key, False, "duplicate_tg_user_id")
            return False, model.ActivationResult.DUPLICATE, None
    return True, model.ActivationResult.OK, me


def _preconditions(row: Optional[Dict[str, Any]], prepared_meta: Optional[Dict[str, Any]]):
    """Shared guard for verify()/activate(): row exists, is prepared, and
    its substate is activatable (not DRAFT -- see model.can_activate)."""
    if not row or not model.is_prepared_row(row):
        return False
    return model.can_activate(row, prepared_meta)


async def verify(
    manager_key: str,
    *,
    get_manager_row: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    get_prepared_metadata: Callable[[str], Optional[Dict[str, Any]]],
    claim_activation: Callable[..., bool],
    build_client: Callable[[str, str, str], Awaitable[Any]],
    check_duplicate_tg_user: Callable[[int], Optional[str]],
    mark_verify: Callable[[str, bool, str], Any],
    claim_ttl_seconds: int = _DEFAULT_CLAIM_TTL_SECONDS,
) -> Dict[str, Any]:
    """🔄 Проверить аккаунт -- live-checks a prepared account's session
    WITHOUT activating it. status stays 'prepared', runtime never starts,
    no ManagerBot access is created, no source-pick marker appears."""
    key = manager_registry.normalize_manager_key(manager_key)
    row = await get_manager_row(key)
    prepared_meta = get_prepared_metadata(key)
    if not _preconditions(row, prepared_meta):
        return {"ok": False, "status": model.ActivationResult.DRAFT_NOT_READY}

    if not claim_activation(key, ttl_seconds=claim_ttl_seconds):
        return {"ok": False, "status": model.ActivationResult.BUSY}

    substate = model.derive_substate(row, prepared_meta)
    ok, status, _me = await _live_verify(
        row, substate, build_client=build_client,
        check_duplicate_tg_user=check_duplicate_tg_user, mark_verify=mark_verify,
    )
    if ok:
        mark_verify(key, True, "")
    return {"ok": ok, "status": status}


async def activate(
    manager_key: str,
    *,
    requested_by: int = 0,
    get_manager_row: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    get_prepared_metadata: Callable[[str], Optional[Dict[str, Any]]],
    claim_activation: Callable[..., bool],
    build_client: Callable[[str, str, str], Awaitable[Any]],
    check_duplicate_tg_user: Callable[[int], Optional[str]],
    mark_verify: Callable[[str, bool, str], Any],
    finalize_login: Callable[..., Awaitable[str]],
    mark_activated: Callable[[str], Any],
    claim_ttl_seconds: int = _DEFAULT_CLAIM_TTL_SECONDS,
) -> Dict[str, Any]:
    """▶️ Подключить -- the ONLY prepared/0/1 -> active/1/0 lifecycle
    transition. Never re-logs in: reuses the already-installed session
    through the already-assigned proxy for a live re-check, then hands off
    to the EXISTING onboarding finalize primitive
    (_manager_finalize_login, injected as finalize_login) with
    model.activation_target_fields' exact kwargs -- identity, ManagerBot
    grant, screenshots, runtime spawn, and the source-pick marker are ALL
    finalize's own existing behavior, not reimplemented here."""
    key = manager_registry.normalize_manager_key(manager_key)
    row = await get_manager_row(key)
    prepared_meta = get_prepared_metadata(key)
    if not _preconditions(row, prepared_meta):
        return {"ok": False, "status": model.ActivationResult.DRAFT_NOT_READY}

    if not claim_activation(key, ttl_seconds=claim_ttl_seconds):
        return {"ok": False, "status": model.ActivationResult.BUSY}

    substate = model.derive_substate(row, prepared_meta)
    ok, status, me = await _live_verify(
        row, substate, build_client=build_client,
        check_duplicate_tg_user=check_duplicate_tg_user, mark_verify=mark_verify,
    )
    if not ok:
        # Verify-before-flip (approved plan, section 15): nothing about the
        # managers row's lifecycle fields was touched above -- the failure
        # is reported as-is, prepared stays prepared, the assigned proxy
        # (if any) is left exactly as it was (section 19 -- no auto-release).
        return {"ok": False, "status": status}

    phone = str(getattr(me, "phone", "") or row.get("phone") or "")
    try:
        result_text = await finalize_login(
            int(requested_by or 0), key, phone, me,
            **model.activation_target_fields(row),
        )
    except Exception as exc:  # noqa: BLE001
        mark_verify(key, False, "internal_error")
        return {"ok": False, "status": model.ActivationResult.INTERNAL_ERROR, "error_text": type(exc).__name__}

    mark_activated(key)
    return {"ok": True, "status": model.ActivationResult.OK, "result_text": result_text}


async def delete_prepared(
    manager_key: str,
    *,
    requested_by: int = 0,
    get_manager_row: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    delete_manager: Callable[..., Awaitable[Any]],
) -> Dict[str, Any]:
    """🗑 Удалить -- refuses anything that isn't currently a prepared
    account (this command is scoped to the prepared-accounts feature, not a
    general-purpose manager-delete backdoor), then delegates to the SAME
    canonical _manager_delete_full_core every other manager delete already
    uses (injected as delete_manager) -- no new delete engine."""
    key = manager_registry.normalize_manager_key(manager_key)
    row = await get_manager_row(key)
    if not row or not model.is_prepared_row(row):
        return {"ok": False, "error_class": "manager_not_prepared", "error_text": f"Не найден подготовленный аккаунт: {key}"}
    result_text = await delete_manager(key, requested_by=requested_by)
    return {"ok": True, "result_text": result_text}
