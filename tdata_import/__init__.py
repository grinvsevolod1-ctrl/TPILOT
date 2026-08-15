# -*- coding: utf-8 -*-
"""TPilot tdata/session import package (third AdminBot authorization method).

Isolated backend for the "upload tdata/session archive" flow. The heavy
Telegram UI/FSM stays in panel_bot.py; the controller command layer stays in
main.py. This package holds only backend processing.

IMPORTANT (import hygiene): this package must NOT import telethon or main.py at
module load time. `errors` and `models` are pure (stdlib only) and safe to
import anywhere (e.g. offline selftests). `service` and the network-touching
modules are imported lazily via get_service()/get_module() so that importing
`tdata_import` never triggers Telethon/env side effects.

SECURITY: never log or persist credential material (auth_key, session blob,
2FA/passcode, API hash, full phone, proxy password). Only safe metadata leaves
this package.
"""
from __future__ import annotations

from . import errors, models

__all__ = ["errors", "models", "get_service", "get_module", "__version__"]

__version__ = "0.1.0-p1"


def get_service():
    """Lazily import and return the orchestrator module (Phase 5+)."""
    from . import service  # noqa: WPS433 (deferred on purpose)

    return service


def get_module(name: str):
    """Lazily import one of the package's backend modules by name.

    Kept lazy so that importing `tdata_import` (or its pure `models`/`errors`)
    never pulls telethon or main.py transitively.
    """
    import importlib

    return importlib.import_module(f"{__name__}.{name}")
