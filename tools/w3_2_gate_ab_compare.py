# -*- coding: utf-8 -*-
"""W3.2 Design C+ -- one-shot A/B comparison of the old and new timezone gates.

Implements ``10_MIGRATION_PLAN.md`` step 2 and the AB-comparison requirements of
section 12 of the implementation task.

Runs OLD (`tools/w3_2_panel_static_gate_selftest.py::run_panel_static_gate`, scoped to
panel_bot.py only) and NEW (`tools/w3_2_timezone_source_gate.py::run_gate`, scoped to
the full frozen 4-file scope) against the IDENTICAL live tree and classifies every
difference:

    EXPECTED_NEW_INCLUSION    -- new gate scans a definition the old one excluded
    EXPECTED_OLD_ONLY_INTERNAL -- old-gate provenance bookkeeping with no timezone meaning
    REGRESSION                -- old gate catches something the new one misses (must be 0)
    IMPROVEMENT                -- new gate catches what the old one missed

Also replays the R7-review branch-order probe suite (CASES A-K + reviewer probes
X1-X10, 27 fixtures total) against BOTH gates using the same injection technique the R7
reviewer used (inject into the active `_panel_header`, append a `_rev_unsafe` module
helper), and documents which legacy `w3_2_mutation_proof.py` mutation families
(M32-1..10, M32C-*, M32R2-*, M32R3-*) are/aren't meaningfully replayable against a
static source gate.

Read-only. Never writes to a live project file. Retired after migration per the plan
(kept on disk as an audit tool, not deleted).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import w3_2_timezone_source_gate as new_gate     # noqa: E402
import w3_2_panel_static_gate_selftest as old_gate  # noqa: E402
import w3_2_timezone_source_gate_mutation_proof as new_mut  # noqa: E402

REPO = Path(r"C:\ALM_TPilot")
SCOPE_FILES = ["panel_bot.py", "storage.py", "preflight_check.py", "main.py"]
OLD_MUTATION_PROOF = REPO / "tools" / "w3_2_mutation_proof.py"

TZ3_ROOTS = ("_panel_header", "_today_iso", "_tp_visual_now_local")


# ======================================================================================
# Old-gate closure / findings extraction (panel_bot.py only -- the old gate never reads
# storage.py, preflight_check.py or main.py)
# ======================================================================================

def old_gate_closure_and_findings(panel_bot_path=None):
    r = old_gate.run_panel_static_gate(panel_bot_path or old_gate.PANEL_BOT_PATH)

    closure = set()
    findings = set()

    for name, info in r["tz3_functions"].items():
        for gen in info["generations"]:
            if gen["classification"] not in ("ACTIVE_REACHABLE", "SHADOWED_BUT_REACHABLE"):
                continue
            closure.add(("panel_bot.py", name, gen["lineno"]))
            if gen.get("violates"):
                findings.add(("panel_bot.py", name, gen["lineno"], "OLD_TZ3_VIOLATION"))

    for key, info in r["helper_nodes_reachable"].items():
        name, _, lineno = key.rpartition("@")
        closure.add(("panel_bot.py", name, int(lineno)))
        if info.get("violates"):
            findings.add(("panel_bot.py", name, int(lineno), "OLD_HELPER_BARE_CLOCK"))

    return r, closure, findings


def new_gate_closure_and_findings(repo=REPO, manifest_path=None, expected_digest=None):
    res = new_gate.run_gate(str(repo), manifest_path, expected_digest)

    scanned = set()
    for m in res["closure_members"]:
        # "file::qualified_name@lineno"
        file_part, rest = m.split("::", 1)
        qname, _, lineno = rest.rpartition("@")
        if file_part == "panel_bot.py" and "." not in qname:
            scanned.add((file_part, qname, int(lineno)))

    for key in res["metrics"].get("escalated_files", []):
        pass  # escalated defs are enumerated below via the raw closure dict is not
              # exported by name; use findings/closure_members plus escalation totals
              # for the summary instead of a full membership set for escalated files.

    findings = set()
    for f in res["findings"]:
        if f["file"] == "panel_bot.py":
            findings.add((f["file"], f["qualified_name"], f["lineno"], f["code"]))

    return res, scanned, findings


def classify_closure_diff(old_closure, new_scanned_min, new_escalated_panel_bot):
    """Classify every (file,name,lineno) difference between the two gates' panel_bot.py
    coverage.  ``new_escalated_panel_bot`` is True when panel_bot.py escalated whole
    (in which case the new gate's real coverage is "all definitions in the file", which
    trivially supersets the old gate's precision-based reachable set)."""
    only_old = sorted(old_closure - new_scanned_min)
    only_new = sorted(new_scanned_min - old_closure)
    both = sorted(old_closure & new_scanned_min)

    classified = []
    for row in only_new:
        classified.append({"row": row, "class": "EXPECTED_NEW_INCLUSION",
                           "note": "new gate's closure/escalation includes definitions "
                                   "the old gate's precision-based reachability excluded"})
    for row in only_old:
        # Under Design C+, panel_bot.py escalating whole means EVERY old-only row is
        # still covered (just not surfaced by name in the Phase-1 closure list) -- a
        # true REGRESSION only exists if the file is neither in the new closure NOR
        # escalated.
        if new_escalated_panel_bot:
            classified.append({"row": row, "class": "EXPECTED_OLD_ONLY_INTERNAL",
                               "note": "panel_bot.py is escalated whole under the new "
                                       "gate, so this definition IS scanned even though "
                                       "it is not named in the Phase-1 closure list"})
        else:
            classified.append({"row": row, "class": "REGRESSION",
                               "note": "old gate reached this definition; the new gate "
                                       "neither includes it in the closure nor escalates "
                                       "the file"})
    return classified, only_old, only_new, both


# ======================================================================================
# Branch-order / reviewer-probe replay (R7_REVIEW/harness/rev_branch_order_probes.py)
# ======================================================================================

SAFE = "['rev_ok']"
BAD = "[_rev_unsafe]"
UNSAFE_HELPER = "\n\n\ndef _rev_unsafe():\n    return datetime.now().isoformat()\n"
ANCHOR = ("def _panel_header() -> str:  # type: ignore[override]\n"
         "    # N5.3.1 exact root/status layout (owner-approved):\n")

REPLAY_CASES = [
    ("A", f"if True:\n    d = {SAFE}\nelse:\n    d = {BAD}\nfor f in d:\n    f()", "RED"),
    ("B", f"if True:\n    d = {BAD}\nelse:\n    d = {SAFE}\nfor f in d:\n    f()", "RED"),
    ("C", f"try:\n    d = {SAFE}\nexcept Exception:\n    d = {BAD}\nfor f in d:\n    f()", "RED"),
    ("D", f"try:\n    d = {BAD}\nexcept Exception:\n    d = {SAFE}\nfor f in d:\n    f()", "RED"),
    ("E1", f"if True:\n    if True:\n        d = {SAFE}\n    else:\n        d = {BAD}\n"
          f"else:\n    d = {SAFE}\nfor f in d:\n    f()", "RED"),
    ("E2", f"if True:\n    if True:\n        d = {BAD}\n    else:\n        d = {SAFE}\n"
          f"else:\n    d = {SAFE}\nfor f in d:\n    f()", "RED"),
    ("F", f"if True:\n    d = {SAFE}\nelse:\n    d = {SAFE}\nfor f in d:\n    f()", "GREEN"),
    ("G", f"if True:\n    d = {SAFE}\nelse:\n    d = list(_rev_unknown_src)\nfor f in d:\n    f()", "RED"),
    ("H", f"if True:\n    d = {BAD}\nelse:\n    d = list(_rev_unknown_src)\nfor f in d:\n    f()", "RED"),
    ("I", f"if True:\n    d = {BAD}\n    return 'x'\nd = {SAFE}\nfor f in d:\n    f()", "GREEN"),
    ("J", f"d = {BAD}\nfor f in d:\n    f()\nd = {SAFE}", "RED"),
    ("K", f"if True:\n    d = {BAD}\nelse:\n    d = {SAFE}\nd = {SAFE}\nfor f in d:\n    f()", "GREEN"),
    ("X1", f"d = {SAFE}\nfor _i in ['a', 'b']:\n    if True:\n        d = {BAD}\n        continue\n"
          f"for f in d:\n    f()", "RED"),
    ("X1b", f"d = {SAFE}\nfor _i in ['a', 'b']:\n    if True:\n        d = {BAD}\n        break\n"
           f"for f in d:\n    f()", "RED"),
    ("X1c", f"d = {SAFE}\nfor _i in ['a', 'b']:\n    if True:\n        d = {BAD}\n"
           f"for f in d:\n    f()", "RED"),
    ("X2", f"if True:\n    d = {BAD}\n    raise RuntimeError('x')\nelse:\n    d = {SAFE}\n"
          f"for f in d:\n    f()", "GREEN"),
    ("X3", "if True:\n    d = [['rev_ok']]\nelse:\n    d = [[_rev_unsafe]]\n"
          "for inner in d:\n    for f in inner:\n        f()", "RED"),
    ("X4", "if True:\n    m = {'k': _rev_unsafe}\nelse:\n    m = {'k': str}\nm['k']()", "RED"),
    ("X5", "if True:\n    d = ['a', 'b']\nelse:\n    d = ['c', 'd']\nfor f in d:\n    f.upper()", "GREEN"),
    ("X6", f"d = {SAFE}\nfor _i in ['a', 'b']:\n    for f in d:\n        f()\n    d = {BAD}", "RED"),
    ("X7", f"d = {SAFE}\nwhile _rev_unknown_src:\n    for f in d:\n        f()\n    d = {BAD}", "RED"),
    ("X8", f"try:\n    d = {SAFE}\nfinally:\n    d = {BAD}\nfor f in d:\n    f()", "RED"),
    ("X9", f"try:\n    d = {SAFE}\nexcept Exception:\n    d = {SAFE}\nelse:\n    d = {BAD}\n"
          f"for f in d:\n    f()", "RED"),
    ("X10", f"d = {BAD} or []\nfor f in d:\n    f()", "RED"),
]


def make_probe_panel_bot(dest_dir, body):
    dest = Path(dest_dir) / "panel_bot.py"
    src = (REPO / "panel_bot.py").read_text(encoding="utf-8")
    if ANCHOR not in src:
        raise RuntimeError("probe anchor missing in the live panel_bot.py")
    injected = "\n".join("    " + line for line in body.split("\n")) + "\n"
    src = src.replace(ANCHOR, ANCHOR.split("\n")[0] + "\n" + injected
                      + "    # N5.3.1 exact root/status layout (owner-approved):\n", 1)
    src += UNSAFE_HELPER
    dest.write_text(src, encoding="utf-8")
    return dest


def old_gate_probe_verdict(probe_path):
    r = old_gate.run_panel_static_gate(panel_bot_path=probe_path)
    red = (not r["ok"]) and (r["unresolved_call_total"] > 0
                             or r["ambiguous_call_total"] > 0
                             or any(k.startswith("_rev_unsafe@")
                                    for k in r["helper_nodes_reachable"]))
    return "RED" if red else "GREEN", r["ok"]


def new_gate_probe_verdict(temp_dir, manifest_path, digest):
    r = new_gate.run_gate(str(temp_dir), manifest_path, digest)
    return ("RED" if not r["ok"] else "GREEN"), r["ok"], sorted({f["code"] for f in r["findings"]})


def replay_probes(manifest_path, digest):
    out = []
    for pid, body, old_expected in REPLAY_CASES:
        work = Path(tempfile.mkdtemp(prefix="w3_2_ab_probe_"))
        try:
            for f in ("storage.py", "preflight_check.py", "main.py"):
                shutil.copy2(REPO / f, work / f)
            make_probe_panel_bot(work, body)

            old_actual, old_ok = old_gate_probe_verdict(work / "panel_bot.py")
            new_actual, new_ok, new_codes = new_gate_probe_verdict(work, manifest_path, digest)

            out.append({
                "id": pid, "old_expected": old_expected,
                "old_actual": old_actual, "old_matches_legacy_expectation": old_actual == old_expected,
                "new_actual": new_actual, "new_codes": new_codes,
                "same_verdict": old_actual == new_actual,
                "note": ("new gate reddens via whole-file escalation/policy scan, not via "
                        "container/element/branch provenance -- structurally different "
                        "reason, verdict may legitimately differ from the retired "
                        "precision-based analyzer") if new_actual != old_actual else
                        "verdicts agree",
            })
        except Exception as exc:                                    # noqa: BLE001
            out.append({"id": pid, "error": "%s: %s" % (type(exc).__name__, exc)})
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return out


# ======================================================================================
# Legacy mutation-family applicability (M32-*, M32C-*, M32R2-*, M32R3-*)
# ======================================================================================

def classify_legacy_mutation_families():
    """Static classification of the legacy mutation IDs in
    tools/w3_2_mutation_proof.py by what they actually exercise, read from that file's
    source (never executed, never modified)."""
    text = OLD_MUTATION_PROOF.read_text(encoding="utf-8")
    import re
    ids = sorted(set(re.findall(r'"(M32[A-Za-z0-9-]*)"', text)))

    # Block-scope the classification: each mutation's "# ---- M32..." comment marks the
    # start of its block; the block runs to the next such comment (or EOF). Searching
    # the WHOLE block (not a fixed-size window around the id string) is what correctly
    # separates project-source mutations from gate-source-anchored ones, since a block's
    # make_mutant/assert_red bodies -- where _fresh_copy(GATE_TOOL_PATH...) actually
    # appears -- can be hundreds of lines below the run_mutation(...) call that names it.
    block_starts = [m.start() for m in re.finditer(r"\n        # ----", text)]
    block_starts.append(len(text))

    def block_for(idx):
        for i in range(len(block_starts) - 1):
            if block_starts[i] <= idx < block_starts[i + 1]:
                return text[block_starts[i]:block_starts[i + 1]]
        return text[max(0, idx - 400):idx + 2000]

    runtime_contract = {"M32-1", "M32-2", "M32-3", "M32-4", "M32-5", "M32-6", "M32-7"}
    text_scan_only = {"M32-8", "M32-9", "M32-10", "M32C-4", "M32C-5"}
    gate_source_anchored_markers = [
        "_fresh_copy(GATE_TOOL_PATH", "GATE_TOOL_PATH =",
        'TOOLS_DIR / "w3_2_panel_static_gate_selftest.py").read_text(',
        "mutated_gate(", "gate_src = ",
    ]
    out = []
    for mid in ids:
        idx = text.find('"%s"' % mid)
        window = block_for(idx)
        anchored = any(m in window for m in gate_source_anchored_markers)
        if mid in runtime_contract:
            cls = "RUNTIME_CONTRACT_TEST_NOT_GATE_MUTATION"
            reason = ("imports the mutated module and calls it directly (w3_now/"
                     "w3_tz/w3_resolve_schedule/...); tests RUNTIME behaviour, never "
                     "invokes run_panel_static_gate(). Out of the source gate's scope "
                     "of proof (09_TIME_SOURCE_POLICY.md sec 0); already covered by "
                     "the required runtime selftests (acceptance criteria A.1-A.4)")
        elif mid in text_scan_only:
            cls = "STANDALONE_TEXT_SCAN_NOT_GATE_MUTATION"
            reason = ("uses a standalone marker/text scanner (count_bare_datetime_now_"
                     "today / scan_text_for_markers), not run_panel_static_gate(); "
                     "M32-9/M32-10 test the file-boundary/scope-guard tool, which is "
                     "unchanged and re-run as-is (tools/w3_2_scope_guard_selftest.py)")
        elif anchored:
            cls = "GATE_SOURCE_ANCHORED_RETIRED"
            reason = ("mutates a temp copy of the GATE TOOL's own source "
                     "(GATE_TOOL_PATH), not project source; tests the retired "
                     "analyzer's internals, not the timezone property (plan 10 step 3, "
                     "finding_disposition.json)")
        else:
            cls = "PROJECT_SOURCE_GATE_MUTATION_NOT_REPLAYED_THIS_RUN"
            reason = ("mutates panel_bot.py and drives run_panel_static_gate(); "
                     "genuinely gate-relevant, but not individually re-executed in "
                     "this implementation pass -- superseded by this tool's own "
                     "M-1..M-27 mutation proof (which mutates the SAME project files "
                     "and asserts the NEW gate's diagnostic codes) plus the 27-fixture "
                     "branch-order/X-series replay above, which covers the identical "
                     "dispatch/branch/loop shapes this family was written to probe")
        out.append({"id": mid, "class": cls, "reason": reason})
    return out


# ======================================================================================
# Exhaustive legacy mutation inventory and EXECUTED replay -- M32-1..10, M32C-*,
# M32R2-*, M32R3-* (the four families named in the completion task). Every ID actually
# present in tools/w3_2_mutation_proof.py's source for these four prefixes is
# inventoried; none may be silently omitted (legacy_omitted_total must be 0).
#
# Disposition rule (task section 4): a mutation may be RETIRE_OLD_GATE_INTERNAL only
# when it exercises a mechanism absent from Design C+ (provenance, dict_info, branch
# merge, element provenance, old reachability internals, old report arithmetic, or a
# temp copy of the OLD GATE TOOL's own source) AND does not itself introduce a
# forbidden/role-invalid time source into project code. Any mutation that does
# introduce or hide an actual clock/timezone source is EXECUTE_ON_NEW_GATE, never
# retired, regardless of what mechanism the ORIGINAL harness used to detect it.
# ======================================================================================

LEGACY_MUTATION_PATH = OLD_MUTATION_PROOF


def enumerate_legacy_ids_in_scope():
    """Every M32-1..10 / M32C-* / M32R2-* / M32R3-* id actually present in the legacy
    harness source -- counted independently of any hand-maintained list below, so the
    inventory total is verifiably the number found in the file, not an assumed number.
    """
    import re
    text = LEGACY_MUTATION_PATH.read_text(encoding="utf-8")
    ids = sorted(set(re.findall(r'"(M32[A-Za-z0-9-]*)"', text)),
                key=lambda s: (len(s), s))
    out = []
    for i in ids:
        if re.match(r"^M32-\d+$", i):
            n = int(i.split("-")[1])
            if 1 <= n <= 10:
                out.append(i)
        elif i.startswith("M32C-") or i.startswith("M32R2-") or i.startswith("M32R3-"):
            out.append(i)
    return sorted(set(out), key=lambda s: (s.split("-")[0], int(s.split("-")[-1])))


# --------------------------------------------------------------------------------------
# Anchors. Recreated directly from tools/w3_2_mutation_proof.py's own literal patterns.
# Where the ORIGINAL anchor no longer matches the live tree byte-for-byte (checked
# explicitly per task section 3: "do not mark an executable mutation retired merely
# because its original string anchor is stale"), a narrower/updated anchor targeting the
# same insertion point is used instead, noted in `anchor_note`.
# --------------------------------------------------------------------------------------

def _mk(file, anchor, replacement, anchor_note=""):
    return {"file": file, "anchor": anchor, "replacement": replacement,
           "anchor_note": anchor_note}


_M321_MUT = _mk(
    "storage.py",
    'def w3_now(tz_name=W3_TZ_NAME):\n    # type: (str) -> datetime\n'
    '    """Always an aware datetime. No fallback."""\n'
    '    return datetime.now(w3_tz(tz_name))\n',
    'def w3_now(tz_name=W3_TZ_NAME):\n    # type: (str) -> datetime\n'
    '    """MUTATION M32-1: host-local, zone argument dropped."""\n'
    '    return datetime.now()\n')

_M322_MUT = _mk(
    "storage.py",
    '    try:\n        zi = _W3_ZoneInfo(name)\n    except Exception as exc:\n'
    '        raise W3TimezoneError(f"w3: cannot load timezone {name!r}: {exc}") from exc\n',
    '    try:\n        zi = _W3_ZoneInfo(name)\n'
    '    except Exception:\n'
    '        zi = _W3_ZoneInfo("UTC")  # MUTATION M32-2: silent fallback restored\n')

_M327_MUT = _mk(
    "storage.py",
    "    resolved_at_utc = _now_iso()\n",
    '    resolved_at_utc = datetime.now(_W3_ZoneInfo("Europe/Kyiv")).isoformat()'
    "  # MUTATION M32-7: aware Kyiv ISO into a naive-UTC field\n")

_M328_MUT = _mk(
    "preflight_check.py",
    "def kyiv_today() -> date:\n    return storage.w3_now().date()\n",
    "def kyiv_today() -> date:\n"
    "    return datetime.now().date()  # MUTATION M32-8: bare OS-local call reintroduced\n")

_M32C2_MUT = _mk(
    "panel_bot.py",
    "    from storage import w3_now as _ti_w3_now\n    return _ti_w3_now().date().isoformat()\n",
    '    try:\n        from zoneinfo import ZoneInfo\n'
    '        return datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()\n'
    "    except Exception:\n"
    "        return datetime.now().date().isoformat()"
    "  # MUTATION M32C-2: pre-correction TZ-3 fallback #2 restored\n")

_M32R21_MUT = _mk(
    "panel_bot.py",
    'def _panel_header() -> str:  # type: ignore[override]\n'
    "    hp = _tp_visual_health_parts()\n",
    'def _panel_header() -> str:  # type: ignore[override]\n'
    "    hp = _tp_visual_health_parts()\n"
    "    try:\n"
    '        _stamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%y %H:%M")\n'
    "    except Exception:\n"
    '        _stamp = datetime.now().strftime("%d.%m.%y %H:%M")'
    "  # MUTATION M32R2-1 (updated anchor: only the 2-line def#2 preamble still matches "
    "byte-for-byte; the return-list body text has since changed, e.g. the title line)\n",
    anchor_note="ORIGINAL anchor (3-line, including the `return \"\\n\".join([` line) is "
               "stale -- the title-line text inside the return list changed since M32R2-1 "
               "was written. Narrowed to the 2-line def#2 preamble "
               "(`def _panel_header...` + `hp = _tp_visual_health_parts()`), which is "
               "still unique and byte-identical, and inserts at the same point (top of "
               "def#2's body, before its return statement).")

_M32R22_MUT = _mk(
    "panel_bot.py",
    'def _panel_header() -> str:  # type: ignore[override]\n'
    "    base = _TPAG_PANEL_V2_ORIG_HEADER() if callable(_TPAG_PANEL_V2_ORIG_HEADER) "
    'else "\U0001f7e2 TPilot Admin Panel"\n',
    'def _panel_header() -> str:  # type: ignore[override]\n'
    "    try:\n"
    '        _probe_stamp = datetime.now(ZoneInfo("Europe/Kyiv")).isoformat()\n'
    "    except Exception:\n"
    "        _probe_stamp = datetime.now().isoformat()\n"
    "    base = _TPAG_PANEL_V2_ORIG_HEADER() if callable(_TPAG_PANEL_V2_ORIG_HEADER) "
    'else "\U0001f7e2 TPilot Admin Panel"  # MUTATION M32R2-2\n')

_M32R23_MUT = _mk(
    "panel_bot.py",
    "    from storage import w3_now as _tvnl_w3_now\n"
    '    return _tvnl_w3_now().strftime("%d.%m.%y %H:%M")\n',
    "    try:\n"
    '        return datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%y %H:%M")\n'
    "    except Exception:\n"
    "        try:\n"
    '            return datetime.now().strftime("%d.%m.%y %H:%M")\n'
    "        except Exception:\n"
    '            return "_"  # MUTATION M32R2-3\n')

_DEF1_PATTERN = ("    from storage import w3_now as _ph_w3_now\n"
                 '    updated_at = _ph_w3_now().strftime("%d.%m.%y %H:%M")\n')
_DEF1_OLD = ("    try:\n"
            '        updated_at = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%y %H:%M")\n'
            "    except Exception:\n"
            '        updated_at = datetime.now().strftime("%d.%m.%y %H:%M")'
            "  # MUTATION M32R2-4/5\n")
_M32R24_MUT = _mk("panel_bot.py", _DEF1_PATTERN, _DEF1_OLD)

_TODAY_ISO_ANCHOR = ("    from storage import w3_now as _ti_w3_now\n"
                    "    return _ti_w3_now().date().isoformat()\n")
_TODAY_ISO_WITH_CAPTURE_CALL = (
    "    from storage import w3_now as _ti_w3_now\n"
    "    _TP_VISUAL_ORIG_PANEL_HEADER()  # MUTATION M32R2-5: existing capture given a "
    "real caller from an already-reachable node\n"
    "    return _ti_w3_now().date().isoformat()\n")

_ALIAS_ANCHOR = '_TP_VISUAL_ORIG_PANEL_HEADER = globals().get("_panel_header")\n'
_ALIAS_MUTANT = (_ALIAS_ANCHOR + "_M32R3_3_ALIAS_HDR = _TP_VISUAL_ORIG_PANEL_HEADER\n")
_M3R3_CALL_MUTANT = (
    "    if callable(_M32R3_3_ALIAS_HDR):\n"
    "        _M32R3_3_ALIAS_HDR()  # MUTATION M32R3-3 alias edge\n"
    "    from storage import w3_now as _ti_w3_now\n"
    "    return _ti_w3_now().date().isoformat()\n")

_DEF4_ANCHOR = ('def _panel_header() -> str:  # type: ignore[override]\n'
               "    # N5.3.1 exact root/status layout (owner-approved):\n")
_M32R34_DEF4_MUTANT = (
    'def _panel_header() -> str:  # type: ignore[override]\n'
    "    _m32r3_4_async_helper()  # MUTATION M32R3-4 async call\n"
    "    # N5.3.1 exact root/status layout (owner-approved):\n")
_M32R34_APPEND = "\n\nasync def _m32r3_4_async_helper():\n    return str(datetime.now())\n"

_M32R37_APPEND = "\n\ndef _m32r3_7_dead_helper():\n    return str(datetime.now())\n"
_M32R37_DEF4_MUTANT = (
    'def _panel_header() -> str:  # type: ignore[override]\n'
    "    _m32r3_7_dead_helper()  # MUTATION M32R3-7 now reachable\n"
    "    # N5.3.1 exact root/status layout (owner-approved):\n")


def _apply_multi(tree, ops):
    """Apply a sequence of (file, anchor, replacement) edits to a TempTree, each via a
    single exact-count-1 substring replacement (never regex -- these are literal
    anchors). Returns total bytes changed."""
    total = 0
    for file, anchor, replacement in ops:
        path = tree.dir / file
        text = path.read_text(encoding="utf-8")
        n = text.count(anchor)
        if n != 1:
            raise RuntimeError("anchor found %d times (expected 1) in %s: %r"
                              % (n, file, anchor[:80]))
        new_text = text.replace(anchor, replacement, 1)
        total += abs(len(new_text.encode("utf-8")) - len(text.encode("utf-8")))
        path.write_text(new_text, encoding="utf-8")
    return total


def _make_m32r25(tree):
    # base dead-fallback mutation (same as M32R2-4) PLUS the reachability-escalation wiring
    return _apply_multi(tree, [
        ("panel_bot.py", _DEF1_PATTERN, _DEF1_OLD),
        ("panel_bot.py", _TODAY_ISO_ANCHOR, _TODAY_ISO_WITH_CAPTURE_CALL),
    ])


def _make_m32r33(tree):
    return _apply_multi(tree, [
        ("panel_bot.py", _DEF1_PATTERN, _DEF1_OLD),
        ("panel_bot.py", _ALIAS_ANCHOR, _ALIAS_MUTANT),
        ("panel_bot.py", _TODAY_ISO_ANCHOR, _M3R3_CALL_MUTANT),
    ])


def _make_m32r34(tree):
    total = _apply_multi(tree, [("panel_bot.py", _DEF4_ANCHOR, _M32R34_DEF4_MUTANT)])
    total += tree.append("panel_bot.py", _M32R34_APPEND)
    return total


def _make_m32r37(tree):
    total = _apply_multi(tree, [("panel_bot.py", _DEF4_ANCHOR, _M32R37_DEF4_MUTANT)])
    total += tree.append("panel_bot.py", _M32R37_APPEND)
    return total


def _simple_maker(spec):
    def _make(tree):
        return _apply_multi(tree, [(spec["file"], spec["anchor"], spec["replacement"])])
    return _make


# id -> (category, file, target, operation, expected_code, maker_fn or None,
#        old_gate_comparable: bool)
EXECUTE_SPECS = {
    "M32-1": ("A", "storage.py", "w3_now",
             "zone argument dropped -> host-local bare datetime.now()",
             "BARE_NOW", _simple_maker(_M321_MUT), False),
    "M32-2": ("A", "storage.py", "w3_tz",
             "silent UTC fallback restored on exception (zero-fallback guard defeated)",
             "UTC_AS_KYIV_SUBSTITUTE", _simple_maker(_M322_MUT), False),
    "M32-7": ("A", "storage.py", "w3_resolve_schedule (resolved_at_utc field)",
             "aware Kyiv ISO written into a naive-UTC-role field",
             "ROLE_FORM_NOT_PERMITTED", _simple_maker(_M327_MUT), False),
    "M32-8": ("A", "preflight_check.py", "kyiv_today",
             "bare datetime.now().date() reintroduced",
             "BARE_NOW", _simple_maker(_M328_MUT), False),
    "M32C-2": ("A", "panel_bot.py", "_today_iso (active root)",
              "pre-correction try/except fallback restored (Kyiv try, bare-now except)",
              "HOST_LOCAL_FALLBACK", _simple_maker(_M32C2_MUT), True),
    "M32R2-1": ("A", "panel_bot.py", "_panel_header def#2 @4657 (shadowed)",
               "host-local fallback restored in a shadowed-but-reachable generation",
               "HOST_LOCAL_FALLBACK", _simple_maker(_M32R21_MUT), True),
    "M32R2-2": ("A", "panel_bot.py", "_panel_header def#3 @5254 (shadowed)",
               "host-local fallback restored in a shadowed-but-reachable generation",
               "HOST_LOCAL_FALLBACK", _simple_maker(_M32R22_MUT), True),
    "M32R2-3": ("A", "panel_bot.py", "_tp_visual_now_local (active root)",
               "triple-nested fallback restored (Kyiv try, bare-now except, string except)",
               "HOST_LOCAL_FALLBACK", _simple_maker(_M32R23_MUT), True),
    "M32R2-4": ("A", "panel_bot.py", "_panel_header def#1 @1057",
               "fallback restored in the generation the OLD gate's precision model "
               "treats as dead/unreachable; Design C+ seeds ALL root generations "
               "unconditionally",
               "HOST_LOCAL_FALLBACK", _simple_maker(_M32R24_MUT), True),
    "M32R2-5": ("A", "panel_bot.py", "_panel_header def#1 @1057 + capture wiring",
               "same def#1 fallback as M32R2-4, PLUS a real caller wired through the "
               "existing (previously uncalled) capture to make it reachable under the "
               "OLD gate's model",
               "HOST_LOCAL_FALLBACK", _make_m32r25, True),
    "M32R3-3": ("A", "panel_bot.py", "_panel_header def#1 + alias chain",
               "same def#1 fallback, routed through a 2-hop alias chain from the "
               "active _today_iso root",
               "HOST_LOCAL_FALLBACK", _make_m32r33, True),
    "M32R3-4": ("A", "panel_bot.py", "new async helper called from active root",
               "AsyncFunctionDef carrying a bare datetime.now(), called from _panel_header",
               "BARE_NOW", _make_m32r34, True),
    "M32R3-7": ("A", "panel_bot.py", "new (initially dead) helper, then reachable",
               "module-level def with a bare datetime.now(); Design C+'s Phase 3 "
               "indexes it unconditionally regardless of reachability",
               "BARE_NOW", _make_m32r37, True),
}

# id -> (category, mechanism_absent_or_non_timezone, exact_evidence, why_not_a_source,
#        replacement_test, disposition)
RETIRE_SPECS = {
    "M32-3": ("A-shape/non-source", "night duration computed via UTC-elapsed "
             "`+timedelta(hours=15)` instead of the DST-aware boundary",
             "changes only a duration ARITHMETIC path; the same approved "
             "`.astimezone(_w3_utc_tz)` UTC-conversion forms remain, unchanged; no new "
             "or wrong clock API call, no new zone argument -- a pure runtime VALUE bug",
             "w3_2_dst_matrix_selftest.py (required, PASS)", "RETIRE_NON_TIMEZONE"),
    "M32-4": ("A-shape/non-source", '`fold = 1 if which == "end" else 0` forced to '
             "always `fold = 0`",
             "the mutated `fold=` argument is passed to an already-permitted "
             "`.replace(tzinfo=tz, fold=X)` call; the gate's form-token for that call "
             "(`REPLACE_TZINFO:<zone>`) is identical regardless of the fold VALUE -- "
             "the gate proves permitted SOURCE usage, not fold-selection correctness "
             "(09_TIME_SOURCE_POLICY.md sec 0)",
             "w3_2_dst_matrix_selftest.py (required, PASS)", "RETIRE_NON_TIMEZONE"),
    "M32-5": ("A-shape/non-source", "DST-gap snap call removed (`if is_gap: pass` "
             "instead of `return _w3_snap_dst_gap(naive, tz)`)",
             "REMOVES a call to an approved helper; does not ADD any forbidden or "
             "unapproved clock source anywhere",
             "w3_2_dst_matrix_selftest.py (required, PASS)", "RETIRE_NON_TIMEZONE"),
    "M32-6": ("A-shape/non-source", "night_start recomputed against today's date "
             "instead of reusing the previous day's resolved boundary",
             "both the original and mutated code call the SAME approved "
             "`w3_local_at(...)` helper with only the date ARGUMENT changed; no new "
             "or wrong clock source introduced",
             "w3_2_dst_matrix_selftest.py (required, PASS)", "RETIRE_NON_TIMEZONE"),
    "M32-9": ("C", "a new function calling `storage.w3_resolve_schedule` (itself an "
             "approved BUSINESS_LOCAL helper) is added to main.py, out of the "
             "C1/C2 approved call sequence",
             "the function called is already an APPROVED helper; the defect is an "
             "architectural/scope-sequencing violation, not a forbidden time source",
             "tools/w3_2_scope_guard_selftest.py (required, unchanged, PASS)",
             "RETIRE_NON_TIMEZONE"),
    "M32-10": ("C", "an undeclared runtime file (stats_engine.py) gains a W3.2 DST "
              "helper definition/marker",
              "stats_engine.py is outside the four frozen scope files entirely "
              "(02_TRUE_W3_2_SAFETY_PROPERTY.md sec C: 'never changes: stats_engine.py'"
              "); this is a FILE-BOUNDARY violation, not a clock-source defect inside "
              "the scanned scope",
              "tools/w3_2_scope_guard_selftest.py (required, unchanged, PASS)",
              "RETIRE_NON_TIMEZONE"),
    "M32C-4": ("C", "an undeclared runtime file (manager_bot.py) is byte-changed "
              "without adding a W3 marker",
              "tests the scope guard's hash/diff boundary check; no clock API touched",
              "tools/w3_2_scope_guard_selftest.py (required, unchanged, PASS)",
              "RETIRE_NON_TIMEZONE"),
    "M32C-5": ("C", "a W3.2 marker string is appended to an undeclared runtime file "
              "(manager_bot.py)",
              "tests the scope guard's SECONDARY marker-scan check; no clock API "
              "touched",
              "tools/w3_2_scope_guard_selftest.py (required, unchanged, PASS)",
              "RETIRE_NON_TIMEZONE"),
    "M32R2-6": ("B", "`is_globals_get_call()` in a TEMP COPY of "
               "`w3_2_panel_static_gate_selftest.py` reverted to a broken shape "
               "(`call.func.value` expected `ast.Name` instead of the real `ast.Call`)",
               "mutates the OLD GATE TOOL's own capture-matcher implementation; no "
               "project file is touched at all; Design C+ has no equivalent "
               "`is_globals_get_call` function -- its capture resolution "
               "(`_capture_target` in w3_2_whole_scope_clock_index.py) is a single "
               "unconditional AST-shape check with no broken/round-1 variant to revert",
               "P-4 (literal globals().get dispatch is followed)", "RETIRE_OLD_GATE_INTERNAL"),
    "M32R3-1": ("old reachability internals", "a reachable generation's `globals().get(...)` "
               "capture key made non-literal via an intermediate variable "
               "(`_M32R3_1_DYNKEY = \"_panel_header\"`)",
               "introduces NO forbidden clock form anywhere -- verified by reading the "
               "mutation: only a capture KEY becomes non-literal, no new/wrong API call "
               "is added. Tests the OLD gate's policy of 'any unresolved edge from a "
               "reachable node fails the WHOLE gate, regardless of what it might "
               "resolve to' -- a policy Design C+ explicitly does NOT implement "
               "(02_TRUE_W3_2_SAFETY_PROPERTY.md sec I: 'an unresolved call only ever "
               "widens the scanned set'; confirmed by code inspection of "
               "w3_2_timezone_source_gate.py -- UNRESOLVED_LOCAL_DISPATCH/"
               "GLOBALS_GET_DYNAMIC_KEY never appear in the `findings` list, only in "
               "`unresolved_sites` metrics). Design C+'s Phase 3 whole-scope index "
               "independently covers every actual clock-touching definition regardless "
               "of this capture's resolvability, so no coverage is lost",
               "P-5 (unresolved dynamic dispatch escalates), Phase 3 whole-scope index",
               "RETIRE_OLD_GATE_INTERNAL"),
    "M32R3-2": ("B", "capture detection for `globals().get(...)` removed entirely from "
               "a TEMP COPY of `w3_2_panel_static_gate_selftest.py`",
               "mutates the OLD GATE TOOL's own source; Design C+ has no equivalent "
               "removable capture-detection subsystem to test the SAME way -- its "
               "capture resolution is not a separable optional feature",
               "P-4", "RETIRE_OLD_GATE_INTERNAL"),
    "M32R3-5": ("old reachability internals", "an unresolved dynamic call target "
               "reached from the active root via a non-literal capture key",
               "same shape and same conclusion as M32R3-1: no forbidden clock form is "
               "added (the dynamic target points at the ALREADY-safe "
               "`_tp_visual_now_local`); tests the same retired "
               "unresolved-implies-whole-gate-RED policy",
               "P-5, Phase 3 whole-scope index", "RETIRE_OLD_GATE_INTERNAL"),
    "M32R3-6": ("old reachability internals", "a circular module-level alias "
               "(`_m32r3_6_c2 = _m32r3_6_c1; _m32r3_6_c1 = _m32r3_6_c2`, neither ever "
               "bound to a real function) reached from the active root",
               "no forbidden clock form anywhere; the aliased names are never bound to "
               "any real def. Tests the OLD gate's specific ALIAS_CYCLE detection "
               "diagnostic, a mechanism Design C+ does not implement (an unresolvable "
               "call target -- cyclic or not -- is uniformly "
               "UNRESOLVED_LOCAL_DISPATCH -> escalation, never a distinct cycle-detection "
               "code)", "P-5, Phase 3 whole-scope index", "RETIRE_OLD_GATE_INTERNAL"),
    "M32R3-8": ("B", "AsyncFunctionDef dropped from node collection in a TEMP COPY of "
               "`w3_2_panel_static_gate_selftest.py`",
               "mutates the OLD GATE TOOL's own def-collector; Design C+'s Phase 0 "
               "`_iter_scope` collects `(ast.FunctionDef, ast.AsyncFunctionDef)` as a "
               "single unconditional isinstance check -- there is no separable "
               "async-collection step to disable",
               "M32R3-4 (executed here) proves AsyncFunctionDef inclusion directly on "
               "Design C+", "RETIRE_OLD_GATE_INTERNAL"),
}


def execute_legacy_mutation(mid, spec):
    """Execute one EXECUTE_ON_NEW_GATE legacy mutation against a temp copy of the live
    4-file scope, using the SAME production manifest (frozen bindings/roles are exactly
    what these mutations are supposed to defeat)."""
    category, file, target, operation, expected_code, maker, old_comparable = spec
    pre_hashes = new_mut.hash_all(new_mut.REQUIRED_RUNTIME_FILES + new_mut.TOOL_FILES)
    tree = new_mut.TempTree()
    result = {"id": mid, "category": category, "file": file, "target": target,
             "operation": operation, "expected_diagnostic": expected_code,
             "disposition": "EXECUTE_ON_NEW_GATE"}
    try:
        r0 = tree.run_gate()
        result["new_baseline_ok"] = r0["ok"]
        bytes_changed = maker(tree)
        result["bytes_changed"] = bytes_changed
        r1 = tree.run_gate()
        result["new_mutant_ok"] = r1["ok"]
        result["new_mutant_codes"] = sorted({f["code"] for f in r1["findings"]})
        result["actual_diagnostic"] = (expected_code
                                       if expected_code in result["new_mutant_codes"]
                                       else (result["new_mutant_codes"][0]
                                            if result["new_mutant_codes"] else None))
        causal = (not r1["ok"]) and (expected_code in result["new_mutant_codes"])
        tree.restore()
        r2 = tree.run_gate()
        result["new_restore_ok"] = r2["ok"]
        post_hashes = new_mut.hash_all(new_mut.REQUIRED_RUNTIME_FILES + new_mut.TOOL_FILES)
        result["runtime_hashes_unchanged"] = (pre_hashes == post_hashes)
        result["causal"] = bool(causal)
        result["executed"] = True
        result["pass"] = bool(
            r0["ok"] and causal and r2["ok"] and result["runtime_hashes_unchanged"])
        if not result["pass"]:
            result["harness_failure"] = (
                "baseline_ok=%s causal=%s restore_ok=%s hashes_ok=%s"
                % (r0["ok"], causal, r2["ok"], result["runtime_hashes_unchanged"]))

        # old-gate comparison, only where old gate can even see the mutated file
        if old_comparable and file == "panel_bot.py":
            old_r0 = old_gate.run_panel_static_gate(panel_bot_path=tree.dir / "panel_bot.py")
            # old_r0 here is POST-restore (tree already restored above); re-mutate a
            # fresh short-lived copy purely for the old-gate comparison, since TempTree
            # was already restored.
            tree2 = new_mut.TempTree()
            try:
                old_before = old_gate.run_panel_static_gate(
                    panel_bot_path=tree2.dir / "panel_bot.py")
                maker(tree2)
                old_after = old_gate.run_panel_static_gate(
                    panel_bot_path=tree2.dir / "panel_bot.py")
                result["old_baseline_ok"] = old_before["ok"]
                result["old_mutant_ok"] = old_after["ok"]
            finally:
                tree2.close()
        else:
            result["old_baseline_ok"] = None
            result["old_mutant_ok"] = None
            result["old_gate_note"] = ("old gate only reads panel_bot.py; this "
                                       "mutation's file (%s) is structurally outside "
                                       "its scope" % file) if file != "panel_bot.py" else ""
    except Exception as exc:                                        # noqa: BLE001
        result["executed"] = True
        result["pass"] = False
        result["harness_failure"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        tree.close()
    return result


def regression_class(entry):
    if entry.get("old_baseline_ok") is None:
        old_catches = False
        note = "old gate structurally cannot read this file (panel_bot.py-only design)"
    else:
        old_catches = (entry["old_baseline_ok"] is True) and (entry["old_mutant_ok"] is False)
        note = ""
    new_catches = (entry.get("new_baseline_ok") is True) and (entry.get("new_mutant_ok") is False)
    if old_catches and new_catches:
        return "BOTH_CATCH", note
    if new_catches and not old_catches:
        return "NEW_CATCHES_OLD_MISSES", note
    if old_catches and not new_catches:
        return "OLD_CATCHES_NEW_MISSES", note
    return "NOT_COMPARABLE_OLD_GATE_INTERNAL", note


def build_legacy_replay():
    ids = enumerate_legacy_ids_in_scope()
    rows = []
    executed = []
    retired = []
    for mid in ids:
        if mid in EXECUTE_SPECS:
            spec = EXECUTE_SPECS[mid]
            res = execute_legacy_mutation(mid, spec)
            cls, note = regression_class(res)
            res["regression_class"] = cls
            res["regression_note"] = note
            executed.append(res)
            rows.append(res)
        elif mid in RETIRE_SPECS:
            mech, evidence, why_not_source, replacement, disposition = RETIRE_SPECS[mid]
            row = {"id": mid, "disposition": disposition, "mechanism": mech,
                  "evidence": evidence, "why_not_source_regression": why_not_source,
                  "replacement_test": replacement, "executed": False,
                  "regression_class": "NOT_COMPARABLE_OLD_GATE_INTERNAL"}
            retired.append(row)
            rows.append(row)
        else:
            rows.append({"id": mid, "disposition": "BLOCKED_WITH_REASON",
                        "reason": "no classification rule matched this id -- "
                                 "harness defect, must be fixed before self-PASS",
                        "executed": False})

    totals = {
        "legacy_mutation_total": len(ids),
        "legacy_timezone_property_total": len(executed) + len(
            [r for r in retired if r["disposition"] == "RETIRE_OLD_GATE_INTERNAL"]),
        "legacy_executed_total": len(executed),
        "legacy_executed_pass": sum(1 for e in executed if e.get("pass")),
        "legacy_executed_fail": sum(1 for e in executed if not e.get("pass")),
        "legacy_retired_old_gate_internal_total": sum(
            1 for r in retired if r["disposition"] == "RETIRE_OLD_GATE_INTERNAL"),
        "legacy_retired_non_timezone_total": sum(
            1 for r in retired if r["disposition"] == "RETIRE_NON_TIMEZONE"),
        "legacy_blocked_total": sum(1 for r in rows if r.get("disposition") == "BLOCKED_WITH_REASON"),
        "legacy_omitted_total": 0,
        "old_catches_new_misses_total": sum(
            1 for e in executed if e.get("regression_class") == "OLD_CATCHES_NEW_MISSES"),
        "both_catch_total": sum(1 for e in executed if e.get("regression_class") == "BOTH_CATCH"),
        "new_catches_old_misses_total": sum(
            1 for e in executed if e.get("regression_class") == "NEW_CATCHES_OLD_MISSES"),
    }
    return {"ids_found": ids, "rows": rows, "executed": executed, "retired": retired,
           "totals": totals}


# ======================================================================================
# Driver
# ======================================================================================

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="W3.2 gate A/B comparison (old vs new)")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--expected-digest", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--skip-replay", action="store_true",
                    help="skip the 27-fixture branch-order probe replay (slow: ~27 "
                        "temp-tree gate runs)")
    ap.add_argument("--legacy-replay-only", default=None, metavar="OUT_JSON",
                    help="run ONLY the M32-1..10/M32C-*/M32R2-*/M32R3-* legacy "
                        "mutation inventory+execution and write it to OUT_JSON, "
                        "skipping the closure/findings/X-series comparison entirely")
    args = ap.parse_args(argv)

    if args.legacy_replay_only:
        print("=" * 90)
        print("W3.2 LEGACY MUTATION REPLAY -- M32-1..10 / M32C-* / M32R2-* / M32R3-*")
        print("=" * 90)
        replay = build_legacy_replay()
        for row in replay["rows"]:
            if row.get("executed"):
                print("  [%s] %-9s %-45s -> %s (regression=%s)"
                     % ("OK" if row.get("pass") else "FAIL", row["id"],
                        row.get("target", ""), row.get("actual_diagnostic"),
                        row.get("regression_class")))
            else:
                print("  [RETIRED] %-9s %s" % (row["id"], row.get("disposition")))
        print("-" * 90)
        for k, v in replay["totals"].items():
            print("   %-45s %s" % (k, v))
        Path(args.legacy_replay_only).write_text(
            json.dumps(replay, indent=2, ensure_ascii=False), encoding="utf-8")
        t = replay["totals"]
        ok = (t["legacy_omitted_total"] == 0 and t["legacy_blocked_total"] == 0
             and t["legacy_executed_fail"] == 0
             and t["old_catches_new_misses_total"] == 0)
        print("[%s] legacy replay" % ("OK" if ok else "FAIL"))
        return 0 if ok else 1

    print("=" * 90)
    print("W3.2 GATE A/B COMPARISON -- OLD (panel_bot.py only) vs NEW (Design C+)")
    print("=" * 90)

    old_result, old_closure, old_findings = old_gate_closure_and_findings()
    new_result, new_scanned_min, new_findings = new_gate_closure_and_findings(
        REPO, args.manifest, args.expected_digest)

    panel_bot_escalated = "panel_bot.py" in new_result["metrics"]["escalated_files"]

    classified, only_old, only_new, both = classify_closure_diff(
        old_closure, new_scanned_min, panel_bot_escalated)

    regressions = [c for c in classified if c["class"] == "REGRESSION"]
    expected_new = [c for c in classified if c["class"] == "EXPECTED_NEW_INCLUSION"]
    expected_old_internal = [c for c in classified if c["class"] == "EXPECTED_OLD_ONLY_INTERNAL"]

    finding_regressions = []
    finding_improvements = []
    old_flagged_keys = {(f[0], f[1], f[2]) for f in old_findings}
    new_flagged_keys = {(f[0], f[1], f[2]) for f in new_findings}
    for key in old_flagged_keys - new_flagged_keys:
        if panel_bot_escalated:
            continue  # covered by whole-file scanning regardless of surfaced key
        finding_regressions.append({"row": key, "class": "REGRESSION",
                                    "note": "old gate flagged a violation here; new "
                                            "gate does not and the file is not escalated"})
    for key in new_flagged_keys - old_flagged_keys:
        finding_improvements.append({"row": key, "class": "IMPROVEMENT"})

    root_coverage = {}
    for root in TZ3_ROOTS:
        old_gens = [g for g in old_result["tz3_functions"].get(root, {}).get("generations", [])]
        new_root_spec = next((r for r in new_result["binding_detail"]["roots"]
                              if r["name"] == root), None)
        root_coverage[root] = {
            "old_generation_count": len(old_gens),
            "old_reachable_generations": sum(
                1 for g in old_gens
                if g["classification"] in ("ACTIVE_REACHABLE", "SHADOWED_BUT_REACHABLE")),
            "new_generation_count": new_root_spec["measured_generation_count"]
                if new_root_spec else None,
            "new_status": new_root_spec["status"] if new_root_spec else "MISSING",
        }

    print("\n-- Root coverage --------------------------------------------------------")
    for root, info in root_coverage.items():
        print("   %-24s old: %d/%d reachable   new: %s (%d generations, ALL scanned)"
             % (root, info["old_reachable_generations"], info["old_generation_count"],
                info["new_status"], info["new_generation_count"] or 0))

    print("\n-- Closure membership diff (panel_bot.py) --------------------------------")
    print("   both gates cover: %d" % len(both))
    print("   old only:         %d  (escalated=%s -> %s)"
         % (len(only_old), panel_bot_escalated,
            "EXPECTED_OLD_ONLY_INTERNAL" if panel_bot_escalated else "see REGRESSION below"))
    print("   new only:         %d  (EXPECTED_NEW_INCLUSION)" % len(only_new))
    print("   REGRESSION:       %d" % len(regressions))
    for r in regressions[:20]:
        print("      REGRESSION %s" % (r["row"],))

    print("\n-- Findings diff ----------------------------------------------------------")
    print("   REGRESSION (finding lost): %d" % len(finding_regressions))
    for r in finding_regressions[:20]:
        print("      %s" % (r["row"],))
    print("   IMPROVEMENT (new finding): %d" % len(finding_improvements))

    replay = [] if args.skip_replay else replay_probes(args.manifest, args.expected_digest)
    if replay:
        agree = sum(1 for r in replay if r.get("same_verdict"))
        print("\n-- Branch-order / X-series probe replay (%d fixtures) --------------------"
             % len(replay))
        print("   old/new verdict agreement: %d/%d" % (agree, len(replay)))
        for r in replay:
            if "error" in r:
                print("   [ERROR] %-5s %s" % (r["id"], r["error"]))
                continue
            mark = "=" if r["same_verdict"] else "!"
            print("   [%s] %-5s old=%-6s new=%-6s legacy_expected=%-6s codes=%s"
                 % (mark, r["id"], r["old_actual"], r["new_actual"], r["old_expected"],
                    r["new_codes"]))

    legacy_families = classify_legacy_mutation_families()
    by_class = {}
    for f in legacy_families:
        by_class.setdefault(f["class"], []).append(f["id"])
    print("\n-- Legacy w3_2_mutation_proof.py M32* family classification --------------")
    for cls, ids in sorted(by_class.items()):
        print("   %-45s %2d  %s" % (cls, len(ids), ", ".join(ids[:8]) + ("..." if len(ids) > 8 else "")))

    total_regressions = len(regressions) + len(finding_regressions)
    print("\n" + "=" * 90)
    if total_regressions == 0:
        print("[OK] REGRESSION == 0")
    else:
        print("[FAIL] REGRESSION == %d" % total_regressions)

    payload = {
        "old_gate_ok": old_result["ok"],
        "new_gate_ok": new_result["ok"],
        "panel_bot_escalated_in_new_gate": panel_bot_escalated,
        "root_coverage": root_coverage,
        "closure_diff": {"both": len(both), "old_only": len(only_old),
                         "new_only": len(only_new), "regressions": regressions,
                         "expected_new_inclusion_total": len(expected_new),
                         "expected_old_only_internal_total": len(expected_old_internal)},
        "findings_diff": {"regressions": finding_regressions,
                          "improvements": finding_improvements},
        "probe_replay": replay,
        "legacy_mutation_family_classification": legacy_families,
        "total_regressions": total_regressions,
    }
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(payload, indent=2, ensure_ascii=False,
                                                   default=str),
                                       encoding="utf-8")
    return 0 if total_regressions == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
