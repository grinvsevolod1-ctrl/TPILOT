#!/usr/bin/env python3
"""Offline parity selftest for the non_liquid_locations data-move refactor
(2026-08-20).

non_liquid_locations.py mixes DATA (large dict literals) with LOGIC (key
normalization for-loops, _ps_norm_keep_spaces, _ps_items, ABBR assembly).
The refactor extracted only the five biggest pure-data dicts to
data/non_liquid_locations.json and left every line of logic untouched, so the
for-loops now run over the JSON-loaded dicts and must produce the identical
FINAL runtime state.

This test loads the pre-refactor module (preserved as a
`non_liquid_locations.py.bak_datamove_*` backup) and the live module from
their file paths, executes both fully, then compares EVERY module-level dict
and set attribute deeply -- keys, order, values and value TYPES (tuple vs
list). That covers both the extracted dicts and everything the loops derive
from them.

No network, no DB, no spend. Exit 0 = PASS.
"""
from __future__ import annotations

import glob
import importlib.util
import os
from importlib.machinery import SourceFileLoader

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module_from_path(path: str, mod_name: str):
    # SourceFileLoader handles non-".py" paths (e.g. the .bak_datamove_* backup),
    # which spec_from_file_location refuses to recognize.
    loader = SourceFileLoader(mod_name, path)
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)  # __name__ != "__main__", guard won't fire
    return mod


def _deep_sig(v):
    if isinstance(v, tuple):
        return ("tuple", tuple(_deep_sig(x) for x in v))
    if isinstance(v, list):
        return ("list", tuple(_deep_sig(x) for x in v))
    if isinstance(v, (set, frozenset)):
        return (type(v).__name__, frozenset(_deep_sig(x) for x in v))
    if isinstance(v, dict):
        return ("dict", tuple((k, _deep_sig(x)) for k, x in v.items()))
    return type(v).__name__


# Loader-internal names introduced by the data-move shim. They are NOT part
# of the geo data surface: router.py harvests only non-underscore attributes
# and geo_lexicon imports specific names, so these are invisible to consumers.
_IGNORED_NAMES = {"_NLQ_BLOCKS", "__builtins__"}


def _public_containers(mod):
    """All module-level dict/set attributes (data + derived), minus the
    loader-internal cache and dunder containers."""
    out = {}
    for name in dir(mod):
        if name in _IGNORED_NAMES:
            continue
        val = getattr(mod, name)
        if isinstance(val, (dict, set, frozenset)):
            out[name] = val
    return out


def _compare_dict(name, o, c, failures):
    if len(o) != len(c):
        failures.append(f"{name}: length {len(o)} != {len(c)}")
    ok, ck = list(o.keys()), list(c.keys())
    if ok != ck:
        for i, (a, b) in enumerate(zip(ok, ck)):
            if a != b:
                failures.append(f"{name}: key order diverges at {i}: {a!r} != {b!r}")
                break
        else:
            failures.append(f"{name}: key list differs in length/tail")
        miss, extra = set(o) - set(c), set(c) - set(o)
        if miss:
            failures.append(f"{name}: missing keys sample {list(miss)[:5]}")
        if extra:
            failures.append(f"{name}: extra keys sample {list(extra)[:5]}")
    vmis = tmis = 0
    fex = None
    for k in o:
        if k not in c:
            continue
        if o[k] != c[k]:
            vmis += 1
            fex = fex or (k, o[k], c[k])
        elif _deep_sig(o[k]) != _deep_sig(c[k]):
            tmis += 1
            fex = fex or (k, _deep_sig(o[k]), _deep_sig(c[k]))
    if vmis:
        failures.append(f"{name}: {vmis} value mismatches, first {fex}")
    if tmis:
        failures.append(f"{name}: {tmis} deep-type mismatches, first {fex}")


def main() -> int:
    backups = sorted(glob.glob(os.path.join(REPO_ROOT, "non_liquid_locations.py.bak_datamove_*")))
    if not backups:
        raise SystemExit("FAIL: no non_liquid_locations.py.bak_datamove_* backup found")
    backup = backups[-1]
    live = os.path.join(REPO_ROOT, "non_liquid_locations.py")

    print(f"[parity] ground truth: {os.path.basename(backup)}")
    orig = _load_module_from_path(backup, "nl_orig")
    cand = _load_module_from_path(live, "nl_cand")

    oc, cc = _public_containers(orig), _public_containers(cand)
    print(f"[parity] original containers: {sorted(oc)}")

    failures = []

    only_o = set(oc) - set(cc)
    only_c = set(cc) - set(oc)
    if only_o:
        failures.append(f"attributes lost after refactor: {sorted(only_o)}")
    if only_c:
        failures.append(f"unexpected new attributes: {sorted(only_c)}")

    for name in sorted(set(oc) & set(cc)):
        o, c = oc[name], cc[name]
        if type(o) is not type(c):
            failures.append(f"{name}: container type {type(o).__name__} != {type(c).__name__}")
            continue
        if isinstance(o, dict):
            _compare_dict(name, o, c, failures)
        else:  # set/frozenset
            if o != c:
                failures.append(f"{name}: set contents differ (+{sorted(c-o)[:3]} / -{sorted(o-c)[:3]})")

    # the normalization helper must still exist and be callable
    if not callable(getattr(cand, "_ps_norm_keep_spaces", None)):
        failures.append("_ps_norm_keep_spaces missing or not callable in refactored module")

    if failures:
        print("FAIL: non_liquid_locations parity broken:")
        for f in failures:
            print(f"  - {f}")
        return 1

    total = sum(len(v) for v in cc.values())
    print(f"[parity] OK: {len(cc)} containers, {total} total entries identical "
          f"in keys, order, values and types")
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
