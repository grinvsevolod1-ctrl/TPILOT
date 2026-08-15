# -*- coding: utf-8 -*-
"""
admin_ai.config — environment/config plumbing for the admin_ai package.

Mirrors the fail-open `_cfg` / `_cfg_bool` / `_cfg_int` pattern already
established in llm_supervisor.py, but under its own ADMIN_AI_* namespace
and with an independent lifecycle: the lead-dialog supervisor and this
AdminBot assistant are separate features on purpose (2026-07-21 audit
decision: llm_supervisor.py and its UI stay unchanged; this package is
built entirely separately, copying proven patterns rather than importing
them).

Only the constants actually used by P0+P1 modules (registry/schema/
entities/context) are relied upon today; the rest are declared now so
later local milestones (provider.py etc.) have a single source of truth
for names, without this package depending on files that don't exist yet.

Pure stdlib. No panel_bot/main/telethon imports.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict

PACKAGE_DIR = Path(__file__).resolve().parent          # C:\ALM_TPilot\admin_ai
PROJECT_ROOT = PACKAGE_DIR.parent                       # C:\ALM_TPilot

CAPABILITIES_FILENAME = "capabilities.json"
CAPABILITY_INDEX_FILENAME = "capability_index.json"
INDEX_CACHE_FILENAME = "index_cache.json"  # future milestone (change-detector), not created by P0+P1

DEFAULT_CAPABILITIES_PATH = PACKAGE_DIR / CAPABILITIES_FILENAME
DEFAULT_CAPABILITY_INDEX_PATH = PACKAGE_DIR / CAPABILITY_INDEX_FILENAME

_CFG_DEFAULTS: Dict[str, str] = {
    "ADMIN_AI_ENABLED": "false",
    "ADMIN_AI_MODEL": "claude-sonnet-4-6",
    "ADMIN_AI_TIMEOUT_SEC": "20",
    "ADMIN_AI_MAX_TOKENS": "800",
    "ADMIN_AI_MAX_CAPABILITY_IDS": "5",
    "ADMIN_AI_LOG_PATH": "logs/admin_ai.log",
    "ADMIN_AI_INVALID_RATIO_FAIL_CLOSED": "0.30",
    "ADMIN_AI_MANAGER_MATCH_MIN_SCORE": "0.75",
    "ADMIN_AI_MAX_CONTEXT_ROWS_PER_SECTION": "25",
}


def cfg(key: str) -> str:
    return os.environ.get(key, _CFG_DEFAULTS.get(key, "")).strip()


def cfg_bool(key: str) -> bool:
    return cfg(key).lower() in ("1", "true", "yes", "on")


def cfg_int(key: str, default: int) -> int:
    v = cfg(key)
    try:
        return int(v) if v else default
    except Exception:
        return default


def cfg_float(key: str, default: float) -> float:
    v = cfg(key)
    try:
        return float(v) if v else default
    except Exception:
        return default
