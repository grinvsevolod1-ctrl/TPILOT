#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Locate the ACTIVE definition of a function in main.py / panel_bot.py.

WHY THIS EXISTS (AGENTS.md section 4, "override traps")
-------------------------------------------------------
main.py and panel_bot.py stack many definitions of the same function name. Only the
LAST one is active; every earlier one is usually captured first, e.g.

    _TPILOT_PROXY_CHECK_V2_ORIG_PANEL_EXEC = globals().get("_panel_execute_command_text")
    async def _panel_execute_command_text(...):      # new behaviour
        ...
        return await _TPILOT_PROXY_CHECK_V2_ORIG_PANEL_EXEC(...)   # delegate to previous

So the shadowed definitions are NOT dead code -- they are links in a delegation chain.
Deleting them, or "cleaning up the duplicates", silently removes behaviour. Editing the
first definition you find is the other half of the same trap: the change lands in a link
that the active version may never delegate to for your input.

The rule in AGENTS.md is "grep -n 'def name' and take the last". That works but is
error-prone by hand: it does not distinguish top-level defs from nested/class methods,
and it does not show the delegation aliases. This tool answers the question exactly:

    python tools/override_map.py _panel_execute_command_text
    python tools/override_map.py --all            # every shadowed name, both files
    python tools/override_map.py --stats          # one-line inventory

Read-only: never edits, never imports the target modules (import has Telethon/env side
effects), pure AST + text scan.
"""
from __future__ import annotations

import argparse
import ast
import collections
import os
import re
import sys
from typing import Dict, List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ("main.py", "panel_bot.py")


def _top_level_defs(path: str) -> Dict[str, List[Tuple[int, bool]]]:
    """name -> [(lineno, is_async), ...] in file order, TOP LEVEL only.

    Nested functions and class methods are deliberately excluded: they cannot
    participate in the module-level override chain, and including them is what makes a
    raw `grep -n "def name"` misleading.
    """
    src = open(path, encoding="utf-8-sig").read()
    out: Dict[str, List[Tuple[int, bool]]] = collections.defaultdict(list)
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name].append((node.lineno, isinstance(node, ast.AsyncFunctionDef)))
    return out


def _capture_aliases(path: str, name: str) -> List[Tuple[int, str]]:
    """Find `SOME_ALIAS = globals().get("name")` capture lines for a function."""
    pat = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*globals\(\)\s*(?:\.get\(|\[)\s*"
        rf"[\"']{re.escape(name)}[\"']"
    )
    found: List[Tuple[int, str]] = []
    with open(path, encoding="utf-8-sig") as fh:
        for i, line in enumerate(fh, 1):
            m = pat.match(line)
            if m:
                found.append((i, m.group(1)))
    return found


def describe(name: str) -> int:
    hits = 0
    for fname in TARGETS:
        path = os.path.join(REPO_ROOT, fname)
        if not os.path.exists(path):
            continue
        defs = _top_level_defs(path).get(name, [])
        if not defs:
            continue
        hits += 1
        aliases = _capture_aliases(path, name)
        print(f"\n=== {fname}: {len(defs)} top-level definition(s) of {name}() ===")
        for idx, (lineno, is_async) in enumerate(defs):
            kind = "async def" if is_async else "def"
            if idx == len(defs) - 1:
                print(f"  line {lineno:<7} {kind:<9}  <== ACTIVE (edit this one)")
            else:
                print(f"  line {lineno:<7} {kind:<9}      shadowed (link in the chain)")
        if aliases:
            print(f"  -- {len(aliases)} delegation capture(s); shadowed defs stay reachable:")
            for lineno, alias in aliases:
                print(f"     line {lineno:<7} {alias}")
            print("  Do NOT delete shadowed defs: the chain calls back into them.")
        elif len(defs) > 1:
            print("  -- no globals() capture found: later defs REPLACE earlier ones")
            print("     outright, so earlier behaviour is unreachable. Verify before")
            print("     assuming the old code still runs.")
    if not hits:
        print(f"{name}: no top-level definition in {', '.join(TARGETS)}")
        return 1
    return 0


def list_all(stats_only: bool = False) -> int:
    for fname in TARGETS:
        path = os.path.join(REPO_ROOT, fname)
        if not os.path.exists(path):
            continue
        defs = _top_level_defs(path)
        dup = {k: v for k, v in defs.items() if len(v) > 1}
        shadowed = sum(len(v) - 1 for v in dup.values())
        total_caps = len(re.findall(r"globals\(\)\s*(?:\.get\(|\[)", open(path, encoding="utf-8-sig").read()))
        print(f"\n=== {fname} ===")
        print(f"  top-level defs      : {sum(len(v) for v in defs.values())}")
        print(f"  duplicated names    : {len(dup)}")
        print(f"  shadowed defs       : {shadowed}")
        print(f"  globals() captures  : {total_caps}")
        if stats_only:
            continue
        for name, occ in sorted(dup.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            caps = len(_capture_aliases(path, name))
            print(f"  {len(occ):3}x  {name:<52} ACTIVE@{occ[-1][0]:<7} captures={caps}")
    return 0


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Show which definition of a function is ACTIVE in main.py/panel_bot.py.")
    ap.add_argument("name", nargs="?", help="function name to locate")
    ap.add_argument("--all", action="store_true", help="list every shadowed name")
    ap.add_argument("--stats", action="store_true", help="inventory counts only")
    args = ap.parse_args(argv)
    if args.stats:
        return list_all(stats_only=True)
    if args.all:
        return list_all()
    if not args.name:
        ap.print_help()
        return 2
    return describe(args.name)


if __name__ == "__main__":
    raise SystemExit(main())
