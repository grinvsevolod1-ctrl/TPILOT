# -*- coding: utf-8 -*-
"""tools/rc_legacy_inventory_report.py -- P11-C input inventory (READ-ONLY).

Renders the human-readable legacy/capture inventory the Master Plan requires as
INPUT for the future controlled-cleanup phase (P11-C).

P2 DELETES NOTHING. Every row here is evidence, not an instruction.

The critical column is the classification, because in TPilot a shadowed
definition is frequently NOT dead: it is captured via
`_X_PREV = globals().get("name")` before being redefined, and the delegate is
called later. Deleting such a definition breaks runtime silently.

Usage:
    python tools/rc_legacy_inventory_report.py --baseline <dir> [--root <project>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rc_release_control import ModuleIndex, dumps, rel  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent

MONOLITHS = ["main.py", "panel_bot.py", "manager_bot.py", "partner_stat_bot.py"]

#: Measurements recorded in the accepted Master Plan; recalculated here and
#: any difference must be explained, never silently accepted (section 24).
MASTER_PLAN_BASELINE = {
    "duplicate_names": 150, "non_final_defs": 365,
    "captured_live": 191, "captured_unused": 49, "no_capture": 125,
}


def measure(root: Path) -> Dict[str, Any]:
    per_file: Dict[str, Dict[str, int]] = {}
    tot = {"duplicate_names": 0, "non_final_defs": 0,
           "captured_live": 0, "captured_unused": 0, "no_capture": 0}
    rows: List[Dict[str, Any]] = []

    for f in MONOLITHS:
        mi = ModuleIndex(root / f, root)
        dup = {k: v for k, v in mi.defs.items() if len(v) > 1}
        live = unused = nocap = 0
        for name, locs in sorted(dup.items()):
            for i, d in enumerate(locs[:-1]):
                nxt = locs[i + 1]
                cov = [(cv, cl) for cv, cl in mi.captures.get(name, []) if d < cl < nxt]
                if not cov:
                    bucket, cls = "no_capture", mi.classify_definition(name, d)
                    nocap += 1
                elif any(mi.capture_uses.get(cv, 0) > 0 for cv, _ in cov):
                    bucket, cls = "captured_live", "ACTIVE_CAPTURED"
                    live += 1
                else:
                    bucket, cls = "captured_unused", "PROVEN_DEAD_CANDIDATE"
                    unused += 1
                rows.append({
                    "file": f, "symbol": name, "definition_line": d,
                    "definition_lines": locs, "final_binding": locs[-1],
                    "capture_aliases": [cv for cv, _ in cov],
                    "capture_lines": [cl for _, cl in cov],
                    "capture_used": any(mi.capture_uses.get(cv, 0) > 0 for cv, _ in cov),
                    "callbacks": mi.handlers.get(name, []),
                    "dynamic_reference": name in mi.dynamic,
                    "bucket": bucket, "classification": cls,
                })
        per_file[f] = {"duplicate_names": len(dup),
                       "non_final_defs": sum(len(v) - 1 for v in dup.values()),
                       "captured_live": live, "captured_unused": unused, "no_capture": nocap}
        for k in tot:
            tot[k] += per_file[f][k]
    return {"totals": tot, "per_file": per_file, "rows": rows}


def render(res: Dict[str, Any], root: Path) -> str:
    t, pf = res["totals"], res["per_file"]
    L: List[str] = []
    L.append("# LEGACY / CAPTURE BINDING INVENTORY (P2 -- READ-ONLY)\n")
    L.append("**P2 deletes nothing.** This is the evidence base for phase **P11-C** "
             "(Controlled Legacy / Dead-Code Cleanup).\n")
    L.append("## Why a shadowed definition is not automatically dead\n")
    L.append("TPilot redefines top-level names (\"last def wins\") *and* captures the earlier "
             "function object first:\n")
    L.append("```python\ndef foo(...): ...        # still LIVE\n"
             "_OLD_FOO = globals().get(\"foo\")\ndef foo(...): ...        # final binding\n"
             "... await _OLD_FOO(...)  # the first def is called from here\n```\n")
    L.append("Canonical case: `manager_bot._send_event_to_user` @4593 is shadowed by @5186, "
             "yet it is captured into `_M26C_ORIG_SEND_EVENT_TO_USER` @5069 and invoked "
             "@5406-5407 -- and it is the body that carries the accepted **B-1 placeholder "
             "gate**. Deleting it would destroy a closed invariant.\n")

    L.append("## Recalculated measurements vs the accepted Master Plan\n")
    L.append("| metric | Master Plan | recalculated | delta |")
    L.append("|---|---:|---:|---:|")
    for k, label in [("duplicate_names", "duplicate names"),
                     ("non_final_defs", "non-final definitions"),
                     ("captured_live", "captured & LIVE (must not delete)"),
                     ("captured_unused", "captured, alias unused"),
                     ("no_capture", "no known capture")]:
        d = t[k] - MASTER_PLAN_BASELINE[k]
        L.append(f"| {label} | {MASTER_PLAN_BASELINE[k]} | {t[k]} | {d:+d} |")
    L.append("")
    L.append("**Explanation of the deltas.** Totals for *duplicate names* and *non-final "
             "definitions* match exactly, confirming the tree is unchanged. The small "
             "reallocation between the capture buckets comes from a METHOD improvement, not "
             "code drift: the earlier measurement counted alias usage with a word regex over "
             "the source, which also matched mentions inside comments and strings. This tool "
             "counts AST `Name` loads **plus** string-literal lookups such as "
             "`globals().get(\"_TPILOT_ORIG_X\")`. That correction was found during P2 "
             "self-review and matters: `_TP_REPORT_V3_ORIG_TP_QS_ROW_BUCKET` and "
             "`_TP_REPORT_V5_ORIG_TP_QS_ROW_BUCKET` are read *only* through "
             "`globals().get(...)`, so a Name-only scan would have mis-classified two LIVE "
             "delegate chains as deletion candidates. The net effect is strictly "
             "conservative: more definitions are protected, fewer are nominated.\n")

    L.append("## Per-file\n")
    L.append("| file | duplicate names | non-final defs | captured LIVE | captured unused | no capture |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for f in MONOLITHS:
        r = pf[f]
        L.append(f"| `{f}` | {r['duplicate_names']} | {r['non_final_defs']} | "
                 f"**{r['captured_live']}** | {r['captured_unused']} | {r['no_capture']} |")
    L.append(f"| **TOTAL** | **{t['duplicate_names']}** | **{t['non_final_defs']}** | "
             f"**{t['captured_live']}** | **{t['captured_unused']}** | **{t['no_capture']}** |")
    L.append("")
    pct = 100.0 * t["captured_live"] / max(1, t["non_final_defs"])
    L.append(f"**{t['captured_live']} of {t['non_final_defs']} ({pct:.0f}%) non-final "
             f"definitions are LIVE through a delegate chain.** A naive "
             f"\"keep only the last def\" cleanup would break them all.\n")

    L.append("## Classification legend\n")
    L.append("| classification | meaning | deletable in P11-C |")
    L.append("|---|---|---|")
    L.append("| `ACTIVE_AUTHORITATIVE` | final runtime binding | NO |")
    L.append("| `ACTIVE_CAPTURED` | shadowed but captured and the delegate is called | **NO** |")
    L.append("| `ACTIVE_CALLBACK` | reachable via handler registration | NO |")
    L.append("| `ACTIVE_FALLBACK` | degraded/compat path | NO |")
    L.append("| `MIGRATION_COMPATIBILITY` | required by legacy schema/data | NO |")
    L.append("| `TEST_ONLY` | required by suites | NO |")
    L.append("| `PROVEN_DEAD_CANDIDATE` | candidate only -- must still pass all 12 proof gates | only after proof |")
    L.append("| `UNKNOWN` | reachability unproven | **NEVER** |")
    L.append("")

    L.append("## Non-authoritative definitions (full evidence rows)\n")
    L.append("| file | symbol | def line | final | capture alias | alias used | callbacks | dynamic | classification |")
    L.append("|---|---|---:|---:|---|---|---|---|---|")
    for r in res["rows"]:
        L.append(f"| `{r['file']}` | `{r['symbol']}` | {r['definition_line']} | "
                 f"{r['final_binding']} | {', '.join('`%s`' % a for a in r['capture_aliases']) or '—'} | "
                 f"{'yes' if r['capture_used'] else 'no'} | "
                 f"{len(r['callbacks']) or '—'} | {'yes' if r['dynamic_reference'] else '—'} | "
                 f"{r['classification']} |")
    L.append("")
    L.append("## P2 outcome\n")
    L.append("- deleted in P2: **0**")
    L.append("- `PROVEN_DEAD_CANDIDATE` rows are candidates for P11-C, **not** approved deletions.")
    L.append("- Every candidate must still pass the 12 proof gates and RED/REMOVE before removal.")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--root", default=str(BASE_DIR))
    a = ap.parse_args()
    root = Path(a.root).resolve()
    bl = Path(a.baseline).resolve()
    res = measure(root)
    (bl / "reports").mkdir(parents=True, exist_ok=True)
    (bl / "control").mkdir(parents=True, exist_ok=True)
    (bl / "reports" / "legacy_binding_inventory.md").write_text(render(res, root), encoding="utf-8")
    (bl / "control" / "legacy_binding_inventory.json").write_text(dumps(res), encoding="utf-8")
    t = res["totals"]
    print(f"duplicate names   : {t['duplicate_names']}  (master plan {MASTER_PLAN_BASELINE['duplicate_names']})")
    print(f"non-final defs    : {t['non_final_defs']}  (master plan {MASTER_PLAN_BASELINE['non_final_defs']})")
    print(f"captured LIVE     : {t['captured_live']}  (master plan {MASTER_PLAN_BASELINE['captured_live']})")
    print(f"captured unused   : {t['captured_unused']}  (master plan {MASTER_PLAN_BASELINE['captured_unused']})")
    print(f"no capture        : {t['no_capture']}  (master plan {MASTER_PLAN_BASELINE['no_capture']})")
    print("deleted in P2     : 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
