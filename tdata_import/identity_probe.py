# -*- coding: utf-8 -*-
"""First-and-only network use of an imported/generated session.

This session is used ONLY with source="tdata_import" (the fixed Telegram
Desktop API/device profile -- see main.py's `_api_profile_for`/TDIMPORT_API_*,
required by opentele UseCurrentSession semantics for reusing an existing
auth_key) and ONLY through the already-verified SOCKS5 proxy. This module
does NOT construct a TelegramClient itself (main.py cannot be imported
standalone, and this package must not import telethon.TelegramClient/connect
logic on its own) -- the caller (main.py's controller command, Phase 6)
constructs the client via its OWN `_build_manager_telegram_client_from_row(row,
path, source="tdata_import")` and passes it in already connected. This keeps
the identity/health/dedup CONTRACT fully offline-testable with a fake client,
exactly like the project's own `manager_replacement_backend_selftest.py`
FakeTelegramClient convention.

Authorization proof is `get_me()` alone -- deliberately NOT preceded by an
`is_user_authorized()` check. Telethon's `is_user_authorized()` sends a bare
GetState and swallows EVERY RPCError (auth failures, flood waits, transient
server errors, alike) into a single `False`, which would misclassify
transient/unrelated RPCErrors as `session_unauthorized`. `get_me()` instead
catches only the narrow `UnauthorizedError` family (AuthKeyUnregisteredError,
SessionRevokedError, UserDeactivatedError, UserDeactivatedBanError) and
returns `None` for those, while letting every other RPCError (FloodWaitError,
ServerError, etc.) propagate untouched. That makes `me is None` a precise,
Telethon-native signal of genuine unauthorized/revoked -- anything else
propagates out of this function and is classified upstream by
service.py's generic exception handling (-> runtime_failed), not folded into
session_unauthorized.

SECURITY: never logs auth_key/phone in full. `SafeMeta.masked_phone` is the
only phone representation that ever leaves this module.
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol

from .errors import DuplicateTelegramAccount, IdentityMismatch, SessionUnauthorized
from .models import SafeMeta


class TelegramClientLike(Protocol):  # pragma: no cover - structural typing only
    async def get_me(self): ...


def _mask_phone(phone: str) -> str:
    """Same convention as manager_registry.mask_phone -- reimplemented here
    (tiny, pure) rather than importing manager_registry, to keep this module
    dependency-free besides the errors/models pair."""
    s = str(phone or "").strip()
    if not s:
        return "_"
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) < 6:
        return s
    return "+" + digits[:2] + "*" * max(0, len(digits) - 5) + digits[-3:]


def _health_problem(me) -> Optional[str]:
    """Mirrors `_tp_hg_me_health_problem` (main.py:20988)'s bot/deleted checks
    for this flow's purposes. `restricted` alone is not a hard reject
    project-wide (LIMITED, not BLOCKED, per the existing Auth Guard health
    classification) -- only bot/deleted disqualify an account from being a
    usable manager identity here."""
    if bool(getattr(me, "bot", False)):
        return "account is a bot, not a usable manager identity"
    if bool(getattr(me, "deleted", False)):
        return "account is deleted"
    return None


DuplicateCheckFn = Callable[[int], Optional[str]]
ReservedCheckFn = Callable[[int], Optional[str]]


async def probe_identity(
    client: TelegramClientLike,
    *,
    operation_id: str,
    expected_tg_user_id: Optional[int] = None,
    duplicate_check: Optional[DuplicateCheckFn] = None,
    reserved_check: Optional[ReservedCheckFn] = None,
) -> SafeMeta:
    """Verify the (already-connected-through-the-verified-proxy) client's
    session is authorized, healthy, and not a duplicate of an existing or
    in-flight manager identity. Returns SafeMeta on success.

    `duplicate_check(tg_user_id) -> Optional[manager_key]` and
    `reserved_check(tg_user_id) -> Optional[operation_id]` are injected so
    this stays offline-testable; the real callers are
    storage.tdata_import_tg_user_conflict /
    storage.tdata_import_tgid_reserved_by_other.
    """
    me = await client.get_me()
    if me is None:
        raise SessionUnauthorized("get_me() returned no user (session unusable)")

    problem = _health_problem(me)
    if problem:
        raise SessionUnauthorized(problem)

    tg_user_id = int(getattr(me, "id", 0) or 0)
    if tg_user_id <= 0:
        raise SessionUnauthorized("get_me() returned no usable user id")

    if expected_tg_user_id and int(expected_tg_user_id) != tg_user_id:
        raise IdentityMismatch(
            f"identity mismatch: expected tg_user_id={expected_tg_user_id}, got {tg_user_id}"
        )

    if duplicate_check is not None:
        conflict = duplicate_check(tg_user_id)
        if conflict:
            raise DuplicateTelegramAccount(
                f"tg_user_id {tg_user_id} is already assigned to manager {conflict!r}"
            )

    if reserved_check is not None:
        reserved_by = reserved_check(tg_user_id)
        if reserved_by:
            raise DuplicateTelegramAccount(
                f"tg_user_id {tg_user_id} is already reserved by import operation {reserved_by!r}"
            )

    return SafeMeta(
        tg_user_id=tg_user_id,
        username=str(getattr(me, "username", "") or ""),
        display_name=" ".join(
            p for p in (getattr(me, "first_name", "") or "", getattr(me, "last_name", "") or "") if p
        ).strip(),
        masked_phone=_mask_phone(getattr(me, "phone", "") or ""),
        import_method="",  # filled in by service.py, which knows session_selected vs tdata_converted
    )
