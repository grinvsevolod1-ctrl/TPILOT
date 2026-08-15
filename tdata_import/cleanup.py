# -*- coding: utf-8 -*-
"""Idempotent cleanup of an import operation's working directory and any
leftover scratch session file.

Never raises -- logging/marking failure is the caller's job (storage's
cleanup_done column tracks whether cleanup ever fully succeeded; a failed
cleanup is harmless clutter, not a correctness issue, matching the project's
existing `proxy_renew_notify_purge_old`-is-optional precedent).
"""
from __future__ import annotations

import os
import shutil
from typing import Iterable, Optional


def cleanup_work_root(work_root: Optional[str]) -> bool:
    """Remove the operation's `runtime\\tdata_import\\<operation_id>\\` tree
    (upload/extracted/working/quarantine). Returns True if the directory is
    gone (or never existed) afterwards."""
    if not work_root:
        return True
    if not os.path.isdir(work_root):
        return True
    try:
        shutil.rmtree(work_root, ignore_errors=False)
    except OSError:
        return not os.path.isdir(work_root)
    return True


def cleanup_scratch_session(scratch_session_path: Optional[str]) -> bool:
    """Remove a leftover `<key>.session.tmp_<op>` scratch file + sidecars
    (used when an operation fails/cancels before install, or after a
    successful install has moved the real file away already)."""
    if not scratch_session_path:
        return True
    ok = True
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = scratch_session_path + suffix
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                ok = False
    return ok


def cleanup_upload(archive_path: Optional[str]) -> bool:
    """Remove the admin-uploaded ZIP (credential material -- owner rule:
    "archive = password") as soon as it is no longer needed.

    The uploaded ZIP lives at `runtime\\tdata_import\\_uploads\\<token>\\archive.zip`
    (panel_bot.py writes it there before the operation_id exists). When the
    file sits in such a per-upload `<token>` directory under `_uploads`, the
    WHOLE token directory is removed (it only ever holds this one upload);
    otherwise just the file is removed. Never raises; best-effort."""
    if not archive_path:
        return True
    token_dir = os.path.dirname(archive_path)
    parent = os.path.basename(os.path.dirname(token_dir)) if token_dir else ""
    # Only ever rmtree a per-upload token dir that is STRUCTURALLY under
    # <...>/tdata_import/_uploads/<token> (defence-in-depth: the parent is
    # literally "_uploads" AND the path contains the tdata_import/_uploads
    # segment), so a stray/crafted path can never turn this into an
    # arbitrary-directory delete.
    norm = (token_dir.replace("\\", "/").lower() + "/") if token_dir else ""
    under_uploads = "/tdata_import/_uploads/" in norm
    if parent == "_uploads" and under_uploads and os.path.isdir(token_dir):
        try:
            shutil.rmtree(token_dir, ignore_errors=False)
        except OSError:
            return not os.path.isdir(token_dir)
        return True
    if os.path.isfile(archive_path):
        try:
            os.remove(archive_path)
        except OSError:
            return False
    return True


def cleanup_paths(paths: Iterable[str]) -> bool:
    """Best-effort removal of an arbitrary list of files/dirs (e.g. quarantine
    contents flagged during archive extraction). Never raises."""
    ok = True
    for p in paths or ():
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                ok = ok and not os.path.isdir(p)
            elif os.path.isfile(p):
                os.remove(p)
        except OSError:
            ok = False
    return ok
