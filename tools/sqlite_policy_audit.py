# -*- coding: utf-8 -*-
"""
P5 / F0 -- read-only static inventory of SQLite connection/transaction policy.

Scans local source only. Makes NO network calls, opens NO database, changes
NOTHING. For each target file, uses `ast` to enumerate:

  - sqlite3.connect(...) / aiosqlite.connect(...) call sites (heuristic: any
    `<name>.connect(...)` where <name> contains "sqlite", case-insensitive --
    covers the many local aliases this codebase uses, e.g. _bsl_sqlite3,
    _tp_ci_sqlite3, aiosqlite)
  - "BEGIN" / "BEGIN IMMEDIATE" / "PRAGMA busy_timeout" string literals
    passed to .execute()-like calls
  - `timeout=` / `isolation_level` / `autocommit` keyword arguments on
    connect() calls

For module-level function defs, classifies ACTIVE (last definition of that
name in the file, per this project's override-chain convention -- see
CLAUDE.md section 4) vs SHADOWED (an earlier same-named definition).

Usage:
    python tools\\sqlite_policy_audit.py [--out PATH.csv]
"""

from __future__ import annotations

import argparse
import ast
import csv
import os
import sys

TARGET_FILES = [
    "main.py",
    "storage.py",
    "panel_bot.py",
    "manager_bot.py",
    "partner_stat_bot.py",
]

CENTRAL_DB_NAME_HINTS = ("TPILOT_DB_PATH", "QUEUE_DB_PATH", "DEFAULT_QUEUE_DB_PATH", "DB_PATH", "db_path")


def _read_source(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return f.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()


def _module_level_function_ranges(tree: ast.Module):
    """Return {name: [(lineno, is_async), ...]} for module-level defs only,
    in source order -- this is what CLAUDE.md's override-chain rule applies to."""
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, []).append(node.lineno)
    return out


def _active_lines(name_to_lines) -> dict:
    """name -> the line number of its ACTIVE (last) definition."""
    return {name: max(lines) for name, lines in name_to_lines.items()}


class _EnclosingFuncTracker(ast.NodeVisitor):
    """Walks the whole tree, remembers the nearest enclosing module-level
    function name for every node visited (approximation: nested defs are
    attributed to their nearest ancestor def, which is what we want for
    per-site reporting)."""

    def __init__(self, filename: str, active_by_name: dict, source_lines):
        self.filename = filename
        self.active_by_name = active_by_name
        self.source_lines = source_lines
        self.stack = []  # list of (name, is_async, is_module_level)
        self.rows = []

    def _current(self):
        if not self.stack:
            return "<module>", False, "n/a"
        return self.stack[-1][0], self.stack[-1][1], self.stack[-1][2]

    def _is_active(self, name: str, lineno_of_def: int) -> str:
        if name == "<module>":
            return "n/a"
        active_ln = self.active_by_name.get(name)
        if active_ln is None:
            return "unknown"
        return "active" if lineno_of_def == active_ln else "shadowed"

    def visit_FunctionDef(self, node):
        self._visit_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._visit_func(node, is_async=True)

    def _visit_func(self, node, is_async: bool):
        module_level = not self.stack  # only track override status for module-level names
        status = self._is_active(node.name, node.lineno) if module_level else "nested"
        self.stack.append((node.name, is_async, status))
        self.generic_visit(node)
        self.stack.pop()

    def _db_class_hint(self, call_node: ast.Call) -> str:
        try:
            for arg in list(call_node.args) + [kw.value for kw in call_node.keywords]:
                if isinstance(arg, ast.Name) and arg.id in CENTRAL_DB_NAME_HINTS:
                    return "central"
                if isinstance(arg, ast.Attribute) and arg.attr in CENTRAL_DB_NAME_HINTS:
                    return "central"
        except Exception:
            pass
        return "unknown"

    def visit_Call(self, node):
        func_name, is_async_fn, status = self._current()
        try:
            # connect() sites
            if isinstance(node.func, ast.Attribute) and node.func.attr == "connect":
                target_name = ""
                if isinstance(node.func.value, ast.Name):
                    target_name = node.func.value.id
                elif isinstance(node.func.value, ast.Attribute):
                    target_name = node.func.value.attr
                if "sqlite" in target_name.lower():
                    is_aiosqlite = "aiosqlite" in target_name.lower()
                    kw_names = {kw.arg for kw in node.keywords if kw.arg}
                    self.rows.append({
                        "file": self.filename,
                        "line": node.lineno,
                        "function": func_name,
                        "override_status": status,
                        "kind": "aiosqlite.connect" if is_aiosqlite else "sqlite3.connect",
                        "sync_or_async": "async" if is_aiosqlite else "sync",
                        "db_class_hint": self._db_class_hint(node),
                        "has_timeout_kw": "timeout" in kw_names,
                        "has_isolation_level_kw": "isolation_level" in kw_names,
                        "has_autocommit_kw": "autocommit" in kw_names,
                        "detail": target_name + ".connect(...)",
                    })

            # execute()-like calls carrying a string literal we care about
            if isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executescript"):
                for a in node.args:
                    text = None
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        text = a.value
                    if not text:
                        continue
                    up = text.upper()
                    kind = None
                    if "BEGIN IMMEDIATE" in up:
                        kind = "BEGIN IMMEDIATE"
                    elif "BEGIN DEFERRED" in up:
                        kind = "BEGIN DEFERRED"
                    elif up.strip().startswith("BEGIN"):
                        kind = "BEGIN (plain)"
                    elif "BUSY_TIMEOUT" in up:
                        kind = "PRAGMA busy_timeout"
                    if kind:
                        parent_is_await = isinstance(getattr(node, "_p5_parent", None), ast.Await)
                        self.rows.append({
                            "file": self.filename,
                            "line": node.lineno,
                            "function": func_name,
                            "override_status": status,
                            "kind": kind,
                            "sync_or_async": "async(awaited)" if parent_is_await else ("async_fn" if is_async_fn else "sync"),
                            "db_class_hint": "unknown",
                            "has_timeout_kw": False,
                            "has_isolation_level_kw": False,
                            "has_autocommit_kw": False,
                            "detail": text.strip()[:60],
                        })
        except Exception:
            pass
        self.generic_visit(node)


def _annotate_await_parents(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Await):
            setattr(node.value, "_p5_parent", node)


def audit_file(path: str) -> list:
    filename = os.path.basename(path)
    source = _read_source(path)
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [{
            "file": filename, "line": e.lineno or 0, "function": "<parse-error>",
            "override_status": "n/a", "kind": "PARSE_ERROR", "sync_or_async": "n/a",
            "db_class_hint": "n/a", "has_timeout_kw": False, "has_isolation_level_kw": False,
            "has_autocommit_kw": False, "detail": str(e)[:120],
        }]
    _annotate_await_parents(tree)
    name_lines = _module_level_function_ranges(tree)
    active_by_name = _active_lines(name_lines)
    visitor = _EnclosingFuncTracker(filename, active_by_name, source.splitlines())
    visitor.visit(tree)
    return visitor.rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    all_rows = []
    for name in TARGET_FILES:
        path = os.path.join(args.root, name)
        if not os.path.exists(path):
            all_rows.append({
                "file": name, "line": 0, "function": "<missing-file>", "override_status": "n/a",
                "kind": "FILE_NOT_FOUND", "sync_or_async": "n/a", "db_class_hint": "n/a",
                "has_timeout_kw": False, "has_isolation_level_kw": False, "has_autocommit_kw": False,
                "detail": path,
            })
            continue
        all_rows.extend(audit_file(path))

    all_rows.sort(key=lambda r: (r["file"], r["line"]))

    out_path = args.out or os.path.join(args.root, "sqlite_policy_inventory.csv")
    fieldnames = ["file", "line", "function", "override_status", "kind", "sync_or_async",
                  "db_class_hint", "has_timeout_kw", "has_isolation_level_kw",
                  "has_autocommit_kw", "detail"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in all_rows:
            w.writerow(r)

    connect_sites = [r for r in all_rows if "connect" in r["kind"]]
    begin_immediate = [r for r in all_rows if r["kind"] == "BEGIN IMMEDIATE"]
    print(f"rows={len(all_rows)} connect_sites={len(connect_sites)} begin_immediate={len(begin_immediate)}")
    print(f"written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
