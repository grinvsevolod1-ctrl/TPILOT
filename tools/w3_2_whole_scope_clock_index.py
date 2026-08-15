# -*- coding: utf-8 -*-
"""W3.2 Design C+ -- Phase 0 scope index and Phase 3 independent whole-scope clock index.

Implements sections 5 and 6 of the approved implementation plan and Phase 0 / Phase 3 of
``08_CONSERVATIVE_CLOSURE_ALGORITHM.md``.

This module is BOTH:

  * a library used by ``w3_2_timezone_source_gate.py`` (definition universe, alias-aware
    clock detection, form tokens, adjudication primitives), and
  * a standalone CLI with ``--emit-baseline`` that mechanically produces the whole-scope
    clock-index rows for the external manifest.

Phase 3 is an INDEPENDENT safety layer.  It walks every definition instance in every
frozen scope file regardless of closure membership, unresolved count or escalation
count, and it never consults the reachability model.

The tool holds NO project-specific names.  Frozen roots, approved helpers, time-wrapper
definitions, zone-classification patterns and role declarations all arrive either from
the external manifest (normal runs) or from an external owner-declared seed file
(``--emit-baseline``).

Read-only.  Pure ``ast.parse`` over source text.  No import of the analysed files,
no DB, no network, no server.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from pathlib import Path

# ======================================================================================
# Clock / timezone API surface (generic Python API names -- not project identifiers)
# ======================================================================================

CLOCK_ATTRS = frozenset({
    "now", "today", "utcnow", "astimezone", "fromtimestamp",
    "localtime", "mktime", "monotonic", "perf_counter",
})

ZONEINFO_CALLABLE = "ZoneInfo"

UNAPPROVED_TIME_LIB_MARKERS = ("pendulum", "arrow", "dateutil.tz.tzlocal", "tzlocal")

# --------------------------------------------------------------------------------------
# Form tokens.  A token is "<KIND>:<ZONE_CLASS>".  Adjudication is uniform:
#     token in entry.permitted_forms  ->  OK
#     otherwise                       ->  violation with code_for(token, role)
# No syntax form is globally approved anywhere.
# --------------------------------------------------------------------------------------

ZONE_KYIV = "KYIV"
ZONE_UTC = "UTC"
ZONE_OTHER_LITERAL = "OTHER_LITERAL"
ZONE_NONLITERAL = "NONLITERAL"
ZONE_OTHER = "OTHER"
ZONE_NONE = "NONE"
ZONE_STRIP = "STRIP"
ZONE_HOST = "HOST"

KIND_BARE_NOW = "BARE_NOW"
KIND_BARE_TODAY = "BARE_TODAY"
KIND_BARE_ASTIMEZONE = "BARE_ASTIMEZONE"
KIND_ARGED_NOW = "ARGED_NOW"
KIND_UTCNOW = "UTCNOW"
KIND_ASTIMEZONE = "ASTIMEZONE"
KIND_FROMTIMESTAMP = "FROMTIMESTAMP"
KIND_LOCALTIME = "LOCALTIME"
KIND_MKTIME = "MKTIME"
KIND_MONOTONIC = "MONOTONIC"
KIND_PERF_COUNTER = "PERF_COUNTER"
KIND_ZONEINFO = "ZONEINFO"
KIND_REPLACE_TZINFO = "REPLACE_TZINFO"
KIND_WRAPPER = "WRAPPER_DELEGATION"
KIND_UNAPPROVED_LIB = "UNAPPROVED_TIME_LIB"

# Tokens that may never be added to permitted_forms by the mechanical baseline
# generator.  They require an explicit owner declaration in the seed; without one the
# row is emitted REVIEW_REQUIRED and the gate stays RED (ROLE_REVIEW_REQUIRED).
NEVER_AUTO_PERMIT = frozenset({
    "%s:%s" % (KIND_BARE_NOW, ZONE_NONE),
    "%s:%s" % (KIND_BARE_TODAY, ZONE_NONE),
    "%s:%s" % (KIND_BARE_ASTIMEZONE, ZONE_NONE),
    "%s:%s" % (KIND_LOCALTIME, ZONE_HOST),
    "%s:%s" % (KIND_MKTIME, ZONE_HOST),
    "%s:%s" % (KIND_FROMTIMESTAMP, "NAIVE"),
    "%s:%s" % (KIND_ZONEINFO, ZONE_NONLITERAL),
    "%s:%s" % (KIND_ZONEINFO, ZONE_OTHER_LITERAL),
    "%s:%s" % (KIND_ARGED_NOW, ZONE_OTHER_LITERAL),
    "%s:%s" % (KIND_ASTIMEZONE, ZONE_OTHER_LITERAL),
    "%s:%s" % (KIND_UNAPPROVED_LIB, "LIB"),
})

# Absolute diagnostic codes -- used when a token is not permitted for the instance.
_ABSOLUTE_CODE = {
    KIND_BARE_NOW: "BARE_NOW",
    KIND_BARE_TODAY: "BARE_TODAY",
    KIND_BARE_ASTIMEZONE: "BARE_ASTIMEZONE",
    KIND_LOCALTIME: "HOST_LOCAL_TIME_API",
    KIND_MKTIME: "HOST_LOCAL_TIME_API",
    KIND_UNAPPROVED_LIB: "UNAPPROVED_TIME_LIB",
}

UTC_FAMILY_TOKENS = frozenset({
    "%s:%s" % (KIND_UTCNOW, ZONE_UTC),
    "%s:%s" % (KIND_ARGED_NOW, ZONE_UTC),
    "%s:%s" % (KIND_ASTIMEZONE, ZONE_UTC),
    "%s:%s" % (KIND_ZONEINFO, ZONE_UTC),
    "%s:%s" % (KIND_REPLACE_TZINFO, ZONE_UTC),
    "%s:%s" % (KIND_REPLACE_TZINFO, ZONE_STRIP),
})

KYIV_FAMILY_TOKENS = frozenset({
    "%s:%s" % (KIND_ARGED_NOW, ZONE_KYIV),
    "%s:%s" % (KIND_ASTIMEZONE, ZONE_KYIV),
    "%s:%s" % (KIND_ZONEINFO, ZONE_KYIV),
    "%s:%s" % (KIND_REPLACE_TZINFO, ZONE_KYIV),
})

MONOTONIC_TOKENS = frozenset({
    "%s:%s" % (KIND_MONOTONIC, ZONE_NONE),
    "%s:%s" % (KIND_PERF_COUNTER, ZONE_NONE),
})

KYIV_ROLES = frozenset({"BUSINESS_LOCAL", "DATE_ONLY_KYIV"})
UTC_ROLES = frozenset({"UTC_PERSISTENCE", "UTC_INSTANT", "FRESHNESS_UTC"})
MIXED_ROLE = "MIXED_CLOCK_CONTRACT"

KIND_WRAPPER_CALL = "WRAPPER_CALL"

# W3.2 D6/D7 correction (2026-08-01): wrapper-call tokens are produced ONLY for
# registered mixed_clock_scan_targets (08_MANIFEST_ROLE_PLAN.md sec 3/7) and exist for
# per-site adjudication only -- they are never permitted at row level, so the row-level
# family-coherence check (H2) never encounters one. Family membership here governs only
# whether a site contract role swap is caught as a substitution.
UTC_FAMILY_TOKENS = UTC_FAMILY_TOKENS | frozenset({"%s:%s" % (KIND_WRAPPER_CALL, ZONE_UTC)})
KYIV_FAMILY_TOKENS = KYIV_FAMILY_TOKENS | frozenset({"%s:%s" % (KIND_WRAPPER_CALL, ZONE_KYIV)})


def token(kind: str, zone: str) -> str:
    return "%s:%s" % (kind, zone)


def code_for(tok: str, role) -> str:
    """Diagnostic code for a form token that is NOT permitted for its instance."""
    kind = tok.split(":", 1)[0]
    zone = tok.split(":", 1)[1] if ":" in tok else ""

    if kind in _ABSOLUTE_CODE:
        return _ABSOLUTE_CODE[kind]
    if kind == KIND_FROMTIMESTAMP and zone == "NAIVE":
        return "NAIVE_FROMTIMESTAMP"
    if kind == KIND_ZONEINFO and zone in (ZONE_OTHER_LITERAL, ZONE_NONLITERAL):
        return "WRONG_ZONE"
    if kind in (KIND_ARGED_NOW, KIND_ASTIMEZONE) and zone == ZONE_OTHER_LITERAL:
        return "WRONG_ZONE_ARG"
    if kind == KIND_REPLACE_TZINFO and zone != ZONE_STRIP:
        return "TZ_COERCION"

    # Role-sensitive substitution.
    if role == "MONOTONIC" and tok not in MONOTONIC_TOKENS:
        return "WALL_CLOCK_IN_MONOTONIC_ROLE"
    if role in KYIV_ROLES and tok in UTC_FAMILY_TOKENS:
        return "UTC_AS_KYIV_SUBSTITUTE"
    if role in UTC_ROLES and tok in KYIV_FAMILY_TOKENS:
        return "KYIV_AS_UTC_SUBSTITUTE"
    if kind in (KIND_ARGED_NOW, KIND_ASTIMEZONE) and zone == ZONE_OTHER:
        return "WRONG_ZONE_ARG"
    if kind == KIND_REPLACE_TZINFO:
        return "TZ_COERCION"
    return "ROLE_FORM_NOT_PERMITTED"


# ======================================================================================
# Phase 0 -- parse and index
# ======================================================================================

class DefRecord(object):
    __slots__ = ("file", "name", "qualified_name", "kind", "lineno", "end_lineno",
                 "node", "generation_index", "fingerprint", "class_owner")

    def __init__(self, file, name, qualified_name, kind, node, class_owner=None):
        self.file = file
        self.name = name
        self.qualified_name = qualified_name
        self.kind = kind
        self.node = node
        self.lineno = node.lineno
        self.end_lineno = getattr(node, "end_lineno", node.lineno)
        self.generation_index = 0
        self.class_owner = class_owner
        self.fingerprint = hashlib.sha256(
            ast.unparse(node).encode("utf-8")).hexdigest()

    @property
    def key(self):
        return (self.file, self.qualified_name, self.generation_index)

    def ident(self):
        return "%s:%s@%d" % (self.file, self.qualified_name, self.lineno)

    def as_dict(self):
        return {
            "file": self.file,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "generation_index": self.generation_index,
            "source_span": [self.lineno, self.end_lineno],
            "fingerprint": self.fingerprint,
        }


class ClassRecord(object):
    __slots__ = ("file", "name", "qualified_name", "node", "bases", "metaclass",
                 "constructors", "lineno")

    def __init__(self, file, name, qualified_name, node):
        self.file = file
        self.name = name
        self.qualified_name = qualified_name
        self.node = node
        self.lineno = node.lineno
        self.bases = [ast.unparse(b) for b in node.bases]
        self.metaclass = None
        for kw in node.keywords:
            if kw.arg == "metaclass":
                self.metaclass = ast.unparse(kw.value)
        self.constructors = []


class FileIndex(object):
    """Per-file parse products."""

    def __init__(self, name, path, tree, source):
        self.name = name
        self.path = path
        self.tree = tree
        self.source = source
        self.defs = []                 # list[DefRecord] in source order
        self.classes = []              # list[ClassRecord]
        self.alias_env = {}            # local binding -> canonical dotted origin
        self.import_bindings = {}      # local binding -> (module, is_from, asname)
        self.module_const = {}         # module-level Name -> unparsed value expression
        self.module_capture = {}       # module-level capture bindings (globals().get / alias)
        self.module_level_def_names = set()


def _iter_scope(node, prefix, file_name, out_defs, out_classes, class_owner=None):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = (prefix + "." + child.name) if prefix else child.name
            kind = "method" if class_owner else ("nested" if prefix else "module")
            rec = DefRecord(file_name, child.name, qname, kind, child, class_owner)
            out_defs.append(rec)
            _iter_scope(child, qname, file_name, out_defs, out_classes, None)
        elif isinstance(child, ast.ClassDef):
            qname = (prefix + "." + child.name) if prefix else child.name
            crec = ClassRecord(file_name, child.name, qname, child)
            out_classes.append(crec)
            before = len(out_defs)
            _iter_scope(child, qname, file_name, out_defs, out_classes, crec)
            for d in out_defs[before:]:
                if d.class_owner is crec and d.name in ("__init__", "__new__"):
                    crec.constructors.append(d)
        else:
            _iter_scope(child, prefix, file_name, out_defs, out_classes, class_owner)


def _build_alias_env(tree):
    """Collect every import binding in the file, at any nesting level.

    Conservative by construction: a name bound more than once keeps every origin, and a
    match against any origin counts.
    """
    env = {}
    bindings = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                local = a.asname or a.name.split(".")[0]
                origin = a.name if a.asname else a.name.split(".")[0]
                env.setdefault(local, set()).add(origin)
                bindings[local] = (a.name, False, a.asname)
        elif isinstance(n, ast.ImportFrom):
            if not n.module:
                continue
            for a in n.names:
                local = a.asname or a.name
                env.setdefault(local, set()).add(n.module + "." + a.name)
                bindings[local] = (n.module, True, a.asname)
    return env, bindings


def _build_module_env(tree, def_names):
    """Module-level simple assignments: constants and capture bindings."""
    consts = {}
    captures = {}
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        tgt = n.targets[0]
        if not isinstance(tgt, ast.Name):
            continue
        val = n.value
        consts.setdefault(tgt.id, ast.unparse(val))
        cap = _capture_target(val, def_names)
        if cap is not None:
            captures[tgt.id] = cap
    return consts, captures


def _capture_target(val, def_names):
    """Recognise ``globals().get("literal"[, fb])``, ``globals()["literal"]`` and
    ``X = <def name>`` module-level capture bindings (the TPilot override-chain idiom)."""
    if isinstance(val, ast.Name) and val.id in def_names:
        return val.id
    if isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute) \
            and val.func.attr == "get" \
            and isinstance(val.func.value, ast.Call) \
            and isinstance(val.func.value.func, ast.Name) \
            and val.func.value.func.id == "globals" \
            and val.args and isinstance(val.args[0], ast.Constant) \
            and isinstance(val.args[0].value, str):
        return val.args[0].value
    if isinstance(val, ast.Subscript) and isinstance(val.value, ast.Call) \
            and isinstance(val.value.func, ast.Name) and val.value.func.id == "globals" \
            and isinstance(val.slice, ast.Constant) and isinstance(val.slice.value, str):
        return val.slice.value
    return None


def build_scope_index(repo_root, scope_files):
    """Parse every scope file and build the definition universe (Phase 0)."""
    repo_root = Path(repo_root)
    files = {}
    for fname in scope_files:
        path = repo_root / fname
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=str(path))
        fi = FileIndex(fname, path, tree, source)
        _iter_scope(tree, "", fname, fi.defs, fi.classes)
        fi.alias_env, fi.import_bindings = _build_alias_env(tree)
        fi.module_level_def_names = {
            d.name for d in fi.defs if d.kind == "module"
        }
        all_names = {d.name for d in fi.defs}
        fi.module_const, fi.module_capture = _build_module_env(tree, all_names)
        # generation index per (file, qualified_name), source order
        counters = {}
        for d in fi.defs:
            d.generation_index = counters.get(d.qualified_name, 0)
            counters[d.qualified_name] = d.generation_index + 1
        files[fname] = fi
    return files


def universe_defs(files):
    out = []
    for fname in files:
        out.extend(files[fname].defs)
    return out


# ======================================================================================
# Zone classification (patterns supplied externally -- never hardcoded project names)
# ======================================================================================

class ZoneClassifier(object):
    """Classify a timezone-argument expression as KYIV / UTC / OTHER_LITERAL / ... .

    ``config`` comes from the manifest (``zone_classification``) or the baseline seed:

        {"kyiv_patterns": [regex, ...],
         "utc_patterns":  [regex, ...],
         "kyiv_zone_literals": ["Europe/Kyiv", ...],
         "utc_zone_literals":  ["UTC", ...]}

    One hop of module-level constant resolution is applied first.  That is a *labelling*
    aid, not a safety proof: an unresolved expression is classified OTHER and remains
    pinned by the instance body fingerprint.
    """

    def __init__(self, config):
        self.kyiv_res = [re.compile(p) for p in config.get("kyiv_patterns", [])]
        self.utc_res = [re.compile(p) for p in config.get("utc_patterns", [])]
        self.kyiv_literals = set(config.get("kyiv_zone_literals", []))
        self.utc_literals = set(config.get("utc_zone_literals", []))

    def literal_zone_class(self, value):
        if value in self.kyiv_literals:
            return ZONE_KYIV
        if value in self.utc_literals:
            return ZONE_UTC
        return ZONE_OTHER_LITERAL

    def classify(self, expr, fi):
        if expr is None:
            return ZONE_NONE
        if isinstance(expr, ast.Constant) and expr.value is None:
            return ZONE_STRIP
        text = ast.unparse(expr)
        for _ in range(1):  # exactly one hop
            if isinstance(expr, ast.Name) and expr.id in fi.module_const:
                text = fi.module_const[expr.id]
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return self.literal_zone_class(expr.value)
        for rx in self.kyiv_res:
            if rx.search(text):
                return ZONE_KYIV
        for rx in self.utc_res:
            if rx.search(text):
                return ZONE_UTC
        return ZONE_OTHER


# ======================================================================================
# Phase 3 -- alias-aware clock detection
# ======================================================================================

class ClockSite(object):
    __slots__ = ("token", "lineno", "expr", "in_except")

    def __init__(self, tok, lineno, expr, in_except):
        self.token = tok
        self.lineno = lineno
        self.expr = expr
        self.in_except = in_except

    def as_dict(self):
        return {"form": self.token, "lineno": self.lineno,
                "expr": self.expr, "in_except_handler": self.in_except}


def _origins(fi, name):
    return fi.alias_env.get(name, frozenset())


def _is_zoneinfo_callable(func, fi):
    if isinstance(func, ast.Name):
        if func.id == ZONEINFO_CALLABLE:
            return True
        return any(o.endswith("." + ZONEINFO_CALLABLE) or o == ZONEINFO_CALLABLE
                   for o in _origins(fi, func.id))
    if isinstance(func, ast.Attribute):
        return func.attr == ZONEINFO_CALLABLE
    return False


def _name_is_time_api(fi, ident, api):
    """``from time import monotonic as _m`` style bindings."""
    for o in _origins(fi, ident):
        if o == "time." + api or o.endswith("." + api):
            return True
    return ident == api and bool(_origins(fi, ident))


def _direct_body_walk(node):
    """Like ``ast.walk(node)`` but does NOT descend into nested ``FunctionDef`` /
    ``AsyncFunctionDef`` / ``Lambda`` / ``ClassDef`` bodies.

    H4 (single-owner clock sites, 08_MANIFEST_ROLE_PLAN.md sec 4 / 09 sec 5): a measured
    clock site belongs to the innermost independently indexed definition whose direct
    body contains it.  Nested definitions are indexed separately (``_iter_scope``) and
    must never be flattened into the enclosing row -- that was the pre-existing defect
    (P-1) affecting 5 rows.  ``node`` itself is never yielded (matching the previous
    ``ast.walk(node)`` call sites, which only ever matched on descendants).
    """
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))


def _except_line_ranges(defnode):
    ranges = []
    for n in _direct_body_walk(defnode):
        if isinstance(n, ast.ExceptHandler):
            ranges.append((n.lineno, getattr(n, "end_lineno", n.lineno)))
    return ranges


def _base_root(node):
    """Leftmost element of an attribute/subscript chain."""
    cur = node
    while True:
        if isinstance(cur, ast.Attribute):
            cur = cur.value
        elif isinstance(cur, ast.Subscript):
            cur = cur.value
        else:
            return cur


def build_wrapper_index(wrapper_registry):
    """``(file, qualified_name) -> produced_family`` from the manifest's wrapper_registry
    (or the seed's equivalent list of dicts)."""
    idx = {}
    for w in wrapper_registry or ():
        idx[(w["file"], w["qualified_name"])] = w["produced_family"]
    return idx


def _wrapper_family_for_call(func, fi, defrec_file, wrapper_index):
    """Alias-aware match of a Call.func against the wrapper registry (plan 08 sec 3.1 B):
    direct ``Name`` calls (same-file def or a proven ``from X import y [as z]`` alias) and
    module-qualified ``X.y()`` / ``import X as m; m.y()`` calls."""
    if not wrapper_index:
        return None
    if isinstance(func, ast.Name):
        ident = func.id
        fam = wrapper_index.get((defrec_file, ident))
        if fam:
            return fam
        for origin in _origins(fi, ident):
            if "." not in origin:
                continue
            mod, _, orig_name = origin.rpartition(".")
            modstem = mod.split(".")[-1]
            for (wfile, wqname), wfam in wrapper_index.items():
                if wqname == orig_name and wfile.split(".")[0] == modstem:
                    return wfam
        return None
    if isinstance(func, ast.Attribute):
        root = _base_root(func)
        attr = func.attr
        if isinstance(root, ast.Name):
            for origin in _origins(fi, root.id):
                modstem = origin.split(".")[-1]
                for (wfile, wqname), wfam in wrapper_index.items():
                    if wqname == attr and wfile.split(".")[0] == modstem:
                        return wfam
        return None
    return None


def detect_wrapper_call_sites(defrec, fi, wrapper_index):
    """Scan a mixed-scan target's DIRECT body only (H4) for calls to a registered
    wrapper, unconditionally -- discovery never depends on role or site_contracts
    (plan 08 sec 3, closes the circularity P-2/H5)."""
    sites = []
    node = defrec.node
    ex_ranges = _except_line_ranges(node)

    def in_except(lineno):
        return any(a <= lineno <= b for a, b in ex_ranges)

    for n in _direct_body_walk(node):
        if not isinstance(n, ast.Call):
            continue
        fam = _wrapper_family_for_call(n.func, fi, defrec.file, wrapper_index)
        if fam:
            sites.append(ClockSite(token(KIND_WRAPPER_CALL, fam), n.lineno,
                                   ast.unparse(n), in_except(n.lineno)))
    return sites


def site_identity(defrec, clocksite):
    """``SHA256(form_token | line_offset_from_owning_def_start | normalized_expr |
    in_except_handler)`` (plan 04 sec 6 / 08 sec 6) -- stable under edits elsewhere in
    the file, matching the position-independence of the body fingerprint."""
    offset = clocksite.lineno - defrec.lineno
    payload = "%s|%d|%s|%s" % (clocksite.token, offset, clocksite.expr, clocksite.in_except)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def detect_clock_forms(defrec, fi, zc, wrapper_qnames=()):
    """Return the list of ClockSite for one definition instance (alias-aware).

    Scans the definition's DIRECT body only -- H4 single-owner clock sites -- including
    dead branches, except handlers and loop bodies, but never descending into a nested
    ``FunctionDef`` / ``AsyncFunctionDef`` / ``Lambda`` / ``ClassDef`` (those are indexed,
    and their sites owned, independently). No dataflow, no branch exclusion.
    """
    sites = []
    node = defrec.node
    ex_ranges = _except_line_ranges(node)

    def in_except(lineno):
        return any(a <= lineno <= b for a, b in ex_ranges)

    for n in _direct_body_walk(node):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mod = getattr(n, "module", None) or ""
            blob = (mod + " " + " ".join(a.name for a in n.names)).lower()
            if any(m in blob for m in UNAPPROVED_TIME_LIB_MARKERS):
                sites.append(ClockSite(token(KIND_UNAPPROVED_LIB, "LIB"),
                                       n.lineno, ast.unparse(n), in_except(n.lineno)))
            continue
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        line = n.lineno
        text = ast.unparse(n)

        if _is_zoneinfo_callable(func, fi):
            if n.args:
                arg = n.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    zone = zc.literal_zone_class(arg.value)
                else:
                    zone = ZONE_NONLITERAL
            else:
                zone = ZONE_NONLITERAL
            sites.append(ClockSite(token(KIND_ZONEINFO, zone), line, text, in_except(line)))
            continue

        attr = None
        if isinstance(func, ast.Attribute):
            attr = func.attr
        elif isinstance(func, ast.Name):
            for api in ("monotonic", "perf_counter", "localtime", "mktime"):
                if _name_is_time_api(fi, func.id, api):
                    attr = api
                    break

        if attr is None:
            continue

        if attr == "now":
            if not n.args and not n.keywords:
                sites.append(ClockSite(token(KIND_BARE_NOW, ZONE_NONE), line, text,
                                       in_except(line)))
            else:
                arg = n.args[0] if n.args else (n.keywords[0].value if n.keywords else None)
                sites.append(ClockSite(token(KIND_ARGED_NOW, zc.classify(arg, fi)), line,
                                       text, in_except(line)))
        elif attr == "today":
            sites.append(ClockSite(token(KIND_BARE_TODAY, ZONE_NONE), line, text,
                                   in_except(line)))
        elif attr == "utcnow":
            sites.append(ClockSite(token(KIND_UTCNOW, ZONE_UTC), line, text,
                                   in_except(line)))
        elif attr == "astimezone":
            if not n.args and not n.keywords:
                sites.append(ClockSite(token(KIND_BARE_ASTIMEZONE, ZONE_NONE), line, text,
                                       in_except(line)))
            else:
                arg = n.args[0] if n.args else n.keywords[0].value
                sites.append(ClockSite(token(KIND_ASTIMEZONE, zc.classify(arg, fi)), line,
                                       text, in_except(line)))
        elif attr == "fromtimestamp":
            has_tz = len(n.args) > 1 or any(kw.arg == "tz" for kw in n.keywords)
            sites.append(ClockSite(token(KIND_FROMTIMESTAMP, "TZ" if has_tz else "NAIVE"),
                                   line, text, in_except(line)))
        elif attr == "localtime":
            sites.append(ClockSite(token(KIND_LOCALTIME, ZONE_HOST), line, text,
                                   in_except(line)))
        elif attr == "mktime":
            sites.append(ClockSite(token(KIND_MKTIME, ZONE_HOST), line, text,
                                   in_except(line)))
        elif attr == "monotonic":
            sites.append(ClockSite(token(KIND_MONOTONIC, ZONE_NONE), line, text,
                                   in_except(line)))
        elif attr == "perf_counter":
            sites.append(ClockSite(token(KIND_PERF_COUNTER, ZONE_NONE), line, text,
                                   in_except(line)))
        elif attr == "replace":
            for kw in n.keywords:
                if kw.arg == "tzinfo":
                    sites.append(ClockSite(token(KIND_REPLACE_TZINFO, zc.classify(kw.value, fi)),
                                           line, text, in_except(line)))

    if defrec.qualified_name in wrapper_qnames or \
            ("%s::%s" % (defrec.file, defrec.qualified_name)) in wrapper_qnames:
        sites.append(ClockSite(token(KIND_WRAPPER, ZONE_NONE), defrec.lineno,
                               "<manifested project time wrapper>", False))
    return sites


def build_clock_index(files, zc, wrapper_qnames=(), mixed_targets=(), wrapper_index=None):
    """Phase 3: every definition instance in every scope file, unconditionally.

    ``mixed_targets`` -- an iterable of ``(file, qualified_name)`` -- additionally gets its
    direct body scanned for registered-wrapper calls (plan 08 sec 3).  That scan is
    unconditional: it runs whether or not the target already has raw clock sites, and
    independently of any role or site_contracts (H5).
    """
    mixed_set = set(mixed_targets)
    rows = []
    for defrec in universe_defs(files):
        fi = files[defrec.file]
        sites = list(detect_clock_forms(defrec, fi, zc, wrapper_qnames))
        if (defrec.file, defrec.qualified_name) in mixed_set:
            sites.extend(detect_wrapper_call_sites(defrec, fi, wrapper_index or {}))
        if sites:
            rows.append((defrec, sites))
    return rows


# ======================================================================================
# Role proposal (mechanical evidence only -- never a silent final assignment)
# ======================================================================================

_FRESHNESS_HINT = re.compile(
    r"fresh|stale|age_|_age|elapsed|total_seconds|ttl|expired|since|timeout", re.I)
_PERSIST_HINT = re.compile(r"_iso\b|iso\(|strftime|insert|update |commit|save|persist|write", re.I)
_DATE_ONLY_HINT = re.compile(r"%Y-%m-%d(?!\s*%H)|\.date\(\)|business_date|today_iso", re.I)
_TRANSMIT_HINT = re.compile(r"payload|json|send|publish|report|ping|heartbeat|status", re.I)


def propose_role(defrec, sites, body_text):
    """Return (role, confidence, evidence dict).  Confidence is HIGH only when the
    evidence is unambiguous; everything else is REVIEW_REQUIRED and keeps the gate RED."""
    toks = {s.token for s in sites}
    kinds = {t.split(":", 1)[0] for t in toks}
    zones = {t.split(":", 1)[1] for t in toks}

    ev = {
        "detected_forms": sorted(toks),
        "stored": bool(_PERSIST_HINT.search(body_text)),
        "transmitted": bool(_TRANSMIT_HINT.search(body_text)),
        "compared_for_freshness": bool(_FRESHNESS_HINT.search(body_text)),
        "kyiv_business_time": ZONE_KYIV in zones,
        "date_only": bool(_DATE_ONLY_HINT.search(body_text)),
        "monotonic_duration": bool(kinds & {KIND_MONOTONIC, KIND_PERF_COUNTER}),
    }

    never = toks & NEVER_AUTO_PERMIT
    if never:
        ev["blocked_tokens"] = sorted(never)
        return None, "REVIEW_REQUIRED", ev

    wall = toks - MONOTONIC_TOKENS - {token(KIND_WRAPPER, ZONE_NONE)}
    if ev["monotonic_duration"] and not wall:
        return "MONOTONIC", "HIGH", ev

    has_kyiv = bool(toks & KYIV_FAMILY_TOKENS)
    has_utc = bool(toks & UTC_FAMILY_TOKENS)
    strip_only_utc = (toks & UTC_FAMILY_TOKENS) <= {token(KIND_REPLACE_TZINFO, ZONE_STRIP)}
    other_zone = ZONE_OTHER in zones

    if has_kyiv and not has_utc and not other_zone:
        return ("DATE_ONLY_KYIV" if ev["date_only"] else "BUSINESS_LOCAL"), "HIGH", ev

    if has_kyiv and has_utc:
        # a conversion boundary: Kyiv wall-clock produced from / converted to stored UTC
        return "BUSINESS_LOCAL", "HIGH", ev

    if has_utc and not has_kyiv and not other_zone:
        # H1 (09_ROLE_VALIDATION_HARDENING.md sec 2, closes review finding R-2): a
        # UTC-only source that ALSO shows date-only evidence (``.date()``, business_date,
        # today_iso, ...) must never auto-pass as UTC_PERSISTENCE/FRESHNESS_UTC -- that
        # silent fall-through is exactly the root cause behind D6 and D7. Ambiguity is
        # surfaced to the owner instead of guessed.
        if ev["date_only"]:
            return None, "REVIEW_REQUIRED", ev
        if ev["compared_for_freshness"]:
            return "FRESHNESS_UTC", "HIGH", ev
        if strip_only_utc or ev["stored"]:
            return "UTC_PERSISTENCE", "HIGH", ev
        if token(KIND_ARGED_NOW, ZONE_UTC) in toks and not ev["stored"]:
            return "UTC_INSTANT", "HIGH", ev
        return "UTC_PERSISTENCE", "HIGH", ev

    if toks == {token(KIND_WRAPPER, ZONE_NONE)}:
        return None, "REVIEW_REQUIRED", ev

    if other_zone:
        # non-literal zone argument: cannot be classified by syntax alone
        return None, "REVIEW_REQUIRED", ev

    return None, "REVIEW_REQUIRED", ev


# ======================================================================================
# H2 -- role / form-family coherence (09_ROLE_VALIDATION_HARDENING.md sec 3)
# ======================================================================================

def _role_mismatch(r, tok, detail):
    return {
        "code": "ROLE_CONTRACT_MISMATCH",
        "file": r["file"], "qualified_name": r["qualified_name"],
        "lineno": r["source_span"][0], "form": tok, "role": r.get("role"),
        "detail": detail, "layer": "policy",
    }


# DATE_ONLY_KYIV is a pure business-date output -- exactly D6's shape -- so a UTC token
# permitted there is always the D6 defect class (UTC_AS_KYIV_SUBSTITUTE) and is checked
# unconditionally. BUSINESS_LOCAL is deliberately excluded from that direction of the
# check: R4 boundary conversion (Kyiv wall-clock <-> stored/received UTC, e.g.
# storage.py::w3_parse_utc_to_local, stats_today_kyiv's outbound .astimezone(UTC), and
# the ~14 pre-existing UTC<->Kyiv converters spot-checked during this correction) is the
# role's own defining, already-reviewed pattern -- permitted_forms is the existing,
# intentional per-instance mechanism for exactly this (adjudicate() only ever flags an
# UNPERMITTED token). Narrowing this check to DATE_ONLY_KYIV keeps the property that
# actually matters (a pure date output can never legitimately carry a raw UTC form)
# without retroactively invalidating already-approved conversion-boundary rows outside
# this correction's D6/D7 scope. Disclosed explicitly in the implementation report.
_STRICT_KYIV_ROLES = frozenset({"DATE_ONLY_KYIV"})


def check_role_family_coherence(rows):
    """A ``UTC_FAMILY_TOKENS`` member permitted under ``DATE_ONLY_KYIV``, a
    ``KYIV_FAMILY_TOKENS`` member permitted under a ``UTC_ROLES`` role, any wall-clock
    token permitted under ``MONOTONIC``, or a ``MIXED_CLOCK_CONTRACT`` row with a
    non-empty row-level ``permitted_forms`` -- all ``ROLE_CONTRACT_MISMATCH``.  The only
    family-neutral token is ``WRAPPER_DELEGATION:NONE``; ``WRAPPER_CALL:*`` tokens are
    never permitted at row level (only inside mixed-target ``site_contracts``), so this
    row-level check never encounters one -- no blanket exemption is required."""
    findings = []
    for r in rows:
        role = r.get("role")
        permitted = set(r.get("permitted_forms") or [])
        if role == MIXED_ROLE:
            if permitted:
                findings.append(_role_mismatch(
                    r, None, "MIXED_CLOCK_CONTRACT row carries non-empty "
                             "row-level permitted_forms"))
            continue
        for tok in permitted:
            if tok == token(KIND_WRAPPER, ZONE_NONE):
                continue
            if role in _STRICT_KYIV_ROLES and tok in UTC_FAMILY_TOKENS:
                findings.append(_role_mismatch(
                    r, tok, "UTC_FAMILY_TOKENS member permitted under a KYIV role"))
            elif role in UTC_ROLES and tok in KYIV_FAMILY_TOKENS:
                findings.append(_role_mismatch(
                    r, tok, "KYIV_FAMILY_TOKENS member permitted under a UTC role"))
            elif role == "MONOTONIC" and tok not in MONOTONIC_TOKENS:
                findings.append(_role_mismatch(
                    r, tok, "wall-clock token permitted under MONOTONIC"))
    return findings


# ======================================================================================
# H3 -- manifest identity uniqueness (09_ROLE_VALIDATION_HARDENING.md sec 4)
# ======================================================================================

def check_identity_uniqueness(manifest):
    """Assert uniqueness of ``(file, qualified_name, generation_index)`` across
    ``clock_index``, ``wrapper_registry`` and ``mixed_clock_scan_targets``.  Closes R-8b
    (a duplicate identity used to silently resolve last-wins)."""
    problems = []
    sections = (
        ("clock_index", manifest.get("clock_index") or []),
        ("wrapper_registry", manifest.get("wrapper_registry") or []),
        ("mixed_clock_scan_targets", manifest.get("mixed_clock_scan_targets") or []),
    )
    for label, rows in sections:
        seen = {}
        for row in rows:
            key = (row.get("file"), row.get("qualified_name"), row.get("generation_index", 0))
            if key in seen:
                problems.append("%s: duplicate identity %s" % (label, key))
            else:
                seen[key] = row
    return problems


# ======================================================================================
# H4/H5/H6 -- mixed-scan-target integrity (09 sec 5/6/7, runtime-adjudicated every run)
# ======================================================================================

def check_duplicate_site_ownership(manifest):
    """Cross-row assertion: no ``(file, absolute line, normalized expr, form)`` may
    appear under two manifest identities (H4).  Runs over the frozen manifest content so
    a manifest-level mutation (M-OWN-1) is caught even though the source-level scan
    (single-owner by construction) would never reproduce it."""
    findings = []
    seen = {}
    for r in manifest.get("clock_index") or []:
        owner = (r["file"], r["qualified_name"], r.get("generation_index", 0))
        for s in r.get("sites") or []:
            k = (r["file"], s.get("lineno"), s.get("expr"), s.get("form"))
            if k in seen and seen[k] != owner:
                findings.append({
                    "code": "DUPLICATE_CLOCK_SITE_OWNERSHIP", "file": r["file"],
                    "qualified_name": r["qualified_name"], "lineno": s.get("lineno"),
                    "detail": "site %s already owned by %s" % (k, seen[k]),
                    "layer": "phase_ownership",
                })
            else:
                seen[k] = owner
    return findings


def adjudicate_mixed_targets(files, manifest, wrapper_index):
    """H5 -- mixed_clock_scan_targets integrity, run at every gate invocation (never only
    at manifest-build time) and NEVER gated on a row's role or its site_contracts (plan 08
    sec 3, closes circularity P-2): locate -> verify binding -> scan the direct body
    unconditionally -> only then read role/contracts."""
    findings = []
    targets = manifest.get("mixed_clock_scan_targets") or []
    index = {(r["file"], r["qualified_name"], r.get("generation_index", 0)): r
             for r in manifest.get("clock_index") or []}
    zc = ZoneClassifier(manifest.get("zone_classification", {}))
    wrapper_qnames = {"%s::%s" % (w["file"], w["qualified_name"])
                      for w in manifest.get("wrapper_registry") or []}
    target_identities = set()
    measured_site_total = 0
    contracted_site_total = 0

    for t in targets:
        fname, qname = t["file"], t["qualified_name"]
        gi = t.get("generation_index", 0)
        target_identities.add((fname, qname, gi))
        gens = [d for d in files[fname].defs if d.qualified_name == qname] if fname in files else []
        if gi >= len(gens):
            findings.append({"code": "MIXED_SCAN_TARGET_MISSING", "file": fname,
                             "qualified_name": qname, "lineno": 0,
                             "detail": "registered mixed-scan target generation %d not present" % gi,
                             "layer": "phase_mixed"})
            continue
        defrec = gens[gi]
        if defrec.fingerprint != t.get("fingerprint"):
            findings.append({"code": "MIXED_SCAN_TARGET_BINDING_DRIFT", "file": fname,
                             "qualified_name": qname, "lineno": defrec.lineno,
                             "detail": "target body fingerprint drift", "layer": "phase_mixed"})

        entry = index.get((fname, qname, gi))
        if entry is None:
            findings.append({"code": "MIXED_TARGET_ROW_MISSING", "file": fname,
                             "qualified_name": qname, "lineno": defrec.lineno,
                             "detail": "registered mixed-scan target has no clock_index row",
                             "layer": "phase_mixed"})
        elif entry.get("role") != MIXED_ROLE:
            findings.append({"code": "MIXED_TARGET_ROLE_MISMATCH", "file": fname,
                             "qualified_name": qname, "lineno": defrec.lineno,
                             "detail": "clock_index row role %r != %s"
                                       % (entry.get("role"), MIXED_ROLE),
                             "layer": "phase_mixed"})

        fi = files[fname]
        raw_sites = list(detect_clock_forms(defrec, fi, zc, wrapper_qnames))
        wrap_sites = detect_wrapper_call_sites(defrec, fi, wrapper_index)
        measured = raw_sites + wrap_sites
        measured_by_id = {site_identity(defrec, s): s for s in measured}
        measured_site_total += len(measured_by_id)

        contracts = (entry.get("site_contracts") or []) if entry else []
        contracted_site_total += len(contracts)
        contracted_ids = set()
        for c in contracts:
            sid = c.get("site_id")
            contracted_ids.add(sid)
            s = measured_by_id.get(sid)
            if s is None:
                findings.append({"code": "SITE_CONTRACT_NOT_MEASURED", "file": fname,
                                 "qualified_name": qname, "lineno": defrec.lineno,
                                 "detail": "site contract %s (%s/%s) references no measured site"
                                           % (sid, c.get("form"), c.get("site_role")),
                                 "layer": "phase_mixed"})
                continue
            sr = c.get("site_role")
            if sr in KYIV_ROLES and s.token in UTC_FAMILY_TOKENS:
                findings.append({"code": "UTC_AS_KYIV_SUBSTITUTE", "file": fname,
                                 "qualified_name": qname, "lineno": s.lineno, "form": s.token,
                                 "role": sr, "detail": "site form %s under site_role %s"
                                 % (s.token, sr), "layer": "phase_mixed"})
            elif sr in UTC_ROLES and s.token in KYIV_FAMILY_TOKENS:
                findings.append({"code": "KYIV_AS_UTC_SUBSTITUTE", "file": fname,
                                 "qualified_name": qname, "lineno": s.lineno, "form": s.token,
                                 "role": sr, "detail": "site form %s under site_role %s"
                                 % (s.token, sr), "layer": "phase_mixed"})
        for sid, s in measured_by_id.items():
            if sid not in contracted_ids:
                findings.append({"code": "SITE_CONTRACT_UNMATCHED", "file": fname,
                                 "qualified_name": qname, "lineno": s.lineno, "form": s.token,
                                 "detail": "measured site has no site_contract",
                                 "layer": "phase_mixed"})

    mixed_role_identities = {(r["file"], r["qualified_name"], r.get("generation_index", 0))
                             for r in manifest.get("clock_index") or []
                             if r.get("role") == MIXED_ROLE}
    for key in mixed_role_identities - target_identities:
        findings.append({"code": "MIXED_TARGET_REGISTRY_MISMATCH", "file": key[0],
                         "qualified_name": key[1], "lineno": 0,
                         "detail": "MIXED_CLOCK_CONTRACT row has no mixed_clock_scan_targets entry",
                         "layer": "phase_mixed"})

    totals = {
        "mixed_scan_target_total": len(targets),
        "mixed_role_row_total": len(mixed_role_identities),
        "mixed_target_role_mismatch_total": len(
            [f for f in findings if f["code"] == "MIXED_TARGET_ROLE_MISMATCH"]),
        "mixed_target_missing_total": len(
            [f for f in findings if f["code"] in
             ("MIXED_SCAN_TARGET_MISSING", "MIXED_TARGET_ROW_MISSING")]),
        "measured_mixed_site_total": measured_site_total,
        "contracted_mixed_site_total": contracted_site_total,
    }
    return findings, totals


# ======================================================================================
# Baseline emission
# ======================================================================================

def _body_text(defrec, fi):
    lines = fi.source.splitlines()
    return "\n".join(lines[defrec.lineno - 1: defrec.end_lineno])


def emit_baseline(repo_root, seed):
    scope_files = seed["scope_files"]
    files = build_scope_index(repo_root, scope_files)
    zc = ZoneClassifier(seed.get("zone_classification", {}))
    wrapper_seed = seed.get("wrapper_registry") or []
    wrappers = {"%s::%s" % (w["file"], w["qualified_name"]) for w in wrapper_seed}
    wrapper_idx = build_wrapper_index(
        [{"file": w["file"], "qualified_name": w["qualified_name"],
          "produced_family": w["produced_family"]} for w in wrapper_seed])
    mixed_seed = seed.get("mixed_clock_scan_targets") or []
    mixed_targets = [(m["file"], m["qualified_name"]) for m in mixed_seed]
    mixed_site_decls = {(m["file"], m["qualified_name"]): m.get("site_contracts", [])
                        for m in mixed_seed}

    # Owner declarations arrive from two seed sections; approved_helpers is the
    # role table of plan 09 1.2, role_declarations is the per-instance residue that
    # the mechanical proposer could not settle.  role_declarations wins on conflict.
    decls = {}
    for h in seed.get("approved_helpers", []):
        decls["%s::%s" % (h["file"], h["qualified_name"])] = {
            "role": h["role"],
            "contract_note": h.get("contract_note", ""),
            "permit_tokens": h.get("permit_tokens", []),
        }
    for key, decl in (seed.get("role_declarations") or {}).items():
        merged = dict(decls.get(key) or {})
        merged.update(decl)
        decls[key] = merged

    rows = []
    counts = {f: 0 for f in scope_files}
    for defrec, sites in build_clock_index(files, zc, wrappers, mixed_targets, wrapper_idx):
        fi = files[defrec.file]
        body = _body_text(defrec, fi)
        toks = sorted({s.token for s in sites})
        key = "%s::%s" % (defrec.file, defrec.qualified_name)
        decl = decls.get(key) or decls.get(defrec.qualified_name)
        is_mixed_target = (defrec.file, defrec.qualified_name) in mixed_site_decls

        role, confidence, evidence = propose_role(defrec, sites, body)
        contract = ""
        permitted = [t for t in toks if t not in NEVER_AUTO_PERMIT]

        if decl:
            role = decl["role"]
            confidence = "HIGH"
            contract = decl.get("contract_note", "")
            extra = decl.get("permit_tokens", [])
            for t in extra:
                if t in toks and t not in permitted:
                    permitted.append(t)
            permitted = sorted(set(permitted))
            if decl.get("finding_id"):
                evidence["finding_id"] = decl["finding_id"]
        elif confidence == "HIGH":
            contract = _auto_contract_note(role, evidence)

        site_contracts = None
        if is_mixed_target:
            role = MIXED_ROLE
            confidence = "HIGH"
            permitted = []
            declared = mixed_site_decls[(defrec.file, defrec.qualified_name)]
            site_contracts = []
            for sc in declared:
                matches = [s for s in sites if s.token == sc["form"]]
                if len(matches) != 1:
                    raise ValueError(
                        "mixed target %s::%s site_contract form %s matches %d measured "
                        "sites (need exactly 1)"
                        % (defrec.file, defrec.qualified_name, sc["form"], len(matches)))
                s = matches[0]
                site_contracts.append({
                    "site_id": site_identity(defrec, s),
                    "form": s.token,
                    "site_role": sc["site_role"],
                    "lineno": s.lineno,
                    "owner": "%s::%s" % (defrec.file, defrec.qualified_name),
                })

        row = {
            "file": defrec.file,
            "qualified_name": defrec.qualified_name,
            "generation_index": defrec.generation_index,
            "source_span": [defrec.lineno, defrec.end_lineno],
            "fingerprint": defrec.fingerprint,
            "role": role,
            "permitted_forms": sorted(set(permitted)),
            "contract_note": contract,
            "detected_forms": toks,
            "sites": [dict(s.as_dict(), site_id=site_identity(defrec, s)) for s in sites],
            "confidence": confidence,
            "evidence": evidence,
        }
        if site_contracts is not None:
            row["site_contracts"] = site_contracts
        if decl and decl.get("finding_id"):
            row["finding_id"] = decl["finding_id"]
        rows.append(row)
        counts[defrec.file] = counts.get(defrec.file, 0) + 1

    totals = {
        "whole_scope_def_total": len(universe_defs(files)),
        "whole_scope_clock_touching_total": len(rows),
        "per_file_definitions": {f: len(files[f].defs) for f in scope_files},
        "per_file_clock_touching": counts,
        "review_required": sum(1 for r in rows if r["confidence"] == "REVIEW_REQUIRED"),
        "by_role": {},
    }
    for r in rows:
        totals["by_role"][r["role"]] = totals["by_role"].get(r["role"], 0) + 1
    return files, rows, totals


def build_manifest_from_seed(repo_root, seed):
    """Assemble the external manifest mechanically from the owner-declared seed.

    Every project-specific value (scope files, roots, approved helpers, wrapper
    definitions, zone patterns, role table, per-instance role declarations) comes from
    the seed.  This function contributes only measurement: generation counts, active
    indices, source spans, ``SHA256(ast.unparse(node))`` fingerprints, detected form
    tokens and the closure budget.
    """
    files, rows, totals = emit_baseline(repo_root, seed)
    if totals["review_required"]:
        raise ValueError("%d REVIEW_REQUIRED rows remain; roles must be declared"
                         % totals["review_required"])

    roots = []
    for spec in seed["roots"]:
        gens = [d for d in files[spec["file"]].defs if d.qualified_name == spec["name"]]
        if not gens:
            raise ValueError("frozen root %s::%s not found" % (spec["file"], spec["name"]))
        roots.append({
            "file": spec["file"],
            "name": spec["name"],
            "generation_count": len(gens),
            "active_index": len(gens) - 1,
            "active_fingerprint": gens[-1].fingerprint,
            "generations": [{"generation_index": g.generation_index,
                             "source_span": [g.lineno, g.end_lineno],
                             "fingerprint": g.fingerprint} for g in gens],
            "note": spec.get("note", "frozen timezone entry point; all generations are "
                                     "seeded into the closure unconditionally"),
        })

    helpers = []
    for h in seed["approved_helpers"]:
        gens = [d for d in files[h["file"]].defs if d.qualified_name == h["qualified_name"]]
        if not gens:
            raise ValueError("approved helper %s::%s not found"
                             % (h["file"], h["qualified_name"]))
        g = gens[-1]
        helpers.append({
            "file": h["file"],
            "qualified_name": h["qualified_name"],
            "generation_index": g.generation_index,
            "generation_count": len(gens),
            "source_span": [g.lineno, g.end_lineno],
            "fingerprint": g.fingerprint,
            "role": h["role"],
            "contract_note": h["contract_note"],
        })

    aware_by_role = seed.get("aware_by_role", {})
    purpose_by_role = seed.get("purpose_by_role", {})
    index_rows = []
    for r in rows:
        row = dict(r)
        row["aware"] = aware_by_role.get(r["role"])
        row["purpose"] = purpose_by_role.get(r["role"])
        index_rows.append(row)

    coherence_problems = check_role_family_coherence(index_rows)
    if coherence_problems:
        raise ValueError("%d ROLE_CONTRACT_MISMATCH row(s): %s"
                         % (len(coherence_problems),
                            "; ".join("%s::%s %s" % (p["file"], p["qualified_name"], p["detail"])
                                     for p in coherence_problems[:8])))

    deferred = [{"file": r["file"], "qualified_name": r["qualified_name"],
                 "generation_index": r["generation_index"],
                 "finding_id": r.get("finding_id"), "role": r["role"],
                 "permitted_forms": r["permitted_forms"], "note": r["contract_note"]}
                for r in index_rows if r["role"] == "DEFERRED_W3_4"]

    # WRAPPER_CALL:KYIV is excluded -- it is a call to an already-approved wrapper, not a
    # zone construction (plan 08 sec 7); the count stays at its historical value.
    def _kyiv_zone_forms(forms):
        return [f for f in forms
                if f.endswith(":" + ZONE_KYIV) and not f.startswith(KIND_WRAPPER_CALL + ":")]

    grandfathered = [{"file": r["file"], "qualified_name": r["qualified_name"],
                      "generation_index": r["generation_index"],
                      "source_span": r["source_span"],
                      "forms": _kyiv_zone_forms(r["detected_forms"])}
                     for r in index_rows if _kyiv_zone_forms(r["detected_forms"])]

    wrapper_registry_seed = seed.get("wrapper_registry") or []
    wrapper_registry = []
    for w in wrapper_registry_seed:
        gens = [d for d in files[w["file"]].defs if d.qualified_name == w["qualified_name"]]
        if not gens:
            raise ValueError("wrapper_registry entry %s::%s not found"
                             % (w["file"], w["qualified_name"]))
        g = gens[-1]
        wrapper_registry.append({
            "file": w["file"],
            "qualified_name": w["qualified_name"],
            "generation_index": g.generation_index,
            "fingerprint": g.fingerprint,
            "produced_family": w["produced_family"],
            "contract_note": w.get("contract_note", ""),
        })

    mixed_targets_seed = seed.get("mixed_clock_scan_targets") or []
    mixed_clock_scan_targets = []
    for m in mixed_targets_seed:
        gens = [d for d in files[m["file"]].defs if d.qualified_name == m["qualified_name"]]
        if not gens:
            raise ValueError("mixed_clock_scan_targets entry %s::%s not found"
                             % (m["file"], m["qualified_name"]))
        g = gens[-1]
        mixed_clock_scan_targets.append({
            "file": m["file"],
            "qualified_name": m["qualified_name"],
            "generation_index": g.generation_index,
            "fingerprint": g.fingerprint,
            "adjudication_mode": "PER_SITE",
            "contract_note": m.get("contract_note", ""),
        })

    manifest = {
        "schema_version": 1,
        "manifest_id": "w3_2_timezone_gate_manifest",
        "generated_for": seed.get("generated_for",
                                  "W3.2 Design C+ conservative timezone source gate"),
        "generated_date": seed.get("generated_date", ""),
        "authoritative_plan": seed.get("authoritative_plan", ""),
        "scope_files": seed["scope_files"],
        "closure_budget": sum(len(files[f].defs) for f in seed["scope_files"]),
        "unresolved_policy": "ESCALATE_HOME_FILE_WHOLE",
        "escalation_constants": {
            "escalation_unit": "file",
            "escalation_impossible_is_red": True,
            "escalation_weakens_policy": False,
        },
        "zone_classification": seed["zone_classification"],
        "wrapper_registry": wrapper_registry,
        "mixed_clock_scan_targets": mixed_clock_scan_targets,
        "roles": seed["roles"],
        "roots": roots,
        "approved_helpers": helpers,
        "clock_index": index_rows,
        "deferred_entries": deferred,
        "grandfathered_kyiv_instances": grandfathered,
        "measured_totals_at_generation": totals,
    }

    identity_problems = check_identity_uniqueness(manifest)
    if identity_problems:
        raise ValueError("%d duplicate identity problem(s): %s"
                         % (len(identity_problems), "; ".join(identity_problems[:8])))
    return manifest


def write_manifest(manifest, out_path):
    """Write the manifest plus its integrity sidecar; return the SHA256 digest."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    digest = hashlib.sha256(out_path.read_bytes()).hexdigest()
    Path(str(out_path) + ".sha256").write_text("%s  %s\n" % (digest, out_path.name),
                                               encoding="utf-8")
    return digest


def _auto_contract_note(role, ev):
    bits = []
    if ev.get("kyiv_business_time"):
        bits.append("Kyiv zone argument")
    if ev.get("compared_for_freshness"):
        bits.append("value consumed as an age/staleness delta")
    if ev.get("stored"):
        bits.append("value persisted/serialised")
    if ev.get("monotonic_duration"):
        bits.append("monotonic counter used for an interval")
    if ev.get("date_only"):
        bits.append("date-only output")
    return "%s: %s (forms pinned: %s)" % (
        role, "; ".join(bits) or "no cross-domain evidence", ", ".join(ev["detected_forms"]))


# ======================================================================================
# Standalone Phase-3 run against the real manifest
# ======================================================================================

def run_index_against_manifest(repo_root, manifest):
    """Independent Phase 3: never consults the closure."""
    scope_files = manifest["scope_files"]
    files = build_scope_index(repo_root, scope_files)
    zc = ZoneClassifier(manifest.get("zone_classification", {}))
    wrapper_registry = manifest.get("wrapper_registry") or []
    wrappers = {"%s::%s" % (w["file"], w["qualified_name"]) for w in wrapper_registry}
    wrapper_idx = build_wrapper_index(wrapper_registry)
    mixed_targets = [(m["file"], m["qualified_name"])
                     for m in (manifest.get("mixed_clock_scan_targets") or [])]

    # H3 (09 sec 4, closes R-8b): a duplicate manifest identity used to resolve
    # silently last-wins here; it must instead turn the whole run RED.
    index = {}
    dup_identity_findings = []
    for row in manifest["clock_index"]:
        key = (row["file"], row["qualified_name"], row["generation_index"])
        if key in index:
            dup_identity_findings.append({
                "code": "MANIFEST_SCHEMA_FAIL", "file": row["file"],
                "qualified_name": row["qualified_name"], "lineno": row["source_span"][0],
                "detail": "duplicate clock_index identity %s" % (key,),
                "layer": "phase0_index",
            })
        else:
            index[key] = row

    measured = build_clock_index(files, zc, wrappers, mixed_targets, wrapper_idx)
    findings = list(dup_identity_findings)
    manifested = 0
    unmanifested = 0
    measured_rows = []

    for defrec, sites in measured:
        key = (defrec.file, defrec.qualified_name, defrec.generation_index)
        entry = index.get(key)
        toks = sorted({s.token for s in sites})
        rec = {
            "file": defrec.file,
            "qualified_name": defrec.qualified_name,
            "generation_index": defrec.generation_index,
            "source_span": [defrec.lineno, defrec.end_lineno],
            "fingerprint": defrec.fingerprint,
            "detected_forms": toks,
            "manifested": entry is not None,
            "role": entry.get("role") if entry else None,
        }
        measured_rows.append(rec)
        if entry is None:
            unmanifested += 1
            findings.append({
                "code": "UNMANIFESTED_CLOCK_TOUCHING_DEF",
                "file": defrec.file,
                "qualified_name": defrec.qualified_name,
                "lineno": defrec.lineno,
                "detail": "clock-touching definition has no manifest entry "
                          "(detected forms: %s)" % ", ".join(toks),
                "layer": "phase3_index",
            })
        else:
            manifested += 1
            if entry.get("fingerprint") != defrec.fingerprint:
                findings.append({
                    "code": "CLOCK_INDEX_FINGERPRINT_DRIFT",
                    "file": defrec.file,
                    "qualified_name": defrec.qualified_name,
                    "lineno": defrec.lineno,
                    "detail": "body fingerprint %s != manifest %s"
                              % (defrec.fingerprint[:16], str(entry.get("fingerprint"))[:16]),
                    "layer": "phase3_index",
                })
            if entry.get("confidence") == "REVIEW_REQUIRED":
                findings.append({
                    "code": "ROLE_REVIEW_REQUIRED",
                    "file": defrec.file,
                    "qualified_name": defrec.qualified_name,
                    "lineno": defrec.lineno,
                    "detail": "manifest role is not settled for this instance",
                    "layer": "phase3_index",
                })
        findings.extend(adjudicate(defrec, sites, entry))

    missing = []
    seen = {(d.file, d.qualified_name, d.generation_index) for d, _ in measured}
    for key, row in index.items():
        if key not in seen:
            missing.append(row)
            findings.append({
                "code": "MANIFESTED_ROW_NOT_MEASURED",
                "file": row["file"],
                "qualified_name": row["qualified_name"],
                "lineno": row["source_span"][0],
                "detail": "manifest declares a clock-touching definition that the index "
                          "no longer measures (removed, renamed or de-clocked)",
                "layer": "phase3_index",
            })

    totals = {
        "whole_scope_def_total": len(universe_defs(files)),
        "whole_scope_clock_touching_total": len(measured),
        "manifested_clock_touching_total": manifested,
        "unmanifested_clock_touching_total": unmanifested,
        "manifested_rows_not_measured": len(missing),
    }
    return files, measured_rows, findings, totals


def adjudicate(defrec, sites, entry):
    """Phase 4 role policy for one definition instance.  Identical in every phase.

    A MIXED_CLOCK_CONTRACT row carries an empty row-level permitted_forms by contract
    (H2) -- ALL of its per-site adjudication happens in ``adjudicate_mixed_targets``
    against the row's ``site_contracts``, never here."""
    role = entry.get("role") if entry else None
    if role == MIXED_ROLE:
        return []
    permitted = set(entry.get("permitted_forms") or []) if entry else set()
    out = []
    for s in sites:
        if s.token in permitted:
            continue
        code = code_for(s.token, role)
        if s.in_except and code in ("BARE_NOW", "BARE_TODAY", "BARE_ASTIMEZONE",
                                    "HOST_LOCAL_TIME_API", "NAIVE_FROMTIMESTAMP",
                                    "WRONG_ZONE", "WRONG_ZONE_ARG", "TZ_COERCION"):
            code = "HOST_LOCAL_FALLBACK"
        out.append({
            "code": code,
            "file": defrec.file,
            "qualified_name": defrec.qualified_name,
            "lineno": s.lineno,
            "form": s.token,
            "role": role,
            "expr": s.expr,
            "detail": "form %s is not permitted for role %s at this instance"
                      % (s.token, role),
            "layer": "policy",
        })
    return out


# ======================================================================================
# CLI
# ======================================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="W3.2 whole-scope clock index (Phase 3)")
    ap.add_argument("--repo", default=r"C:\ALM_TPilot")
    ap.add_argument("--emit-baseline", action="store_true")
    ap.add_argument("--build-manifest", default=None,
                    help="assemble the external manifest from --seed and write it here")
    ap.add_argument("--seed", default=None,
                    help="owner-declared baseline seed (required with --emit-baseline)")
    ap.add_argument("--out", default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--expected-digest", default=None)
    args = ap.parse_args(argv)

    if args.build_manifest:
        if not args.seed:
            print("[FAIL] --build-manifest requires --seed")
            return 2
        seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))
        manifest = build_manifest_from_seed(args.repo, seed)
        digest = write_manifest(manifest, args.build_manifest)
        print("[OK] manifest written: %s" % args.build_manifest)
        print("     rows=%d roots=%d helpers=%d deferred=%d budget=%d"
              % (len(manifest["clock_index"]), len(manifest["roots"]),
                 len(manifest["approved_helpers"]), len(manifest["deferred_entries"]),
                 manifest["closure_budget"]))
        print("     sha256=%s" % digest)
        return 0

    if args.emit_baseline:
        if not args.seed:
            print("[FAIL] --emit-baseline requires --seed")
            return 2
        seed = json.loads(Path(args.seed).read_text(encoding="utf-8"))
        _files, rows, totals = emit_baseline(args.repo, seed)
        payload = {"rows": rows, "totals": totals}
        if args.out:
            Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                      encoding="utf-8")
        print("[OK] baseline emitted: %d clock-touching rows of %d definitions"
              % (totals["whole_scope_clock_touching_total"], totals["whole_scope_def_total"]))
        for f, n in sorted(totals["per_file_clock_touching"].items()):
            print("     %-22s %4d / %d defs" % (f, n, totals["per_file_definitions"][f]))
        print("     REVIEW_REQUIRED rows: %d" % totals["review_required"])
        for r in rows:
            if r["confidence"] == "REVIEW_REQUIRED":
                print("       ? %s::%s@%d  forms=%s"
                      % (r["file"], r["qualified_name"], r["source_span"][0],
                         ",".join(r["detected_forms"])))
        return 0 if totals["review_required"] == 0 else 1

    import w3_2_manifest_integrity as mi  # noqa: E402
    try:
        manifest, _rep = mi.load_manifest(args.manifest, None, args.expected_digest)
    except mi.ManifestError as exc:
        print("[FAIL] %s -- %s" % (exc.code, exc.detail))
        return 1

    _files, rows, findings, totals = run_index_against_manifest(args.repo, manifest)
    payload = {"totals": totals, "findings": findings, "rows": rows}
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    for k in sorted(totals):
        print("     %-38s %s" % (k, totals[k]))
    if findings:
        print("[FAIL] %d finding(s)" % len(findings))
        for f in findings[:25]:
            print("   %-32s %s::%s@%d" % (f["code"], f["file"], f["qualified_name"], f["lineno"]))
        return 1
    print("[OK] whole-scope clock index clean")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
