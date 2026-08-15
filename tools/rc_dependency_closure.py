# -*- coding: utf-8 -*-
"""tools/rc_dependency_closure.py -- first-party dependency closure (Master Plan P2).

Answers the packaging question that a hand-maintained file list cannot:
"which first-party modules MUST be in the release, or the system fails to start?"

This exists because main.py and panel_bot.py import two packages at MODULE
SCOPE with no try/except guard:

    main.py:3006   import features.prepared_accounts as _prepared_accounts_pkg
    main.py:38968  from tdata_import import service as _tdimport_service
    panel_bot.py:38 from features.prepared_accounts import model as _prepared_model

Omitting either from the package = immediate ImportError on start = total outage.
So their presence is a HARD gate, not a checklist item.

NEVER imports the entrypoints (they build Telethon clients at import time).
Pure AST + filesystem resolution.

Usage:
    python tools/rc_dependency_closure.py [--root <project>] [--json <out>] [--md <out>]
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rc_release_control import (  # noqa: E402
    STDLIB, THIRD_PARTY, ModuleIndex, dumps, first_party_modules, is_archival, rel,
)

BASE_DIR = Path(__file__).resolve().parent.parent

ENTRYPOINTS = [
    "main.py", "panel_bot.py", "manager_bot.py", "partner_stat_bot.py",
    "soft_watchdog_pinger.py", "health_server.py", "preflight_check.py",
    "manager_registry.py",
]

#: Packages whose absence is a release-stopping failure (module-level imports).
HARD_REQUIRED_PACKAGES = ("features", "tdata_import")


class ImportRef:
    __slots__ = ("module", "lineno", "hard", "context", "source_file", "kind")

    def __init__(self, module: str, lineno: int, hard: bool, context: str,
                 source_file: str, kind: str):
        self.module, self.lineno, self.hard = module, lineno, hard
        self.context, self.source_file, self.kind = context, source_file, kind

    def as_dict(self, resolved: Optional[str], party: str, required: bool) -> Dict[str, Any]:
        return {
            "source_file": self.source_file, "import": self.module,
            "line": self.lineno, "kind": self.kind,
            "hard": self.hard, "context": self.context,
            "resolved_path": resolved, "party": party,
            "required_in_package": required,
            "why": ("module-level unguarded import -- absence breaks startup"
                    if self.hard and party == "first-party" else
                    "guarded/deferred import -- absence degrades, does not break startup"
                    if party == "first-party" else f"{party}, not packaged from project tree"),
        }


def collect_imports(path: Path, root: Path) -> List[ImportRef]:
    """Every import in a module, tagged HARD (module-level, unguarded) or SOFT."""
    src = path.read_text(encoding="utf-8-sig", errors="replace")
    tree = ast.parse(src)
    relp = rel(root, path)
    out: List[ImportRef] = []

    # Map each node to its enclosing context by walking with parent links.
    parents: Dict[int, Any] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def context_of(node: ast.AST) -> Tuple[bool, str]:
        """(is_hard, context-label). Hard = module scope AND not inside try/except."""
        cur, in_func, in_try = node, False, False
        while id(cur) in parents:
            cur = parents[id(cur)]
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                in_func = True
            elif isinstance(cur, ast.Try):
                in_try = True
            elif isinstance(cur, ast.ClassDef):
                in_func = True
        if in_func and in_try:
            return False, "function+try"
        if in_func:
            return False, "function-local"
        if in_try:
            return False, "module-level try/except"
        return True, "module-level unguarded"

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            hard, ctx = context_of(n)
            for a in n.names:
                out.append(ImportRef(a.name.split(".")[0], n.lineno, hard, ctx, relp, "import"))
        elif isinstance(n, ast.ImportFrom):
            hard, ctx = context_of(n)
            if n.level and n.level > 0:
                pkg_parts = Path(relp).parent.as_posix().replace("/", ".")
                base = pkg_parts.split(".")[0] if pkg_parts and pkg_parts != "." else ""
                if base:
                    out.append(ImportRef(base, n.lineno, hard, ctx + " (relative)", relp, "from-relative"))
                continue
            if n.module:
                out.append(ImportRef(n.module.split(".")[0], n.lineno, hard, ctx, relp, "from"))
    return out


def resolve_first_party(root: Path, top: str, dotted: str = "") -> Optional[Path]:
    mods = first_party_modules(root)
    p = mods.get(top)
    if p is None:
        return None
    return p


def classify(root: Path, top: str) -> str:
    if top in first_party_modules(root):
        return "first-party"
    if top in STDLIB:
        return "stdlib"
    if top in THIRD_PARTY:
        return "third-party"
    return "unknown"


def package_files(root: Path, pkg_dir: Path) -> List[str]:
    out = []
    for p in sorted(pkg_dir.rglob("*.py")):
        if "__pycache__" in p.parts or is_archival(p.name):
            continue
        out.append(rel(root, p))
    return out


def lifecycle_referenced_scripts(root: Path) -> Dict[str, List[str]]:
    """First-party .py referenced from .bat/.ps1 launchers."""
    refs: Dict[str, List[str]] = {}
    for p in sorted(list(root.glob("*.ps1")) + list(root.glob("*.bat"))):
        if is_archival(p.name):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except Exception:
            continue
        found = sorted(set(re.findall(r'([A-Za-z_][A-Za-z_0-9]*\.py)', txt)))
        if found:
            refs[p.name] = found
    return refs


def closure(root: Path) -> Dict[str, Any]:
    seen: Set[str] = set()
    queue: List[str] = []
    rows: List[Dict[str, Any]] = []
    hard_first_party: Set[str] = set()
    soft_first_party: Set[str] = set()
    missing_hard: List[Dict[str, Any]] = []
    dynamic: List[Dict[str, Any]] = []

    for e in ENTRYPOINTS:
        if (root / e).is_file():
            queue.append(e)

    while queue:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        p = root / cur
        if not p.is_file():
            continue
        try:
            refs = collect_imports(p, root)
        except SyntaxError as ex:
            rows.append({"source_file": cur, "import": "<SYNTAX ERROR>", "why": str(ex)})
            continue

        try:
            mi = ModuleIndex(p, root)
            for d in sorted(mi.dynamic):
                if d in first_party_modules(root):
                    dynamic.append({"source_file": cur, "symbol": d,
                                    "note": "dynamic getattr/globals reference to a first-party name"})
        except Exception:
            pass

        for r in refs:
            party = classify(root, r.module)
            required = False
            resolved = None
            if party == "first-party":
                target = first_party_modules(root)[r.module]
                resolved = rel(root, target)
                required = r.hard
                if r.hard:
                    hard_first_party.add(r.module)
                else:
                    soft_first_party.add(r.module)
                if target.is_dir():
                    for f in package_files(root, target):
                        if f not in seen:
                            queue.append(f)
                else:
                    if resolved not in seen:
                        queue.append(resolved)
            elif party == "unknown":
                if not (root / f"{r.module}.py").exists() and not (root / r.module).is_dir():
                    if r.hard:
                        missing_hard.append({"source_file": cur, "import": r.module,
                                             "line": r.lineno, "context": r.context})
            rows.append(r.as_dict(resolved, party, required))

    required_paths: Set[str] = set()
    for m in sorted(hard_first_party):
        t = first_party_modules(root)[m]
        if t.is_dir():
            required_paths.update(package_files(root, t))
        else:
            required_paths.add(rel(root, t))
    for e in ENTRYPOINTS:
        if (root / e).is_file():
            required_paths.add(e)

    hard_gate = {}
    for pkg in HARD_REQUIRED_PACKAGES:
        present = pkg in hard_first_party
        files = package_files(root, root / pkg) if (root / pkg).is_dir() else []
        hard_gate[pkg] = {
            "discovered_as_hard_dependency": present,
            "files": files,
            "file_count": len(files),
            "all_in_required_paths": all(f in required_paths for f in files) if files else False,
        }

    return {
        "entrypoints": [e for e in ENTRYPOINTS if (root / e).is_file()],
        "modules_traversed": sorted(seen),
        "hard_first_party_modules": sorted(hard_first_party),
        "soft_first_party_modules": sorted(soft_first_party - hard_first_party),
        "missing_hard_first_party": missing_hard,
        "dynamic_import_candidates": dynamic,
        "lifecycle_script_references": lifecycle_referenced_scripts(root),
        "required_package_paths": sorted(required_paths),
        "hard_required_package_gate": hard_gate,
        "imports": rows,
    }


def write_md(res: Dict[str, Any], out: Path) -> None:
    L: List[str] = []
    L.append("# DEPENDENCY CLOSURE REPORT (Master Plan P2)\n")
    L.append(f"- entrypoints: {len(res['entrypoints'])}")
    L.append(f"- modules traversed: {len(res['modules_traversed'])}")
    L.append(f"- HARD first-party modules: {len(res['hard_first_party_modules'])}")
    L.append(f"- SOFT first-party modules: {len(res['soft_first_party_modules'])}")
    L.append(f"- required package paths: {len(res['required_package_paths'])}")
    L.append(f"- **missing HARD first-party: {len(res['missing_hard_first_party'])}**\n")

    L.append("## HARD REQUIRED PACKAGE GATE\n")
    L.append("| package | discovered as HARD | files | all in required paths |")
    L.append("|---|---|---:|---|")
    for pkg, g in sorted(res["hard_required_package_gate"].items()):
        L.append(f"| `{pkg}` | {'YES' if g['discovered_as_hard_dependency'] else '**NO**'} "
                 f"| {g['file_count']} | {'YES' if g['all_in_required_paths'] else '**NO**'} |")

    L.append("\n## MODULE-LEVEL UNGUARDED FIRST-PARTY IMPORTS (hard dependencies)\n")
    L.append("| ENTRYPOINT/SOURCE | IMPORT | LINE | TYPE | RESOLVED | HARD/SOFT | PARTY | IN PACKAGE | WHY |")
    L.append("|---|---|---:|---|---|---|---|---|---|")
    for r in res["imports"]:
        if r.get("party") != "first-party" or not r.get("hard"):
            continue
        L.append(f"| `{r['source_file']}` | `{r['import']}` | {r['line']} | {r['kind']} | "
                 f"`{r['resolved_path']}` | HARD | first-party | "
                 f"{'YES' if r['required_in_package'] else 'no'} | {r['why']} |")

    L.append("\n## GUARDED / DEFERRED FIRST-PARTY IMPORTS (soft)\n")
    soft = [r for r in res["imports"] if r.get("party") == "first-party" and not r.get("hard")]
    if soft:
        L.append("| SOURCE | IMPORT | LINE | CONTEXT |")
        L.append("|---|---|---:|---|")
        for r in soft[:80]:
            L.append(f"| `{r['source_file']}` | `{r['import']}` | {r['line']} | {r['context']} |")
        if len(soft) > 80:
            L.append(f"\n_… and {len(soft)-80} more_")
    else:
        L.append("_none_")

    L.append("\n## DYNAMIC IMPORT / REFLECTION CANDIDATES (manual review)\n")
    if res["dynamic_import_candidates"]:
        for d in res["dynamic_import_candidates"]:
            L.append(f"- `{d['source_file']}`: `{d['symbol']}` — {d['note']}")
    else:
        L.append("_none detected_")

    L.append("\n## LIFECYCLE SCRIPT REFERENCES\n")
    for s, fs in sorted(res["lifecycle_script_references"].items()):
        L.append(f"- `{s}` -> {', '.join('`%s`' % f for f in fs)}")

    L.append("\n## MISSING HARD FIRST-PARTY (must be empty for P2 PASS)\n")
    if res["missing_hard_first_party"]:
        for m in res["missing_hard_first_party"]:
            L.append(f"- **{m}**")
    else:
        L.append("_none_")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(BASE_DIR))
    ap.add_argument("--json")
    ap.add_argument("--md")
    a = ap.parse_args()
    root = Path(a.root).resolve()
    res = closure(root)

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(dumps(res), encoding="utf-8")
    if a.md:
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        write_md(res, Path(a.md))

    print(f"entrypoints            : {len(res['entrypoints'])}")
    print(f"modules traversed      : {len(res['modules_traversed'])}")
    print(f"HARD first-party       : {res['hard_first_party_modules']}")
    print(f"SOFT first-party       : {res['soft_first_party_modules']}")
    print(f"required package paths : {len(res['required_package_paths'])}")
    print(f"missing HARD           : {len(res['missing_hard_first_party'])}")
    for pkg, g in sorted(res["hard_required_package_gate"].items()):
        print(f"  gate {pkg:<14}: hard={g['discovered_as_hard_dependency']} "
              f"files={g['file_count']} covered={g['all_in_required_paths']}")

    failed = bool(res["missing_hard_first_party"])
    for pkg, g in res["hard_required_package_gate"].items():
        if not g["discovered_as_hard_dependency"] or not g["all_in_required_paths"]:
            print(f"[FAIL] hard-required package not protected: {pkg}")
            failed = True
    print("RESULT:", "FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
