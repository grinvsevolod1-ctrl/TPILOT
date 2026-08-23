"""De-vein override chains: give every shadowed def a unique name (stage R1).

THE PROBLEM
-----------
main.py / panel_bot.py define the same top-level function name repeatedly.
Only the LAST def is active; earlier ones are kept reachable through
module-level capture aliases (`_ORIG_X = globals().get("x")`) evaluated at
import time, which freeze whichever def was most recent AT THAT LINE.
This "last def wins + line-position-sensitive captures" pattern is the single
biggest editing hazard in the codebase (AGENTS.md section 4/5).

THE TRANSFORMATION (behavior-preserving, purely mechanical)
-----------------------------------------------------------
For every name with N>1 top-level defs at lines L1 < L2 < ... < LN:

  1. Rename def i (i < N) to  {name}__prev{i}.  The ACTIVE def keeps the name.
  2. Rewrite every module-level capture  VAR = globals().get("{name}")  (or
     globals()["{name}"]) to a direct reference to the def that was current
     at that line:  VAR = {name}__prev{i}  --  or  VAR = {name}  if the
     capture sits after the last def.  Import-time binding is identical.
  3. Rewrite any OTHER module-level Load of the name between def i and
     def i+1 to {name}__prev{i} (same import-time binding).  References
     inside function bodies are left alone: they resolve through globals()
     at call time and the active def keeps its name.

Runtime object graph before and after is IDENTICAL; the difference is that
every binding is now static and grep-able, and redefining a function can no
longer silently orphan an earlier version.

Safety rails: refuses to write if the result does not parse, if any target
rename collides with an existing name, or if the set of (capture var ->
resolved def line) mappings is not total and unambiguous.

Usage:
    python tools/devein_chains.py main.py [name ...] [--apply]
"""

from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict

sys.path.insert(0, "tools")
from chain_reachability import _read  # noqa: E402

CAPTURE_RE = re.compile(
    r"""globals\(\)\s*(?:\.get\(\s*(['"])(\w+)\1\s*\)|\[\s*(['"])(\w+)\3\s*\])"""
)


def _capture_name(m: "re.Match[str]") -> str:
    return m.group(2) or m.group(4)


def transform(fname: str, only: list[str] | None, apply: bool) -> int:
    src = _read(fname)
    had_bom = open(fname, "rb").read(3) == b"\xef\xbb\xbf"
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)

    defs: dict[str, list[ast.AST]] = defaultdict(list)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name].append(node)
    dup = {k: v for k, v in defs.items() if len(v) > 1}
    if only:
        dup = {k: v for k, v in dup.items() if k in only}
    if not dup:
        print(f"{fname}: no duplicated names selected")
        return 0

    all_names = set(defs)
    for name in dup:
        for i in range(1, len(dup[name])):
            newname = f"{name}__prev{i}"
            if newname in all_names or newname in src:
                print(f"FATAL: rename target {newname} already exists")
                return 1

    def resolve(name: str, lineno: int) -> str:
        """Name that a module-level reference at `lineno` binds to."""
        nodes = dup[name]
        idx = 0
        for j, node in enumerate(nodes, start=1):
            if node.lineno < lineno:
                idx = j
        if idx == 0:
            print(f"FATAL: reference to {name} at line {lineno} precedes "
                  f"every def")
            raise SystemExit(1)
        if idx == len(nodes):
            return name  # active def keeps its name
        return f"{name}__prev{idx}"

    # ------- pass 1: def-line renames (def i<N gets __prev{i}) -------
    edits: list[tuple[int, str, str]] = []  # (line_idx, old_fragment, new_fragment)
    for name, nodes in dup.items():
        for i, node in enumerate(nodes[:-1], start=1):
            li = node.lineno - 1
            old = lines[li]
            kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            pat = re.compile(r"\b%s\s+%s\b" % (kw.replace(" ", r"\s+"), re.escape(name)))
            if not pat.search(old):
                print(f"FATAL: def line {node.lineno} does not contain "
                      f"'{kw} {name}'")
                return 1
            edits.append((li, old, pat.sub(f"{kw} {name}__prev{i}", old, count=1)))

    # ------- pass 2: module-level references (captures and plain loads) ----
    # Collect every module-level statement that is NOT one of the dup defs,
    # walk it for (a) capture patterns, (b) plain Name loads of dup names.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue  # bodies resolve at call time via globals(); leave alone
        seg_start, seg_end = node.lineno, getattr(node, "end_lineno", node.lineno)
        for li in range(seg_start - 1, seg_end):
            line = lines[li]
            # skip already-edited def lines (cannot overlap: defs excluded)
            new_line = line
            for m in CAPTURE_RE.finditer(line):
                name = _capture_name(m)
                if name not in dup:
                    continue
                target = resolve(name, li + 1)
                new_line = new_line.replace(m.group(0), target, 1)
            if new_line != line:
                edits.append((li, line, new_line))
        # plain Name loads at module level (rare; assignment aliases etc.)
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                    and sub.id in dup):
                li = sub.lineno - 1
                line = lines[li]
                if CAPTURE_RE.search(line):
                    continue  # handled above
                target = resolve(sub.id, sub.lineno)
                if target == sub.id:
                    continue
                new_line = re.sub(r"\b%s\b" % re.escape(sub.id), target, line)
                edits.append((li, line, new_line))

    # apply edits (dedupe: later edits may hit same line; merge sequentially)
    by_line: dict[int, str] = {}
    for li, old, new in edits:
        cur = by_line.get(li, lines[li])
        if by_line.get(li) is None and cur != old:
            # first edit for this line must match the on-disk text
            print(f"FATAL: line {li+1} changed unexpectedly")
            return 1
        if by_line.get(li) is not None:
            # sequential edit: recompute from current
            new = new if cur == old else None
            if new is None:
                # re-run replacement logic is complex; bail out loudly
                print(f"FATAL: overlapping edits on line {li+1}")
                return 1
        by_line[li] = new

    out_lines = list(lines)
    for li, new in by_line.items():
        out_lines[li] = new
    new_src = "".join(out_lines)

    try:
        new_tree = ast.parse(new_src)
    except SyntaxError as exc:
        print(f"FATAL: result does not parse: {exc}")
        return 1

    # verify: no duplicated top-level names remain among the selected set
    counts: dict[str, int] = defaultdict(int)
    for node in new_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            counts[node.name] += 1
    still = {k: c for k, c in counts.items() if c > 1 and k in dup}
    if still:
        print(f"FATAL: still duplicated after transform: {still}")
        return 1
    # verify: no dangling globals().get captures of renamed names at top level
    for node in new_tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        seg = ast.get_source_segment(new_src, node) or ""
        for m in CAPTURE_RE.finditer(seg):
            if _capture_name(m) in dup:
                print(f"FATAL: unrewritten module-level capture of "
                      f"{_capture_name(m)} near line {node.lineno}")
                return 1

    n_renamed = sum(len(v) - 1 for v in dup.values())
    print(f"{fname}: renamed {n_renamed} shadowed defs across "
          f"{len(dup)} chains; rewired module-level captures to static refs")

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
    return transform(args[0], args[1:] or None, apply)


if __name__ == "__main__":
    sys.exit(main())
