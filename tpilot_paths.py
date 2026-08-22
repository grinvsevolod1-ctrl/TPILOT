"""Shared path resolution for the ad-hoc diagnostic and repair scripts.

UBUNTU MIGRATION STAGE 2: the check_*/repair_*/refresh_* helpers each hardcoded
`C:\\ALM_TPilot`, so on Ubuntu every one of them failed with "unable to open
database file" -- or, worse, silently reported zero rows because a glob over a
non-existent directory returns an empty list rather than raising.

Paths are derived from THIS FILE's location, so a checkout works anywhere
(/opt/tpilot, a dev home directory, the old C:\\ALM_TPilot) with no edits. The
DB location still honours the .env override via the project's existing canonical
resolver -- this module does not invent a second notion of where the DB lives.
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import List, Optional

__all__ = [
    "ROOT",
    "env_file",
    "tpilot_db",
    "manager_db_paths",
    "manager_runtime_dir",
    "require_db",
]

# The repo root is this file's directory. Every diagnostic script lives at the
# top level next to it, so this holds for all of them.
ROOT = Path(__file__).resolve().parent

# Allow an explicit override for the unusual case of inspecting a COPY of a
# production tree (e.g. a downloaded snapshot) from a checkout elsewhere.
_ROOT_OVERRIDE = os.getenv("TPILOT_ROOT")
if _ROOT_OVERRIDE:
    ROOT = Path(_ROOT_OVERRIDE).expanduser().resolve()


def env_file(name: str = ".env.TPilot") -> Path:
    return ROOT / name


def tpilot_db() -> Path:
    """The central DB, honouring the .env override when one is readable.

    Falls back to the documented default layout when .env is missing, so a
    read-only inspection of a bare tree still works."""
    env = env_file()
    if env.exists():
        try:
            import manager_registry
            return Path(manager_registry.resolve_tpilot_db_path(str(env)))
        except Exception:
            # A diagnostic script must not die because .env is malformed; the
            # default layout below is almost always right anyway.
            pass
    return ROOT / "db" / "data_tpilot.db"


def manager_runtime_dir(manager_key: str) -> Path:
    return ROOT / "runtime" / "managers" / str(manager_key or "").strip().lower()


def manager_db_paths() -> List[str]:
    """Every per-manager SQLite file: runtime/managers/<key>/<key>.db."""
    return sorted(glob.glob(str(ROOT / "runtime" / "managers" / "*" / "*.db")))


def require_db(path: Optional[Path] = None) -> Path:
    """Return the central DB path, failing LOUDLY when it is absent.

    The scripts that call this are diagnostics whose output gets pasted into
    decisions. Letting sqlite3 create an empty file on connect (its default) and
    then printing "0 rows" would be actively misleading, so the absence is
    turned into an explicit error instead."""
    target = Path(path) if path is not None else tpilot_db()
    if not target.exists():
        raise SystemExit(
            f"DB not found: {target}\n"
            f"  root={ROOT}\n"
            f"  Set TPILOT_ROOT=/path/to/tree if the tree lives elsewhere."
        )
    return target
