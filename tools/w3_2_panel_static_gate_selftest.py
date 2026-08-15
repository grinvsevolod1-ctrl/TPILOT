# -*- coding: utf-8 -*-
"""tools/w3_2_panel_static_gate_selftest.py -- the frozen Plan Freeze section 2.4
static gate for panel_bot.py.

W3.2 CORRECTION ROUND 4 (2026-07-30): an independent final review of round 3
(C:\\ALM_TPilot_AUDIT\\20260730\\W3_2_CORRECTION_R3_REVIEW\\, verdict FAIL) found a
residual class of the SAME defect F-2 was meant to close (finding "F-2b"): round 3's
`outgoing_edges()` only ever emitted an edge for a called `Name` that was a module-level
def, a tracked module-level `globals().get(...)` capture/alias, or a `callable()`-guarded
orphan. Every OTHER call shape -- a function-LOCAL alias (`_x = _helper; _x()`), a
callback invoked through a function PARAMETER, a call through a module-level dict
dispatch table, an attribute call, a subscript call -- produced NO EDGE AND NO
DIAGNOSTIC. The review's probes X1-X4 proved the concrete consequence: a real
`datetime.now()` fallback helper, reachable from ACTIVE `_panel_header` def#4 through a
two-line local alias, was reported GREEN, with `unresolved_reachable_edges == []`,
identically to genuinely dead code.

Round 4 replaces the whole call-resolution layer. The core rule is now:

    EVERY `Call` AST node inside a definition instance already confirmed reachable is
    classified into exactly one of nine categories -- RESOLVED_CALL, RESOLVED_BUILTIN,
    RESOLVED_IMPORT (all three "safe", i.e. the gate is allowed to treat them as
    accounted for), or UNRESOLVED_LOCAL_ALIAS, UNRESOLVED_PARAMETER_CALLBACK,
    UNRESOLVED_ATTRIBUTE_CALL, UNRESOLVED_SUBSCRIPT_CALL, UNRESOLVED_DYNAMIC_CALL,
    AMBIGUOUS_CALL (all six of which fail the WHOLE GATE when the call sits inside a
    confirmed-reachable node). No call may silently produce no edge and no diagnostic.
    A definition instance may be classified DEAD_UNREACHABLE only when deadness is
    POSITIVELY proven; an unresolved/ambiguous call sourced from a reachable node makes
    the whole gate RED regardless of what that call might have targeted.

This closes the R3-review's F-2b findings:

  1. **Function-local aliases** (`_x = _helper` then `_x()`, including 2+-hop chains,
     aliases assigned inside `if`/`try` branches, and aliases rebound between different
     targets) are tracked with a genuine, per-function, flow-sensitive dataflow
     (`analyze_block()`): a forward pass over the function body that threads a local
     binding environment through straight-line code and MERGES it at every `if`/`try`/
     `for`/`while` branch point -- two branches that bind the same name to the SAME
     resolved target merge cleanly; two branches that disagree, or a branch that binds a
     name the other does not, merge to `AMBIGUOUS_CALL`. Reassignment strictly earlier in
     a shared block always overrides an EARLIER assignment (last-textual-assignment-wins,
     matching real Python execution) rather than merging.
  2. **Function-parameter callbacks** (`def _wrapper(cb): cb()`) are resolved only when
     EVERY direct callsite of `_wrapper()` found anywhere in the file supplies the SAME,
     staticaly-resolvable argument for that parameter (transitively, through a chain of
     wrapper parameters up to `PARAM_PROOF_DEPTH` hops, so "callback passed through two
     wrapper levels" resolves correctly) -- otherwise `UNRESOLVED_PARAMETER_CALLBACK`.
  3. **Attribute calls** (`obj.method()`) resolve to `RESOLVED_IMPORT` when the base
     chain bottoms out at a name bound by a module-level or function-local `import`
     statement (so `os.path.exists(...)`, `datetime.now()`, `subprocess.run(...)` etc.
     remain green and are not falsely flagged); otherwise, since NO attribute name
     appearing on any actually-reachable call in this file collides with the name of any
     module-level definition (audited fact, see report), an attribute call whose method
     name matches NO module-level def anywhere in the file is `RESOLVED_BUILTIN`
     ("this cannot be invoking one of our own tracked functions, because none of our
     functions has this name") -- an attribute call whose method name DOES collide with a
     real def name is `UNRESOLVED_ATTRIBUTE_CALL` (fail closed on the one shape that could
     plausibly be reaching one of our own helpers dynamically).
  4. **Subscript calls** (`table[key]()`) resolve only when the key is a literal AND the
     mapping is a single, provably-unmutated dict literal (module-level or function-local)
     whose value at that key is itself resolvable; otherwise `UNRESOLVED_SUBSCRIPT_CALL`.
  5. **`getattr(...)()` and any call whose own `func` is itself a `Call`** (a call
     result invoked immediately) is unconditionally `UNRESOLVED_DYNAMIC_CALL`.
  6. **Alias cycles**, whether purely local or routed through a module-level capture/
     alias cycle, resolve to `AMBIGUOUS_CALL` (reusing the module-level cycle-safe
     `resolve_reference()`, never a new re-implementation of cycle detection).
  7. A per-node and file-wide **call inventory** is generated independently of the
     classifier's own bookkeeping (`sum(1 for n in ast.walk(node) if isinstance(n,
     ast.Call))` against the classifier's own count) so a future regression in the new
     traversal that silently drops a `Call` node -- the exact shape of the R2-round
     `M32R2-6` matcher regression -- fails the gate closed via `omitted_call_total != 0`
     rather than silently returning a clean report.

What this proves, on the LIVE, ACTIVE, reachable code (not a substring grep):

  1. Robust `globals().get` capture detection, including non-literal keys and fallbacks
     (unchanged from round 3).
  2. A fail-closed reachability graph over sync and async definitions, with module-level
     alias chains resolved and cycles detected (unchanged from round 3).
  3. A fail-closed CALL classification over every single `Call` AST node inside a
     reachable node -- function-local aliases, parameter callbacks, attribute calls,
     subscript calls and dynamic-reflection calls are all resolved or explicitly failed
     closed; none are silently dropped (NEW in round 4).
  4. Structural completeness self-checks (round 3's `def`/capture regex cross-checks,
     PLUS a new independent Call-node count cross-check) that fail closed if any
     collector regresses.

    python tools\\w3_2_panel_static_gate_selftest.py
"""
from __future__ import annotations

import ast
import builtins
import re
import sys
from collections import deque
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PANEL_BOT_PATH = BASE_DIR / "panel_bot.py"

# The frozen literal set of TZ-3 enclosing function names (02_S9_TIMEZONE_CONTRACT.md
# 2.2's TZ-3 row). This is the ASSERTION TARGET the gate reports against -- root
# discovery itself is derived from real call-site evidence (build_reachability_graph),
# not from this tuple; see the module docstring, point 5.
TZ3_FUNCTIONS = ("_panel_header", "_today_iso", "_tp_visual_now_local")

# Runaway guard only -- the real panel_bot.py TZ-3 closure converges well under this;
# not a functional limiter.
NODE_BUDGET = 2000

# How many parameter-forwarding hops (wrapper calling wrapper calling wrapper...) the
# parameter-callback prover will chase before giving up and reporting
# UNRESOLVED_PARAMETER_CALLBACK. A runaway/cycle guard only -- real wrapper chains in
# this codebase are one or two hops deep.
PARAM_PROOF_DEPTH = 8

# Sentinel "as of" point for resolving a reference used INSIDE a function body: by the
# time any function is actually called, the entire module has finished executing top
# to bottom, so every module-level capture/alias assignment is already in effect --
# regardless of whether it sits textually before or after the calling function's own
# `def` line. See `outgoing_edges()`'s docstring (round 3) and `resolve_reference()`.
_MODULE_FULLY_LOADED = float("inf")

# The nine required call-classification categories (task spec section 2). Every Call
# AST node inside a reachable definition instance receives exactly one of these.
SAFE_CALL_CATEGORIES = ("RESOLVED_CALL", "RESOLVED_BUILTIN", "RESOLVED_IMPORT")
UNSAFE_CALL_CATEGORIES = (
    "UNRESOLVED_LOCAL_ALIAS", "UNRESOLVED_PARAMETER_CALLBACK",
    "UNRESOLVED_ATTRIBUTE_CALL", "UNRESOLVED_SUBSCRIPT_CALL",
    "UNRESOLVED_DYNAMIC_CALL", "AMBIGUOUS_CALL",
)
ALL_CALL_CATEGORIES = SAFE_CALL_CATEGORIES + UNSAFE_CALL_CATEGORIES

# ROUND 5: categories that prove a VALUE/RECEIVER is genuinely external/builtin --
# deliberately NOT the same set as `SAFE_CALL_CATEGORIES`. `RESOLVED_CALL` means "this
# is a reference to a KNOWN PROJECT FUNCTION" -- that identifies WHAT it is, never
# that it is safe to treat its attributes/boolop-operand-role as harmless (an
# adversarial probe, S5-E in the R5 probe suite, confirmed that including
# `RESOLVED_CALL` here lets a parameter whose reachable callsite passes a project
# helper DIRECTLY be waved through as a "safe" receiver/value). `LOCAL_DICT_LITERAL`
# (a plain dict literal -- never assigned to a real Call node; `classify_call_func`
# converts a bare dict object called directly into `UNRESOLVED_LOCAL_ALIAS`/
# `DICT_OBJECT_CALLED_DIRECTLY`) IS added: a plain dict object cannot carry a
# project-assigned attribute. Every consumer that asks "is this VALUE safe"
# (BoolOp/BinOp/IfExp operands, an attribute receiver, a for-loop/with/comprehension
# binding target, a branch merge) checks membership here, never `SAFE_CALL_CATEGORIES`
# (reserved for "is this a safe CALLABLE TARGET", where `RESOLVED_CALL` correctly
# belongs since it names something to enqueue and clock-scan).
_VALUE_SAFE_CATEGORIES = ("RESOLVED_BUILTIN", "RESOLVED_IMPORT", "LOCAL_DICT_LITERAL")

BUILTIN_NAMES = frozenset(dir(builtins))

# Method names on the project's own dict/list mutation idiom -- used only to detect
# "this dict literal is provably NOT immutable in the analyzed scope" for subscript-call
# resolution (task spec section 5: "immutable in the analyzed scope").
_DICT_MUTATION_METHODS = frozenset(
    {"update", "pop", "popitem", "clear", "setdefault", "__setitem__", "__delitem__"})

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
BRANCH_TYPES = (ast.If, ast.Try, ast.For, ast.AsyncFor, ast.While)
WITH_TYPES = (ast.With, ast.AsyncWith)


# ======================================================================
# SECTION 2 -- robust globals().get(...) capture detection, incl. non-literal
# keys and fallbacks (round 3, unchanged)
# ======================================================================

def is_globals_get_call(call: ast.AST) -> bool:
    """True iff `call` is exactly `globals().get(...)` -- i.e. an attribute call
    `.get` on the ZERO-ARGUMENT result of calling the builtin name `globals`. This is
    the actual AST shape (a Call wrapping a Call), not a bare Name -- the round-1
    matcher's bug (fixed in round 2, retained here). Deliberately does NOT match
    `some_dict.get(...)` (the base must itself be a `globals()` call) or
    `obj.globals().get(...)` (the base call's func must be the bare Name `globals`)."""
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "get"
        and isinstance(call.func.value, ast.Call)
        and isinstance(call.func.value.func, ast.Name)
        and call.func.value.func.id == "globals"
        and not call.func.value.args
        and not call.func.value.keywords
    )


def find_globals_get_captures(tree: ast.Module) -> list[dict]:
    """Every module-level `X = globals().get(...)` capture, in source order. For each
    capture, records: the assignment target, the captured key (resolved to a literal
    string when possible, else flagged non-literal), whether the key is dynamic, any
    fallback expression and whether IT is dynamic, and the exact source span. A
    non-literal key is never silently dropped -- `key_is_literal=False` captures still
    appear in the returned list and participate in reference resolution as
    `UNRESOLVED_DYNAMIC` (section 3: "do not silently ignore the capture")."""
    out = []
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        call = node.value
        if not is_globals_get_call(call):
            continue
        key_node = call.args[0] if call.args else None
        key_literal = (key_node.value if isinstance(key_node, ast.Constant)
                       and isinstance(key_node.value, str) else None)
        fallback_node = call.args[1] if len(call.args) > 1 else None
        fallback_code = ast.unparse(fallback_node) if fallback_node is not None else None
        fallback_is_dynamic = fallback_node is not None and not isinstance(fallback_node, ast.Constant)
        out.append({
            "target": node.targets[0].id,
            "captured_name": key_literal,
            "key_is_literal": key_literal is not None,
            "key_code": ast.unparse(key_node) if key_node is not None else None,
            "fallback_code": fallback_code,
            "fallback_is_dynamic": fallback_is_dynamic,
            "lineno": node.lineno,
            "end_lineno": node.end_lineno,
            "col_offset": node.col_offset,
        })
    return out


# Backward-compat alias -- delegates to the fixed matcher, so any external caller
# (e.g. an older mutation-proof revision) that still imports `find_capture_chain` gets
# the corrected behavior rather than a silent no-op.
def find_capture_chain(tree: ast.Module, name: str) -> list[dict]:
    return [c for c in find_globals_get_captures(tree) if c["captured_name"] == name]


def find_all_globals_get_calls(tree: ast.Module) -> list[dict]:
    """Every `globals().get(...)` call ANYWHERE in the file -- a module-level simple
    assignment (what `find_globals_get_captures` tracks and what feeds reachability),
    a module-level call embedded in a larger expression, or a call nested inside a
    function body. Used ONLY for the report-consistency totals: `all_globals_get_total`
    / `module_level_globals_get_total` / `nested_globals_get_total` must be three
    DISTINCT, additively-consistent fields, never one number silently standing in for
    another. Never used for reachability itself -- captures outside the module-level
    simple-assignment shape are deliberately out of that model (see
    `find_globals_get_captures`'s docstring)."""
    return [{"lineno": n.lineno, "col_offset": n.col_offset}
            for n in ast.walk(tree) if is_globals_get_call(n)]


def find_alias_assignments(tree: ast.Module) -> list[dict]:
    """Every module-level `X = Y` where Y is a bare Name. `Y` may itself be a plain def
    name, a capture variable, or another alias -- all three are resolved transitively by
    `resolve_reference()`."""
    out = []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Name)):
            out.append({
                "target": node.targets[0].id,
                "source_name": node.value.id,
                "lineno": node.lineno,
                "end_lineno": node.end_lineno,
                "col_offset": node.col_offset,
            })
    return out


def build_reference_index(captures: list[dict], aliases: list[dict]) -> dict:
    """name -> [record, ...] sorted by lineno, for every module-level `X = globals().get
    (...)` capture and `X = Y` alias assignment. A name may be (re)assigned more than
    once; `latest_ref_before()` always picks the assignment in effect at a given call
    site's line, so a LATER reassignment never changes what an EARLIER call resolved to."""
    idx: dict[str, list[dict]] = {}
    for c in captures:
        idx.setdefault(c["target"], []).append({"kind": "capture", **c})
    for a in aliases:
        idx.setdefault(a["target"], []).append({"kind": "alias", **a})
    for name in idx:
        idx[name].sort(key=lambda r: r["lineno"])
    return idx


def latest_ref_before(ref_index: dict, name: str, at_lineno: int):
    recs = ref_index.get(name)
    if not recs:
        return None
    cand = [r for r in recs if r["lineno"] < at_lineno]
    return cand[-1] if cand else None


# ======================================================================
# SECTION 3 -- unified MODULE-LEVEL reference resolution (captures + aliases),
# fail-closed (round 3, unchanged). Local-scope resolution (round 4) builds on top of
# this rather than duplicating it.
# ======================================================================
#
# Resolution statuses:
#   RESOLVED            -- target is a specific, known definition instance (or,
#                           for a literal fallback with no prior def, "safely nothing
#                           to call" -- def=None, never propagates an edge).
#   UNRESOLVED_DYNAMIC   -- a non-literal globals().get key, or a non-literal fallback
#                           that would actually be evaluated.
#   UNRESOLVED_ALIAS     -- the referenced name is not a def, not a tracked
#                           capture/alias, or a literal capture key matches no
#                           definition anywhere in the file.
#   AMBIGUOUS            -- a cyclic alias chain (A -> B -> A).

def resolve_reference(name: str, at_lineno: int, ref_index: dict, all_defs: dict,
                       visiting: tuple = ()) -> dict:
    if name in visiting:
        return {"status": "AMBIGUOUS", "diagnostic": "ALIAS_CYCLE", "def": None,
                "chain": list(visiting) + [name]}
    visiting = visiting + (name,)

    rec = latest_ref_before(ref_index, name, at_lineno)
    if rec is None:
        if name in all_defs and all_defs[name]:
            cand = [d for d in all_defs[name] if d.lineno < at_lineno]
            if cand:
                return {"status": "RESOLVED", "def": (cand[-1].name, cand[-1].lineno),
                       "diagnostic": None, "chain": list(visiting)}
            return {"status": "UNRESOLVED_ALIAS", "diagnostic": "NO_PRIOR_DEF",
                   "def": None, "chain": list(visiting)}
        return {"status": "UNRESOLVED_ALIAS", "diagnostic": "UNTRACKED_NAME",
               "def": None, "chain": list(visiting)}

    if rec["kind"] == "capture":
        if not rec["key_is_literal"]:
            return {"status": "UNRESOLVED_DYNAMIC", "diagnostic": "DYNAMIC_GLOBALS_KEY",
                   "def": None, "chain": list(visiting), "span": (rec["lineno"], rec["col_offset"])}
        target_name = rec["captured_name"]
        cand = [d for d in all_defs.get(target_name, []) if d.lineno < rec["lineno"]]
        if cand:
            return {"status": "RESOLVED", "def": (cand[-1].name, cand[-1].lineno),
                   "diagnostic": None, "chain": list(visiting)}
        if rec["fallback_code"] is not None:
            if rec["fallback_is_dynamic"]:
                return {"status": "UNRESOLVED_DYNAMIC", "diagnostic": "DYNAMIC_GLOBALS_FALLBACK",
                       "def": None, "chain": list(visiting), "span": (rec["lineno"], rec["col_offset"])}
            return {"status": "RESOLVED", "def": None, "diagnostic": "LITERAL_FALLBACK_NO_TARGET",
                   "chain": list(visiting)}
        return {"status": "UNRESOLVED_ALIAS", "diagnostic": "UNRESOLVED_CAPTURE_TARGET",
               "def": None, "chain": list(visiting), "span": (rec["lineno"], rec["col_offset"])}

    # alias -- recurse on the source name, evaluated as of THIS assignment's own line
    return resolve_reference(rec["source_name"], rec["lineno"], ref_index, all_defs, visiting)


# ======================================================================
# SECTION 4 -- node universe, call sites, roots (sync + async, round 3 unchanged).
# Root discovery (which frozen TZ-3 name is reached from real external evidence) is
# NOT part of the round-4 change -- F-2b is about what TZ-3 code calls INTO, not about
# how TZ-3 functions are themselves invoked.
# ======================================================================

def build_parent_map(tree: ast.Module) -> dict:
    parent = {}
    for n in ast.walk(tree):
        for ch in ast.iter_child_nodes(n):
            parent[id(ch)] = n
    return parent


def collect_all_module_defs(tree: ast.Module) -> dict:
    """name -> [FunctionDef|AsyncFunctionDef, ...] for EVERY module-level definition,
    sync and async both."""
    out: dict[str, list] = {}
    for n in tree.body:
        if isinstance(n, DEF_TYPES):
            out.setdefault(n.name, []).append(n)
    return out


def _outermost_container(node: ast.AST, parent_map: dict):
    """Nearest enclosing module-level FunctionDef/AsyncFunctionDef, or None if `node`
    is a module-level statement itself."""
    cur = None
    p = parent_map.get(id(node))
    while p is not None:
        if isinstance(p, DEF_TYPES):
            cur = p
        p = parent_map.get(id(p))
    return cur


def collect_call_sites(tree: ast.Module, parent_map: dict) -> list[dict]:
    """(callee, container_name_or_None, container_lineno, call_lineno) for every
    Call(Name(...)) in the file -- inside any top-level def (sync OR async) and at
    true module level (container=None), so a direct module-level call counts as
    external-root evidence too."""
    sites = []
    for container in tree.body:
        if not isinstance(container, DEF_TYPES):
            continue
        for n in ast.walk(container):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                sites.append({"callee": n.func.id, "container": container.name,
                             "container_lineno": container.lineno, "call_lineno": n.lineno,
                             "container_is_async": isinstance(container, ast.AsyncFunctionDef)})
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            if _outermost_container(n, parent_map) is None:
                sites.append({"callee": n.func.id, "container": None, "container_lineno": None,
                             "call_lineno": n.lineno, "container_is_async": False})
    return sites


def _is_callable_guard_context(name_node: ast.Name, parent_map: dict) -> bool:
    """True iff `name_node` is the sole argument of `callable(name_node)` -- the
    project's standard "is this captured chain link populated" guard idiom. Testing
    callability is not invoking the target, so it is excluded from the escaping-
    reference scan below."""
    p = parent_map.get(id(name_node))
    return (isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == "callable"
            and len(p.args) == 1 and p.args[0] is name_node)


def _is_call_func_position(name_node: ast.Name, parent_map: dict) -> bool:
    p = parent_map.get(id(name_node))
    return isinstance(p, ast.Call) and p.func is name_node


def _callable_guarded_names(tree: ast.Module) -> set:
    """Every name X appearing as the sole argument of `callable(X)` anywhere in the
    file -- the project's own documented capture-chain guard idiom. Cross-referenced
    against ref_index/all_defs to detect a capture/alias assignment that was removed
    out from under a guard that still references it."""
    names = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "callable"
                and len(n.args) == 1 and isinstance(n.args[0], ast.Name)):
            names.add(n.args[0].id)
    return names


def _tz3_relevant_escape_watch(ref_index: dict, all_defs: dict, tz3_names) -> set:
    """Names (capture/alias variables) whose resolution chain terminates -- successfully
    or not -- at one of the frozen TZ-3 names. Scoping the bare-reference escape scan to
    just this set keeps it targeted at the defect class it exists to catch, instead of
    flagging every incidental reference to any of the file's ~870 other functions."""
    watch = set()
    for name, recs in ref_index.items():
        for rec in recs:
            res = resolve_reference(name, rec["lineno"] + 1, ref_index, all_defs)
            if res["status"] == "RESOLVED" and res["def"] is not None and res["def"][0] in tz3_names:
                watch.add(name)
            elif res["status"] != "RESOLVED":
                if rec["kind"] == "capture" and not rec.get("key_is_literal", True):
                    watch.add(name)
                elif rec["kind"] == "alias":
                    watch.add(name)
    return watch


# ======================================================================
# SECTION 5 -- ROUND 4: module-level import bindings + dict-literal registry
# ======================================================================

def collect_import_bindings(tree: ast.Module) -> tuple:
    """Every name bound by a module-level `import X [as Y]` (-> `module_names`, the
    module/package alias itself) or `from X import Y [as Z]` (-> `from_names`, the
    imported symbol's local name), scanned across THE WHOLE FILE (imports are not
    restricted to module level in Python -- a local `import` inside a function is
    equally a real, safe binding) so a call through either kind of import binding,
    wherever it is declared, is classified RESOLVED_IMPORT rather than falling through
    to unresolved."""
    module_names: set = set()
    from_names: set = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name == "*":
                    continue
                from_names.add(a.asname or a.name)
    return module_names, from_names


def collect_from_import_source_modules(tree: ast.Module) -> dict:
    """ROUND 6: {local_name: source_module_name} for every `from <module> import
    Y [as Z]` in this file (module-level or local, matching `collect_import_bindings`'s
    own scope) -- used exclusively by `_external_def_element_family` to find a NAMED
    external function's OWN source file for a bounded, auditable cross-file
    return-annotation lookup (task section 3.G: "proven external APIs only when the
    concrete live source pattern demonstrates the element shape"). `from . import x`
    / `from .. import x` (relative, `n.module is None`) are skipped -- out of scope,
    not used anywhere in this file."""
    out: dict = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            for a in n.names:
                if a.name != "*":
                    out[a.asname or a.name] = n.module
    return out


def collect_json_decode_bindings(tree: ast.Module) -> tuple:
    """ROUND 6: the local name(s) this file's own `import json [as X]` binds (for
    `<name>.loads`/`.load` attribute calls), and the local name(s) a
    `from json import loads/load [as Y]` binds directly -- used exclusively by
    `_is_json_decode_call` (task section 3.G) to prove an ELEMENT shape for a
    json-decoded value. Scoped to literally the `json` module, nothing else."""
    module_names: set = set()
    from_names: set = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "json":
                    module_names.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module == "json":
            for a in n.names:
                if a.name in ("loads", "load"):
                    from_names.add(a.asname or a.name)
    return module_names, from_names


def _dict_literal_key_map(dict_node: ast.Dict) -> dict | None:
    """For an `ast.Dict` literal, return {literal_key: value_node} iff EVERY key is a
    literal Constant (never for a non-literal key -- section 5: "literal-key mapping
    call may resolve only if the mapping assignment is statically known"). Returns None
    if any key is non-literal, a `**spread`, or duplicated (ambiguous which value wins)."""
    out: dict = {}
    for k, v in zip(dict_node.keys, dict_node.values):
        if k is None or not isinstance(k, ast.Constant):
            return None
        if k.value in out:
            return None
        out[k.value] = v
    return out


def _name_is_mutated_anywhere(tree: ast.Module, name: str) -> bool:
    """True iff `name` is EVER used as a subscript-assignment target (`name[...] = `),
    ever receives a known dict-mutation method call (`name.update(...)`, etc.), or is
    assigned more than the one time already accounted for by the caller -- used to
    decide whether a dict literal is "immutable in the analyzed scope" (section 5).
    A cheap, whole-file, conservative check: any positive match means "not provably
    immutable", which always fails the mapping resolution closed, never open."""
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name)
                        and t.value.id == name):
                    return True
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == name
                and n.func.attr in _DICT_MUTATION_METHODS):
            return True
    return False


def collect_module_dict_literals(tree: ast.Module) -> dict:
    """name -> {"key_map": {literal_key: value_node}, "immutable": bool} for every
    module-level `NAME = {...}` OR ANNOTATED `NAME: T = {...}` dict-literal assignment
    with ALL-literal keys (ROUND 5: `AnnAssign` added -- `_N53_SERVICE_SCAN_CACHE:
    Dict[str, Any] = {...}` is exactly this shape and was previously invisible here). A
    name assigned a dict literal MORE than once at module level, or ever mutated in
    place anywhere in the file, is marked not immutable -- subscript resolution against
    it always fails closed to UNRESOLVED_SUBSCRIPT_CALL regardless of key match."""
    assigns: dict[str, list] = {}
    for st in tree.body:
        if (isinstance(st, ast.Assign) and len(st.targets) == 1
                and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Dict)):
            assigns.setdefault(st.targets[0].id, []).append(st.value)
        elif (isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name)
              and isinstance(st.value, ast.Dict)):
            assigns.setdefault(st.target.id, []).append(st.value)
    out = {}
    for name, dicts in assigns.items():
        multiple = len(dicts) > 1
        mutated = _name_is_mutated_anywhere(tree, name)
        key_map = _dict_literal_key_map(dicts[-1]) if dicts else None
        # ROUND 6: retain the raw node for element-provenance purposes (see the
        # analogous comment on the local LOCAL_DICT_LITERAL branch in
        # classify_value_expr()) -- "immutable" still gates SUBSCRIPT resolution only;
        # direct iteration / .keys()/.values()/.items() classify every key/value
        # expression independently and do not depend on immutability at all.
        out[name] = {"key_map": key_map or {}, "immutable": (not multiple) and (not mutated)
                     and key_map is not None, "node": dicts[-1] if dicts else None}
    return out


# ======================================================================
# SECTION 5b -- ROUND 5: declared-type receiver provenance + module-level
# attribute-assignment / value-binding tracking
# ======================================================================
#
# W3.2 CORRECTION ROUND 5 (2026-07-31): an independent final review of round 4
# (C:\ALM_TPilot_AUDIT\20260730\W3_2_CORRECTION_R4_REVIEW\, verdict FAIL) found the
# LAST residual F-2 shape: `classify_attribute_expr()`'s fallback branch treated ANY
# attribute call as safe whenever the attribute's NAME did not collide with a
# module-level def name -- "safe because the name doesn't match a def" is exactly the
# unsound basis the round-4 task spec (and this round's) explicitly forbid. The review
# proved it fail-open with `_RV_OBJ.tick = _rv_bad_helper; _RV_OBJ.tick()`: `tick`
# collides with nothing, so the call was reported RESOLVED_BUILTIN with no edge and no
# diagnostic, identically to a genuinely safe call.
#
# Round 5 deletes that fallback branch entirely. An attribute call is now safe ONLY
# when ONE of these holds, each requiring concrete, checkable evidence -- never the
# attribute's spelling:
#
#   1. An explicit assignment into THIS EXACT (receiver, attribute) pair is tracked
#      (function-local, flow-sensitively, via the SAME env/`_merge_envs` machinery as
#      local Name aliases; or module-level, via `build_module_level_provenance()`
#      below) -- the call is then classified EXACTLY as if it were a call to whatever
#      was assigned (a local helper resolves to RESOLVED_CALL and is enqueued into the
#      SAME reachability BFS and clock-scan as a direct call). This is priority 1,
#      checked before anything else, so a monkey-patched import or proven container
#      still correctly turns RED.
#   2. The base chain is a proven import binding (round 3/4, unchanged).
#   3. The attribute name collides with a real module-level def name -- unconditional
#      RED, kept as defense-in-depth (round 3/4, unchanged).
#   4. The RECEIVER expression itself is provably one of a bounded, auditable set of
#      external/builtin/container families ACTUALLY used in this closure: a literal: a
#      builtin-constructor call (`str(...)`, `dict(...)`, `list(...)`, ...); the direct
#      result of a call already proven safe (import or builtin); the result of a call
#      to a project function whose OWN declared return-type annotation names one of
#      these families (`-> Dict[str, Any]`, `-> "list | None"`, `-> sqlite3.Connection`,
#      ...); a function PARAMETER whose OWN declared annotation names one of these
#      families; a function parameter with NO annotation, proven ONLY by tracing every
#      REACHABLE callsite's actual (already flow-analyzed) argument value -- never a
#      context-free guess, and NEVER expanded into dead/unrelated project code
#      (`resolve_parameter_receiver_binding()` below); or a for-loop/`with` variable
#      bound from an expression itself proven under this same chain. Anything else is
#      UNKNOWN_RECEIVER -- `UNRESOLVED_ATTRIBUTE_CALL`, RED.
#
# `attribute_calls_justified_by_name_only_total` (report 03) is asserted `== 0`: no
# code path in this file may return a safe classification whose ONLY supporting
# evidence is "the name doesn't collide with something else".

_FAMILY_ALIASES = {
    "dict": "dict", "Dict": "dict", "list": "list", "List": "list",
    "str": "str", "int": "int", "float": "float", "bool": "bool", "bytes": "bytes",
    "Path": "Path",
}
# Dotted annotations recognized by their fully-unparsed form -- a small, curated,
# auditable set (not an attempt at general type inference). Each entry corresponds to
# a real stdlib type actually returned by a function in this closure (see report 03).
_SAFE_ANNOTATION_DOTTED = {"sqlite3.Connection", "datetime.datetime", "pathlib.Path"}


def _annotation_safe_family(ann) -> str | None:
    """Resolve a PEP-484-style annotation AST node (bare, string-quoted forward-ref,
    `Dict[...]`/`List[...]`/`Optional[...]` subscript, or a `X | None` PEP-604 union)
    to one of a small, curated set of external/builtin family names, or `None` if it
    names anything else. Never returns a family for an annotation that names (or
    could plausibly name) a project-local class -- the curated dotted/simple sets
    only ever match genuine stdlib/builtin names."""
    if ann is None:
        return None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            parsed = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
        return _annotation_safe_family(parsed)
    if isinstance(ann, ast.Constant):
        return None  # e.g. the literal `None` in a union -- not itself a family
    if isinstance(ann, ast.Name):
        return _FAMILY_ALIASES.get(ann.id)
    if isinstance(ann, ast.Attribute):
        chain = ast.unparse(ann)
        return chain if chain in _SAFE_ANNOTATION_DOTTED else None
    if isinstance(ann, ast.Subscript):
        base = ann.value
        base_name = (base.id if isinstance(base, ast.Name)
                    else ast.unparse(base) if isinstance(base, ast.Attribute) else None)
        if base_name in ("Dict",):
            return "dict"
        if base_name in ("List",):
            return "list"
        if base_name == "Optional":
            return _annotation_safe_family(ann.slice)
        return None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _annotation_safe_family(ann.left) or _annotation_safe_family(ann.right)
    return None


def _annotation_element_family(ann) -> str | None:
    """ROUND 6: resolve a `List[X]`/`list[X]` (bare, string-quoted, or wrapped in
    `Optional[...]`/`X | None`) annotation to X's OWN safe family via
    `_annotation_safe_family` -- the ELEMENT shape, not the list's own family. Returns
    `None` for a bare `list`/`List` with no argument, or for anything that is not
    (optionally-wrapped) a `List`/`list` subscript -- a bare "list" annotation carries
    no element information and must never be guessed. This is Form G ("a project
    function's OWN declared return-type annotation naming a safe family") applied one
    level deeper: `_manager_rows() -> List[dict]` proves its ELEMENTS are dict-like,
    which is a materially different (and, for iteration, the actually needed) claim
    from `_annotation_safe_family` proving the LIST ITSELF is a safe value."""
    if ann is None:
        return None
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        try:
            parsed = ast.parse(ann.value, mode="eval").body
        except SyntaxError:
            return None
        return _annotation_element_family(parsed)
    if isinstance(ann, ast.Subscript):
        base = ann.value
        base_name = (base.id if isinstance(base, ast.Name)
                    else ast.unparse(base) if isinstance(base, ast.Attribute) else None)
        if base_name in ("List", "list"):
            return _annotation_safe_family(ann.slice)
        if base_name == "Optional":
            return _annotation_element_family(ann.slice)
        return None
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return _annotation_element_family(ann.left) or _annotation_element_family(ann.right)
    return None


def build_module_level_provenance(tree: ast.Module, mctx: dict) -> None:
    """Single forward pass over MODULE-LEVEL statements only, in textual order (real
    Python module-execution order -- NOT the "module fully loaded" sentinel used for
    captures/aliases, an unrelated, separately-proven mechanism this does not touch),
    populating two indexes IN PLACE on the already-present (empty) dicts
    `mctx["module_attr_assignments"]` and `mctx["module_value_bindings"]` so a later
    module-level statement can see the classification of an earlier one:

      * `module_attr_assignments[(base_name, attr)]` -- every module-level
        `X.attr = value` (single target, `X` a bare Name), classified via
        `classify_value_expr()`. This is priority 1 for `classify_attribute_expr()`.
      * `module_value_bindings[name]` -- every module-level `X = value` NOT already
        covered by `find_globals_get_captures()`/`find_alias_assignments()` (i.e. the
        RHS is neither `globals().get(...)` nor a bare Name) -- covers
        `STATUS_FILE = _abs_path(WATCHDOG_STATUS_FILE)`-style bindings so
        `STATUS_FILE.exists()` resolves through `_abs_path`'s own `-> Path` return
        annotation rather than falling through unresolved."""
    attr_assignments = mctx["module_attr_assignments"]
    value_bindings = mctx["module_value_bindings"]
    for stmt in tree.body:
        if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and stmt.value is not None):
            continue
        target = stmt.targets[0]
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            attr_assignments[(target.value.id, target.attr)] = classify_value_expr(
                stmt.value, {}, mctx)
        elif (isinstance(target, ast.Name) and not is_globals_get_call(stmt.value)
              and not isinstance(stmt.value, ast.Name)):
            value_bindings[target.id] = classify_value_expr(stmt.value, {}, mctx)


# ======================================================================
# SECTION 6 -- ROUND 4: unified name/value/call classification
# ======================================================================
#
# Every classification result is a dict:
#   {"category": one of ALL_CALL_CATEGORIES, "target": (name, lineno) or None,
#    "diagnostic": short code or None, "resolved_via": human-readable trail}
#
# `target` is populated ONLY for RESOLVED_CALL (a specific module-level definition
# instance) -- RESOLVED_BUILTIN/RESOLVED_IMPORT carry target=None (nothing in the
# def universe to enqueue; they are safe precisely because they cannot be one of our
# own tracked functions).

def _mk(category: str, target=None, diagnostic=None, via=None) -> dict:
    return {"category": category, "target": target, "diagnostic": diagnostic, "resolved_via": via}


def resolve_name_reference(name: str, env: dict, mctx: dict) -> dict:
    """Resolve a bare Name reference (as a call callee, or as the RHS of a local alias
    assignment) against, in priority order: (1) the CURRENT local environment (a
    parameter or an already-classified local alias -- local scope always shadows module
    scope, matching real Python name resolution), (2) a module-level
    `globals().get(...)` capture or `X = Y` alias (delegates to the existing, cycle-safe
    `resolve_reference()`), (3) a direct module-level definition, (4) a name referenced
    only through the project's `callable(X)` guard idiom with no backing definition
    anywhere (a deleted capture), (5) a Python builtin, (6) a module or from-import
    binding, (7) otherwise: a genuinely untracked name -- UNRESOLVED_DYNAMIC_CALL."""
    if name in env:
        return env[name]
    if name in mctx["ref_index"]:
        res = resolve_reference(name, _MODULE_FULLY_LOADED, mctx["ref_index"], mctx["all_defs"])
        if res["status"] == "RESOLVED":
            return _mk("RESOLVED_CALL", res["def"], res["diagnostic"], "module_capture_or_alias")
        if res["status"] == "AMBIGUOUS":
            return _mk("AMBIGUOUS_CALL", None, res["diagnostic"], "module_alias_cycle")
        return _mk("UNRESOLVED_DYNAMIC_CALL", None, res["diagnostic"], "module_capture_unresolved")
    if name in mctx["all_defs"] and mctx["all_defs"][name]:
        target = mctx["active_binding"][name]
        return _mk("RESOLVED_CALL", (target.name, target.lineno), None, "direct_module_def")
    if name in mctx["undefined_guarded"]:
        return _mk("UNRESOLVED_DYNAMIC_CALL", None, "CAPTURE_VARIABLE_UNDEFINED", "deleted_capture_guard")
    if name in BUILTIN_NAMES:
        return _mk("RESOLVED_BUILTIN", None, None, "builtin")
    if name in mctx["imported_module_names"] or name in mctx["imported_from_names"]:
        return _mk("RESOLVED_IMPORT", None, None, "import_binding")
    if name in mctx["module_dict_literals"]:
        # ROUND 5: a module-level dict-literal cache (e.g.
        # `_N53_SERVICE_SCAN_CACHE: Dict[str, Any] = {...}`) referenced by bare name --
        # a genuinely safe dict receiver regardless of mutation-in-place elsewhere
        # (mutation only matters for SUBSCRIPT key resolution, not for "is this a
        # dict", which is unconditionally true for a name only ever assigned a Dict
        # literal at module level).
        return _mk("RESOLVED_BUILTIN", None, "PROVEN_MODULE_DICT_LITERAL",
                  f"{name} is a module-level dict-literal cache")
    if name in mctx.get("module_value_bindings", {}):
        return mctx["module_value_bindings"][name]
    return _mk("UNRESOLVED_DYNAMIC_CALL", None, "UNTRACKED_NAME", "unknown_name")


def _attribute_base_chain_is_import(expr: ast.AST, env: dict, mctx: dict) -> bool:
    """True iff `expr` is a Name, or a chain of Attribute accesses bottoming out at a
    Name, whose root name resolves (checking local shadowing first) to a known import
    binding -- covers `os.path.exists(...)`, `urllib.request.urlopen(...)`,
    `datetime.now()` (datetime bound via `from datetime import datetime`), etc."""
    if isinstance(expr, ast.Name):
        if expr.id in env:
            return env[expr.id]["category"] in ("RESOLVED_IMPORT",)
        return (expr.id in mctx["imported_module_names"]
                or expr.id in mctx["imported_from_names"])
    if isinstance(expr, ast.Attribute):
        return _attribute_base_chain_is_import(expr.value, env, mctx)
    return False


def _is_json_decode_call(func_node: ast.AST, env: dict, mctx: dict) -> bool:
    """ROUND 6: True iff `func_node` is exactly `<json_alias>.loads`/`.load` (the
    module bound via THIS FILE's own `import json [as X]`) or a bare `loads`/`load`
    name bound via `from json import loads`/`load` -- the one concrete, auditable,
    live source pattern (task section 3.G) this round trusts to establish an ELEMENT
    shape without a declared annotation: JSON decoding is closed under {dict, list,
    str, int, float, bool, None} by the JSON grammar itself, so an element pulled out
    of a `json.loads()`/`json.load()` result can never structurally be a project-local
    callable, regardless of what the decoded data actually contains. A locally
    shadowed name (e.g. a parameter or alias also called `loads`) is never trusted."""
    if isinstance(func_node, ast.Attribute) and func_node.attr in ("loads", "load"):
        base = func_node.value
        return (isinstance(base, ast.Name) and base.id not in env
                and base.id in mctx.get("json_module_aliases", ()))
    if isinstance(func_node, ast.Name):
        return func_node.id not in env and func_node.id in mctx.get("json_from_names", ())
    return False


def classify_attribute_expr(node: ast.Attribute, env: dict, mctx: dict) -> dict:
    """Classify an `Attribute` expression used either as a CALL's `func` (`obj.method()`)
    or as the RHS VALUE of a local alias assignment (`x = obj.method`).

    ROUND 5 (closes the R4-review's residual F-2 finding): safety is NEVER decided by
    the attribute's name alone. In priority order:

      1. An explicit assignment into this EXACT (receiver, attribute) pair is tracked
         (function-local, flow-sensitive `env[("ATTR", base, attr)]`, or module-level
         `mctx["module_attr_assignments"]`) -- resolve to whatever was assigned,
         exactly as if this were a call through that alias directly. Checked FIRST so
         a monkey-patched import or proven container still correctly turns RED.
      2. The base chain is a proven import binding (round 3/4, unchanged) ->
         RESOLVED_IMPORT.
      3. The attribute name collides with a real module-level def name -> unconditional
         UNRESOLVED_ATTRIBUTE_CALL (round 3/4, unchanged; defense-in-depth for the one
         shape that could plausibly be reaching one of our own functions dynamically).
      4. The RECEIVER expression (`node.value`) is itself provably one of a bounded set
         of external/builtin/container families (see section 5b) -> RESOLVED_BUILTIN,
         with the EVIDENCE recorded in `diagnostic`/`resolved_via` (never just "the name
         didn't collide with anything").

    Anything else is UNKNOWN_RECEIVER -> UNRESOLVED_ATTRIBUTE_CALL. There is no
    fallback branch that accepts a call as safe merely because its attribute name does
    not match a local def -- that rule is deleted (see section 5b docstring)."""
    if isinstance(node.value, ast.Name):
        base = node.value.id
        tracked = env.get(("ATTR", base, node.attr))
        if tracked is not None:
            return tracked
        tracked = mctx["module_attr_assignments"].get((base, node.attr))
        if tracked is not None:
            return tracked
    if _attribute_base_chain_is_import(node.value, env, mctx):
        return _mk("RESOLVED_IMPORT", None, None, "attribute_on_import_chain")
    if node.attr in mctx["all_defs"]:
        return _mk("UNRESOLVED_ATTRIBUTE_CALL", None, "ATTR_NAME_COLLIDES_WITH_DEF",
                   f"attribute .{node.attr} matches a real module-level def name")
    base_class = classify_value_expr(node.value, env, mctx)
    if base_class["category"] in _VALUE_SAFE_CATEGORIES:
        evidence = base_class.get("diagnostic") or base_class.get("resolved_via") or "proven safe"
        return _mk("RESOLVED_BUILTIN", None, "ATTRIBUTE_ON_PROVEN_SAFE_RECEIVER",
                  f"receiver proven safe ({evidence}): {ast.unparse(node.value)[:60]}")
    return _mk("UNRESOLVED_ATTRIBUTE_CALL", None, "UNKNOWN_RECEIVER",
              f"receiver {ast.unparse(node.value)[:60]!r} is not proven safe by any "
              f"tracked assignment, import chain, or declared/inferred type")


def _resolve_dict_literal_for_base(base_name: str, env: dict, mctx: dict) -> dict | None:
    """Return {"key_map":..., "immutable":...} for `base_name`, checking the LOCAL
    environment (a function-local dict literal) before the module-level registry --
    local scope shadows module scope."""
    if base_name in env and env[base_name].get("category") == "LOCAL_DICT_LITERAL":
        return env[base_name]["dict_info"]
    return mctx["module_dict_literals"].get(base_name)


def _iterable_dict_node(expr: ast.AST, env: dict, mctx: dict) -> ast.Dict | None:
    """ROUND 6: resolve `expr` to the RAW `ast.Dict` literal node it names, for
    element-provenance purposes (direct dict iteration yields keys; `.keys()`/
    `.values()`/`.items()` need the actual key/value expressions, not just the
    literal-key map `_dict_literal_key_map` builds for SUBSCRIPT resolution, which
    drops non-literal keys entirely). Handles a literal `{...}` used directly, or a
    Name bound (locally or at module level) to one -- local scope shadows module
    scope, matching `_resolve_dict_literal_for_base`."""
    if isinstance(expr, ast.Dict):
        return expr
    if isinstance(expr, ast.Name):
        info = _resolve_dict_literal_for_base(expr.id, env, mctx)
        if info is not None:
            return info.get("node")
    return None


def _dict_node_pure_pairs(dict_node: ast.Dict) -> tuple | None:
    """ROUND 6: (keys, values) AST-node lists for `dict_node`, or `None` if it
    contains ANY `**spread` entry (a `None` key) -- a spread could inject arbitrary,
    unenumerable content, so direct iteration / `.keys()`/`.values()`/`.items()`
    element-provenance is simply not attempted for such a dict, exactly like an
    unresolved shape elsewhere in this file (fail closed, never guess)."""
    if any(k is None for k in dict_node.keys):
        return None
    return list(dict_node.keys), list(dict_node.values)


def classify_subscript_expr(node: ast.Subscript, env: dict, mctx: dict) -> dict:
    """Classify a `Subscript` expression used either as a CALL's `func`
    (`table[key]()`) or as a local alias assignment's RHS VALUE (`x = table[key]`).
    A non-literal key is ALWAYS unresolved (dynamic dispatch, section 5: "non-literal
    Subscript call -> UNRESOLVED_SUBSCRIPT_CALL"), regardless of the base. A literal key
    against a base that is not a provably-immutable, all-literal-key dict literal (local
    or module-level) is also unresolved. Only a literal key found in an immutable dict
    literal's key map resolves -- to whatever the corresponding value itself resolves to."""
    key_node = node.slice
    if not isinstance(key_node, ast.Constant):
        return _mk("UNRESOLVED_SUBSCRIPT_CALL", None, "DYNAMIC_SUBSCRIPT_KEY",
                  "subscript key is not a literal")
    if not isinstance(node.value, ast.Name):
        return _mk("UNRESOLVED_SUBSCRIPT_CALL", None, "NON_NAME_SUBSCRIPT_BASE",
                  "subscript base is not a simple mapping name")
    dict_info = _resolve_dict_literal_for_base(node.value.id, env, mctx)
    if not dict_info or not dict_info.get("immutable"):
        return _mk("UNRESOLVED_SUBSCRIPT_CALL", None, "MAPPING_NOT_PROVABLY_IMMUTABLE",
                  f"{node.value.id} is not a single, unmutated, all-literal-key dict literal")
    key_map = dict_info["key_map"]
    if key_node.value not in key_map:
        return _mk("UNRESOLVED_SUBSCRIPT_CALL", None, "SUBSCRIPT_KEY_NOT_IN_MAP",
                  f"key {key_node.value!r} not found in {node.value.id}'s literal map")
    value_node = key_map[key_node.value]
    return classify_value_expr(value_node, env, mctx)


def classify_value_expr(expr: ast.AST, env: dict, mctx: dict) -> dict:
    """Classify the VALUE bound by a simple local (or module-level) assignment
    `x = <expr>` -- used to populate the local environment, to classify a dict
    literal's value, AND (ROUND 5) as the general RECEIVER-provenance prover consumed
    by `classify_attribute_expr()`'s priority-4 fallback. A Name delegates to
    `resolve_name_reference`; an Attribute/Subscript delegate to their own classifiers
    (which themselves recurse back into this function for THEIR receiver, so an
    arbitrarily long safe chain like `_ti_w3_now().date().isoformat` resolves in one
    pass); a literal container/constant/f-string is unconditionally safe; a
    `BoolOp`/`BinOp` is safe only when EVERY operand independently proves safe (an
    `X or fallback` pattern is safe only if `X` itself is proven -- the literal
    fallback alone never launders an unproven operand); a `Call` delegates to
    `classify_call_result_as_value()`. Anything else (a comprehension, an unhandled
    literal shape, ...) is NOT a proven reference -- `UNRESOLVED_LOCAL_ALIAS` /
    `NON_REFERENCE_VALUE`, so a later call through it fails closed."""
    if isinstance(expr, ast.Name):
        return resolve_name_reference(expr.id, env, mctx)
    if isinstance(expr, ast.Attribute):
        return classify_attribute_expr(expr, env, mctx)
    if isinstance(expr, ast.Subscript):
        return classify_subscript_expr(expr, env, mctx)
    if isinstance(expr, ast.Dict):
        key_map = _dict_literal_key_map(expr)
        return {"category": "LOCAL_DICT_LITERAL", "target": None, "diagnostic": None,
               "resolved_via": "local_dict_literal",
               # ROUND 6: the RAW ast.Dict node is retained (not just the literal-key
               # map, which drops non-literal keys) so classify_iterable_element() can
               # classify EVERY key/value expression individually for direct iteration,
               # .keys()/.values()/.items() -- see _iterable_dict_node().
               "dict_info": {"key_map": key_map or {}, "immutable": key_map is not None,
                            "node": expr}}
    if isinstance(expr, (ast.List, ast.Set, ast.Tuple)):
        result = _mk("RESOLVED_BUILTIN", None, "PROVEN_LITERAL_CONTAINER",
                    f"{type(expr).__name__} literal")
        # ROUND 6 (Form A): ADDITIONALLY compute what iterating THIS literal would
        # yield -- each element classified individually and merged, exactly like
        # classify_iterable_element's own literal-container handling -- so a NAME
        # bound to a literal container (`data = [data]` re-wrapping an
        # already-classified value, or a genuine multi-element literal) carries its
        # element shape forward through the ordinary env lookup. An empty literal
        # gets `_merge_many([])`'s own vacuous-safe sentinel.
        result["element_provenance"] = _merge_many(
            [classify_value_expr(e, env, mctx) for e in expr.elts])
        return result
    if isinstance(expr, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        # ROUND 5: a comprehension/generator expression ALWAYS produces a plain
        # list/set/dict/generator object -- unconditionally true from the Python
        # grammar alone, regardless of what its element expression computes.
        result = _mk("RESOLVED_BUILTIN", None, "PROVEN_COMPREHENSION_RESULT_TYPE",
                    f"{type(expr).__name__} always produces a plain container/generator")
        # ROUND 6: ADDITIONALLY compute what a later iteration of THIS value would
        # yield (Form B) -- the comprehension's own yielded expression (its `elt`, or
        # a DictComp's `key`, matching real Python dict-iteration semantics),
        # evaluated in comprehension scope. Attached here (not just in
        # classify_iterable_element) so a NAME bound to a comprehension's result
        # (`reasons = [str(x) for x in ...]` ... `for r in reasons:`) carries its
        # element shape forward through the ordinary env lookup, with no separate
        # bookkeeping -- never inferred from "the result is a safe container" alone.
        result["element_provenance"] = _comprehension_yield_classification(expr, env, mctx)
        return result
    if isinstance(expr, (ast.Constant, ast.JoinedStr)):
        return _mk("RESOLVED_BUILTIN", None, "PROVEN_LITERAL_VALUE",
                  "literal constant or f-string, no call involved")
    if isinstance(expr, ast.BoolOp):
        parts = [classify_value_expr(v, env, mctx) for v in expr.values]
        if all(p["category"] in _VALUE_SAFE_CATEGORIES for p in parts):
            result = _mk("RESOLVED_BUILTIN", None, "PROVEN_BOOLOP_ALL_OPERANDS_SAFE",
                        "or/and expression, every operand independently proven safe")
            # ROUND 6: propagate element provenance through a `X = X or <fallback>`
            # idiom (e.g. `procs = procs or []`) -- without this, re-binding a value
            # that already carries a proven element shape through a BoolOp would
            # silently drop it, exactly like the literal-container/comprehension gap
            # this round already closes for those two forms.
            result["element_provenance"] = classify_iterable_element(expr, env, mctx)
            return result
        return _mk("UNRESOLVED_LOCAL_ALIAS", None, "BOOLOP_OPERAND_NOT_PROVEN",
                  "or/and expression has at least one operand not proven safe")
    if isinstance(expr, ast.BinOp):
        left = classify_value_expr(expr.left, env, mctx)
        right = classify_value_expr(expr.right, env, mctx)
        if left["category"] in _VALUE_SAFE_CATEGORIES and right["category"] in _VALUE_SAFE_CATEGORIES:
            return _mk("RESOLVED_BUILTIN", None, "PROVEN_BINOP_BOTH_OPERANDS_SAFE",
                      "arithmetic between two independently proven-safe operands")
        return _mk("UNRESOLVED_LOCAL_ALIAS", None, "BINOP_OPERAND_NOT_PROVEN",
                  "arithmetic has at least one operand not proven safe")
    if isinstance(expr, ast.IfExp):
        # ROUND 5: a ternary `X if cond else Y` (e.g. `latest_ts = ok_at if
        # latest_is_ok else bad_at`) is safe only when BOTH branches independently
        # prove safe -- same all-branches-safe rule as BoolOp, since at runtime
        # either could be the actual value.
        body_class = classify_value_expr(expr.body, env, mctx)
        else_class = classify_value_expr(expr.orelse, env, mctx)
        if (body_class["category"] in _VALUE_SAFE_CATEGORIES
                and else_class["category"] in _VALUE_SAFE_CATEGORIES):
            return _mk("RESOLVED_BUILTIN", None, "PROVEN_IFEXP_BOTH_BRANCHES_SAFE",
                      "ternary expression, both branches independently proven safe")
        return _mk("UNRESOLVED_LOCAL_ALIAS", None, "IFEXP_BRANCH_NOT_PROVEN",
                  "ternary expression has at least one branch not proven safe")
    if isinstance(expr, ast.Call):
        return classify_call_result_as_value(expr, env, mctx)
    return _mk("UNRESOLVED_LOCAL_ALIAS", None, "NON_REFERENCE_VALUE",
              f"RHS is a {type(expr).__name__}, not a simple reference")


def classify_call_result_as_value(node: ast.Call, env: dict, mctx: dict) -> dict:
    """ROUND 5: classify the RESULT of a Call used as a VALUE (an assignment RHS, or a
    receiver expression, e.g. `_connect_panel_db()` inside `con = _connect_panel_db()`
    then `con.execute(...)`) -- distinct from `_record_call()`, which separately
    records this SAME Call node in the reachable call inventory regardless of what this
    function returns. Safe when the callee itself already classifies safe (its result
    is then a plain external/builtin/stdlib value, e.g. `subprocess.run(...)`,
    `str(...)`, `sqlite3.connect(...)`), OR when the callee is a project function
    (`RESOLVED_CALL`) whose OWN declared return-type annotation names one of the
    curated safe families (section 5b) -- e.g. `_connect_panel_db() -> sqlite3.Connection`
    or `_manager_rows(...) -> List[dict]`. Never infers a family from a project
    function's return statements or from any other structural guess."""
    # CORRECTNESS NOTE: `RESOLVED_CALL` means the callee is a KNOWN PROJECT FUNCTION --
    # that tells us WHAT is being called, never what it RETURNS. Checking the full
    # `SAFE_CALL_CATEGORIES` tuple here (which also contains "RESOLVED_CALL") would
    # treat ANY project function's return value as safe unconditionally -- an
    # adversarial probe (bug_check3.py) confirmed this silently defeats the entire
    # round's fix. A `RESOLVED_CALL` result is safe ONLY via the return-annotation
    # check below; only an actual builtin/stdlib callable makes the result safe here.
    func_class = classify_call_func(node.func, env, mctx)
    if func_class["category"] in ("RESOLVED_BUILTIN", "RESOLVED_IMPORT"):
        result = _mk("RESOLVED_BUILTIN", None, "PROVEN_RESULT_OF_SAFE_EXTERNAL_CALL",
                    f"result of a call already proven safe: {ast.unparse(node.func)[:60]}")
        # ROUND 6: a json.loads()/json.load() result additionally carries a proven
        # ELEMENT shape -- see _is_json_decode_call(). This is the ONLY place that
        # attaches it: element_provenance is never inferred merely because the VALUE
        # itself is safe (that was the R5 regression; see classify_iterable_element).
        if _is_json_decode_call(node.func, env, mctx):
            result["element_provenance"] = _mk(
                "RESOLVED_BUILTIN", None, "PROVEN_JSON_DECODED_ELEMENT",
                "element of a json.loads()/json.load() result -- JSON decoding is "
                "closed under {dict, list, str, int, float, bool, None}, which can "
                "never structurally be a project-local callable")
        elif (isinstance(node.func, ast.Name) and node.func.id not in env
              and node.func.id in mctx.get("from_import_source_module", {})):
            # ROUND 6 (Form G): a bare-name call through a `from <module> import
            # <name>` binding -- try that EXTERNAL function's own List[X] return
            # annotation, read from its home module (see _external_def_element_family).
            elem = _external_def_element_family(node.func.id, mctx)
            if elem is not None:
                result["element_provenance"] = elem
        return result
    if func_class["category"] == "RESOLVED_CALL" and func_class.get("target"):
        fname, flineno = func_class["target"]
        ann = mctx["return_annotations"].get((fname, flineno))
        fam = _annotation_safe_family(ann)
        target_node = next((d for d in mctx["all_defs"].get(fname, []) if d.lineno == flineno), None)

        def _elem_provenance_for_callee():
            # ROUND 6: element shape, tried in order: (1) the callee's OWN
            # List[X]/list[X] return annotation naming X directly; (2) failing that
            # (e.g. a bare "list | None" annotation, which names no element), the
            # bounded accumulator idiom (`X = []` ... `X.append(<safe-expr>)` ...
            # `return X`) inspected via `_accumulator_element_family` -- tried
            # regardless of whether a (family-only) VALUE annotation already made the
            # whole call safe, since that says nothing about elements.
            elem_fam = _annotation_element_family(ann)
            if elem_fam:
                return _mk("RESOLVED_BUILTIN", None, "PROVEN_ANNOTATED_LIST_ELEMENT",
                          f"{fname}()'s own List[...] return annotation names element "
                          f"family {elem_fam!r}")
            if target_node is not None:
                return _accumulator_element_family(target_node, mctx)
            return None

        if fam:
            result = _mk("RESOLVED_BUILTIN", None, "PROVEN_BY_CALLEE_RETURN_ANNOTATION",
                        f"{fname}() is annotated to return {fam!r}")
            elem = _elem_provenance_for_callee()
            if elem is not None:
                result["element_provenance"] = elem
            return result
        # ROUND 5: no usable annotation -- fall back to inspecting the callee's
        # ACTUAL return statements (`_function_return_safe`), never a name/guess.
        if target_node is not None and _function_return_safe(target_node, mctx):
            result = _mk("RESOLVED_BUILTIN", None, "PROVEN_BY_CALLEE_RETURN_STATEMENTS",
                        f"every explicit return in {fname}() independently proves safe")
            elem = _elem_provenance_for_callee()
            if elem is not None:
                result["element_provenance"] = elem
            return result
    return _mk("UNRESOLVED_LOCAL_ALIAS", None, "CALL_RESULT_NOT_PROVEN_SAFE",
              f"result of {ast.unparse(node.func)[:60]} is not proven safe")


def classify_call_func(func_node: ast.AST, env: dict, mctx: dict) -> dict:
    """Classify a CALL's `func` expression -- the single entry point used for every
    `Call` node found anywhere in a reachable node's body."""
    if isinstance(func_node, ast.Name):
        res = resolve_name_reference(func_node.id, env, mctx)
        if res["category"] == "LOCAL_DICT_LITERAL":
            # a bare dict object called directly, e.g. `_RV_TBL()` -- not a valid
            # Python call target at runtime; statically, not a resolvable reference.
            return _mk("UNRESOLVED_LOCAL_ALIAS", None, "DICT_OBJECT_CALLED_DIRECTLY")
        return res
    if isinstance(func_node, ast.Attribute):
        return classify_attribute_expr(func_node, env, mctx)
    if isinstance(func_node, ast.Subscript):
        return classify_subscript_expr(func_node, env, mctx)
    if isinstance(func_node, ast.Call):
        # a call whose result is immediately invoked, e.g. `getattr(obj, key)()` or
        # `_make_handler()()` -- unconditionally dynamic (section 5/9).
        return _mk("UNRESOLVED_DYNAMIC_CALL", None, "CALL_RESULT_INVOKED",
                  "func is itself a Call expression (e.g. getattr(...)())")
    return _mk("UNRESOLVED_DYNAMIC_CALL", None, "UNHANDLED_CALL_FUNC_SHAPE",
              f"func is a {type(func_node).__name__}")


# ======================================================================
# SECTION 6b -- ROUND 6: element provenance for `for`/comprehension targets
# ======================================================================
#
# W3.2 CORRECTION ROUND 6 (2026-07-31): an independent review of round 5
# (C:\ALM_TPilot_AUDIT\20260731\W3_2_CORRECTION_R5_REVIEW\, verdict FAIL) found the
# LAST residual F-2 shape: the `For`/`AsyncFor` and comprehension-generator target
# binding treated a loop/comprehension variable as safe whenever the ITERABLE/
# CONTAINER it was drawn from classified as a safe VALUE (`_VALUE_SAFE_CATEGORIES`) --
# "the container is a list" was treated as proof that "an element pulled out of it is
# safe", which does not follow: a `list` can hold an ARBITRARY object, including a
# reference to a project-local function. Proven fail-open with
# `for fn in [unsafe_helper]: fn()` and `[fn() for fn in [unsafe_helper]]`.
#
# Round 6 deletes that inference entirely. A loop/comprehension target is now safe
# ONLY when the ELEMENT ITSELF has independent provenance, via a bounded, auditable
# set of concrete forms (task section 3, A-G) -- never merely because the outer
# container/iterable classifies as a safe VALUE:
#
#   A. a literal container (`List`/`Set`/`Tuple`/`Dict`) -- each element/key
#      classified INDIVIDUALLY (`classify_iterable_element`'s literal branches);
#   B. a comprehension/generator whose YIELDED expression is independently
#      classifiable (`_comprehension_yield_classification`);
#   C. direct iteration over a dict literal -- Python semantics: yields KEYS;
#   D. `.keys()`/`.values()`/`.items()` on a dict literal -- each key/value AST node
#      classified individually (`_iterable_dict_node`/`_dict_node_pure_pairs`);
#   E. `enumerate(x)` -- the index is always a plain int; the item is `x`'s own
#      element provenance, recursively (`bind_for_targets`);
#   F. `zip(a, b, ...)` -- each unpacked name is that positional iterable's own
#      element provenance, independently;
#   G. a bounded set of concrete external/annotation-derived shapes actually present
#      in this closure: a `json.loads()`/`json.load()` result (JSON decoding is
#      closed under {dict, list, str, int, float, bool, None} by the JSON grammar
#      itself -- `_is_json_decode_call`); a project function/parameter whose OWN
#      declared `List[X]` annotation names X (`_annotation_element_family`); or an
#      unannotated project function's own bounded accumulator idiom (`X = []`;
#      `X.append(<safe-expr>)`; `return X`) via `_accumulator_element_family`.
#
# Anything else is `UNKNOWN_ELEMENT_PROVENANCE` -- fails closed, exactly like any
# other unresolved shape in this file. A `Tuple`/`List` (unpack) target is bound ONLY
# for the three concrete forms proven sound above (items/enumerate/zip); any other
# tuple-unpack shape binds NOTHING (unchanged from this file's pre-round-6 behavior,
# which never bound tuple targets at all), so an unhandled shape fails closed via the
# existing UNTRACKED_NAME path rather than through a new, unaudited guess.

def classify_iterable_element(iter_expr: ast.AST, env: dict, mctx: dict) -> dict:
    """Classify what a SINGLE-NAME `for`/comprehension target binds to when iterating
    `iter_expr` -- the element, never the container. See the section docstring above
    for the full form list (A-G)."""
    if isinstance(iter_expr, ast.Dict):
        pairs = _dict_node_pure_pairs(iter_expr)
        if pairs is None:
            return _mk("UNRESOLVED_LOCAL_ALIAS", None, "UNKNOWN_ELEMENT_PROVENANCE",
                      "dict literal contains a **spread -- keys not enumerable")
        keys, _values = pairs
        return _merge_many([classify_value_expr(k, env, mctx) for k in keys])
    if isinstance(iter_expr, (ast.List, ast.Set, ast.Tuple)):
        return _merge_many([classify_value_expr(e, env, mctx) for e in iter_expr.elts])
    if isinstance(iter_expr, _COMPREHENSION_TYPES):
        return _comprehension_yield_classification(iter_expr, env, mctx)
    if isinstance(iter_expr, ast.BoolOp):
        return _merge_many([classify_iterable_element(v, env, mctx) for v in iter_expr.values])
    if isinstance(iter_expr, ast.Call):
        func = iter_expr.func
        if (isinstance(func, ast.Attribute) and func.attr in ("keys", "values")
                and not iter_expr.args and not iter_expr.keywords):
            dict_node = _iterable_dict_node(func.value, env, mctx)
            if dict_node is not None:
                pairs = _dict_node_pure_pairs(dict_node)
                if pairs is not None:
                    keys, values = pairs
                    exprs = keys if func.attr == "keys" else values
                    return _merge_many([classify_value_expr(e, env, mctx) for e in exprs])
        if (isinstance(func, ast.Name) and func.id in ("enumerate", "zip")
                and func.id not in env):
            # enumerate()/zip() ALWAYS yield real `tuple` objects when bound to a
            # SINGLE (non-unpacked) name -- a genuine builtin tuple instance can never
            # carry a project-local attribute, regardless of its contents (the same
            # reasoning that already makes a bare literal tuple/list/dict a safe
            # RECEIVER -- see classify_value_expr's literal-container branch).
            return _mk("RESOLVED_BUILTIN", None, "PROVEN_ENUMERATE_OR_ZIP_TUPLE",
                      f"{func.id}(...) always yields a real tuple object")
    base = classify_value_expr(iter_expr, env, mctx)
    ep = base.get("element_provenance")
    if ep is not None:
        return ep
    return _mk("UNRESOLVED_LOCAL_ALIAS", None, "UNKNOWN_ELEMENT_PROVENANCE",
              f"cannot prove the element shape of {ast.unparse(iter_expr)[:60]!r} -- "
              f"the container/value being safe does NOT make an element drawn from "
              f"it safe (the exact R5 regression this round closes)")


def _comprehension_yield_classification(comp_node: ast.AST, env: dict, mctx: dict) -> dict:
    """Form B: the classification of a comprehension/generator's OWN yielded
    expression (`elt` for List/Set/GeneratorExp; the KEY for a DictComp, matching real
    Python semantics -- iterating a dict yields its keys), evaluated in a
    comprehension-scoped env built the same way `_scan_expr_for_calls_rec` threads
    one: each `for` clause's target bound via `bind_for_targets`, in order, in a COPY
    of the outer env, before the yielded expression is classified."""
    comp_env = dict(env)
    for gen in comp_node.generators:
        comp_env.update(bind_for_targets(gen.target, gen.iter, comp_env, mctx))
    if isinstance(comp_node, ast.DictComp):
        return classify_value_expr(comp_node.key, comp_env, mctx)
    return classify_value_expr(comp_node.elt, comp_env, mctx)


def bind_for_targets(target_node: ast.AST, iter_expr: ast.AST, env: dict, mctx: dict) -> dict:
    """The single target-binding entry point shared by `analyze_block`'s `For`/
    `AsyncFor` handling and `_scan_expr_for_calls_rec`'s comprehension-generator
    handling (task section 4: "preserve target provenance inside the loop/
    comprehension body" -- one implementation, not two that could drift). Returns a
    dict of {name: classification} to merge into the caller's env; never mutates
    `env` itself."""
    if isinstance(target_node, ast.Name):
        return {target_node.id: classify_iterable_element(iter_expr, env, mctx)}
    if not isinstance(target_node, (ast.Tuple, ast.List)):
        return {}
    elts = target_node.elts

    if (isinstance(iter_expr, ast.Call) and isinstance(iter_expr.func, ast.Attribute)
            and iter_expr.func.attr == "items" and not iter_expr.args
            and not iter_expr.keywords and len(elts) == 2
            and isinstance(elts[0], ast.Name) and isinstance(elts[1], ast.Name)):
        dict_node = _iterable_dict_node(iter_expr.func.value, env, mctx)
        if dict_node is not None:
            pairs = _dict_node_pure_pairs(dict_node)
            if pairs is not None:
                keys, values = pairs
                return {
                    elts[0].id: _merge_many([classify_value_expr(k, env, mctx) for k in keys]),
                    elts[1].id: _merge_many([classify_value_expr(v, env, mctx) for v in values]),
                }
        return {}

    if (isinstance(iter_expr, ast.Call) and isinstance(iter_expr.func, ast.Name)
            and iter_expr.func.id == "enumerate" and iter_expr.func.id not in env
            and len(iter_expr.args) == 1 and not iter_expr.keywords
            and len(elts) == 2 and isinstance(elts[0], ast.Name)):
        out = {elts[0].id: _mk("RESOLVED_BUILTIN", None, "PROVEN_ENUMERATE_INDEX",
                              "enumerate()'s own index is always a plain int")}
        out.update(bind_for_targets(elts[1], iter_expr.args[0], env, mctx))
        return out

    if (isinstance(iter_expr, ast.Call) and isinstance(iter_expr.func, ast.Name)
            and iter_expr.func.id == "zip" and iter_expr.func.id not in env
            and not iter_expr.keywords and len(iter_expr.args) == len(elts)
            and all(isinstance(e, ast.Name) for e in elts)):
        return {e.id: classify_iterable_element(a, env, mctx)
                for e, a in zip(elts, iter_expr.args)}

    return {}


# ======================================================================
# SECTION 7 -- ROUND 4: function-parameter callback proving
# ======================================================================

def _param_names_in_order(fn_node) -> list:
    args = fn_node.args
    return [a.arg for a in list(args.posonlyargs) + list(args.args)]


def _param_default_expr(fn_node, param_name: str):
    """The default-value AST expression for `param_name`, if the signature provides
    one (positional-or-keyword defaults align to the END of args; kwonly defaults are
    parallel to kwonlyargs, `None` entries meaning "no default")."""
    args = fn_node.args
    positional = _param_names_in_order(fn_node)
    if param_name in positional:
        idx = positional.index(param_name)
        n_defaults = len(args.defaults)
        first_default_idx = len(positional) - n_defaults
        if idx >= first_default_idx and n_defaults:
            return args.defaults[idx - first_default_idx]
        return None
    for kwarg, default in zip(args.kwonlyargs, args.kw_defaults):
        if kwarg.arg == param_name and default is not None:
            return default
    return None


def _match_call_argument(call_node: ast.Call, fn_node, param_name: str):
    """The AST expression bound to `param_name` at this specific callsite of `fn_node`,
    or `"__UNKNOWN__"` if the binding cannot be statically determined at this callsite
    (a `*args`/`**kwargs` spread, or simply not enough information), or `None` if the
    callsite doesn't supply it and the parameter has no default (should not happen for
    valid code, treated the same as unknown)."""
    positional = _param_names_in_order(fn_node)
    for a in call_node.args:
        if isinstance(a, ast.Starred):
            return "__UNKNOWN__"
    for kw in call_node.keywords:
        if kw.arg is None:
            return "__UNKNOWN__"  # **kwargs spread
    if param_name in positional:
        idx = positional.index(param_name)
        if idx < len(call_node.args):
            return call_node.args[idx]
        for kw in call_node.keywords:
            if kw.arg == param_name:
                return kw.value
        default = _param_default_expr(fn_node, param_name)
        return default if default is not None else "__UNKNOWN__"
    for kw in call_node.keywords:
        if kw.arg == param_name:
            return kw.value
    default = _param_default_expr(fn_node, param_name)
    return default if default is not None else "__UNKNOWN__"


def find_callsites_of(fn_name: str, tree: ast.Module, fn_node) -> list:
    """Every `Call(Name(fn_name))` anywhere in the module EXCEPT inside `fn_node`'s own
    body (a self-recursive call does not help determine what an EXTERNAL caller
    provides). Returns (call_node, containing_def_node_or_None) pairs."""
    out = []
    fn_body_ids = {id(n) for n in ast.walk(fn_node)}
    # module-level scan, tracking containment explicitly:
    parent_map: dict = {}
    for n in ast.walk(tree):
        for ch in ast.iter_child_nodes(n):
            parent_map[id(ch)] = n

    def containing_def(node):
        p = parent_map.get(id(node))
        best = None
        while p is not None:
            if isinstance(p, DEF_TYPES):
                best = p
            p = parent_map.get(id(p))
        return best

    for n in ast.walk(tree):
        if id(n) in fn_body_ids:
            continue
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == fn_name:
            out.append((n, containing_def(n)))
    return out


def resolve_parameter_binding(fn_node, param_name: str, mctx: dict, visiting: tuple = ()) -> dict:
    """Prove what `param_name` is bound to at every external callsite of `fn_node`.
    Resolved ONLY when at least one callsite is found, EVERY found callsite supplies a
    statically-determined argument for this parameter, and every one of those arguments
    resolves to the SAME target -- otherwise `UNRESOLVED_PARAMETER_CALLBACK`. An
    argument that is itself a bare Name matching one of the CALLING function's own
    parameters is chased recursively (bounded by `PARAM_PROOF_DEPTH` and a `visiting`
    cycle guard) so a callback forwarded through N wrapper levels still resolves."""
    cache_key = (id(fn_node), param_name)
    cache = mctx["param_cache"]
    if cache_key in cache:
        return cache[cache_key]
    if (cache_key in visiting) or (len(visiting) >= PARAM_PROOF_DEPTH):
        result = _mk("AMBIGUOUS_CALL", None, "PARAMETER_PROOF_CYCLE_OR_DEPTH_EXCEEDED")
        return result

    sites = find_callsites_of(fn_node.name, mctx["tree"], fn_node)
    if not sites:
        result = _mk("UNRESOLVED_PARAMETER_CALLBACK", None, "NO_KNOWN_CALLSITE",
                    f"no direct call to {fn_node.name}() found anywhere in the file")
        cache[cache_key] = result
        return result

    resolved_targets = []
    for call_node, container in sites:
        arg_expr = _match_call_argument(call_node, fn_node, param_name)
        if arg_expr in ("__UNKNOWN__", None):
            result = _mk("UNRESOLVED_PARAMETER_CALLBACK", None, "CALLSITE_ARGUMENT_NOT_STATIC",
                        f"callsite at line {call_node.lineno} does not statically bind {param_name}")
            cache[cache_key] = result
            return result
        if isinstance(arg_expr, ast.Name):
            if container is not None and arg_expr.id in _param_names_in_order(container):
                sub = resolve_parameter_binding(container, arg_expr.id, mctx,
                                                visiting + (cache_key,))
                if sub["category"] != "RESOLVED_CALL" and sub["category"] not in (
                        "RESOLVED_BUILTIN", "RESOLVED_IMPORT"):
                    result = _mk("UNRESOLVED_PARAMETER_CALLBACK", None,
                                "FORWARDED_PARAMETER_UNRESOLVED",
                                f"{container.name}'s own parameter {arg_expr.id} is unresolved")
                    cache[cache_key] = result
                    return result
                resolved_targets.append((sub["category"], sub["target"]))
            else:
                res = resolve_name_reference(arg_expr.id, {}, mctx)
                if res["category"] not in ("RESOLVED_CALL", "RESOLVED_BUILTIN", "RESOLVED_IMPORT"):
                    result = _mk("UNRESOLVED_PARAMETER_CALLBACK", None,
                                "CALLSITE_ARGUMENT_UNRESOLVED",
                                f"argument {arg_expr.id!r} at line {call_node.lineno} is unresolved")
                    cache[cache_key] = result
                    return result
                resolved_targets.append((res["category"], res["target"]))
        else:
            result = _mk("UNRESOLVED_PARAMETER_CALLBACK", None, "CALLSITE_ARGUMENT_NOT_A_NAME",
                        f"argument at line {call_node.lineno} is a {type(arg_expr).__name__}, "
                        f"not a simple reference")
            cache[cache_key] = result
            return result

    if len(set(resolved_targets)) != 1:
        result = _mk("AMBIGUOUS_CALL", None, "INCONSISTENT_CALLSITE_ARGUMENTS",
                    f"different callsites of {fn_node.name}() bind {param_name} to "
                    f"different targets: {sorted(set(resolved_targets))}")
        cache[cache_key] = result
        return result

    category, target = resolved_targets[0]
    result = _mk(category, target, "PROVEN_VIA_ALL_KNOWN_CALLSITES",
                f"every callsite of {fn_node.name}() binds {param_name} to the same target")
    cache[cache_key] = result
    return result


def resolve_parameter_receiver_binding(fn_node, param_name: str, mctx: dict) -> dict | None:
    """ROUND 5: prove a parameter is a safe RECEIVER -- a materially different, WEAKER
    question than `resolve_parameter_binding()`'s "is this parameter a proven CALLABLE
    TARGET" above. Used only as a fallback when that callable-target proof already
    failed (a parameter genuinely used as `param.attr()`, never `param()` itself).

    Classifies the actual argument expression at every callsite this gate has ALREADY
    analyzed as part of the reachable closure (`mctx["call_env_at"]`, populated by
    `_record_call()` -- present ONLY for a callsite inside a node this BFS has visited),
    using THAT callsite's own real flow-computed environment -- never an empty/
    context-free guess, so a loop variable or nested local alias at the callsite
    resolves correctly. Deliberately restricted to reachable callsites: per this
    round's task section 6 ("do not expand analysis to dead unrelated project code"),
    a callsite inside a function this gate has not visited contributes NO evidence
    either way, rather than being fetched and analyzed on demand.

    Returns `None` -- never a fabricated result -- when there is no such evidence at
    all, or when at least one known callsite's argument is not itself provably safe;
    the caller then keeps the conservative `UNRESOLVED_PARAMETER_CALLBACK` classification
    from `resolve_parameter_binding`, correctly failing closed."""
    cache = mctx.setdefault("recv_param_cache", {})
    cache_key = (id(fn_node), param_name)
    if cache_key in cache:
        return cache[cache_key]
    sites = find_callsites_of(fn_node.name, mctx["tree"], fn_node)
    call_env_at = mctx.get("call_env_at", {})
    known_linenos = []
    for call_node, _container in sites:
        callsite_env = call_env_at.get(id(call_node))
        if callsite_env is None:
            continue  # not (yet) analyzed as part of the reachable closure -- ignored
        arg_expr = _match_call_argument(call_node, fn_node, param_name)
        if arg_expr in ("__UNKNOWN__", None):
            cache[cache_key] = None
            return None
        cls = classify_value_expr(arg_expr, callsite_env, mctx)
        if cls["category"] not in _VALUE_SAFE_CATEGORIES:
            cache[cache_key] = None
            return None
        known_linenos.append(call_node.lineno)
    if not known_linenos:
        cache[cache_key] = None
        return None
    result = _mk("RESOLVED_BUILTIN", None, "PROVEN_BY_REACHABLE_CALLSITE_ARGUMENT",
                f"every REACHABLE callsite of {fn_node.name}() (line(s) {known_linenos}) "
                f"binds {param_name!r} to an independently proven-safe value")
    cache[cache_key] = result
    return result


# ======================================================================
# SECTION 8 -- ROUND 4: per-function flow-sensitive local dataflow
# ======================================================================

_AMBIGUOUS_LOCAL = _mk("AMBIGUOUS_CALL", None, "LOCAL_REBINDING_AMBIGUOUS",
                      "conditional branches bind this name to different targets")


def _block_always_exits(stmts: list) -> bool:
    """ROUND 5: True iff this statement list provably never falls through to whatever
    follows it. A conservative, bounded check -- NOT a general control-flow prover --
    used only to decide whether an exception handler's environment should participate
    in a try/except merge (`_block_always_exits(handler.body)`): a handler whose last
    statement is a bare `return`/`raise`/`continue`/`break` (or an `if` whose body AND
    orelse both recursively always-exit) can never reach code after the try/except, so
    its PRE-handler environment must not spuriously merge into the post-try/except env
    (see the Try-handling comment in `analyze_block`)."""
    if not stmts:
        return False
    last = stmts[-1]
    if isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(last, ast.If) and last.orelse:
        return _block_always_exits(last.body) and _block_always_exits(last.orelse)
    return False


_UNKNOWN_ELEMENT_ON_BRANCH = _mk(
    "UNRESOLVED_LOCAL_ALIAS", None, "UNKNOWN_ELEMENT_PROVENANCE_ON_BRANCH",
    "this branch attached no independent element evidence")


def _merge_element_provenance(ea: dict | None, eb: dict | None) -> dict:
    """ROUND 7 (closes the R6-review's D1 finding): merge two branches'
    `element_provenance` classifications via the SAME symmetric join
    `_merge_classifications` uses for ordinary bindings -- recursive, so a
    container-of-container element shape (`[[unsafe_helper]]`) merges correctly at
    every depth, and a branch that attached NO element evidence at all is treated as
    UNKNOWN rather than silently dropped or silently inherited from the branch that
    DID attach one."""
    return _merge_classifications(
        ea if ea is not None else _UNKNOWN_ELEMENT_ON_BRANCH,
        eb if eb is not None else _UNKNOWN_ELEMENT_ON_BRANCH,
        "PROVEN_ELEMENT_SAFE_ON_ALL_MERGED_BRANCHES",
        "every branch's element provenance independently merges safe")


def _merge_classifications(ra: dict, rb: dict, diagnostic: str = "PROVEN_SAFE_ON_ALL_MERGED_BRANCHES",
                           via: str = "every incoming branch independently proves this safe "
                                      "(no specific callable target on any path)") -> dict:
    """ROUND 6: the two-classification join rule ORIGINALLY inline in `_merge_envs`
    (round 5), extracted so it can ALSO fold N element classifications together
    (`_merge_many`, used by `classify_iterable_element` for a literal container's
    individually-classified elements) without a second, drifting copy of the same
    logic. A name/element bound identically (same category AND target) on both sides
    merges to that binding. A name/element proven safe with NO specific callable
    target on EITHER side (`target is None` on both) also merges cleanly -- both sides
    independently prove "this cannot be one of our own tracked functions", so the
    conjunction is still safe even though the concrete evidence differs. A genuine
    callable-TARGET disagreement, or anything else, merges to AMBIGUOUS -- "if multiple
    possible targets remain, AMBIGUOUS/RED" (section 3.F).

    ROUND 7 (closes the R6-review's D1 finding, task section 2): matching
    `(category, target)` used to mean "return `ra` unchanged" -- silently keeping
    whichever branch happened to be passed first and discarding the OTHER branch's
    `element_provenance`/`dict_info` ("receiver provenance" for subscript dispatch),
    making the merge depend on source order (`if: data=["s"] else: data=[unsafe]` vs
    the branches swapped produced different verdicts for the exact same program). Both
    nested payloads are now reconciled explicitly -- via the SAME symmetric join,
    recursively for `element_provenance` -- and NEVER taken from one side alone. An
    AMBIGUOUS/UNRESOLVED category has no "safe on all branches" claim to make and
    (in this closure) never carries either nested payload, so it is returned as-is,
    preserving its original diagnostic rather than relabelling it "proven safe"."""
    if (ra.get("category"), ra.get("target")) == (rb.get("category"), rb.get("target")):
        ea, eb = ra.get("element_provenance"), rb.get("element_provenance")
        da, db = ra.get("dict_info"), rb.get("dict_info")
        same_dict_info = (da is None and db is None) or (
            da is not None and db is not None and da.get("node") is db.get("node"))
        if (ea is None and eb is None and same_dict_info
                and ra.get("diagnostic") == rb.get("diagnostic")
                and ra.get("resolved_via") == rb.get("resolved_via")):
            # both sides agree on literally every field this merge cares about --
            # returning either is equivalent and order-independent.
            return ra
        if ra["category"] == "AMBIGUOUS_CALL" or str(ra["category"]).startswith("UNRESOLVED"):
            return ra
        merged = _mk(ra["category"], ra["target"],
                    "PROVEN_SAME_CLASSIFICATION_ON_ALL_MERGED_BRANCHES",
                    "every incoming branch independently proves this exact "
                    "classification -- element/receiver provenance reconciled "
                    "explicitly, never taken from one branch alone")
        if ea is not None or eb is not None:
            merged["element_provenance"] = _merge_element_provenance(ea, eb)
        if same_dict_info and da is not None:
            merged["dict_info"] = da
        return merged
    if (ra.get("category") in _VALUE_SAFE_CATEGORIES and ra.get("target") is None
            and rb.get("category") in _VALUE_SAFE_CATEGORIES and rb.get("target") is None):
        result = _mk("RESOLVED_BUILTIN", None, diagnostic, via)
        ea, eb = ra.get("element_provenance"), rb.get("element_provenance")
        if ea is not None or eb is not None:
            result["element_provenance"] = _merge_element_provenance(ea, eb)
        return result
    return _AMBIGUOUS_LOCAL


def _merge_many(classes: list) -> dict:
    """ROUND 6: fold a LIST of classifications (e.g. every element of a literal
    container, or every operand of a BoolOp iterable) into one, via
    `_merge_classifications`, left to right. An empty list (an empty literal
    container -- genuinely zero elements at every execution, not merely unproven) is
    its own base case: vacuously safe, since there is no element for an unsafe
    reference to ever occupy."""
    if not classes:
        return _mk("RESOLVED_BUILTIN", None, "PROVEN_ELEMENT_OF_EMPTY_LITERAL",
                  "the container is a literal with zero elements")
    result = classes[0]
    for c in classes[1:]:
        result = _merge_classifications(result, c, "PROVEN_SAFE_ON_ALL_MERGED_ELEMENTS",
                                        "every element independently proves safe "
                                        "(no specific callable target on any element)")
    return result


def _merge_envs(a: dict, b: dict) -> dict:
    """Join two local environments at a branch-merge point (the end of an if/else, or
    a try's body/handlers/orelse). See `_merge_classifications` for the per-name rule.
    A name present on only one incoming path still merges to AMBIGUOUS -- cannot prove
    it is bound at all on the other path, so it cannot be trusted after the join either."""
    out = {}
    for name in set(a) | set(b):
        ra, rb = a.get(name), b.get(name)
        if ra is not None and rb is not None:
            out[name] = _merge_classifications(ra, rb)
        else:
            out[name] = _AMBIGUOUS_LOCAL
    return out


def _record_call(calls: list, node: ast.Call, env: dict, mctx: dict) -> None:
    res = classify_call_func(node.func, env, mctx)
    calls.append({
        "lineno": node.lineno, "col": node.col_offset,
        "func_kind": type(node.func).__name__,
        "category": res["category"], "target": res.get("target"),
        "diagnostic": res.get("diagnostic"), "resolved_via": res.get("resolved_via"),
    })
    # ROUND 5: snapshot the flow-sensitive env IN EFFECT at this exact callsite (a
    # copy -- `env` is mutated in place as analyze_block proceeds through the rest of
    # the block) so `resolve_parameter_receiver_binding()` can later classify this
    # call's OWN arguments using the caller's REAL local state, never a context-free
    # guess. Only ever populated for callsites inside a node this gate has itself
    # analyzed (i.e. reachable) -- a callsite inside dead/unrelated code is simply
    # never recorded here, so receiver-proving never expands into it.
    mctx["call_env_at"][id(node)] = dict(env)


_COMPREHENSION_TYPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _scan_expr_for_calls(expr: ast.AST, env: dict, mctx: dict, calls: list) -> None:
    """Every `Call` AST node anywhere inside `expr`, each classified against the
    environment in effect at its position. ROUND 5: comprehensions/generator
    expressions (`[... for x in y ...]`, `{k: v for ...}`, `(... for ...)`) get a
    DEDICATED recursive walk instead of a blind `ast.walk` -- Python gives each
    comprehension its own scope, so a `for x in y` generator clause must bind `x`
    (proven as a container element when `y` itself proves safe, exactly like a real
    `for` statement) in a COPY of the outer environment before classifying the
    comprehension's own element/condition sub-expressions, or a receiver like
    `r.get(...)` inside `[r.get(...) for r in rows]` would resolve `r` against the
    OUTER scope (where it does not exist) and fail closed for no real reason."""
    if expr is None:
        return
    _scan_expr_for_calls_rec(expr, env, mctx, calls)


def _push_fn_key(mctx: dict, fn_node) -> None:
    """ROUND 7: mark `fn_node` as the def instance currently being analyzed, for the
    unique-target inventory (task section 6). Called by every analysis ENTRY POINT
    (`analyze_function_calls`, `_function_return_safe`, `_accumulator_element_family`)
    -- always paired with `_pop_fn_key` in a `try/finally`."""
    mctx.setdefault("fn_key_stack", []).append((fn_node.name, fn_node.lineno))


def _pop_fn_key(mctx: dict) -> None:
    mctx["fn_key_stack"].pop()


def _current_fn_key(mctx: dict):
    stack = mctx.get("fn_key_stack")
    return stack[-1] if stack else None


def _span(node: ast.AST) -> tuple:
    return (node.lineno, node.col_offset,
            getattr(node, "end_lineno", None), getattr(node, "end_col_offset", None))


def _log_element_binding(mctx: dict, kind: str, target_node: ast.AST, name: str,
                         iter_expr: ast.AST, classification: dict) -> None:
    """ROUND 6: record ONE loop/comprehension target binding for the element-provenance
    inventory (task section 9 originally; task section 6 in round 7). Every field the
    inventory reports is derived from this log, never asserted separately from what the
    classifier actually did.

    ROUND 7 (closes the R6-review's C-1/C-2 finding: the round-6 inventory reported
    ANALYSIS-LOG-ENTRY counts as if they were unique source-target counts, because a
    def analyzed more than once -- see `_current_fn_key`'s docstring -- logs the SAME
    target more than once): each entry additionally carries a STABLE IDENTITY (the
    containing def instance + this binding's own AST span + kind), so
    `run_panel_static_gate()` can deduplicate log entries into unique targets instead
    of conflating the two concepts."""
    target_is_direct_name = isinstance(target_node, ast.Name)
    sub_node = target_node
    if isinstance(target_node, (ast.Tuple, ast.List)):
        sub_node = next((e for e in target_node.elts
                         if isinstance(e, ast.Name) and e.id == name), target_node)
    fn_key = _current_fn_key(mctx)
    target_span = _span(sub_node)
    iter_span = _span(iter_expr)
    mctx.setdefault("element_binding_log", []).append({
        "kind": kind, "fn_key": fn_key, "name": name,
        "lineno": target_span[0],  # retained for backward-compat display only
        "target_span": target_span, "iter_span": iter_span,
        # ROUND 7: whether the ENCLOSING for/comprehension target is a bare `Name`
        # (`for x in ...`) versus a `Tuple`/`List` unpack (`for k, v in ...items()`)
        # -- a Tuple/List site can log 0, 1 or 2 entries depending on the iterable
        # shape (see bind_for_targets), so the independent AST cross-check in
        # run_panel_static_gate() only compares the NAME-target subset on both sides;
        # tuple-derived entries are counted and reported, never silently folded into
        # that 1:1 comparison.
        "target_is_direct_name": target_is_direct_name,
        "iter_source": ast.unparse(iter_expr)[:80],
        "category": classification["category"],
        "diagnostic": classification.get("diagnostic"),
        "resolved_via": classification.get("resolved_via"),
        "identity": (fn_key, kind, target_span, iter_span),
    })


def _scan_expr_for_calls_rec(expr: ast.AST, env: dict, mctx: dict, calls: list) -> None:
    if isinstance(expr, ast.Call):
        _record_call(calls, expr, env, mctx)
    if isinstance(expr, _COMPREHENSION_TYPES):
        comp_env = dict(env)
        for gen in expr.generators:
            _scan_expr_for_calls_rec(gen.iter, comp_env, mctx, calls)
            # ROUND 6: element provenance via bind_for_targets() -- see section 6b.
            # NEVER "the iterable is a safe VALUE, therefore the element is safe"
            # (the exact R5 regression this round closes).
            bindings = bind_for_targets(gen.target, gen.iter, comp_env, mctx)
            comp_env.update(bindings)
            for name, cls in bindings.items():
                _log_element_binding(mctx, "comprehension", gen.target, name, gen.iter, cls)
            for cond in gen.ifs:
                _scan_expr_for_calls_rec(cond, comp_env, mctx, calls)
        if isinstance(expr, ast.DictComp):
            _scan_expr_for_calls_rec(expr.key, comp_env, mctx, calls)
            _scan_expr_for_calls_rec(expr.value, comp_env, mctx, calls)
        else:
            _scan_expr_for_calls_rec(expr.elt, comp_env, mctx, calls)
        return  # do NOT fall through to generic child recursion -- wrong (outer) env
    for child in ast.iter_child_nodes(expr):
        _scan_expr_for_calls_rec(child, env, mctx, calls)


def _bind_simple_target(stmt, env: dict, mctx: dict) -> None:
    """Update `env` for a simple `Assign`/`AnnAssign` with a single `Name` target. The
    binding is computed EAGERLY (not a lazy pointer) so later chained aliases and cycle
    detection are trivial -- `classify_value_expr` itself may recurse through
    `resolve_name_reference` back into `env`, which already holds this statement's OWN
    prior bindings (not yet overwritten), giving correct sequential (not cyclic)
    semantics for ordinary chains, and delegating to module-level cycle-safe
    `resolve_reference()` for anything routed through a module capture/alias cycle."""
    target = stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target
    value = stmt.value
    if value is None:
        return
    env[target.id] = classify_value_expr(value, env, mctx)


def analyze_block(stmts: list, env_in: dict, mctx: dict, return_sink: list | None = None) -> tuple:
    """The core round-4/5 dataflow: thread a local binding environment forward through
    a list of statements (a function body, or any nested block), in source order,
    updating `env` at every simple alias assignment and MERGING environments at every
    branch point (`if`/`try`/`for`/`while`/`with`), so a use after a branch sees either
    a single agreed-upon binding (if all paths that could reach it agree) or the
    AMBIGUOUS sentinel (section 3.F). Returns (env_out, calls, return_sink) where
    `calls` is every `Call` AST node encountered, each already classified against the
    environment in effect at ITS position, and `return_sink` (ROUND 5) is a list
    accumulating the classification of every `return <value>` statement's value,
    threaded through every recursive call within the SAME enclosing function (NOT
    reset for if/try/for/while/with bodies -- only a NESTED `def` gets its own,
    separate, discarded sink, matching its already-separate parameter scope) -- used
    by `_function_return_safe()` to prove an UNANNOTATED project function's return
    shape from its actual body, never from a name or guess."""
    env = dict(env_in)
    calls: list = []
    if return_sink is None:
        return_sink = []
    for stmt in stmts:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            # scan the RHS for calls FIRST (uses the environment as of BEFORE this
            # assignment -- correct Python evaluation order), then update env.
            _scan_expr_for_calls(stmt.value, env, mctx, calls)
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                _bind_simple_target(stmt, env, mctx)
            elif (len(targets) == 1 and isinstance(targets[0], ast.Attribute)
                  and isinstance(targets[0].value, ast.Name) and stmt.value is not None):
                # ROUND 5: obj.attr = value -- track the (receiver, attr) binding in
                # THIS SAME env dict via a tuple key, so it participates in the
                # existing branch-merge/sequential-overwrite/deletion machinery
                # completely unchanged (see classify_attribute_expr priority 1).
                env[("ATTR", targets[0].value.id, targets[0].attr)] = classify_value_expr(
                    stmt.value, env, mctx)
            else:
                for t in targets:
                    if isinstance(t, ast.Name):
                        env[t.id] = _mk("UNRESOLVED_LOCAL_ALIAS", None, "TUPLE_OR_MULTI_TARGET")
        elif isinstance(stmt, ast.AugAssign):
            _scan_expr_for_calls(stmt.value, env, mctx, calls)
            if isinstance(stmt.target, ast.Name):
                env[stmt.target.id] = _mk("UNRESOLVED_LOCAL_ALIAS", None,
                                          "AUGASSIGN_INVALIDATES_ALIAS")
        elif isinstance(stmt, ast.Delete):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id in env:
                    del env[t.id]
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if isinstance(stmt, ast.Import):
                for a in stmt.names:
                    env[(a.asname or a.name).split(".")[0]] = _mk("RESOLVED_IMPORT", None,
                                                                   None, "local_import")
            else:
                for a in stmt.names:
                    if a.name != "*":
                        env[a.asname or a.name] = _mk("RESOLVED_IMPORT", None, None,
                                                      "local_from_import")
        elif isinstance(stmt, ast.Return):
            # ROUND 5: record this return value's classification (in the env in
            # effect AT this exact point) for `_function_return_safe()` -- never
            # reset for nested if/try/for/while/with blocks, only for a separately-
            # scoped NESTED def (see that branch below).
            _scan_expr_for_calls(stmt.value, env, mctx, calls)
            if stmt.value is not None:
                return_sink.append(classify_value_expr(stmt.value, env, mctx))
        elif isinstance(stmt, ast.If):
            _scan_expr_for_calls(stmt.test, env, mctx, calls)
            body_env, body_calls, _ = analyze_block(stmt.body, env, mctx, return_sink)
            if stmt.orelse:
                orelse_env, orelse_calls, _ = analyze_block(stmt.orelse, env, mctx, return_sink)
            else:
                orelse_env, orelse_calls = env, []
            # ROUND 7 (D1 probe M11): a branch that provably never falls through (a
            # bare return/raise/continue/break as its last statement) can never reach
            # code AFTER the if/else -- merging its env in anyway would let an UNSAFE
            # binding from a branch that can never actually continue here poison (or,
            # under the pre-round-7 merge bug, silently WIN over) the one branch that
            # genuinely reaches this point. Mirrors the existing Try/except handling
            # (`_block_always_exits(h.body)`) rather than introducing a new mechanism.
            body_exits = _block_always_exits(stmt.body)
            orelse_exits = bool(stmt.orelse) and _block_always_exits(stmt.orelse)
            if body_exits and not orelse_exits:
                env = orelse_env
            elif orelse_exits and not body_exits:
                env = body_env
            else:
                env = _merge_envs(body_env, orelse_env)
            calls += body_calls + orelse_calls
        elif isinstance(stmt, ast.Try):
            body_env, body_calls, _ = analyze_block(stmt.body, env, mctx, return_sink)
            handler_envs, handler_calls = [], []
            for h in stmt.handlers:
                h_env, h_calls, _ = analyze_block(h.body, env, mctx, return_sink)
                handler_calls += h_calls
                # ROUND 5: a handler that unconditionally exits (bare
                # return/raise/continue/break as its last statement) can never fall
                # through to code AFTER the try/except -- including its env in the
                # merge would spuriously mark a binding the try body set cleanly as
                # AMBIGUOUS (e.g. `try: cp = subprocess.run(...) \n except: return
                # None` -- `cp` is only ever used past this point on the success
                # path, where it IS bound).
                if not _block_always_exits(h.body):
                    handler_envs.append(h_env)
            if stmt.orelse:
                orelse_env, orelse_calls, _ = analyze_block(stmt.orelse, body_env, mctx, return_sink)
            else:
                orelse_env, orelse_calls = body_env, []
            merged = orelse_env
            for h_env in handler_envs:
                merged = _merge_envs(merged, h_env)
            calls += body_calls + handler_calls + orelse_calls
            if stmt.finalbody:
                fin_env, fin_calls, _ = analyze_block(stmt.finalbody, merged, mctx, return_sink)
                merged = fin_env
                calls += fin_calls
            env = merged
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            _scan_expr_for_calls(stmt.iter, env, mctx, calls)
            # ROUND 6: element provenance via bind_for_targets() -- see section 6b.
            # The old rule ("the iterable classifies as a safe VALUE, therefore its
            # element is safe") is DELETED, not narrowed; a Name target with no
            # provable element shape gets `classify_iterable_element`'s own
            # UNKNOWN_ELEMENT_PROVENANCE -- the conservative default, unchanged in
            # spirit from this file's pre-round-6 UNRESOLVED_LOCAL_ALIAS/LOOP_VARIABLE.
            bindings = bind_for_targets(stmt.target, stmt.iter, env, mctx)
            env.update(bindings)
            for name, cls in bindings.items():
                _log_element_binding(mctx, "loop", stmt.target, name, stmt.iter, cls)
            body_env, body_calls, _ = analyze_block(stmt.body, env, mctx, return_sink)
            merged = _merge_envs(env, body_env)  # loop body may run zero times
            if stmt.orelse:
                orelse_env, orelse_calls, _ = analyze_block(stmt.orelse, merged, mctx, return_sink)
                merged = orelse_env
                calls += orelse_calls
            calls += body_calls
            env = merged
        elif isinstance(stmt, ast.While):
            _scan_expr_for_calls(stmt.test, env, mctx, calls)
            body_env, body_calls, _ = analyze_block(stmt.body, env, mctx, return_sink)
            merged = _merge_envs(env, body_env)
            if stmt.orelse:
                orelse_env, orelse_calls, _ = analyze_block(stmt.orelse, merged, mctx, return_sink)
                merged = orelse_env
                calls += orelse_calls
            calls += body_calls
            env = merged
        elif isinstance(stmt, WITH_TYPES):
            for item in stmt.items:
                _scan_expr_for_calls(item.context_expr, env, mctx, calls)
                if isinstance(item.optional_vars, ast.Name):
                    # ROUND 5: `with urllib.request.urlopen(req) as resp:` -- if the
                    # context-manager expression is itself proven safe, propagate that
                    # to the bound name instead of the blanket unresolved marker.
                    ctx_class = classify_value_expr(item.context_expr, env, mctx)
                    if ctx_class["category"] in _VALUE_SAFE_CATEGORIES:
                        env[item.optional_vars.id] = _mk(
                            "RESOLVED_BUILTIN", None, "PROVEN_WITH_BOUND_FROM_SAFE_CALL",
                            f"bound from {ast.unparse(item.context_expr)[:60]}")
                    else:
                        env[item.optional_vars.id] = _mk("UNRESOLVED_LOCAL_ALIAS", None,
                                                         "WITH_BOUND_VARIABLE")
            body_env, body_calls, _ = analyze_block(stmt.body, env, mctx, return_sink)
            env = body_env
            calls += body_calls
        elif isinstance(stmt, DEF_TYPES):
            # a NESTED def: not separately reachability-tracked, so its calls are
            # folded into the ENCLOSING reachable node's inventory. Its own parameters
            # shadow the outer environment (closure semantics: the nested body can
            # still see outer local aliases for anything it does not itself rebind).
            # ROUND 5: its OWN return values get a fresh, discarded sink -- a nested
            # def's return shape is unrelated to the ENCLOSING function's.
            nested_env = dict(env)
            for pname in _param_names_in_order(stmt):
                nested_env[pname] = _mk("UNRESOLVED_PARAMETER_CALLBACK", None,
                                        "NESTED_DEF_PARAMETER_NOT_PROVEN")
            _, nested_calls, _ = analyze_block(stmt.body, nested_env, mctx, [])
            calls += nested_calls
            env[stmt.name] = _mk("UNRESOLVED_LOCAL_ALIAS", None, "NESTED_DEF_NOT_TRACKED")
        else:
            _scan_expr_for_calls(stmt, env, mctx, calls)
    return env, calls, return_sink


def _seed_param_env(fn_node, mctx: dict) -> dict:
    """ROUND 5: parameter-seeding logic shared by `analyze_function_calls()` (the
    main reachable-node entry point) AND `_function_return_safe()` below (a
    standalone, on-demand analysis of a callee's return shape, run regardless of
    whether that callee itself is BFS-reachable) -- both need IDENTICAL parameter
    treatment. Each parameter is bound in priority order: (1) a declared type
    annotation naming one of the curated safe families (section 5b) -- a parameter
    annotated `dict`/`"list | None"`/etc. is neither a forwarded callback nor an
    unproven receiver, it is exactly what its own signature says; (2) round 4's
    callable-target proving (`resolve_parameter_binding`) -- resolves a parameter that
    IS used as a forwarded callback; (3) receiver-family proving via every REACHABLE
    callsite's actual argument (`resolve_parameter_receiver_binding`), tried only when
    (1)-(2) did not resolve; otherwise the conservative `UNRESOLVED_PARAMETER_CALLBACK`
    from (2) stands, correctly failing closed for both callable-target and receiver
    purposes."""
    env: dict = {}
    args = fn_node.args
    all_param_names = (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs))
    for a in all_param_names:
        ann = getattr(a, "annotation", None)
        ann_fam = _annotation_safe_family(ann)
        if ann_fam:
            binding = _mk("RESOLVED_BUILTIN", None, "PROVEN_BY_PARAMETER_ANNOTATION",
                         f"parameter {a.arg!r} annotated as {ann_fam!r}")
            # ROUND 6: a parameter declared List[X]/list[X] additionally proves X's
            # OWN family as the ELEMENT shape for anyone iterating this parameter.
            elem_fam = _annotation_element_family(ann)
            if elem_fam:
                binding["element_provenance"] = _mk(
                    "RESOLVED_BUILTIN", None, "PROVEN_ANNOTATED_LIST_ELEMENT",
                    f"parameter {a.arg!r}'s own List[...] annotation names element "
                    f"family {elem_fam!r}")
            else:
                # A bare "list"/"list | None" annotation names no element -- fall
                # back to proving the ELEMENT shape via every REACHABLE callsite's
                # actual argument (the SAME receiver-binding machinery used below for
                # an unannotated parameter), tried purely for element purposes; the
                # parameter's own WHOLE-VALUE binding above is unaffected either way.
                recv = resolve_parameter_receiver_binding(fn_node, a.arg, mctx)
                if recv is not None:
                    binding["element_provenance"] = recv
            env[a.arg] = binding
            continue
        cb = resolve_parameter_binding(fn_node, a.arg, mctx)
        if cb["category"] in ("RESOLVED_CALL", "RESOLVED_BUILTIN", "RESOLVED_IMPORT"):
            env[a.arg] = cb
            continue
        recv = resolve_parameter_receiver_binding(fn_node, a.arg, mctx)
        env[a.arg] = recv if recv is not None else cb
    if args.vararg:
        env[args.vararg.arg] = _mk("UNRESOLVED_PARAMETER_CALLBACK", None, "VARARG_NOT_PROVEN")
    if args.kwarg:
        env[args.kwarg.arg] = _mk("UNRESOLVED_PARAMETER_CALLBACK", None, "KWARG_NOT_PROVEN")
    return env


def analyze_function_calls(fn_node, mctx: dict) -> list:
    """Entry point for round-4/5 call classification of one reachable definition
    instance: bind its parameters (`_seed_param_env`), then thread the flow-sensitive
    dataflow through its body."""
    _push_fn_key(mctx, fn_node)
    try:
        env = _seed_param_env(fn_node, mctx)
        _, calls, _ = analyze_block(fn_node.body, env, mctx)
        return calls
    finally:
        _pop_fn_key(mctx)


def _function_return_safe(fn_node, mctx: dict) -> bool:
    """ROUND 5: prove an UNANNOTATED project function's return value is safe by
    inspecting its ACTUAL body -- used as `classify_call_result_as_value()`'s fallback
    when the callee has no usable return-type annotation (e.g.
    `_tpag_panel_v2_parse_dt(raw)`, which always returns `None` or a `datetime`
    derived from `datetime.fromisoformat(...)`, never a project helper). Runs the SAME
    flow analysis as a normal call-classification pass (`_seed_param_env` +
    `analyze_block`) to collect every `return <value>` statement's classification
    (`return_sink`) with the CORRECT environment at each return point (not a
    context-free guess), then requires AT LEAST ONE explicit return AND EVERY one of
    them to independently prove safe -- a function with no explicit `return <value>`
    (implicit `None`) proves nothing either way here (harmless as a value, but not
    itself evidence of a "family"). Cached; safe against recursion (a function that
    calls itself, directly or through this same proof, is treated as NOT proven while
    still being computed, matching every other proof cache in this module)."""
    cache = mctx.setdefault("return_safety_cache", {})
    key = (fn_node.name, fn_node.lineno)
    if key in cache:
        return cache[key]
    cache[key] = False  # provisional, guards against infinite recursion
    _push_fn_key(mctx, fn_node)
    try:
        env = _seed_param_env(fn_node, mctx)
        _, _, return_sink = analyze_block(fn_node.body, env, mctx)
        result = bool(return_sink) and all(r["category"] in _VALUE_SAFE_CATEGORIES for r in return_sink)
        cache[key] = result
        return result
    finally:
        _pop_fn_key(mctx)


def _external_def_element_family(local_name: str, mctx: dict) -> dict | None:
    """ROUND 6 (Form G): for a call to `local_name`, proven bound via THIS file's own
    `from <module> import local_name` (see `collect_from_import_source_modules`), look
    up that function's OWN declared return-type annotation in its home module -- a
    single, cached, read-only parse of that ONE external file -- and extract the
    ELEMENT family exactly like `_annotation_element_family` does for a local def
    (e.g. `manager_registry.list_manager_rows_from_db_sync(...) -> List[dict]` proves
    its elements are dict-like). Narrowly scoped: only ever consulted for a name this
    file's own `from X import Y` statement actually names; the external function's own
    BODY is never parsed or analyzed, and it never becomes a `RESOLVED_CALL` target --
    this proves ONLY what its return annotation says, nothing about what calling it
    does. Returns `None` -- never a guess -- if the source module cannot be found, does
    not define the function, or the function has no `List[X]`-shaped annotation."""
    cache = mctx.setdefault("external_def_element_cache", {})
    if local_name in cache:
        return cache[local_name]
    cache[local_name] = None
    module_name = mctx.get("from_import_source_module", {}).get(local_name)
    if module_name is None:
        return None
    try:
        mod_path = BASE_DIR / f"{module_name}.py"
        if not mod_path.is_file():
            return None
        mod_tree = ast.parse(_src(mod_path))
    except (OSError, SyntaxError):
        return None
    target = next((n for n in mod_tree.body if isinstance(n, DEF_TYPES)
                  and n.name == local_name), None)
    if target is None:
        return None
    elem_fam = _annotation_element_family(target.returns)
    if not elem_fam:
        return None
    result = _mk("RESOLVED_BUILTIN", None, "PROVEN_EXTERNAL_ANNOTATED_LIST_ELEMENT",
                f"{module_name}.{local_name}()'s own List[...] return annotation "
                f"names element family {elem_fam!r}")
    cache[local_name] = result
    return result


def _accumulator_element_family(fn_node, mctx: dict) -> dict | None:
    """ROUND 6: prove the ELEMENT shape of an UNANNOTATED project function's return
    value for the ONE bounded accumulator idiom actually present in this closure
    (`_get_python_processes`):

        out = []                 # seed: a literal empty list, some local Name
        ...
        out.append(<expr>)       # zero or more times, anywhere in the body
        ...
        return out                # a bare Name, directly, matching the seeded name

    This is NOT a general mutable-container analysis: it recognizes exactly this
    shape (a plain `ast.walk` over the function body, not a flow-sensitive mutation
    tracker) and every `<expr>` argument of every matching `.append()` call must
    independently prove safe via `classify_value_expr`, run with a FRESH on-demand
    analysis of `fn_node` (mirroring `_function_return_safe`'s own on-demand pattern,
    so this proof does not depend on the main BFS traversal having already visited
    `fn_node` -- a caller may be discovered before its callee). Returns `None` --
    never a guess -- when there is no such seeded-and-returned accumulator, or when
    it is never appended to, or when ANY append argument fails to prove safe; the
    caller then has no element_provenance to attach, correctly leaving iteration of
    this function's result to fail closed on UNKNOWN_ELEMENT_PROVENANCE."""
    cache = mctx.setdefault("accumulator_element_cache", {})
    key = (fn_node.name, fn_node.lineno)
    if key in cache:
        return cache[key]
    cache[key] = None  # provisional, guards against infinite recursion
    _push_fn_key(mctx, fn_node)
    try:
        seeds = {
            stmt.targets[0].id
            for stmt in ast.walk(fn_node)
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.List) and not stmt.value.elts)
        }
        returned = next(
            (stmt.value.id for stmt in ast.walk(fn_node)
             if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Name)
             and stmt.value.id in seeds),
            None)
        if returned is None:
            return None
        env = _seed_param_env(fn_node, mctx)
        analyze_block(fn_node.body, env, mctx)  # populates mctx["call_env_at"] for this fn
        append_classes = []
        for node in ast.walk(fn_node):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "append" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == returned and len(node.args) == 1
                    and not node.keywords):
                continue
            call_env = mctx.get("call_env_at", {}).get(id(node))
            if call_env is None:
                return None  # unreachable within this on-demand pass -- fail closed
            cls = classify_value_expr(node.args[0], call_env, mctx)
            if cls["category"] not in _VALUE_SAFE_CATEGORIES:
                return None
            append_classes.append(cls)
        if not append_classes:
            return None  # a seeded accumulator that is never appended to proves nothing
        merged = _merge_many(append_classes)
        if merged["category"] not in _VALUE_SAFE_CATEGORIES:
            return None
        result = _mk("RESOLVED_BUILTIN", None, "PROVEN_ACCUMULATOR_ELEMENT",
                    f"every {returned}.append(...) argument in {fn_node.name}() "
                    f"independently proves safe")
        cache[key] = result
        return result
    finally:
        _pop_fn_key(mctx)


# ======================================================================
# SECTION 9 -- ROUND 4: file-wide local-alias inventory (independent of reachability;
# used for reporting/completeness, section 9/11 -- NEVER used for gating)
# ======================================================================

def collect_all_local_aliases(tree: ast.Module, all_defs: dict, visited: dict) -> list:
    """Every simple local `X = Y` / `X = obj.attr` / `X = table[key]` assignment inside
    ANY module-level def (reachable or not), tagged with its containing top-level def's
    reachability. A purely descriptive inventory -- dead entries are recorded, never
    gated (section 9: "Dead code must be recorded but must not fail the active runtime
    gate.")."""
    out = []
    for name, defs in all_defs.items():
        for d in defs:
            is_reachable = (d.name, d.lineno) in visited
            for n in ast.walk(d):
                if n is d:
                    continue
                if (isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                        and isinstance(n.value, (ast.Name, ast.Attribute, ast.Subscript))):
                    out.append({
                        "containing_def": f"{name}@{d.lineno}",
                        "reachable": is_reachable,
                        "target": n.targets[0].id, "lineno": n.lineno,
                        "value_kind": type(n.value).__name__,
                        "value_code": ast.unparse(n.value)[:80],
                    })
    return out


# ======================================================================
# SECTION 10 -- reachability graph: fail-closed BFS
# ======================================================================

def outgoing_edges(fn_node, mctx: dict, escape_watch: set) -> list:
    """Every outgoing reference from `fn_node`'s body: (1) EVERY `Call` AST node,
    classified via the round-4 flow-sensitive dataflow (`analyze_function_calls`) --
    the SAFE categories (RESOLVED_CALL/RESOLVED_BUILTIN/RESOLVED_IMPORT) behave exactly
    like round 3's `RESOLVED` status for BFS purposes (a RESOLVED_CALL with a concrete
    target is enqueued; RESOLVED_BUILTIN/RESOLVED_IMPORT carry no target and terminate
    there, safely); the six UNSAFE categories all fail the whole gate when sourced from
    a reachable node, exactly like round 3's UNRESOLVED_DYNAMIC/UNRESOLVED_ALIAS/
    AMBIGUOUS did -- and (2) a bare Name reference (not in call position, not inside a
    `callable(...)` guard) to one of the TZ-3-relevant `escape_watch` names, retained
    UNCHANGED from round 3 as an additional safety net for an escape shape this round's
    call classification does not itself cover (e.g. returning a captured reference as a
    plain value)."""
    calls = analyze_function_calls(fn_node, mctx)
    mctx["node_call_inventory"][(fn_node.name, fn_node.lineno)] = calls

    edges = []
    for c in calls:
        category = c["category"]
        if category in SAFE_CALL_CATEGORIES:
            status = "RESOLVED"
        else:
            status = category
        edges.append({
            "kind": "call", "via_name": f"<{c['func_kind']}>",
            "call_lineno": c["lineno"], "call_col": c["col"],
            "resolution": {"status": status, "def": c.get("target"),
                          "diagnostic": c.get("diagnostic"), "chain": None,
                          "call_category": category},
        })

    parent_map = mctx["parent_map"]
    ref_index, all_defs = mctx["ref_index"], mctx["all_defs"]
    for n in ast.walk(fn_node):
        if n is fn_node:
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in escape_watch:
            if _is_call_func_position(n, parent_map) or _is_callable_guard_context(n, parent_map):
                continue
            if n.id in ref_index:
                res = resolve_reference(n.id, _MODULE_FULLY_LOADED, ref_index, all_defs)
            elif n.id in all_defs:
                res = {"status": "RESOLVED", "def": None, "diagnostic": "BARE_DEF_NAME_REFERENCE",
                      "chain": [n.id]}
            else:
                continue
            edges.append({"kind": "bare_reference_escape", "via_name": n.id,
                         "call_lineno": n.lineno, "call_col": n.col_offset,
                         "resolution": {**res, "call_category": None}})
    return edges


def build_reachability_graph(tree: ast.Module, tz3_names=TZ3_FUNCTIONS) -> dict:
    parent_map = build_parent_map(tree)
    all_defs = collect_all_module_defs(tree)
    captures = find_globals_get_captures(tree)
    aliases = find_alias_assignments(tree)
    ref_index = build_reference_index(captures, aliases)
    active_binding = {name: defs[-1] for name, defs in all_defs.items() if defs}
    call_sites = collect_call_sites(tree, parent_map)
    escape_watch = _tz3_relevant_escape_watch(ref_index, all_defs, tz3_names)
    undefined_guarded = {n for n in _callable_guarded_names(tree)
                         if n not in ref_index and n not in all_defs}
    imported_module_names, imported_from_names = collect_import_bindings(tree)
    module_dict_literals = collect_module_dict_literals(tree)
    json_module_aliases, json_from_names = collect_json_decode_bindings(tree)
    from_import_source_module = collect_from_import_source_modules(tree)
    return_annotations = {(d.name, d.lineno): d.returns
                          for name, defs in all_defs.items() for d in defs}

    mctx = {
        "tree": tree, "all_defs": all_defs, "active_binding": active_binding,
        "ref_index": ref_index, "undefined_guarded": undefined_guarded,
        "parent_map": parent_map,
        "imported_module_names": imported_module_names,
        "imported_from_names": imported_from_names,
        "module_dict_literals": module_dict_literals,
        "json_module_aliases": json_module_aliases,
        "json_from_names": json_from_names,
        "from_import_source_module": from_import_source_module,
        "return_annotations": return_annotations,
        "param_cache": {},
        "recv_param_cache": {},
        "node_call_inventory": {},
        "call_env_at": {},
        # ROUND 6: every loop/comprehension target binding, for the element-provenance
        # inventory/arithmetic gate (task section 9) -- populated by
        # _log_element_binding() as analyze_block()/_scan_expr_for_calls_rec() run.
        "element_binding_log": [],
        # ROUND 7: which def instance is CURRENTLY being analyzed -- pushed/popped by
        # every analysis ENTRY POINT (analyze_function_calls, _function_return_safe,
        # _accumulator_element_family), so _log_element_binding can tag each entry
        # with the def it actually came from, for the unique-target inventory
        # (task section 6). A stack, not a single value, because an on-demand proof
        # (e.g. the accumulator idiom) can recursively trigger ANOTHER on-demand proof
        # of a different callee while still "inside" the first def's own analysis.
        "fn_key_stack": [],
        # ROUND 5: populated below by build_module_level_provenance() -- present as
        # empty dicts here only so classify_value_expr() can be called safely by
        # that same function while it builds them (see its own docstring).
        "module_attr_assignments": {},
        "module_value_bindings": {},
    }
    build_module_level_provenance(tree, mctx)

    roots = []
    root_evidence = {}
    for name in tz3_names:
        ev = [cs for cs in call_sites if cs["callee"] == name and cs["container"] not in tz3_names]
        root_evidence[name] = len(ev)
        if ev and name in active_binding:
            roots.append(active_binding[name])

    visited: dict = {}
    edge_log: list[dict] = []
    unresolved_reachable_edges: list[dict] = []
    q = deque()
    for r in roots:
        visited[(r.name, r.lineno)] = {"via": "external_root", "from": None}
        q.append(r)

    node_budget_hit = False
    while q:
        if len(visited) >= NODE_BUDGET:
            node_budget_hit = True
            break
        cur = q.popleft()
        for e in outgoing_edges(cur, mctx, escape_watch):
            res = e["resolution"]
            edge_record = {
                "source": f"{cur.name}@{cur.lineno}",
                "source_kind": "async" if isinstance(cur, ast.AsyncFunctionDef) else "sync",
                "kind": e["kind"], "via_name": e["via_name"],
                "call_lineno": e["call_lineno"], "call_col": e["call_col"],
                "status": res["status"], "diagnostic": res.get("diagnostic"),
                "resolved_target": (f"{res['def'][0]}@{res['def'][1]}" if res.get("def") else None),
                "chain": res.get("chain"),
                "call_category": res.get("call_category"),
            }
            edge_log.append(edge_record)
            if res["status"] == "RESOLVED":
                if res["def"] is None:
                    continue
                tkey = res["def"]
                if tkey not in visited:
                    tnode = next((d for d in all_defs.get(tkey[0], []) if d.lineno == tkey[1]), None)
                    if tnode is None:
                        edge_record["diagnostic"] = "RESOLVED_TARGET_NOT_IN_NODE_UNIVERSE"
                        unresolved_reachable_edges.append(edge_record)
                        continue
                    visited[tkey] = {"via": e["kind"], "from": edge_record["source"]}
                    q.append(tnode)
            else:
                unresolved_reachable_edges.append(edge_record)

    local_alias_inventory = collect_all_local_aliases(tree, all_defs, visited)

    return {
        "all_defs": all_defs, "captures": captures, "aliases": aliases, "ref_index": ref_index,
        "active_binding": active_binding, "call_sites": call_sites, "escape_watch": sorted(escape_watch),
        "undefined_guarded": sorted(undefined_guarded),
        "roots": [(r.name, r.lineno) for r in roots], "root_evidence": root_evidence,
        "visited": visited, "edge_log": edge_log,
        "unresolved_reachable_edges": unresolved_reachable_edges,
        "node_budget_hit": node_budget_hit,
        "parent_map": parent_map,
        "node_call_inventory": mctx["node_call_inventory"],
        "local_alias_inventory": local_alias_inventory,
        "imported_module_names": sorted(imported_module_names),
        "imported_from_names": sorted(imported_from_names),
        "element_binding_log": mctx["element_binding_log"],
    }


def classify_generations(all_defs: dict, active_binding: dict, visited: dict, tz3_names) -> tuple:
    classification = {}
    unknown_targets = []
    for name in tz3_names:
        defs = all_defs.get(name, [])
        if not defs:
            unknown_targets.append({"name": name, "reason": "MISSING_DEF"})
            continue
        for d in defs:
            key = (d.name, d.lineno)
            is_active = d is active_binding[name]
            is_reach = key in visited
            if is_reach and is_active:
                cls = "ACTIVE_REACHABLE"
            elif is_reach and not is_active:
                cls = "SHADOWED_BUT_REACHABLE"
            else:
                cls = "DEAD_UNREACHABLE"
            classification[f"{name}@{d.lineno}"] = {
                "classification": cls, "node": d, "is_active": is_active, "reachable": is_reach}
    return classification, unknown_targets


# ======================================================================
# structural completeness cross-checks -- independent of the AST-based collectors, so a
# matcher regression fails closed instead of silently returning zero findings with
# ok=True.
# ======================================================================

def run_completeness_checks(src: str, graph: dict) -> dict:
    regex_def_count = len(re.findall(r'^(?:async\s+def|def)\s+\w+\s*\(', src, re.M))
    ast_def_count = sum(len(v) for v in graph["all_defs"].values())
    def_count_ok = ast_def_count == regex_def_count

    regex_capture_count = len(re.findall(r'^\w+\s*=\s*globals\s*\(\s*\)\s*\.\s*get\s*\(', src, re.M))
    ast_capture_count = len(graph["captures"])
    capture_zero_regression = regex_capture_count > 0 and ast_capture_count == 0
    capture_count_deficit = ast_capture_count < regex_capture_count

    # ROUND 4: independent per-node AND file-wide Call-node count cross-check --
    # sum(ast.walk Call nodes) over the reachable closure must equal the classifier's
    # own count exactly; any deficit means a Call node was silently dropped (the exact
    # shape of the M32R2-6 regression, applied to the new call-classification layer).
    per_node_mismatches = []
    total_ast_calls = 0
    total_classified_calls = 0
    for (name, lineno), calls in graph["node_call_inventory"].items():
        node = next((d for d in graph["all_defs"].get(name, []) if d.lineno == lineno), None)
        if node is None:
            continue
        ast_call_count = sum(1 for n in ast.walk(node) if isinstance(n, ast.Call))
        total_ast_calls += ast_call_count
        total_classified_calls += len(calls)
        if ast_call_count != len(calls):
            per_node_mismatches.append({"node": f"{name}@{lineno}", "ast_calls": ast_call_count,
                                        "classified_calls": len(calls)})
    call_count_ok = not per_node_mismatches and total_ast_calls == total_classified_calls

    ok = (def_count_ok and not capture_zero_regression and not capture_count_deficit
          and call_count_ok)
    return {
        "ok": ok,
        "regex_def_count": regex_def_count, "ast_def_count": ast_def_count,
        "def_count_ok": def_count_ok,
        "regex_capture_count": regex_capture_count, "ast_capture_count": ast_capture_count,
        "capture_zero_regression": capture_zero_regression,
        "capture_count_deficit": capture_count_deficit,
        "call_count_ok": call_count_ok,
        "total_ast_calls_in_reachable_nodes": total_ast_calls,
        "total_classified_calls_in_reachable_nodes": total_classified_calls,
        "per_node_call_count_mismatches": per_node_mismatches,
    }


# ======================================================================
# scanning primitives (bare clock calls, UTC substitution, contract usage) -- unchanged
# in substance from round 2/3.
# ======================================================================

def scan_bare_clock_calls(node: ast.AST) -> list[dict]:
    hits = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        if isinstance(n.func, ast.Attribute):
            if n.func.attr in ("now", "today") and not n.args and not n.keywords:
                base = ast.unparse(n.func.value).split(".")[-1]
                if base in ("datetime", "date"):
                    hits.append({"line": n.lineno, "kind": "bare_now_today",
                                "code": ast.unparse(n)[:120]})
            elif n.func.attr == "astimezone" and not n.args and not n.keywords:
                hits.append({"line": n.lineno, "kind": "bare_astimezone",
                            "code": ast.unparse(n)[:120]})
    return hits


def scan_utc_as_kyiv_substitute(node: ast.AST) -> list[dict]:
    hits = []
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "utc":
            base = ast.unparse(n.value)
            if base.split(".")[-1] == "timezone":
                hits.append({"line": n.lineno, "kind": "timezone.utc", "code": ast.unparse(n)})
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ZoneInfo":
            if n.args and isinstance(n.args[0], ast.Constant) and str(n.args[0].value).upper() == "UTC":
                hits.append({"line": n.lineno, "kind": 'ZoneInfo("UTC")', "code": ast.unparse(n)})
    return hits


def scan_w3_contract_usage(node: ast.AST) -> list[dict]:
    hits = []
    for n in ast.walk(node):
        if isinstance(n, ast.ImportFrom) and n.module == "storage":
            for alias in n.names:
                if alias.name in ("w3_now", "w3_tz"):
                    hits.append({"line": n.lineno, "kind": "import", "name": alias.name,
                                "asname": alias.asname})
        if isinstance(n, ast.Attribute) and n.attr in ("w3_now", "w3_tz"):
            base = ast.unparse(n.value)
            if base.split(".")[-1] == "storage":
                hits.append({"line": n.lineno, "kind": "attribute", "name": n.attr})
    return hits


def scan_touches_clock_at_all(node: ast.AST) -> bool:
    """True iff `node` makes ANY datetime-construction call whatsoever. A node that
    never touches the clock cannot structurally carry the host-local-fallback defect,
    so it is exempt from the no-host-dependency requirement."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in ("now", "today", "astimezone"):
                return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ZoneInfo":
            return True
    return False


def scan_explicit_kyiv_construction(node: ast.AST) -> list[dict]:
    hits = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "now" and n.args:
            arg = n.args[0]
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name) \
                    and arg.func.id == "ZoneInfo" and arg.args \
                    and isinstance(arg.args[0], ast.Constant) \
                    and str(arg.args[0].value) == "Europe/Kyiv":
                hits.append({"line": n.lineno, "kind": "datetime.now(ZoneInfo('Europe/Kyiv'))"})
    return hits


def _independent_reachable_target_sites(graph: dict) -> list:
    """ROUND 7 (task section 6): an INDEPENDENT count of loop/comprehension sites in
    the reachable closure, built by a FRESH `ast.walk` over `graph['visited']`/
    `graph['all_defs']` -- this function NEVER calls `bind_for_targets()` or
    `classify_iterable_element()`, so it is a genuine second source of truth for
    cross-checking the element-binding log's own unique-target count, not the same
    computation read back at itself. Each site reports its target's SHAPE (`Name` vs
    `Tuple`/`List`): a bare-`Name` target always yields exactly one log entry per site
    by construction (`bind_for_targets`'s `Name` branch), giving a genuine 1:1
    cross-check; a `Tuple`/`List` (unpack) target yields 0, 1 or 2 entries depending on
    the iterable shape (`.items()`/`enumerate()`/`zip()` vs anything else, which binds
    nothing), so those sites are reported but deliberately excluded from the 1:1
    comparison rather than approximated."""
    sites = []
    for (name, lineno) in graph["visited"]:
        node = next((d for d in graph["all_defs"].get(name, []) if d.lineno == lineno), None)
        if node is None:
            continue
        fn_key = (name, lineno)
        for sub in ast.walk(node):
            if isinstance(sub, (ast.For, ast.AsyncFor)):
                sites.append({"fn_key": fn_key, "kind": "loop",
                             "target_is_name": isinstance(sub.target, ast.Name)})
            elif isinstance(sub, _COMPREHENSION_TYPES):
                for gen in sub.generators:
                    sites.append({"fn_key": fn_key, "kind": "comprehension",
                                 "target_is_name": isinstance(gen.target, ast.Name)})
    return sites


# ======================================================================
# gate assembly
# ======================================================================

def run_panel_static_gate(panel_bot_path: Path = PANEL_BOT_PATH) -> dict:
    src = _src(panel_bot_path)
    tree = ast.parse(src)

    graph = build_reachability_graph(tree)
    fixtures = run_globals_get_fixture_tests()
    async_alias_fixtures = run_async_alias_fixture_tests()
    local_alias_fixtures = run_local_alias_fixture_tests()
    parameter_callback_fixtures = run_parameter_callback_fixture_tests()
    completeness = run_completeness_checks(src, graph)
    all_globals_get_calls = find_all_globals_get_calls(tree)

    classification, unknown_targets = classify_generations(
        graph["all_defs"], graph["active_binding"], graph["visited"], TZ3_FUNCTIONS)

    functions = {}
    dead_code_inventory = {}
    for name in TZ3_FUNCTIONS:
        gens_out = []
        any_violation = False
        for key, entry in classification.items():
            if not key.startswith(name + "@"):
                continue
            node = entry["node"]
            bare = scan_bare_clock_calls(node)
            utc_sub = scan_utc_as_kyiv_substitute(node)
            w3_usage = scan_w3_contract_usage(node)
            explicit_kyiv = scan_explicit_kyiv_construction(node)
            touches_clock = scan_touches_clock_at_all(node)
            no_host_dep = bool(w3_usage) or bool(explicit_kyiv)
            host_dep_violation = touches_clock and not no_host_dep
            is_reachable = entry["classification"] in ("ACTIVE_REACHABLE", "SHADOWED_BUT_REACHABLE")
            violates = bool(bare) or bool(utc_sub) or (is_reachable and host_dep_violation)
            if is_reachable and violates:
                any_violation = True
            gens_out.append({
                "lineno": node.lineno, "end_lineno": node.end_lineno,
                "kind": "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
                "classification": entry["classification"],
                "bare_clock_calls": bare, "utc_as_kyiv_substitute": utc_sub,
                "w3_contract_usage": w3_usage, "explicit_kyiv_construction": explicit_kyiv,
                "touches_clock": touches_clock,
                "no_host_dependency": no_host_dep if touches_clock else "not_applicable",
                "gates": is_reachable, "violates": violates if is_reachable else None,
            })
            if not is_reachable:
                dead_code_inventory[key] = {"bare_clock_calls": bare,
                                            "note": "DEAD_UNREACHABLE -- informational only"}
        functions[name] = {"generations": gens_out, "active_ok": not any_violation}

    helper_results = {}
    any_helper_violation = False
    for (nm, ln), info in graph["visited"].items():
        if nm in TZ3_FUNCTIONS:
            continue
        node = next((d for d in graph["all_defs"].get(nm, []) if d.lineno == ln), None)
        if node is None:
            continue
        bare = scan_bare_clock_calls(node)
        violates = bool(bare)
        if violates:
            any_helper_violation = True
        helper_results[f"{nm}@{ln}"] = {
            "reached_via": info["via"], "reached_from": info["from"],
            "kind": "async" if isinstance(node, ast.AsyncFunctionDef) else "sync",
            "bare_clock_calls": bare, "violates": violates}

    # ROUND 4: dead-helper inventory extended to EVERY module-level def, not just the
    # TZ-3-named ones (closes the R3-review's probe-H completeness observation:
    # a dead non-TZ3 helper used to be entirely invisible -- neither reachable-scanned
    # nor dead-inventoried). Informational only; never gates.
    dead_helper_inventory = {}
    for name, defs in graph["all_defs"].items():
        if name in TZ3_FUNCTIONS:
            continue
        for d in defs:
            key = (d.name, d.lineno)
            if key in graph["visited"]:
                continue
            bare = scan_bare_clock_calls(d)
            if bare:
                dead_helper_inventory[f"{name}@{d.lineno}"] = {
                    "bare_clock_calls": bare, "note": "DEAD_UNREACHABLE -- informational only"}

    unresolved_edges = graph["unresolved_reachable_edges"]
    fixture_ok = all(f["ok"] for f in fixtures)
    async_alias_ok = all(f["ok"] for f in async_alias_fixtures)
    local_alias_fixture_ok = all(f["ok"] for f in local_alias_fixtures)
    parameter_callback_fixture_ok = all(f["ok"] for f in parameter_callback_fixtures)

    # ROUND 4: call-inventory totals over the WHOLE reachable closure (every node in
    # graph["visited"], not just the 3 TZ-3-named functions) -- section 7/11.
    all_calls = [c for calls in graph["node_call_inventory"].values() for c in calls]
    resolved_call_total = sum(1 for c in all_calls if c["category"] == "RESOLVED_CALL")
    safe_external_call_total = sum(1 for c in all_calls
                                   if c["category"] in ("RESOLVED_BUILTIN", "RESOLVED_IMPORT"))
    unresolved_call_total = sum(1 for c in all_calls if c["category"] in (
        "UNRESOLVED_LOCAL_ALIAS", "UNRESOLVED_PARAMETER_CALLBACK", "UNRESOLVED_ATTRIBUTE_CALL",
        "UNRESOLVED_SUBSCRIPT_CALL", "UNRESOLVED_DYNAMIC_CALL"))
    ambiguous_call_total = sum(1 for c in all_calls if c["category"] == "AMBIGUOUS_CALL")
    reachable_call_total = len(all_calls)
    omitted_call_total = (completeness["total_ast_calls_in_reachable_nodes"]
                          - completeness["total_classified_calls_in_reachable_nodes"])
    arithmetic_ok = (reachable_call_total == resolved_call_total + safe_external_call_total
                     + unresolved_call_total + ambiguous_call_total)

    local_alias_inventory = graph["local_alias_inventory"]
    local_alias_total = len(local_alias_inventory)
    reachable_local_alias_total = sum(1 for a in local_alias_inventory if a["reachable"])
    dead_local_alias_total = local_alias_total - reachable_local_alias_total
    parameter_callback_total = sum(1 for c in all_calls
                                   if c["category"] == "UNRESOLVED_PARAMETER_CALLBACK")
    attribute_call_total = sum(1 for c in all_calls if c["func_kind"] == "Attribute")
    subscript_call_total = sum(1 for c in all_calls if c["func_kind"] == "Subscript")

    # ROUND 6 introduced this inventory over `element_binding_log`; ROUND 7 (closes
    # the R6-review's C-1/C-2/C-5 findings) fixes two defects in how it was reported:
    # (1) a def analyzed more than once during a single run (see `_current_fn_key`'s
    #     docstring -- `_get_python_processes` is analyzed once via the main BFS and
    #     again via `_accumulator_element_family`'s on-demand pass) logs the SAME
    #     source target twice; round 6 reported these raw LOG-ENTRY counts as if they
    #     were unique target counts. Round 7 deduplicates by a stable identity
    #     (containing def instance + this binding's own AST span + kind, task
    #     section 6) and reports BOTH numbers separately, never conflated.
    # (2) round 6's "unresolved := total - proven - ambiguous" was a tautology --
    #     both sides partition the SAME list, so the resulting "arithmetic_ok" could
    #     never be False. Round 7 replaces it with a GENUINE, falsifiable check:
    #     the deduplicated loop/comprehension totals are cross-checked against an
    #     INDEPENDENT ast.walk over the reachable closure (`_independent_reachable_
    #     target_sites`), which never calls bind_for_targets()/
    #     classify_iterable_element() -- two sources of truth, not one identity.
    element_log = graph["element_binding_log"]
    analysis_log_entry_total = len(element_log)

    _unique_by_identity: dict = {}
    duplicate_analysis_disagreements = []
    for e in element_log:
        ident = e["identity"]
        if ident in _unique_by_identity:
            prev = _unique_by_identity[ident]
            if (prev["category"], prev["diagnostic"]) != (e["category"], e["diagnostic"]):
                duplicate_analysis_disagreements.append({
                    "identity": ident,
                    "prev": {"category": prev["category"], "diagnostic": prev["diagnostic"]},
                    "new": {"category": e["category"], "diagnostic": e["diagnostic"]}})
        else:
            _unique_by_identity[ident] = e
    unique_targets = list(_unique_by_identity.values())
    unique_target_total = len(unique_targets)
    repeated_analysis_entry_total = analysis_log_entry_total - unique_target_total
    duplicate_analysis_disagreement_total = len(duplicate_analysis_disagreements)

    # A target's containing def may not itself be in the BFS-reachable closure
    # (`graph["visited"]`): an ON-DEMAND proof (`_function_return_safe`,
    # `_accumulator_element_family`) walks a CALLEE's body to prove something for a
    # reachable CALLER, even when that callee is not itself reachable (e.g.
    # `_parse_ids` -- called nowhere in the reachable closure, but still walked
    # on-demand while proving an unrelated caller's return safety). Those targets are
    # real and correctly logged, but they are not part of "the reachable closure's
    # loop/comprehension targets", so the independent AST cross-check below (which
    # only visits `graph["visited"]`) is scoped to the IN-CLOSURE subset; off-closure
    # targets are reported separately, never silently folded into either total.
    unique_in_closure = [e for e in unique_targets if e["fn_key"] in graph["visited"]]
    unique_off_closure = [e for e in unique_targets if e["fn_key"] not in graph["visited"]]
    off_closure_unique_target_total = len(unique_off_closure)

    unique_loop_target_total = sum(1 for e in unique_in_closure if e["kind"] == "loop")
    unique_comprehension_target_total = sum(1 for e in unique_in_closure if e["kind"] == "comprehension")
    # "proven" includes RESOLVED_CALL -- a loop/comprehension target CAN legitimately
    # resolve to a specific project-function target (e.g. `for fn in [helper]:
    # fn()`, which is exactly how the correct fail-CLOSED path enqueues `helper` into
    # the reachability BFS/clock-scan). Ambiguous is its own explicit category;
    # unresolved is every remaining category -- a genuine per-category PREDICATE for
    # each of the three buckets, not one bucket defined as the leftover of the other
    # two (see the coverage cross-check below for the actual falsifiable check).
    # Computed over the SAME in-closure subset as the loop/comprehension totals above
    # (not over all unique targets) so unique_loop_target_total +
    # unique_comprehension_target_total == unique_proven_target_total +
    # unique_unresolved_target_total + unique_ambiguous_target_total stays a REAL,
    # checkable identity over one consistently-scoped set -- rather than silently
    # mixing in the off-closure target and breaking that correspondence.
    _ELEMENT_PROVEN_CATEGORIES = SAFE_CALL_CATEGORIES + ("LOCAL_DICT_LITERAL",)
    unique_proven_target_total = sum(1 for e in unique_in_closure
                                     if e["category"] in _ELEMENT_PROVEN_CATEGORIES)
    unique_ambiguous_target_total = sum(1 for e in unique_in_closure
                                        if e["category"] == "AMBIGUOUS_CALL")
    unique_unresolved_target_total = sum(
        1 for e in unique_in_closure
        if e["category"] not in _ELEMENT_PROVEN_CATEGORIES and e["category"] != "AMBIGUOUS_CALL")

    # ROUND 7: the independent AST walk only knows, per SITE, whether its target is a
    # bare Name (always exactly one binding) -- it cannot predict how many bindings a
    # Tuple/List unpack site produces (0, 1 or 2, depending on the iterable shape;
    # see bind_for_targets). The 1:1 comparison is therefore restricted to the
    # NAME-target subset on both sides; tuple-derived unique targets are counted in
    # unique_loop_target_total/unique_comprehension_target_total above but excluded
    # from this specific cross-check by design, not by coincidence.
    unique_name_target_loop_total = sum(
        1 for e in unique_in_closure if e["kind"] == "loop" and e["target_is_direct_name"])
    unique_name_target_comprehension_total = sum(
        1 for e in unique_in_closure if e["kind"] == "comprehension" and e["target_is_direct_name"])
    unique_tuple_target_total = sum(1 for e in unique_in_closure if not e["target_is_direct_name"])

    independent_sites = _independent_reachable_target_sites(graph)
    independent_name_loop_target_total = sum(
        1 for s in independent_sites if s["kind"] == "loop" and s["target_is_name"])
    independent_name_comprehension_target_total = sum(
        1 for s in independent_sites if s["kind"] == "comprehension" and s["target_is_name"])
    independent_non_name_target_site_total = sum(1 for s in independent_sites if not s["target_is_name"])
    unique_target_coverage_ok = (
        unique_name_target_loop_total == independent_name_loop_target_total
        and unique_name_target_comprehension_total == independent_name_comprehension_target_total
        and duplicate_analysis_disagreement_total == 0)

    # The two diagnostics ROUND 5 used for its unsound "container is safe, therefore
    # the element is safe" rule -- deleted from this file's code entirely (see
    # section 6b); counted here as a live, re-checked-every-run assertion, not merely
    # a claim, that the pattern the R5 review found no longer occurs.
    _R5_UNSOUND_ELEMENT_DIAGNOSTICS = (
        "PROVEN_LOOP_ELEMENT_OF_SAFE_CONTAINER", "PROVEN_COMPREHENSION_ELEMENT_OF_SAFE_CONTAINER")
    safe_container_inheritance_total = sum(
        1 for e in unique_targets if e["diagnostic"] in _R5_UNSOUND_ELEMENT_DIAGNOSTICS)

    ok = (
        all(f.get("active_ok", False) for f in functions.values())
        and not any_helper_violation
        and not unknown_targets
        and not unresolved_edges
        and completeness["ok"]
        and fixture_ok
        and async_alias_ok
        and local_alias_fixture_ok
        and parameter_callback_fixture_ok
        and not graph["node_budget_hit"]
        and omitted_call_total == 0
        and arithmetic_ok
        and unresolved_call_total == 0
        and ambiguous_call_total == 0
        and unique_target_coverage_ok
        and safe_container_inheritance_total == 0
    )

    return {
        "tz3_functions": functions,
        "frozen_function_set": list(TZ3_FUNCTIONS),
        "helper_nodes_reachable": helper_results,
        "dead_code_inventory": dead_code_inventory,
        "dead_helper_inventory": dead_helper_inventory,
        "unknown_targets": unknown_targets,
        "unresolved_reachable_edges": unresolved_edges,
        "roots": graph["roots"],
        "root_evidence": graph["root_evidence"],
        "escape_watch": graph["escape_watch"],
        "edge_log": graph["edge_log"],
        "captures": graph["captures"],
        "aliases": graph["aliases"],
        "completeness": completeness,
        "fixture_tests": fixtures,
        "async_alias_fixture_tests": async_alias_fixtures,
        "local_alias_fixture_tests": local_alias_fixtures,
        "parameter_callback_fixture_tests": parameter_callback_fixtures,
        "node_budget_hit": graph["node_budget_hit"],
        "all_globals_get_total": len(all_globals_get_calls),
        "module_level_globals_get_total": len(graph["captures"]),
        "nested_globals_get_total": len(all_globals_get_calls) - len(graph["captures"]),
        "reachable_graph_node_total": len(graph["visited"]),
        "frozen_target_total": len(TZ3_FUNCTIONS),
        "unresolved_edge_total": len(unresolved_edges),
        # ROUND 4 required report fields (task spec section 11):
        "reachable_call_total": reachable_call_total,
        "resolved_call_total": resolved_call_total,
        "safe_external_call_total": safe_external_call_total,
        "unresolved_call_total": unresolved_call_total,
        "ambiguous_call_total": ambiguous_call_total,
        "omitted_call_total": omitted_call_total,
        "arithmetic_ok": arithmetic_ok,
        "local_alias_total": local_alias_total,
        "reachable_local_alias_total": reachable_local_alias_total,
        "dead_local_alias_total": dead_local_alias_total,
        "parameter_callback_total": parameter_callback_total,
        "attribute_call_total": attribute_call_total,
        "subscript_call_total": subscript_call_total,
        "local_alias_inventory": local_alias_inventory,
        "imported_module_names": graph["imported_module_names"],
        "imported_from_names": graph["imported_from_names"],
        # ROUND 6 introduced this inventory (task section 9); ROUND 7 (task section 6)
        # reports analysis-log-entry counts and unique-target counts as two SEPARATE
        # concepts, cross-checked against an independent AST walk (never against
        # each other's own subtraction):
        "element_binding_log": element_log,
        "analysis_log_entry_total": analysis_log_entry_total,
        "unique_target_total": unique_target_total,
        "repeated_analysis_entry_total": repeated_analysis_entry_total,
        "duplicate_analysis_disagreement_total": duplicate_analysis_disagreement_total,
        "duplicate_analysis_disagreements": duplicate_analysis_disagreements,
        # in-closure ("this def is in graph['visited']") vs off-closure ("this def was
        # only ever seen via an on-demand proof of a DIFFERENT, reachable caller") --
        # see the comment above unique_in_closure for why these are kept separate.
        "unique_in_closure_target_total": len(unique_in_closure),
        "off_closure_unique_target_total": off_closure_unique_target_total,
        "off_closure_unique_targets": [
            {"fn_key": e["fn_key"], "kind": e["kind"], "name": e["name"],
             "target_span": e["target_span"], "category": e["category"]}
            for e in unique_off_closure],
        "unique_loop_target_total": unique_loop_target_total,
        "unique_comprehension_target_total": unique_comprehension_target_total,
        "unique_proven_target_total": unique_proven_target_total,
        "unique_unresolved_target_total": unique_unresolved_target_total,
        "unique_ambiguous_target_total": unique_ambiguous_target_total,
        "independent_name_loop_target_total": independent_name_loop_target_total,
        "independent_name_comprehension_target_total": independent_name_comprehension_target_total,
        "independent_non_name_target_site_total": independent_non_name_target_site_total,
        "unique_name_target_loop_total": unique_name_target_loop_total,
        "unique_name_target_comprehension_total": unique_name_target_comprehension_total,
        "unique_tuple_target_total": unique_tuple_target_total,
        "unique_target_coverage_ok": unique_target_coverage_ok,
        "safe_container_inheritance_total": safe_container_inheritance_total,
        "ok": ok,
    }


# ---- required fixtures (globals().get, round 3, unchanged) ------------------------

_FIXTURES = [
    ("one_argument_globals_get",
     'X = globals().get("foo")\n',
     {"detected": True, "captured_name": "foo", "fallback_code": None}),
    ("two_argument_globals_get",
     'X = globals().get("foo", bar)\n',
     {"detected": True, "captured_name": "foo", "fallback_code": "bar"}),
    ("non_literal_key",
     'X = globals().get(some_var)\n',
     {"detected": True, "captured_name": None, "key_is_literal": False}),
    ("dictionary_get_not_globals",
     'X = some_dict.get("foo")\n',
     {"detected": False}),
    ("nested_call_as_key",
     'X = globals().get(compute_key())\n',
     {"detected": True, "captured_name": None, "key_is_literal": False}),
    ("missing_fallback_explicit_none",
     'X = globals().get("foo", None)\n',
     {"detected": True, "captured_name": "foo", "fallback_code": "None"}),
    ("dynamic_fallback",
     'X = globals().get("foo", compute_fallback())\n',
     {"detected": True, "captured_name": "foo", "fallback_code": "compute_fallback()"}),
]


def run_globals_get_fixture_tests() -> list[dict]:
    results = []
    for fixture_name, src, expected in _FIXTURES:
        tree = ast.parse(src)
        caps = find_globals_get_captures(tree)
        detected = bool(caps)
        ok = detected == expected["detected"]
        detail = {"detected": detected}
        if detected and "captured_name" in expected:
            detail["captured_name"] = caps[0]["captured_name"]
            ok = ok and caps[0]["captured_name"] == expected["captured_name"]
        if detected and "key_is_literal" in expected:
            detail["key_is_literal"] = caps[0]["key_is_literal"]
            ok = ok and caps[0]["key_is_literal"] == expected["key_is_literal"]
        if detected and "fallback_code" in expected:
            detail["fallback_code"] = caps[0]["fallback_code"]
            ok = ok and caps[0]["fallback_code"] == expected["fallback_code"]
        results.append({"fixture": fixture_name, "src": src.strip(), "ok": ok,
                        "expected": expected, "observed": detail})
    return results


# ---- required fixtures (async support + alias resolution, round 3, unchanged) -----

def _graph_visited_keys(src: str, tz3_names):
    tree = ast.parse(src)
    graph = build_reachability_graph(tree, tz3_names=tz3_names)
    return graph, set(graph["visited"].keys())


def _def_lineno(tree: ast.Module, name: str, occurrence: int = 0) -> int:
    lns = [n.lineno for n in tree.body if isinstance(n, DEF_TYPES) and n.name == name]
    return lns[occurrence]


def run_async_alias_fixture_tests() -> list[dict]:
    results = []

    def rec(fid, ok, detail):
        results.append({"fixture": fid, "ok": ok, "detail": detail})

    # ---- ASYNC ----
    try:
        src = (
            "def _fx_dispatcher():\n"
            "    _fx_root()\n\n"
            "def _fx_root():\n"
            "    _fx_async_helper()\n\n"
            "async def _fx_async_helper():\n"
            "    return 1\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_fx_root",))
        target = ("_fx_async_helper", _def_lineno(tree, "_fx_async_helper"))
        ok = target in visited and not graph["unresolved_reachable_edges"]
        rec("async_reachable_helper", ok, {"visited": sorted(visited), "expected": target})
    except Exception as exc:
        rec("async_reachable_helper", False, {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _fx_dispatcher2():\n"
            "    _fx_root2()\n\n"
            "def _fx_root2():\n"
            "    pass\n\n"
            "async def _fx_dead_async():\n"
            "    return 1\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_fx_root2",))
        dead = ("_fx_dead_async", _def_lineno(tree, "_fx_dead_async"))
        ok = dead not in visited
        rec("async_dead_unrelated_helper", ok, {"visited": sorted(visited), "dead": dead})
    except Exception as exc:
        rec("async_dead_unrelated_helper", False, {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _fx_dispatcher3():\n"
            "    _fx_h()\n\n"
            "async def _fx_h():\n"
            "    return 1\n\n"
            "_FX_ORIG_H = globals().get('_fx_h')\n\n"
            "async def _fx_h():\n"
            "    return _FX_ORIG_H()\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_fx_h",))
        shadowed = ("_fx_h", _def_lineno(tree, "_fx_h", 0))
        active = ("_fx_h", _def_lineno(tree, "_fx_h", 1))
        shadowed_reached = shadowed in visited
        active_reached = active in visited
        ok = shadowed_reached and active_reached and not graph["unresolved_reachable_edges"]
        rec("async_shadowed_captured_reachable", ok,
            {"shadowed_reached": shadowed_reached, "active_reached": active_reached,
             "visited": sorted(visited)})
    except Exception as exc:
        rec("async_shadowed_captured_reachable", False, {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _fx_dispatcher4():\n"
            "    _fx_wrapper()\n\n"
            "async def _fx_wrapper():\n"
            "    return _fx_sync_body()\n\n"
            "def _fx_sync_body():\n"
            "    return 1\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_fx_wrapper",))
        wrapper = ("_fx_wrapper", _def_lineno(tree, "_fx_wrapper"))
        body = ("_fx_sync_body", _def_lineno(tree, "_fx_sync_body"))
        ok = wrapper in visited and body in visited and not graph["unresolved_reachable_edges"]
        rec("async_wrapper_delegates_to_sync", ok, {"visited": sorted(visited),
                                                     "expected": [wrapper, body]})
    except Exception as exc:
        rec("async_wrapper_delegates_to_sync", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # ---- ALIAS (module-level) ----
    try:
        src = (
            "def _al_dispatcher():\n"
            "    _al_root()\n\n"
            "def _al_target():\n"
            "    return 1\n\n"
            "_al_x = _al_target\n\n"
            "def _al_root():\n"
            "    _al_x()\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_al_root",))
        target = ("_al_target", _def_lineno(tree, "_al_target"))
        ok = target in visited and not graph["unresolved_reachable_edges"]
        rec("alias_single", ok, {"visited": sorted(visited), "expected": target})
    except Exception as exc:
        rec("alias_single", False, {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _al_dispatcher2():\n"
            "    _al_root2()\n\n"
            "def _al_target2():\n"
            "    return 1\n\n"
            "_al_a = _al_target2\n"
            "_al_b = _al_a\n\n"
            "def _al_root2():\n"
            "    _al_b()\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_al_root2",))
        target = ("_al_target2", _def_lineno(tree, "_al_target2"))
        ok = target in visited and not graph["unresolved_reachable_edges"]
        rec("alias_chained", ok, {"visited": sorted(visited), "expected": target})
    except Exception as exc:
        rec("alias_chained", False, {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _al_dispatcher3():\n"
            "    _al_root3()\n\n"
            "def _al_h():\n"
            "    return 1\n\n"
            "_AL_ORIG = globals().get('_al_h')\n"
            "_al_alias_to_capture = _AL_ORIG\n\n"
            "def _al_h():\n"
            "    return 2\n\n"
            "def _al_root3():\n"
            "    _al_alias_to_capture()\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_al_root3",))
        shadowed = ("_al_h", _def_lineno(tree, "_al_h", 0))
        ok = shadowed in visited and not graph["unresolved_reachable_edges"]
        rec("alias_to_captured_shadowed_def", ok, {"visited": sorted(visited), "expected": shadowed})
    except Exception as exc:
        rec("alias_to_captured_shadowed_def", False, {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _al_dispatcher4():\n"
            "    _al_root4()\n\n"
            "_al_c2 = _al_c1\n"
            "_al_c1 = _al_c2\n\n"
            "def _al_root4():\n"
            "    _al_c1()\n"
        )
        tree = ast.parse(src)
        graph = build_reachability_graph(tree, tz3_names=("_al_root4",))
        cyc = [e for e in graph["unresolved_reachable_edges"] if e["diagnostic"] == "ALIAS_CYCLE"]
        ok = bool(cyc)
        rec("alias_cycle", ok, {"unresolved_edges": graph["unresolved_reachable_edges"]})
    except Exception as exc:
        rec("alias_cycle", False, {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _al_dispatcher5():\n"
            "    _al_root5()\n\n"
            "def _al_h2():\n"
            "    return 1\n\n"
            "_al_y = globals().get('_al_h2')\n\n"
            "def _al_h2():\n"
            "    return 2\n\n"
            "def _al_root5():\n"
            "    _al_y()\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_al_root5",))
        gen1 = ("_al_h2", _def_lineno(tree, "_al_h2", 0))
        gen2 = ("_al_h2", _def_lineno(tree, "_al_h2", 1))
        ok = (gen1 in visited and gen2 not in visited
              and not graph["unresolved_reachable_edges"])
        rec("alias_overwritten_later_preserves_old_capture", ok,
            {"visited": sorted(visited), "expected_reachable": gen1, "expected_dead": gen2})
    except Exception as exc:
        rec("alias_overwritten_later_preserves_old_capture", False,
            {"exception": f"{type(exc).__name__}: {exc}"})

    try:
        src = (
            "def _al_dispatcher6():\n"
            "    _al_root6()\n\n"
            "async def _al_ah():\n"
            "    return 1\n\n"
            "_al_alias_async = _al_ah\n\n"
            "def _al_root6():\n"
            "    _al_alias_async()\n"
        )
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=("_al_root6",))
        target = ("_al_ah", _def_lineno(tree, "_al_ah"))
        ok = target in visited and not graph["unresolved_reachable_edges"]
        rec("alias_to_async_definition", ok, {"visited": sorted(visited), "expected": target})
    except Exception as exc:
        rec("alias_to_async_definition", False, {"exception": f"{type(exc).__name__}: {exc}"})

    return results


# ---- ROUND 4 required fixtures: local-alias dataflow (task spec section 3) --------

def run_local_alias_fixture_tests() -> list[dict]:
    """Required local-alias patterns (section 3): simple assignment, chained aliases,
    aliases defined before use, aliases inside try/except branches, aliases inside if
    branches, rebinding, deletion/reassignment where statically visible -- each proven
    against the LIVE round-4 implementation."""
    results = []

    def rec(fid, ok, detail):
        results.append({"fixture": fid, "ok": ok, "detail": detail})

    def _reachable(src, root_name, target_name, target_occurrence=0):
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=(root_name,))
        target = (target_name, _def_lineno(tree, target_name, target_occurrence))
        return target in visited, graph, target

    # LA1: simple local alias, used directly.
    try:
        src = ("def _la_dispatch1():\n    _la_root()\n\n"
               "def _la_root():\n    _la_x = _la_helper\n    _la_x()\n\n"
               "def _la_helper():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root", "_la_helper")
        rec("local_alias_simple", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("local_alias_simple", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # LA2: chained local aliases (2 hops).
    try:
        src = ("def _la_dispatch2():\n    _la_root2()\n\n"
               "def _la_root2():\n    _la_a = _la_helper2\n    _la_b = _la_a\n    _la_b()\n\n"
               "def _la_helper2():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root2", "_la_helper2")
        rec("local_alias_chained", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("local_alias_chained", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # LA3: alias defined, then used later in the SAME block (before-use ordering).
    try:
        src = ("def _la_dispatch3():\n    _la_root3()\n\n"
               "def _la_root3():\n    _la_c = _la_helper3\n    _x = 1\n    _y = 2\n    _la_c()\n\n"
               "def _la_helper3():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root3", "_la_helper3")
        rec("local_alias_defined_before_use", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("local_alias_defined_before_use", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # LA4: alias inside an if-branch, USED inside the same branch -> resolves normally.
    try:
        src = ("def _la_dispatch4():\n    _la_root4(True)\n\n"
               "def _la_root4(flag):\n"
               "    if flag:\n"
               "        _la_d = _la_helper4\n"
               "        _la_d()\n\n"
               "def _la_helper4():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root4", "_la_helper4")
        rec("local_alias_inside_if_branch_same_branch_use", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("local_alias_inside_if_branch_same_branch_use", False,
            {"exception": f"{type(exc).__name__}: {exc}"})

    # LA5: alias inside try body, used inside the same try body.
    try:
        src = ("def _la_dispatch5():\n    _la_root5()\n\n"
               "def _la_root5():\n"
               "    try:\n"
               "        _la_e = _la_helper5\n"
               "        _la_e()\n"
               "    except Exception:\n"
               "        pass\n\n"
               "def _la_helper5():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root5", "_la_helper5")
        rec("local_alias_inside_try_branch", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("local_alias_inside_try_branch", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # LA6: unconditional rebinding (sequential, same block) -> deterministic latest wins.
    try:
        src = ("def _la_dispatch6():\n    _la_root6()\n\n"
               "def _la_root6():\n"
               "    _la_f = _la_safe6\n"
               "    _la_f = _la_unsafe6\n"
               "    _la_f()\n\n"
               "def _la_safe6():\n    return 1\n\n"
               "def _la_unsafe6():\n    return 2\n")
        ok, graph, target = _reachable(src, "_la_root6", "_la_unsafe6")
        safe_ok, _, _ = _reachable(src, "_la_root6", "_la_safe6")
        rec("local_alias_unconditional_rebinding_latest_wins",
            ok and not safe_ok and not graph["unresolved_reachable_edges"],
            {"unsafe_reached": ok, "safe_reached": safe_ok})
    except Exception as exc:
        rec("local_alias_unconditional_rebinding_latest_wins", False,
            {"exception": f"{type(exc).__name__}: {exc}"})

    # LA7: conditional rebinding (if/else bind DIFFERENT targets) -> AMBIGUOUS/RED.
    try:
        src = ("def _la_dispatch7():\n    _la_root7(True)\n\n"
               "def _la_root7(flag):\n"
               "    if flag:\n"
               "        _la_g = _la_safe7\n"
               "    else:\n"
               "        _la_g = _la_unsafe7\n"
               "    _la_g()\n\n"
               "def _la_safe7():\n    return 1\n\n"
               "def _la_unsafe7():\n    return 2\n")
        tree = ast.parse(src)
        graph = build_reachability_graph(tree, tz3_names=("_la_root7",))
        amb = [e for e in graph["unresolved_reachable_edges"]
               if e["diagnostic"] == "LOCAL_REBINDING_AMBIGUOUS"]
        rec("local_alias_conditional_rebinding_ambiguous", bool(amb), {"count": len(amb)})
    except Exception as exc:
        rec("local_alias_conditional_rebinding_ambiguous", False,
            {"exception": f"{type(exc).__name__}: {exc}"})

    # LA8: deletion -- a deleted alias, used afterward, must NOT resolve to the OLD def.
    try:
        src = ("def _la_dispatch8():\n    _la_root8()\n\n"
               "def _la_root8():\n"
               "    _la_h = _la_helper8\n"
               "    del _la_h\n"
               "    _la_h()\n\n"
               "def _la_helper8():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root8", "_la_helper8")
        rec("local_alias_deletion_invalidates", not ok, {"still_reached_after_delete": ok})
    except Exception as exc:
        rec("local_alias_deletion_invalidates", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # LA9: if/else BOTH branches bind the SAME target -> resolves cleanly (not ambiguous).
    try:
        src = ("def _la_dispatch9():\n    _la_root9(True)\n\n"
               "def _la_root9(flag):\n"
               "    if flag:\n"
               "        _la_i = _la_helper9\n"
               "    else:\n"
               "        _la_i = _la_helper9\n"
               "    _la_i()\n\n"
               "def _la_helper9():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root9", "_la_helper9")
        rec("local_alias_if_else_same_target_not_ambiguous",
            ok and not graph["unresolved_reachable_edges"], {"target": target})
    except Exception as exc:
        rec("local_alias_if_else_same_target_not_ambiguous", False,
            {"exception": f"{type(exc).__name__}: {exc}"})

    # LA10: async function using a local alias to an unsafe helper (mirrors F2B-15).
    try:
        src = ("def _la_dispatch10():\n    _la_root10()\n\n"
               "def _la_root10():\n    _la_j()\n\n"
               "async def _la_j():\n"
               "    _la_k = _la_unsafe10\n"
               "    _la_k()\n\n"
               "def _la_unsafe10():\n    return 1\n")
        ok, graph, target = _reachable(src, "_la_root10", "_la_unsafe10")
        rec("local_alias_in_async_function", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("local_alias_in_async_function", False, {"exception": f"{type(exc).__name__}: {exc}"})

    return results


# ---- ROUND 4 required fixtures: parameter callbacks (task spec section 4) ---------

def run_parameter_callback_fixture_tests() -> list[dict]:
    """Required parameter-callback patterns (section 4): safe known helper, unsafe
    fallback helper, callsite unknown, two wrapper levels, rebound locally, async
    callback parameter."""
    results = []

    def rec(fid, ok, detail):
        results.append({"fixture": fid, "ok": ok, "detail": detail})

    def _reachable(src, root_name, target_name, target_occurrence=0):
        tree = ast.parse(src)
        graph, visited = _graph_visited_keys(src, tz3_names=(root_name,))
        target = (target_name, _def_lineno(tree, target_name, target_occurrence))
        return target in visited, graph, target

    # PC1: callback passed a safe known helper -- proven, resolved, GREEN.
    try:
        src = ("def _pc_dispatch1():\n    _pc_root()\n\n"
               "def _pc_root():\n    _pc_wrap(_pc_safe1)\n\n"
               "def _pc_wrap(cb):\n    cb()\n\n"
               "def _pc_safe1():\n    return 1\n")
        ok, graph, target = _reachable(src, "_pc_root", "_pc_safe1")
        rec("parameter_callback_safe_known_helper", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("parameter_callback_safe_known_helper", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # PC2: callback passed an unsafe fallback helper -- proven, resolved, RED (reached).
    try:
        src = ("def _pc_dispatch2():\n    _pc_root2()\n\n"
               "def _pc_root2():\n    _pc_wrap2(_pc_unsafe2)\n\n"
               "def _pc_wrap2(cb):\n    cb()\n\n"
               "def _pc_unsafe2():\n    return 1\n")
        ok, graph, target = _reachable(src, "_pc_root2", "_pc_unsafe2")
        rec("parameter_callback_unsafe_helper_proven", ok and not graph["unresolved_reachable_edges"],
            {"target": target})
    except Exception as exc:
        rec("parameter_callback_unsafe_helper_proven", False,
            {"exception": f"{type(exc).__name__}: {exc}"})

    # PC3: callback callsite unknown -- _pc_wrap3 is NEVER called anywhere, so its
    # parameter cannot be proven; if it were somehow reached, it must fail closed.
    try:
        src = ("def _pc_root3():\n    _pc_wrap3(_pc_helper3)\n    pass\n\n"
               "def _pc_wrap3(cb):\n    cb()\n\n"
               "def _pc_helper3():\n    return 1\n")
        tree = ast.parse(src)
        graph = build_reachability_graph(tree, tz3_names=("_pc_wrap3",))
        # _pc_wrap3 is now itself a "TZ3" root, so its OWN callsite (from _pc_root3) is
        # excluded from the "prove the parameter" search (self is not an external
        # caller of itself in this framing -- but _pc_root3 IS external). Force the
        # unknown-callsite case by making the ONLY callsite pass a non-Name argument.
        src2 = ("def _pc_root3b():\n    _pc_wrap3b(compute_cb())\n\n"
                "def compute_cb():\n    return _pc_helper3b\n\n"
                "def _pc_wrap3b(cb):\n    cb()\n\n"
                "def _pc_helper3b():\n    return 1\n")
        tree2 = ast.parse(src2)
        graph2 = build_reachability_graph(tree2, tz3_names=("_pc_wrap3b",))
        ue = [e for e in graph2["unresolved_reachable_edges"]
              if e.get("call_category") == "UNRESOLVED_PARAMETER_CALLBACK"]
        rec("parameter_callback_callsite_unknown", bool(ue), {"unresolved": len(ue)})
    except Exception as exc:
        rec("parameter_callback_callsite_unknown", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # PC4: callback passed through TWO wrapper levels.
    try:
        src = ("def _pc_dispatch4():\n    _pc_root4()\n\n"
               "def _pc_root4():\n    _pc_w1(_pc_safe4)\n\n"
               "def _pc_w1(cb):\n    _pc_w2(cb)\n\n"
               "def _pc_w2(cb2):\n    cb2()\n\n"
               "def _pc_safe4():\n    return 1\n")
        ok, graph, target = _reachable(src, "_pc_root4", "_pc_safe4")
        rec("parameter_callback_two_wrapper_levels",
            ok and not graph["unresolved_reachable_edges"], {"target": target})
    except Exception as exc:
        rec("parameter_callback_two_wrapper_levels", False,
            {"exception": f"{type(exc).__name__}: {exc}"})

    # PC5: callback rebound locally (param reassigned to something else before use).
    try:
        src = ("def _pc_dispatch5():\n    _pc_root5()\n\n"
               "def _pc_root5():\n    _pc_w5(_pc_safe5)\n\n"
               "def _pc_w5(cb):\n    cb = _pc_unsafe5\n    cb()\n\n"
               "def _pc_safe5():\n    return 1\n\n"
               "def _pc_unsafe5():\n    return 2\n")
        ok_unsafe, graph, target = _reachable(src, "_pc_root5", "_pc_unsafe5")
        ok_safe, _, _ = _reachable(src, "_pc_root5", "_pc_safe5")
        rec("parameter_callback_rebound_locally",
            ok_unsafe and not ok_safe and not graph["unresolved_reachable_edges"],
            {"unsafe_reached": ok_unsafe, "safe_reached": ok_safe})
    except Exception as exc:
        rec("parameter_callback_rebound_locally", False, {"exception": f"{type(exc).__name__}: {exc}"})

    # PC6: async function receiving a callback parameter.
    try:
        src = ("def _pc_dispatch6():\n    _pc_root6()\n\n"
               "def _pc_root6():\n    _pc_a6(_pc_unsafe6)\n\n"
               "async def _pc_a6(cb):\n    cb()\n\n"
               "def _pc_unsafe6():\n    return 1\n")
        ok, graph, target = _reachable(src, "_pc_root6", "_pc_unsafe6")
        rec("parameter_callback_async_function",
            ok and not graph["unresolved_reachable_edges"], {"target": target})
    except Exception as exc:
        rec("parameter_callback_async_function", False, {"exception": f"{type(exc).__name__}: {exc}"})

    return results


def main() -> int:
    result = run_panel_static_gate()

    for f in result["fixture_tests"]:
        check(f"globals().get fixture: {f['fixture']}", f["ok"],
              f"expected={f['expected']} observed={f['observed']}")
    for f in result["async_alias_fixture_tests"]:
        check(f"async/alias fixture: {f['fixture']}", f["ok"], str(f["detail"])[:200])
    for f in result["local_alias_fixture_tests"]:
        check(f"local-alias fixture: {f['fixture']}", f["ok"], str(f["detail"])[:200])
    for f in result["parameter_callback_fixture_tests"]:
        check(f"parameter-callback fixture: {f['fixture']}", f["ok"], str(f["detail"])[:200])

    c = result["completeness"]
    check("completeness: AST def count matches independent regex sweep",
          c["def_count_ok"], f"ast={c['ast_def_count']} regex={c['regex_def_count']}")
    check("completeness: capture count did not collapse to zero",
          not c["capture_zero_regression"],
          f"ast={c['ast_capture_count']} regex={c['regex_capture_count']}")
    check("completeness: AST capture count >= independent regex sweep",
          not c["capture_count_deficit"],
          f"ast={c['ast_capture_count']} regex={c['regex_capture_count']}")
    check("completeness: independent Call-node count matches the classifier's own "
          "count on every reachable node (no silently omitted call)",
          c["call_count_ok"],
          f"ast_calls={c['total_ast_calls_in_reachable_nodes']} "
          f"classified={c['total_classified_calls_in_reachable_nodes']} "
          f"mismatches={c['per_node_call_count_mismatches']}")

    check("report-consistency: all_globals_get_total == module_level + nested",
          result["all_globals_get_total"] ==
          result["module_level_globals_get_total"] + result["nested_globals_get_total"],
          f"all={result['all_globals_get_total']} "
          f"module_level={result['module_level_globals_get_total']} "
          f"nested={result['nested_globals_get_total']}")
    check("report-consistency: module_level_globals_get_total == len(captures fed to "
          "the reachability graph)",
          result["module_level_globals_get_total"] == len(result["captures"]))
    check("report-consistency: frozen_target_total == len(TZ3_FUNCTIONS)",
          result["frozen_target_total"] == len(TZ3_FUNCTIONS))
    check("report-consistency: omitted_call_total == 0",
          result["omitted_call_total"] == 0, f"omitted={result['omitted_call_total']}")
    check("report-consistency: reachable_call_total == resolved + safe_external + "
          "unresolved + ambiguous",
          result["arithmetic_ok"],
          f"reachable={result['reachable_call_total']} resolved={result['resolved_call_total']} "
          f"safe_external={result['safe_external_call_total']} "
          f"unresolved={result['unresolved_call_total']} ambiguous={result['ambiguous_call_total']}")
    check("report-consistency (ROUND 7): unique loop/comprehension target totals "
          "match an INDEPENDENT ast.walk of the reachable closure (not each other's "
          "own subtraction) and no duplicate-analysis entry disagrees with itself",
          result["unique_target_coverage_ok"],
          f"unique_loop={result['unique_loop_target_total']} "
          f"independent_loop={result['independent_name_loop_target_total']} "
          f"unique_comprehension={result['unique_comprehension_target_total']} "
          f"independent_comprehension={result['independent_name_comprehension_target_total']} "
          f"duplicate_disagreements={result['duplicate_analysis_disagreement_total']}")
    check("ROUND 6: no element classified safe via container-inheritance "
          "(the deleted R5 rule)",
          result["safe_container_inheritance_total"] == 0,
          f"count={result['safe_container_inheritance_total']}")
    print(f"       [element inventory, ROUND 7] "
          f"analysis_log_entry_total={result['analysis_log_entry_total']} "
          f"unique_target_total={result['unique_target_total']} "
          f"repeated_analysis_entry_total={result['repeated_analysis_entry_total']} "
          f"unique_loop={result['unique_loop_target_total']} "
          f"unique_comprehension={result['unique_comprehension_target_total']} "
          f"unique_proven={result['unique_proven_target_total']} "
          f"unique_unresolved={result['unique_unresolved_target_total']} "
          f"unique_ambiguous={result['unique_ambiguous_target_total']} "
          f"independent_non_name_target_sites={result['independent_non_name_target_site_total']}")

    print(f"       [totals] all_globals_get_total={result['all_globals_get_total']} "
          f"module_level_globals_get_total={result['module_level_globals_get_total']} "
          f"nested_globals_get_total={result['nested_globals_get_total']} "
          f"reachable_graph_node_total={result['reachable_graph_node_total']} "
          f"frozen_target_total={result['frozen_target_total']} "
          f"unresolved_edge_total={result['unresolved_edge_total']}")
    print(f"       [call totals] reachable_call_total={result['reachable_call_total']} "
          f"resolved_call_total={result['resolved_call_total']} "
          f"safe_external_call_total={result['safe_external_call_total']} "
          f"unresolved_call_total={result['unresolved_call_total']} "
          f"ambiguous_call_total={result['ambiguous_call_total']} "
          f"omitted_call_total={result['omitted_call_total']}")
    print(f"       [alias/param totals] local_alias_total={result['local_alias_total']} "
          f"reachable_local_alias_total={result['reachable_local_alias_total']} "
          f"dead_local_alias_total={result['dead_local_alias_total']} "
          f"parameter_callback_total={result['parameter_callback_total']} "
          f"attribute_call_total={result['attribute_call_total']} "
          f"subscript_call_total={result['subscript_call_total']}")

    for cap in result["captures"]:
        print(f"       [capture] {cap['target']} = globals().get({cap['key_code']}"
              f"{', ' + cap['fallback_code'] if cap['fallback_code'] else ''}) @ line "
              f"{cap['lineno']} -> captured_name={cap['captured_name']!r} "
              f"key_is_literal={cap['key_is_literal']}")

    print(f"       [graph] external roots: {result['roots']}  evidence={result['root_evidence']}")
    print(f"       [graph] reachable nodes: {result['reachable_graph_node_total']}")

    check("no UNKNOWN classification among frozen TZ-3 targets (fail-closed)",
          not result["unknown_targets"], str(result["unknown_targets"]))
    check("reachability BFS did not hit the node budget (graph genuinely converged)",
          not result["node_budget_hit"])
    check("no unresolved edge sourced from a confirmed-reachable node (fail-closed core rule)",
          not result["unresolved_reachable_edges"],
          str(result["unresolved_reachable_edges"])[:800])
    check("no unresolved call anywhere in the reachable closure",
          result["unresolved_call_total"] == 0, f"count={result['unresolved_call_total']}")
    check("no ambiguous call anywhere in the reachable closure",
          result["ambiguous_call_total"] == 0, f"count={result['ambiguous_call_total']}")

    for name, f in result["tz3_functions"].items():
        gens = f["generations"]
        print(f"       {name}: generations="
              f"{[(g['lineno'], g['kind'], g['classification']) for g in gens]}")
        classes = {g["classification"] for g in gens}
        check(f"{name}: at least one ACTIVE_REACHABLE or SHADOWED_BUT_REACHABLE generation exists",
              any(c in ("ACTIVE_REACHABLE", "SHADOWED_BUT_REACHABLE") for c in classes), str(classes))
        for g in gens:
            if not g["gates"]:
                if g["bare_clock_calls"]:
                    print(f"       [info] {name}@{g['lineno']} is {g['classification']} "
                          f"(dead code, not gated) and still contains "
                          f"{g['bare_clock_calls']} -- correctly NOT gated")
                continue
            check(f"{name}@{g['lineno']} ({g['classification']}): ZERO bare clock calls",
                  not g["bare_clock_calls"], str(g["bare_clock_calls"]))
            check(f"{name}@{g['lineno']} ({g['classification']}): no UTC-as-Kyiv substitution",
                  not g["utc_as_kyiv_substitute"], str(g["utc_as_kyiv_substitute"]))
            if g["touches_clock"]:
                check(f"{name}@{g['lineno']} ({g['classification']}): no OS-local dependency "
                      f"(storage.w3_now/w3_tz OR explicit ZoneInfo('Europe/Kyiv'))",
                      g["no_host_dependency"],
                      f"w3_contract={g['w3_contract_usage']} explicit_kyiv={g['explicit_kyiv_construction']}")
            else:
                print(f"       [n/a]  {name}@{g['lineno']} ({g['classification']}): does not "
                      f"touch the clock at all (pure delegator) -- not applicable")
        check(f"{name}: overall active_ok (no violation on any reachable generation)",
              f["active_ok"])

    for key, h in result["helper_nodes_reachable"].items():
        check(f"reachable helper {key} ({h['kind']}, via {h['reached_via']} from "
              f"{h['reached_from']}): no bare (host-local) clock call introduced",
              not h["violates"], f"bare={h['bare_clock_calls']}")

    if result["dead_code_inventory"]:
        print("       [dead-code inventory (TZ-3), informational only, never gates]")
        for key, d in result["dead_code_inventory"].items():
            print(f"         {key}: bare_clock_calls={d['bare_clock_calls']}")
    if result["dead_helper_inventory"]:
        print(f"       [dead-helper inventory, informational only, never gates] "
              f"{len(result['dead_helper_inventory'])} dead helper(s) with a bare clock call")

    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (frozen panel_bot.py fail-closed reachability-graph AND "
          "call-classification static gate green -- every call in the reachable "
          "closure is resolved or explicitly fails closed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
