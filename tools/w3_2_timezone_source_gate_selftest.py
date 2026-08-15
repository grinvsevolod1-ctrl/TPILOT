# -*- coding: utf-8 -*-
"""W3.2 Design C+ -- acceptance probes P-1 ... P-28 for the conservative timezone gate.

Every probe builds a synthetic scope in a temporary directory, generates a matching
external manifest with the same code path that produced the production manifest, and
runs the real gate against it.  No project file, no production manifest and no live
tree is read or written by any probe.

P-1  .. P-12  candidate harvesting, dispatch, role policy, bindings, manifest integrity
P-13 .. P-14  negative controls: branch reorder / loop with continue+break stay GREEN
P-15          undeclared clock-touching definition is RED
P-16 .. P-19  unknown-receiver attribute dispatch (P-19 negative control)
P-20          escalation cannot weaken the role policy
P-21 .. P-24  local class constructors (P-22 negative control)
P-25 .. P-28  independent whole-scope clock index
P-29 .. P-35  wrapper-discovery independence (W3.2 F-2 evidence correction, 2026-08-03;
              10_TEST_AND_MUTATION_PLAN.md sec 2 / acceptance 33; closes R-D2). Synthetic
              MIXED_CLOCK_CONTRACT scopes exercising the same H5 machinery (adjudicate_
              mixed_targets) that D7 (main.py::_manager_recover_link_limit) uses in
              production, built through build_manifest_from_seed()/write_manifest() like
              every other probe here -- never against the live tree or the production
              manifest.

Negative controls P-13, P-14, P-19 and P-22 exist to prove the gate is not always RED.
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import w3_2_manifest_integrity as mi                # noqa: E402
import w3_2_timezone_source_gate as gate            # noqa: E402
import w3_2_whole_scope_clock_index as ci           # noqa: E402

# --------------------------------------------------------------------------------------
# Fixture scaffolding.  The role table and zone patterns below are TEST scaffolding for
# synthetic scopes; the production values live only in the external manifest.
# --------------------------------------------------------------------------------------

_ROLES = {
    "BUSINESS_LOCAL": {"meaning": "kyiv wall clock", "family": "KYIV",
                       "violation_code": "UTC_AS_KYIV_SUBSTITUTE"},
    "DATE_ONLY_KYIV": {"meaning": "kyiv date", "family": "KYIV",
                       "violation_code": "UTC_AS_KYIV_SUBSTITUTE"},
    "UTC_PERSISTENCE": {"meaning": "naive utc storage", "family": "UTC",
                        "violation_code": "KYIV_AS_UTC_SUBSTITUTE"},
    "UTC_INSTANT": {"meaning": "aware utc instant", "family": "UTC",
                    "violation_code": "KYIV_AS_UTC_SUBSTITUTE"},
    "FRESHNESS_UTC": {"meaning": "utc staleness delta", "family": "UTC",
                      "violation_code": "KYIV_AS_UTC_SUBSTITUTE"},
    "MONOTONIC": {"meaning": "duration", "family": "MONOTONIC",
                  "violation_code": "WALL_CLOCK_IN_MONOTONIC_ROLE"},
    "DEFERRED_W3_4": {"meaning": "deferred instance", "family": "DEFERRED",
                      "violation_code": "DEFERRED_EXCEPTION"},
}

_ZONE_CFG = {
    "kyiv_patterns": ["Europe/Kyiv", "\\bw3_tz\\s*\\(", "\\bw3_now\\s*\\("],
    "utc_patterns": ["\\butc\\b", "\\bUTC\\b"],
    "kyiv_zone_literals": ["Europe/Kyiv"],
    "utc_zone_literals": ["UTC"],
}

_AWARE = {r: "test" for r in _ROLES}
_PURPOSE = {r: "test" for r in _ROLES}


class Fixture(object):
    """A synthetic scope plus its generated manifest."""

    def __init__(self, files, roots, role_declarations=None, wrappers=None,
                 approved_helpers=None, mixed_targets=None):
        self.dir = Path(tempfile.mkdtemp(prefix="w3_2_probe_"))
        self.files = dict(files)
        for name, src in files.items():
            (self.dir / name).write_text(src, encoding="utf-8")
        # `wrappers` stays the established probe-fixture shape (bare "file::qualname"
        # strings); translated here into wrapper_registry entries (family defaults to
        # KYIV -- irrelevant to P-1..P-28, none of which exercise wrapper-CALL detection,
        # only whether a definition itself is a registered wrapper).
        wrapper_registry = []
        for w in (wrappers or []):
            wfile, _, wqname = w.partition("::")
            wrapper_registry.append({"file": wfile, "qualified_name": wqname,
                                     "produced_family": "KYIV", "contract_note": "test fixture"})
        # `mixed_targets`: list of {"file", "qualified_name", "contract_note",
        # "site_contracts": [{"form": TOKEN, "site_role": ROLE}, ...]} -- the seed shape
        # for mixed_clock_scan_targets (P-29 .. P-35, W3.2 F-2 correction).
        self.seed = {
            "scope_files": sorted(files),
            "roots": roots,
            "approved_helpers": approved_helpers or [],
            "wrapper_registry": wrapper_registry,
            "mixed_clock_scan_targets": list(mixed_targets or []),
            "zone_classification": _ZONE_CFG,
            "roles": dict(_ROLES, MIXED_CLOCK_CONTRACT={
                "meaning": "mixed per-site", "family": "MIXED",
                "violation_code": "SITE_CONTRACT_UNMATCHED"}),
            "aware_by_role": _AWARE,
            "purpose_by_role": _PURPOSE,
            "role_declarations": role_declarations or {},
        }
        self.manifest_path = self.dir / "manifest.json"
        self.digest = None

    def build_manifest(self):
        manifest = ci.build_manifest_from_seed(str(self.dir), self.seed)
        self.digest = ci.write_manifest(manifest, self.manifest_path)
        return manifest

    def run_mutated_manifest(self, manifest, mutate):
        """Apply `mutate` to a deep copy of an already-built manifest dict, write it to a
        SEPARATE path inside the same temp dir (never touching self.manifest_path/digest,
        so the fixture's own baseline stays re-runnable), correctly re-pin its digest, and
        run the gate against the mutant. Source files are untouched by this path -- it
        mutates only the manifest, matching plan sec 3's "source unchanged" mutations."""
        mutated = copy.deepcopy(manifest)
        mutate(mutated)
        mut_path = self.dir / "manifest_mut.json"
        digest = ci.write_manifest(mutated, mut_path)
        return gate.run_gate(str(self.dir), str(mut_path), digest)

    def rewrite(self, name, src):
        (self.dir / name).write_text(src, encoding="utf-8")

    def run(self):
        return gate.run_gate(str(self.dir), str(self.manifest_path), self.digest)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def codes(res):
    return sorted({f["code"] for f in res["findings"]})


def has(res, code):
    return any(f["code"] == code for f in res["findings"])


# --------------------------------------------------------------------------------------
# Shared source snippets
# --------------------------------------------------------------------------------------

HEAD = (
    "from datetime import datetime, timezone, timedelta\n"
    "from zoneinfo import ZoneInfo\n"
    "import time\n"
    "import os\n"
    "import telethon\n"
    "\n"
)

ROOTS_ONE = [{"file": "probe_main.py", "name": "_root"}]


def probe(fn):
    fn._is_probe = True
    return fn


# ======================================================================================
# P-1 .. P-12
# ======================================================================================

@probe
def P_1_ui_strings_are_not_candidates():
    """A UI string equal to a definition name must not create a candidate or escalation."""
    base = HEAD + (
        "def _unsafe_helper():\n"
        "    return 1\n"
        "\n"
        "def _root():\n"
        "    label = '_unsafe_helper'\n"
        "    return 'header ' + label\n"
    )
    mutant = HEAD + (
        "def _unsafe_helper():\n"
        "    return datetime.now()\n"
        "\n"
        "def _root():\n"
        "    label = '_unsafe_helper'\n"
        "    return 'header ' + label\n"
    )
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["ok"], codes(r0)
        n0 = r0["metrics"]["closure_total"]
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        # the clock is caught by the INDEX (phase 3), never by a string-derived edge
        assert r1["metrics"]["closure_total"] == n0, "string became a candidate"
        assert r1["metrics"]["escalations_total"] == 0, "string triggered escalation"
        assert has(r1, "BARE_NOW"), codes(r1)
        assert not any(m.endswith("_unsafe_helper@2") for m in r1["closure_members"]), \
            "_unsafe_helper entered the closure via a UI string"
        return True, "string not a candidate; clock caught by the index only"
    finally:
        f.close()


@probe
def P_2_json_and_sql_keys_are_not_candidates():
    base = HEAD + (
        "def _helper_name():\n"
        "    return 1\n"
        "\n"
        "def _root():\n"
        "    row = {'_helper_name': 'x'}\n"
        "    sql = 'SELECT _helper_name FROM t'\n"
        "    return str(row) + sql\n"
    )
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["closure_total"] == 1, r["closure_members"]
        assert r["metrics"]["escalations_total"] == 0
        return True, "dict key and SQL text produced no candidate and no escalation"
    finally:
        f.close()


@probe
def P_3_literal_callable_registry_is_followed():
    base = HEAD + (
        "def _reg_target():\n"
        "    return 1\n"
        "\n"
        "def _root():\n"
        "    reg = {'a': _reg_target}\n"
        "    return reg['a']()\n"
    )
    mutant = base.replace("def _reg_target():\n    return 1",
                          "def _reg_target():\n    return datetime.now()")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["ok"], codes(r0)
        assert any("_reg_target" in m for m in r0["closure_members"]), r0["closure_members"]
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        assert has(r1, "BARE_NOW"), codes(r1)
        return True, "registry value followed; forbidden source in the target is RED"
    finally:
        f.close()


@probe
def P_4_literal_globals_get_dispatch_is_followed():
    base = HEAD + (
        "def _prev_impl():\n"
        "    return 1\n"
        "\n"
        "def _root():\n"
        "    fn = globals().get('_prev_impl')\n"
        "    return fn()\n"
    )
    mutant = base.replace("def _prev_impl():\n    return 1",
                          "def _prev_impl():\n    return date_today()")
    mutant = mutant.replace("import os\n", "import os\nfrom datetime import date\n")
    mutant = mutant.replace("return date_today()", "return date.today()")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["ok"], codes(r0)
        assert any("_prev_impl" in m for m in r0["closure_members"]), r0["closure_members"]
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        assert has(r1, "BARE_TODAY"), codes(r1)
        return True, "globals().get literal key followed; date.today() is RED"
    finally:
        f.close()


@probe
def P_5_unresolved_dynamic_dispatch_escalates():
    src = HEAD + (
        "def _root(key):\n"
        "    fn = globals().get(key)\n"
        "    return fn()\n"
        "\n"
        "def _never_called_directly():\n"
        "    return 1\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["metrics"]["escalations_total"] >= 1, r["metrics"]
        trig = {c for e in r["escalations"] for c in e.get("trigger_codes", [])}
        assert "GLOBALS_GET_DYNAMIC_KEY" in trig or "CALL_RESULT_INVOKED" in trig, trig
        assert r["metrics"]["scanned_total"] == r["metrics"]["whole_scope_def_total"]
        return True, "dynamic globals key escalated the home file whole (%s)" % sorted(trig)
    finally:
        f.close()


@probe
def P_6_external_stdlib_and_telethon_calls_stay_external():
    src = HEAD + (
        "def _root():\n"
        "    p = os.path.join('a', 'b')\n"
        "    telethon.sync.start()\n"
        "    return p\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["escalations_total"] == 0, r["escalations"]
        assert r["metrics"]["external_by_code"].get("EXTERNAL_ATTR_IMPORT", 0) >= 2, \
            r["metrics"]["external_by_code"]
        return True, "os/telethon attribute calls recorded external, no escalation"
    finally:
        f.close()


@probe
def P_7_utc_form_green_in_its_declared_role():
    src = HEAD + (
        "def _root():\n"
        "    return _health_instant()\n"
        "\n"
        "def _health_instant():\n"
        "    return datetime.now(timezone.utc)\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE,
                role_declarations={"probe_main.py::_health_instant": {
                    "role": "UTC_INSTANT",
                    "contract_note": "aware UTC instant for the health payload"}})
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        return True, "datetime.now(timezone.utc) is GREEN in a declared UTC_INSTANT role"
    finally:
        f.close()


@probe
def P_8_same_utc_form_in_a_kyiv_root_is_red():
    base = HEAD + (
        "def _root():\n"
        "    return datetime.now(ZoneInfo('Europe/Kyiv')).isoformat()\n"
    )
    mutant = HEAD + (
        "def _root():\n"
        "    return datetime.now(ZoneInfo('Europe/Kyiv')).isoformat() + str(datetime.utcnow())\n"
    )
    f = Fixture({"probe_main.py": base}, ROOTS_ONE,
                role_declarations={"probe_main.py::_root": {
                    "role": "BUSINESS_LOCAL",
                    "contract_note": "grandfathered explicit Kyiv construction"}})
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["ok"], codes(r0)
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        assert has(r1, "UTC_AS_KYIV_SUBSTITUTE"), codes(r1)
        return True, "the identical UTC form is RED inside a BUSINESS_LOCAL root"
    finally:
        f.close()


@probe
def P_9_root_generation_drift_is_red():
    base = HEAD + "def _root():\n    return 'v1'\n"
    mutant = base + "\n\ndef _root():\n    return 'v2'\n"
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        assert f.run()["ok"]
        f.rewrite("probe_main.py", mutant)
        r = f.run()
        assert has(r, "ROOT_BINDING_DRIFT"), codes(r)
        return True, "a new active root generation is a blocking ROOT_BINDING_DRIFT"
    finally:
        f.close()


@probe
def P_10_missing_manifest_is_red():
    f = Fixture({"probe_main.py": HEAD + "def _root():\n    return 1\n"}, ROOTS_ONE)
    try:
        f.build_manifest()
        assert f.run()["ok"]
        f.manifest_path.unlink()
        r = f.run()
        assert has(r, "MANIFEST_MISSING"), codes(r)
        return True, "absent manifest is MANIFEST_MISSING"
    finally:
        f.close()


@probe
def P_11_corrupt_manifest_is_red():
    f = Fixture({"probe_main.py": HEAD + "def _root():\n    return 1\n"}, ROOTS_ONE)
    try:
        f.build_manifest()
        assert f.run()["ok"]
        raw = f.manifest_path.read_text(encoding="utf-8")
        f.manifest_path.write_text(raw.replace('"schema_version": 1',
                                               '"schema_version": 1 '), encoding="utf-8")
        r = f.run()
        assert has(r, "MANIFEST_INTEGRITY_FAIL"), codes(r)
        return True, "a byte-flipped manifest is MANIFEST_INTEGRITY_FAIL"
    finally:
        f.close()


@probe
def P_12_escalation_is_causal():
    """A forbidden source reachable ONLY through the escalation the fixture triggers."""
    base = HEAD + (
        "def _root(key):\n"
        "    fn = globals().get(key)\n"
        "    return fn()\n"
        "\n"
        "def _only_via_escalation():\n"
        "    return 1\n"
    )
    mutant = base.replace("def _only_via_escalation():\n    return 1",
                          "def _only_via_escalation():\n    return datetime.now()")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["ok"], codes(r0)
        assert not any("_only_via_escalation" in m for m in r0["closure_members"])
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        assert has(r1, "BARE_NOW"), codes(r1)
        esc = [e for e in r1["escalations"] if e["file"] == "probe_main.py"]
        assert esc and esc[0]["definitions_added"] >= 1, r1["escalations"]
        return True, ("escalation named probe_main.py (defs_added=%d) and the planted "
                      "source is RED" % esc[0]["definitions_added"])
    finally:
        f.close()


# ======================================================================================
# P-13 .. P-14  negative controls
# ======================================================================================

@probe
def P_13_branch_reorder_stays_green():
    """Branch order changes inside a NON-root helper reached from the closure.

    (The frozen root itself is deliberately fingerprint-pinned by Phase 5 -- ANY body
    change to it, semantic or not, is ROOT_BINDING_DRIFT. That is a different, and
    intentionally stricter, mechanism. This probe isolates branch-order invariance in
    ordinary closure members, which is what M-20a / the R7 branch-order probes concern.)
    """
    a = HEAD + (
        "def _safe():\n"
        "    return 1\n"
        "\n"
        "def _helper(flag):\n"
        "    if flag:\n"
        "        f = _safe\n"
        "    else:\n"
        "        f = _safe\n"
        "    return f()\n"
        "\n"
        "def _root(flag):\n"
        "    return _helper(flag)\n"
    )
    b = HEAD + (
        "def _safe():\n"
        "    return 1\n"
        "\n"
        "def _helper(flag):\n"
        "    if not flag:\n"
        "        f = _safe\n"
        "    else:\n"
        "        f = _safe\n"
        "    return f()\n"
        "\n"
        "def _root(flag):\n"
        "    return _helper(flag)\n"
    )
    f = Fixture({"probe_main.py": a}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["ok"], codes(r0)
        m0 = sorted(r0["closure_members"])
        f.rewrite("probe_main.py", b)
        r1 = f.run()
        assert r1["ok"], codes(r1)
        assert sorted(r1["closure_members"]) == m0, "branch order changed the closure"
        return True, ("GREEN under both branch orders in a non-root helper, identical "
                      "closure -- no dataflow fact is load-bearing")
    finally:
        f.close()


@probe
def P_14_safe_loop_with_continue_and_break_stays_green():
    src = HEAD + (
        "def _safe():\n"
        "    return 1\n"
        "\n"
        "def _root(items):\n"
        "    f = _safe\n"
        "    total = 0\n"
        "    for it in items:\n"
        "        if it == 0:\n"
        "            f = _safe\n"
        "            continue\n"
        "        if it < 0:\n"
        "            f = _safe\n"
        "            break\n"
        "        total += f()\n"
        "    while total < 3:\n"
        "        total += f()\n"
        "        continue\n"
        "    return total\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert any("_safe" in m for m in r["closure_members"])
        return True, ("GREEN: the D4/D5 shapes (continue/break, loop-carried rebinding) "
                      "are structurally inert -- reachability is over definitions")
    finally:
        f.close()


# ======================================================================================
# P-15  undeclared clock-touching definition
# ======================================================================================

@probe
def P_15_undeclared_clock_touching_def_is_red():
    base = HEAD + "def _root():\n    return 1\n"
    mutant = base + "\n\ndef _brand_new_clock():\n    return datetime.utcnow()\n"
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        assert f.run()["ok"]
        f.rewrite("probe_main.py", mutant)
        r = f.run()
        assert has(r, "UNMANIFESTED_CLOCK_TOUCHING_DEF"), codes(r)
        assert r["metrics"]["unmanifested_clock_touching_total"] == 1
        return True, "a clock-touching definition with no manifest entry is RED"
    finally:
        f.close()


# ======================================================================================
# P-16 .. P-19  unknown-receiver attribute dispatch
# ======================================================================================

@probe
def P_16_unknown_receiver_attribute_escalates():
    src = HEAD + (
        "def _root(obj):\n"
        "    return obj.render()\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["metrics"]["unresolved_attribute_dispatch_total"] >= 1, r["metrics"]
        assert r["metrics"]["escalations_total"] >= 1, r["escalations"]
        return True, "unknown receiver -> UNRESOLVED_ATTRIBUTE_DISPATCH -> escalation"
    finally:
        f.close()


@probe
def P_17_attribute_alias_cannot_bypass():
    """The mandated adversarial fixture: obj.tick = unsafe_local_helper; obj.tick().

    No definition named ``tick`` exists.  The call must NEVER be classified external
    on that basis, and the aliased helper's forbidden source must be caught.
    """
    base = HEAD + (
        "def unsafe_local_helper():\n"
        "    return 1\n"
        "\n"
        "def _root(obj):\n"
        "    obj.tick = unsafe_local_helper\n"
        "    return obj.tick()\n"
    )
    mutant = base.replace("def unsafe_local_helper():\n    return 1",
                          "def unsafe_local_helper():\n    return datetime.now()")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["metrics"]["unresolved_attribute_dispatch_total"] >= 1, \
            "obj.tick() was terminated as external although no def named tick exists"
        assert r0["metrics"]["escalations_total"] >= 1
        assert any("unsafe_local_helper" in m for m in r0["closure_members"]), \
            r0["closure_members"]
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        assert has(r1, "BARE_NOW"), codes(r1)
        return True, ("obj.tick() unresolved despite no matching def; the aliased helper "
                      "is in the closure and its forbidden source is RED")
    finally:
        f.close()


@probe
def P_18_imported_method_remains_external():
    src = HEAD + (
        "def _root():\n"
        "    return telethon.client.connect()\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["external_by_code"].get("EXTERNAL_ATTR_IMPORT", 0) >= 1
        assert r["metrics"]["escalations_total"] == 0
        return True, "telethon.client.connect() -> EXTERNAL_ATTR_IMPORT, no escalation"
    finally:
        f.close()


@probe
def P_19_proven_builtin_method_remains_external():
    """Negative control."""
    src = HEAD + (
        "def _root(n):\n"
        "    a = 'x'.strip()\n"
        "    b = str(n).upper()\n"
        "    c = [1, 2].count(1)\n"
        "    return a + b + str(c)\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["escalations_total"] == 0, r["escalations"]
        ext = r["metrics"]["external_by_code"]
        assert ext.get("EXTERNAL_ATTR_LITERAL", 0) >= 2, ext
        assert ext.get("EXTERNAL_ATTR_BUILTIN_CALL", 0) >= 1, ext
        return True, "builtin/literal receivers proven external, GREEN, no escalation"
    finally:
        f.close()


# ======================================================================================
# P-20  escalation cannot weaken policy
# ======================================================================================

@probe
def P_20_escalation_cannot_weaken_policy():
    base = HEAD + (
        "def _root(key):\n"
        "    fn = globals().get(key)\n"
        "    return fn()\n"
        "\n"
        "def _outside():\n"
        "    return 1\n"
    )
    mutant = base.replace("def _outside():\n    return 1",
                          "def _outside():\n    return time.localtime()")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["metrics"]["escalations_total"] >= 1
        assert r0["ok"], codes(r0)
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        assert has(r1, "HOST_LOCAL_TIME_API"), codes(r1)
        assert has(r1, "UNMANIFESTED_CLOCK_TOUCHING_DEF"), codes(r1)
        return True, ("a clock-touching definition brought in by escalation is held to "
                      "the identical role policy")
    finally:
        f.close()


# ======================================================================================
# P-21 .. P-24  local class constructors
# ======================================================================================

_CLASS_BASE = HEAD + (
    "class Widget:\n"
    "    def __init__(self):\n"
    "        self.v = 1\n"
    "\n"
    "def _root():\n"
    "    return Widget().v\n"
)


@probe
def P_21_local_constructor_with_forbidden_clock_is_red():
    mutant = _CLASS_BASE.replace("        self.v = 1",
                                 "        self.v = datetime.now()")
    f = Fixture({"probe_main.py": _CLASS_BASE}, ROOTS_ONE)
    try:
        f.build_manifest()
        assert f.run()["ok"]
        f.rewrite("probe_main.py", mutant)
        r = f.run()
        assert has(r, "BARE_NOW"), codes(r)
        return True, "forbidden clock in a local __init__ reached from the closure is RED"
    finally:
        f.close()


@probe
def P_22_clean_local_constructor_is_green():
    """Negative control."""
    f = Fixture({"probe_main.py": _CLASS_BASE}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert any("Widget.__init__" in m for m in r["closure_members"]), \
            r["closure_members"]
        assert r["metrics"]["local_classes_reached"] >= 1
        return True, "clean local constructor included in the closure and GREEN"
    finally:
        f.close()


@probe
def P_23_imported_constructor_remains_external():
    src = HEAD + (
        "from telethon import TelegramClient\n"
        "\n"
        "def _root():\n"
        "    return TelegramClient()\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["external_by_code"].get("EXTERNAL_IMPORT", 0) >= 1
        assert r["metrics"]["local_classes_reached"] == 0
        assert r["metrics"]["escalations_total"] == 0
        return True, "imported constructor recorded EXTERNAL_IMPORT, never expanded"
    finally:
        f.close()


@probe
def P_24_custom_local_metaclass_escalates_or_reds():
    src = HEAD + (
        "class Meta(type):\n"
        "    pass\n"
        "\n"
        "class Widget(metaclass=Meta):\n"
        "    def __init__(self):\n"
        "        self.v = 1\n"
        "\n"
        "def _root():\n"
        "    return Widget().v\n"
    )
    f = Fixture({"probe_main.py": src}, ROOTS_ONE)
    try:
        f.build_manifest()
        r = f.run()
        trig = {c for e in r["escalations"] for c in e.get("trigger_codes", [])}
        assert "CUSTOM_LOCAL_METACLASS" in trig or not r["ok"], (trig, codes(r))
        assert r["metrics"]["local_metaclasses_reached"] >= 1, r["metrics"]
        return True, "custom local metaclass recorded and escalated (%s)" % sorted(trig)
    finally:
        f.close()


# ======================================================================================
# P-25 .. P-28  independent whole-scope clock index
# ======================================================================================

@probe
def P_25_clock_source_outside_the_closure_is_red_from_the_index():
    base = HEAD + (
        "def _root():\n"
        "    return 1\n"
        "\n"
        "def _far_away():\n"
        "    return 2\n"
    )
    mutant = base.replace("def _far_away():\n    return 2",
                          "def _far_away():\n    return datetime.now()")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        r0 = f.run()
        assert r0["ok"] and r0["metrics"]["escalations_total"] == 0
        f.rewrite("probe_main.py", mutant)
        r1 = f.run()
        assert r1["metrics"]["escalations_total"] == 0, "closure must stay uninvolved"
        assert not any("_far_away" in m for m in r1["closure_members"])
        assert has(r1, "BARE_NOW"), codes(r1)
        assert r1["metrics"]["outside_closure_clock_touching_total"] >= 1
        return True, ("caught by phase 3 with an empty escalation set and the definition "
                      "outside the closure")
    finally:
        f.close()


@probe
def P_26_new_clock_touching_def_outside_closure_is_unmanifested():
    base = HEAD + "def _root():\n    return 1\n"
    mutant = base + "\n\ndef _new_far_away():\n    return time.monotonic()\n"
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        assert f.run()["ok"]
        f.rewrite("probe_main.py", mutant)
        r = f.run()
        assert has(r, "UNMANIFESTED_CLOCK_TOUCHING_DEF"), codes(r)
        f_ = [x for x in r["findings"] if x["code"] == "UNMANIFESTED_CLOCK_TOUCHING_DEF"]
        assert f_[0]["layer"] == "phase3_index", f_[0]
        return True, "new out-of-closure clock definition is UNMANIFESTED (phase 3 layer)"
    finally:
        f.close()


@probe
def P_27_alias_based_clock_source_is_detected():
    base = HEAD + "def _root():\n    return 1\n"
    mutant = (HEAD + "from datetime import datetime as _q\n"
                     "from zoneinfo import ZoneInfo as _Z\n"
                     "\n"
                     "def _root():\n"
                     "    a = _q.now()\n"
                     "    b = _Z('Europe/Berlin')\n"
                     "    return (a, b)\n")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE)
    try:
        f.build_manifest()
        assert f.run()["ok"]
        f.rewrite("probe_main.py", mutant)
        r = f.run()
        assert has(r, "BARE_NOW"), codes(r)
        assert has(r, "WRONG_ZONE"), codes(r)
        return True, "aliased datetime/ZoneInfo imports are resolved before matching"
    finally:
        f.close()


@probe
def P_28_changed_manifested_clock_body_is_fingerprint_drift():
    base = HEAD + (
        "def _root():\n"
        "    return _stamp()\n"
        "\n"
        "def _stamp():\n"
        "    return datetime.utcnow().isoformat()\n"
    )
    mutant = base.replace("    return datetime.utcnow().isoformat()",
                          "    v = datetime.utcnow()\n    return v.isoformat()")
    f = Fixture({"probe_main.py": base}, ROOTS_ONE,
                role_declarations={"probe_main.py::_stamp": {
                    "role": "UTC_PERSISTENCE", "contract_note": "naive utc stamp"}})
    try:
        f.build_manifest()
        assert f.run()["ok"]
        f.rewrite("probe_main.py", mutant)
        r = f.run()
        assert has(r, "CLOCK_INDEX_FINGERPRINT_DRIFT"), codes(r)
        return True, "a changed manifested clock body is fingerprint drift"
    finally:
        f.close()


# ======================================================================================
# P-29 .. P-35  wrapper-discovery independence (W3.2 F-2 evidence correction, 2026-08-03)
#
# Synthetic MIXED_CLOCK_CONTRACT scope mirroring D7's shape: a wrapper definition
# (`_kyiv_wrap`, KYIV family), a mixed-scan target (`_mixed_target`) with exactly two
# site_contracts (WRAPPER_CALL:KYIV / DATE_ONLY_KYIV and UTCNOW:UTC / UTC_PERSISTENCE --
# same form/site_role pairing as production D7's today_iso_r5 / UTCNOW:UTC sites), and a
# non-registered helper reached from the same body.  Recovered from
# 10_TEST_AND_MUTATION_PLAN.md sec 2 and the independent review's r08/r08b mutation
# legs against production (M-MIXED-1b/2/3/5b, M-WRAP-2A) -- those proved the SAME
# properties against the live manifest; these probes prove them as durable, synthetic,
# repository-owned assets per acceptance 33.
# ======================================================================================

_MIXED_HEAD = HEAD

_MIXED_WRAP_SRC = _MIXED_HEAD + (
    "def _kyiv_wrap():\n"
    "    return datetime.now(ZoneInfo('Europe/Kyiv')).isoformat()\n"
)

_MIXED_MAIN_SRC = _MIXED_HEAD + (
    "def _kyiv_wrap():\n"
    "    return datetime.now(ZoneInfo('Europe/Kyiv')).isoformat()\n"
    "\n"
    "\n"
    "def _ordinary_helper():\n"
    "    return 'ok'\n"
    "\n"
    "\n"
    "def _mixed_target():\n"
    "    a = _kyiv_wrap()\n"
    "    b = datetime.utcnow().isoformat()\n"
    "    d = datetime.now(timezone.utc).isoformat()\n"
    "    c = _ordinary_helper()\n"
    "    return a, b, c, d\n"
    "\n"
    "\n"
    "def _root():\n"
    "    return _mixed_target()\n"
)

_MIXED_MAIN_ALIAS_SRC = _MIXED_HEAD + (
    "from probe_wrap import _kyiv_wrap as _aliased_wrap\n"
    "\n"
    "\n"
    "def _mixed_target():\n"
    "    a = _aliased_wrap()\n"
    "    b = datetime.utcnow().isoformat()\n"
    "    return a, b\n"
    "\n"
    "\n"
    "def _root():\n"
    "    return _mixed_target()\n"
)

_MIXED_MAIN_NESTED_SRC = _MIXED_HEAD + (
    "def _kyiv_wrap():\n"
    "    return datetime.now(ZoneInfo('Europe/Kyiv')).isoformat()\n"
    "\n"
    "\n"
    "def _mixed_target():\n"
    "    a = _kyiv_wrap()\n"
    "    b = datetime.utcnow().isoformat()\n"
    "\n"
    "    def _nested_caller():\n"
    "        return _kyiv_wrap()\n"
    "\n"
    "    _nested_caller()\n"
    "    return a, b\n"
    "\n"
    "\n"
    "def _root():\n"
    "    return _mixed_target()\n"
)

_MIXED_SITE_CONTRACTS = [
    {"form": "WRAPPER_CALL:KYIV", "site_role": "DATE_ONLY_KYIV"},
    {"form": "UTCNOW:UTC", "site_role": "UTC_PERSISTENCE"},
]

# The base fixture (`_mixed_fixture`) carries a THIRD site (ARGED_NOW:UTC / FRESHNESS_UTC)
# so that P-29's single-contract removal still satisfies H6's ">=2 site_contracts spanning
# >=2 distinct site roles" schema rule on the mutant -- the property under test is that the
# REMOVED site stays independently measured, not whether a 2-contract row can shrink to 1
# (which the schema forbids by construction, matching production D7's 2-contract shape;
# see w3_2_manifest_integrity.py H6).
_MIXED_SITE_CONTRACTS_3 = _MIXED_SITE_CONTRACTS + [
    {"form": "ARGED_NOW:UTC", "site_role": "FRESHNESS_UTC"},
]


def _mixed_fixture(files=None, wrappers=None):
    files = files if files is not None else {"probe_main.py": _MIXED_MAIN_SRC}
    wrappers = wrappers if wrappers is not None else ["probe_main.py::_kyiv_wrap"]
    return Fixture(files, ROOTS_ONE, wrappers=wrappers,
                    mixed_targets=[{"file": "probe_main.py",
                                    "qualified_name": "_mixed_target",
                                    "contract_note": "P-29..P-35 synthetic mixed target",
                                    "site_contracts": _MIXED_SITE_CONTRACTS_3}])


def _mixed_row(manifest):
    for r in manifest["clock_index"]:
        if r["file"] == "probe_main.py" and r["qualified_name"] == "_mixed_target":
            return r
    raise AssertionError("_mixed_target row not found")


@probe
def P_29_site_contract_removed_source_unchanged():
    """plan sec 2 probe 1: D7 site contract removed, source unchanged."""
    f = _mixed_fixture()
    try:
        m0 = f.build_manifest()
        r0 = f.run()
        assert r0["ok"], codes(r0)
        assert r0["metrics"]["measured_mixed_site_total"] == 3, r0["metrics"]

        def drop_wrapper_contract(m):
            row = _mixed_row(m)
            row["site_contracts"] = [c for c in row["site_contracts"]
                                     if c["form"] != "WRAPPER_CALL:KYIV"]

        r1 = f.run_mutated_manifest(m0, drop_wrapper_contract)
        assert not r1["ok"]
        assert has(r1, "SITE_CONTRACT_UNMATCHED"), codes(r1)
        assert r1["metrics"]["measured_mixed_site_total"] == 3, \
            "WRAPPER_CALL:KYIV must still be measured after its contract is removed"
        return True, "site contract removed; wrapper site still measured, SITE_CONTRACT_UNMATCHED RED"
    finally:
        f.close()


@probe
def P_30_role_changed_away_from_mixed_correctly_repinned():
    """plan sec 2 probe 2 (M-MIXED-1b shape, R-D10): D7 role changed away from
    MIXED_CLOCK_CONTRACT, source unchanged, manifest correctly re-pinned so it stays
    schema-valid (site_contracts removed, permitted_forms made consistent with the new
    role -- otherwise H6 refuses the manifest before the gate can measure anything, which
    is a plan defect this review already documented, not the property under test)."""
    f = _mixed_fixture()
    try:
        m0 = f.build_manifest()
        assert f.run()["ok"]

        def flip_role(m):
            row = _mixed_row(m)
            row["role"] = "UTC_PERSISTENCE"
            row.pop("site_contracts", None)
            row["permitted_forms"] = ["UTCNOW:UTC"]

        r1 = f.run_mutated_manifest(m0, flip_role)
        assert not r1["ok"]
        assert has(r1, "MIXED_TARGET_ROLE_MISMATCH"), codes(r1)
        assert r1["metrics"]["mixed_scan_target_total"] == 1, r1["metrics"]
        assert r1["metrics"]["measured_mixed_site_total"] == 3, \
            "the mixed-scan target must still be independently rescanned (H5) even " \
            "though its row no longer claims MIXED_CLOCK_CONTRACT"
        return True, "role flipped away from MIXED; scan still ran; MIXED_TARGET_ROLE_MISMATCH RED"
    finally:
        f.close()


@probe
def P_31_clock_index_row_removed_mixed_target_remains():
    """plan sec 2 probe 3: D7 clock-index row removed while its mixed-scan-target
    registration remains."""
    f = _mixed_fixture()
    try:
        m0 = f.build_manifest()
        assert f.run()["ok"]

        def drop_row(m):
            m["clock_index"] = [r for r in m["clock_index"]
                                if not (r["file"] == "probe_main.py"
                                        and r["qualified_name"] == "_mixed_target")]

        r1 = f.run_mutated_manifest(m0, drop_row)
        assert not r1["ok"]
        assert has(r1, "MIXED_TARGET_ROW_MISSING"), codes(r1)
        return True, "clock_index row removed, scan-target entry kept -> MIXED_TARGET_ROW_MISSING"
    finally:
        f.close()


@probe
def P_32_mixed_target_removed_mixed_role_row_remains():
    """plan sec 2 probe 4: D7 removed from mixed_clock_scan_targets while its
    MIXED_CLOCK_CONTRACT clock_index row remains -- must never silently lose per-site
    scanning."""
    f = _mixed_fixture()
    try:
        m0 = f.build_manifest()
        assert f.run()["ok"]

        def drop_target(m):
            m["mixed_clock_scan_targets"] = []

        r1 = f.run_mutated_manifest(m0, drop_target)
        assert not r1["ok"]
        assert has(r1, "MIXED_TARGET_REGISTRY_MISMATCH") or has(r1, "MANIFEST_SCHEMA_FAIL"), \
            codes(r1)
        return True, ("mixed_clock_scan_targets entry removed, MIXED role row kept -> RED "
                      "(%s), never a silent GREEN" % codes(r1))
    finally:
        f.close()


@probe
def P_33_wrapper_call_through_imported_alias_measured_independently():
    """plan sec 2 probe 5 (M-WRAP-2A shape): the mixed target reaches the registered
    wrapper through a `from X import Y as Z` alias in a different scope file -- the site
    is still measured and the contract still matches, GREEN."""
    f = Fixture({"probe_wrap.py": _MIXED_WRAP_SRC, "probe_main.py": _MIXED_MAIN_ALIAS_SRC},
                ROOTS_ONE, wrappers=["probe_wrap.py::_kyiv_wrap"],
                mixed_targets=[{"file": "probe_main.py", "qualified_name": "_mixed_target",
                                "contract_note": "alias-reached wrapper",
                                "site_contracts": _MIXED_SITE_CONTRACTS}])
    try:
        f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["measured_mixed_site_total"] == 2, r["metrics"]
        assert r["metrics"]["contracted_mixed_site_total"] == 2, r["metrics"]
        return True, "wrapper reached through an import alias in another file: measured, GREEN"
    finally:
        f.close()


@probe
def P_34_non_registered_helper_not_misclassified_as_wrapper():
    """plan sec 2 probe 6: an ordinary, non-registered helper called inside the mixed
    target must never be classified as a time wrapper or produce a phantom site."""
    f = _mixed_fixture()
    try:
        m = f.build_manifest()
        wrapper_qnames = {(w["file"], w["qualified_name"]) for w in m["wrapper_registry"]}
        assert ("probe_main.py", "_ordinary_helper") not in wrapper_qnames, \
            "the ordinary helper must never appear in the wrapper registry"
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["measured_mixed_site_total"] == 3, \
            "calling an unregistered helper must not add a phantom measured site"
        row = _mixed_row(m)
        assert not any("_ordinary_helper" in (s.get("expr") or "") and s["form"].startswith("WRAPPER_CALL")
                      for s in row["sites"]), row["sites"]
        return True, "ordinary helper call produced no wrapper/clock site; GREEN, 3 declared sites"
    finally:
        f.close()


@probe
def P_35_wrapper_call_in_nested_definition_owned_only_by_nested():
    """plan sec 2 probe 7: a wrapper call inside a definition NESTED inside the mixed
    target belongs only to the nested definition (H4 -- ``_direct_body_walk`` never
    descends into a nested ``FunctionDef``), never the outer mixed-scan target.

    Ground truth confirmed against ``build_clock_index``/``detect_wrapper_call_sites``
    (w3_2_whole_scope_clock_index.py:679-696): WRAPPER_CALL detection runs ONLY over a
    registered mixed-scan target's own direct body -- an ordinary (non-mixed) definition,
    nested or not, is never wrapper-scanned at all, mirroring how a def that only calls an
    approved wrapper is never flagged UNMANIFESTED_CLOCK_TOUCHING_DEF in production either
    (delegating to an audited wrapper is safe by construction). So the correct, durable
    proof of "owned only by the nested definition" is double-sided: the outer target's
    measured-site count is unaffected by the nested call (H4 exclusion), AND the nested
    definition -- reachable from the frozen root -- produces no RED anywhere (no phantom
    UNMANIFESTED_CLOCK_TOUCHING_DEF, no closure/escalation disturbance) purely because its
    only content is a wrapper delegation, exactly as if it were the top-level wrapper call
    site itself."""
    f = Fixture({"probe_main.py": _MIXED_MAIN_NESTED_SRC}, ROOTS_ONE,
                wrappers=["probe_main.py::_kyiv_wrap"],
                mixed_targets=[{"file": "probe_main.py", "qualified_name": "_mixed_target",
                                "contract_note": "nested wrapper call ownership",
                                "site_contracts": _MIXED_SITE_CONTRACTS}])
    try:
        m = f.build_manifest()
        r = f.run()
        assert r["ok"], codes(r)
        assert r["metrics"]["measured_mixed_site_total"] == 2, \
            "the nested call must not be attributed to the outer mixed-scan target"
        row = _mixed_row(m)
        assert len(row["sites"]) == 2, \
            ("the outer mixed target's OWN declared sites must stay exactly the two "
             "direct-body ones; the nested call must not leak in: %s" % row["sites"])
        nested = [n for n in m["clock_index"]
                  if n["file"] == "probe_main.py"
                  and n["qualified_name"] == "_mixed_target._nested_caller"]
        assert not nested, (
            "a nested def whose ONLY content is delegating to an approved wrapper must "
            "never itself become a general clock_index row (WRAPPER_CALL is scanned only "
            "for the registered mixed target's own direct body, never universally): %s"
            % nested)
        assert r["metrics"].get("unmanifested_clock_touching_total", 0) == 0, r["metrics"]
        return True, ("nested wrapper-only delegation excluded from the outer target's "
                      "sites (H4) and produces no findings anywhere (safe by construction)")
    finally:
        f.close()


# ======================================================================================
# Driver
# ======================================================================================

PROBES = [
    ("P-1", P_1_ui_strings_are_not_candidates, "positive"),
    ("P-2", P_2_json_and_sql_keys_are_not_candidates, "positive"),
    ("P-3", P_3_literal_callable_registry_is_followed, "positive"),
    ("P-4", P_4_literal_globals_get_dispatch_is_followed, "positive"),
    ("P-5", P_5_unresolved_dynamic_dispatch_escalates, "positive"),
    ("P-6", P_6_external_stdlib_and_telethon_calls_stay_external, "positive"),
    ("P-7", P_7_utc_form_green_in_its_declared_role, "positive"),
    ("P-8", P_8_same_utc_form_in_a_kyiv_root_is_red, "positive"),
    ("P-9", P_9_root_generation_drift_is_red, "positive"),
    ("P-10", P_10_missing_manifest_is_red, "positive"),
    ("P-11", P_11_corrupt_manifest_is_red, "positive"),
    ("P-12", P_12_escalation_is_causal, "positive"),
    ("P-13", P_13_branch_reorder_stays_green, "NEGATIVE_CONTROL"),
    ("P-14", P_14_safe_loop_with_continue_and_break_stays_green, "NEGATIVE_CONTROL"),
    ("P-15", P_15_undeclared_clock_touching_def_is_red, "positive"),
    ("P-16", P_16_unknown_receiver_attribute_escalates, "positive"),
    ("P-17", P_17_attribute_alias_cannot_bypass, "positive"),
    ("P-18", P_18_imported_method_remains_external, "positive"),
    ("P-19", P_19_proven_builtin_method_remains_external, "NEGATIVE_CONTROL"),
    ("P-20", P_20_escalation_cannot_weaken_policy, "positive"),
    ("P-21", P_21_local_constructor_with_forbidden_clock_is_red, "positive"),
    ("P-22", P_22_clean_local_constructor_is_green, "NEGATIVE_CONTROL"),
    ("P-23", P_23_imported_constructor_remains_external, "positive"),
    ("P-24", P_24_custom_local_metaclass_escalates_or_reds, "positive"),
    ("P-25", P_25_clock_source_outside_the_closure_is_red_from_the_index, "index"),
    ("P-26", P_26_new_clock_touching_def_outside_closure_is_unmanifested, "index"),
    ("P-27", P_27_alias_based_clock_source_is_detected, "index"),
    ("P-28", P_28_changed_manifested_clock_body_is_fingerprint_drift, "index"),
    ("P-29", P_29_site_contract_removed_source_unchanged, "mixed_wrapper_discovery"),
    ("P-30", P_30_role_changed_away_from_mixed_correctly_repinned, "mixed_wrapper_discovery"),
    ("P-31", P_31_clock_index_row_removed_mixed_target_remains, "mixed_wrapper_discovery"),
    ("P-32", P_32_mixed_target_removed_mixed_role_row_remains, "mixed_wrapper_discovery"),
    ("P-33", P_33_wrapper_call_through_imported_alias_measured_independently, "mixed_wrapper_discovery"),
    ("P-34", P_34_non_registered_helper_not_misclassified_as_wrapper, "mixed_wrapper_discovery"),
    ("P-35", P_35_wrapper_call_in_nested_definition_owned_only_by_nested, "mixed_wrapper_discovery"),
]


def main(argv=None) -> int:
    out_json = None
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--json" in argv:
        out_json = argv[argv.index("--json") + 1]

    results = []
    passed = 0
    print("=" * 86)
    print("W3.2 DESIGN C+ TIMEZONE SOURCE GATE -- PROBES P-1 .. P-28")
    print("=" * 86)
    for pid, fn, kind in PROBES:
        try:
            ok, detail = fn()
        except AssertionError as exc:
            ok, detail = False, "assertion: %s" % exc
        except Exception as exc:                                   # noqa: BLE001
            ok, detail = False, "%s: %s" % (type(exc).__name__, exc)
        passed += 1 if ok else 0
        results.append({"id": pid, "kind": kind, "pass": bool(ok),
                        "detail": detail, "name": fn.__name__})
        print("  [%s] %-6s %-52s %s"
              % ("OK" if ok else "FAIL", pid, fn.__name__[:52], detail[:70]))

    total = len(PROBES)
    print("-" * 86)
    print("probes: %d/%d passed  (negative controls: %s)"
          % (passed, total,
             ", ".join(p for p, _, k in PROBES if k == "NEGATIVE_CONTROL")))
    payload = {"total": total, "passed": passed,
               "negative_controls": [p for p, _, k in PROBES if k == "NEGATIVE_CONTROL"],
               "results": results,
               "gate_tool_sha_note": "probes run the real gate via run_gate()"}
    if out_json:
        Path(out_json).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    if passed == total:
        print("[OK] all probes passed")
        return 0
    print("[FAIL] %d probe(s) failed" % (total - passed))
    return 1


if __name__ == "__main__":
    sys.exit(main())
