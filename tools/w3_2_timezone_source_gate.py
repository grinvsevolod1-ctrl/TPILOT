# -*- coding: utf-8 -*-
"""W3.2 Design C+ -- conservative timezone source gate.

Implements Phases 0-5 of ``08_CONSERVATIVE_CLOSURE_ALGORITHM.md`` from the approved
redesign plan (C:\\ALM_TPilot_AUDIT\\20260731\\W3_2_GATE_REDESIGN_PLAN\\).

Structure
---------
Phase 0  parse and index                    (w3_2_whole_scope_clock_index.build_scope_index)
Phase 1  strict conservative closure        callable-context-sensitive, monotone fixpoint
Phase 2  whole-file escalation              any unresolved dispatch escalates its home file
Phase 3  independent whole-scope clock index runs unconditionally, never consults 1 or 2
Phase 4  scan and adjudicate                syntactic, role-based, identical in all phases
Phase 5  frozen binding integrity           roots and approved helpers, by fingerprint

Design invariants
-----------------
* The gate NEVER proves a call safe.  An unresolved call only ever widens the scanned set.
* Attribute calls are external only on POSITIVE receiver proof.  Absence of an
  attribute-name match is never evidence of safety.
* Escalation expands coverage and never weakens the role policy.
* Phase 3 is independent: a defect in the reachability model cannot produce a fail-open.
* No dataflow, no CFG, no branch exclusion, no container/element provenance, no
  points-to analysis, no annotation-based safety proving.
* Expected values (roots, helpers, permitted instances, roles) live ONLY in the external
  manifest.  This file embeds none of them.

Read-only.  Pure ``ast.parse``.  No DB, no network, no server, no runtime import.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import w3_2_manifest_integrity as mi                      # noqa: E402
import w3_2_whole_scope_clock_index as ci                 # noqa: E402

BUILTIN_NAMES = frozenset(dir(builtins))

# Unresolved-dispatch diagnostic codes that trigger Phase-2 escalation.
ESCALATION_TRIGGERS = (
    "UNRESOLVED_LOCAL_DISPATCH",
    "UNRESOLVED_ATTRIBUTE_DISPATCH",
    "UNRESOLVED_SUBSCRIPT_DISPATCH",
    "GLOBALS_GET_DYNAMIC_KEY",
    "GETATTR_DYNAMIC",
    "CALL_RESULT_INVOKED",
    "UNRESOLVED_CONSTRUCTOR_DISPATCH",
    "CUSTOM_LOCAL_METACLASS",
)


# ======================================================================================
# Candidate harvesting (Phase 1.2)
# ======================================================================================

class Harvest(object):
    def __init__(self):
        self.local = []          # (name, harvest_kind, lineno)
        self.classes = []        # (class_name, lineno)
        self.external = []       # (code, text, lineno)
        self.unresolved = []     # (code, text, lineno)
        self.widened = []        # (name, lineno)  attribute-name widening only


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


def _is_literal_receiver(node):
    return isinstance(node, (ast.Constant, ast.JoinedStr, ast.List, ast.Dict,
                             ast.Set, ast.Tuple, ast.ListComp, ast.SetComp,
                             ast.DictComp, ast.GeneratorExp))


def _module_stem(mod):
    return (mod or "").split(".")[0]


class ScopeResolver(object):
    """Resolution helpers over the parsed scope (no value analysis anywhere)."""

    def __init__(self, files):
        self.files = files
        self.scope_file_stems = {f[:-3]: f for f in files if f.endswith(".py")}
        self.universe_names = set()
        self.by_name = {}
        for fname in files:
            for d in files[fname].defs:
                self.universe_names.add(d.name)
                self.by_name.setdefault(d.name, []).append(d)
                if d.qualified_name != d.name:
                    self.universe_names.add(d.qualified_name)
                    self.by_name.setdefault(d.qualified_name, []).append(d)
        self.class_by_name = {}
        for fname in files:
            for c in files[fname].classes:
                self.class_by_name.setdefault(c.name, []).append(c)

    def import_kind(self, fi, ident):
        """Return ('in_scope', target_file) / ('external', module) / None."""
        binding = fi.import_bindings.get(ident)
        if binding is None:
            return None
        module, is_from, _asname = binding
        stem = _module_stem(module)
        if stem in self.scope_file_stems:
            return ("in_scope", self.scope_file_stems[stem])
        return ("external", module)


def harvest_candidates(defrec, fi, resolver):
    """Callable-context-sensitive candidate harvesting for one definition instance."""
    h = Harvest()
    node = defrec.node

    def add_name_candidate(ident, kind, line):
        if ident in resolver.universe_names:
            h.local.append((ident, kind, line))
            return True
        return False

    def resolve_call_name(ident, line, kind="call_name"):
        imp = resolver.import_kind(fi, ident)
        if imp is not None:
            if imp[0] == "in_scope":
                target = imp[1]
                binding = fi.import_bindings[ident]
                original = binding[2] and ident or ident
                # `from storage import w3_now as _x` -> resolve the ORIGINAL name
                orig_name = ident
                for n in ast.walk(fi.tree):
                    if isinstance(n, ast.ImportFrom) and n.module and \
                            _module_stem(n.module) in resolver.scope_file_stems:
                        for a in n.names:
                            if (a.asname or a.name) == ident:
                                orig_name = a.name
                if orig_name in resolver.universe_names:
                    h.local.append((orig_name, "in_scope_import", line))
                else:
                    h.external.append(("EXTERNAL_IMPORT",
                                       "%s (in-scope module %s, no matching def)"
                                       % (ident, target), line))
                del original
                return
            h.external.append(("EXTERNAL_IMPORT", ident, line))
            return
        if ident in fi.module_capture:
            captured = fi.module_capture[ident]
            if not add_name_candidate(captured, "module_capture_binding", line):
                h.unresolved.append(("UNRESOLVED_LOCAL_DISPATCH",
                                     "%s -> capture %r (no matching def)"
                                     % (ident, captured), line))
            return
        if ident in resolver.class_by_name:
            h.classes.append((ident, line))
            return
        if add_name_candidate(ident, kind, line):
            return
        if ident in BUILTIN_NAMES:
            h.external.append(("EXTERNAL_BUILTIN", ident, line))
            return
        h.unresolved.append(("UNRESOLVED_LOCAL_DISPATCH", ident, line))

    def handle_call(n):
        func = n.func
        line = n.lineno

        # ---- Call(Name(x))
        if isinstance(func, ast.Name):
            ident = func.id
            if ident == "getattr":
                if len(n.args) >= 2 and isinstance(n.args[1], ast.Constant) \
                        and isinstance(n.args[1].value, str):
                    add_name_candidate(n.args[1].value, "getattr_literal", line)
                    h.external.append(("EXTERNAL_BUILTIN", "getattr", line))
                else:
                    h.unresolved.append(("GETATTR_DYNAMIC", ast.unparse(n)[:120], line))
                return
            if ident == "globals":
                return
            resolve_call_name(ident, line)
            return

        # ---- Call(Attribute(base, attr))
        if isinstance(func, ast.Attribute):
            root = _base_root(func)
            attr = func.attr
            classified = None
            if isinstance(root, ast.Name):
                imp = resolver.import_kind(fi, root.id)
                if imp is not None:
                    if imp[0] == "in_scope":
                        if attr in resolver.universe_names:
                            h.local.append((attr, "in_scope_module_attr", line))
                            classified = "resolved"
                        else:
                            h.external.append(
                                ("EXTERNAL_ATTR_IMPORT",
                                 "%s.%s (in-scope module, no matching def)"
                                 % (root.id, attr), line))
                            classified = "external"
                    else:
                        h.external.append(("EXTERNAL_ATTR_IMPORT",
                                           "%s.%s" % (root.id, attr), line))
                        classified = "external"
                elif root.id in BUILTIN_NAMES:
                    h.external.append(("EXTERNAL_ATTR_BUILTIN",
                                       "%s.%s" % (root.id, attr), line))
                    classified = "external"
            elif _is_literal_receiver(root):
                h.external.append(("EXTERNAL_ATTR_LITERAL", ast.unparse(func)[:120], line))
                classified = "external"
            elif isinstance(root, ast.Call):
                inner = root.func
                proven = False
                if isinstance(inner, ast.Name):
                    if inner.id in BUILTIN_NAMES and inner.id not in resolver.universe_names:
                        proven = True
                    else:
                        imp = resolver.import_kind(fi, inner.id)
                        if imp is not None and imp[0] == "external":
                            proven = True
                elif isinstance(inner, ast.Attribute):
                    iroot = _base_root(inner)
                    if isinstance(iroot, ast.Name):
                        imp = resolver.import_kind(fi, iroot.id)
                        if (imp is not None and imp[0] == "external") or \
                                (imp is None and iroot.id in BUILTIN_NAMES):
                            proven = True
                if proven:
                    h.external.append(("EXTERNAL_ATTR_BUILTIN_CALL",
                                       ast.unparse(func)[:120], line))
                    classified = "external"

            if classified is None:
                # NOT proven external -> unresolved.  Never terminated on the argument
                # that the attribute name matches no definition name (the R4 prohibition).
                h.unresolved.append(("UNRESOLVED_ATTRIBUTE_DISPATCH",
                                     ast.unparse(func)[:120], line))

            # Widening only: an attribute name that matches a universe name adds a
            # candidate.  It is never a reason to stop.
            if attr in resolver.universe_names:
                h.local.append((attr, "attribute_name_widening", line))
                h.widened.append((attr, line))
            return

        # ---- Call(Subscript(base, key))
        if isinstance(func, ast.Subscript):
            key = func.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                add_name_candidate(key.value, "literal_registry_key", line)
            else:
                h.unresolved.append(("UNRESOLVED_SUBSCRIPT_DISPATCH",
                                     ast.unparse(func)[:120], line))
            return

        # ---- Call(Call(...))
        if isinstance(func, ast.Call):
            h.unresolved.append(("CALL_RESULT_INVOKED", ast.unparse(func)[:120], line))
            return

        h.unresolved.append(("UNRESOLVED_LOCAL_DISPATCH", ast.unparse(func)[:120], line))

    def handle_globals_dispatch(n):
        """globals().get("x"[, fb]) and globals()["x"] anywhere in the subtree."""
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == "get" and isinstance(n.func.value, ast.Call) \
                and isinstance(n.func.value.func, ast.Name) \
                and n.func.value.func.id == "globals":
            if n.args and isinstance(n.args[0], ast.Constant) \
                    and isinstance(n.args[0].value, str):
                add_name_candidate(n.args[0].value, "globals_get_literal", n.lineno)
            else:
                h.unresolved.append(("GLOBALS_GET_DYNAMIC_KEY", ast.unparse(n)[:120],
                                     n.lineno))
        elif isinstance(n, ast.Subscript) and isinstance(n.value, ast.Call) \
                and isinstance(n.value.func, ast.Name) and n.value.func.id == "globals":
            if isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                add_name_candidate(n.slice.value, "globals_subscript_literal", n.lineno)
            else:
                h.unresolved.append(("GLOBALS_GET_DYNAMIC_KEY", ast.unparse(n)[:120],
                                     n.lineno))

    def bare_name(value, kind, line):
        if isinstance(value, ast.Name):
            add_name_candidate(value.id, kind, line)

    for n in ast.walk(node):
        handle_globals_dispatch(n)

        if isinstance(n, ast.Call):
            handle_call(n)
            for a in n.args:
                bare_name(a, "call_argument", n.lineno)
            for kw in n.keywords:
                bare_name(kw.value, "keyword_argument", n.lineno)

        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            val = getattr(n, "value", None)
            if val is None:
                continue
            bare_name(val, "alias_assign", n.lineno)
            if isinstance(val, (ast.List, ast.Tuple, ast.Set)):
                for e in val.elts:
                    bare_name(e, "container_literal_element", n.lineno)
            elif isinstance(val, ast.Dict):
                for v in val.values:
                    bare_name(v, "dict_literal_value", n.lineno)

        elif isinstance(n, ast.Return):
            if n.value is not None:
                bare_name(n.value, "return_bare_name", n.lineno)

        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                bare_name(d, "decorator", n.lineno)
            for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x]:
                bare_name(d, "parameter_default", n.lineno)

    return h


# ======================================================================================
# Phase 1 + Phase 2
# ======================================================================================

def run_closure(files, resolver, manifest):
    roots_spec = manifest["roots"]
    budget = manifest["closure_budget"]

    closure = {}          # key -> DefRecord
    reasons = {}          # key -> list of reason dicts
    unresolved_sites = []
    external_sites = []
    widening_sites = []
    class_records = []
    name_collisions = []
    ctor_notes = []

    def add_def(defrec, reason):
        k = defrec.key
        reasons.setdefault(k, []).append(reason)
        if k not in closure:
            closure[k] = defrec
            return True
        return False

    # ---- 1.1 seed: ALL generations of every frozen root
    missing_roots = []
    for spec in roots_spec:
        fname, rname = spec["file"], spec["name"]
        gens = [d for d in files[fname].defs if d.qualified_name == rname]
        if not gens:
            missing_roots.append(spec)
            continue
        for d in gens:
            add_def(d, {"kind": "root_seed", "root": rname, "file": fname})

    def expand_name(candidate, src):
        added = 0
        targets = resolver.by_name.get(candidate, [])
        hit_files = {t.file for t in targets}
        if len(hit_files) > 1:
            name_collisions.append({
                "name": candidate,
                "files": sorted(hit_files),
                "from": src["from_def"],
                "code": "CONSERVATIVE_NAME_COLLISION_INCLUSION",
            })
        for t in targets:
            if add_def(t, src):
                added += 1
        return added

    worklist = list(closure.values())
    seen_harvest = set()
    iterations = 0
    blowup = False

    while worklist:
        iterations += 1
        if len(closure) > budget:
            blowup = True
            break
        current = worklist
        worklist = []
        for defrec in current:
            if defrec.key in seen_harvest:
                continue
            seen_harvest.add(defrec.key)
            fi = files[defrec.file]
            h = harvest_candidates(defrec, fi, resolver)

            for code, text, line in h.unresolved:
                unresolved_sites.append({
                    "code": code, "detail": text, "lineno": line,
                    "file": defrec.file, "qualified_name": defrec.qualified_name,
                    "from_def": defrec.ident(),
                })
            for code, text, line in h.external:
                external_sites.append({
                    "code": code, "detail": text, "lineno": line,
                    "file": defrec.file, "qualified_name": defrec.qualified_name,
                })
            for name, line in h.widened:
                widening_sites.append({"name": name, "lineno": line,
                                       "file": defrec.file,
                                       "qualified_name": defrec.qualified_name})

            for name, kind, line in h.local:
                src = {"kind": kind, "from_def": defrec.ident(), "lineno": line,
                       "candidate": name}
                before = set(closure)
                expand_name(name, src)
                for k in set(closure) - before:
                    worklist.append(closure[k])

            for cname, line in h.classes:
                for crec in resolver.class_by_name.get(cname, []):
                    rec = {"class": cname, "file": crec.file, "lineno": crec.lineno,
                           "metaclass": crec.metaclass,
                           "bases": crec.bases,
                           "from_def": defrec.ident()}
                    if crec.metaclass:
                        is_local_meta = crec.metaclass.split(".")[-1] in resolver.class_by_name
                        rec["custom_local_metaclass"] = bool(is_local_meta)
                        if is_local_meta:
                            unresolved_sites.append({
                                "code": "CUSTOM_LOCAL_METACLASS",
                                "detail": "class %s metaclass=%s" % (cname, crec.metaclass),
                                "lineno": crec.lineno, "file": crec.file,
                                "qualified_name": defrec.qualified_name,
                                "from_def": defrec.ident(),
                            })
                    ctors = list(crec.constructors)
                    # in-scope base classes contribute their constructors too
                    for b in crec.bases:
                        for bc in resolver.class_by_name.get(b.split(".")[-1], []):
                            ctors.extend(bc.constructors)
                    if not ctors:
                        rec["structurally_empty_constructor"] = True
                        rec["code"] = "STRUCTURALLY_EMPTY_CONSTRUCTOR"
                    else:
                        rec["constructors_added"] = []
                        for c in ctors:
                            if add_def(c, {"kind": "local_constructor",
                                           "from_def": defrec.ident(),
                                           "class": cname, "lineno": line}):
                                worklist.append(c)
                            rec["constructors_added"].append(c.ident())
                    class_records.append(rec)
                    ctor_notes.append(rec)

    # ---- Phase 2 escalation
    escalations = []
    escalated_files = []
    trigger_by_file = {}
    for site in unresolved_sites:
        trigger_by_file.setdefault(site["file"], []).append(site)

    escalated_defs = {}
    escalation_added = {}
    for fname, sites in sorted(trigger_by_file.items()):
        if fname not in files:
            escalations.append({"file": fname, "code": "ESCALATION_IMPOSSIBLE",
                                "detail": "home file is outside the frozen scope"})
            continue
        escalated_files.append(fname)
        added_keys = []
        for d in files[fname].defs:
            if d.key not in closure and d.key not in escalated_defs:
                escalated_defs[d.key] = d
                added_keys.append(d.key)
        escalation_added[fname] = added_keys
        first = sites[0]
        escalations.append({
            "file": fname,
            "trigger_definition": first["from_def"],
            "trigger_lineno": first["lineno"],
            "diagnostic_code": first["code"],
            "trigger_site_total": len(sites),
            "trigger_codes": sorted({s["code"] for s in sites}),
            "definitions_added": len(added_keys),
            "definitions_in_file": len(files[fname].defs),
            "clock_touching_added": None,   # filled by the driver once Phase 3 has run
        })

    return {
        "closure": closure,
        "reasons": reasons,
        "unresolved_sites": unresolved_sites,
        "external_sites": external_sites,
        "widening_sites": widening_sites,
        "class_records": class_records,
        "name_collisions": name_collisions,
        "missing_roots": missing_roots,
        "iterations": iterations,
        "blowup": blowup,
        "budget": budget,
        "escalations": escalations,
        "escalated_files": escalated_files,
        "escalated_defs": escalated_defs,
        "escalation_added": escalation_added,
    }


# ======================================================================================
# Phase 5 -- frozen binding integrity
# ======================================================================================

def check_bindings(files, manifest):
    findings = []
    detail = {"roots": [], "approved_helpers": []}

    for spec in manifest["roots"]:
        fname, rname = spec["file"], spec["name"]
        gens = [d for d in files[fname].defs if d.qualified_name == rname]
        row = {"file": fname, "name": rname,
               "expected_generation_count": spec["generation_count"],
               "measured_generation_count": len(gens),
               "expected_active_index": spec["active_index"],
               "measured_active_index": (len(gens) - 1) if gens else None,
               "expected_active_fingerprint": spec["active_fingerprint"],
               "measured_active_fingerprint": gens[-1].fingerprint if gens else None,
               "status": "OK"}
        if not gens:
            row["status"] = "FROZEN_ROOT_MISSING"
            findings.append({"code": "FROZEN_ROOT_MISSING", "file": fname,
                             "qualified_name": rname, "lineno": 0,
                             "detail": "frozen root not present in %s" % fname,
                             "layer": "phase5_bindings"})
        else:
            if len(gens) != spec["generation_count"]:
                row["status"] = "ROOT_BINDING_DRIFT"
                findings.append({"code": "ROOT_BINDING_DRIFT", "file": fname,
                                 "qualified_name": rname, "lineno": gens[-1].lineno,
                                 "detail": "generation count %d != manifest %d"
                                           % (len(gens), spec["generation_count"]),
                                 "layer": "phase5_bindings"})
            if (len(gens) - 1) != spec["active_index"]:
                row["status"] = "ROOT_BINDING_DRIFT"
                findings.append({"code": "ROOT_BINDING_DRIFT", "file": fname,
                                 "qualified_name": rname, "lineno": gens[-1].lineno,
                                 "detail": "active index %d != manifest %d"
                                           % (len(gens) - 1, spec["active_index"]),
                                 "layer": "phase5_bindings"})
            if gens[-1].fingerprint != spec["active_fingerprint"]:
                row["status"] = "ROOT_BINDING_DRIFT"
                findings.append({"code": "ROOT_BINDING_DRIFT", "file": fname,
                                 "qualified_name": rname, "lineno": gens[-1].lineno,
                                 "detail": "active body fingerprint drift",
                                 "layer": "phase5_bindings"})
        detail["roots"].append(row)

    for spec in manifest["approved_helpers"]:
        fname, qname = spec["file"], spec["qualified_name"]
        gens = [d for d in files[fname].defs if d.qualified_name == qname]
        gi = spec["generation_index"]
        row = {"file": fname, "qualified_name": qname, "generation_index": gi,
               "expected_fingerprint": spec["fingerprint"], "status": "OK"}
        if gi >= len(gens):
            row["status"] = "APPROVED_HELPER_CONTRACT_DRIFT"
            row["measured_fingerprint"] = None
            findings.append({"code": "APPROVED_HELPER_CONTRACT_DRIFT", "file": fname,
                             "qualified_name": qname, "lineno": 0,
                             "detail": "approved helper generation %d not present" % gi,
                             "layer": "phase5_bindings"})
        else:
            row["measured_fingerprint"] = gens[gi].fingerprint
            if gens[gi].fingerprint != spec["fingerprint"]:
                row["status"] = "APPROVED_HELPER_CONTRACT_DRIFT"
                findings.append({"code": "APPROVED_HELPER_CONTRACT_DRIFT", "file": fname,
                                 "qualified_name": qname, "lineno": gens[gi].lineno,
                                 "detail": "approved helper body fingerprint drift",
                                 "layer": "phase5_bindings"})
        detail["approved_helpers"].append(row)

    detail["wrapper_registry"] = []
    for spec in manifest.get("wrapper_registry") or []:
        fname, qname = spec["file"], spec["qualified_name"]
        gens = [d for d in files[fname].defs if d.qualified_name == qname]
        gi = spec["generation_index"]
        row = {"file": fname, "qualified_name": qname, "generation_index": gi,
               "expected_fingerprint": spec["fingerprint"], "status": "OK"}
        if gi >= len(gens):
            row["status"] = "FROZEN_WRAPPER_MISSING"
            row["measured_fingerprint"] = None
            findings.append({"code": "FROZEN_WRAPPER_MISSING", "file": fname,
                             "qualified_name": qname, "lineno": 0,
                             "detail": "registered wrapper generation %d not present" % gi,
                             "layer": "phase5_bindings"})
        else:
            row["measured_fingerprint"] = gens[gi].fingerprint
            if gens[gi].fingerprint != spec["fingerprint"]:
                row["status"] = "APPROVED_HELPER_CONTRACT_DRIFT"
                findings.append({"code": "APPROVED_HELPER_CONTRACT_DRIFT", "file": fname,
                                 "qualified_name": qname, "lineno": gens[gi].lineno,
                                 "detail": "registered wrapper body fingerprint drift",
                                 "layer": "phase5_bindings"})
        detail["wrapper_registry"].append(row)

    # WRAPPER_REGISTRY_INCOMPLETE: a clock_index row declared as a wrapper-definition
    # (WRAPPER_DELEGATION:NONE) whose identity has been removed from wrapper_registry --
    # never a silent disappearance (plan 08 sec 2).
    wrapper_keys = {(w["file"], w["qualified_name"]) for w in manifest.get("wrapper_registry") or []}
    for row in manifest.get("clock_index") or []:
        if "WRAPPER_DELEGATION:NONE" in (row.get("permitted_forms") or []):
            if (row["file"], row["qualified_name"]) not in wrapper_keys:
                findings.append({"code": "WRAPPER_REGISTRY_INCOMPLETE", "file": row["file"],
                                 "qualified_name": row["qualified_name"],
                                 "lineno": row["source_span"][0],
                                 "detail": "wrapper-definition row has no wrapper_registry entry",
                                 "layer": "phase5_bindings"})

    return findings, detail


# ======================================================================================
# Gate driver
# ======================================================================================

def run_gate(repo_root=r"C:\ALM_TPilot", manifest_path=None, expected_digest=None):
    result = {
        "ok": False,
        "findings": [],
        "metrics": {},
        "escalations": [],
        "deferred_exceptions": [],
        "manifest": {},
    }

    try:
        manifest, integrity = mi.load_manifest(manifest_path, None, expected_digest)
    except mi.ManifestError as exc:
        result["findings"].append({"code": exc.code, "file": "-", "qualified_name": "-",
                                   "lineno": 0, "detail": exc.detail,
                                   "layer": "manifest_integrity"})
        result["metrics"]["manifest_loaded"] = False
        return result

    result["manifest"] = integrity
    result["metrics"]["manifest_loaded"] = True

    scope_files = manifest["scope_files"]
    files = ci.build_scope_index(repo_root, scope_files)
    resolver = ScopeResolver(files)
    zc = ci.ZoneClassifier(manifest.get("zone_classification", {}))
    wrapper_registry = manifest.get("wrapper_registry") or []
    wrappers = {"%s::%s" % (w["file"], w["qualified_name"]) for w in wrapper_registry}
    wrapper_idx = ci.build_wrapper_index(wrapper_registry)

    findings = []

    # ---- Phase 5 (runs first so drift is visible even if closure changes)
    binding_findings, binding_detail = check_bindings(files, manifest)
    findings.extend(binding_findings)

    # ---- H2 role/form-family coherence (structured finding; validate_schema() already
    # refuses to load a manifest built with a coherence violation -- this is defense in
    # depth for anything that reaches this point regardless).
    findings.extend(ci.check_role_family_coherence(manifest.get("clock_index") or []))

    # ---- H4 duplicate clock-site ownership, across the whole manifest.
    findings.extend(ci.check_duplicate_site_ownership(manifest))

    # ---- H5 mixed_clock_scan_targets integrity -- independent of role, independent of
    # site_contracts, run at every invocation (plan 08 sec 3, closes circularity P-2).
    mixed_findings, mixed_totals = ci.adjudicate_mixed_targets(files, manifest, wrapper_idx)
    findings.extend(mixed_findings)

    # ---- Phase 1 + Phase 2
    cl = run_closure(files, resolver, manifest)
    if cl["blowup"]:
        findings.append({"code": "CLOSURE_BLOWUP", "file": "-", "qualified_name": "-",
                         "lineno": 0,
                         "detail": "closure exceeded CLOSURE_BUDGET=%d" % cl["budget"],
                         "layer": "phase1_closure"})
    for esc in cl["escalations"]:
        if esc.get("code") == "ESCALATION_IMPOSSIBLE":
            findings.append({"code": "ESCALATION_IMPOSSIBLE", "file": esc["file"],
                             "qualified_name": "-", "lineno": 0,
                             "detail": esc["detail"], "layer": "phase2_escalation"})

    scanned = dict(cl["closure"])
    scanned.update(cl["escalated_defs"])

    # ---- Phase 3 (independent, unconditional)
    _files3, index_rows, index_findings, index_totals = ci.run_index_against_manifest(
        repo_root, manifest)
    findings.extend(index_findings)

    # A DEFERRED_W3_4 role is a visible declaration, never a silent permission: every
    # such clock_index row must have a matching deferred_entries record naming a
    # finding ID.  Removing the declaration (while leaving the role) must turn RED,
    # not silently keep permitting the form (mutation M-25).
    deferred_keys = {(d.get("file"), d.get("qualified_name"), d.get("generation_index"))
                     for d in manifest.get("deferred_entries") or []}
    for row in manifest.get("clock_index") or []:
        if row.get("role") != "DEFERRED_W3_4":
            continue
        key = (row.get("file"), row.get("qualified_name"), row.get("generation_index"))
        if key not in deferred_keys:
            findings.append({
                "code": "DEFERRED_DECLARATION_MISSING",
                "file": row.get("file"), "qualified_name": row.get("qualified_name"),
                "lineno": row.get("source_span", [0])[0],
                "detail": "role is DEFERRED_W3_4 but no deferred_entries record names "
                          "the finding ID -- a deferral must be a visible declaration, "
                          "never a silent permission",
                "layer": "phase3_index",
            })

    # ---- module-level unapproved time libraries (outside any definition)
    for fname in scope_files:
        fi = files[fname]
        for n in fi.tree.body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                mod = getattr(n, "module", None) or ""
                blob = (mod + " " + " ".join(a.name for a in n.names)).lower()
                if any(m in blob for m in ci.UNAPPROVED_TIME_LIB_MARKERS):
                    findings.append({"code": "UNAPPROVED_TIME_LIB", "file": fname,
                                     "qualified_name": "<module>", "lineno": n.lineno,
                                     "detail": ast.unparse(n), "layer": "policy"})

    # ---- clock-touching membership per layer (reporting only; policy is uniform)
    clock_keys = {(r["file"], r["qualified_name"], r["generation_index"])
                  for r in index_rows}
    closure_keys = set(cl["closure"])
    escalated_keys = set(cl["escalated_defs"])
    closure_ct = len(clock_keys & closure_keys)
    escalation_ct = len(clock_keys & escalated_keys)
    outside_ct = len(clock_keys - closure_keys)

    # Required escalation reporting: clock-touching definitions added per escalation.
    for esc in cl["escalations"]:
        if esc.get("code") == "ESCALATION_IMPOSSIBLE":
            continue
        added = cl["escalation_added"].get(esc["file"], [])
        esc["clock_touching_added"] = len([k for k in added if k in clock_keys])

    # ---- deferred exceptions, reported every run
    for row in manifest.get("deferred_entries", []):
        result["deferred_exceptions"].append({
            "code": "DEFERRED_EXCEPTION",
            "file": row["file"],
            "qualified_name": row["qualified_name"],
            "finding_id": row.get("finding_id"),
            "role": row.get("role"),
            "detail": row.get("note", ""),
        })

    # ---- independent cross-check of the definition universe
    independent_total = 0
    for fname in scope_files:
        src = (Path(repo_root) / fname).read_text(encoding="utf-8-sig")
        tree = ast.parse(src)
        independent_total += sum(
            1 for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    universe_total = len(ci.universe_defs(files))
    if independent_total != universe_total:
        findings.append({"code": "DEFINITION_UNIVERSE_CROSSCHECK_FAIL", "file": "-",
                         "qualified_name": "-", "lineno": 0,
                         "detail": "independent walk %d != indexed %d"
                                   % (independent_total, universe_total),
                         "layer": "phase0_index"})

    unresolved_by_code = {}
    for s in cl["unresolved_sites"]:
        unresolved_by_code[s["code"]] = unresolved_by_code.get(s["code"], 0) + 1
    external_by_code = {}
    for s in cl["external_sites"]:
        external_by_code[s["code"]] = external_by_code.get(s["code"], 0) + 1

    metrics = {
        "manifest_loaded": True,
        "manifest_sha256": integrity["computed_sha256"],
        "manifest_anchor": integrity["anchor"],
        "scope_files": scope_files,
        "whole_scope_def_total": universe_total,
        "definition_universe_crosscheck": independent_total,
        "closure_total": len(cl["closure"]),
        "closure_unique_names": len({d.qualified_name for d in cl["closure"].values()}),
        "closure_fixpoint_waves": cl["iterations"],
        "closure_budget": cl["budget"],
        "closure_fixpoint_reached": not cl["blowup"],
        "scanned_total": len(scanned),
        "escalated_def_total": len(cl["escalated_defs"]),
        "escalations_total": len([e for e in cl["escalations"]
                                  if e.get("code") != "ESCALATION_IMPOSSIBLE"]),
        "escalated_files": cl["escalated_files"],
        "unresolved_total": len(cl["unresolved_sites"]),
        "unresolved_distinct_sites": len({(s["file"], s["lineno"], s["detail"])
                                          for s in cl["unresolved_sites"]}),
        "unresolved_by_code": unresolved_by_code,
        "unresolved_name_dispatch_total":
            unresolved_by_code.get("UNRESOLVED_LOCAL_DISPATCH", 0),
        "unresolved_attribute_dispatch_total":
            unresolved_by_code.get("UNRESOLVED_ATTRIBUTE_DISPATCH", 0),
        "external_by_code": external_by_code,
        "attribute_name_widening_sites": len(cl["widening_sites"]),
        "conservative_name_collision_inclusions": len({c["name"] for c in cl["name_collisions"]}),
        "conservative_name_collision_events": len(cl["name_collisions"]),
        "local_classes_reached": len(cl["class_records"]),
        "local_metaclasses_reached": len([c for c in cl["class_records"]
                                          if c.get("metaclass")]),
        "structurally_empty_constructors": len([c for c in cl["class_records"]
                                                if c.get("structurally_empty_constructor")]),
        "whole_scope_clock_touching_total": index_totals["whole_scope_clock_touching_total"],
        "manifested_clock_touching_total": index_totals["manifested_clock_touching_total"],
        "unmanifested_clock_touching_total": index_totals["unmanifested_clock_touching_total"],
        "manifested_rows_not_measured": index_totals["manifested_rows_not_measured"],
        "closure_clock_touching_total": closure_ct,
        "escalation_clock_touching_total": escalation_ct,
        "outside_closure_clock_touching_total": outside_ct,
        "deferred_exceptions_total": len(result["deferred_exceptions"]),
        "policy_findings_total": len([f for f in findings if f["layer"] == "policy"]),
        "mixed_scan_target_total": mixed_totals["mixed_scan_target_total"],
        "mixed_role_row_total": mixed_totals["mixed_role_row_total"],
        "mixed_target_role_mismatch_total": mixed_totals["mixed_target_role_mismatch_total"],
        "mixed_target_missing_total": mixed_totals["mixed_target_missing_total"],
        "measured_mixed_site_total": mixed_totals["measured_mixed_site_total"],
        "contracted_mixed_site_total": mixed_totals["contracted_mixed_site_total"],
        "wrapper_registry_entries": len(wrapper_registry),
        "duplicate_clock_site_ownership_total": len(
            [f for f in findings if f["code"] == "DUPLICATE_CLOCK_SITE_OWNERSHIP"]),
        "role_contract_mismatch_total": len(
            [f for f in findings if f["code"] == "ROLE_CONTRACT_MISMATCH"]),
        "findings_total": 0,
    }

    if metrics["scanned_total"] != len(cl["closure"]) + len(cl["escalated_defs"]):
        findings.append({"code": "SCANNED_SET_ARITHMETIC_FAIL", "file": "-",
                         "qualified_name": "-", "lineno": 0,
                         "detail": "scanned_total != closure + escalated",
                         "layer": "phase1_closure"})

    metrics["findings_total"] = len(findings)
    result["metrics"] = metrics
    result["findings"] = findings
    result["escalations"] = cl["escalations"]
    result["binding_detail"] = binding_detail
    result["name_collisions"] = cl["name_collisions"]
    result["class_records"] = cl["class_records"]
    result["unresolved_sites"] = cl["unresolved_sites"]
    result["closure_members"] = sorted(
        "%s::%s@%d" % (d.file, d.qualified_name, d.lineno) for d in cl["closure"].values())
    result["index_rows"] = index_rows
    result["ok"] = not findings
    return result


def format_report(res, verbose=True):
    out = []
    m = res["metrics"]
    out.append("=" * 86)
    out.append("W3.2 DESIGN C+ CONSERVATIVE TIMEZONE SOURCE GATE")
    out.append("=" * 86)
    if not m.get("manifest_loaded"):
        for f in res["findings"]:
            out.append("[FAIL] %s -- %s" % (f["code"], f["detail"]))
        return "\n".join(out)

    out.append("manifest    : %s (%s)" % (m["manifest_sha256"], m["manifest_anchor"]))
    out.append("")
    out.append("-- Phase 0/1: conservative closure --------------------------------------")
    for k in ("whole_scope_def_total", "definition_universe_crosscheck", "closure_total",
              "closure_unique_names", "closure_fixpoint_waves", "closure_budget",
              "closure_fixpoint_reached", "conservative_name_collision_inclusions",
              "attribute_name_widening_sites", "local_classes_reached",
              "local_metaclasses_reached", "structurally_empty_constructors"):
        out.append("   %-42s %s" % (k, m[k]))
    out.append("")
    out.append("-- Dispatch classification ----------------------------------------------")
    out.append("   %-42s %s" % ("unresolved_total", m["unresolved_total"]))
    out.append("   %-42s %s" % ("unresolved_distinct_sites", m["unresolved_distinct_sites"]))
    for code, n in sorted(m["unresolved_by_code"].items()):
        out.append("      %-39s %s" % (code, n))
    for code, n in sorted(m["external_by_code"].items()):
        out.append("      %-39s %s" % (code, n))
    out.append("")
    out.append("-- Phase 2: whole-file escalation ---------------------------------------")
    out.append("   %-42s %s" % ("escalations_total", m["escalations_total"]))
    out.append("   %-42s %s" % ("escalated_def_total", m["escalated_def_total"]))
    out.append("   %-42s %s" % ("scanned_total", m["scanned_total"]))
    for e in res["escalations"]:
        if e.get("code") == "ESCALATION_IMPOSSIBLE":
            out.append("   [ESCALATION_IMPOSSIBLE] %s" % e["file"])
            continue
        out.append("   ESCALATED %-20s trigger=%s@%d code=%s defs_added=%d clock_added=%s"
                   % (e["file"], e["trigger_definition"], e["trigger_lineno"],
                      e["diagnostic_code"], e["definitions_added"],
                      e.get("clock_touching_added", "-")))
    out.append("   note: escalation expands coverage and never weakens the role policy.")
    out.append("")
    out.append("-- Phase 3: independent whole-scope clock index -------------------------")
    for k in ("whole_scope_clock_touching_total", "manifested_clock_touching_total",
              "unmanifested_clock_touching_total", "manifested_rows_not_measured",
              "closure_clock_touching_total", "escalation_clock_touching_total",
              "outside_closure_clock_touching_total"):
        out.append("   %-42s %s" % (k, m[k]))
    out.append("")
    out.append("-- Phase 5: frozen binding integrity ------------------------------------")
    for r in res["binding_detail"]["roots"]:
        out.append("   root   %-24s gens=%s active=%s  %s"
                   % (r["name"], r["measured_generation_count"],
                      r["measured_active_index"], r["status"]))
    drift = [h for h in res["binding_detail"]["approved_helpers"] if h["status"] != "OK"]
    out.append("   approved helpers checked: %d, drift: %d"
               % (len(res["binding_detail"]["approved_helpers"]), len(drift)))
    out.append("")
    out.append("-- Deferred exceptions (reported every run) -----------------------------")
    for d in res["deferred_exceptions"]:
        out.append("   DEFERRED_EXCEPTION %s::%s  finding=%s role=%s"
                   % (d["file"], d["qualified_name"], d["finding_id"], d["role"]))
    out.append("")
    if res["findings"]:
        out.append("-- FINDINGS ------------------------------------------------------------")
        for f in res["findings"][:60]:
            out.append("   %-34s %s::%s@%s  %s"
                       % (f["code"], f["file"], f["qualified_name"], f["lineno"],
                          f.get("detail", "")[:80]))
        if len(res["findings"]) > 60:
            out.append("   ... %d more" % (len(res["findings"]) - 60))
        out.append("")
        out.append("[FAIL] W3.2 timezone source gate RED -- %d finding(s)"
                   % len(res["findings"]))
    else:
        out.append("[OK] W3.2 timezone source gate GREEN")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="W3.2 Design C+ conservative timezone source gate")
    ap.add_argument("--repo", default=r"C:\ALM_TPilot")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--expected-digest", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    res = run_gate(args.repo, args.manifest, args.expected_digest)
    if not args.quiet:
        print(format_report(res))
    if args.json_out:
        dump = {k: v for k, v in res.items() if k != "index_rows"}
        dump["index_row_total"] = len(res.get("index_rows") or [])
        Path(args.json_out).write_text(
            json.dumps(dump, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
