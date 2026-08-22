#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline guard for the main.py / panel_bot.py override chains (AGENTS.md section 4).

WHAT THIS PROTECTS
------------------
Both files stack many top-level definitions of the same function name; only the LAST is
active, and the earlier ones are captured into `_ORIG_*` aliases and invoked as
fallbacks. Two failure modes follow from that, and both are silent:

  1. Someone "cleans up the duplicates" and deletes a shadowed def that the chain still
     delegates into -> behaviour disappears with no import error and no test failure.
  2. Someone edits the FIRST definition they grepped instead of the active last one ->
     the change never runs for inputs the active version handles itself.

This test cannot prevent either, but it makes the shape of the chains an asserted fact,
so a change in that shape shows up as a diff in an expected number instead of as a
production surprise. It is intentionally a characterization test: the counts below are
the CURRENT state, not a target. When you legitimately change a chain, update the
expected value in the same commit -- that edit is the review signal.

Also enforces the invariant that actually matters for safety: every duplicated
top-level name must still have at least one `globals().get("name")` capture, i.e. no
duplicated name has silently become a plain overwrite that drops earlier behaviour.

No network, no DB, no imports of the target modules (importing main.py has Telethon and
env side effects). Pure AST + text scan. Exit 0 = PASS.
"""
from __future__ import annotations

import ast
import collections
import os
import re
import sys
from typing import Dict, List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Characterization baseline, measured 2026-08-22. Update deliberately, in the same
# commit as the change that moves it, so the delta is visible in review.
EXPECTED: Dict[str, Dict[str, int]] = {
    "main.py": {"duplicated_names": 52, "shadowed_defs": 139},
    "panel_bot.py": {"duplicated_names": 20, "shadowed_defs": 65},
}
# The single worst chain: its size is load-bearing knowledge for anyone editing panel
# command handling, so assert it explicitly rather than burying it in a total.
EXPECTED_WORST = {"file": "main.py", "name": "_panel_execute_command_text", "count": 35}

FAILURES: List[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[OK]   {label}")
    else:
        FAILURES.append(f"{label} {detail}".rstrip())
        print(f"[FAIL] {label}  {detail}")


def _top_level_defs(path: str) -> Dict[str, List[int]]:
    src = open(path, encoding="utf-8-sig").read()
    out: Dict[str, List[int]] = collections.defaultdict(list)
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name].append(node.lineno)
    return out


def _has_capture(src: str, name: str) -> bool:
    return re.search(
        r"=\s*globals\(\)\s*(?:\.get\(|\[)\s*[\"']" + re.escape(name) + r"[\"']", src
    ) is not None


def main() -> int:
    for fname, exp in EXPECTED.items():
        path = os.path.join(REPO_ROOT, fname)
        if not os.path.exists(path):
            check(f"{fname} exists", False, "(file missing)")
            continue
        src = open(path, encoding="utf-8-sig").read()
        defs = _top_level_defs(path)
        dup = {k: v for k, v in defs.items() if len(v) > 1}
        shadowed = sum(len(v) - 1 for v in dup.values())

        check(f"{fname}: duplicated top-level names == {exp['duplicated_names']}",
              len(dup) == exp["duplicated_names"],
              f"got {len(dup)}")
        check(f"{fname}: shadowed defs == {exp['shadowed_defs']}",
              shadowed == exp["shadowed_defs"],
              f"got {shadowed}")

        # The real invariant: a duplicated name with no capture means the later def
        # REPLACED the earlier one outright, dropping whatever it did.
        uncaptured = sorted(n for n in dup if not _has_capture(src, n))
        check(f"{fname}: every duplicated name still has a delegation capture",
              not uncaptured,
              f"uncaptured: {uncaptured[:6]}")

        # Sanity: the active def is the last one by line number, which is the whole
        # basis of the "take the last" rule. Guards against an ordering surprise.
        bad_order = [n for n, ls in dup.items() if ls != sorted(ls)]
        check(f"{fname}: definition line numbers are ascending per name",
              not bad_order, f"unordered: {bad_order[:4]}")

    wpath = os.path.join(REPO_ROOT, EXPECTED_WORST["file"])
    if os.path.exists(wpath):
        occ = _top_level_defs(wpath).get(EXPECTED_WORST["name"], [])
        check(f"{EXPECTED_WORST['file']}: {EXPECTED_WORST['name']} defined "
              f"{EXPECTED_WORST['count']}x (active is the last)",
              len(occ) == EXPECTED_WORST["count"], f"got {len(occ)}")
        if occ:
            print(f"       -> ACTIVE definition at line {occ[-1]}; "
                  f"use tools/override_map.py before editing it")

    print()
    if FAILURES:
        print("=" * 70)
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s)")
        for f in FAILURES:
            print(f"  - {f}")
        print()
        print("If you intentionally changed an override chain, update EXPECTED in this")
        print("file in the same commit. If you did NOT, you may have deleted a shadowed")
        print("def that the chain still delegates into -- see tools/override_map.py.")
        return 1
    print("=" * 70)
    print("SELFTEST OK: override chains match the recorded baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
