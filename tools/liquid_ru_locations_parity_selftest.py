#!/usr/bin/env python3
"""Offline parity selftest for the liquid_ru_locations data-move refactor
(2026-08-20).

Guarantees that replacing the 160k-line dict-literal module with a thin
JSON loader produced a BYTE-IDENTICAL RU_LOCATIONS mapping:

  * same set of keys, in the same insertion order;
  * same values;
  * same value TYPES (tuple, with a nested tuple), so any consumer that
    introspects types (e.g. isinstance checks in router._unpack_mapping_value)
    behaves exactly as before.

The "ground truth" is the pre-refactor module preserved as a
`liquid_ru_locations.py.bak_datamove_*` backup, parsed via ast.literal_eval
(no import side effects). The "candidate" is the live module import.

No network, no DB, no spend. Exit 0 = PASS.
"""
from __future__ import annotations

import ast
import fnmatch
import glob
import importlib
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ground_truth_from_git(basename_glob: str) -> tuple[str, str] | None:
    """Recover the pre-refactor backup from git history.

    STAGE 3: commit 5e13c32 ("stop tracking timestamped .bak_* safety backups")
    untracked these files on purpose -- a 20 MB generated artifact does not belong in
    the working tree. But the blob is still in history, so the ground truth is not
    lost, only unlinked from the filesystem. Reading it straight out of git keeps this
    parity test (160k lines of location data) alive without re-adding the artifact.

    Returns (source_text, provenance_label) or None when git cannot supply it.
    """
    try:
        listing = subprocess.run(
            ["git", "log", "--all", "--pretty=format:", "--name-only", "--diff-filter=A"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = sorted({
        ln.strip() for ln in listing.splitlines()
        if fnmatch.fnmatch(ln.strip(), basename_glob)
    })
    if not names:
        return None
    newest = names[-1]  # timestamped names sort chronologically
    try:
        rev = subprocess.run(
            ["git", "log", "--all", "--format=%H", "--diff-filter=A", "--", newest],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=True,
        ).stdout.split()
        if not rev:
            return None
        blob = subprocess.run(
            ["git", "cat-file", "-p", f"{rev[0]}:{newest}"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return (blob, f"git:{rev[0][:8]}:{newest}") if blob else None


def _load_original_from_backup() -> dict:
    backups = sorted(
        glob.glob(os.path.join(REPO_ROOT, "liquid_ru_locations.py.bak_datamove_*"))
    )
    if backups:
        newest = backups[-1]
        src = open(newest, encoding="utf-8").read()
    else:
        # No on-disk backup: fall back to git history before giving up.
        recovered = _ground_truth_from_git("liquid_ru_locations.py.bak_datamove_*")
        if not recovered:
            raise SystemExit(
                "FAIL: no liquid_ru_locations.py.bak_datamove_* backup on disk and it "
                "could not be recovered from git history; cannot establish parity "
                "ground truth"
            )
        src, newest = recovered
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "RU_LOCATIONS":
            return ast.literal_eval(node.value), newest
    raise SystemExit(f"FAIL: RU_LOCATIONS not found in backup {newest}")


def _load_candidate() -> dict:
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    mod = importlib.import_module("liquid_ru_locations")
    importlib.reload(mod)
    return mod.RU_LOCATIONS


def _deep_type_signature(v):
    """Structural type signature so ('a',('b','c')) != ['a',['b','c']]."""
    if isinstance(v, tuple):
        return ("tuple", tuple(_deep_type_signature(x) for x in v))
    if isinstance(v, list):
        return ("list", tuple(_deep_type_signature(x) for x in v))
    return type(v).__name__


def main() -> int:
    original, backup_path = _load_original_from_backup()
    candidate = _load_candidate()

    print(f"[parity] backup ground truth: {os.path.basename(backup_path)}")
    print(f"[parity] original keys: {len(original)} | candidate keys: {len(candidate)}")

    failures = []

    # 1. same length
    if len(original) != len(candidate):
        failures.append(f"length mismatch: {len(original)} != {len(candidate)}")

    # 2. same keys, same order
    ok, ck = list(original.keys()), list(candidate.keys())
    if ok != ck:
        # find first divergence
        for i, (a, b) in enumerate(zip(ok, ck)):
            if a != b:
                failures.append(f"key order diverges at index {i}: {a!r} != {b!r}")
                break
        else:
            failures.append("key sets differ in length/tail order")
        missing = set(original) - set(candidate)
        extra = set(candidate) - set(original)
        if missing:
            failures.append(f"missing keys (sample): {list(missing)[:5]}")
        if extra:
            failures.append(f"extra keys (sample): {list(extra)[:5]}")

    # 3. same values AND same deep types
    val_mismatch = 0
    type_mismatch = 0
    first_val_ex = None
    first_type_ex = None
    for k in original:
        if k not in candidate:
            continue
        ov, cv = original[k], candidate[k]
        if ov != cv:
            val_mismatch += 1
            if first_val_ex is None:
                first_val_ex = (k, ov, cv)
        elif _deep_type_signature(ov) != _deep_type_signature(cv):
            type_mismatch += 1
            if first_type_ex is None:
                first_type_ex = (k, _deep_type_signature(ov), _deep_type_signature(cv))
    if val_mismatch:
        failures.append(f"value mismatches: {val_mismatch}; first: {first_val_ex}")
    if type_mismatch:
        failures.append(f"deep-type mismatches: {type_mismatch}; first: {first_type_ex}")

    if failures:
        print("FAIL: liquid_ru_locations parity broken:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"[parity] OK: {len(candidate)} entries identical in keys, order, values and types")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
