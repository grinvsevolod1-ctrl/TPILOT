# -*- coding: utf-8 -*-
"""W3.2 D6/D7 final business-date correction (2026-08-01/02) -- correction-specific
scope guard.

Companion to the legacy `tools\\w3_2_scope_guard_selftest.py` (F-5), which this guard
does NOT edit or repair. Editing `main.py` in this correction NECESSARILY makes the
legacy guard's frozen 20260730 `main.py` assertion report drift -- that is expected,
recorded elsewhere as HISTORICAL_SCOPE_DRIFT_EXPECTED, and is evidence of the earlier
correction boundary, not a regression (11_FILE_BOUNDARY.md sec 3 of the authoritative
plan). This guard supplies the CURRENT boundary's own proof:

  1. every one of the 12 REQUIRED_RUNTIME_FILES is hashed; a changed hash is permitted
     ONLY for main.py, and only when it equals the single approved post-fix digest;
  2. only the 5 already-approved Design C+ tool changes and 2 new tools are permitted
     to differ from "did not exist / untouched" -- this guard does not itself gate their
     content, only that no OTHER *.py file changed;
  3. the legacy scope guard and the 20260730 baseline artifact are asserted
     byte-identical -- HISTORICAL_ARTIFACT_MUTATED otherwise;
  4. main._tp_pss_parse_date_token's AST fingerprint (F-3) is asserted unchanged;
  5. allow_spend=True AST count stays exactly 2; mojibake patterns stay at 0 hits;
  6. a repository residue scan flags any *.bak*/*.tmp/*.orig/*.rej/*_cursor_copy* file
     OUTSIDE the frozen pre-existing-residue snapshot (this correction creates its
     backups only under the audit output root, never inside C:\\ALM_TPilot).

Baseline artifact (never inside the repository):
  C:\\ALM_TPilot_AUDIT\\20260801\\W3_2_FINAL_BUSINESS_DATE_FIX_IMPLEMENTATION\\
      d6d7_baseline_hashes.json
      pre_existing_residue_snapshot.json

Run:  python tools\\w3_2_d6d7_correction_scope_guard_selftest.py
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
AUDIT_ROOT = Path(r"C:\ALM_TPilot_AUDIT\20260801\W3_2_FINAL_BUSINESS_DATE_FIX_IMPLEMENTATION")
BASELINE_PATH = AUDIT_ROOT / "d6d7_baseline_hashes.json"
RESIDUE_SNAPSHOT_PATH = AUDIT_ROOT / "pre_existing_residue_snapshot.json"
LEGACY_GUARD_PATH = BASE_DIR / "tools" / "w3_2_scope_guard_selftest.py"
HISTORICAL_BASELINE_PATH = Path(r"C:\ALM_TPilot_AUDIT\20260730\W3_2_CORRECTION\baseline_hashes.json")

# W3.2 F-2 evidence correction (2026-08-03, closes R-D4): pre-image of every tools\*.py
# file NOT approved to change by this round, used by run_tool_boundary_check() below.
# See w3_2_f2_tool_boundary_baseline.json's own _purpose for provenance.
TOOL_BOUNDARY_BASELINE_PATH = Path(
    r"C:\ALM_TPilot_AUDIT\20260803\W3_2_F2_EVIDENCE_CORRECTION"
    r"\w3_2_f2_tool_boundary_baseline.json")

REQUIRED_RUNTIME_FILES = (
    "storage.py", "main.py", "panel_bot.py", "preflight_check.py", "manager_bot.py",
    "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py", "manager_registry.py",
    "soft_watchdog_pinger.py", "health_server.py", "stats_parity_harness.py",
)

RESIDUE_PATTERNS = ("*.bak*", "*.tmp", "*.orig", "*.rej", "*_cursor_copy*")
RESIDUE_EXCLUDE_DIRS = {"venv", "__pycache__", "logs", "db", "sessions", "runtime",
                        ".backups", ".git"}

MOJIBAKE_PATTERNS = ("\u00d0", "\u00d1", "\u00e2\u20ac")

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def load_residue_snapshot() -> set:
    data = json.loads(RESIDUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return set(data["files"])


def load_tool_boundary_baseline() -> dict:
    return json.loads(TOOL_BOUNDARY_BASELINE_PATH.read_text(encoding="utf-8"))


def run_runtime_file_hash_check(baseline: dict) -> dict:
    """Requirement 1: hash all 12 required files; a changed hash is permitted ONLY for
    main.py, and only when it equals the single approved post-fix digest."""
    rows = []
    ok = True
    pre = baseline["pre_fix_sha256"]
    approved_post = baseline["approved_post_fix_sha256"]
    allowed = set(baseline["allowed_runtime_changes"])
    for fname in REQUIRED_RUNTIME_FILES:
        p = BASE_DIR / fname
        if not p.is_file():
            rows.append({"file": fname, "status": "MISSING_RUNTIME_FILE", "ok": False})
            ok = False
            continue
        live = _sha256(p)
        base = pre.get(fname)
        if base is None:
            rows.append({"file": fname, "status": "MISSING_BASELINE_ENTRY", "ok": False})
            ok = False
            continue
        if live == base:
            rows.append({"file": fname, "status": "UNCHANGED_AS_REQUIRED", "ok": True})
            continue
        if fname not in allowed:
            rows.append({"file": fname, "status": "UNAUTHORIZED_RUNTIME_CHANGE", "ok": False,
                        "delta": f"{base[:12]}->{live[:12]}"})
            ok = False
            continue
        expected = approved_post.get(fname)
        row_ok = live == expected
        rows.append({"file": fname, "status": "CHANGED_AS_APPROVED" if row_ok
                    else "CHANGED_BUT_NOT_THE_APPROVED_DIGEST", "ok": row_ok,
                    "delta": f"live={live[:16]} approved={str(expected)[:16]}"})
        ok = ok and row_ok
    return {"ok": ok, "rows": rows}


def run_tool_boundary_check(baseline: dict, tool_boundary_baseline: dict = None) -> dict:
    """Requirement 2 (W3.2 F-2 evidence correction, 2026-08-03 -- R-D4 fix): no *.py file
    OUTSIDE the approved runtime/tool/new-tool set changed in C:\\ALM_TPilot\\tools.

    Previously dead code: the loop body was an unconditional ``continue`` (``rows`` was
    always ``[]``, ``ok`` was always ``True``) and it was never called from ``main()``
    (R-D4). Now genuinely enforced against a recorded pre-image
    (``w3_2_f2_tool_boundary_baseline.json``, ``TOOL_BOUNDARY_BASELINE_PATH``):

      * every tools\\*.py file this round is approved to change (this file plus the
        Design C+ selftest/mutation-proof pair) is skipped -- its CONTENT is proven by the
        focused selftest and the regression suite, not re-derived here;
      * every OTHER tools\\*.py file present in the pre-image must be byte-identical to
        it -- a live/pre-image mismatch is ``UNAPPROVED_TOOL_FILE_MODIFIED``;
      * a tools\\*.py file present live but ABSENT from both the pre-image and the
        approved set is ``UNAPPROVED_NEW_TOOL_FILE`` (also covers the two tools this
        round is NOT approved to touch that were themselves new in the prior D6/D7
        round -- they are already present in ``other_tool_files`` and therefore covered
        by the first bullet, not this one);
      * a tools\\*.py file present in the pre-image but missing live is
        ``UNAPPROVED_TOOL_FILE_DELETED``.
    """
    tool_boundary_baseline = tool_boundary_baseline or load_tool_boundary_baseline()
    approved_rel = set(tool_boundary_baseline["approved_boundary_files"])
    other_files = tool_boundary_baseline["other_tool_files"]

    rows = []
    ok = True
    tools_dir = BASE_DIR / "tools"
    seen = set()
    for p in sorted(tools_dir.glob("*.py")):
        name = p.name
        seen.add(name)
        if name in approved_rel:
            continue
        live = _sha256(p)
        if name not in other_files:
            rows.append({"file": "tools/" + name, "status": "UNAPPROVED_NEW_TOOL_FILE",
                        "ok": False})
            ok = False
            continue
        expected = other_files[name]
        row_ok = live == expected
        rows.append({"file": "tools/" + name,
                    "status": "UNCHANGED" if row_ok else "UNAPPROVED_TOOL_FILE_MODIFIED",
                    "ok": row_ok,
                    "delta": "" if row_ok else "%s -> %s" % (expected[:16], live[:16])})
        ok = ok and row_ok

    for name in other_files:
        if name not in seen:
            rows.append({"file": "tools/" + name, "status": "UNAPPROVED_TOOL_FILE_DELETED",
                        "ok": False})
            ok = False

    return {"ok": ok, "rows": rows}


def run_historical_artifact_check(baseline: dict) -> dict:
    """Requirement 3: legacy scope guard and the 20260730 baseline artifact stay
    byte-identical. Never repaired, never edited."""
    rows = []
    ok = True
    legacy_hash = _sha256(LEGACY_GUARD_PATH) if LEGACY_GUARD_PATH.is_file() else None
    hist_hash = _sha256(HISTORICAL_BASELINE_PATH) if HISTORICAL_BASELINE_PATH.is_file() else None
    exp_legacy = baseline["legacy_scope_guard_sha256"]
    exp_hist = baseline["historical_20260730_baseline_sha256"]
    row_ok = legacy_hash == exp_legacy
    rows.append({"file": "tools/w3_2_scope_guard_selftest.py",
                "status": "UNCHANGED" if row_ok else "HISTORICAL_ARTIFACT_MUTATED",
                "ok": row_ok, "delta": f"{legacy_hash} vs {exp_legacy}"})
    ok = ok and row_ok
    row_ok = hist_hash == exp_hist
    rows.append({"file": "20260730/W3_2_CORRECTION/baseline_hashes.json",
                "status": "UNCHANGED" if row_ok else "HISTORICAL_ARTIFACT_MUTATED",
                "ok": row_ok, "delta": f"{hist_hash} vs {exp_hist}"})
    ok = ok and row_ok
    return {"ok": ok, "rows": rows}


def run_f3_fingerprint_check(baseline: dict) -> dict:
    """Requirement 4: main._tp_pss_parse_date_token unchanged (F-3, DEFERRED_TO_W3.4,
    not touched by this correction). Re-established directly by fingerprint since the
    legacy guard's whole-file main.py hash can no longer carry this proof after the
    authorized D6/D7 edit."""
    src = (BASE_DIR / "main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    matches = [n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_tp_pss_parse_date_token"]
    if len(matches) != 1:
        return {"ok": False, "rows": [{"file": "main.py", "status": "F3_DEF_COUNT_UNEXPECTED",
                                      "ok": False, "delta": str(len(matches))}]}
    fp = hashlib.sha256(ast.unparse(matches[0]).encode("utf-8")).hexdigest()
    expected = baseline["f3_fingerprint"]
    row_ok = fp == expected
    return {"ok": row_ok, "rows": [{"file": "main.py::_tp_pss_parse_date_token",
                                   "status": "UNCHANGED" if row_ok else "F3_FINGERPRINT_DRIFT",
                                   "ok": row_ok, "delta": f"{fp} vs {expected}"}]}


def run_allow_spend_and_mojibake_check() -> dict:
    """Requirement 5."""
    rows = []
    ok = True
    src = (BASE_DIR / "main.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    count = sum(
        1 for node in ast.walk(tree) if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True
    )
    row_ok = count == 2
    rows.append({"file": "main.py", "status": "allow_spend=True count",
                "ok": row_ok, "delta": str(count)})
    ok = ok and row_ok

    for pat in MOJIBAKE_PATTERNS:
        n = src.count(pat)
        row_ok = n == 0
        rows.append({"file": "main.py", "status": f"mojibake pattern {ascii(pat)}",
                    "ok": row_ok, "delta": str(n)})
        ok = ok and row_ok
    return {"ok": ok, "rows": rows}


def run_residue_scan(snapshot: set) -> dict:
    """Requirement 6: any residue file OUTSIDE the frozen pre-existing snapshot is RED --
    proves no backup/temp/mutation file was created inside C:\\ALM_TPilot by this
    correction."""
    hits_new = []
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in RESIDUE_EXCLUDE_DIRS]
        for fn in files:
            if any(fnmatch.fnmatch(fn, p) for p in RESIDUE_PATTERNS):
                rel = os.path.normpath(os.path.relpath(os.path.join(root, fn), BASE_DIR))
                if rel not in snapshot:
                    hits_new.append(rel)
    hits_new.sort()
    return {"ok": not hits_new, "new_residue_files": hits_new}


def main() -> int:
    baseline = load_baseline()
    snapshot = load_residue_snapshot()
    tool_boundary_baseline = load_tool_boundary_baseline()

    r1 = run_runtime_file_hash_check(baseline)
    for row in r1["rows"]:
        check(f"runtime-hash: {row['file']}: {row['status']}", row["ok"], row.get("delta") or "")

    r2 = run_tool_boundary_check(baseline, tool_boundary_baseline)
    for row in r2["rows"]:
        check(f"tool-boundary: {row['file']}: {row['status']}", row["ok"], row.get("delta") or "")

    r3 = run_historical_artifact_check(baseline)
    for row in r3["rows"]:
        check(f"historical: {row['file']}: {row['status']}", row["ok"], row.get("delta") or "")

    r4 = run_f3_fingerprint_check(baseline)
    for row in r4["rows"]:
        check(f"F-3: {row['file']}: {row['status']}", row["ok"], row.get("delta") or "")

    r5 = run_allow_spend_and_mojibake_check()
    for row in r5["rows"]:
        check(f"{row['file']}: {row['status']}", row["ok"], row.get("delta") or "")

    r6 = run_residue_scan(snapshot)
    check("repository residue scan: no NEW *.bak*/*.tmp/*.orig/*.rej/*_cursor_copy* file "
         "inside C:\\ALM_TPilot beyond the pre-existing snapshot",
         r6["ok"], str(r6["new_residue_files"]))

    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (D6/D7 correction scope guard green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
