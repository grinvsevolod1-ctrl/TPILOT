# -*- coding: utf-8 -*-
"""tools/w3_2_scope_guard_selftest.py -- W3.2 file-boundary and static scope guard
(task spec sections 9/10; hardened across the W3.2 correction rounds, closing
independent-review finding F-5).

PRIMARY proof (hash/diff boundary, section 4): every watched runtime file's live
sha256 is compared against a FROZEN baseline artifact
(C:\\ALM_TPilot_AUDIT\\20260730\\W3_2_CORRECTION\\baseline_hashes.json, itself sourced
only from pre-existing immutable audit artifacts -- never a hand-typed/invented hash).
Only files listed in the baseline's `allowed_correction_changes` may differ from it;
every other watched file must be byte-identical.

SECONDARY signal (marker scan, retained, not the sole proof): no out-of-scope file may
contain a W3.2 DST marker or a W3.2-correction marker.

W3.2 CORRECTION ROUND 2: `REQUIRED_RUNTIME_FILES` became a frozen literal in this
module, independent of the baseline JSON's own keys -- fixing a fail-open where a
dropped baseline entry silently skipped that file. `enforce_no_extra_files=True`
(opt-in) flagged an untracked `*.py` file, but defaulted to False against the live
project root.

W3.2 CORRECTION ROUND 3 (2026-07-30): an independent re-review of round 2
(C:\\ALM_TPilot_AUDIT\\20260730\\W3_2_CORRECTION_R2_REVIEW\\06_SCOPE_GUARD_FAIL_CLOSED_REVIEW.md)
found three residual gaps, all closed here:

  1. **Inventory omissions failed open.** If `REQUIRED_RUNTIME_FILES` itself were ever
     shrunk (a source edit to THIS module, or a runtime monkeypatch), no check inside
     the same run could detect its own constant had been tampered with -- a tool cannot
     audit its own definition from within itself. Fixed by cross-validating
     `REQUIRED_RUNTIME_FILES` against an EXTERNAL, independently-anchored canonical list
     (`required_runtime_files.json`, outside this module and outside `C:\\ALM_TPilot`,
     same pattern as `BASELINE_PATH`) -- any mismatch is `INVENTORY_MISMATCH_VS_CANONICAL`,
     fail-closed, regardless of which side was edited.
  2. **The default invocation was permissive.** `enforce_no_extra_files` defaulted to
     False, so a genuinely new, undeclared runtime `.py` file dropped into the live
     project root went undetected under normal (default-argument) use -- "no optional
     parameter may default to permissive mode in production gate execution" (task spec
     section 9). Fixed by making new-file detection UNCONDITIONAL: every `*.py` present
     in `base_dir` is compared against a frozen snapshot of the file names that
     legitimately existed at round-3 time (`top_level_py_inventory.json`, external,
     same anchoring pattern) -- any name outside that snapshot AND outside
     `REQUIRED_RUNTIME_FILES` is `UNDECLARED_NEW_PY_FILE`, always, with no flag to turn
     it off. `enforce_no_extra_files` is retained only as an ADDITIONAL, narrower,
     opt-in tier for the mutation harness's small purpose-built temp trees (where
     "extra file" has an unambiguous meaning against just the 12 required names);
     removing it does not restore any permissive behavior -- the unconditional check
     already covers the live root by default.
  3. **An extra baseline key was invisible.** `extra_in_baseline_not_required` was
     computed but never gated. Now any baseline key with no corresponding required file
     is `UNCLASSIFIED_EXTRA_BASELINE_KEY`, fail-closed, unless explicitly listed in
     `KNOWN_NON_REQUIRED_BASELINE_KEYS` (empty by default -- section 9: "extra baseline
     key -> RED unless explicitly classified").

Also added: corrupt/partial baseline JSON (missing top-level key, or unparseable) is
caught and reported structurally rather than relying on an uncaught exception; the
baseline's own keys are checked for a case-normalized duplicate
(`CASE_DUPLICATE_BASELINE_KEY`); a case-alias path bypass remains defeated via an
exact-case `os.listdir()` presence check (round 2, retained).

    python tools\\w3_2_scope_guard_selftest.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BACKUP_DIR = Path(r"C:\ALM_TPilot_AUDIT\20260729\W3_2_TIMEZONE_DST\BACKUP")
BASELINE_PATH = Path(r"C:\ALM_TPilot_AUDIT\20260730\W3_2_CORRECTION\baseline_hashes.json")

# External, independent of this module's own `REQUIRED_RUNTIME_FILES` tuple -- an edit
# to EITHER side alone (this module's constant, or the canonical JSON) is detectable as
# a mismatch, rather than a shrunk in-file tuple silently reducing coverage with nothing
# to compare it against (R2 review, "inventory omissions must fail closed").
CANONICAL_REQUIRED_FILES_PATH = Path(
    r"C:\ALM_TPilot_AUDIT\20260730\W3_2_CORRECTION_R3\required_runtime_files.json")

# External snapshot of the top-level `.py` files that legitimately existed in
# `C:\ALM_TPilot` at round-3 time (~46 files: the 12 required runtime files plus
# unrelated one-off utility/check/dump scripts). A name outside this snapshot AND
# outside `REQUIRED_RUNTIME_FILES` is a genuinely NEW file -- this check runs
# unconditionally (R2 review, "default flags must not disable critical validation
# silently").
TOP_LEVEL_INVENTORY_PATH = Path(
    r"C:\ALM_TPilot_AUDIT\20260730\W3_2_CORRECTION_R3\top_level_py_inventory.json")

# Frozen, independent of whatever the baseline JSON artifact happens to contain.
REQUIRED_RUNTIME_FILES = (
    "storage.py", "main.py", "panel_bot.py", "preflight_check.py", "manager_bot.py",
    "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py", "manager_registry.py",
    "soft_watchdog_pinger.py", "health_server.py", "stats_parity_harness.py",
)

# A baseline key with no corresponding required file is RED unless explicitly listed
# here (section 9: "extra baseline key -> RED unless explicitly classified"). Empty by
# design -- nothing is pre-approved.
KNOWN_NON_REQUIRED_BASELINE_KEYS: tuple = ()

REQUIRED_BASELINE_TOP_LEVEL_KEYS = ("baseline_sha256", "allowed_correction_changes")

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latest_backup(fname: str) -> Path | None:
    matches = sorted(BACKUP_DIR.glob(f"{fname}.bak_w32_*"))
    return matches[-1] if matches else None


def scan_text_for_markers(text: str, markers) -> list[str]:
    """THE real marker-scan check: return which of `markers` appear as a literal
    substring in `text`. Retained as the SECONDARY signal, never the sole proof."""
    return [m for m in markers if m in text]


def load_baseline(baseline_path: Path = BASELINE_PATH) -> dict:
    """Load the frozen pre-correction baseline. Raises on I/O error; JSON parse
    errors and missing required top-level keys are caught by the caller
    (`run_hash_diff_boundary`) and turned into a structured fail-closed result rather
    than an uncaught exception (section 9: "corrupt or partial inventory configuration
    must fail")."""
    return json.loads(baseline_path.read_text(encoding="utf-8"))


def load_canonical_required_files(path: Path = CANONICAL_REQUIRED_FILES_PATH) -> tuple:
    data = json.loads(path.read_text(encoding="utf-8"))
    return tuple(sorted(data["required_runtime_files"]))


def load_known_top_level_py_files(path: Path = TOP_LEVEL_INVENTORY_PATH) -> set:
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data["known_top_level_py_files"])


def run_hash_diff_boundary(base_dir: Path = BASE_DIR, baseline_path: Path = BASELINE_PATH,
                            enforce_no_extra_files: bool = False) -> dict:
    """PRIMARY check: live sha256 of every REQUIRED runtime file vs the frozen baseline,
    plus the round-3 hardening checks below. Returns a structured result -- never just
    True/False.

    `enforce_no_extra_files`: an ADDITIONAL, narrower opt-in tier retained for the
    mutation harness's small purpose-built temp trees, where "extra" means "not one of
    the 12 required names" with no other legitimate file expected at all. It does NOT
    gate the unconditional `UNDECLARED_NEW_PY_FILE` check below, which always runs
    against `base_dir` regardless of this flag -- there is no argument combination that
    disables new-file detection.
    """
    rows: list[dict] = []
    ok = True

    # ---- ROUND 4 hardening: record the sha256 of the two external anchor files
    # (required_runtime_files.json, top_level_py_inventory.json) at the START of this
    # run -- exposed in the result for `verification.json` (R3-review's LOW note:
    # neither anchor's own hash was ever recorded anywhere). Re-checked at the END of
    # this same function (section "6" below) so an external process mutating either
    # anchor MID-RUN fails closed rather than silently going undetected. This is
    # local hash consistency ONLY -- it is not a cryptographic authenticity/signature
    # check, and does not by itself resolve the P11 baseline-provenance class (see the
    # module docstring / R4 report 07).
    def _anchor_sha_or_none(path: Path):
        try:
            return _sha256_path(path)
        except OSError:
            return None

    anchor_hashes_start = {
        "required_runtime_files.json": _anchor_sha_or_none(CANONICAL_REQUIRED_FILES_PATH),
        "top_level_py_inventory.json": _anchor_sha_or_none(TOP_LEVEL_INVENTORY_PATH),
    }

    # ---- 0. baseline structural integrity -------------------------------------------
    try:
        baseline = load_baseline(baseline_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False, "rows": [{"file": "<baseline>", "status": "BASELINE_UNREADABLE_OR_CORRUPT",
                                   "ok": False, "baseline_sha256": None, "live_sha256": None,
                                   "delta": f"{type(exc).__name__}: {exc}"}],
            "allowed_correction_changes": [], "required_runtime_files": sorted(REQUIRED_RUNTIME_FILES),
            "missing_from_baseline": sorted(REQUIRED_RUNTIME_FILES),
            "extra_in_baseline_not_required": [], "unclassified_extra_baseline_keys": [],
            "extra_files_in_base_dir": [], "undeclared_new_py_files": [],
            "inventory_mismatch_vs_canonical": None, "case_duplicate_baseline_keys": [],
            "enforce_no_extra_files": enforce_no_extra_files,
            "anchor_file_sha256": anchor_hashes_start, "anchor_file_changed_during_run": False,
        }

    missing_top_level = [k for k in REQUIRED_BASELINE_TOP_LEVEL_KEYS if k not in baseline]
    if missing_top_level:
        rows.append({"file": "<baseline>", "status": "PARTIAL_BASELINE_MISSING_TOP_LEVEL_KEYS",
                    "ok": False, "baseline_sha256": None, "live_sha256": None,
                    "delta": f"missing keys: {missing_top_level}"})
        ok = False

    allowed = set(baseline.get("allowed_correction_changes", []) or [])
    baseline_hashes = baseline.get("baseline_sha256", {}) or {}

    # ---- 0b. case-normalized duplicate keys in the baseline's own map ---------------
    lower_keys = [k.lower() for k in baseline_hashes]
    case_dupes = sorted({k for k in lower_keys if lower_keys.count(k) > 1})
    if case_dupes:
        rows.append({"file": "<baseline>", "status": "CASE_DUPLICATE_BASELINE_KEY", "ok": False,
                    "baseline_sha256": None, "live_sha256": None, "delta": str(case_dupes)})
        ok = False

    # ---- 1. REQUIRED_RUNTIME_FILES vs the external canonical list (fail-closed on a
    # shrunk/edited in-module constant, either direction) --------------------------
    try:
        # Reads the module-level global by bare name, resolved AT CALL TIME (not
        # bound once as a stale function-default) -- so a monkeypatch of
        # CANONICAL_REQUIRED_FILES_PATH (or a real edit to the file it points at)
        # is honored on the next call, matching how `baseline_path` is threaded
        # explicitly everywhere else in this function.
        canonical = load_canonical_required_files(CANONICAL_REQUIRED_FILES_PATH)
        inventory_mismatch = sorted(REQUIRED_RUNTIME_FILES) != list(canonical)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        canonical = None
        inventory_mismatch = f"CANONICAL_INVENTORY_UNREADABLE: {type(exc).__name__}: {exc}"
    if inventory_mismatch:
        rows.append({"file": "<REQUIRED_RUNTIME_FILES>", "status": "INVENTORY_MISMATCH_VS_CANONICAL",
                    "ok": False, "baseline_sha256": None, "live_sha256": None,
                    "delta": f"module={sorted(REQUIRED_RUNTIME_FILES)} canonical={canonical}"})
        ok = False

    required = set(REQUIRED_RUNTIME_FILES)

    try:
        real_names = set(os.listdir(base_dir))
        listdir_error = None
    except OSError as exc:
        real_names = set()
        listdir_error = str(exc)
    if listdir_error is not None:
        rows.append({"file": "<base_dir>", "status": "LISTDIR_ERROR", "ok": False,
                    "baseline_sha256": None, "live_sha256": None, "delta": listdir_error})
        ok = False

    # ---- 2. per-required-file hash/diff boundary -------------------------------------
    for fname in sorted(required):
        base_hash = baseline_hashes.get(fname)
        if base_hash is None:
            rows.append({"file": fname, "status": "MISSING_BASELINE_ENTRY", "ok": False,
                        "baseline_sha256": None, "live_sha256": None,
                        "delta": "required runtime file has no baseline entry -- fail closed"})
            ok = False
            continue
        if fname not in real_names:
            rows.append({"file": fname, "status": "MISSING_RUNTIME_FILE", "ok": False,
                        "baseline_sha256": base_hash, "live_sha256": None,
                        "delta": "required runtime file not found by exact-case name in "
                                "base_dir's directory listing"})
            ok = False
            continue
        p = base_dir / fname
        live_hash = _sha256_path(p)
        if fname in allowed:
            row_ok = live_hash != base_hash
            status = "CHANGED_AS_EXPECTED" if row_ok else "UNCHANGED_BUT_ALLOWED_FILE_SHOULD_DIFFER"
        else:
            row_ok = live_hash == base_hash
            status = "UNCHANGED_AS_REQUIRED" if row_ok else "UNDECLARED_CHANGE"
        rows.append({"file": fname, "status": status, "ok": row_ok,
                     "baseline_sha256": base_hash, "live_sha256": live_hash,
                     "delta": "no change" if base_hash == live_hash
                     else f"{base_hash[:12]}->{live_hash[:12]}"})
        ok = ok and row_ok

    # ---- 3. extra/unclassified baseline keys (fail-closed, section 9) ---------------
    extra_in_baseline = sorted(set(baseline_hashes) - required)
    unclassified_extra = sorted(set(extra_in_baseline) - set(KNOWN_NON_REQUIRED_BASELINE_KEYS))
    if unclassified_extra:
        rows.append({"file": "<baseline>", "status": "UNCLASSIFIED_EXTRA_BASELINE_KEY", "ok": False,
                    "baseline_sha256": None, "live_sha256": None, "delta": str(unclassified_extra)})
        ok = False

    # ---- 4. UNCONDITIONAL new-.py-file detection (no permissive default, section 9) -
    undeclared_new = []
    if listdir_error is None:
        try:
            # Same call-time (not def-time) resolution as CANONICAL_REQUIRED_FILES_PATH
            # above -- TOP_LEVEL_INVENTORY_PATH is read fresh here, not via a stale
            # bound default.
            known_top_level = load_known_top_level_py_files(TOP_LEVEL_INVENTORY_PATH)
            inventory_readable = True
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            known_top_level = set()
            inventory_readable = False
            rows.append({"file": "<top_level_inventory>", "status": "TOP_LEVEL_INVENTORY_UNREADABLE",
                        "ok": False, "baseline_sha256": None, "live_sha256": None,
                        "delta": f"{type(exc).__name__}: {exc}"})
            ok = False
        if inventory_readable:
            for name in sorted(real_names):
                if name.endswith(".py") and name not in required and name not in known_top_level:
                    undeclared_new.append(name)
                    rows.append({"file": name, "status": "UNDECLARED_NEW_PY_FILE", "ok": False,
                                "baseline_sha256": None, "live_sha256": None,
                                "delta": "not in REQUIRED_RUNTIME_FILES nor in the frozen "
                                        "top-level inventory snapshot"})
                    ok = False

    # ---- 5. narrower opt-in tier (unchanged from round 2, purpose-built temp trees) -
    extra_rows = []
    if enforce_no_extra_files and listdir_error is None:
        for name in sorted(real_names):
            if name.endswith(".py") and name not in required:
                extra_rows.append({"file": name, "status": "EXTRA_RUNTIME_FILE", "ok": False})
                ok = False

    # ---- 6. ROUND 4: anchor-file integrity -- re-hash both external anchors and
    # compare against the values recorded at the START of this run. A mismatch means
    # something mutated one of the guard's own trust anchors WHILE this run was in
    # flight -- fails closed rather than silently using stale in-memory data read
    # earlier in this same call.
    anchor_hashes_end = {
        "required_runtime_files.json": _anchor_sha_or_none(CANONICAL_REQUIRED_FILES_PATH),
        "top_level_py_inventory.json": _anchor_sha_or_none(TOP_LEVEL_INVENTORY_PATH),
    }
    anchor_changed = anchor_hashes_start != anchor_hashes_end
    if anchor_changed:
        rows.append({"file": "<anchor_files>", "status": "ANCHOR_FILE_CHANGED_DURING_RUN",
                    "ok": False, "baseline_sha256": None, "live_sha256": None,
                    "delta": f"start={anchor_hashes_start} end={anchor_hashes_end}"})
        ok = False

    return {
        "ok": ok, "allowed_correction_changes": sorted(allowed), "rows": rows,
        "required_runtime_files": sorted(required),
        "missing_from_baseline": sorted(required - set(baseline_hashes)),
        "extra_in_baseline_not_required": extra_in_baseline,
        "unclassified_extra_baseline_keys": unclassified_extra,
        "case_duplicate_baseline_keys": case_dupes,
        "inventory_mismatch_vs_canonical": inventory_mismatch,
        "undeclared_new_py_files": undeclared_new,
        "extra_files_in_base_dir": extra_rows,
        "enforce_no_extra_files": enforce_no_extra_files,
        "anchor_file_sha256": anchor_hashes_end,
        "anchor_file_changed_during_run": anchor_changed,
    }


def run_marker_scan(base_dir: Path = BASE_DIR) -> dict:
    """SECONDARY check: no W3.2 or W3.2-correction marker leaks into an out-of-scope
    file. Kept as defense-in-depth, never the sole proof (see F-5)."""
    W32_MARKERS = ("dst_gap_snapped", "dst_fold_ambiguous", "_w3_snap_dst_gap",
                   "_w3_classify_local")
    W32C_MARKERS = ("W3.2 correction (TZ-3", "_ph_w3_now", "_ti_w3_now", "_tvnl_w3_now")
    OUT_OF_SCOPE_FILES = [
        "manager_bot.py", "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py",
        "manager_registry.py", "soft_watchdog_pinger.py", "health_server.py",
        "proxy_provider.py", "proxy_parser.py", "router.py", "profile_extractor.py",
        "profile_dialog.py", "profile_texts.py", "post_followup_texts.py", "texts.py",
    ]
    rows = []
    ok = True
    for fname in OUT_OF_SCOPE_FILES:
        p = base_dir / fname
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8-sig")
        hits = scan_text_for_markers(src, W32_MARKERS + W32C_MARKERS)
        row_ok = not hits
        rows.append({"file": fname, "ok": row_ok, "marker_hits": hits})
        ok = ok and row_ok
    return {"ok": ok, "rows": rows}


def run_scope_guard(base_dir: Path = BASE_DIR, baseline_path: Path = BASELINE_PATH,
                     enforce_no_extra_files: bool = False) -> dict:
    """Combined result, both signals independently inspectable. Calling this with NO
    arguments (the CLI's own default) exercises every fail-closed check in
    `run_hash_diff_boundary` -- section 9's "invoke gate with default arguments only ->
    all critical checks still active" (SG-R3-3) is this exact call shape."""
    hash_result = run_hash_diff_boundary(base_dir, baseline_path, enforce_no_extra_files)
    marker_result = run_marker_scan(base_dir)
    return {
        "hash_diff_boundary": hash_result,
        "marker_scan": marker_result,
        "hash_diff_boundary_ok": hash_result["ok"],
        "marker_scan_ok": marker_result["ok"],
        "ok": hash_result["ok"] and marker_result["ok"],
    }


def main() -> int:
    # ---- PRIMARY: hash/diff boundary against the frozen baseline -----------------
    guard = run_scope_guard()
    for row in guard["hash_diff_boundary"]["rows"]:
        check(f"hash-boundary: {row['file']}: {row['status']}", row["ok"], row.get("delta") or "")
    check("hash-boundary: overall (undeclared changes detected regardless of markers)",
          guard["hash_diff_boundary_ok"])
    check("hash-boundary: baseline artifact covers every required runtime file "
          "(fail-closed on a dropped baseline entry)",
          not guard["hash_diff_boundary"]["missing_from_baseline"],
          str(guard["hash_diff_boundary"]["missing_from_baseline"]))
    check("hash-boundary: REQUIRED_RUNTIME_FILES matches the external canonical list",
          not guard["hash_diff_boundary"]["inventory_mismatch_vs_canonical"],
          str(guard["hash_diff_boundary"]["inventory_mismatch_vs_canonical"]))
    check("hash-boundary: no unclassified extra baseline key",
          not guard["hash_diff_boundary"]["unclassified_extra_baseline_keys"],
          str(guard["hash_diff_boundary"]["unclassified_extra_baseline_keys"]))
    check("hash-boundary: no undeclared new .py file in base_dir (unconditional, no "
          "permissive default)",
          not guard["hash_diff_boundary"]["undeclared_new_py_files"],
          str(guard["hash_diff_boundary"]["undeclared_new_py_files"]))
    check("hash-boundary: neither external anchor file (required_runtime_files.json, "
          "top_level_py_inventory.json) changed during this run",
          not guard["hash_diff_boundary"].get("anchor_file_changed_during_run"),
          str(guard["hash_diff_boundary"].get("anchor_file_sha256")))
    print(f"       [anchor hashes] {guard['hash_diff_boundary'].get('anchor_file_sha256')}")

    # ---- SECONDARY: marker scan (retained, not sole proof) -----------------------
    for row in guard["marker_scan"]["rows"]:
        check(f"marker-scan: no W3.2/W3.2-correction marker in {row['file']}",
              row["ok"], str(row["marker_hits"]))

    # ---- allow_spend=True remains exactly 2 AST keyword nodes in main.py ---------
    main_src = (BASE_DIR / "main.py").read_text(encoding="utf-8-sig")
    main_tree = ast.parse(main_src, filename="main.py")
    allow_spend_true = [
        kw for node in ast.walk(main_tree) if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True
    ]
    check("main.py has exactly 2 AST keyword nodes allow_spend=True (unchanged)",
          len(allow_spend_true) == 2, str(len(allow_spend_true)))

    # ---- W1/W2/W3.1 markers remain present in storage.py -------------------------
    storage_src = (BASE_DIR / "storage.py").read_text(encoding="utf-8-sig")
    check("W2 banner 'TPILOT W2 ACCESS & DELIVERY' still present in storage.py",
          "TPILOT W2 ACCESS & DELIVERY" in storage_src)
    check("W3.1 resolver foundation marker still present in storage.py",
          "TPILOT W3.1 SCHEDULE RESOLVER FOUNDATION" in storage_src)
    check("storage.w3_resolve_schedule still defined exactly once",
          len([n for n in ast.walk(ast.parse(storage_src)) if isinstance(n, ast.FunctionDef)
               and n.name == "w3_resolve_schedule"]) == 1)

    # ---- W3.3-deferred symbols present and UNCHANGED (not redirected early) ------
    check("panel_bot.py: _sched_pb_kyiv_today unchanged (still bare ZoneInfo, W3.3 not started)",
          'def _sched_pb_kyiv_today():\n    return datetime.now(tz=ZoneInfo("Europe/Kyiv")).date()'
          in (BASE_DIR / "panel_bot.py").read_text(encoding="utf-8-sig"))
    mb_src = (BASE_DIR / "manager_bot.py").read_text(encoding="utf-8-sig")
    check("manager_bot.py: _MBSTAT_TZ unchanged (still its own ZoneInfo alias, W3.3 not started)",
          '_MBSTAT_TZ = _MBStatZoneInfo("Europe/Kyiv")' in mb_src)
    check("manager_bot.py: _SCHED_TZ_MB unchanged (still its own ZoneInfo alias, W3.3 not started)",
          '_SCHED_TZ_MB = _MBStatZoneInfo("Europe/Kyiv")' in mb_src)
    psb_src = (BASE_DIR / "partner_stat_bot.py").read_text(encoding="utf-8-sig")
    check("partner_stat_bot.py: TZ unchanged (still its own ZoneInfo, W3.3 not started)",
          'TZ = ZoneInfo("Europe/Kyiv")' in psb_src)
    check("partner_stat_bot.py: _kyiv_now unchanged (still its own def, W3.3 not started)",
          "def _kyiv_now() -> datetime:\n    return datetime.now(TZ)" in psb_src)

    # F-3 explicitly deferred to W3.4: main._tp_pss_parse_date_token must NOT have been
    # touched (out of scope). Compared against the frozen baseline's own main.py entry.
    baseline_for_main = load_baseline()["baseline_sha256"]["main.py"]
    check("main.py: byte-identical to its W3.2 (pre-correction) state (F-3 "
          "correctly deferred to W3.4, not touched by this correction)",
          baseline_for_main == _sha256_path(BASE_DIR / "main.py"))

    # ---- empty w3_* tables remain inert / no versioned scheduling activated ------
    for fname in ("main.py", "panel_bot.py", "preflight_check.py"):
        src = (BASE_DIR / fname).read_text(encoding="utf-8-sig")
        check(f"{fname}: no w3_resolve_schedule call introduced (C1/C2 readers not redirected)",
              "w3_resolve_schedule(" not in src)
        check(f"{fname}: no activation_mode override introduced", "activation_mode=" not in src)

    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all scope/static guards green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
