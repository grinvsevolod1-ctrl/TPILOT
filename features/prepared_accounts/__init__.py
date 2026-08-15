# -*- coding: utf-8 -*-
"""TPilot "Prepared accounts" (Подготовленные аккаунты) feature package.

Orchestration layer over TPilot's EXISTING onboarding, Telegram auth, and
proxy backends -- see the approved plan
(model-opus-5-mode-deep-pascal.md) for the full design. This package never
reimplements QR/phone/2FA/session/tdata auth, never reimplements proxy
buy/pool/manual assignment, and never reimplements storage.py's SQLite
access; it only orchestrates calls into those existing, already-working
mechanisms via thin adapters injected by main.py/panel_bot.py.

IMPORTANT (import hygiene, same contract as tdata_import/__init__.py): this
package must NOT import telethon, main.py, panel_bot.py, or storage.py at
module load time. `model` and `texts` are pure (stdlib only) and safe to
import anywhere (e.g. offline selftests). `repository`, `service`, and
`import_offline` (added in later phases) are network/DB-touching and are
imported lazily via get_repository()/get_service()/get_import_offline()/
get_module() so that importing `prepared_accounts` never triggers
Telethon/env/DB side effects.

SECURITY: never log or persist credential material (auth_key, session blob,
2FA/passcode, API hash, full phone, proxy password). Only safe metadata
leaves this package.

PHASE 1 NOTE: only `model` and `texts` exist so far. `repository`, `service`,
and `import_offline` are introduced in later phases per the approved plan;
their accessors below are wired ahead of time but will raise ImportError
until those modules are added -- calling them before that phase is a bug in
the caller, not in this package.
"""
from __future__ import annotations

from . import model, texts

__all__ = [
    "model",
    "texts",
    "get_repository",
    "get_service",
    "get_import_offline",
    "get_module",
    "__version__",
]

__version__ = "0.1.0-p1"


def get_repository():
    """Lazily import and return the storage-facing repository module (Phase 2+)."""
    from . import repository  # noqa: WPS433 (deferred on purpose)

    return repository


def get_service():
    """Lazily import and return the prepare/activate orchestrator module (Phase 5+)."""
    from . import service  # noqa: WPS433 (deferred on purpose)

    return service


def get_import_offline():
    """Lazily import and return the offline Session/TData import module (Phase 5+)."""
    from . import import_offline  # noqa: WPS433 (deferred on purpose)

    return import_offline


def get_module(name: str):
    """Lazily import one of the package's backend modules by name.

    Kept lazy so that importing `prepared_accounts` (or its pure
    `model`/`texts`) never pulls telethon, storage, or main.py transitively.
    """
    import importlib

    return importlib.import_module(f"{__name__}.{name}")
