# -*- coding: utf-8 -*-
"""
admin_ai.index_builder — read-only AST extraction of AdminBot's currently
ACTIVE capability surface (menu keys, callback-data prefixes/exact matches,
and controller /commands) from panel_bot.py and main.py.

Pure stdlib (ast / hashlib / json / dataclasses). Never imports panel_bot,
main, storage, or telethon — this module only ever *reads text* from the
two source files whose paths are passed in by the caller. Standalone
importable; used by:
  - tools\\capability_index_build.py   (CLI that writes capability_index.json)
  - admin_ai\\registry.py              (validates capabilities.json against it)
  - tools\\admin_ai_index_selftest.py  (offline correctness/tamper tests)

TPilot's override style means "last def wins" for ordinary function names
(only the final definition in source order is reachable), but TWO router
functions are COOPERATIVE chains: every layer captures a `*_PREV` reference
to the previous layer and falls through to it, so ALL layers stay live.
Those two names are treated specially — every def is unioned instead of
only the last:
  - _title_for_menu              (panel_bot.py — menu/screen router)
  - _panel_execute_command_text  (main.py — controller /command router)

Extraction targets specific, verified source conventions rather than
generic string-literal harvesting (which would pull in unrelated string
comparisons and pollute the index):
  - Menu keys: within each active `_title_for_menu` def, the router
    variable is always derived at the top via `<var> = str(menu or ...)`
    (seen as both `raw` and `raw_menu` across the 31 defs). We resolve the
    actual local variable name(s) per def rather than hardcoding one.
  - Callback data: every CallbackQuery handler decodes
    `data = (event.data or b"").decode("utf-8", errors="ignore")` before
    comparing/`.startswith()`-ing against the literal `data` name — this
    convention is consistent project-wide.
  - Commands: `_panel_execute_command_text` consistently binds the parsed
    command word to a local named `cmd` (`cmd, _, args = ...partition(" ")`
    or `cmd, args = _parse_cmd(text)`), then does `cmd == "/xxx"` /
    `cmd in (...)` comparisons, plus (separately) module-level dispatch
    dicts (`_PLC_DISPATCH = {"/proxy_pool_list": handler, ...}`).
  - Button callback data: `Button.inline(text, data)` call sites anywhere
    in panel_bot.py's active functions, resolving literal bytes/str,
    `"...".encode()`, and f-string prefixes (up to the first `{...}`).

Everything here is best-effort/heuristic by nature (regex-free but still
pattern-based over an evolving 20k+ line codebase) — the `diagnostics`
block in the returned index always reports counts so drift is visible
rather than silently swallowed.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple, Union

TOOL_VERSION = "1"

# Router functions whose dispatch is COOPERATIVE (every def stays reachable
# via a captured *_PREV chain) rather than "last def wins".
COOPERATIVE_NAMES: FrozenSet[str] = frozenset({
    "_title_for_menu",
    "_panel_execute_command_text",
})

_STR_CONTAINER_TYPES = (ast.Tuple, ast.List, ast.Set)


# --------------------------------------------------------------------------- #
# Low-level source loading / hashing
# --------------------------------------------------------------------------- #
def _read_source(path: Path) -> str:
    # utf-8-sig: several project files carry a UTF-8 BOM (main.py does,
    # panel_bot.py does not) -- this strips it transparently either way.
    return path.read_text(encoding="utf-8-sig")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Active vs. shadowed top-level function defs
# --------------------------------------------------------------------------- #
def module_active_nodes(tree: ast.Module) -> Tuple[List[ast.AST], Dict[str, Any]]:
    """Return (active_function_nodes, diagnostics) for a parsed module.

    Ordinary names: only the LAST def (by source order) is active; earlier
    defs are shadowed and excluded. Names in COOPERATIVE_NAMES: every def
    is active (cooperative PREV-chain dispatch).
    """
    by_name: Dict[str, List[ast.AST]] = {}
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            by_name.setdefault(stmt.name, []).append(stmt)

    active: List[ast.AST] = []
    shadowed = 0
    cooperative_info: Dict[str, Any] = {}
    duplicate_names = 0

    for name, defs in by_name.items():
        defs_sorted = sorted(defs, key=lambda n: n.lineno)
        if len(defs_sorted) > 1:
            duplicate_names += 1
        if name in COOPERATIVE_NAMES:
            active.extend(defs_sorted)
            cooperative_info[name] = {
                "defs": len(defs_sorted),
                "lines": [d.lineno for d in defs_sorted],
            }
        else:
            active.append(defs_sorted[-1])
            shadowed += len(defs_sorted) - 1

    diagnostics = {
        "functions_total": sum(len(v) for v in by_name.values()),
        "functions_unique_names": len(by_name),
        "functions_active": len(active),
        "functions_shadowed": shadowed,
        "duplicate_names": duplicate_names,
        "cooperative": cooperative_info,
    }
    return active, diagnostics


# --------------------------------------------------------------------------- #
# Generic literal-comparison extraction helpers
# --------------------------------------------------------------------------- #
def _is_var(node: ast.AST, names: Iterable[str]) -> bool:
    return isinstance(node, ast.Name) and node.id in names


def _is_str_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _const_str(node: ast.Constant) -> str:
    return node.value


def _iter_str_consts_in_container(node: ast.AST) -> Iterable[str]:
    if _is_str_const(node):
        yield _const_str(node)
        return
    if isinstance(node, _STR_CONTAINER_TYPES):
        for elt in node.elts:
            if _is_str_const(elt):
                yield _const_str(elt)


def iter_compare_literals(func_node: ast.AST, var_names: Set[str]) -> Iterable[Tuple[str, str]]:
    """Yield (literal, match_type) for `<var> == "lit"`, `<var> in (...)`,
    and `<var>.startswith("lit" | (...))` occurring anywhere inside
    func_node, where <var>.id is in var_names. match_type is "exact" or
    "prefix".
    """
    for n in ast.walk(func_node):
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            op = n.ops[0]
            left, right = n.left, n.comparators[0]
            if isinstance(op, ast.Eq):
                if _is_var(left, var_names) and _is_str_const(right):
                    yield _const_str(right), "exact"
                elif _is_var(right, var_names) and _is_str_const(left):
                    yield _const_str(left), "exact"
            elif isinstance(op, ast.In):
                if _is_var(left, var_names):
                    for lit in _iter_str_consts_in_container(right):
                        yield lit, "exact"
        elif isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == "startswith" and _is_var(f.value, var_names):
                for a in n.args:
                    if _is_str_const(a):
                        yield _const_str(a), "prefix"
                    else:
                        for lit in _iter_str_consts_in_container(a):
                            yield lit, "prefix"


# --------------------------------------------------------------------------- #
# Menu-router local-variable resolution (_title_for_menu)
# --------------------------------------------------------------------------- #
def _menu_var_names(func_node: ast.AST) -> Set[str]:
    """Resolve the local variable name(s) that hold the normalized menu key
    inside one `_title_for_menu` def (seen as `raw`, `raw_menu`, ...):
    the function's own parameter name(s), plus any local reassigned via
    `X = str(<expr containing the param>)`. Fixed-point over a few passes
    to catch simple chained reassignment.
    """
    names: Set[str] = set()
    if isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.update(a.arg for a in func_node.args.args)

    for _ in range(3):
        added = False
        for stmt in ast.walk(func_node):
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                callee = stmt.value.func
                if isinstance(callee, ast.Name) and callee.id == "str":
                    if any(isinstance(x, ast.Name) and x.id in names for x in ast.walk(stmt.value)):
                        for t in stmt.targets:
                            if isinstance(t, ast.Name) and t.id not in names:
                                names.add(t.id)
                                added = True
        if not added:
            break
    return names


# --------------------------------------------------------------------------- #
# Command-router local-variable resolution (_panel_execute_command_text)
# --------------------------------------------------------------------------- #
def _cmd_var_names(func_node: ast.AST) -> Set[str]:
    """The controller's command router consistently binds the parsed
    command word to a local named `cmd` (via tuple-unpack from
    `.partition(" ")` or `_parse_cmd(...)`). Always include "cmd" as the
    established convention; also pick up any other target literally named
    "cmd" (covers both unpack shapes without hardcoding either)."""
    names: Set[str] = {"cmd"}
    for stmt in ast.walk(func_node):
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name) and n.id == "cmd":
                        names.add("cmd")
    return names


# --------------------------------------------------------------------------- #
# Module-level dispatch-dict extraction (main.py: `_X_DISPATCH = {"/cmd": h}`)
# --------------------------------------------------------------------------- #
def extract_dispatch_dicts(tree: ast.Module) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Dict):
            dict_name = None
            if stmt.targets and isinstance(stmt.targets[0], ast.Name):
                dict_name = stmt.targets[0].id
            for key in stmt.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.startswith("/"):
                    out.append({"command": key.value, "dict": dict_name or "?", "line": stmt.lineno})
    return out


# --------------------------------------------------------------------------- #
# CallbackQuery(pattern=b"^prefix:") decorator-filter extraction
# --------------------------------------------------------------------------- #
def extract_callbackquery_patterns(tree: ast.Module) -> Set[str]:
    prefixes: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "CallbackQuery":
                for kw in node.keywords:
                    if kw.arg == "pattern" and isinstance(kw.value, ast.Constant):
                        raw = kw.value.value
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "ignore")
                        if isinstance(raw, str):
                            prefixes.add(raw.lstrip("^").rstrip("$"))
    return prefixes


# --------------------------------------------------------------------------- #
# Button.inline(text, data) extraction
# --------------------------------------------------------------------------- #
def _best_effort_str(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for p in node.values:
            if isinstance(p, ast.Constant) and isinstance(p.value, str):
                parts.append(p.value)
            else:
                parts.append("{…}")
        return "".join(parts)
    return "<dynamic>"


def _resolve_button_data(node: ast.AST) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a Button.inline(...) data argument to (value, match_type).
    match_type is "literal" (fully known) or "fstring_prefix" (dynamic
    suffix, only the literal prefix up to the first `{expr}` is known).
    Returns (None, None) if it cannot be resolved at all (skipped)."""
    target = node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "encode":
        target = node.func.value

    if isinstance(target, ast.Constant):
        v = target.value
        if isinstance(v, bytes):
            return v.decode("utf-8", "ignore"), "literal"
        if isinstance(v, str):
            return v, "literal"
        return None, None

    if isinstance(target, ast.JoinedStr):
        prefix = ""
        for part in target.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                prefix += part.value
            else:
                break
        return (prefix, "fstring_prefix") if prefix else (None, None)

    if isinstance(target, ast.BinOp) and isinstance(target.op, ast.Add):
        left, _lt = _resolve_button_data(target.left)
        right, _rt = _resolve_button_data(target.right)
        if left is not None and right is not None:
            return left + right, "literal"

    return None, None


def extract_button_callbacks(active_nodes: List[ast.AST]) -> Tuple[List[Dict[str, Any]], int]:
    out: List[Dict[str, Any]] = []
    skipped = 0
    for func_node in active_nodes:
        func_name = getattr(func_node, "name", "?")
        for n in ast.walk(func_node):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            is_button_inline = (
                isinstance(f, ast.Attribute)
                and f.attr == "inline"
                and isinstance(f.value, ast.Name)
                and f.value.id == "Button"
            )
            if not is_button_inline or len(n.args) < 2:
                continue
            data_val, match_type = _resolve_button_data(n.args[1])
            if data_val is None:
                skipped += 1
                continue
            out.append({
                "text": _best_effort_str(n.args[0]),
                "data": data_val,
                "match_type": match_type,
                "source_function": func_name,
                "line": getattr(n, "lineno", None),
            })
    return out, skipped


# --------------------------------------------------------------------------- #
# Top-level orchestration
# --------------------------------------------------------------------------- #
def build_index(panel_bot_path: Union[str, Path], main_path: Union[str, Path]) -> Dict[str, Any]:
    """Pure/deterministic: same file contents in -> byte-identical output
    (aside from the caller-stamped `built_at`, which this function does
    NOT set -- see tools\\capability_index_build.py). No filesystem writes,
    no imports of panel_bot/main/telethon.
    """
    panel_path = Path(panel_bot_path)
    main_path_ = Path(main_path)

    panel_src = _read_source(panel_path)
    main_src = _read_source(main_path_)

    panel_tree = ast.parse(panel_src, filename=panel_path.name)
    main_tree = ast.parse(main_src, filename=main_path_.name)

    panel_active, panel_diag = module_active_nodes(panel_tree)
    main_active, main_diag = module_active_nodes(main_tree)

    # --- menu keys (panel_bot.py, _title_for_menu, all cooperative defs) ---
    menu_exact: Set[str] = set()
    menu_prefix: Set[str] = set()
    for node in panel_active:
        if getattr(node, "name", None) != "_title_for_menu":
            continue
        var_names = _menu_var_names(node)
        for lit, mtype in iter_compare_literals(node, var_names):
            (menu_exact if mtype == "exact" else menu_prefix).add(lit)

    # --- callback data (panel_bot.py, every active fn except the menu router) ---
    callback_exact: Set[str] = set()
    callback_prefix: Set[str] = set()
    for node in panel_active:
        if getattr(node, "name", None) == "_title_for_menu":
            continue
        for lit, mtype in iter_compare_literals(node, {"data"}):
            (callback_exact if mtype == "exact" else callback_prefix).add(lit)
    callback_prefix |= extract_callbackquery_patterns(panel_tree)

    # --- commands (main.py, _panel_execute_command_text, all cooperative defs) ---
    commands_exact: Set[str] = set()
    commands_prefix: Set[str] = set()
    for node in main_active:
        if getattr(node, "name", None) != "_panel_execute_command_text":
            continue
        cmd_vars = _cmd_var_names(node)
        for lit, mtype in iter_compare_literals(node, cmd_vars):
            if not lit.startswith("/"):
                continue
            (commands_exact if mtype == "exact" else commands_prefix).add(lit)
    dispatch_entries = extract_dispatch_dicts(main_tree)
    for entry in dispatch_entries:
        commands_exact.add(entry["command"])

    # --- Button.inline(text, data) call sites (panel_bot.py, all active fns) ---
    button_callbacks, skipped_dynamic_buttons = extract_button_callbacks(panel_active)

    source_hashes = {
        panel_path.name: _sha256_text(panel_src),
        main_path_.name: _sha256_text(main_src),
    }

    semantic_payload = {
        "menu_keys_exact": sorted(menu_exact),
        "menu_keys_prefix": sorted(menu_prefix),
        "callback_exact": sorted(callback_exact),
        "callback_prefix": sorted(callback_prefix),
        "commands_exact": sorted(commands_exact),
        "commands_prefix": sorted(commands_prefix),
    }
    semantic_hash = hashlib.sha256(
        json.dumps(semantic_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return {
        "tool_version": TOOL_VERSION,
        "built_at": None,  # stamped by the CLI; build_index() itself stays pure
        "source_hashes": source_hashes,
        "semantic_hash": semantic_hash,
        "menu_keys": {"exact": sorted(menu_exact), "prefix": sorted(menu_prefix)},
        "callback_prefixes": {"exact": sorted(callback_exact), "prefix": sorted(callback_prefix)},
        "commands": {
            "exact": sorted(commands_exact),
            "prefix": sorted(commands_prefix),
            "dispatch_dict": dispatch_entries,
        },
        "button_callbacks": button_callbacks,
        "diagnostics": {
            "panel_bot.py": panel_diag,
            "main.py": main_diag,
            "skipped_dynamic_buttons": skipped_dynamic_buttons,
        },
    }
