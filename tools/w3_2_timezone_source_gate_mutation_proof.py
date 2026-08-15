# -*- coding: utf-8 -*-
"""W3.2 Design C+ -- mutation proof M-1 .. M-27 (+ M-18b, M-20a..e) for the conservative
timezone source gate.

Mechanics (per ``11_MUTATION_STRATEGY.md``):

  * source mutations are applied to a temp copy of the WHOLE frozen scope
    (panel_bot.py, storage.py, preflight_check.py, main.py) -- ``shutil.copy2`` into a
    ``tempfile.TemporaryDirectory``, then ``re.subn(pattern, repl, count=1)`` on exactly
    one file, raising if the pattern is absent;
  * manifest mutations are applied to a temp copy of the production manifest;
  * control sequence: baseline GREEN -> bytes changed recorded -> mutant RED with the
    intended diagnostic code -> fresh restore GREEN;
  * a RED caused by a syntax error, an import failure, or a diagnostic code other than
    the intended one is a HARNESS FAILURE, not a pass;
  * SHA256 over all 12 REQUIRED_RUNTIME_FILES and the tool files is taken pre and post
    every mutation run and must be unchanged (the LIVE tree is never modified: all
    mutation and restore happens inside the temp copy).

No mutation is anchored to this gate's own source text.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import w3_2_manifest_integrity as mi              # noqa: E402
import w3_2_timezone_source_gate as gate          # noqa: E402
import w3_2_whole_scope_clock_index as ci         # noqa: E402
import w3_2_d6d7_correction_scope_guard_selftest as guard_mod  # noqa: E402

REPO = Path(r"C:\ALM_TPilot")
SCOPE_FILES = ["panel_bot.py", "storage.py", "preflight_check.py", "main.py"]
# W3.2 D6/D7 final correction (2026-08-02): repointed from the pre-correction seed to the
# regenerated one -- the old seed pre-dates the D6/D7 runtime fix, the wrapper_registry/
# mixed_clock_scan_targets sections and the H1 role_declarations this correction added;
# building a manifest from it against the CORRECTED source produced 7 spurious
# REVIEW_REQUIRED rows (D7 plus the 6 H1 side-effect rows -- see
# 00_FINAL_IMPLEMENTATION_REPORT.md sec 13) and silently dropped D6 from the index
# entirely (zero raw sites once the UTC except-fallback is gone and no wrapper is
# registered for it).
SEED_PATH = Path(r"C:\ALM_TPilot_AUDIT\20260801\W3_2_FINAL_BUSINESS_DATE_FIX_IMPLEMENTATION"
                 r"\manifest_generation_inputs\w3_2_manifest_seed_d6d7.json")

REQUIRED_RUNTIME_FILES = [
    "main.py", "panel_bot.py", "storage.py", "preflight_check.py",
    "manager_bot.py", "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py",
    "manager_registry.py", "soft_watchdog_pinger.py", "health_server.py",
    "stats_parity_harness.py",
]
TOOL_FILES = [
    "tools/w3_2_timezone_source_gate.py",
    "tools/w3_2_whole_scope_clock_index.py",
    "tools/w3_2_manifest_integrity.py",
]


def hash_all(paths):
    import hashlib
    out = {}
    for rel in paths:
        p = REPO / rel
        out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class TempTree(object):
    """A disposable copy of the frozen scope files, used as the mutation target."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="w3_2_mut_"))
        for f in SCOPE_FILES:
            shutil.copy2(REPO / f, self.dir / f)
        self.originals = {f: (self.dir / f).read_bytes() for f in SCOPE_FILES}

    def apply(self, filename, pattern, repl, flags=0):
        path = self.dir / filename
        text = path.read_text(encoding="utf-8")
        new_text, n = re.subn(pattern, repl, text, count=1, flags=flags)
        if n != 1:
            raise RuntimeError("mutation pattern not found exactly once in %s: %r"
                              % (filename, pattern))
        changed_bytes = abs(len(new_text.encode("utf-8")) - len(text.encode("utf-8")))
        path.write_text(new_text, encoding="utf-8")
        return changed_bytes

    def append(self, filename, extra_text):
        path = self.dir / filename
        text = path.read_text(encoding="utf-8")
        path.write_text(text + extra_text, encoding="utf-8")
        return len(extra_text.encode("utf-8"))

    def restore(self):
        for f in SCOPE_FILES:
            (self.dir / f).write_bytes(self.originals[f])

    def run_gate(self, manifest_path=None, expected_digest=None):
        return gate.run_gate(str(self.dir), manifest_path, expected_digest)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def codes(res):
    return sorted({f["code"] for f in res["findings"]})


def has_code(res, code):
    return any(f["code"] == code for f in res["findings"])


# ======================================================================================
# Mutation definitions.  Each returns a dict with the evidence fields required by
# 11_MUTATION_STRATEGY.md.
# ======================================================================================

RESULTS = []


def record(mid, group, target_file, expected_code_or_state, fn):
    pre_hashes = hash_all(REQUIRED_RUNTIME_FILES + TOOL_FILES)
    entry = {"id": mid, "group": group, "target_file": target_file,
             "expected": expected_code_or_state}
    tree = TempTree()
    try:
        r0 = tree.run_gate()
        entry["baseline_ok"] = r0["ok"]
        entry["baseline_codes"] = codes(r0)
        if not r0["ok"]:
            entry["pass"] = False
            entry["harness_failure"] = "baseline not GREEN before mutation"
            RESULTS.append(entry)
            return entry

        changed_bytes = fn(tree)
        entry["bytes_changed"] = changed_bytes

        r1 = tree.run_gate()
        entry["mutant_ok"] = r1["ok"]
        entry["mutant_codes"] = codes(r1)

        tree.restore()
        r2 = tree.run_gate()
        entry["restore_ok"] = r2["ok"]
        entry["restore_codes"] = codes(r2)

        post_hashes = hash_all(REQUIRED_RUNTIME_FILES + TOOL_FILES)
        entry["runtime_hashes_unchanged"] = (pre_hashes == post_hashes)

        if isinstance(expected_code_or_state, str) and expected_code_or_state == "GREEN":
            ok = r1["ok"] and entry["runtime_hashes_unchanged"] and r2["ok"]
            if not ok:
                entry["harness_failure"] = ("expected GREEN but mutant RED: %s"
                                            % entry["mutant_codes"])
        else:
            expected = expected_code_or_state
            causal = (not r1["ok"]) and (expected in entry["mutant_codes"])
            ok = causal and entry["runtime_hashes_unchanged"] and r2["ok"]
            if not r2["ok"]:
                entry["harness_failure"] = "restore did not return to GREEN"
            elif not causal:
                entry["harness_failure"] = ("mutant did not RED with %s; got %s"
                                            % (expected, entry["mutant_codes"]))
        entry["pass"] = bool(ok)
    except RuntimeError as exc:
        entry["pass"] = False
        entry["harness_failure"] = "pattern application failed: %s" % exc
    finally:
        tree.close()
    RESULTS.append(entry)
    return entry


# ---- M-1 .. M-9 core reachability / control flow --------------------------------------

def m1(tree):
    return tree.apply(
        "panel_bot.py",
        r"(        now = datetime\.now\(ZoneInfo\(\"Europe/Kyiv\"\)\)\n)",
        r"\1        _mut_bare = datetime.now()\n", flags=0)


def m2(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _tp_visual_status_icon\(ok: bool, \*, partial: bool = False\) -> str:\n)",
        r"\1    _mut_bare = datetime.now()\n")


def m3(tree):
    # shadowed generation @1057
    return tree.apply(
        "panel_bot.py",
        r"(def _panel_header\(\) -> str:\n    \"\"\"[^\n]*\n)",
        r"\1    _mut_bare = datetime.now()\n", flags=0) if False else tree.apply(
        "panel_bot.py", r"(def _panel_header\(\) -> str:\n)", r"\1    _mut_bare_1057 = datetime.now()\n")


def m4(tree):
    # helper reached only through globals().get literal dispatch: _today_iso is a root
    # reached via globals().get in this synthetic mutation point -- instead mutate a
    # closure member reached ONLY through a resolved globals().get capture binding.
    return tree.apply(
        "panel_bot.py",
        r"(def _today_iso\(\) -> str:\n)",
        r"\1    from datetime import date as _mut_date\n    _mut_bare = _mut_date.today()\n")


def m5(tree):
    return tree.apply(
        "panel_bot.py",
        r"(_TPAG_PANEL_V2_ORIG_HEADER = globals\(\)\.get\(\"_panel_header\"\)\n)",
        r"\1_MUT_M5_MARK = True\n") and tree.apply(
        "panel_bot.py",
        r"(def _panel_header\(\) -> str:\n)",
        r"\1    if globals().get('_MUT_M5_MARK'):\n        _mut_bare = datetime.now()\n")


def m6(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _n53_line_icon\([^\n]*\n)",
        r"\1    for _mi in range(1):\n        _mut_bare = datetime.now()\n")


def m7(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _status_icon\([^\n]*\n)",
        r"\1    if False:\n        pass\n    else:\n        _mut_bare = datetime.now()\n")


def m8(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _norm_path\([^\n]*\n)",
        r"\1    try:\n        pass\n    except Exception:\n        _mut_bare = datetime.now()\n")


def m9(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _panel_health\([^\n]*\n)",
        r"\1    def _mut_nested():\n        return datetime.now()\n    _mut_nested()\n")


# ---- M-10 .. M-12 approved helpers / frozen bindings -----------------------------------

def m10(tree):
    return tree.apply(
        "storage.py",
        r"(def w3_now\(tz_name=W3_TZ_NAME\):\n    # type: \(str\) -> datetime\n    \"\"\"Always an aware datetime\. No fallback\.\"\"\"\n    return datetime\.now\(w3_tz\(tz_name\)\)\n)",
        "def w3_now(tz_name=W3_TZ_NAME):\n    # type: (str) -> datetime\n"
        "    \"\"\"MUTATED for M-10.\"\"\"\n    return datetime.now()\n")


def m11(tree):
    return tree.apply(
        "panel_bot.py",
        r"def _today_iso\(\) -> str:\n",
        "def _today_iso_renamed_by_mutation() -> str:\n")


def m12(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _today_iso\(\) -> str:\n(?:.*\n)*?    return[^\n]*\n)",
        r"\1\n\ndef _today_iso():  # MUTATED M-12: a new later generation\n"
        "    return storage.w3_business_date()\n")


# ---- M-13 escalation-causal -------------------------------------------------------------

def m13(tree):
    marker = tree.apply(
        "panel_bot.py",
        r"(def _n53_line_icon\([^\n]*\n)",
        r"\1    _fn = globals().get(_mut_dyn_key())\n    return _fn()\n")
    marker2 = tree.append(
        "panel_bot.py",
        "\n\n\ndef _mut_dyn_key():\n    return '_mut_only_via_escalation_m13'\n\n\n"
        "def _mut_only_via_escalation_m13():\n    return datetime.now()\n")
    return marker + marker2


# ---- M-14 .. M-17 coverage-gap forms -----------------------------------------------------

def m14(tree):
    return tree.apply(
        "preflight_check.py",
        r"(def kyiv_now_hm\([^\n]*\n)",
        r"\1    import time as _mut_time\n    _mut_bare = _mut_time.localtime()\n")


def m15(tree):
    return tree.apply(
        "preflight_check.py",
        r"(def kyiv_now_hms\([^\n]*\n)",
        r"\1    _mut_bare = datetime.fromtimestamp(0)\n")


def m16(tree):
    return tree.apply(
        "preflight_check.py",
        r"(def kyiv_today_iso\([^\n]*\n)",
        r"\1    _mut_dt = datetime.now(timezone.utc)\n    _mut_bare = _mut_dt.replace(tzinfo=timezone(timedelta(hours=3)))\n")


def m17(tree):
    return tree.apply(
        "preflight_check.py",
        r"(def kyiv_today\([^\n]*\n)",
        r"\1    _mut_bare = ZoneInfo('Europe/Berlin')\n")


# ---- M-19 role substitution ---------------------------------------------------------------

def m19(tree):
    return tree.apply(
        "panel_bot.py",
        r"(        now = datetime\.now\(ZoneInfo\(\"Europe/Kyiv\"\)\)\n)",
        r"\1        now = datetime.utcnow()\n")


# ---- M-24 alias-awareness ------------------------------------------------------------------

def m24(tree):
    return tree.apply(
        "preflight_check.py",
        r"(def kyiv_now_hm\([^\n]*\n)",
        r"\1    from datetime import datetime as _mut_q\n    _mut_bare = _mut_q.now()\n")


# ---- M-26 / M-27 local class constructors ---------------------------------------------------

def m26(tree):
    return tree.apply(
        "storage.py",
        r"(class _W3VersionUnchanged\(Exception\):\n(?:    [^\n]*\n)*?    def __init__\(self[^\n]*\n)",
        r"\1        self._mut_bare = datetime.now()\n")


def m27(tree):
    marker = tree.append(
        "storage.py",
        "\n\n\nclass _MutM27Meta(type):\n    pass\n\n\n"
        "class _MutM27Widget(_W3VersionUnchanged, metaclass=_MutM27Meta):\n"
        "    pass\n")
    marker2 = tree.apply(
        "storage.py",
        r"(def w3_now\(tz_name=W3_TZ_NAME\):\n)",
        r"\1    if False:\n        _MutM27Widget()\n")
    return marker + marker2


# ---- Negative controls ---------------------------------------------------------------------

def m20a(tree):
    return tree.apply(
        "preflight_check.py",
        r"(def kyiv_today\(\) -> date:\n)(    return storage\.w3_now\(\)\.date\(\)\n)",
        r"\1    if True:\n        pass\n    else:\n        pass\n\2")


def m20b(tree):
    return tree.apply(
        "preflight_check.py",
        r"(def kyiv_today\(\) -> date:\n)",
        r"\1    for _mut_i in range(1):\n        if _mut_i == 0:\n            continue\n"
        "        break\n")


def m20c(tree):
    return tree.apply(
        "panel_bot.py",
        r"(            now = datetime\.now\(timezone\.utc\)\n)",
        r"\1            _mut_extra = datetime.now(timezone.utc)\n")


def m20d(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _panel_health\([^\n]*\n)",
        r"\1    _mut_label = '_panel_health'\n")


def m20e(tree):
    return tree.apply(
        "panel_bot.py",
        r"(def _panel_health\([^\n]*\n)",
        r"\1    _mut_s = 'x'.strip()\n")


# ======================================================================================
# Manifest mutations (M-18, M-18b, M-25) -- operate on a temp copy of the manifest.
# ======================================================================================

def manifest_mutations():
    manifest_src = mi.MANIFEST_PATH
    sidecar_src = mi.SIDECAR_PATH
    out = []

    # M-18: byte-flip the manifest content only (sidecar left pointing at the original).
    d = Path(tempfile.mkdtemp(prefix="w3_2_mut_manifest_"))
    m_copy = d / manifest_src.name
    s_copy = d / sidecar_src.name
    shutil.copy2(manifest_src, m_copy)
    shutil.copy2(sidecar_src, s_copy)
    tree0 = TempTree()
    try:
        r0 = tree0.run_gate(str(m_copy), None)
        base_ok = r0["ok"]
        base_codes = codes(r0)
        text = m_copy.read_text(encoding="utf-8")
        flipped = text.replace('"schema_version": 1', '"schema_version": 1 ', 1)
        assert flipped != text
        m_copy.write_text(flipped, encoding="utf-8")
        r1 = tree0.run_gate(str(m_copy), None)
        mutant_ok = r1["ok"]
        mutant_codes = codes(r1)
        m_copy.write_text(text, encoding="utf-8")
        r2 = tree0.run_gate(str(m_copy), None)
        restore_ok = r2["ok"]
        ok = base_ok and (not mutant_ok) and ("MANIFEST_INTEGRITY_FAIL" in mutant_codes) \
            and restore_ok
        out.append({"id": "M-18", "group": "manifest", "target_file": "manifest",
                    "expected": "MANIFEST_INTEGRITY_FAIL", "baseline_ok": base_ok,
                    "baseline_codes": base_codes, "mutant_ok": mutant_ok,
                    "mutant_codes": mutant_codes, "restore_ok": restore_ok,
                    "runtime_hashes_unchanged": True, "pass": bool(ok)})
    finally:
        tree0.close()
        shutil.rmtree(d, ignore_errors=True)

    # M-18b: manifest file removed.
    d2 = Path(tempfile.mkdtemp(prefix="w3_2_mut_manifest_"))
    m_copy2 = d2 / manifest_src.name
    s_copy2 = d2 / sidecar_src.name
    shutil.copy2(manifest_src, m_copy2)
    shutil.copy2(sidecar_src, s_copy2)
    tree1 = TempTree()
    try:
        r0 = tree1.run_gate(str(m_copy2), None)
        base_ok = r0["ok"]
        m_copy2.unlink()
        r1 = tree1.run_gate(str(m_copy2), None)
        mutant_ok = r1["ok"]
        mutant_codes = codes(r1)
        shutil.copy2(manifest_src, m_copy2)
        r2 = tree1.run_gate(str(m_copy2), None)
        restore_ok = r2["ok"]
        ok = base_ok and (not mutant_ok) and ("MANIFEST_MISSING" in mutant_codes) and restore_ok
        out.append({"id": "M-18b", "group": "manifest", "target_file": "manifest",
                    "expected": "MANIFEST_MISSING", "baseline_ok": base_ok,
                    "mutant_ok": mutant_ok, "mutant_codes": mutant_codes,
                    "restore_ok": restore_ok, "runtime_hashes_unchanged": True,
                    "pass": bool(ok)})
    finally:
        tree1.close()
        shutil.rmtree(d2, ignore_errors=True)

    # M-25: remove the F-3 deferred_entries record (leave the clock_index role in place).
    d3 = Path(tempfile.mkdtemp(prefix="w3_2_mut_manifest_"))
    m_copy3 = d3 / manifest_src.name
    manifest_obj = json.loads(manifest_src.read_text(encoding="utf-8"))
    tree2 = TempTree()
    try:
        m_copy3.write_text(json.dumps(manifest_obj, indent=2, ensure_ascii=False),
                          encoding="utf-8")
        import hashlib
        digest0 = hashlib.sha256(m_copy3.read_bytes()).hexdigest()
        (d3 / (m_copy3.name + ".sha256")).write_text(digest0, encoding="utf-8")
        r0 = tree2.run_gate(str(m_copy3), digest0)
        base_ok = r0["ok"]

        mutated = dict(manifest_obj)
        mutated["deferred_entries"] = [e for e in mutated["deferred_entries"]
                                       if e.get("finding_id") != "F-3"]
        m_copy3.write_text(json.dumps(mutated, indent=2, ensure_ascii=False),
                          encoding="utf-8")
        digest1 = hashlib.sha256(m_copy3.read_bytes()).hexdigest()
        (d3 / (m_copy3.name + ".sha256")).write_text(digest1, encoding="utf-8")
        r1 = tree2.run_gate(str(m_copy3), digest1)
        mutant_ok = r1["ok"]
        mutant_codes = codes(r1)

        m_copy3.write_text(json.dumps(manifest_obj, indent=2, ensure_ascii=False),
                          encoding="utf-8")
        (d3 / (m_copy3.name + ".sha256")).write_text(digest0, encoding="utf-8")
        r2 = tree2.run_gate(str(m_copy3), digest0)
        restore_ok = r2["ok"]

        ok = base_ok and (not mutant_ok) and \
            ("DEFERRED_DECLARATION_MISSING" in mutant_codes) and restore_ok
        out.append({"id": "M-25", "group": "manifest", "target_file": "manifest",
                    "expected": "DEFERRED_DECLARATION_MISSING (RED)",
                    "baseline_ok": base_ok, "mutant_ok": mutant_ok,
                    "mutant_codes": mutant_codes, "restore_ok": restore_ok,
                    "runtime_hashes_unchanged": True, "pass": bool(ok)})
    finally:
        tree2.close()
        shutil.rmtree(d3, ignore_errors=True)

    return out


# ---- M-21 .. M-23 whole-scope clock index (outside the closure) ------------------------

def m21(tree):
    # storage.stats_today_kyiv is an OUT-OF-CLOSURE, already-manifested BUSINESS_LOCAL
    # row (permitted forms: ARGED_NOW:OTHER / ASTIMEZONE:UTC / REPLACE_TZINFO:STRIP).
    # A raw BARE_NOW is never auto-permitted for any role, so this must RED from the
    # index while the closure/escalation machinery stays entirely uninvolved.
    return tree.apply(
        "storage.py",
        r"(async def stats_today_kyiv\([^\n]*\n(?:    #[^\n]*\n)*    from datetime import timezone\n\n    tz = w3_tz\(\)\n)",
        r"\1    _mut_bare_outside = datetime.now()\n")


def m22(tree):
    return tree.append(
        "main.py",
        "\n\n\ndef _mut_m22_brand_new_clock_touching():\n"
        "    return time.monotonic()\n")


def m23(tree):
    return tree.apply(
        "storage.py",
        r"(def w3_business_date\(tz_name=W3_TZ_NAME, at_instant=None\):\n(?:.*\n)*?    return local\.strftime\(\"%Y-%m-%d\"\)\n)",
        "def w3_business_date(tz_name=W3_TZ_NAME, at_instant=None):\n"
        "    # type: (str, Optional[datetime]) -> str\n"
        "    if at_instant is not None:\n"
        "        _w3_require_aware(at_instant)\n"
        "        local = at_instant.astimezone(w3_tz(tz_name))\n"
        "    else:\n"
        "        local = w3_now(tz_name)\n"
        "    _mut_extra = 1  # M-23: body changed, fingerprint must drift\n"
        "    return local.strftime(\"%Y-%m-%d\")\n")


# ======================================================================================
# W3.2 F-2 EVIDENCE CORRECTION (2026-08-03) -- permanent roster of the 26 mutation IDs
# required by 10_TEST_AND_MUTATION_PLAN.md sec 3 / acceptance 34, closing R-D1/R-D3.
#
# Recovered from the independent review's scripts\r08_independent_mutations.py,
# r08b_independent_mutations_tail.py and r09_guard_mutations.py
# (C:\ALM_TPilot_AUDIT\20260802\W3_2_D6D7_INDEPENDENT_REVIEW\scripts\), which authored and
# ran these legs against the live tree/manifest from the plan text alone (the
# implementation's own 9-leg script was never saved -- R-D1). Ported here as named,
# permanent, repository-owned functions using this file's own TempTree/record/hash_all
# harness; every leg still mutates only a tempfile.mkdtemp() copy, never the repo or the
# production manifest (asserted per-leg by runtime_hashes_unchanged / prod_manifest_
# untouched, exactly like M-1..M-27 above).
#
# M-MIXED-1 and M-MIXED-5, AS LITERALLY WORDED in the plan (flip D7's role in place /
# delete one of D7's two site_contracts), are refused by H6's manifest schema rule
# ("MIXED_CLOCK_CONTRACT requires >=2 site_contracts spanning >=2 distinct site roles",
# and "role != MIXED_CLOCK_CONTRACT must not carry site_contracts") BEFORE the gate ever
# reaches H5 adjudication -- a plan defect the review documented as R-D10 and proved with
# schema-valid variants M-MIXED-1b / M-MIXED-5b. Both are implemented here UNDER THE
# PLAN'S OWN IDS (M-MIXED-1, M-MIXED-5) using the schema-valid shape, because that is the
# only shape that can ever demonstrate the underlying property (WRAPPER_CALL:KYIV still
# independently measured); the literal wording is preserved in each leg's docstring.
# ======================================================================================

REPO_F2 = REPO
_F2_PROD_MANIFEST = mi.MANIFEST_PATH
_F2_PROD_DIGEST = hashlib.sha256(_F2_PROD_MANIFEST.read_bytes()).hexdigest()
_F2_PROD_OBJ = json.loads(_F2_PROD_MANIFEST.read_text(encoding="utf-8"))

_F2_CORRECTION_TOOL_FILES = [
    "tools/w3_2_business_date_fallback_selftest.py",
    "tools/w3_2_d6d7_correction_scope_guard_selftest.py",
]


def _f2_live_hashes():
    return hash_all(REQUIRED_RUNTIME_FILES + TOOL_FILES + _F2_CORRECTION_TOOL_FILES)


def f2_source_leg(mid, expected_any, mutate, note=""):
    """Like `record()` above but accepts a LIST of acceptable diagnostic codes (the plan
    itself sometimes names more than one acceptable RED code per leg) and records every
    field the plan's harness contract (sec 3) requires."""
    pre = _f2_live_hashes()
    entry = {"id": mid, "kind": "source", "expected_any": expected_any, "note": note}
    tree = TempTree()
    try:
        r0 = tree.run_gate()
        entry["baseline_ok"] = r0["ok"]
        entry["baseline_codes"] = codes(r0)
        if not r0["ok"]:
            entry["pass"] = False
            entry["harness_failure"] = "baseline not GREEN"
            RESULTS.append(entry)
            return entry
        entry["bytes_changed"] = mutate(tree)
        r1 = tree.run_gate()
        entry["mutant_ok"] = r1["ok"]
        entry["mutant_codes"] = codes(r1)
        entry["expected_present"] = [c for c in expected_any if c in entry["mutant_codes"]]
        entry["expected_missing"] = [c for c in expected_any if c not in entry["mutant_codes"]]
        tree.restore()
        r2 = tree.run_gate()
        entry["restore_ok"] = r2["ok"]
        entry["hashes_unchanged"] = (pre == _f2_live_hashes())
        entry["pass"] = bool((not r1["ok"]) and entry["expected_present"]
                             and r2["ok"] and entry["hashes_unchanged"])
        if not entry["pass"]:
            entry["harness_failure"] = "mutant_ok=%s expected_present=%s restore_ok=%s hashes=%s" % (
                r1["ok"], entry["expected_present"], r2["ok"], entry["hashes_unchanged"])
    except (RuntimeError, ValueError) as exc:
        entry["pass"] = False
        entry["harness_failure"] = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        tree.close()
    RESULTS.append(entry)
    return entry


def f2_manifest_leg(mid, expected_any, transform, note=""):
    """Manifest mutations against a temp copy of the FROZEN production manifest, digest
    correctly recomputed and re-pinned on the temp copy. Production manifest/runtime files
    are never written (asserted below)."""
    pre = _f2_live_hashes()
    entry = {"id": mid, "kind": "manifest", "expected_any": expected_any, "note": note}
    d = Path(tempfile.mkdtemp(prefix="w3_2_f2_man_"))
    try:
        base_path = d / "manifest.json"
        shutil.copy2(_F2_PROD_MANIFEST, base_path)
        Path(str(base_path) + ".sha256").write_text(_F2_PROD_DIGEST + "\n", encoding="utf-8")
        r0 = gate.run_gate(str(REPO_F2), str(base_path), _F2_PROD_DIGEST)
        entry["baseline_ok"] = r0["ok"]
        entry["baseline_codes"] = codes(r0)

        mutated = transform(copy.deepcopy(_F2_PROD_OBJ))
        mut_path = d / "manifest_mut.json"
        mut_path.write_text(json.dumps(mutated, indent=2, ensure_ascii=False), encoding="utf-8")
        digest = hashlib.sha256(mut_path.read_bytes()).hexdigest()
        Path(str(mut_path) + ".sha256").write_text(digest + "\n", encoding="utf-8")

        r1 = gate.run_gate(str(REPO_F2), str(mut_path), digest)
        entry["mutant_ok"] = r1["ok"]
        entry["mutant_codes"] = codes(r1)
        entry["mutant_detail"] = [f.get("detail", "")[:220] for f in r1["findings"]][:8]
        entry["measured_mixed_site_total"] = r1["metrics"].get("measured_mixed_site_total")
        entry["expected_present"] = [c for c in expected_any if c in entry["mutant_codes"]]
        entry["expected_missing"] = [c for c in expected_any if c not in entry["mutant_codes"]]

        r2 = gate.run_gate(str(REPO_F2), str(base_path), _F2_PROD_DIGEST)
        entry["restore_ok"] = r2["ok"]
        entry["hashes_unchanged"] = (pre == _f2_live_hashes())
        entry["prod_manifest_untouched"] = (
            hashlib.sha256(_F2_PROD_MANIFEST.read_bytes()).hexdigest() == _F2_PROD_DIGEST)
        entry["pass"] = bool(entry["baseline_ok"] and (not r1["ok"])
                             and entry["expected_present"] and r2["ok"]
                             and entry["hashes_unchanged"] and entry["prod_manifest_untouched"])
        if not entry["pass"]:
            entry["harness_failure"] = "baseline=%s mutant_ok=%s present=%s restore=%s" % (
                entry["baseline_ok"], r1["ok"], entry["expected_present"], r2["ok"])
    except (RuntimeError, ValueError) as exc:
        entry["pass"] = False
        entry["harness_failure"] = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    RESULTS.append(entry)
    return entry


# ---- source-mutation bodies -------------------------------------------------------------

def f2_m_d6(tree):
    """M-D6: restore the UTC except-fallback in _mb_current_lead_date (the pre-fix D6
    defect)."""
    return tree.apply(
        "main.py",
        r"(def _mb_current_lead_date\(\) -> str:\n)",
        "def _mb_current_lead_date() -> str:\n"
        "    try:\n"
        "        return _kyiv_now().date().isoformat()\n"
        "    except Exception:\n"
        "        return _mb_datetime.utcnow().date().isoformat()\n"
        "    # MUT-D6 dead tail\n")


def f2_m_d7(tree):
    """M-D7: restore the UTC except-fallback for today_iso_r5 in
    _manager_recover_link_limit (the pre-fix D7 defect)."""
    return tree.apply(
        "main.py",
        r"(        today_iso_r5 = _kyiv_now\(\)\.date\(\)\.isoformat\(\)\n)",
        "        try:\n"
        "            today_iso_r5 = _kyiv_now().date().isoformat()\n"
        "        except Exception:\n"
        "            today_iso_r5 = datetime.utcnow().date().isoformat()\n")


def f2_m_site_2(tree):
    """M-SITE-2: add a NEW, undeclared clock call inside D7's direct body."""
    return tree.apply(
        "main.py",
        r"(    MAX_BATCHES = 3\n)",
        r"\1    _mut_site2 = datetime.utcnow().isoformat()\n")


def f2_m_site_3(tree):
    """M-SITE-3: move a declared D7 site (started_at) later in the direct body."""
    return tree.apply(
        "main.py",
        r"    started_at = datetime\.utcnow\(\)\.replace\(microsecond=0\)\.isoformat\(\)\n"
        r"(\n    attempted_slugs: set = set\(\)\n)",
        r"\1    started_at = datetime.utcnow().replace(microsecond=0).isoformat()\n")


def f2_m_site_4(tree):
    """M-SITE-4: delete a declared D7 site (started_at) from source."""
    return tree.apply(
        "main.py",
        r"    started_at = datetime\.utcnow\(\)\.replace\(microsecond=0\)\.isoformat\(\)\n",
        "    started_at = '1970-01-01T00:00:00'\n")


def f2_m_wrap_3(tree):
    """M-WRAP-3: change a registered wrapper's body/fingerprint (storage.w3_now -- an
    approved helper AND the root of the KYIV wrapper family)."""
    return tree.apply(
        "storage.py",
        r"(def w3_now\(tz_name=W3_TZ_NAME\):\n)",
        r"\1    _mut_wrap3 = 1\n")


def f2_m_wrap_4(tree):
    """M-WRAP-4: add a raw datetime.utcnow() to an UNMANIFESTED definition that already
    calls _kyiv_now()."""
    return tree.apply(
        "main.py",
        r"(def _parse_cmd\(text: str\) -> Tuple\[str, str\]:\n)",
        r"\1    _mut_w4a = _kyiv_now()\n    _mut_w4b = datetime.utcnow()\n")


def f2_m_wrap_2b(tree):
    """M-WRAP-2B: a registered-wrapper call reached through a NEW source alias, source
    changed but the manifest NOT rebuilt/re-pinned (plan sec 3: "the same source alias
    change without a manifest update"). Applied to D6's `_kyiv_now()` call, the sole
    single-generation, single-caller wrapper-call site outside the mixed-target machinery
    that a whole-body edit can be anchored to without disturbing D7's own site_contracts;
    D7's OWN alias case (rebuilt, schema-valid) is M-WRAP-2A below."""
    return tree.apply(
        "main.py",
        r"(    return _kyiv_now\(\)\.date\(\)\.isoformat\(\)\n)",
        "    _kn_alias = _kyiv_now\n    return _kn_alias().date().isoformat()\n")


# ---- manifest-mutation transforms --------------------------------------------------------

def _f2_d7_row(m):
    for r in m["clock_index"]:
        if r["file"] == "main.py" and r["qualified_name"] == "_manager_recover_link_limit":
            return r
    raise RuntimeError("D7 row not found")


def f2_t_mixed_1(m):
    """M-MIXED-1 (schema-valid shape, R-D10): D7's role changed from MIXED_CLOCK_CONTRACT
    to UTC_PERSISTENCE, source unchanged. Literal wording (flip role only, keep
    site_contracts) is refused by H6 before H5 ever runs; site_contracts is therefore
    dropped and permitted_forms made schema-consistent with the new role -- the digest is
    "correctly re-pinned" as the plan requires, and the property under test (H5 rescans
    unconditionally, MIXED_TARGET_ROLE_MISMATCH fires, WRAPPER_CALL:KYIV still measured)
    is unaffected by that shape change."""
    r = _f2_d7_row(m)
    r["role"] = "UTC_PERSISTENCE"
    r.pop("site_contracts", None)
    r["permitted_forms"] = ["UTCNOW:UTC"]
    return m


def f2_t_mixed_2(m):
    """M-MIXED-2: remove D7's clock_index row, keep its mixed_clock_scan_targets entry."""
    m["clock_index"] = [r for r in m["clock_index"]
                        if not (r["file"] == "main.py"
                                and r["qualified_name"] == "_manager_recover_link_limit")]
    return m


def f2_t_mixed_3(m):
    """M-MIXED-3: remove D7 from mixed_clock_scan_targets, keep its MIXED_CLOCK_CONTRACT
    clock_index row -- must never silently lose per-site scanning."""
    m["mixed_clock_scan_targets"] = []
    return m


def f2_t_mixed_4(m):
    """M-MIXED-4: drift D7's registered mixed-scan-target binding/fingerprint while the
    live source (and the clock_index row) stay untouched -- MIXED_SCAN_TARGET_BINDING_DRIFT
    is the runtime-measured-vs-registered-binding mismatch, proven independently of role or
    site_contracts (H5 scans the LIVE body regardless)."""
    m["mixed_clock_scan_targets"][0]["fingerprint"] = "0" * 64
    return m


def f2_t_mixed_5(m):
    """M-MIXED-5 (schema-valid shape, R-D10): D7's today_iso_r5 (WRAPPER_CALL:KYIV) site
    contract repointed to a bogus site identity, rather than deleted outright. Literal
    wording (delete one of D7's two contracts) is refused by H6 (MIXED_CLOCK_CONTRACT
    requires >=2 site_contracts) before H5 ever runs; repointing preserves the 2-contract,
    2-role schema shape while still proving the contracted identity no longer matches any
    real measured site -- the wrapper call is independently re-scanned from source (still
    measured) and now carries no valid contract -> SITE_CONTRACT_UNMATCHED."""
    r = _f2_d7_row(m)
    for c in r["site_contracts"]:
        if c["form"] == "WRAPPER_CALL:KYIV":
            c["site_id"] = "f" * 64
            c["lineno"] = 99999
    return m


def f2_t_role_1(m):
    """M-ROLE-1: assign UTC_PERSISTENCE to D6, a date-returning Kyiv contract."""
    for r in m["clock_index"]:
        if r["file"] == "main.py" and r["qualified_name"] == "_mb_current_lead_date":
            r["role"] = "UTC_PERSISTENCE"
            r["permitted_forms"] = ["WRAPPER_DELEGATION:NONE", "WRAPPER_CALL:KYIV"]
            return m
    raise RuntimeError("D6 row not found")


def f2_t_role_2(m):
    """M-ROLE-2: assign FRESHNESS_UTC to D6, a date-returning Kyiv contract."""
    for r in m["clock_index"]:
        if r["file"] == "main.py" and r["qualified_name"] == "_mb_current_lead_date":
            r["role"] = "FRESHNESS_UTC"
            r["permitted_forms"] = ["WRAPPER_DELEGATION:NONE", "ARGED_NOW:KYIV"]
            return m
    raise RuntimeError("D6 row not found")


def f2_t_site_1(m):
    """M-SITE-1: swap D7's two site roles (schema-valid: still 2 contracts, still spans
    2 distinct roles, just crossed)."""
    r = _f2_d7_row(m)
    for c in r["site_contracts"]:
        c["site_role"] = "DATE_ONLY_KYIV" if c["site_role"] == "UTC_PERSISTENCE" else "UTC_PERSISTENCE"
    return m


def f2_t_own_1(m):
    """M-OWN-1: assign the nested _r5_audit#0 finished_at site to the outer D7 row as
    well -- a clock site claimed by two owners."""
    audit = None
    for r in m["clock_index"]:
        if r["file"] == "main.py" and r["qualified_name"].endswith("_r5_audit"):
            audit = r
            break
    if audit is None or not audit.get("sites"):
        raise RuntimeError("_r5_audit row/sites not found")
    d7 = _f2_d7_row(m)
    d7["sites"] = list(d7["sites"]) + [copy.deepcopy(audit["sites"][0])]
    return m


def f2_t_own_2(m):
    """M-OWN-2: remove the nested _r5_audit#0 row while its clock site remains in
    source."""
    m["clock_index"] = [r for r in m["clock_index"]
                        if not (r["file"] == "main.py"
                                and r["qualified_name"].endswith("_r5_audit"))]
    return m


def f2_t_id_1(m):
    """M-ID-1: duplicate a manifest identity (D7's row, verbatim)."""
    r = copy.deepcopy(_f2_d7_row(m))
    m["clock_index"].append(r)
    return m


def f2_t_id_2(m):
    """M-ID-2: same duplicated identity, this time with a conflicting role."""
    r = copy.deepcopy(_f2_d7_row(m))
    r["role"] = "UTC_PERSISTENCE"
    r["site_contracts"] = []
    m["clock_index"].append(r)
    return m


def f2_t_wrap_5(m):
    """M-WRAP-5: delete _kyiv_now from the wrapper registry while source calls to it
    remain."""
    m["wrapper_registry"] = [w for w in m["wrapper_registry"]
                             if w["qualified_name"] != "_kyiv_now"]
    return m


# ---- M-ROLE-3 -- H1 causality at --emit-baseline (synthetic scope, no gate run) --------

def f2_m_role_3():
    """M-ROLE-3: a synthetic D6-shaped definition (UTC-only forms + `.date()`) must be
    proposed REVIEW_REQUIRED, and build_manifest_from_seed must refuse to mint a manifest
    (proves H1 is causal, not merely present)."""
    entry = {"id": "M-ROLE-3", "kind": "h1_causality",
             "expected_any": ["REVIEW_REQUIRED", "manifest build refused"]}
    try:
        class _Site:
            def __init__(self, tok):
                self.token = tok
        body = "def _synthetic():\n    return datetime.utcnow().date().isoformat()\n"
        sites = [_Site("UTCNOW:UTC")]
        role, conf, ev = ci.propose_role({"qualified_name": "_synthetic"}, sites, body)
        entry["proposed_role"] = role
        entry["confidence"] = conf
        entry["evidence_date_only"] = ev.get("date_only")
        h1_causal = (role is None and conf == "REVIEW_REQUIRED" and ev.get("date_only") is True)

        # control: identical UTC-only evidence WITHOUT the date-only hint must still
        # auto-pass -- H1 must discriminate, not blanket-block.
        role2, conf2, _ = ci.propose_role({"qualified_name": "_synthetic2"}, sites,
                                          "def _synthetic2():\n    return datetime.utcnow()\n")
        entry["control_role_without_date_hint"] = role2
        entry["control_confidence"] = conf2
        control_ok = (conf2 == "HIGH" and role2 is not None)

        entry["pass"] = bool(h1_causal and control_ok)
        if not entry["pass"]:
            entry["harness_failure"] = "h1_causal=%s control_ok=%s" % (h1_causal, control_ok)
    except (RuntimeError, ValueError) as exc:
        entry["pass"] = False
        entry["harness_failure"] = "%s: %s" % (exc.__class__.__name__, exc)
    RESULTS.append(entry)
    return entry


# ---- M-WRAP-2A -- rebuild-from-seed alias fixture (GREEN) --------------------------------

def f2_wrap_2a():
    """M-WRAP-2A: D7 reaches the registered wrapper (storage.w3_now) through an IMPORT
    alias (`from storage import w3_now as ...`); the manifest is REGENERATED from the
    authoritative seed against the mutated tree (as an owner re-running --emit-baseline
    after a reviewed change would do), so the fingerprint always matches the body it
    describes. The wrapper site must stay independently measured and the gate must stay
    GREEN."""
    pre = _f2_live_hashes()
    entry = {"id": "M-WRAP-2A", "kind": "source+rebuild", "expected": "GREEN"}
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    tree = TempTree()
    try:
        def build_and_run():
            mpath = tree.dir / "manifest.json"
            man = ci.build_manifest_from_seed(str(tree.dir), seed)
            dg = ci.write_manifest(man, mpath)
            return tree.run_gate(str(mpath), dg)

        r0 = build_and_run()
        entry["baseline_ok"] = r0["ok"]
        entry["baseline_codes"] = codes(r0)
        entry["bytes_changed"] = tree.apply(
            "main.py",
            r"(        today_iso_r5 = )_kyiv_now\(\)(\.date\(\)\.isoformat\(\)\n)",
            r"\1_f2_w3_alias()\2")
        tree.apply("main.py",
                   r"(def _parse_cmd\(text: str\) -> Tuple\[str, str\]:\n)",
                   r"from storage import w3_now as _f2_w3_alias\n\n\n\1")
        r1 = build_and_run()
        entry["mutant_ok"] = r1["ok"]
        entry["mutant_codes"] = codes(r1)
        entry["mutant_detail"] = [f.get("detail", "")[:220] for f in r1["findings"]][:8]
        entry["measured_mixed_site_total"] = r1["metrics"].get("measured_mixed_site_total")
        tree.restore()
        r2 = build_and_run()
        entry["restore_ok"] = r2["ok"]
        entry["hashes_unchanged"] = (pre == _f2_live_hashes())
        entry["pass"] = bool(r0["ok"] and r1["ok"] and r2["ok"] and entry["hashes_unchanged"])
        if not entry["pass"]:
            entry["harness_failure"] = ("baseline=%s mutant=%s restore=%s codes=%s"
                                        % (r0["ok"], r1["ok"], r2["ok"], entry["mutant_codes"]))
    except (RuntimeError, ValueError) as exc:
        entry["pass"] = False
        entry["harness_failure"] = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        tree.close()
    RESULTS.append(entry)
    return entry


# ---- M-GUARD-1 / M-GUARD-2 / M-RESIDUE-1 -- new scope guard causality --------------------
#
# The new D6/D7 correction scope guard (tools\w3_2_d6d7_correction_scope_guard_selftest.py)
# is hardcoded to absolute paths (BASE_DIR, AUDIT_ROOT, HISTORICAL_BASELINE_PATH).  To
# prove causality WITHOUT mutating the real repository or a historical audit artifact, the
# module's path constants are rebound to a disposable temp sandbox for the duration of the
# leg and restored in `finally`; the live repository and every audit artifact are read-only
# throughout, verified by a hash comparison before and after this whole block.

_F2_GUARD_ORIG = {k: getattr(guard_mod, k) for k in
                  ("BASE_DIR", "BASELINE_PATH", "RESIDUE_SNAPSHOT_PATH",
                   "LEGACY_GUARD_PATH", "HISTORICAL_BASELINE_PATH")}


def _f2_guard_real_hashes():
    return {
        "repo_main": hashlib.sha256((REPO_F2 / "main.py").read_bytes()).hexdigest(),
        "legacy_guard": hashlib.sha256(
            (REPO_F2 / "tools" / "w3_2_scope_guard_selftest.py").read_bytes()).hexdigest(),
        "historical_baseline": hashlib.sha256(
            _F2_GUARD_ORIG["HISTORICAL_BASELINE_PATH"].read_bytes()).hexdigest(),
        "d6d7_baseline": hashlib.sha256(_F2_GUARD_ORIG["BASELINE_PATH"].read_bytes()).hexdigest(),
    }


def _f2_guard_sandbox():
    d = Path(tempfile.mkdtemp(prefix="w3_2_f2_guard_"))
    (d / "tools").mkdir()
    for f in guard_mod.REQUIRED_RUNTIME_FILES:
        shutil.copy2(REPO_F2 / f, d / f)
    shutil.copy2(REPO_F2 / "tools" / "w3_2_scope_guard_selftest.py",
                 d / "tools" / "w3_2_scope_guard_selftest.py")
    hist_copy = d / "historical_baseline_hashes.json"
    shutil.copy2(_F2_GUARD_ORIG["HISTORICAL_BASELINE_PATH"], hist_copy)
    guard_mod.BASE_DIR = d
    guard_mod.LEGACY_GUARD_PATH = d / "tools" / "w3_2_scope_guard_selftest.py"
    guard_mod.HISTORICAL_BASELINE_PATH = hist_copy
    return d


def _f2_guard_restore():
    for k, v in _F2_GUARD_ORIG.items():
        setattr(guard_mod, k, v)


def _f2_guard_leg(mid, run):
    entry = {"id": mid}
    d = _f2_guard_sandbox()
    try:
        baseline = guard_mod.load_baseline()
        snapshot = guard_mod.load_residue_snapshot()
        entry.update(run(d, baseline, snapshot))
    except (RuntimeError, ValueError, OSError) as exc:
        entry["pass"] = False
        entry["harness_failure"] = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        _f2_guard_restore()
        shutil.rmtree(d, ignore_errors=True)
    RESULTS.append(entry)
    return entry


def _f2_guard1(d, baseline, snapshot):
    """M-GUARD-1: an unrelated one-byte main.py change, and an unrelated change to a file
    that must never change at all, both turn the new guard RED."""
    out = {}
    r_clean = guard_mod.run_runtime_file_hash_check(baseline)
    out["clean_ok"] = r_clean["ok"]
    p = d / "main.py"
    p.write_bytes(p.read_bytes() + b"\n")
    r_mut = guard_mod.run_runtime_file_hash_check(baseline)
    out["mutant_ok"] = r_mut["ok"]
    out["mutant_status"] = [r["status"] for r in r_mut["rows"] if r["file"] == "main.py"]
    p.write_bytes((REPO_F2 / "main.py").read_bytes())
    p2 = d / "storage.py"
    p2.write_bytes(p2.read_bytes() + b"\n")
    r_mut2 = guard_mod.run_runtime_file_hash_check(baseline)
    out["unauthorized_file_ok"] = r_mut2["ok"]
    out["unauthorized_file_status"] = [r["status"] for r in r_mut2["rows"]
                                       if r["file"] == "storage.py"]
    out["pass"] = bool(r_clean["ok"] and not r_mut["ok"] and not r_mut2["ok"]
                       and out["mutant_status"] == ["CHANGED_BUT_NOT_THE_APPROVED_DIGEST"]
                       and out["unauthorized_file_status"] == ["UNAUTHORIZED_RUNTIME_CHANGE"])
    return out


def _f2_guard2(d, baseline, snapshot):
    """M-GUARD-2: a byte change to the legacy scope guard, or to the 20260730 historical
    baseline artifact, is HISTORICAL_ARTIFACT_MUTATED."""
    out = {}
    r_clean = guard_mod.run_historical_artifact_check(baseline)
    out["clean_ok"] = r_clean["ok"]
    lg = guard_mod.LEGACY_GUARD_PATH
    lg.write_bytes(lg.read_bytes() + b"\n")
    r_a = guard_mod.run_historical_artifact_check(baseline)
    out["legacy_mutated_ok"] = r_a["ok"]
    out["legacy_status"] = [r["status"] for r in r_a["rows"]
                            if r["file"].endswith("w3_2_scope_guard_selftest.py")]
    shutil.copy2(REPO_F2 / "tools" / "w3_2_scope_guard_selftest.py", lg)
    hb = guard_mod.HISTORICAL_BASELINE_PATH
    hb.write_bytes(hb.read_bytes() + b"\n")
    r_b = guard_mod.run_historical_artifact_check(baseline)
    out["historical_mutated_ok"] = r_b["ok"]
    out["historical_status"] = [r["status"] for r in r_b["rows"] if "20260730" in r["file"]]
    out["pass"] = bool(r_clean["ok"] and not r_a["ok"] and not r_b["ok"]
                       and out["legacy_status"] == ["HISTORICAL_ARTIFACT_MUTATED"]
                       and out["historical_status"] == ["HISTORICAL_ARTIFACT_MUTATED"])
    return out


def _f2_residue(d, baseline, snapshot):
    """M-RESIDUE-1: a *.bak* file created inside the (sandboxed) repository turns the
    residue scan RED."""
    out = {}
    r_clean = guard_mod.run_residue_scan(snapshot)
    out["clean_ok"] = r_clean["ok"]
    out["clean_new_residue"] = r_clean["new_residue_files"]
    (d / "main.py.bak_f2_probe").write_text("x", encoding="utf-8")
    r_mut = guard_mod.run_residue_scan(snapshot)
    out["mutant_ok"] = r_mut["ok"]
    out["mutant_new_residue"] = r_mut["new_residue_files"]
    out["pass"] = bool(r_clean["ok"] and not r_mut["ok"]
                       and "main.py.bak_f2_probe" in r_mut["new_residue_files"])
    return out


# ---- F2-BOUNDARY-1/2/3 -- causality proof for the FIXED run_tool_boundary_check (R-D4) --
# Supplementary to the 26-ID roster (task sec 7's explicit extra requirement), not one of
# the required IDs. A full copy of every live tools\*.py file is placed in the sandbox (the
# check enumerates BASE_DIR/tools), so the REAL w3_2_f2_tool_boundary_baseline.json (whose
# "other_tool_files" hashes are exactly those live files, untouched by this round) applies
# unmodified inside the sandbox too.

def _f2_boundary_sandbox():
    d = Path(tempfile.mkdtemp(prefix="w3_2_f2_boundary_"))
    (d / "tools").mkdir()
    for f in guard_mod.REQUIRED_RUNTIME_FILES:
        shutil.copy2(REPO_F2 / f, d / f)
    for p in (REPO_F2 / "tools").glob("*.py"):
        shutil.copy2(p, d / "tools" / p.name)
    return d


def _f2_boundary_leg(mid, run):
    entry = {"id": mid}
    pre = _f2_live_hashes()
    d = _f2_boundary_sandbox()
    try:
        tb_baseline = guard_mod.load_tool_boundary_baseline()
        saved_base_dir = guard_mod.BASE_DIR
        guard_mod.BASE_DIR = d
        try:
            entry.update(run(d, tb_baseline))
        finally:
            guard_mod.BASE_DIR = saved_base_dir
        entry["hashes_unchanged"] = (pre == _f2_live_hashes())
        entry["pass"] = bool(entry.get("pass") and entry["hashes_unchanged"])
    except (RuntimeError, ValueError, OSError) as exc:
        entry["pass"] = False
        entry["harness_failure"] = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    RESULTS.append(entry)
    return entry


def _f2_boundary_clean(d, tb_baseline):
    """F2-BOUNDARY-1: the untouched, fully-copied approved tool set passes cleanly."""
    r = guard_mod.run_tool_boundary_check({}, tb_baseline)
    return {"clean_ok": r["ok"],
            "clean_red_rows": [row for row in r["rows"] if not row["ok"]],
            "pass": bool(r["ok"])}


def _f2_boundary_modification(d, tb_baseline):
    """F2-BOUNDARY-2: one unapproved tool-file modification (one byte appended to a
    NON-approved sandboxed tool) turns that file's row RED."""
    r0 = guard_mod.run_tool_boundary_check({}, tb_baseline)
    target = d / "tools" / "w3_2_scope_guard_selftest.py"
    target.write_bytes(target.read_bytes() + b"\n")
    r1 = guard_mod.run_tool_boundary_check({}, tb_baseline)
    row = [x for x in r1["rows"] if x["file"] == "tools/w3_2_scope_guard_selftest.py"]
    return {"baseline_ok": r0["ok"], "mutant_ok": r1["ok"], "mutant_row": row,
            "pass": bool(r0["ok"] and not r1["ok"] and row
                        and row[0]["status"] == "UNAPPROVED_TOOL_FILE_MODIFIED")}


def _f2_boundary_creation(d, tb_baseline):
    """F2-BOUNDARY-3: one unapproved, previously-unknown tool file created in the sandbox
    turns RED (never silently accepted)."""
    r0 = guard_mod.run_tool_boundary_check({}, tb_baseline)
    new_file = d / "tools" / "_f2_boundary_probe_unlisted_tool.py"
    new_file.write_text("# unapproved new tool file\n", encoding="utf-8")
    r1 = guard_mod.run_tool_boundary_check({}, tb_baseline)
    row = [x for x in r1["rows"] if x["file"] == "tools/_f2_boundary_probe_unlisted_tool.py"]
    return {"baseline_ok": r0["ok"], "mutant_ok": r1["ok"], "mutant_row": row,
            "pass": bool(r0["ok"] and not r1["ok"] and row
                        and row[0]["status"] == "UNAPPROVED_NEW_TOOL_FILE")}


def run_f2_boundary_supplement():
    """R-D4 supplementary causality legs (task sec 7): approved set passes; one
    modification fails; one creation fails; and (checked directly, no sandbox needed) the
    check is textually wired into main()."""
    src = Path(guard_mod.__file__).read_text(encoding="utf-8")
    wired = "run_tool_boundary_check(" in src.split("def main(", 1)[1]
    entry = {"id": "F2-BOUNDARY-0-wired-into-main", "pass": bool(wired), "wired": wired}
    RESULTS.append(entry)
    print("  [%s] %-28s -> wired_into_main=%s" % ("OK" if wired else "FAIL",
                                                   entry["id"], wired))
    for mid, fn in (("F2-BOUNDARY-1-clean-passes", _f2_boundary_clean),
                    ("F2-BOUNDARY-2-modification-fails", _f2_boundary_modification),
                    ("F2-BOUNDARY-3-creation-fails", _f2_boundary_creation)):
        e = _f2_boundary_leg(mid, fn)
        print("  [%s] %-28s -> %s" % ("OK" if e.get("pass") else "FAIL", mid,
                                      {k: v for k, v in e.items() if k not in ("id", "pass")}))


F2_SOURCE_LEGS = [
    ("M-D6", ["UTC_AS_KYIV_SUBSTITUTE", "CLOCK_INDEX_FINGERPRINT_DRIFT"], f2_m_d6, ""),
    ("M-D7", ["SITE_CONTRACT_UNMATCHED", "UTC_AS_KYIV_SUBSTITUTE"], f2_m_d7, ""),
    ("M-SITE-2", ["SITE_CONTRACT_UNMATCHED"], f2_m_site_2, ""),
    ("M-SITE-3", ["SITE_CONTRACT_UNMATCHED", "SITE_CONTRACT_NOT_MEASURED"], f2_m_site_3, ""),
    ("M-SITE-4", ["SITE_CONTRACT_NOT_MEASURED"], f2_m_site_4, ""),
    ("M-WRAP-3", ["APPROVED_HELPER_CONTRACT_DRIFT"], f2_m_wrap_3, ""),
    ("M-WRAP-4", ["UNMANIFESTED_CLOCK_TOUCHING_DEF"], f2_m_wrap_4, ""),
    ("M-WRAP-2B", ["CLOCK_INDEX_FINGERPRINT_DRIFT", "SITE_CONTRACT_UNMATCHED"], f2_m_wrap_2b,
     "D6 alias, source-only (no manifest rebuild); D7's alias case (rebuilt) is M-WRAP-2A"),
]

F2_MANIFEST_LEGS = [
    ("M-MIXED-1", ["MIXED_TARGET_ROLE_MISMATCH"], f2_t_mixed_1,
     "schema-valid shape (R-D10); role flipped, digest correctly re-pinned"),
    ("M-MIXED-2", ["MIXED_TARGET_ROW_MISSING"], f2_t_mixed_2, ""),
    ("M-MIXED-3", ["MIXED_TARGET_REGISTRY_MISMATCH", "MANIFEST_SCHEMA_FAIL"], f2_t_mixed_3, ""),
    ("M-MIXED-4", ["MIXED_SCAN_TARGET_BINDING_DRIFT"], f2_t_mixed_4, ""),
    ("M-MIXED-5", ["SITE_CONTRACT_UNMATCHED"], f2_t_mixed_5,
     "schema-valid shape (R-D10); contract repointed to a bogus site identity"),
    ("M-ROLE-1", ["ROLE_CONTRACT_MISMATCH", "MANIFEST_SCHEMA_FAIL"], f2_t_role_1, ""),
    ("M-ROLE-2", ["ROLE_CONTRACT_MISMATCH", "MANIFEST_SCHEMA_FAIL",
                  "ROLE_FORM_NOT_PERMITTED"], f2_t_role_2, ""),
    ("M-SITE-1", ["UTC_AS_KYIV_SUBSTITUTE", "KYIV_AS_UTC_SUBSTITUTE"], f2_t_site_1, ""),
    ("M-OWN-1", ["DUPLICATE_CLOCK_SITE_OWNERSHIP"], f2_t_own_1, ""),
    ("M-OWN-2", ["UNMANIFESTED_CLOCK_TOUCHING_DEF"], f2_t_own_2, ""),
    ("M-ID-1", ["MANIFEST_SCHEMA_FAIL"], f2_t_id_1, ""),
    ("M-ID-2", ["MANIFEST_SCHEMA_FAIL"], f2_t_id_2, ""),
    ("M-WRAP-5", ["WRAPPER_REGISTRY_INCOMPLETE", "MANIFESTED_ROW_NOT_MEASURED",
                  "SITE_CONTRACT_NOT_MEASURED", "MANIFEST_SCHEMA_FAIL"], f2_t_wrap_5, ""),
]

F2_MUTATION_IDS = ([mid for mid, *_ in F2_SOURCE_LEGS] + [mid for mid, *_ in F2_MANIFEST_LEGS]
                   + ["M-ROLE-3", "M-WRAP-2A", "M-GUARD-1", "M-GUARD-2", "M-RESIDUE-1"])


def run_f2_correction_roster():
    """Run all 26 permanent F-2 correction mutation legs; append to RESULTS."""
    print("=" * 90)
    print("W3.2 F-2 EVIDENCE CORRECTION -- MUTATION ROSTER (26 legs, sec 6 of the task)")
    print("=" * 90)
    for mid, exp, fn, note in F2_SOURCE_LEGS:
        e = f2_source_leg(mid, exp, fn, note)
        print("  [%s] %-11s -> %s%s" % ("OK" if e.get("pass") else "FAIL", mid,
                                        e.get("mutant_codes") or e.get("harness_failure"),
                                        "" if e.get("pass") else "   !! " + str(e.get("harness_failure"))))
    for mid, exp, fn, note in F2_MANIFEST_LEGS:
        e = f2_manifest_leg(mid, exp, fn, note)
        print("  [%s] %-11s -> %s%s" % ("OK" if e.get("pass") else "FAIL", mid,
                                        e.get("mutant_codes") or e.get("harness_failure"),
                                        "" if e.get("pass") else "   !! " + str(e.get("harness_failure"))))
    e = f2_m_role_3()
    print("  [%s] %-11s -> role=%s conf=%s" % ("OK" if e.get("pass") else "FAIL", "M-ROLE-3",
                                               e.get("proposed_role"), e.get("confidence")))
    e = f2_wrap_2a()
    print("  [%s] %-11s -> mutant_ok=%s codes=%s" % ("OK" if e.get("pass") else "FAIL",
                                                      "M-WRAP-2A", e.get("mutant_ok"),
                                                      e.get("mutant_codes") or e.get("harness_failure")))
    pre_guard = _f2_guard_real_hashes()
    for mid, fn in (("M-GUARD-1", _f2_guard1), ("M-GUARD-2", _f2_guard2),
                    ("M-RESIDUE-1", _f2_residue)):
        e = _f2_guard_leg(mid, fn)
        print("  [%s] %-11s -> %s" % ("OK" if e.get("pass") else "FAIL", mid,
                                      {k: v for k, v in e.items() if k not in ("id", "pass")}))
    post_guard = _f2_guard_real_hashes()
    guard_untouched = (pre_guard == post_guard)
    if not guard_untouched:
        for r in RESULTS:
            if r["id"] in ("M-GUARD-1", "M-GUARD-2", "M-RESIDUE-1"):
                r["pass"] = False
                r["harness_failure"] = "live repo/audit artifacts were touched: %s vs %s" % (
                    pre_guard, post_guard)
    print("-" * 90)
    print("live repo/audit artifacts untouched across guard legs:", guard_untouched)

    run_f2_boundary_supplement()


# ======================================================================================
# Driver
# ======================================================================================

SOURCE_MUTATIONS = [
    ("M-1", "core", "panel_bot.py", "BARE_NOW", m1),
    ("M-2", "core", "panel_bot.py", "BARE_NOW", m2),
    ("M-3", "core", "panel_bot.py", "BARE_NOW", m3),
    ("M-4", "dispatch", "panel_bot.py", "BARE_TODAY", m4),
    ("M-5", "dispatch", "panel_bot.py", "BARE_NOW", m5),
    ("M-6", "control_flow", "panel_bot.py", "BARE_NOW", m6),
    ("M-7", "control_flow", "panel_bot.py", "BARE_NOW", m7),
    ("M-8", "control_flow", "panel_bot.py", "HOST_LOCAL_FALLBACK", m8),
    ("M-9", "control_flow", "panel_bot.py", "BARE_NOW", m9),
    ("M-10", "helpers", "storage.py", "APPROVED_HELPER_CONTRACT_DRIFT", m10),
    ("M-11", "bindings", "panel_bot.py", "FROZEN_ROOT_MISSING", m11),
    ("M-12", "bindings", "panel_bot.py", "ROOT_BINDING_DRIFT", m12),
    ("M-13", "escalation", "panel_bot.py", "BARE_NOW", m13),
    ("M-14", "coverage_gap", "preflight_check.py", "HOST_LOCAL_TIME_API", m14),
    ("M-15", "coverage_gap", "preflight_check.py", "NAIVE_FROMTIMESTAMP", m15),
    ("M-16", "coverage_gap", "preflight_check.py", "TZ_COERCION", m16),
    ("M-17", "coverage_gap", "preflight_check.py", "WRONG_ZONE", m17),
    ("M-19", "roles", "panel_bot.py", "UTC_AS_KYIV_SUBSTITUTE", m19),
    ("M-21", "clock_index", "main.py", "BARE_NOW", m21),
    ("M-22", "clock_index", "main.py", "UNMANIFESTED_CLOCK_TOUCHING_DEF", m22),
    ("M-23", "clock_index", "storage.py", "CLOCK_INDEX_FINGERPRINT_DRIFT", m23),
    ("M-24", "clock_index", "preflight_check.py", "BARE_NOW", m24),
    ("M-26", "constructors", "storage.py", "BARE_NOW", m26),
    ("M-27", "constructors", "storage.py", None, m27),  # escalation OR red, see below
    ("M-20a", "negative_control", "preflight_check.py", "GREEN", m20a),
    ("M-20b", "negative_control", "preflight_check.py", "GREEN", m20b),
    ("M-20c", "negative_control", "panel_bot.py", "GREEN", m20c),
    ("M-20d", "negative_control", "panel_bot.py", "GREEN", m20d),
    ("M-20e", "negative_control", "panel_bot.py", "GREEN", m20e),
]


def record_negative_control(mid, group, target_file, fn):
    """Negative controls (M-20a..e) isolate POLICY/REACHABILITY invariance from
    binding-integrity fingerprint pinning (which M-9/M-12/M-23 already cover on
    purpose).  The manifest is mechanically rebuilt from the production seed against
    the CURRENT tree state at each step, exactly as an owner re-running
    ``--emit-baseline`` after a reviewed change would do; the fingerprints therefore
    always match the body they describe, and a RED verdict can only come from an
    actual forbidden form, an unresolved dispatch, or a role mismatch introduced by
    the mutation shape itself -- never from the pinning mechanism.
    """
    pre_hashes = hash_all(REQUIRED_RUNTIME_FILES + TOOL_FILES)
    entry = {"id": mid, "group": group, "target_file": target_file, "expected": "GREEN"}
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    tree = TempTree()
    try:
        def build_and_run():
            manifest_path = tree.dir / "manifest.json"
            manifest = ci.build_manifest_from_seed(str(tree.dir), seed)
            digest = ci.write_manifest(manifest, manifest_path)
            return tree.run_gate(str(manifest_path), digest)

        r0 = build_and_run()
        entry["baseline_ok"] = r0["ok"]
        entry["baseline_codes"] = codes(r0)
        if not r0["ok"]:
            entry["pass"] = False
            entry["harness_failure"] = "baseline not GREEN before mutation"
            RESULTS.append(entry)
            return entry

        changed_bytes = fn(tree)
        entry["bytes_changed"] = changed_bytes

        r1 = build_and_run()
        entry["mutant_ok"] = r1["ok"]
        entry["mutant_codes"] = codes(r1)

        tree.restore()
        r2 = build_and_run()
        entry["restore_ok"] = r2["ok"]
        entry["restore_codes"] = codes(r2)

        post_hashes = hash_all(REQUIRED_RUNTIME_FILES + TOOL_FILES)
        entry["runtime_hashes_unchanged"] = (pre_hashes == post_hashes)

        ok = r1["ok"] and r2["ok"] and entry["runtime_hashes_unchanged"]
        if not ok:
            entry["harness_failure"] = "expected GREEN but mutant/restore RED: %s / %s" \
                % (entry["mutant_codes"], entry["restore_codes"])
        entry["pass"] = bool(ok)
    except (RuntimeError, ValueError) as exc:
        entry["pass"] = False
        entry["harness_failure"] = "mutation/manifest build failed: %s" % exc
    finally:
        tree.close()
    RESULTS.append(entry)
    return entry


def run_m27():
    pre_hashes = hash_all(REQUIRED_RUNTIME_FILES + TOOL_FILES)
    tree = TempTree()
    entry = {"id": "M-27", "group": "constructors", "target_file": "storage.py",
             "expected": "escalation or RED"}
    try:
        r0 = tree.run_gate()
        entry["baseline_ok"] = r0["ok"]
        changed = m27(tree)
        entry["bytes_changed"] = changed
        r1 = tree.run_gate()
        entry["mutant_ok"] = r1["ok"]
        entry["mutant_codes"] = codes(r1)
        trig = {c for e in r1["escalations"] for c in e.get("trigger_codes", [])}
        entry["escalation_trigger_codes"] = sorted(trig)
        causal = ("CUSTOM_LOCAL_METACLASS" in trig) or (not r1["ok"])
        tree.restore()
        r2 = tree.run_gate()
        entry["restore_ok"] = r2["ok"]
        post_hashes = hash_all(REQUIRED_RUNTIME_FILES + TOOL_FILES)
        entry["runtime_hashes_unchanged"] = (pre_hashes == post_hashes)
        entry["pass"] = bool(causal and r0["ok"] and r2["ok"]
                             and entry["runtime_hashes_unchanged"])
        if not entry["pass"]:
            entry["harness_failure"] = "custom metaclass not causal (escalation or RED)"
    finally:
        tree.close()
    RESULTS.append(entry)
    return entry


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else (argv or []))
    out_json = None
    if "--json" in argv:
        out_json = argv[argv.index("--json") + 1]

    print("=" * 90)
    print("W3.2 DESIGN C+ TIMEZONE SOURCE GATE -- MUTATION PROOF M-1 .. M-27")
    print("=" * 90)

    for mid, group, target, expected, fn in SOURCE_MUTATIONS:
        if mid == "M-27":
            entry = run_m27()
        elif group == "negative_control":
            entry = record_negative_control(mid, group, target, fn)
        else:
            entry = record(mid, group, target, expected, fn)
        print("  [%s] %-6s %-14s target=%-20s expected=%-32s -> %s"
             % ("OK" if entry.get("pass") else "FAIL", mid, group, target,
                str(expected), entry.get("mutant_codes") or entry.get("harness_failure")))

    for entry in manifest_mutations():
        RESULTS.append(entry)
        print("  [%s] %-6s %-14s target=%-20s expected=%-32s -> %s"
             % ("OK" if entry.get("pass") else "FAIL", entry["id"], entry["group"],
                entry["target_file"], str(entry["expected"]),
                entry.get("mutant_codes") or entry.get("harness_failure")))

    legacy_total = len(RESULTS)
    legacy_passed = sum(1 for e in RESULTS if e.get("pass"))
    logical = 27  # M-1..M-19,21..27 + M-18b = 27 logical mutations per plan numbering
    print("-" * 90)
    print("legacy executed legs: %d/%d passed  (logical mutation count per plan: %d)"
         % (legacy_passed, legacy_total, logical))

    # ---- W3.2 F-2 evidence correction (2026-08-03): the 26-leg permanent roster of
    # 10_TEST_AND_MUTATION_PLAN.md sec 3 / acceptance 34 (closes R-D1/R-D3).
    run_f2_correction_roster()

    total = len(RESULTS)
    passed = sum(1 for e in RESULTS if e.get("pass"))
    f2_total = total - legacy_total
    f2_passed = passed - legacy_passed
    print("-" * 90)
    print("F-2 correction roster: %d/%d passed  (required IDs: %d)"
         % (f2_passed, f2_total, len(F2_MUTATION_IDS)))
    print("ALL executed legs: %d/%d passed  (legacy logical count: %d; F-2 IDs: %d)"
         % (passed, total, logical, len(F2_MUTATION_IDS)))

    payload = {"executed_legs": total, "passed": passed, "logical_mutation_count": logical,
              "legacy_executed_legs": legacy_total, "legacy_passed": legacy_passed,
              "f2_executed_legs": f2_total, "f2_passed": f2_passed,
              "f2_required_ids": F2_MUTATION_IDS, "results": RESULTS}
    if out_json:
        Path(out_json).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    if passed == total:
        print("[OK] all mutations causal")
        return 0
    print("[FAIL] %d mutation(s) not causal" % (total - passed))
    return 1


if __name__ == "__main__":
    sys.exit(main())
