# -*- coding: utf-8 -*-
"""tools/allow_spend_ast_gate.py -- semantic allow_spend=True gate (Master Plan P2).

HARD SAFETY INVARIANT (CLAUDE.md section 6):
    `allow_spend=True` may appear as a REAL CALL ARGUMENT in exactly 2 places
    project-wide, both in main.py:
        * make_ipv4(...)     inside the proxy buy-confirm flow
        * prolong_make(...)  inside _prenew_execute_renewal

Substring counting is useless here: the token occurs 20+ times across comments,
docstrings and prose in main.py alone. This gate is AST-based and counts only
`ast.keyword(arg="allow_spend", value=Constant(True))` on an actual `ast.Call`.

Also reported separately (NOT counted as spend):
    * `allow_spend=False`  -- the safe default (provider construction)
    * `allow_spend` as a function PARAMETER default in proxy_provider.py

Usage:
    python tools/allow_spend_ast_gate.py [--root <project>] [--json <out>]
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rc_release_control import dumps, is_archival, rel, walk_active  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent

EXPECTED_TRUE_CALL_SITES = 2
# R2 extraction (AGENTS.md section 4): both spend sites moved verbatim from
# main.py into the extracted proxy subsystem module -- the COUNT stays 2.
EXPECTED_FILE = "proxy_ops.py"

#: The EFFECTIVE spend target, not the syntactic callee.
#: Reality (verified in P2): both sites go through an executor-offload wrapper:
#:     await _pbuy_call(provider.make_ipv4,   ..., allow_spend=True)
#:     await _pbuy_call(provider.prolong_make, ..., allow_spend=True)
#: `_pbuy_call(fn, *args, **kwargs)` runs `fn` in an executor and forwards
#: allow_spend through **kwargs, so asserting on the syntactic callee alone
#: would assert the wrapper's name and miss which provider method actually spends.
EXPECTED_EFFECTIVE_TARGETS = {"make_ipv4", "prolong_make"}
EXPECTED_ENCLOSING = {"_handle_manager_proxy_buy_confirm_command", "_prenew_execute_renewal"}

#: Wrappers that forward *args/**kwargs to their first positional argument.
FORWARDING_WRAPPERS = {"_pbuy_call"}


def enclosing_function(tree: ast.AST, lineno: int) -> str:
    best, best_line = "<module>", -1
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if n.lineno <= lineno <= getattr(n, "end_lineno", n.lineno) and n.lineno > best_line:
                best, best_line = n.name, n.lineno
    return best


def callee_name(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return "<expr>"


def effective_target(call: ast.Call) -> str:
    """The function that ACTUALLY receives allow_spend.

    For a forwarding wrapper (`_pbuy_call(fn, *a, **kw)`) that is the first
    positional argument -- i.e. the real provider method that spends money.
    Otherwise it is the syntactic callee.
    """
    name = callee_name(call)
    if name in FORWARDING_WRAPPERS and call.args:
        first = call.args[0]
        if isinstance(first, ast.Attribute):
            return first.attr
        if isinstance(first, ast.Name):
            return first.id
        return "<unresolved-forward>"
    return name


def scan(root: Path) -> Dict[str, Any]:
    true_sites: List[Dict[str, Any]] = []
    false_sites: List[Dict[str, Any]] = []
    param_defaults: List[Dict[str, Any]] = []

    for p in walk_active(root, suffixes={".py"}):
        relp = rel(root, p)
        if relp.startswith("tools/"):
            continue                      # tests legitimately model spend flows
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue

        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                for kw in n.keywords:
                    if kw.arg != "allow_spend":
                        continue
                    v = kw.value
                    rec = {
                        "file": relp, "line": n.lineno,
                        "function": enclosing_function(tree, n.lineno),
                        "callee": callee_name(n),
                        "effective_target": effective_target(n),
                    }
                    if isinstance(v, ast.Constant) and v.value is True:
                        true_sites.append(rec)
                    elif isinstance(v, ast.Constant) and v.value is False:
                        false_sites.append(rec)
                    else:
                        rec["value"] = ast.unparse(v)
                        true_sites.append({**rec, "dynamic": True})
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = n.args
                allargs = list(args.args) + list(args.kwonlyargs)
                defaults = list(args.defaults) + [d for d in args.kw_defaults if d is not None]
                for a_ in allargs:
                    if a_.arg == "allow_spend":
                        param_defaults.append(
                            {"file": relp, "line": n.lineno, "function": n.name})
                        break

    true_sites.sort(key=lambda r: (r["file"], r["line"]))
    false_sites.sort(key=lambda r: (r["file"], r["line"]))

    problems: List[str] = []
    if len(true_sites) != EXPECTED_TRUE_CALL_SITES:
        problems.append(
            f"expected exactly {EXPECTED_TRUE_CALL_SITES} allow_spend=True call arguments, "
            f"found {len(true_sites)}")
    for s in true_sites:
        if s["file"] != EXPECTED_FILE:
            problems.append(f"allow_spend=True outside {EXPECTED_FILE}: {s}")
        if s.get("dynamic"):
            problems.append(f"allow_spend passed a NON-LITERAL value: {s}")
    found_targets = {s["effective_target"] for s in true_sites}
    if found_targets != EXPECTED_EFFECTIVE_TARGETS:
        problems.append(
            f"expected effective spend targets {sorted(EXPECTED_EFFECTIVE_TARGETS)}, "
            f"found {sorted(found_targets)}")
    found_enclosing = {s["function"] for s in true_sites}
    if found_enclosing != EXPECTED_ENCLOSING:
        problems.append(
            f"expected enclosing business functions {sorted(EXPECTED_ENCLOSING)}, "
            f"found {sorted(found_enclosing)}")
    for s in true_sites:
        if s["effective_target"] == "<unresolved-forward>":
            problems.append(f"allow_spend forwarded to an unresolvable target: {s}")

    return {
        "expected_true_call_sites": EXPECTED_TRUE_CALL_SITES,
        "actual_true_call_sites": len(true_sites),
        "true_call_sites": true_sites,
        "false_call_sites_count": len(false_sites),
        "false_call_sites": false_sites,
        "parameter_declarations": param_defaults,
        "problems": problems,
        "ok": not problems,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(BASE_DIR))
    ap.add_argument("--json")
    a = ap.parse_args()
    res = scan(Path(a.root).resolve())
    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(dumps(res), encoding="utf-8")

    print(f"allow_spend=True call arguments: {res['actual_true_call_sites']} "
          f"(expected {res['expected_true_call_sites']})")
    for s in res["true_call_sites"]:
        print(f"  {s['file']}:{s['line']}  {s['function']}() -> {s['callee']}(...) "
              f"-> EFFECTIVE TARGET: {s['effective_target']}(allow_spend=True)")
    print(f"allow_spend=False call arguments: {res['false_call_sites_count']} (safe default)")
    print(f"allow_spend parameter declarations: {len(res['parameter_declarations'])}")
    for p in res["parameter_declarations"]:
        print(f"  {p['file']}:{p['line']}  def {p['function']}(... allow_spend ...)")
    for pr in res["problems"]:
        print(f"[FAIL] {pr}")
    print("RESULT:", "PASS" if res["ok"] else "FAIL")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
