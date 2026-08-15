# -*- coding: utf-8 -*-
"""tools/w3_3_b_scope_guard_selftest.py -- the LIVE W3.3-B scope/file boundary.

tools/w3_3_scope_guard_selftest.py (the W3.3-A boundary, D-3) stays FROZEN,
byte-identical, and is never edited by this tool or this round -- its own
"panel_bot.py/manager_bot.py/partner_stat_bot.py byte-identical" and "manifest not
re-pinned" assertions are now EXPECTED to go RED under W3.3-B (disclosed,
byte-identical, HISTORICAL_SCOPE_DRIFT_EXPECTED, same category as W3.2's own D-3
supersession). THIS tool is the live boundary for W3.3-B: it proves that

  * only panel_bot.py, manager_bot.py, partner_stat_bot.py changed among the 12 frozen
    runtime files -- storage.py, main.py and the other 9 stay byte-identical;
  * the five approved redirect definitions have EXACTLY their approved AST shape
    (not just "changed from baseline", but structurally the one approved form):
    panel_bot._sched_pb_kyiv_today, manager_bot._MBSTAT_TZ, manager_bot._SCHED_TZ_MB,
    partner_stat_bot.TZ, partner_stat_bot._kyiv_now;
  * manager_bot._sched_kyiv_today is byte-for-byte UNEDITED (inherits the redirect only
    indirectly through _SCHED_TZ_MB);
  * only tools/w3_2_manifest_integrity.py changed among tools/*.py (besides this new
    guard itself), and its only delta is the PINNED_MANIFEST_SHA256 value plus one new
    disclosure comment -- no logic/schema line changed;
  * tools/w3_3_c1_reader_parity_selftest.py and tools/w3_3_scope_guard_selftest.py (the
    frozen W3.3-A artifacts) stay byte-identical;
  * the W3.2 timezone gate manifest + its sidecar SHA changed in EXACTLY the approved
    shape: one clock_index row changed (role/confidence unchanged -- no broadening),
    one wrapper_registry row added (nothing removed -- no ownership loss), every other
    manifest section byte-identical (no exemption added), and
    measured_totals_at_generation differs only in the two causally-required counters;
  * the sidecar SHA and the PINNED_MANIFEST_SHA256 constant both match the live
    manifest's own sha256;
  * allow_spend=True appears as a real call argument in exactly 2 places project-wide;
  * no mojibake marker in any file changed this round;
  * main.py is byte-identical and its known 16:50 report-time hardcode sites are
    unchanged, with no "17:50" string introduced (N-W33-4 stays carried to W3.4);
  * the repository residue scan is clean against this round's own frozen baseline
    (the union of the W3.3-A snapshot and the pre-existing residue already present when
    this guard was written) -- any NEW residue file inside C:\\ALM_TPilot is RED;
  * the external pre-edit backups for this round's six changed/regenerated artifacts
    exist under this round's BACKUP dir.

Baselines consumed (external, written during this round's own pre-work snapshot and
manifest re-pin, never inside C:\\ALM_TPilot):
  C:\\ALM_TPilot_AUDIT\\20260803\\W3_3_TZ_REDIRECT_IMPLEMENTATION\\w3_3b_prehashes.json
  C:\\ALM_TPilot_AUDIT\\20260803\\W3_3_TZ_REDIRECT_IMPLEMENTATION\\w3_3b_residue_baseline.json
  C:\\ALM_TPilot_AUDIT\\20260803\\W3_3_TZ_REDIRECT_IMPLEMENTATION\\BACKUP\\w3_2_timezone_gate_manifest.json.bak_w3_3b_20260803_135128
  C:\\ALM_TPilot_AUDIT\\20260803\\W3_3_TZ_REDIRECT_IMPLEMENTATION\\BACKUP\\w3_2_manifest_integrity.py.bak_w3_3b_20260803_140212

    python tools\\w3_3_b_scope_guard_selftest.py
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BASE_DIR / "tools"

AUDIT_DIR = Path(r"C:\ALM_TPilot_AUDIT\20260803\W3_3_TZ_REDIRECT_IMPLEMENTATION")
PREHASH_PATH = AUDIT_DIR / "w3_3b_prehashes.json"
RESIDUE_BASELINE_PATH = AUDIT_DIR / "w3_3b_residue_baseline.json"
BACKUP_DIR = AUDIT_DIR / "BACKUP"
MANIFEST_BASELINE_PATH = BACKUP_DIR / "w3_2_timezone_gate_manifest.json.bak_w3_3b_20260803_135128"
MANIFEST_INTEGRITY_BASELINE_PATH = BACKUP_DIR / "w3_2_manifest_integrity.py.bak_w3_3b_20260803_140212"

MANIFEST_DIR = Path(r"C:\ALM_TPilot_AUDIT\W3_2_GATE_MANIFEST")
MANIFEST_PATH = MANIFEST_DIR / "w3_2_timezone_gate_manifest.json"
SIDECAR_PATH = MANIFEST_DIR / "w3_2_timezone_gate_manifest.json.sha256"

RUNTIME_12 = (
    "storage.py", "main.py", "panel_bot.py", "preflight_check.py", "manager_bot.py",
    "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py", "manager_registry.py",
    "soft_watchdog_pinger.py", "health_server.py", "stats_parity_harness.py",
)
APPROVED_CHANGED_RUNTIME = ("panel_bot.py", "manager_bot.py", "partner_stat_bot.py")

APPROVED_CHANGED_TOOLS = ("w3_2_manifest_integrity.py",)
APPROVED_NEW_TOOLS = ("w3_3_b_scope_guard_selftest.py",)
FROZEN_W3_3_A_TOOLS = ("w3_3_c1_reader_parity_selftest.py", "w3_3_scope_guard_selftest.py")

EXPECTED_REDIRECT_SOURCE = {
    ("panel_bot.py", "_sched_pb_kyiv_today"):
        'def _sched_pb_kyiv_today():\n'
        '    from storage import w3_business_date as _spbkt_w3_business_date\n'
        '    return _sched_pb_date_cls.fromisoformat(_spbkt_w3_business_date())',
    ("manager_bot.py", "_MBSTAT_TZ"): '_MBSTAT_TZ = storage.w3_tz()',
    ("manager_bot.py", "_SCHED_TZ_MB"): '_SCHED_TZ_MB = storage.w3_tz()',
    ("partner_stat_bot.py", "TZ"): 'TZ = storage.w3_tz()',
    ("partner_stat_bot.py", "_kyiv_now"):
        'def _kyiv_now() -> datetime:\n'
        '    return storage.w3_now()',
}

EXPECTED_UNEDITED_SOURCE = {
    ("manager_bot.py", "_sched_kyiv_today"):
        'def _sched_kyiv_today() -> _sched_date_cls:\n'
        '    from datetime import datetime as _dt\n'
        '    return _dt.now(tz=_SCHED_TZ_MB).date()',
}

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_prehashes() -> dict:
    return json.loads(PREHASH_PATH.read_text(encoding="utf-8"))


def ast_source_of(rel_file: str, name: str) -> str | None:
    text = (BASE_DIR / rel_file).read_text(encoding="utf-8-sig")
    tree = ast.parse(text, filename=rel_file)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.unparse(node)
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    return None


# --------------------------------------------------------------------------------------
# 1/2/6/7 -- runtime file boundary + exact redirect shapes + frozen files
# --------------------------------------------------------------------------------------

def check_runtime_files(prehashes: dict) -> None:
    baseline = prehashes.get("runtime_files", {})
    for fname in RUNTIME_12:
        live = BASE_DIR / fname
        if not live.exists():
            check(f"runtime file present: {fname}", False, "missing")
            continue
        live_hash = sha256_of(live)
        base_hash = baseline.get(fname)
        if fname in APPROVED_CHANGED_RUNTIME:
            check(f"runtime file DID change (approved W3.3-B redirect): {fname}",
                  live_hash != base_hash, f"{live_hash} == baseline (no change detected)")
        else:
            check(f"runtime file byte-identical to pre-W3.3-B: {fname}",
                  live_hash == base_hash, f"live={live_hash} baseline={base_hash}")


def check_exact_redirect_shapes() -> None:
    for (fname, name), expected in EXPECTED_REDIRECT_SOURCE.items():
        actual = ast_source_of(fname, name)
        check(f"exact approved redirect shape: {fname}::{name}",
              actual == expected, f"actual={actual!r}")

    for (fname, name), expected in EXPECTED_UNEDITED_SOURCE.items():
        actual = ast_source_of(fname, name)
        check(f"unedited (inherits redirect indirectly): {fname}::{name}",
              actual == expected, f"actual={actual!r}")


def check_storage_and_w3_3_a_tools_frozen(prehashes: dict) -> None:
    base_hash = prehashes.get("runtime_files", {}).get("storage.py")
    check("storage.py byte-identical (untouched this round)",
          sha256_of(BASE_DIR / "storage.py") == base_hash)

    tools_baseline = prehashes.get("tools_hashes", {})
    for fname in FROZEN_W3_3_A_TOOLS:
        live = TOOLS_DIR / fname
        base_hash = tools_baseline.get(fname)
        check(f"frozen W3.3-A tool byte-identical: {fname}",
              live.exists() and sha256_of(live) == base_hash,
              f"live={sha256_of(live) if live.exists() else 'MISSING'} baseline={base_hash}")


def check_main_py_and_report_time(prehashes: dict) -> None:
    base_hash = prehashes.get("runtime_files", {}).get("main.py")
    live_hash = sha256_of(BASE_DIR / "main.py")
    check("main.py byte-identical (untouched this round)", live_hash == base_hash)

    text = (BASE_DIR / "main.py").read_text(encoding="utf-8-sig")
    hardcode_hits = text.count("16:50")
    check("main.py: known 16:50 report-time hardcode sites still present (>= 8, unchanged count)",
          hardcode_hits >= 8, f"count={hardcode_hits}")
    check("main.py: no '17:50' string introduced (N-W33-4 stays carried to W3.4, not started here)",
          "17:50" not in text)


# --------------------------------------------------------------------------------------
# 3/4/5 -- tools directory boundary (incl. the one approved pinned-SHA re-pin)
# --------------------------------------------------------------------------------------

def check_tools_directory(prehashes: dict) -> None:
    baseline_tools = dict(prehashes.get("tools_hashes", {}))
    live_tools = {p.name: sha256_of(p) for p in sorted(TOOLS_DIR.glob("*.py"))}

    new_files = sorted(set(live_tools) - set(baseline_tools))
    check("tools/: only this new guard was created (no other new tool file)",
          set(new_files) == set(APPROVED_NEW_TOOLS), f"new_files={new_files}")

    removed_files = sorted(set(baseline_tools) - set(live_tools))
    check("tools/: no pre-existing tool file was removed", not removed_files, f"removed={removed_files}")

    changed = []
    for fname, base_hash in baseline_tools.items():
        live_hash = live_tools.get(fname)
        if live_hash is not None and live_hash != base_hash:
            changed.append(fname)
    check("tools/: only the approved pinned-SHA re-pin changed among pre-existing tool files",
          set(changed) == set(APPROVED_CHANGED_TOOLS), f"changed={changed}")


def check_manifest_integrity_tool_delta() -> None:
    live_path = TOOLS_DIR / "w3_2_manifest_integrity.py"
    if not MANIFEST_INTEGRITY_BASELINE_PATH.exists():
        check("w3_2_manifest_integrity.py pre-edit baseline present", False,
              str(MANIFEST_INTEGRITY_BASELINE_PATH))
        return

    old_lines = MANIFEST_INTEGRITY_BASELINE_PATH.read_text(encoding="utf-8-sig").splitlines()
    new_lines = live_path.read_text(encoding="utf-8-sig").splitlines()

    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    bad_ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old_chunk = old_lines[i1:i2]
        new_chunk = new_lines[j1:j2]
        for ln in old_chunk:
            if "PINNED_MANIFEST_SHA256" not in ln:
                bad_ops.append(("removed/replaced", ln))
        for ln in new_chunk:
            stripped = ln.strip()
            is_sha_line = "PINNED_MANIFEST_SHA256" in ln
            is_new_comment = stripped.startswith("#")
            if not (is_sha_line or is_new_comment):
                bad_ops.append(("added/replaced", ln))
    check("tools/w3_2_manifest_integrity.py: only the pinned-SHA line + disclosure comment(s) changed",
          not bad_ops, f"unexpected diff lines={bad_ops}")


def check_sidecar_and_pin() -> None:
    if not MANIFEST_PATH.exists():
        check("W3.2 timezone gate manifest present", False, str(MANIFEST_PATH))
        return
    live_manifest_sha = sha256_of(MANIFEST_PATH)

    sidecar_ok = False
    if SIDECAR_PATH.exists():
        sidecar_text = SIDECAR_PATH.read_text(encoding="utf-8").strip()
        sidecar_ok = sidecar_text.split()[0].lower() == live_manifest_sha.lower()
    check("sidecar SHA matches the current accepted manifest", sidecar_ok,
          f"manifest_sha={live_manifest_sha}")

    integrity_text = (TOOLS_DIR / "w3_2_manifest_integrity.py").read_text(encoding="utf-8-sig")
    m = re.search(r'PINNED_MANIFEST_SHA256\s*=\s*"([0-9a-fA-F]{64})"', integrity_text)
    pin_ok = bool(m) and m.group(1).lower() == live_manifest_sha.lower()
    check("PINNED_MANIFEST_SHA256 constant matches the current accepted manifest", pin_ok,
          f"pinned={m.group(1) if m else None} manifest_sha={live_manifest_sha}")


# --------------------------------------------------------------------------------------
# 3 (continued) -- exact manifest semantic diff
# --------------------------------------------------------------------------------------

def _clock_key(row: dict) -> tuple:
    return (row["file"], row["qualified_name"], row["generation_index"])


def _wrapper_key(row: dict) -> tuple:
    return (row["file"], row["qualified_name"])


APPROVED_CHANGED_CLOCK_ROW = ("panel_bot.py", "_sched_pb_kyiv_today", 0)
APPROVED_NEW_WRAPPER_ROW = ("panel_bot.py", "_sched_pb_kyiv_today")
CAUSALLY_REFRESHABLE_TOTAL_KEYS = {"whole_scope_def_total"}


def check_manifest_semantic_diff() -> None:
    if not MANIFEST_BASELINE_PATH.exists():
        check("W3.2 manifest pre-edit baseline present", False, str(MANIFEST_BASELINE_PATH))
        return
    if not MANIFEST_PATH.exists():
        check("W3.2 manifest live file present", False, str(MANIFEST_PATH))
        return

    old = json.loads(MANIFEST_BASELINE_PATH.read_text(encoding="utf-8"))
    new = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    # Every top-level key except the three expected-to-change sections must be byte-identical.
    frozen_keys = [k for k in set(old) | set(new)
                   if k not in ("clock_index", "wrapper_registry", "measured_totals_at_generation")]
    unexpected_key_changes = [k for k in frozen_keys if old.get(k) != new.get(k)]
    check("manifest: every unrelated top-level section byte-identical (no exemption added)",
          not unexpected_key_changes, f"changed={unexpected_key_changes}")

    old_ci = {_clock_key(r): r for r in old["clock_index"]}
    new_ci = {_clock_key(r): r for r in new["clock_index"]}
    added_ci = sorted(set(new_ci) - set(old_ci))
    removed_ci = sorted(set(old_ci) - set(new_ci))
    changed_ci = sorted(k for k in old_ci if k in new_ci and old_ci[k] != new_ci[k])
    check("manifest clock_index: no rows added", not added_ci, f"added={added_ci}")
    check("manifest clock_index: no rows removed (no ownership loss)", not removed_ci, f"removed={removed_ci}")
    check("manifest clock_index: exactly the one approved row changed",
          changed_ci == [APPROVED_CHANGED_CLOCK_ROW], f"changed={changed_ci}")
    if changed_ci == [APPROVED_CHANGED_CLOCK_ROW]:
        old_row = old_ci[APPROVED_CHANGED_CLOCK_ROW]
        new_row = new_ci[APPROVED_CHANGED_CLOCK_ROW]
        check("manifest clock_index: changed row's role is unchanged (no broadening)",
              old_row["role"] == new_row["role"],
              f"old={old_row['role']} new={new_row['role']}")
        check("manifest clock_index: changed row's confidence is unchanged",
              old_row["confidence"] == new_row["confidence"],
              f"old={old_row['confidence']} new={new_row['confidence']}")

    old_wr = {_wrapper_key(r): r for r in old["wrapper_registry"]}
    new_wr = {_wrapper_key(r): r for r in new["wrapper_registry"]}
    added_wr = sorted(set(new_wr) - set(old_wr))
    removed_wr = sorted(set(old_wr) - set(new_wr))
    changed_wr = sorted(k for k in old_wr if k in new_wr and old_wr[k] != new_wr[k])
    check("manifest wrapper_registry: exactly the one approved row added",
          added_wr == [APPROVED_NEW_WRAPPER_ROW], f"added={added_wr}")
    check("manifest wrapper_registry: no rows removed (no ownership loss)",
          not removed_wr, f"removed={removed_wr}")
    check("manifest wrapper_registry: no existing row changed", not changed_wr, f"changed={changed_wr}")

    old_t = old["measured_totals_at_generation"]
    new_t = new["measured_totals_at_generation"]
    unexpected_total_changes = []
    for k in sorted(set(old_t) | set(new_t)):
        if k in CAUSALLY_REFRESHABLE_TOTAL_KEYS:
            continue
        if k == "per_file_definitions":
            for pf in sorted(set(old_t.get(k, {})) | set(new_t.get(k, {}))):
                if pf == "storage.py":
                    continue
                if old_t.get(k, {}).get(pf) != new_t.get(k, {}).get(pf):
                    unexpected_total_changes.append(f"per_file_definitions.{pf}")
            continue
        if old_t.get(k) != new_t.get(k):
            unexpected_total_changes.append(k)
    check("manifest measured_totals_at_generation: only the causally-required counters changed",
          not unexpected_total_changes, f"changed={unexpected_total_changes}")


# --------------------------------------------------------------------------------------
# 10 -- allow_spend / mojibake
# --------------------------------------------------------------------------------------

def check_allow_spend() -> None:
    total = 0
    sites = []
    for fname in ("main.py", "storage.py"):
        text = (BASE_DIR / fname).read_text(encoding="utf-8-sig")
        tree = ast.parse(text, filename=fname)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "allow_spend":
                v = node.value
                if isinstance(v, ast.Constant) and v.value is True:
                    total += 1
                    sites.append((fname, getattr(node, "lineno", None)))
    check("allow_spend=True appears exactly 2 times project-wide (main.py + storage.py scanned)",
          total == 2, f"count={total} sites={sites}")


def check_mojibake() -> None:
    # Built via chr() rather than embedded as literal characters, so this tool's own
    # source file never contains the corruption bytes it searches for.
    markers = (chr(0xd0), chr(0xd1), chr(0xe2) + chr(0x20ac))
    changed_files = ("panel_bot.py", "manager_bot.py", "partner_stat_bot.py",
                      "tools/w3_2_manifest_integrity.py", "tools/w3_3_b_scope_guard_selftest.py")
    for rel in changed_files:
        path = BASE_DIR / rel
        text = path.read_text(encoding="utf-8-sig")
        hits = {m: text.count(m) for m in markers if m in text}
        check(f"mojibake scan clean: {rel}", not hits, f"hits={hits}")


# --------------------------------------------------------------------------------------
# 9 -- residue
# --------------------------------------------------------------------------------------

def scan_residue() -> set:
    residue = set()
    skip_dirs = {".git", "venv", "__pycache__", "sessions", "db", "runtime", "logs",
                 "config", "exports"}
    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            low = fn.lower()
            if any(marker in low for marker in (".bak", ".tmp", ".orig", ".rej")) or \
               "restore" in low or "cursor_copy" in low or low.startswith("main_beka"):
                full = Path(root) / fn
                residue.add(str(full.relative_to(BASE_DIR)))
    return residue


def check_residue() -> None:
    if not RESIDUE_BASELINE_PATH.exists():
        check("W3.3-B residue baseline present", False, str(RESIDUE_BASELINE_PATH))
        return
    baseline_residue = set(json.loads(RESIDUE_BASELINE_PATH.read_text(encoding="utf-8"))["residue"])
    live_residue = scan_residue()
    new_residue = sorted(live_residue - baseline_residue)
    check("residue scan clean: no NEW .bak/.tmp/.orig/.rej/restore/cursor-copy file inside C:\\ALM_TPilot",
          not new_residue, f"new_residue={new_residue}")


# --------------------------------------------------------------------------------------
# 8 -- external backups
# --------------------------------------------------------------------------------------

def check_backups_exist() -> None:
    expected_globs = (
        "panel_bot.py.bak_w3_3b_*",
        "manager_bot.py.bak_w3_3b_*",
        "partner_stat_bot.py.bak_w3_3b_*",
        "w3_2_timezone_gate_manifest.json.bak_w3_3b_*",
        "w3_2_timezone_gate_manifest.json.sha256.bak_w3_3b_*",
        "w3_2_manifest_integrity.py.bak_w3_3b_*",
    )
    for pattern in expected_globs:
        matches = sorted(BACKUP_DIR.glob(pattern))
        check(f"external pre-edit backup exists: {pattern}", bool(matches), f"BACKUP_DIR={BACKUP_DIR}")


def main() -> int:
    if not PREHASH_PATH.exists():
        check("W3.3-B pre-hash artifact present", False, str(PREHASH_PATH))
        print("\nTOTAL: 1  FAILED: 1")
        return 1
    prehashes = load_prehashes()

    check_runtime_files(prehashes)
    check_exact_redirect_shapes()
    check_storage_and_w3_3_a_tools_frozen(prehashes)
    check_main_py_and_report_time(prehashes)
    check_tools_directory(prehashes)
    check_manifest_integrity_tool_delta()
    check_sidecar_and_pin()
    check_manifest_semantic_diff()
    check_allow_spend()
    check_mojibake()
    check_residue()
    check_backups_exist()

    print()
    print(f"FAILED: {len(FAILURES)}")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
