# -*- coding: utf-8 -*-
"""Atomic installation of a validated session file onto a manager's live
session path.

Mirrors the existing atomic-replace pattern already used by
`_repl4_promote_session` (main.py:34394) and `_manager_relogin_commit`
(main.py:32427, os.replace at 32515): back up any existing live session
first, `os.replace()` the new file into place (atomic on the same volume),
carry over the Telethon rollback-journal sidecar if present, and roll back
from the backup on any failure. There is no general-purpose atomic-file
helper elsewhere in the project to reuse (confirmed during the architecture
review) -- this is net-new, scoped to this one job.

The caller MUST have fully closed any Telethon client on `src_path` before
calling install_session() -- this module never opens a Telethon client
itself, only plain file I/O.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Optional

from .errors import SessionInstallFailed

# Telethon's SQLiteSession sidecar is a rollback JOURNAL (not WAL) --
# confirmed against the installed telethon==1.42.0 and the project's own
# session-backup helpers (_backup_manager_session_files: session + "-journal").
_SESSION_SIDECAR_SUFFIXES = ("-journal",)


@dataclass
class InstallResult:
    final_path: str
    backup_path: Optional[str]
    replaced_existing: bool


def _copy_with_sidecars(src: str, dst: str) -> None:
    shutil.copy2(src, dst)
    for suf in _SESSION_SIDECAR_SUFFIXES:
        if os.path.isfile(src + suf):
            shutil.copy2(src + suf, dst + suf)


def _remove_with_sidecars(path: str) -> None:
    for suf in ("",) + _SESSION_SIDECAR_SUFFIXES:
        p = path + suf
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


def install_session(src_path: str, final_path: str, *, backup_dir: Optional[str] = None) -> InstallResult:
    """Atomically place `src_path` (a closed, already-validated session file)
    at `final_path`.

    If `final_path` already exists (a stale/never-authorized draft from an
    earlier failed attempt at the same manager key -- the live-session guard
    upstream prevents this from ever being a genuinely live manager), it is
    backed up to `backup_dir` (or a `.bak_tdimport_<ts>` sibling if
    `backup_dir` is not given) before being replaced. On any failure after a
    backup was taken, the backup is restored and SessionInstallFailed is
    raised -- final_path is left exactly as it was found.
    """
    if not os.path.isfile(src_path):
        raise SessionInstallFailed("source session file not found")

    final_dir = os.path.dirname(final_path)
    if final_dir:
        os.makedirs(final_dir, exist_ok=True)

    backup_path: Optional[str] = None
    replaced_existing = os.path.isfile(final_path)

    if replaced_existing:
        if backup_dir:
            os.makedirs(backup_dir, exist_ok=True)
            backup_path = os.path.join(backup_dir, os.path.basename(final_path))
        else:
            backup_path = final_path + ".bak_tdimport_preexisting"
        try:
            _copy_with_sidecars(final_path, backup_path)
        except OSError as exc:
            raise SessionInstallFailed(f"could not back up existing session before install: {type(exc).__name__}")

    try:
        # os.replace is atomic on the same volume on both POSIX and Windows.
        os.replace(src_path, final_path)
        # Carry over / clear the sidecar to match the freshly-installed file
        # (the source's sidecar, if any; otherwise ensure no stale sidecar
        # from a previous occupant of final_path lingers).
        src_journal = src_path + "-journal"
        dst_journal = final_path + "-journal"
        if os.path.isfile(src_journal):
            os.replace(src_journal, dst_journal)
        elif os.path.isfile(dst_journal):
            try:
                os.remove(dst_journal)
            except OSError:
                pass
    except OSError as exc:
        if backup_path and os.path.isfile(backup_path):
            try:
                _copy_with_sidecars(backup_path, final_path)
            except OSError:
                pass
        raise SessionInstallFailed(f"atomic install failed: {type(exc).__name__}")

    return InstallResult(final_path=final_path, backup_path=backup_path, replaced_existing=replaced_existing)


def rollback_install(result: InstallResult) -> bool:
    """Undo a successful install_session() call (used when a LATER stage --
    e.g. runtime failing to come up -- requires reverting). Restores the
    backup if one was taken; otherwise removes the newly-installed file
    (there was nothing there before). Returns True on success (best-effort,
    logged by the caller on failure -- never raises)."""
    try:
        if result.backup_path and os.path.isfile(result.backup_path):
            _copy_with_sidecars(result.backup_path, result.final_path)
        elif not result.replaced_existing:
            _remove_with_sidecars(result.final_path)
        return True
    except OSError:
        return False
