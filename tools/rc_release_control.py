# -*- coding: utf-8 -*-
"""tools/rc_release_control.py -- shared release-control library (Master Plan P2).

Single source of truth for:
  * active-tree discovery (what is first-party runtime vs archival);
  * deterministic SHA256/size/AST hashing;
  * active-binding + capture-chain reconstruction (TPilot stacks defs);
  * the manifest SCHEMA later phases freeze (P12) and package (P14).

Design rules enforced here (from the accepted Master Plan):
  * NEVER imports project runtime modules -- panel_bot/main/manager_bot construct
    Telethon clients at module scope. Everything is AST/source inspection.
  * NEVER mutates the project tree.
  * Deterministic ordering everywhere: manifests must be byte-reproducible.
  * Category separation is structural, not advisory:
        A PROJECT CONTENT  -> goes to C:\\ALM_TPilot\\<relative_path>
        B DEPLOYMENT CONTROL -> __tpilot_release__\\... , never into the project
        C SERVER-LOCAL CONFIG (.env*) -> never in a package at all
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

CONTROL_NAMESPACE = "__tpilot_release__"

#: Directories never walked for release content.
EXCLUDED_DIRS = {
    ".git", ".backups", "sessions", "runtime", "logs", "exports", "db",
    "venv", "venv_old_workcar6", "__pycache__", ".claude", ".codex", ".agents",
    "_deleted_managers_backup", "_manager_session_backups", "CLAUDE_NEW_PC",
    "config",
}

#: Filename fragments that mark an archival / non-active artifact.
ARCHIVAL_MARKERS = (
    ".bak", ".retired", "_cursor_copy", "main_beka", "main_before",
)

#: Path prefixes rejected from any package (defence in depth: name + location).
FORBIDDEN_PACKAGE_PREFIXES = (
    "db/", "sessions/", "runtime/", "logs/", "exports/", "venv/",
    ".backups/", "__pycache__/", ".git/", "config/",
    "_deleted_managers_backup/", "_manager_session_backups/",
)

FORBIDDEN_PACKAGE_NAME_PATTERNS = (
    ".env", "-wal", "-shm", ".session", ".sha256",
)

#: Third-party distributions TPilot actually depends on (import name).
THIRD_PARTY = {
    "telethon", "aiosqlite", "dotenv", "openpyxl", "socks", "sockshandler",
    "PIL", "requests", "segno", "opentele", "pytz", "yaml", "numpy",
    "socksio", "cryptg", "rsa", "pyaes",
}


def _stdlib_names() -> Set[str]:
    names = set(getattr(sys, "stdlib_module_names", set()))
    # Defensive: a few that matter to us regardless of interpreter build.
    names |= {"sqlite3", "asyncio", "json", "ast", "hashlib", "pathlib", "typing"}
    return names


STDLIB = _stdlib_names()


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def rel(root: Path, p: Path) -> str:
    """Deterministic POSIX-style relative path used as the manifest key."""
    return p.resolve().relative_to(root.resolve()).as_posix()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_source(p: Path) -> str:
    """utf-8-sig: several TPilot files carry a BOM."""
    return p.read_text(encoding="utf-8-sig", errors="replace")


def is_archival(name: str) -> bool:
    return any(m in name for m in ARCHIVAL_MARKERS)


def dumps(obj: Any) -> str:
    """Deterministic JSON: sorted keys, fixed separators, trailing newline."""
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------
# Active tree discovery
# --------------------------------------------------------------------------

def walk_active(root: Path, suffixes: Optional[Iterable[str]] = None) -> List[Path]:
    """Every non-archival file under root, excluding EXCLUDED_DIRS."""
    out: List[Path] = []
    sfx = set(suffixes) if suffixes else None
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDED_DIRS and not d.startswith("_pkg_")
        )
        for fn in sorted(filenames):
            if is_archival(fn):
                continue
            if sfx and Path(fn).suffix.lower() not in sfx:
                continue
            out.append(Path(dirpath) / fn)
    return sorted(out, key=lambda p: rel(root, p))


def first_party_modules(root: Path) -> Dict[str, Path]:
    """Map import-name -> path for every first-party module/package at root."""
    mods: Dict[str, Path] = {}
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_file() and entry.suffix == ".py" and not is_archival(entry.name):
            mods[entry.stem] = entry
        elif entry.is_dir() and entry.name not in EXCLUDED_DIRS \
                and not entry.name.startswith("_pkg_") \
                and (entry / "__init__.py").is_file():
            mods[entry.name] = entry
    return mods


# --------------------------------------------------------------------------
# AST: definitions, active bindings, capture chains
# --------------------------------------------------------------------------

class ModuleIndex:
    """AST view of one module: definitions, final bindings, capture chains.

    TPilot redefines top-level names repeatedly ("last def wins") AND captures
    earlier function objects via `_X_PREV = globals().get("name")` before the
    redefinition. A shadowed def is therefore NOT automatically dead -- this
    class is what makes that distinction mechanical instead of guessed.
    """

    def __init__(self, path: Path, root: Path):
        self.path = path
        self.rel = rel(root, path)
        self.src = read_source(path)
        self.lines = self.src.splitlines()
        self.tree = ast.parse(self.src)

        self.defs: Dict[str, List[int]] = {}
        self.def_nodes: Dict[Tuple[str, int], ast.AST] = {}
        self.classes: Dict[str, List[int]] = {}
        for n in self.tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.defs.setdefault(n.name, []).append(n.lineno)
                self.def_nodes[(n.name, n.lineno)] = n
            elif isinstance(n, ast.ClassDef):
                self.classes.setdefault(n.name, []).append(n.lineno)

        self.captures = self._captures()
        self.capture_uses = self._capture_uses()
        self.handlers = self._handlers()
        self.dynamic = self._dynamic_refs()

    # -- capture detection -------------------------------------------------
    def _captures(self) -> Dict[str, List[Tuple[str, int]]]:
        """symbol -> [(capture_variable, capture_lineno)] at module scope."""
        out: Dict[str, List[Tuple[str, int]]] = {}
        for n in self.tree.body:
            if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)):
                continue
            v, sym = n.value, None
            # _X = globals().get("name")
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "get"
                    and isinstance(v.func.value, ast.Call)
                    and getattr(v.func.value.func, "id", "") == "globals"
                    and v.args and isinstance(v.args[0], ast.Constant)
                    and isinstance(v.args[0].value, str)):
                sym = v.args[0].value
            # _X = globals()["name"]
            elif (isinstance(v, ast.Subscript) and isinstance(v.value, ast.Call)
                  and getattr(v.value.func, "id", "") == "globals"
                  and isinstance(v.slice, ast.Constant)
                  and isinstance(v.slice.value, str)):
                sym = v.slice.value
            # _X = name   (direct alias of a known def)
            elif isinstance(v, ast.Name) and v.id in self.defs:
                sym = v.id
            if sym:
                out.setdefault(sym, []).append((n.targets[0].id, n.lineno))
        return out

    def _capture_uses(self) -> Dict[str, int]:
        """capture_variable -> number of *uses*, excluding its own store.

        Counts BOTH access forms, because TPilot uses both:
          * static  : `await _TPILOT_ORIG_X(...)`        -> ast.Name Load
          * dynamic : `globals().get("_TPILOT_ORIG_X")`  -> string literal

        Counting only ast.Name Loads produced a real false-negative during P2
        self-review: `_TP_REPORT_V3_ORIG_TP_QS_ROW_BUCKET` and
        `_TP_REPORT_V5_ORIG_TP_QS_ROW_BUCKET` are read exclusively through
        `globals().get(...)`, so a Name-only scan would have mis-classified two
        LIVE delegate chains as PROVEN_DEAD_CANDIDATE -- i.e. it would have
        nominated live code for deletion in P11-C.
        """
        wanted = {cv for lst in self.captures.values() for cv, _ in lst}
        counts = {cv: 0 for cv in wanted}
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Name) and n.id in counts and isinstance(n.ctx, ast.Load):
                counts[n.id] += 1
            # dynamic: globals().get("NAME") / globals()["NAME"] / getattr(o, "NAME")
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in counts:
                counts[n.value] += 1
        return counts

    # -- handler / dynamic detection --------------------------------------
    def _handlers(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for n in self.tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.decorator_list:
                decs = []
                for d in n.decorator_list:
                    try:
                        decs.append(ast.unparse(d))
                    except Exception:
                        decs.append("<undecodable>")
                if decs:
                    out.setdefault(n.name, []).extend(decs)
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Call):
                fn = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
                if fn == "add_event_handler":
                    for a in n.args:
                        if isinstance(a, ast.Name):
                            out.setdefault(a.id, []).append("add_event_handler")
        return out

    def _dynamic_refs(self) -> Set[str]:
        """Symbol names referenced through getattr()/globals()[...] string literals."""
        found: Set[str] = set()
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Call):
                fid = getattr(n.func, "id", None)
                if fid == "getattr" and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
                        and isinstance(n.args[1].value, str):
                    found.add(n.args[1].value)
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Call) \
                    and getattr(n.value.func, "id", "") == "globals" \
                    and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                found.add(n.slice.value)
        return found

    # -- public API --------------------------------------------------------
    def symbol_ast_sha256(self, name: str, lineno: Optional[int] = None) -> Optional[str]:
        """SHA256 of a definition's exact source block (final binding by default)."""
        locs = self.defs.get(name) or self.classes.get(name)
        if not locs:
            return None
        ln = lineno if lineno is not None else locs[-1]
        node = self.def_nodes.get((name, ln))
        if node is None:
            for n in self.tree.body:
                if isinstance(n, ast.ClassDef) and n.name == name and n.lineno == ln:
                    node = n
                    break
        if node is None:
            return None
        block = "\n".join(self.lines[node.lineno - 1: getattr(node, "end_lineno", node.lineno)])
        return sha256_bytes(block.encode("utf-8"))

    def final_binding(self, name: str) -> Optional[int]:
        locs = self.defs.get(name) or self.classes.get(name)
        return locs[-1] if locs else None

    def classify_definition(self, name: str, lineno: int) -> str:
        """Classification for the P11-C cleanup inventory. NEVER deletes."""
        locs = self.defs.get(name, [])
        if not locs:
            return "UNKNOWN"
        if lineno == locs[-1]:
            if name in self.handlers:
                return "ACTIVE_CALLBACK"
            return "ACTIVE_AUTHORITATIVE"
        # non-final definition: is it captured between this def and the next one?
        nxt = min((l for l in locs if l > lineno), default=None)
        covering = [(cv, cl) for cv, cl in self.captures.get(name, [])
                    if lineno < cl < (nxt if nxt is not None else 10 ** 9)]
        if covering:
            if any(self.capture_uses.get(cv, 0) > 0 for cv, _ in covering):
                return "ACTIVE_CAPTURED"
            return "PROVEN_DEAD_CANDIDATE"   # captured, but alias never loaded
        if name in self.dynamic or name in self.handlers:
            return "UNKNOWN"
        return "PROVEN_DEAD_CANDIDATE"


# --------------------------------------------------------------------------
# Manifest schema (P2 golden baseline / P12 final release content)
# --------------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION = "1.0"

#: Fields every content entry carries. P2 fills what it can prove; P12 freezes.
CONTENT_ENTRY_FIELDS = (
    "relative_path", "sha256", "size", "role", "reason_changed", "phase",
    "migration_relevance", "compile_required", "import_smoke_group",
    "existed_on_historical_baseline", "protected_symbols", "symbol_ast_sha256",
)


def make_content_entry(
    relative_path: str,
    sha256: str,
    size: int,
    role: str,
    *,
    reason_changed: Optional[str] = None,
    phase: Optional[str] = None,
    migration_relevance: Optional[str] = None,
    compile_required: bool = False,
    import_smoke_group: Optional[str] = None,
    existed_on_historical_baseline: Optional[bool] = None,
    protected_symbols: Optional[List[str]] = None,
    symbol_ast_sha256: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "relative_path": relative_path,
        "sha256": sha256,
        "size": size,
        "role": role,
        "reason_changed": reason_changed,
        "phase": phase,
        "migration_relevance": migration_relevance,
        "compile_required": bool(compile_required),
        "import_smoke_group": import_smoke_group,
        "existed_on_historical_baseline": existed_on_historical_baseline,
        "protected_symbols": sorted(protected_symbols or []),
        "symbol_ast_sha256": dict(sorted((symbol_ast_sha256 or {}).items())),
    }


def role_for(relative_path: str) -> str:
    p = relative_path
    if p.startswith("tools/"):
        return "test"
    if p.endswith((".bat", ".ps1")):
        return "lifecycle"
    if "/" in p and p.split("/", 1)[0] in ("features", "tdata_import", "admin_ai"):
        return "package"
    if p.endswith(".py"):
        return "runtime"
    return "data"


def is_forbidden_package_path(relative_path: str) -> Optional[str]:
    """Return the reason a path may never enter a package, else None."""
    p = relative_path.replace("\\", "/")
    low = p.lower()
    if p.startswith("/") or (len(p) > 1 and p[1] == ":"):
        return "absolute path"
    if ".." in Path(p).parts:
        return "path traversal"
    for pref in FORBIDDEN_PACKAGE_PREFIXES:
        if low.startswith(pref.lower()):
            return f"forbidden prefix {pref}"
    base = low.rsplit("/", 1)[-1]
    if base.startswith(".env") or ".env." in base:
        return "server-local config (.env)"
    for pat in FORBIDDEN_PACKAGE_NAME_PATTERNS:
        if pat in low:
            return f"forbidden pattern {pat}"
    if is_archival(base):
        return "archival artifact"
    if base.startswith("_pkg_") or low.startswith("_pkg_"):
        return "historical package snapshot"
    return None
