# -*- coding: utf-8 -*-
"""Safe ZIP intake/extraction for the tdata/session import flow.

The uploaded archive is UNTRUSTED credential material. This module extracts it
into an operation-scoped working directory while defending against ZIP-slip /
absolute / drive / UNC paths, symlink/reparse entries, zip bombs, excessive
size/file-count, reserved Windows names, trailing dot/space names, ADS (`:`),
case-insensitive collisions, and nested archives. Suspicious executables and
nested archives are quarantined (extracted aside, never into the working tree,
never executed).

Nothing here connects to Telegram and nothing here logs archive contents.

There is no existing archive-extraction helper in the project to reuse (the
codebase only *creates* zips), so this is net-new. The path-safety idea mirrors
panel_bot.py:_ssv_path_is_safe.
"""
from __future__ import annotations

import os
import stat
import zipfile
from typing import Dict, List, Optional, Tuple

from .errors import InvalidArchive, UnsafeArchive
from .models import ArchiveLimits

# Windows reserved device names (any path component, with or without extension).
_RESERVED_WIN = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Extensions routed to quarantine (never into the working tree).
_EXEC_EXTS = {
    ".exe", ".dll", ".com", ".scr", ".msi", ".bat", ".cmd", ".ps1",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".cpl", ".jar", ".lnk",
}
_NESTED_ARCHIVE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".cab", ".arj",
}


def _reason(msg: str) -> str:
    """Keep reasons generic (they may reach a user/log). Never echo full paths
    of extracted secret files -- only a short structural cause."""
    return str(msg)


def _is_reserved_component(name: str) -> bool:
    base = name.split(".")[0].strip().lower()
    return base in _RESERVED_WIN


def _component_unsafe(comp: str) -> Optional[str]:
    if comp in ("", ".", ".."):
        return "path traversal / empty component"
    if ":" in comp:
        return "alternate data stream or drive component"
    if comp != comp.rstrip(" ."):
        return "trailing dot/space component"
    if _is_reserved_component(comp):
        return "reserved windows name"
    return None


def _safe_relparts(raw_name: str, max_depth: int) -> List[str]:
    """Validate a zip entry name and return its safe relative components.

    Raises UnsafeArchive on any traversal/absolute/UNC/drive/reserved/ADS/
    trailing-dot issue."""
    name = str(raw_name or "").replace("\\", "/")
    if not name or name in ("/",):
        raise UnsafeArchive(_reason("empty archive entry name"))
    if name.startswith("/"):
        raise UnsafeArchive(_reason("absolute path entry"))
    if name.startswith("//") or raw_name.startswith("\\\\"):
        raise UnsafeArchive(_reason("UNC path entry"))
    # Drive-letter (C:\ or C:/) anywhere in the first component.
    first = name.split("/", 1)[0]
    if len(first) >= 2 and first[1] == ":" and first[0].isalpha():
        raise UnsafeArchive(_reason("windows drive path entry"))
    parts = [p for p in name.split("/") if p != ""]
    if len(parts) > max_depth:
        raise UnsafeArchive(_reason("archive path too deep"))
    for comp in parts:
        bad = _component_unsafe(comp)
        if bad:
            raise UnsafeArchive(_reason(bad))
    return parts


def _entry_is_symlink(info: zipfile.ZipInfo) -> bool:
    # Unix mode is stored in the high 16 bits of external_attr.
    mode = (info.external_attr >> 16) & 0xFFFF
    return bool(mode) and stat.S_ISLNK(mode)


def _classify(parts: List[str]) -> str:
    """Return one of: 'exec', 'nested', 'normal' for routing."""
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext in _EXEC_EXTS:
        return "exec"
    if ext in _NESTED_ARCHIVE_EXTS:
        return "nested"
    return "normal"


def extract_archive(zip_path: str, work_root: str, *, limits: Optional[ArchiveLimits] = None) -> Dict:
    """Safely extract `zip_path` under `work_root`.

    Layout created:
        work_root/extracted/   -- the safe working tree
        work_root/quarantine/  -- exec/nested entries (never executed)

    Returns a summary dict:
        {extracted_dir, quarantine_dir, file_count, total_bytes,
         quarantined: [rel names], normal_files: [abs paths]}

    Raises InvalidArchive (not a zip / unreadable) or UnsafeArchive (any guard).
    The original `zip_path` is only read, never modified.
    """
    limits = limits or ArchiveLimits()

    if not os.path.isfile(zip_path):
        raise InvalidArchive(_reason("archive file not found"))
    try:
        zsize = os.path.getsize(zip_path)
    except OSError:
        raise InvalidArchive(_reason("archive not readable"))
    if zsize > limits.max_zip_bytes:
        raise UnsafeArchive(_reason("archive exceeds max upload size"))

    extracted_dir = os.path.join(work_root, "extracted")
    quarantine_dir = os.path.join(work_root, "quarantine")
    os.makedirs(extracted_dir, exist_ok=True)
    os.makedirs(quarantine_dir, exist_ok=True)
    extracted_real = os.path.realpath(extracted_dir)
    quarantine_real = os.path.realpath(quarantine_dir)

    total_bytes = 0
    file_count = 0
    seen_lower: Dict[str, str] = {}
    quarantined: List[str] = []
    normal_files: List[str] = []

    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except (zipfile.BadZipFile, OSError):
        raise InvalidArchive(_reason("not a valid zip archive"))

    with zf:
        infos = zf.infolist()
        if len(infos) > limits.max_file_count:
            raise UnsafeArchive(_reason("too many files in archive"))

        # Pre-pass: total declared uncompressed size + per-entry ratio (bomb).
        declared_total = 0
        for info in infos:
            if info.is_dir():
                continue
            fsize = int(info.file_size or 0)
            csize = int(info.compress_size or 0)
            declared_total += fsize
            if csize > 0 and (fsize / csize) > limits.max_compression_ratio:
                raise UnsafeArchive(_reason("excessive compression ratio"))
        if declared_total > limits.hard_total_extracted_bytes:
            raise UnsafeArchive(_reason("declared extracted size too large"))

        for info in infos:
            if _entry_is_symlink(info):
                raise UnsafeArchive(_reason("symlink/reparse entry not allowed"))

            raw = info.filename
            is_dir = info.is_dir()
            parts = _safe_relparts(raw, limits.max_path_depth)
            if is_dir:
                target = os.path.join(extracted_dir, *parts)
                os.makedirs(target, exist_ok=True)
                continue

            kind = _classify(parts)
            base_dir = quarantine_dir if kind in ("exec", "nested") else extracted_dir
            base_real = quarantine_real if base_dir is quarantine_dir else extracted_real

            rel_lower = "/".join(p.lower() for p in parts)
            if kind == "normal":
                if rel_lower in seen_lower:
                    raise UnsafeArchive(_reason("case-insensitive filename collision"))
                seen_lower[rel_lower] = raw

            target = os.path.join(base_dir, *parts)
            target_real = os.path.realpath(target)
            # Final containment check (defence in depth on top of _safe_relparts).
            if not (target_real == base_real or target_real.startswith(base_real + os.sep)):
                raise UnsafeArchive(_reason("entry escapes extraction directory"))

            os.makedirs(os.path.dirname(target), exist_ok=True)

            # Stream the entry with a running size cap (never trust file_size).
            written = 0
            try:
                with zf.open(info, "r") as src, open(target, "wb", 0) as dst:
                    while True:
                        chunk = src.read(1024 * 256)
                        if not chunk:
                            break
                        written += len(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > limits.hard_total_extracted_bytes:
                            raise UnsafeArchive(_reason("extracted size exceeded hard cap"))
                        dst.write(chunk)
            except (zipfile.BadZipFile, OSError) as exc:
                raise InvalidArchive(_reason(f"entry read/write failed: {type(exc).__name__}"))

            file_count += 1
            if kind in ("exec", "nested"):
                quarantined.append("/".join(parts))
            else:
                normal_files.append(target)

    return {
        "extracted_dir": extracted_dir,
        "quarantine_dir": quarantine_dir,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "quarantined": quarantined,
        "normal_files": normal_files,
    }
