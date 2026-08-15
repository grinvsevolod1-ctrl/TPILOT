# -*- coding: utf-8 -*-
"""Offline-only Session/TData orchestrator for "Prepared accounts".

Reuses tdata_import's OFFLINE submodules directly (archive, detector,
session_inspector, tdata_adapter, installer, cleanup) -- NEVER
tdata_import.service.start_import / confirm_install, both of which have a
network/runtime path (proxy verification, a live identity_probe connect,
runtime spawn) that this module must never take. Every function reused here
is synchronous and offline -- confirmed by reading each module: archive.py
only does local zip I/O, detector.py only walks the filesystem, session_
inspector.py opens the candidate .session read-only/immutable via sqlite3
(never a Telegram connection), tdata_adapter.py's own docstring states
"ZERO Telegram network activity" and only imports telethon.crypto.AuthKey /
telethon.sessions.SQLiteSession (local session-file classes, not network
code), installer.py is plain os.replace()-based file swapping, cleanup.py
is plain os/shutil removal.

Storage-agnostic: never touches storage.py, prepared_accounts, or any
managers-row field -- the caller (features.prepared_accounts.service)
decides what to do with a successful PrepareOfflineResult (write
auth_profile, commit the prepared_accounts side-row) and calls back into
this module's finalize_success()/rollback_success() depending on whether
that commit succeeded. This split exists because cleanup must happen AFTER
the commit boundary (approved plan: install -> manager_set_fields ->
prepared_account_upsert -> cleanup_upload -> cleanup_work_root), not
before it -- a commit failure must still be able to roll back the
already-installed session file, which requires the scratch/backup files to
still exist at that point.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Optional

import manager_registry
from tdata_import import archive, cleanup, detector, errors, installer, session_inspector, tdata_adapter

__all__ = ["PrepareOfflineResult", "prepare_offline", "finalize_success", "rollback_success"]


@dataclass
class PrepareOfflineResult:
    ok: bool
    import_method: str = ""          # "ready_session" | "tdata_converted"
    final_session_path: str = ""
    dc_id: Optional[int] = None
    server_address: str = ""
    error_class: str = ""
    error_text: str = ""
    # Internal state needed by finalize_success()/rollback_success() below --
    # not meant to be read by callers outside this module.
    _install_result: object = field(default=None, repr=False)
    _work_root: str = field(default="", repr=False)
    _archive_path: str = field(default="", repr=False)


def _work_root_for(base_dir: str, manager_key: str) -> str:
    """Separate scratch namespace from tdata_import's own
    runtime\\tdata_import\\<operation_id>\\ -- see the approved plan's
    section 5. Never reused across attempts (uuid4 suffix), so a retry
    after a failed attempt can never collide with leftover scratch state."""
    key = manager_registry.normalize_manager_key(manager_key)
    return os.path.join(base_dir, "runtime", "prepared", key, uuid.uuid4().hex[:12])


def prepare_offline(*, manager_key: str, archive_path: str, base_dir: str) -> PrepareOfflineResult:
    """Archive -> extract -> detect -> validate/convert -> install.

    On ANY failure (including one that happens after install_session
    itself), this function fully rolls back and cleans up (upload + scratch
    work_root) BEFORE returning -- a failed result never leaves anything on
    disk beyond the caller's own pre-existing manager directory skeleton.

    On success, this function does NOT clean up yet (that is finalize_success/
    rollback_success's job, below) -- the caller has not committed the
    manager/side-row metadata yet, and a later commit failure must still be
    able to undo the installed session file via the scratch/backup state
    this result carries.
    """
    key = manager_registry.normalize_manager_key(manager_key)
    work_root = _work_root_for(base_dir, key)
    live_paths = manager_registry.ensure_manager_dirs(base_dir, key)
    known_session_paths = [live_paths["session_path"]]
    install_result = None
    try:
        os.makedirs(work_root, exist_ok=True)
        extraction = archive.extract_archive(archive_path, work_root)
        inv = detector.inventory(extraction["extracted_dir"])
        # Session > TData priority, multiple-account fail-closed, and
        # "no .session and no tdata -> unsupported" are ALL decided by this
        # one existing call -- never re-implemented here. A Session that
        # fails inspect_session() below is never retried against a
        # co-present TData root: choose_source() has already committed to
        # "ready_session" and returned only that path.
        kind, path = detector.choose_source(inv)

        if kind == "ready_session":
            candidate = session_inspector.inspect_session(path, origin="ready", known_session_paths=known_session_paths)
            if not candidate.valid:
                raise errors.from_code(candidate.failure_class, candidate.reason)
            session_source_path = path
            import_method = "ready_session"
        else:
            dest = os.path.join(work_root, "converted", "session.session")
            candidate = tdata_adapter.convert_tdata_to_session(path, dest, known_session_paths=known_session_paths)
            session_source_path = dest
            import_method = "tdata_converted"

        install_result = installer.install_session(session_source_path, live_paths["session_path"], backup_dir=work_root)

        return PrepareOfflineResult(
            ok=True, import_method=import_method, final_session_path=install_result.final_path,
            dc_id=candidate.dc_id, server_address=candidate.server_address,
            _install_result=install_result, _work_root=work_root, _archive_path=archive_path,
        )
    except errors.TdataImportError as exc:
        if install_result is not None:
            installer.rollback_install(install_result)
        cleanup.cleanup_upload(archive_path)
        cleanup.cleanup_work_root(work_root)
        return PrepareOfflineResult(ok=False, error_class=exc.safe_class(), error_text=str(exc)[:200])
    except Exception as exc:  # noqa: BLE001
        if install_result is not None:
            installer.rollback_install(install_result)
        cleanup.cleanup_upload(archive_path)
        cleanup.cleanup_work_root(work_root)
        return PrepareOfflineResult(ok=False, error_class="internal_error", error_text=type(exc).__name__)


def finalize_success(result: PrepareOfflineResult) -> None:
    """Call AFTER the caller has durably committed the manager/side-row
    metadata for a successful prepare_offline() result. Cleans up the
    uploaded archive and the scratch work_root -- the commit boundary has
    already passed, so there is nothing left to roll back. Never raises;
    no-op if `result.ok` is False."""
    if not result.ok:
        return
    cleanup.cleanup_upload(result._archive_path)
    cleanup.cleanup_work_root(result._work_root)


def rollback_success(result: PrepareOfflineResult) -> None:
    """Call INSTEAD of finalize_success when a successful prepare_offline()
    result could NOT be committed (e.g. the manager-metadata write itself
    failed) -- undoes the installed session file via the same installer
    that placed it, then cleans up. Never raises; no-op if `result.ok` is
    False (there is nothing to undo)."""
    if not result.ok:
        return
    if result._install_result is not None:
        installer.rollback_install(result._install_result)
    cleanup.cleanup_upload(result._archive_path)
    cleanup.cleanup_work_root(result._work_root)
