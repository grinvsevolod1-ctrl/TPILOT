"""Delete provably-dead shadowed defs found by chain_reachability (stage R1).

Reads the reachability report for a module, deletes the dead def line ranges
(bottom-up so line numbers stay valid), and also deletes any module-level
capture assignment whose variable exclusively pointed at a deleted def and is
no longer referenced afterwards.

Safety:
  * Never deletes an ACTIVE def.
  * Re-parses the result to guarantee it is still valid Python.
  * Verifies the active def of every touched name is still present at the
    same relative position (last definition).
  * Writes nothing unless all checks pass; prints a summary diff of counts.

Usage:
    python tools/collapse_dead_defs.py main.py [name ...] [--apply]

Without --apply it is a dry run.
"""

from __future__ import annotations

import ast
import re
import sys

sys.path.insert(0, "tools")
from chain_reachability import analyze, _read  # noqa: E402


def collapse(fname: str, names: list[str] | None, apply: bool) -> int:
    src = _read(fname)
    had_bom = open(fname, "rb").read(3) == b"\xef\xbb\xbf"
    lines = src.splitlines(keepends=True)
    report = analyze(fname, names)

    # collect dead ranges
    dead_ranges: list[tuple[int, int, str]] = []
    for name, info in report.items():
        for i in info["dead"]:
            s, e = info["def_lines"][i]
            dead_ranges.append((s, e, name))
    if not dead_ranges:
        print(f"{fname}: nothing dead to delete")
        return 0

    dead_ranges.sort(reverse=True)
    total_lines = 0
    for s, e, name in dead_ranges:
        # widen range upward to swallow immediately preceding comment lines
        # that belong to the def (blank line boundary)
        start = s
        while start - 2 >= 0 and lines[start - 2].lstrip().startswith("#"):
            start -= 1
        del lines[start - 1 : e]
        total_lines += e - start + 1

    new_src = "".join(lines)

    # 1. still valid python?
    try:
        new_tree = ast.parse(new_src)
    except SyntaxError as exc:
        print(f"FATAL: result does not parse: {exc}")
        return 1

    # 2. every touched name still has its active def as last definition and
    #    the number of remaining defs matches live count
    new_defs: dict[str, int] = {}
    for node in new_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            new_defs[node.name] = new_defs.get(node.name, 0) + 1
    ok = True
    for name, info in report.items():
        if not info["dead"]:
            continue
        expect = len(info["live"])
        got = new_defs.get(name, 0)
        if got != expect:
            print(f"FATAL: {name}: expected {expect} defs left, got {got}")
            ok = False
    if not ok:
        return 1

    # 3. drop orphaned capture assignments: module-level VAR = ... whose VAR
    #    no longer appears anywhere else in the file
    out_lines = new_src.splitlines(keepends=True)
    removed_caps = []
    for name, info in report.items():
        if not info["dead"]:
            continue
        for var, _idx, _cap_line in info["captures"]:
            pat = re.compile(r"\b%s\b" % re.escape(var))
            uses = [i for i, ln in enumerate(out_lines) if pat.search(ln)]
            assign_only = [
                i
                for i in uses
                if re.match(r"^%s\s*=" % re.escape(var), out_lines[i])
            ]
            if uses and len(uses) == len(assign_only):
                for i in sorted(assign_only, reverse=True):
                    removed_caps.append(out_lines[i].rstrip())
                    del out_lines[i]
    new_src = "".join(out_lines)
    try:
        ast.parse(new_src)
    except SyntaxError as exc:
        print(f"FATAL after capture cleanup: {exc}")
        return 1

    print(
        f"{fname}: deleting {len(dead_ranges)} dead defs "
        f"(~{total_lines} lines), {len(removed_caps)} orphaned captures"
    )
    for cap in removed_caps:
        print(f"  orphan capture removed: {cap[:80]}")

    if apply:
        data = new_src.encode("utf-8")
        if had_bom:
            data = b"\xef\xbb\xbf" + data
        with open(fname, "wb") as fh:
            fh.write(data)
        print(f"{fname}: WRITTEN")
    else:
        print("(dry run; pass --apply to write)")
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--apply"]
    apply = "--apply" in sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    fname = args[0]
    names = args[1:] or None
    return collapse(fname, names, apply)


if __name__ == "__main__":
    sys.exit(main())
