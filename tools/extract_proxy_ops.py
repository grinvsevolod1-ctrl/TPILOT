"""One-shot R2 extraction: move the proxy buy/renew/pool subsystem out of main.py.

Generates proxy_ops.py from the contiguous subsystem block (roughly
L29123-31742 pre-extraction) and rewrites main.py to import from it.

Behavior-preserving by construction:
  * function bodies are moved VERBATIM (exact source segments, not unparse);
  * constants move verbatim;
  * late-bound main.py dependencies are injected via proxy_ops.bind(globals())
    called at main.py module bottom, so call-time resolution is identical;
  * chain-link functions (_panel_execute_command_text__prevN) and their
    capture assigns stay in main.py untouched.

Run once from repo root: python tools/extract_proxy_ops.py
Idempotence: refuses to run if proxy_ops.py already exists.
"""
from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
OUT = ROOT / "proxy_ops.py"

BLOCK_START = 29123  # start of the STAGE4 spend-safety comment block
BLOCK_END = 31742    # end of _handle_proxy_pool_sync_command

PROXY_KEYS = ("proxy", "_pbuy", "_prenew", "_ppool")

# Names that stay in main.py even though they sit inside the block: the panel
# exec chain links and their delegation captures are executor wiring, not
# proxy logic.
STAY_IN_MAIN_PREFIXES = ("_panel_execute_command_text__prev",)
STAY_IN_MAIN_NAMES = {"_PBUY_PREV_PANEL_EXEC", "_PRENEW_PREV_PANEL_EXEC"}

# Module-level constants/state to move (verbatim), in addition to defs.
MOVE_ASSIGN_NAMES = {
    "PROXY_SELLER_API_KEY",
    "_PBUY_COUNTRY_ALPHA3", "_PBUY_COUNTRY_NAME", "_PBUY_PERIOD_ID",
    "_PBUY_PERIOD_NAME", "_PBUY_PAYMENT_ID", "_PBUY_QUANTITY",
    "_PBUY_PROXY_TYPE", "_PBUY_PROVISION_POLL_ATTEMPTS",
    "_PBUY_PROVISION_POLL_INTERVAL_SEC", "_PBUY_RECOVER_POLL_ATTEMPTS",
    "_PBUY_RECOVER_POLL_INTERVAL_SEC",
    "_PPOOL_AVAILABLE_STATUSES", "_PPOOL_VALID_FILTERS",
    "_PRENEW_AUTORENEW_SLOTS", "_PRENEW_LOOP_TICK_SEC",
    "_PRENEW_NOTIFY_LOG_RETENTION_DAYS", "_PRENEW_PAYMENT_ID",
    "_PRENEW_PROXY_TYPE", "_PRENEW_ROLE_ENFORCEMENT_ENABLED",
    "_PRENEW_SLOT_WINDOW_MIN", "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS",
    "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC", "_PRENEW_WARN_SLOTS",
    "_prenew_last_purge_date",
}

# Constants that other main.py code (renewal config zone, L37000+) still
# reads: re-exported to main via the from-import.
REEXPORT_TO_MAIN = [
    "PROXY_SELLER_API_KEY", "_PBUY_PERIOD_ID", "_PBUY_PERIOD_NAME",
    "_PBUY_PROXY_TYPE", "_PRENEW_PAYMENT_ID", "_PRENEW_PROXY_TYPE",
]


def main() -> None:
    if OUT.exists():
        sys.exit("proxy_ops.py already exists; refusing to re-run")

    src = MAIN.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)

    # ---- collect nodes to move -------------------------------------------
    move_segments: list[tuple[int, int]] = []  # 1-based inclusive line ranges
    moved_defs: list[str] = []
    moved_assigns: list[str] = []
    module_import_map: dict[str, str] = {}  # name -> import stmt source
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            seg = "".join(lines[node.lineno - 1: node.end_lineno])
            for a in node.names:
                module_import_map[a.asname or a.name.split(".")[0]] = seg
        if not (BLOCK_START <= node.lineno <= BLOCK_END):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n = node.name
            if any(n.startswith(p) for p in STAY_IN_MAIN_PREFIXES):
                continue
            if any(k in n.lower() for k in PROXY_KEYS):
                # include decorator lines
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                move_segments.append((start, node.end_lineno))
                moved_defs.append(n)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if isinstance(node, ast.Assign):
                tgts = [t.id for t in node.targets if isinstance(t, ast.Name)]
            else:
                tgts = [node.target.id] if isinstance(node.target, ast.Name) else []
            if any(t in STAY_IN_MAIN_NAMES for t in tgts):
                continue
            if any(t in MOVE_ASSIGN_NAMES for t in tgts):
                move_segments.append((node.lineno, node.end_lineno))
                moved_assigns.extend(tgts)

    missing_assigns = MOVE_ASSIGN_NAMES - set(moved_assigns)
    if missing_assigns:
        sys.exit(f"assigns not found in block: {sorted(missing_assigns)}")
    print(f"moving {len(moved_defs)} defs, {len(moved_assigns)} assigns")

    # ---- compute external dependencies ------------------------------------
    moved_names = set(moved_defs) | set(moved_assigns)
    top_defs = {}
    top_assign_names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top_assign_names.add(t.id)

    # Scope-accurate free-name analysis via symtable on the assembled
    # mini-module: locals, params, nested defs and comprehension vars are
    # excluded automatically; only true global references remain.
    import symtable

    mini_src = "".join(
        "".join(lines[s - 1: e]) + "\n" for s, e in sorted(move_segments)
    )
    refs: set[str] = set()

    def _walk_scope(st: symtable.SymbolTable) -> None:
        for sym in st.get_symbols():
            if sym.is_global() and not sym.is_assigned():
                refs.add(sym.get_name())
        for child in st.get_children():
            _walk_scope(child)

    _walk_scope(symtable.symtable(mini_src, "proxy_ops_mini", "exec"))

    bset = set(dir(builtins))
    unresolved = sorted(
        r for r in refs
        if r not in moved_names and r not in bset
    )
    importable = {r: module_import_map[r] for r in unresolved if r in module_import_map}
    bind_deps = sorted(
        r for r in unresolved
        if r not in importable and (r in top_defs or r in top_assign_names)
    )
    leftovers = [r for r in unresolved if r not in importable and r not in bind_deps]
    print("importable deps:", sorted(importable))
    print("bind deps:", bind_deps)
    if leftovers:
        sys.exit(f"UNRESOLVED names (fix manually): {leftovers}")

    # ---- build proxy_ops.py ------------------------------------------------
    header = '''"""Proxy buy / renewal / pool subsystem, extracted from main.py (R2 stage 1).

Controller-side only: PanelBot is UI + panel_commands submission -- all
provider HTTP calls, storage writes, and manager-field application happen
here, never in panel_bot.py.

SPEND SAFETY (AGENTS.md section 4): allow_spend=True appears as a real call
argument in EXACTLY 2 places project-wide, and both live in this module:
  1. make_ipv4(...) inside _handle_manager_proxy_buy_confirm_command --
     only reachable via the "/manager_proxy_buy_confirm <key>" panel command,
     which PanelBot submits only after the admin explicitly confirms a
     no-spend calc_ipv4() price preview.
  2. prolong_make(...) inside _prenew_execute_renewal -- the renewal
     executor, likewise behind an explicit admin confirm (or the autorenew
     gates in _prenew_autorenew_gates_ok).
_pbuy_provider() always constructs the provider with allow_spend=False;
spend calls opt in per-call. Raw proxy passwords never enter
panel_commands/result_text -- only a has_password boolean.

Late-bound main.py dependencies (notification, renewal-guard and registry
helpers) are injected via bind(globals()) at main.py module bottom; every
function here resolves them at call time, exactly as it did pre-extraction.

This module is import-safe: no Telethon, no DB access, no network at import
time.
"""
from __future__ import annotations

import asyncio
import json as _pbuy_json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import proxy_parser
'''
    extra_imports = sorted({seg for name, seg in importable.items()
                            if name not in {"proxy_parser", "asyncio", "re", "datetime",
                                            "timedelta", "Any", "Dict", "List", "Optional",
                                            "Tuple", "_pbuy_json", "os"}})
    header += "".join(extra_imports)

    bind_block_lines = [
        "",
        "# ---------------------------------------------------------------------------",
        "# Late-bound main.py dependencies. main.py calls bind(globals()) once at",
        "# module bottom (after every dependency is defined, before the event loop",
        "# starts). Until then each slot is a loud placeholder, never a silent None.",
        "_BIND_DEPS = (",
    ]
    for d in bind_deps:
        bind_block_lines.append(f"    {d!r},")
    bind_block_lines += [
        ")",
        "",
        "",
        "def _unbound(name):",
        "    def _raise(*a, **k):",
        '        raise RuntimeError(f"proxy_ops dependency {name!r} used before bind()")',
        "    return _raise",
        "",
        "",
        "for _dep in _BIND_DEPS:",
        "    globals()[_dep] = _unbound(_dep)",
        "del _dep",
        "",
        "",
        "def bind(ns):",
        '    """Inject late dependencies from main.py globals. Call once at startup."""',
        "    g = globals()",
        "    missing = [n for n in _BIND_DEPS if n not in ns]",
        "    if missing:",
        '        raise RuntimeError(f"proxy_ops.bind: missing deps: {missing}")',
        "    for n in _BIND_DEPS:",
        "        g[n] = ns[n]",
        "",
    ]

    body_parts: list[str] = []
    for s, e in sorted(move_segments):
        seg = "".join(lines[s - 1: e])
        if not seg.endswith("\n"):
            seg += "\n"
        body_parts.append(seg)

    OUT.write_text(
        header + "\n" + "\n".join(bind_block_lines) + "\n\n" + "\n\n".join(body_parts),
        encoding="utf-8",
    )

    # ---- rewrite main.py ---------------------------------------------------
    remove = set()
    for s, e in move_segments:
        remove.update(range(s, e + 1))
    # also remove the STAGE4 leading comment block (moved into module docstring)
    # only if it is contiguous comment lines right before the first moved segment
    first = min(s for s, _ in move_segments)
    i = first - 1
    while i >= 1 and (lines[i - 1].strip().startswith("#") or lines[i - 1].strip() == ""):
        if lines[i - 1].strip().startswith("#"):
            remove.add(i)
        i -= 1

    import_names = sorted(set(moved_defs)) + [n for n in REEXPORT_TO_MAIN]
    import_stmt = ["import proxy_ops\n", "from proxy_ops import (  # noqa: F401  (re-exported for panel exec chain + renewal zone)\n"]
    for n in import_names:
        import_stmt.append(f"    {n},\n")
    import_stmt.append(")\n")

    new_lines: list[str] = []
    inserted = False
    for idx, line in enumerate(lines, start=1):
        if idx in remove:
            if idx == first and not inserted:
                new_lines.extend(import_stmt)
                inserted = True
            continue
        new_lines.append(line)

    # insert bind BEFORE the __main__ guard: asyncio.run(main()) inside the
    # guard blocks forever, so anything appended after it would never run.
    bind_call = (
        "\n# R2: inject late-bound dependencies into the extracted proxy subsystem.\n"
        "# Must run at module level before the entrypoint: every _BIND_DEPS name\n"
        "# must already be defined by this point in the file.\n"
        "proxy_ops.bind(globals())\n\n"
    )
    guard_idx = None
    for idx, line in enumerate(new_lines):
        if line.startswith('if __name__ == "__main__":'):
            guard_idx = idx
            break
    if guard_idx is None:
        sys.exit("could not find __main__ guard in rewritten main.py")
    new_lines.insert(guard_idx, bind_call)
    out_src = "".join(new_lines)
    MAIN.write_text(out_src, encoding="utf-8")
    print(f"proxy_ops.py written ({OUT.stat().st_size} bytes); main.py rewritten")
    print("moved defs:", moved_defs)


if __name__ == "__main__":
    main()
