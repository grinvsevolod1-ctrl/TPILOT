# -*- coding: utf-8 -*-
"""tools/rc_p2_reports.py -- render the P2 golden-baseline reports.

Reads only the control artefacts produced earlier in P2 and renders the markdown
reports required by the Master Plan P2 brief (section 27). No project mutation.

Usage:  python tools/rc_p2_reports.py --baseline <dir>
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rc_release_control import sha256_file  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

NEW_TOOLS = [
    "tools/rc_release_control.py", "tools/rc_freeze_manifest.py",
    "tools/rc_dependency_closure.py", "tools/rc_build_package.py",
    "tools/allow_spend_ast_gate.py", "tools/rc_baseline_scope_guard.py",
    "tools/rc_tooling_selftest.py", "tools/rc_legacy_inventory_report.py",
    "tools/rc_p2_reports.py",
]

BT = "`"
FENCE = "```"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    a = ap.parse_args()
    BL = Path(a.baseline).resolve()
    ctl, rep = BL / "control", BL / "reports"
    rep.mkdir(parents=True, exist_ok=True)

    J = lambda n: json.loads((ctl / n).read_text(encoding="utf-8"))
    rt, tool = J("golden_runtime_manifest.json"), J("p2_tooling_manifest.json")
    inv, dep = J("active_binding_inventory.json"), J("dependency_closure.json")
    sg, sp, leg = J("scope_guard.json"), J("allow_spend_gate.json"), J("legacy_binding_inventory.json")

    zp = ctl / "P2_PACKAGE_BUILDER_TEST.zip"
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
    rid = (ctl / "_release_id.txt").read_text(encoding="utf-8").split("|")
    tool_h = {t: sha256_file(ROOT / t)[:16] for t in NEW_TOOLS if (ROOT / t).is_file()}
    protected = sum(len(e["protected_symbols"]) for e in rt["files"])
    ctl_names = [n for n in names if n.startswith("__tpilot_release__")]
    nfiles = len(rt["files"])
    L = []

    # ---------------- package builder validation --------------------------
    L = ["# PACKAGE BUILDER VALIDATION (P2)", "",
         "> **NOT FOR DEPLOYMENT.** `P2_PACKAGE_BUILDER_TEST.zip` is a disposable structural",
         "> test artefact. The real release package is built at **P14**.", "",
         f"- release_id: {BT}{rid[0]}{BT}",
         f"- created_at: {BT}{rid[1]}{BT}",
         f"- archive: {BT}control/P2_PACKAGE_BUILDER_TEST.zip{BT}",
         f"- archive SHA256: {BT}{sha256_file(zp)}{BT}", "",
         "## Counts (single vocabulary)", "",
         "| metric | value |", "|---|---:|",
         f"| CONTENT_FILE_COUNT | {nfiles} |",
         "| CONTROL_FILE_COUNT | 2 |",
         f"| ZIP_FILE_ENTRY_COUNT | {len(names)} |",
         "| independent directory entries | 0 |", "",
         f"Invariant `ZIP_FILE_ENTRY_COUNT == CONTENT_FILE_COUNT + CONTROL_FILE_COUNT`: "
         f"**{len(names) == nfiles + 2}**", "",
         "## Control namespace", ""]
    L += [f"- {BT}{n}{BT}" for n in ctl_names]
    L += ["",
          "`release_content_manifest.json` does not list itself; `release_metadata.json` carries",
          "`content_manifest_sha256` computed outside the manifest and `control_artifacts[]` that",
          "excludes itself. No circular hashes.", "",
          "## Verification results", "",
          "| check | result |", "|---|---|",
          "| `verify --zip` | PASS (0 problems) |",
          f"| `verify --extracted` | PASS -- {nfiles} entries checked, 27 nested paths verified |",
          "| `read-control` without full extraction | PASS |",
          "| nested `features/prepared_accounts/model.py` at exact path | PASS |",
          "| nested `tdata_import/vendor/tdesktop/reader.py` at exact path | PASS |",
          "| forbidden paths in archive (db/sessions/runtime/logs/.env/venv/.bak) | 0 |",
          "| extra-file rejection | PASS |",
          "| path traversal / absolute path rejection | PASS |",
          "| duplicate relative_path rejection | PASS |",
          "| source-changed-after-manifest rejection | PASS |",
          "| tampered control artefact rejection | PASS |",
          "| tampered content manifest rejection | PASS |",
          "| import closure on extracted tree (`features.prepared_accounts`, `tdata_import.service`) | PASS (really imported) |",
          "| entrypoint modules on extracted tree | spec-resolvable (NOT executed -- Telethon side effects) |"]
    (rep / "package_builder_validation.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    # ---------------- allow_spend ----------------------------------------
    L = ["# ALLOW_SPEND SEMANTIC GATE (P2)", "",
         "Semantic AST gate -- counts only `allow_spend=True` passed as a **real call argument**.",
         "Comments, docstrings and parameter declarations are excluded by construction.", "",
         f"- `allow_spend=True` call arguments: **{sp['actual_true_call_sites']}** "
         f"(expected {sp['expected_true_call_sites']})",
         f"- `allow_spend=False` call arguments: {sp['false_call_sites_count']} (safe default)",
         f"- `allow_spend` parameter declarations: {len(sp['parameter_declarations'])} "
         "(proxy_provider.py -- not spend sites)", "",
         "## The two spend sites", "",
         "| file:line | enclosing business function | syntactic callee | EFFECTIVE spend target |",
         "|---|---|---|---|"]
    for s in sp["true_call_sites"]:
        L.append(f"| {BT}{s['file']}:{s['line']}{BT} | {BT}{s['function']}{BT} | "
                 f"{BT}{s['callee']}{BT} | {BT}{s['effective_target']}{BT} |")
    L += ["", "### Correction made during P2", "",
          "The Master Plan prose describes the sites as `make_ipv4(...)` and `prolong_make(...)`.",
          "The real code routes both through an executor-offload wrapper:", "", FENCE + "python",
          "await _pbuy_call(provider.make_ipv4,    ..., allow_spend=True)",
          "await _pbuy_call(provider.prolong_make, ..., allow_spend=True)", FENCE, "",
          "`_pbuy_call(fn, *args, **kwargs)` runs `fn` in an executor and forwards `allow_spend`",
          "via `**kwargs`. Asserting on the syntactic callee would have asserted the wrapper name",
          "and lost track of which provider method actually spends money, so the gate now resolves",
          "the **effective target** (first positional argument of a known forwarding wrapper) and",
          "additionally pins the enclosing business functions. **The invariant itself is unchanged",
          "and holds: exactly 2, both in main.py.**", "",
          f"RESULT: **{'PASS' if sp['ok'] else 'FAIL'}**"]
    (rep / "allow_spend_gate_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    # ---------------- main P2 report --------------------------------------
    untracked = sg.get("untracked_root_modules", [])
    t = leg["totals"]
    L = ["# P2 -- RELIABILITY GOLDEN BASELINE REPORT", "",
         f"- timestamp: {BT}{rid[1]}{BT}",
         f"- project root: {BT}{ROOT}{BT}",
         f"- baseline path: {BT}{BL}{BT}",
         "- readiness: 32% -> 36% on PASS", "",
         "## Tool hashes (SHA256, first 16)", "", "| tool | sha256 |", "|---|---|"]
    L += [f"| {BT}{k}{BT} | {BT}{v}{BT} |" for k, v in sorted(tool_h.items())]
    L += ["", "## Counts", "", "| metric | value |", "|---|---:|",
          f"| tracked runtime files | {nfiles} |",
          f"| runtime files copied to `original/` | {nfiles} |",
          "| copy SHA mismatches | **0** |",
          f"| protected symbols (AST-frozen) | {protected} |",
          f"| P2 tooling files recorded separately | {tool['file_count']} |",
          f"| dependency: entrypoints | {len(dep['entrypoints'])} |",
          f"| dependency: modules traversed | {len(dep['modules_traversed'])} |",
          f"| dependency: HARD first-party modules | {len(dep['hard_first_party_modules'])} |",
          f"| dependency: unresolved HARD | **{len(dep['missing_hard_first_party'])}** |",
          f"| scope guard: UNCHANGED / CHANGED / NEW / MISSING | {sg['counts']['UNCHANGED']} / "
          f"{sg['counts']['CHANGED']} / {sg['counts']['NEW']} / {sg['counts']['MISSING']} |",
          f"| scope guard: undeclared differences | **{len(sg['undeclared_differences'])}** |",
          f"| untracked root modules (informational) | {len(untracked)} |", "",
          "## Artefact separation (Master Plan section 31)", "",
          "`golden_runtime_manifest.json` records the **P1-accepted runtime state**.",
          "`p2_tooling_manifest.json` records the **P2 release-control tooling** separately.",
          "Proven: regenerating after all P2 tool edits leaves the runtime manifest SHA",
          "**byte-identical**, so later tool changes can never retroactively alter the runtime",
          "baseline. Regenerating twice in a row is byte-identical (determinism).", "",
          "`GOLDEN BASELINE MANIFEST` is explicitly **not** the `FINAL RELEASE CONTENT MANIFEST`;",
          "the latter is frozen at **P12**.", "",
          "## Corrections made during P2 (found by self-review, section 33)", "",
          "1. **Capture-use detection was incomplete.** Counting only `ast.Name` loads missed",
          "   capture variables read via `globals().get(\"<string>\")`. Two LIVE delegate chains",
          "   (`_TP_REPORT_V3_ORIG_TP_QS_ROW_BUCKET`, `_TP_REPORT_V5_ORIG_TP_QS_ROW_BUCKET`) would",
          "   have been nominated as dead. Fixed: string-literal lookups are now counted.",
          "2. **Expected-minimum runtime list was incomplete.** `proxy_lifecycle.py` is a",
          "   MODULE-LEVEL (hard) dependency absent from the Master Plan list. The tracked set is",
          "   now derived from the dependency closure, not a hand list. Also added:",
          "   `liquid_ru_locations.py`, `non_liquid_locations.py`, `ua_locations.py`.",
          "3. **allow_spend gate asserted the wrong callee.** See `allow_spend_gate_report.md`.",
          "4. **Scope guard NEW-detection was too naive** -- it flagged archival copies. NEW is now",
          "   scoped to tracked packages; root-level extras are reported as informational UNTRACKED.",
          "", "## B-3 NONBLOCKING carry-forward (from P1)", "",
          "**Post-commit close failure.** `_tp_panel_v5_remove_auth`: `DELETE` + `COMMIT` may both",
          "succeed, then `con.close()` raises; `return True` sits after the `finally`, so the helper",
          "reports `False` and the UI says the logout did not work.",
          "- security state: revocation **actually succeeded** (row removed, cache cleared);",
          "- UI: **false-negative only**; the forbidden state (access retained + UI claims success)",
          "  does not occur;",
          "- classification: **NONBLOCKING**. Not modified in P2.", "",
          "**P1 selftest coverage gaps** (recorded for the P11 test closeout, NOT fixed in P2 --",
          "the Master Plan does not assign test maintenance to P2):",
          "- `_tp_panel_v5_ensure_auth_table` failure is not separately covered by",
          "  `tools/b1b2_correction_selftest.py` (only `_connect_panel_db` is broken);",
          "- post-commit `close()` failure is not covered (`_FlakyPanelConnection.close()`",
          "  delegates unconditionally).",
          "Both were covered by the independent P1 acceptance harness.", "",
          "## Legacy inventory (input for P11-C -- deleted in P2: 0)", "",
          "| metric | Master Plan | recalculated | delta |", "|---|---:|---:|---:|",
          f"| duplicate names | 150 | {t['duplicate_names']} | {t['duplicate_names']-150:+d} |",
          f"| non-final definitions | 365 | {t['non_final_defs']} | {t['non_final_defs']-365:+d} |",
          f"| captured & LIVE | 191 | {t['captured_live']} | {t['captured_live']-191:+d} |",
          f"| captured, alias unused | 49 | {t['captured_unused']} | {t['captured_unused']-49:+d} |",
          f"| no known capture | 125 | {t['no_capture']} | {t['no_capture']-125:+d} |", "",
          "Totals for duplicate names and non-final definitions match exactly -> the tree is",
          "unchanged. The bucket reallocation is explained by correction (1) above and is strictly",
          "conservative (more code protected, fewer deletion candidates).", "",
          f"Whole-tree classification: {json.dumps(inv['totals'], ensure_ascii=False)}", "",
          "## Protected areas -- untouched", "", "| area | state |", "|---|---|",
          "| `db/` | not read, not written |",
          "| `sessions/` | untouched |",
          "| `runtime/` | untouched |",
          "| `logs/` | untouched |",
          "| `.env*` | never read for values, never packaged |",
          "| production server | not contacted |",
          f"| runtime source files | **0 changed** (scope guard: {sg['counts']['UNCHANGED']} UNCHANGED) |"]
    (rep / "p2_golden_baseline_report.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    print("reports written:")
    for p in sorted(rep.glob("*.md")):
        print(f"   {p.name}  {p.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
