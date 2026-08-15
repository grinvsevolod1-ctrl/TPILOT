# -*- coding: utf-8 -*-
"""Override-stack reachability analyzer (refactor stage 1, 2026-08-15).

This project historically appended new versions of a function as a fresh
top-level `def` with the same name ("override stack"): only the LAST def is
bound at import time. Earlier (shadowed) defs are dead code UNLESS some LIVE
code captured them before the redefinition via the PREV pattern:

    _X_PREV = globals().get("name")   # snapshot of the previous def
    def name(...):
        ...uses _X_PREV as fallback...

Classification rules (CONSERVATIVE -- any doubt means LIVE):

  For each name defined N>1 times at module top level, the LAST def is
  always LIVE. For each earlier def D (ending before line L of the next
  def of the same name):

  1. If NO `globals().get("name")` / `globals().get('name')` snapshot
     appears anywhere in the file AFTER D starts and BEFORE the next
     redefinition of the name, D is DEAD (nothing could have captured it).
  2. If a snapshot exists in that window, D is captured by some variable V.
     D is then LIVE unless V itself is provably dead -- we do NOT chase
     that transitively: captured == LIVE.
  3. Decorated defs, defs inside try/except at module level, and defs whose
     name is also assigned via module-level `name = ...` assignments are
     LIVE (too risky).
  4. If anything about a def is ambiguous, it is LIVE.

Output: a per-file report and a machine-readable deletion list
(`/tmp/override_dead_spans_<file>.json`) with [start_line, end_line] spans
(1-based, inclusive) of DEAD defs, including any immediately preceding
comment block and decorator lines (there are none for dead defs by rule 3).

The report is REVIEW input; the deletion script consumes the JSON.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

FILES = ["partner_stat_bot.py", "manager_bot.py", "panel_bot.py", "main.py"]

# Names that selftests extract by name-position or that are part of the
# safety-critical spend path: NEVER classify any of their defs as dead.
EXCLUDE_NAMES = {
    "_mbstat_start_buttons",           # closer_settings/w1 selftests extract by PREV marker
    "_prenew_execute_renewal",         # allow_spend invariant site
    "_handle_manager_proxy_buy_confirm_command",  # allow_spend invariant site
}


def _snapshot_pattern(name: str) -> re.Pattern:
    return re.compile(
        r"""globals\(\)\s*\.\s*get\(\s*['"]""" + re.escape(name) + r"""['"]"""
    )


def analyze(path: Path) -> dict:
    src = path.read_text(encoding="utf-8-sig")
    lines = src.splitlines()
    tree = ast.parse(src)

    defs = defaultdict(list)  # name -> [node, ...] in order
    assigned_names = set()    # names also bound by module-level assignment
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name].append(node)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned_names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned_names.add(node.target.id)

    dead_spans = []
    live_prev = []
    skipped = []

    for name, nodes in defs.items():
        if len(nodes) < 2:
            continue
        if name in EXCLUDE_NAMES:
            skipped.append((name, "excluded (selftest/safety critical)"))
            continue
        if name in assigned_names:
            skipped.append((name, "also bound by module-level assignment"))
            continue
        snap_re = _snapshot_pattern(name)
        for i, node in enumerate(nodes[:-1]):  # every def except the last
            if node.decorator_list:
                skipped.append((name, f"def at {node.lineno} is decorated"))
                continue
            next_def_line = nodes[i + 1].lineno
            # Window: from THIS def's first line to the line before the next
            # redefinition. A snapshot BEFORE this def captures an even
            # earlier version; a snapshot AFTER the next def captures a
            # later version. Only a snapshot in this window keeps D alive.
            window = "\n".join(lines[node.lineno - 1:next_def_line - 1])
            if snap_re.search(window):
                live_prev.append((name, node.lineno, node.end_lineno))
                continue
            # Extend span upward over the contiguous comment/blank block that
            # documents this def (stop at first non-comment, non-blank line).
            start = node.lineno
            j = start - 2  # 0-based index of the line above the def
            while j >= 0:
                stripped = lines[j].strip()
                if stripped.startswith("#") or stripped == "":
                    j -= 1
                else:
                    break
            span_start = j + 2  # back to 1-based first line of the block
            dead_spans.append({
                "name": name,
                "def_line": node.lineno,
                "start": span_start,
                "end": node.end_lineno,
            })

    dead_spans.sort(key=lambda s: s["start"])

    # Sanity: spans must not overlap.
    for a, b in zip(dead_spans, dead_spans[1:]):
        if a["end"] >= b["start"]:
            raise AssertionError(f"overlapping spans in {path.name}: {a} vs {b}")

    return {
        "file": path.name,
        "total_lines": len(lines),
        "dead": dead_spans,
        "live_prev": live_prev,
        "skipped": skipped,
    }


def main() -> None:
    base = Path(__file__).resolve().parent.parent
    only = sys.argv[1:] or FILES
    for fname in only:
        rep = analyze(base / fname)
        dead_lines = sum(s["end"] - s["start"] + 1 for s in rep["dead"])
        print(f"== {rep['file']} ==")
        print(f"   dead defs: {len(rep['dead'])} (~{dead_lines} lines)")
        print(f"   live via PREV capture: {len(rep['live_prev'])}")
        print(f"   skipped (conservative): {len(rep['skipped'])}")
        for name, reason in rep["skipped"]:
            print(f"      SKIP {name}: {reason}")
        out = Path(f"/tmp/override_dead_spans_{rep['file']}.json")
        out.write_text(json.dumps(rep["dead"], indent=1), encoding="utf-8")
        print(f"   spans written: {out}")


if __name__ == "__main__":
    main()
