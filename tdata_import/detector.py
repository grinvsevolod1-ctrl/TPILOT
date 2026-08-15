# -*- coding: utf-8 -*-
"""Inventory an extracted archive and choose the import source.

Priority (owner decision #4): a ready Telethon `.session` always wins; a
Telegram Desktop `tdata` directory is used only as a fallback when no `.session`
is present. More than one candidate account (multiple `.session` files, or more
than one tdata root) is never silently accepted -> MultipleAccounts.

Structural detection only -- no SQLite open here (that is session_inspector's
job) and no Telegram connection.
"""
from __future__ import annotations

import os
from typing import List, Tuple

from .errors import MultipleAccounts, UnsupportedPackage
from .models import ArchiveInventory

# Files that mark a directory as a Telegram Desktop tdata root.
_TDATA_MARKERS = {"key_datas", "key_datas1", "maps", "map0", "map1"}


def _looks_like_tdata_dir(dirpath: str, filenames: List[str]) -> bool:
    lower = {f.lower() for f in filenames}
    if os.path.basename(dirpath).lower() == "tdata":
        return True
    # A tdata root always carries an encrypted local key file.
    if "key_datas" in lower or "key_datas1" in lower:
        return True
    return False


def inventory(extracted_dir: str) -> ArchiveInventory:
    """Walk `extracted_dir` and classify its contents. Never opens file bodies.

    A real-world supplier package often ships the ready `.session`, JSON
    metadata and `Accounts.txt` directly INSIDE the same folder as `key_datas`
    (i.e. the tdata root itself is not a pure tdata-only directory) -- so a
    directory that looks like a tdata root is still scanned for its own
    sibling files. Only descent into the per-account hash subfolder (tdata's
    internal guts, e.g. `D877F783D5D3EF8C/`) is skipped -- the vendored reader
    resolves those paths itself from the root."""
    inv = ArchiveInventory()
    tdata_seen = set()
    for root, dirs, files in os.walk(extracted_dir):
        if _looks_like_tdata_dir(root, files):
            rp = os.path.realpath(root)
            if rp not in tdata_seen:
                tdata_seen.add(rp)
                inv.tdata_dirs.append(root)
            dirs[:] = []  # don't recurse into per-account subfolders

        for fn in files:
            full = os.path.join(root, fn)
            low = fn.lower()
            if low.endswith(".session"):
                inv.session_candidates.append(full)
            elif low.endswith(".json"):
                inv.json_meta_files.append(full)
            elif low == "accounts.txt":
                inv.accounts_txt.append(full)
            else:
                inv.other_files.append(full)
    return inv


def choose_source(inv: ArchiveInventory) -> Tuple[str, str]:
    """Return (kind, path): kind in {'ready_session','tdata'}.

    Raises MultipleAccounts if more than one candidate account is present, or
    UnsupportedPackage if neither a `.session` nor a tdata root exists.
    """
    sessions = list(inv.session_candidates or [])
    tdatas = list(inv.tdata_dirs or [])

    if len(sessions) > 1:
        raise MultipleAccounts("archive contains multiple .session files")
    if sessions:
        # Ready session has priority even when a tdata root is also present.
        return ("ready_session", sessions[0])
    if len(tdatas) > 1:
        raise MultipleAccounts("archive contains multiple tdata roots")
    if tdatas:
        return ("tdata", tdatas[0])
    raise UnsupportedPackage("no .session and no tdata found in archive")
