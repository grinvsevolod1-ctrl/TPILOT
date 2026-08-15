# -*- coding: utf-8 -*-
"""Pure data model for the "Prepared accounts" (Подготовленные аккаунты) feature.

Stdlib only. No I/O, no SQL, no Telegram, no proxy backend calls -- every
name defined here is a constant or a pure function of its arguments. Mirrors
the "string-in-DB" convention the rest of the project already uses for
statuses (see tdata_import/models.py).

A prepared account is an ordinary `managers` row with
status == PREPARED_STATUS, is_enabled == PREPARED_IS_ENABLED,
manual_stopped == PREPARED_MANUAL_STOPPED -- see the approved plan for why
this is the only representation that does not duplicate proxy/session-path
plumbing. This module never reads or writes that row; it only classifies a
row dict (and an optional side-table metadata dict) that some OTHER layer
already loaded.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = [
    "PREPARED_STATUS",
    "PREPARED_IS_ENABLED",
    "PREPARED_MANUAL_STOPPED",
    "AuthSource",
    "PreparedSubstate",
    "PREPARED_READY_MARKER",
    "is_prepared_row",
    "derive_substate",
    "can_activate",
    "requires_proxy_before_auth",
    "client_source_for",
    "activation_target_fields",
    "ActivationResult",
    "auth_source_for_import_method",
]


# --- managers-row invariants for a prepared account -------------------------
PREPARED_STATUS = "prepared"
PREPARED_IS_ENABLED = 0
PREPARED_MANUAL_STOPPED = 1


class AuthSource:
    """How this prepared account's Telegram credential material was obtained.

    Stored verbatim in the prepared_accounts side table (added in a later
    phase). Never used to pick the Telegram API app/device profile for a
    live client -- that authority is `managers.auth_profile`
    ("project" | "tdesktop"), see client_source_for() below.
    """

    QR = "qr"
    PHONE = "phone"
    SESSION = "session"
    TDATA = "tdata"

    ALL = frozenset({QR, PHONE, SESSION, TDATA})

    # QR/phone reach Telegram DURING prepare, so a proxy must already be
    # bound to the managers row before that network use happens. Session/
    # TData prepare never touches the network at all (see the package
    # docstring's forbidden-operations list in import_offline.py, added in a
    # later phase) -- their proxy is chosen only at activation, right before
    # the FIRST network use of that stored session.
    REQUIRES_PROXY_BEFORE_AUTH = frozenset({QR, PHONE})


class PreparedSubstate:
    """Derived (never stored) classification of a prepared row's progress.

    DRAFT: auth_source is qr/phone, Telegram authorization not yet
      completed for this manager_key (no tg_user_id on the row yet). The
      account is visible in the prepared-accounts list but cannot be
      activated -- there is no verified Telegram identity to reconnect.
    VERIFIED: auth_source is qr/phone AND Telegram authorization already
      succeeded during prepare (tg_user_id is set). A proxy is already
      bound. Activation only needs a live re-check through that proxy.
    STORED: auth_source is session/tdata. A .session file was installed
      offline; Telegram was never contacted, so there is no verified
      identity and no bound proxy yet. Activation must select a proxy
      first, then perform the first-ever network use of this session.
    """

    DRAFT = "draft"
    VERIFIED = "verified"
    STORED = "stored"

    ALL = frozenset({DRAFT, VERIFIED, STORED})

    # Only these substates represent a Telegram-authorization outcome that
    # can be reconnected to. DRAFT means prepare itself never finished.
    ACTIVATABLE = frozenset({VERIFIED, STORED})


# ONBOARDING SOURCE PICK precedent (main.py's _ONBOARDING_SOURCE_PICK_MARKER)
# applies here too: the panel and controller processes only ever communicate
# through returned command text or a notification body, never a shared
# Python object, so this literal must stay byte-identical wherever it is
# copied into main.py/panel_bot.py in a later phase.
#
# TPILOT PREPARED ACCOUNTS PHASE 4 (2026-08-10): deliberately NOT
# human-readable UI text (unlike _ONBOARDING_SOURCE_PICK_MARKER, which is a
# real Russian sentence and doubles as harmless leftover text if a caller
# ever fails to intercept it). This marker is a pure internal signal a
# prepared account's finalize embeds in its result text -- Phase 6's panel
# notification loop matches on it exactly, it is never meant to be read by
# an operator, so a technical sentinel avoids it ever being mistaken for
# real UI copy if something forgets to strip it.
PREPARED_READY_MARKER = "__TPILOT_PREPARED_READY__"


def is_prepared_row(row: Optional[Dict[str, Any]]) -> bool:
    """True iff `row` (a managers-table row dict) is a prepared account."""
    if not row:
        return False
    return str(row.get("status") or "").strip().lower() == PREPARED_STATUS


def derive_substate(
    row: Optional[Dict[str, Any]],
    prepared_meta: Optional[Dict[str, Any]],
) -> Optional[str]:
    """Pure classification into one of PreparedSubstate's values, or None if
    `row` is not a prepared account or `prepared_meta` has no recognized
    auth_source yet (e.g. the account was just created and the operator has
    not picked an authorization method in the wizard yet).

    `prepared_meta` is the prepared_accounts side-table row (added in a
    later phase) -- this function does not care where it came from, only
    reads `auth_source` from it.
    """
    if not is_prepared_row(row):
        return None
    auth_source = str((prepared_meta or {}).get("auth_source") or "").strip().lower()
    if auth_source not in AuthSource.ALL:
        return None
    if auth_source in AuthSource.REQUIRES_PROXY_BEFORE_AUTH:
        has_identity = bool(row.get("tg_user_id"))
        return PreparedSubstate.VERIFIED if has_identity else PreparedSubstate.DRAFT
    # session/tdata: offline install never contacts Telegram, so this
    # branch never returns VERIFIED -- there is no network-verified
    # identity to report until activation happens.
    return PreparedSubstate.STORED


def can_activate(
    row: Optional[Dict[str, Any]],
    prepared_meta: Optional[Dict[str, Any]],
) -> bool:
    """Pure substate guard only -- does NOT check proxy readiness or an
    in-progress activation claim (both require reading other rows/tables,
    which belongs to the service layer added in a later phase). A True
    result here is necessary but not sufficient for activation."""
    substate = derive_substate(row, prepared_meta)
    return substate in PreparedSubstate.ACTIVATABLE


def requires_proxy_before_auth(auth_source: str) -> bool:
    """True (qr/phone) iff a proxy must already be bound to the managers row
    BEFORE this account's Telegram authorization happens during prepare.

    False (session/tdata) means prepare itself never selects or reserves a
    proxy at all -- it does NOT mean activation may skip proxy selection.
    Every prepared account, regardless of auth_source, must have a bound
    proxy before its first network use at activation time; that
    activation-time requirement is enforced by the service layer (added in
    a later phase), not by this prepare-time predicate.
    """
    return str(auth_source or "").strip().lower() in AuthSource.REQUIRES_PROXY_BEFORE_AUTH


def client_source_for(row: Optional[Dict[str, Any]]) -> str:
    """The `source=` string the existing Telegram-client factory expects, so
    a live re-check at activation reuses the API app/device profile this
    session was actually authorized under. Mirrors the project's own
    "project" vs "tdesktop" auth_profile split -- never derived from
    auth_source (see AuthSource's docstring)."""
    auth_profile = str((row or {}).get("auth_profile") or "project").strip().lower()
    return "tdata_import" if auth_profile == "tdesktop" else "onboarding"


def activation_target_fields(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure: the exact extra keyword arguments a caller must pass to the
    existing onboarding finalize primitive (main.py's
    _manager_finalize_login, wired in a later phase) to activate this
    prepared row correctly. Returns a plain dict -- performs no I/O and
    writes nothing.

    preserve_owner_user_id: activation must not steal ownership from
      whoever originally created the prepared account.
    preserve_runtime_state: False -- activation is exactly the moment this
      row should flip to status='active', is_enabled=1, manual_stopped=0.
    spawn_after_login: True -- activation is the one prepared-account
      operation that IS allowed to start the runtime.
    activate_prepared: True -- suppresses the finalize primitive's own
      prepared-row auto-detection so it performs the activation write
      instead of re-preserving the prepared state.
    preserve_auth_profile: True -- required whenever `row` was produced via
      the session/tdata import path (auth_profile == "tdesktop"); without
      it activation would silently overwrite that profile back to
      "project" and break the next runtime start under the wrong Telegram
      API app.
    """
    return {
        "preserve_owner_user_id": True,
        "preserve_runtime_state": False,
        "spawn_after_login": True,
        "activate_prepared": True,
        "preserve_auth_profile": True,
    }


class ActivationResult:
    """Canonical result-class vocabulary for prepared_accounts.service's
    verify()/activate() outcomes (Phase 5). Phase 6's UI maps these to
    user-facing text; this module is the single source of truth for the
    exact strings, so the panel-side (a separate process) and the
    controller-side service layer can never disagree on what a result means."""

    OK = "ok"
    NEED_PROXY = "need_proxy"
    DRAFT_NOT_READY = "draft_not_ready"
    BUSY = "busy"
    UNAUTHORIZED = "unauthorized"
    IDENTITY_MISMATCH = "identity_mismatch"
    DUPLICATE = "duplicate_tg_user_id"
    CONNECT_FAILED = "connect_failed"
    INTERNAL_ERROR = "internal_error"

    ALL = frozenset({
        OK, NEED_PROXY, DRAFT_NOT_READY, BUSY, UNAUTHORIZED,
        IDENTITY_MISMATCH, DUPLICATE, CONNECT_FAILED, INTERNAL_ERROR,
    })


def auth_source_for_import_method(import_method: str) -> str:
    """Pure mapping: tdata_import's own vocabulary for a successful offline
    install ("ready_session" / "tdata_converted", see detector.choose_source
    and tdata_adapter.convert_tdata_to_session) -> this feature's own
    AuthSource vocabulary ("session" / "tdata") used by the
    prepared_accounts side-table. One-directional only -- import_offline.py
    never reads AuthSource, and nothing outside this function performs this
    translation, so it can never drift into two different mappings."""
    m = str(import_method or "").strip().lower()
    if m == "ready_session":
        return AuthSource.SESSION
    if m == "tdata_converted":
        return AuthSource.TDATA
    return ""
