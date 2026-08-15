# -*- coding: utf-8 -*-
"""
P5 / F0 -- static (AST) detector for the dangerous shape:

    BEGIN [IMMEDIATE]
    -> write lock acquired
    -> await ...          <-- lock held across a suspension point
    -> COMMIT / ROLLBACK

Read-only. Opens no database, makes no network calls, changes nothing.

Heuristic (line-number based, not full control-flow analysis -- this is a
static best-effort scan, not a proof; see CLAUDE.md P5 spec section 11
"if feasible without invasive monkey-patching... at minimum static AST"):

For every async function, collect (in source order):
  - "begin" lines: any `.execute(...)` call whose string literal starts with
    BEGIN, or any call to *begin_immediate_async* / *_p5_begin_immediate_async*
  - "finish" lines: any `.commit()` / `.rollback()` call, or any call to
    *commit_async* / *rollback_async* / *_p5_commit_async* / *_p5_rollback_async*
  - "await" lines: every `ast.Await` node's line number

Each begin line is paired with the next finish line after it (source order).
Any await line strictly between them (excluding the begin/finish lines
themselves, since the begin/commit call is normally itself awaited) is
reported as "await while transaction open".

Usage:
    python tools\\sqlite_await_in_tx_audit.py [--out-md PATH.md]
"""

from __future__ import annotations

import argparse
import ast
import os
import sys

TARGET_FILES = [
    "main.py",
    "storage.py",
    "panel_bot.py",
    "manager_bot.py",
    "partner_stat_bot.py",
]

BEGIN_CALL_NAME_HINTS = ("begin_immediate_async", "_p5_begin_immediate_async", "traced_tx_async")
FINISH_CALL_NAME_HINTS = ("commit_async", "rollback_async", "_p5_commit_async", "_p5_rollback_async")


def _read_source(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return ""


class _AsyncFuncScanner(ast.NodeVisitor):
    def __init__(self, filename: str):
        self.filename = filename
        self.findings = []  # list of dict

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        begins = []
        finishes = []
        awaits = []
        for sub in ast.walk(node):
            if sub is node:
                continue
            if isinstance(sub, ast.Call):
                name = _call_name(sub)
                if name in ("execute", "executescript"):
                    for a in sub.args:
                        if isinstance(a, ast.Constant) and isinstance(a.value, str):
                            if a.value.strip().upper().startswith("BEGIN"):
                                begins.append(sub.lineno)
                        break
                if name in BEGIN_CALL_NAME_HINTS:
                    begins.append(sub.lineno)
                if name in ("commit", "rollback") or name in FINISH_CALL_NAME_HINTS:
                    finishes.append(sub.lineno)
            if isinstance(sub, ast.Await):
                awaits.append(sub.lineno)

        begins = sorted(set(begins))
        finishes = sorted(set(finishes))
        for b in begins:
            nxt = next((c for c in finishes if c >= b), None)
            in_between = [a for a in awaits if b < a < (nxt if nxt is not None else 10**9)]
            if in_between or nxt is None:
                self.findings.append({
                    "file": self.filename,
                    "function": node.name,
                    "begin_line": b,
                    "finish_line": nxt,
                    "await_lines_inside": in_between,
                    "risk": "AWAIT_WHILE_OPEN" if in_between else "NO_FINISH_FOUND_STATICALLY",
                })
        self.generic_visit(node)


def audit_file(path: str) -> list:
    filename = os.path.basename(path)
    source = _read_source(path)
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [{"file": filename, "function": "<parse-error>", "begin_line": e.lineno or 0,
                  "finish_line": None, "await_lines_inside": [], "risk": "PARSE_ERROR"}]
    scanner = _AsyncFuncScanner(filename)
    scanner.visit(tree)
    return scanner.findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-md", default=None)
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    all_findings = []
    for name in TARGET_FILES:
        path = os.path.join(args.root, name)
        if not os.path.exists(path):
            continue
        all_findings.extend(audit_file(path))

    awaits_open = [f for f in all_findings if f["risk"] == "AWAIT_WHILE_OPEN"]
    print(f"async functions scanned in {len(TARGET_FILES)} files; "
          f"begin/finish pairs flagged: {len(all_findings)}; "
          f"AWAIT_WHILE_OPEN: {len(awaits_open)}")

    if args.out_md:
        lines = []
        lines.append("# P5/F0 -- await-while-transaction-open static report")
        lines.append("")
        lines.append("Static AST heuristic (line-order pairing, not full control-flow analysis).")
        lines.append("A finding here is EVIDENCE to investigate, not a proof of a bug -- confirm")
        lines.append("manually before treating as a defect. `NO_FINISH_FOUND_STATICALLY` commonly")
        lines.append("means the commit/rollback call is a wrapped helper this scanner doesn't")
        lines.append("recognize by name, or lives in a code path a linear line-order pairing")
        lines.append("cannot see (e.g. handled entirely inside a called sub-function).")
        lines.append("")
        lines.append(f"Total begin/finish pairs inspected: {len(all_findings)}")
        lines.append(f"AWAIT_WHILE_OPEN findings: {len(awaits_open)}")
        lines.append("")
        lines.append("| file | function | begin_line | finish_line | await_lines_inside | risk |")
        lines.append("|---|---|---|---|---|---|")
        for f in all_findings:
            lines.append(f"| {f['file']} | {f['function']} | {f['begin_line']} | "
                          f"{f['finish_line']} | {f['await_lines_inside']} | {f['risk']} |")
        with open(args.out_md, "w", encoding="utf-8") as out:
            out.write("\n".join(lines) + "\n")
        print(f"written: {args.out_md}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
