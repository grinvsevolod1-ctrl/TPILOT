# -*- coding: utf-8 -*-
"""tools/rc_baseline_scope_guard.py -- general-purpose golden-baseline scope guard.

WHY A NEW FILE INSTEAD OF EDITING tools/rc_scope_guard_selftest.py
-----------------------------------------------------------------
The existing `rc_scope_guard_selftest.py` already accepts an explicit baseline
directory (`resolve_backup(sys.argv)`) and the Master Plan says to preserve that
interface -- so it is left byte-untouched. But it is RELEASE-SPECIFIC: it carries
a hardcoded `DECLARED` set describing one past release candidate (the wave L2/L3
readiness + bizlink RC). It answers "did THAT RC change only what it declared?".

The Master Plan needs a different, reusable question answered at every phase:
"how does the CURRENT tree differ from the golden baseline, and was each
difference declared by the phase that made it?"

This tool answers that, against any `<baseline>/original/` + control manifest.

Statuses: UNCHANGED / CHANGED / NEW / MISSING.

Usage:
    python tools/rc_baseline_scope_guard.py <baseline_dir> [--root <project>]
        [--declare <relpath>]...        # paths this phase is allowed to change
        [--declare-file <list.txt>]
        [--json <out>] [--md <out>]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rc_release_control import dumps, is_archival, sha256_file  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent


def compare(root: Path, baseline: Path, declared: List[str]) -> Dict[str, Any]:
    control = baseline / "control" / "golden_runtime_manifest.json"
    if not control.is_file():
        raise SystemExit(f"golden_runtime_manifest.json not found under {baseline}")
    manifest = json.loads(control.read_text(encoding="utf-8"))
    tracked = {e["relative_path"]: e for e in manifest["files"]}

    original = baseline / "original"
    rows: List[Dict[str, Any]] = []
    counts = {"UNCHANGED": 0, "CHANGED": 0, "NEW": 0, "MISSING": 0}

    for rp, e in sorted(tracked.items()):
        cur = root / rp
        if not cur.is_file():
            status = "MISSING"
            cur_sha = None
        else:
            cur_sha = sha256_file(cur)
            status = "UNCHANGED" if cur_sha == e["sha256"] else "CHANGED"
        counts[status] += 1
        if status != "UNCHANGED":
            rows.append({"relative_path": rp, "status": status,
                         "baseline_sha256": e["sha256"], "current_sha256": cur_sha,
                         "declared": rp in declared})

    # NEW  = a file appeared inside a tracked PACKAGE the baseline covers.
    #        (that is a genuine scope change -- e.g. a new features/ module)
    # UNTRACKED = a root-level module present in the tree but never tracked.
    #        Informational only: archival copies and one-off operator utilities
    #        legitimately live in the tree and are not release content.
    baseline_paths = set(tracked)
    tracked_pkg_roots = sorted({rp.split("/", 1)[0] for rp in baseline_paths if "/" in rp})

    untracked: List[Dict[str, Any]] = []
    for pkg in tracked_pkg_roots:
        for cur in sorted((root / pkg).rglob("*.py")):
            if "__pycache__" in cur.parts or is_archival(cur.name):
                continue
            rp = cur.resolve().relative_to(root.resolve()).as_posix()
            if rp in baseline_paths:
                continue
            counts["NEW"] += 1
            rows.append({"relative_path": rp, "status": "NEW",
                         "baseline_sha256": None, "current_sha256": sha256_file(cur),
                         "declared": rp in declared})

    for cur in sorted(root.glob("*.py")):
        rp = cur.name
        if rp in baseline_paths or is_archival(rp):
            continue
        untracked.append({"relative_path": rp, "size": cur.stat().st_size,
                          "sha256": sha256_file(cur)})

    undeclared = [r for r in rows if not r["declared"]]
    return {
        "baseline": str(baseline), "project_root": str(root),
        "tracked_count": len(tracked), "counts": counts,
        "declared_paths": sorted(declared),
        "differences": rows,
        "undeclared_differences": undeclared,
        "untracked_root_modules": untracked,
        "untracked_note": (
            "Present in the tree but outside the tracked release baseline: "
            "operator utilities and one-off scripts. Informational, not a scope "
            "violation. Packaging is decided by the dependency closure."),
        "ok": not undeclared,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("--root", default=str(BASE_DIR))
    ap.add_argument("--declare", action="append", default=[])
    ap.add_argument("--declare-file")
    ap.add_argument("--json")
    ap.add_argument("--md")
    a = ap.parse_args()

    declared = list(a.declare)
    if a.declare_file and Path(a.declare_file).is_file():
        declared += [l.strip() for l in Path(a.declare_file).read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.strip().startswith("#")]

    res = compare(Path(a.root).resolve(), Path(a.baseline).resolve(), declared)

    if a.json:
        Path(a.json).parent.mkdir(parents=True, exist_ok=True)
        Path(a.json).write_text(dumps(res), encoding="utf-8")
    if a.md:
        L = ["# SCOPE GUARD VALIDATION\n",
             f"- baseline: `{res['baseline']}`",
             f"- tracked files: {res['tracked_count']}",
             f"- UNCHANGED: {res['counts']['UNCHANGED']}",
             f"- CHANGED: {res['counts']['CHANGED']}",
             f"- NEW: {res['counts']['NEW']}",
             f"- MISSING: {res['counts']['MISSING']}",
             f"- UNTRACKED root modules (informational): {len(res['untracked_root_modules'])}",
             f"- undeclared differences: **{len(res['undeclared_differences'])}**\n"]
        if res["differences"]:
            L += ["| path | status | declared |", "|---|---|---|"]
            L += [f"| `{r['relative_path']}` | {r['status']} | {'yes' if r['declared'] else '**NO**'} |"
                  for r in res["differences"]]
        else:
            L.append("_no differences from the golden baseline_")
        if res["untracked_root_modules"]:
            L += ["", "## UNTRACKED ROOT MODULES (informational)", "",
                  res["untracked_note"], "",
                  "| path | size |", "|---|---:|"]
            L += [f"| `{u['relative_path']}` | {u['size']} |" for u in res["untracked_root_modules"]]
        Path(a.md).parent.mkdir(parents=True, exist_ok=True)
        Path(a.md).write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"baseline : {res['baseline']}")
    print(f"tracked  : {res['tracked_count']}")
    print(f"counts   : {res['counts']}")
    print(f"untracked root modules : {len(res['untracked_root_modules'])} (informational)")
    print(f"undeclared differences: {len(res['undeclared_differences'])}")
    for r in res["undeclared_differences"]:
        print(f"  [UNDECLARED] {r['status']:<9} {r['relative_path']}")
    print("RESULT:", "PASS" if res["ok"] else "FAIL")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
