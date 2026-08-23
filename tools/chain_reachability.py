"""Chain reachability analyzer for override collapse (roadmap stage R1).

For every duplicated top-level def name in a module, determine which shadowed
definitions are actually DEAD (unreachable from the active def or from any
live code through delegation captures) and can therefore be deleted safely.

Model:
  * A duplicated name N has defs D1..Dk (by line).  Only Dk is bound to N at
    runtime.
  * A module-level capture `VAR = globals().get("N")` (or `VAR = N`) taken at
    line L binds VAR to the def of N with the greatest lineno < L.
  * A def Di is LIVE iff:
      - it is the active def (Dk), or
      - some capture VAR pointing to Di is referenced anywhere in the module
        outside of (a) its own assignment line and (b) the bodies of defs
        already proven DEAD.
  * Fixpoint iteration handles wrapper-of-wrapper chains.

Usage:
    python tools/chain_reachability.py main.py [name ...]

With no names, analyzes every duplicated top-level def name and prints a
deletion plan (dead def line ranges).
"""

from __future__ import annotations

import ast
import collections
import re
import sys


def _read(fname: str) -> str:
    return open(fname, encoding="utf-8-sig").read()


def analyze(fname: str, only_names: list[str] | None = None):
    src = _read(fname)
    lines = src.splitlines()
    tree = ast.parse(src)

    # 1. top-level defs of each name, with end lines
    defs_by_name: dict[str, list[ast.AST]] = collections.defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs_by_name[node.name].append(node)

    dup_names = {n: v for n, v in defs_by_name.items() if len(v) > 1}
    if only_names:
        dup_names = {n: v for n, v in dup_names.items() if n in only_names}

    # 2. module-level captures: VAR = globals().get("X")  or  VAR = X
    #    (record var name, target name, line)
    captures: list[tuple[str, str, int]] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt = node.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        val = node.value
        target_name = None
        # VAR = globals().get("X") / globals().get("X", default)
        if (
            isinstance(val, ast.Call)
            and isinstance(val.func, ast.Attribute)
            and val.func.attr == "get"
            and isinstance(val.func.value, ast.Call)
            and isinstance(val.func.value.func, ast.Name)
            and val.func.value.func.id == "globals"
            and val.args
            and isinstance(val.args[0], ast.Constant)
            and isinstance(val.args[0].value, str)
        ):
            target_name = val.args[0].value
        elif isinstance(val, ast.Name):
            target_name = val.id
        if target_name:
            captures.append((tgt.id, target_name, node.lineno))

    report = {}
    for name, defs in sorted(dup_names.items()):
        def_lines = [(d.lineno, d.end_lineno) for d in defs]
        active_idx = len(defs) - 1

        # captures pointing at this name -> which def index each one captured
        my_caps: list[tuple[str, int, int]] = []  # (var, def_idx, cap_line)
        for var, tgt_name, cap_line in captures:
            if tgt_name != name:
                continue
            idx = None
            for i, (s, _e) in enumerate(def_lines):
                if s < cap_line:
                    idx = i
                else:
                    break
            if idx is not None:
                my_caps.append((var, idx, cap_line))

        # usage lines of each capture var (word match), excluding its own
        # assignment line
        var_use_lines: dict[str, list[int]] = {}
        for var, _idx, cap_line in my_caps:
            pat = re.compile(r"\b%s\b" % re.escape(var))
            uses = [
                i + 1
                for i, ln in enumerate(lines)
                if pat.search(ln) and (i + 1) != cap_line
            ]
            # also usages via globals().get("VAR")
            var_use_lines[var] = uses

        # fixpoint: def is live if active, or a capture var pointing to it is
        # used outside dead def bodies
        live = {active_idx}
        changed = True
        while changed:
            changed = False
            dead_ranges = [
                def_lines[i] for i in range(len(defs)) if i not in live
            ]

            def in_dead(line_no: int) -> bool:
                return any(s <= line_no <= e for s, e in dead_ranges)

            for var, idx, _cap_line in my_caps:
                if idx in live:
                    continue
                for use in var_use_lines[var]:
                    if not in_dead(use):
                        live.add(idx)
                        changed = True
                        break

        dead = [i for i in range(len(defs)) if i not in live]
        report[name] = {
            "def_lines": def_lines,
            "live": sorted(live),
            "dead": dead,
            "dead_ranges": [def_lines[i] for i in dead],
            "captures": my_caps,
        }
    return report


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    fname = sys.argv[1]
    names = sys.argv[2:] or None
    report = analyze(fname, names)
    total_dead = 0
    total_dead_lines = 0
    for name, info in sorted(
        report.items(), key=lambda kv: -len(kv[1]["dead"])
    ):
        k = len(info["def_lines"])
        dead = info["dead"]
        total_dead += len(dead)
        dl = sum(e - s + 1 for s, e in info["dead_ranges"])
        total_dead_lines += dl
        status = f"{len(dead)}/{k - 1} shadowed defs DEAD ({dl} lines)"
        print(f"{name}: x{k} -> {status}")
        for i in dead:
            s, e = info["def_lines"][i]
            print(f"    DEAD def #{i + 1} lines {s}-{e}")
    print(
        f"\nTOTAL: {total_dead} dead defs, ~{total_dead_lines} deletable lines"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
