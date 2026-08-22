"""Shared AST extraction harness for tools/*_selftest.py.

WHY THIS EXISTS
---------------
main.py and panel_bot.py cannot be imported standalone (Telethon/env side effects at
import time), so the selftests AST-extract the function under test and `exec` it in a
seeded namespace. That technique is correct and stays.

The problem was that 102 of 154 selftests carried their own copy of the extractor, and
every copy understood only ONE shape: a top-level `def`. When product code was refactored
-- correctly -- to move a helper into a module and re-bind it by assignment:

    _manager_label_from_row = text_format_helpers.manager_label_from_row

...every private copy started reporting "could not find {...} as top-level defs". That is
a STALE HARNESS, not a product defect, but it reads exactly like a real failure, and ten
of them accumulated until the suite stopped being trusted.

This module is the single implementation. It understands three shapes:

  1. `def name(...)` / `async def name(...)` / `class name` -- LAST definition wins,
     which is load-bearing: main.py/panel_bot.py stack override definitions of the same
     function and only the last one is active (AGENTS.md section 4).
  2. `name = module.attr` -- a module-level alias to an extracted helper.
  3. `name = <anything else>` -- a module-level constant the extracted code closes over.

It also auto-injects the project modules the extracted code references, so moving a
helper into a new module does not silently break every consumer of this harness again.

Nothing here touches the network, the production DB, or any .session file.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent.parent

# Modules that are safe to import for a selftest: pure helpers with no Telethon/env
# side effects at import time. main.py / panel_bot.py are deliberately NOT here -- the
# whole point of AST extraction is that importing them is unsafe.
_SAFE_AUTO_IMPORT = {
    "text_format_helpers",
    "proxy_parser",
    "manager_registry",
    "process_control",
    "tpilot_paths",
    "stats_engine",
    "texts",
    "post_followup_texts",
    "profile_texts",
}


def safe_module_ns() -> dict[str, Any]:
    """Namespace seed of every side-effect-free project module.

    Selftests that build their own exec namespace (rather than calling
    extract_and_exec) should splat this in FIRST, then override with their fakes:

        ns = {**ast_extract.safe_module_ns(), "storage": fake_storage, ...}

    Extracted main.py code refers to module-level import aliases (`text_format_helpers`,
    `proxy_parser`, ...). A bare `ast.Import` statement is never captured by name-based
    extraction, so each such module used to be hand-bound in every test -- and every new
    extraction broke a handful of selftests with NameError until someone noticed. Seeding
    them all is cheap and makes that class of breakage structurally impossible.
    """
    ns: dict[str, Any] = {}
    for name in sorted(_SAFE_AUTO_IMPORT):
        try:
            ns[name] = importlib.import_module(name)
        except Exception:
            pass
    return ns


def _strip_docstring(node: ast.AST) -> ast.AST:
    """Drop a leading docstring so unparse output stays compact in failure diffs."""
    body = getattr(node, "body", None)
    if (isinstance(body, list) and body
            and isinstance(body[0], ast.Expr)
            and isinstance(getattr(body[0], "value", None), ast.Constant)
            and isinstance(body[0].value.value, str)):
        node.body = body[1:] or [ast.Pass()]
    return node


def _alias_target_names(node: ast.Assign) -> list[str]:
    return [t.id for t in node.targets if isinstance(t, ast.Name)]


def _root_module_of(node: ast.AST) -> str | None:
    """For `a.b.c` return 'a'; for a bare Name return None."""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    return cur.id if isinstance(cur, ast.Name) else None


def _referenced_names(nodes: Iterable[ast.AST]) -> set[str]:
    seen: set[str] = set()
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Name):
                seen.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                root = _root_module_of(sub)
                if root:
                    seen.add(root)
    return seen


def extract_nodes(path: Path, names: set[str]) -> dict[str, ast.AST]:
    """Return the ACTIVE top-level node for each requested name.

    Later definitions overwrite earlier ones, matching Python's own semantics and the
    override-chain convention in main.py / panel_bot.py.
    """
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    picked: dict[str, ast.AST] = {}
    for node in tree.body:
        # Shape 1: def / async def / class
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in names:
                picked[node.name] = node
        # Shapes 2 and 3: module-level assignment (alias or constant)
        elif isinstance(node, ast.Assign):
            for tgt in _alias_target_names(node):
                if tgt in names:
                    picked[tgt] = node
    return picked


def extract_and_exec(
    path: Path,
    names: set[str],
    extra_ns: dict[str, Any] | None = None,
    *,
    auto_imports: bool = True,
) -> dict[str, Any]:
    """AST-extract the active definition of each name and exec it into a seeded namespace.

    `extra_ns` wins over auto-imported modules, so a test can always substitute a fake
    (that is how the proxy selftests inject a no-spend provider).
    """
    extra_ns = dict(extra_ns or {})
    picked = extract_nodes(path, names)

    missing = names - set(picked)
    if missing:
        # Name the shapes we looked for, so the next person does not have to read this
        # module to understand why a name that plainly exists was "not found".
        raise AssertionError(
            f"could not find {sorted(missing)} in {path.name} as a top-level "
            f"def/async def/class or a module-level assignment. "
            f"If the helper moved into a module, check that the alias assignment is at "
            f"module level (not nested inside a function)."
        )

    ordered = [picked[n] for n in sorted(picked)]
    module_src = "\n\n".join(ast.unparse(_strip_docstring(n)) for n in ordered)

    ns: dict[str, Any] = {}
    if auto_imports:
        for ref in _referenced_names(ordered) & _SAFE_AUTO_IMPORT:
            try:
                ns[ref] = importlib.import_module(ref)
            except Exception:
                # A module that cannot import standalone is not fatal here: if the
                # extracted code actually needs it, exec raises NameError and the test
                # fails loudly with the real reason.
                pass

    ns.update(extra_ns)
    exec(compile(module_src, f"<{path.name}>", "exec"), ns)
    return ns
