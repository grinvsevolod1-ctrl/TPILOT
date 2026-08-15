# -*- coding: utf-8 -*-
"""User-facing copy (Russian) and pure button-spec data for the "Prepared
accounts" (Подготовленные аккаунты) feature.

Stdlib only, plus the sibling `model` module (also stdlib-only, no I/O).
No Telegram library import, no `client`/`event` objects, no SQL, no
storage access, no Telegram network calls, no proxy-provider calls. Button
specs are returned as plain data (label + a method/callback code, never a
telethon.Button instance) -- panel_bot.py turns them into real buttons in a
later phase.

Exact labels below are fixed by the approved plan and must not be
paraphrased or re-worded anywhere else in the codebase.
"""
from __future__ import annotations

from typing import Optional, Tuple

from . import model

__all__ = [
    "SECTION_LABEL",
    "PREPARE_ACTION_LABEL",
    "AUTH_QR_LABEL",
    "AUTH_PHONE_LABEL",
    "AUTH_SESSION_LABEL",
    "AUTH_TDATA_LABEL",
    "BACK_LABEL",
    "AUTH_CHOOSER_OPTIONS",
    "SUBSTATE_DRAFT_LABEL",
    "SUBSTATE_VERIFIED_LABEL",
    "SUBSTATE_STORED_LABEL",
    "ACTIVATION_TITLE",
    "ACTIVATION_VERIFYING_TEXT",
    "substate_label",
]


# --- exact UI labels (do not paraphrase) -------------------------------------
SECTION_LABEL = "💾Подготовленные аккаунты"
PREPARE_ACTION_LABEL = "📦 Подготовить на потом"

AUTH_QR_LABEL = "◻️ QR"
AUTH_PHONE_LABEL = "📱 Номер телефона"
AUTH_SESSION_LABEL = "📁 Session"
AUTH_TDATA_LABEL = "📦 TData"

BACK_LABEL = "⬅️ Назад"

# Pure data, not telethon.Button instances: (auth_source_code, label). The
# panel-side auth chooser (wired in a later phase) iterates this to build
# its own callback_data -- this module makes no decision about callback
# byte strings, that is panel_bot.py's concern.
AUTH_CHOOSER_OPTIONS: Tuple[Tuple[str, str], ...] = (
    (model.AuthSource.QR, AUTH_QR_LABEL),
    (model.AuthSource.PHONE, AUTH_PHONE_LABEL),
    (model.AuthSource.SESSION, AUTH_SESSION_LABEL),
    (model.AuthSource.TDATA, AUTH_TDATA_LABEL),
)


# --- prepared-account substate labels ----------------------------------------
# Session/TData never reach VERIFIED during prepare (prepare never touches
# the network for those two methods) -- STORED intentionally gets its own,
# weaker wording and must never be confused with the qr/phone VERIFIED text.
SUBSTATE_DRAFT_LABEL = "⏳ ожидает авторизации"
SUBSTATE_VERIFIED_LABEL = "✅ Telegram проверен"
SUBSTATE_STORED_LABEL = "⏳ проверка при подключении"

_SUBSTATE_LABELS = {
    model.PreparedSubstate.DRAFT: SUBSTATE_DRAFT_LABEL,
    model.PreparedSubstate.VERIFIED: SUBSTATE_VERIFIED_LABEL,
    model.PreparedSubstate.STORED: SUBSTATE_STORED_LABEL,
}


def substate_label(substate: Optional[str]) -> str:
    """Pure lookup: substate -> its exact display label. Returns "" for an
    unrecognized/None substate rather than guessing -- callers must not
    render a "✅ Telegram проверен"-style claim without a real substate."""
    return _SUBSTATE_LABELS.get(str(substate or "").strip().lower(), "")


# --- activation screen copy ---------------------------------------------------
# Shown while the existing Telegram client factory performs a live
# authorization re-check through the account's assigned proxy (wired in a
# later phase). Deliberately has no "enter the code" style instructions --
# activation never repeats a login, it only re-verifies an already-stored
# session.
ACTIVATION_TITLE = "🔐 Подключение Telegram"
ACTIVATION_VERIFYING_TEXT = "⏳ Проверяю Telegram-сессию через выбранный proxy..."
