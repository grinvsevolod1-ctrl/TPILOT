# -*- coding: utf-8 -*-
"""tools/w3_4_scope_guard_selftest.py -- W3.4-C guard and PRE-baseline hardening.

CORRECTION R2 (2026-08-04). The W3.4-C round frozen at
C:\\ALM_TPilot_AUDIT\\20260803\\W3_4_C_GUARD_HARDENING\\ was independently reviewed at
C:\\ALM_TPilot_AUDIT\\20260804\\W3_4_C_INDEPENDENT_REVIEW\\00_INDEPENDENT_REVIEW_VERDICT.md
with disposition **FAIL** (evidence-integrity and plan-conformance defects B-1..B-9;
the guard's own *protective behaviour* was never found unsound except B-4). This file is
the R2 correction: same scope (guard + PRE-baseline hardening only, zero runtime changes),
now pointed at a freshly generated PRE evidence line under
C:\\ALM_TPilot_AUDIT\\20260804\\W3_4_C_CORRECTION_R2\\W3_4_PRE\\.

R1_STATUS = FAIL
R1_SUPERSEDED_BY = W3_4_C_CORRECTION_R2
R1 evidence (the 20260803 directory) is left byte-identical and is never rewritten by
this file or by anything in R2 -- see C:\\ALM_TPilot_AUDIT\\20260804\\
W3_4_C_CORRECTION_R2\\REPORTS\\ for the itemised correction of every R1 defect.

Authoritative plan: C:\\Users\\PROFESSOR\\.claude\\plans\\model-opus-5-mode-sorted-pizza.md
(section 13, "W3.4-C -- allowed"; section 6/11 discovery; sections 7-12 guard specs
N-5, N-R4..N-R7). Owner-frozen phase order is C -> A -> B, one coupled release.

THIS ROUND (W3.4-C) is guard/PRE-baseline work only. Zero runtime changes. It creates:

  * this file (the only new tool this round);
  * C:\\ALM_TPilot_AUDIT\\20260804\\W3_4_C_CORRECTION_R2\\W3_4_PRE\\*.json (+ sidecar) --
    an immutable pre-W3.4 authorization baseline of the UNTOUCHED tree, never rewritten
    by W3.4-A or W3.4-B.

What this guard proves (against the frozen PRE baseline, never against a self-authored
"current state" fallback):

  N-0   guard self-integrity: this file's own bytes must match a pinned reference hash
        frozen at R2 authoring time (GUARD_SELF_INTEGRITY_FAILURE) -- checked BEFORE the
        digest anchor, because nothing computed by a tampered guard can be trusted.
  N-5   triple digest anchor (baseline artifacts -> digest map -> pinned sidecar
        constant in this file) plus PRE-baseline schema validity (incl. rejection of
        unknown keys -- BASELINE_SCHEMA_UNKNOWN_KEY -- and of bool-as-int type confusion)
        and tool-boundary partition disjointness/coverage, incl. BOUNDARY_ALLOWLIST_
        BROADENED (the live APPROVED_BOUNDARY_FILES constant cross-checked against an
        anchor-protected pinned copy).
  N-R4  no duplicate/overlapping ACTIVE (module top-level tree.body only, never
        unrestricted ast.walk) definition for any name whose count is recorded in the
        PRE baseline's active_def_counts -- with special shape checks for the explicit
        W3.4-protected single-definition symbols (report-time functions/constants and
        the W3.3-B timezone redirects) -- PLUS PROTECTED_NAME_SHADOWED: a protected
        symbol name may never be (re)bound by a module-level ClassDef or by an
        Import/ImportFrom alias, independent of runtime byte-identity.
  N-R5  every pinned W3.2 manifest clock_index row's role/confidence/permitted_forms/
        detected_forms/contract_note stays byte-exact; NEVER_AUTO_PERMIT whitelist is
        exactly the pinned set (never broadened).
  N-R6  root-level *.py inventory and the recursive tools\\ inventory (*.py, non-*.py,
        subdirectories) match the PRE baseline exactly -- additions, removals and
        content drift are all rejected.
  N-R7  the residue baseline carries no bare-basename entries (forward-slash OR
        backslash), uses the pinned skip_dirs contract (drift checked against an
        independently in-code pinned copy, not a self-comparison), and no NEW repository
        residue (root-level or nested) has appeared since the PRE snapshot.
  N-F3  f3_fingerprint_pre is actively re-derived from live main.py source
        (sha256(ast.unparse(...))) and compared, not merely schema-typed
        (F3_PRE_FINGERPRINT_DRIFT).

Plus: all 12 frozen runtime files stay byte-identical; every pre-existing tools\\*.py
file stays byte-identical (only this new guard was created); allow_spend=True AST
count stays exactly 2 (both in main.py, scanned across main.py + storage.py);
mojibake scan clean; historical guards + the historical regression log stay
byte-identical (byte-for-byte, including its own recorded FAILED: 6 -- that log is NOT
re-run here, only hash-pinned); PRE_BASELINE_MUTATED is checked explicitly (a named,
non-tautological hash comparison of w3_4_pre_baseline.json against its PRE-time digest,
in addition to the general BASELINE_ARTIFACT_TAMPERED per-artifact loop).

Phase-state support: this round is C. The RELEASE baseline
(W3_4_RELEASE\\w3_4_release_baseline.json) is expected ABSENT and its absence is valid
-- this guard never fabricates one. A future W3.4-B run of this same file is expected
to find it present and check derived_from_pre; that logic is inert (not exercised) in
the C state and is clearly marked below.

Read-only. No DB, no network, no server, no runtime import, no Telegram.

    python tools\\w3_4_scope_guard_selftest.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BASE_DIR / "tools"

# R1 (FAILED independent review, left byte-identical, never read by this file):
#   C:\ALM_TPilot_AUDIT\20260803\W3_4_C_GUARD_HARDENING\
AUDIT_ROOT = Path(r"C:\ALM_TPilot_AUDIT\20260804\W3_4_C_CORRECTION_R2")
PRE_DIR = AUDIT_ROOT / "W3_4_PRE"
RELEASE_DIR = AUDIT_ROOT / "W3_4_RELEASE"

PRE_BASELINE_PATH = PRE_DIR / "w3_4_pre_baseline.json"
PRE_RESIDUE_BASELINE_PATH = PRE_DIR / "w3_4_pre_residue_baseline.json"
PRE_HISTORICAL_GUARDS_PATH = PRE_DIR / "w3_4_frozen_historical_guards.json"
PRE_DIGESTS_PATH = PRE_DIR / "w3_4_pre_artifact_digests.json"
PRE_DIGESTS_SIDECAR_PATH = PRE_DIR / "w3_4_pre_artifact_digests.json.sha256"
PRE_GUARD_SELF_SHA256_PATH = PRE_DIR / "w3_4_guard_self_sha256.json"
RELEASE_BASELINE_PATH = RELEASE_DIR / "w3_4_release_baseline.json"

MANIFEST_DIR = Path(r"C:\ALM_TPilot_AUDIT\W3_2_GATE_MANIFEST")
MANIFEST_PATH = MANIFEST_DIR / "w3_2_timezone_gate_manifest.json"

# Third anchor of the PRE triple: this constant must equal the sha256 of
# w3_4_pre_artifact_digests.json's own bytes. Pinned once, at PRE-creation time, from
# the sidecar this guard itself demanded exist alongside the digest map. A mismatch
# among {sidecar file, this constant, freshly computed digest} is BASELINE_ARTIFACT_TAMPERED
# and MUST hard-fail before any baseline content is trusted.
#
# NOTE ON BOOTSTRAP ORDER (R2): this constant is filled in LAST, after the three
# digest-anchored baseline artifacts are finalized -- see
# W3_4_C_CORRECTION_R2/REPORTS for the exact generation sequence. The guard's own
# self-integrity pin (PRE_GUARD_SELF_SHA256_PATH, checked by
# verify_guard_self_integrity) is intentionally a SEPARATE, non-nested anchor: it is
# computed from this file's bytes only after this constant's final value is written,
# so there is no circular self-hash dependency.
PINNED_PRE_DIGESTS_SHA256 = "5ac3bf8070cf7f1aab24c406ef7ca19b7e717190ca2c31300b9202af66d86807"

RUNTIME_12 = (
    "storage.py", "main.py", "panel_bot.py", "preflight_check.py", "manager_bot.py",
    "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py", "manager_registry.py",
    "soft_watchdog_pinger.py", "health_server.py", "stats_parity_harness.py",
)

# The only tools\*.py file this round is approved to create. Every other tools\*.py
# file (recorded in the PRE baseline's tools_inventory) must stay byte-identical --
# there is no "changed" partition in W3.4-C (no runtime changes => no tool content
# changes either), only "approved new" vs "everything else, frozen". Cross-checked at
# runtime against pre_baseline["approved_boundary_files_pinned"] (BOUNDARY_ALLOWLIST_
# BROADENED) so widening this set inside the guard is caught even if, hypothetically,
# the self-integrity check were bypassed.
APPROVED_BOUNDARY_FILES = frozenset({"w3_4_scope_guard_selftest.py"})

# Independently in-code pinned copy of the residue baseline's skip_dirs contract.
# verify_residue() compares the BASELINE FILE's skip_dirs against THIS constant --
# a real, non-tautological cross-artifact comparison (R1's version compared the value
# to itself: set(x) == set(x), always True -- B-4 in the independent review).
PINNED_SKIP_DIRS = frozenset({
    ".backups", ".git", "__pycache__", "config", "db", "exports", "logs",
    "runtime", "sessions", "venv", "venv_old_workcar6",
})

MOJIBAKE_MARKERS = (chr(0xd0), chr(0xd1), chr(0xe2) + chr(0x20ac))

FAILURES: list = []
TOTAL_CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> bool:
    global TOTAL_CHECKS
    TOTAL_CHECKS += 1
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)
    return condition


def sha256_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8-sig")


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _type_ok(value, typ) -> bool:
    """Stricter than a bare isinstance(): bool is a subclass of int in Python, so a
    schema field typed `int` must not silently accept True/False (a real, previously
    unguarded type-confusion bypass)."""
    if typ is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, typ)


# ============================================================================
# N-0: guard self-integrity -- must be validated before ANYTHING else. A tampered
# guard can lie about every other check, so this is the first and cheapest gate.
# ============================================================================

def verify_guard_self_integrity(pre_dir: Path, guard_path: Path) -> list:
    rows = []
    pin_path = pre_dir / "w3_4_guard_self_sha256.json"
    if not pin_path.is_file():
        rows.append(("GUARD_SELF_INTEGRITY_FAILURE: pinned self-hash artifact present",
                     False, str(pin_path)))
        return rows
    try:
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("GUARD_SELF_INTEGRITY_FAILURE: pinned self-hash artifact parses as JSON",
                     False, str(exc)))
        return rows
    pinned_sha = str(pin.get("sha256", "")).strip().lower()
    if len(pinned_sha) != 64:
        rows.append(("GUARD_SELF_INTEGRITY_FAILURE: pinned self-hash value is a "
                     "well-formed 64-char sha256", False, f"value={pinned_sha!r}"))
        return rows
    live_sha = sha256_of(guard_path)
    rows.append(("GUARD_SELF_INTEGRITY_FAILURE: this guard file's live bytes match the "
                 "pinned R2 self-hash (rejects in-place tampering, incl. widening "
                 "APPROVED_BOUNDARY_FILES)", live_sha == pinned_sha,
                 f"live={live_sha} pinned={pinned_sha}"))
    return rows


# ============================================================================
# N-5 (a): triple digest anchor -- must be validated BEFORE any baseline content
#          is trusted. Pure function so negative controls can point it at an
#          isolated temp PRE_DIR without touching the live project.
# ============================================================================

def verify_digest_anchor(pre_dir: Path, pinned_sidecar_sha256: str) -> list:
    """Returns a list of (label, ok, detail) tuples. Order matches plan section 5:
    sidecar-vs-bytes, pinned-vs-sidecar, per-artifact hash match."""
    rows = []
    digests_path = pre_dir / "w3_4_pre_artifact_digests.json"
    sidecar_path = pre_dir / "w3_4_pre_artifact_digests.json.sha256"

    if not digests_path.is_file():
        rows.append(("digest map present", False, str(digests_path)))
        return rows
    digest_bytes = digests_path.read_bytes()
    computed_digest_sha = sha256_bytes(digest_bytes)

    if not sidecar_path.is_file():
        rows.append(("digest sidecar present", False, str(sidecar_path)))
        return rows
    sidecar_val = sidecar_path.read_text(encoding="utf-8").strip().split()[0].lower()

    ok = sidecar_val == computed_digest_sha
    rows.append(("BASELINE_ARTIFACT_TAMPERED: sidecar sha256 matches digest-map bytes",
                 ok, f"sidecar={sidecar_val} computed={computed_digest_sha}"))

    ok = (pinned_sidecar_sha256 or "").strip().lower() == computed_digest_sha
    rows.append(("BASELINE_ARTIFACT_TAMPERED: pinned constant in this file matches digest-map bytes",
                 ok, f"pinned={pinned_sidecar_sha256} computed={computed_digest_sha}"))

    ok = (pinned_sidecar_sha256 or "").strip().lower() == sidecar_val
    rows.append(("BASELINE_ARTIFACT_TAMPERED: pinned constant matches sidecar value",
                 ok, f"pinned={pinned_sidecar_sha256} sidecar={sidecar_val}"))

    try:
        digest_map = json.loads(digest_bytes.decode("utf-8")).get("digests", {})
    except Exception as exc:  # noqa: BLE001
        rows.append(("BASELINE_SCHEMA_INVALID: digest map JSON parses", False, str(exc)))
        return rows

    expected_members = {"w3_4_pre_baseline.json", "w3_4_pre_residue_baseline.json",
                        "w3_4_frozen_historical_guards.json"}
    missing = expected_members - set(digest_map)
    rows.append(("digest map covers the required minimum artifact set",
                 not missing, f"missing={sorted(missing)}"))
    rows.append(("digest map is non-empty (rejects an emptied/weakened map)",
                 len(digest_map) > 0, f"count={len(digest_map)}"))

    for name, expected_sha in sorted(digest_map.items()):
        expected_sha_str = str(expected_sha).strip().lower()
        well_formed = len(expected_sha_str) == 64 and all(
            c in "0123456789abcdef" for c in expected_sha_str)
        rows.append((f"BASELINE_ARTIFACT_TAMPERED: {name} digest map value is a "
                     "well-formed 64-char sha256 (rejects a weakened/blanked entry)",
                     well_formed, f"value={expected_sha!r}"))
        if not well_formed:
            continue
        p = pre_dir / name
        if not p.is_file():
            rows.append((f"BASELINE_ARTIFACT_TAMPERED: {name} present", False, "missing"))
            continue
        live_sha = sha256_of(p)
        row_ok = live_sha == expected_sha_str
        rows.append((f"BASELINE_ARTIFACT_TAMPERED: {name} sha256 matches digest map",
                     row_ok, f"live={live_sha} expected={expected_sha_str}"))
        if name == "w3_4_pre_baseline.json":
            # Plan-mandated label (B-2 in the independent review: this exact literal
            # label did not exist anywhere in R1). Same real comparison, given its
            # own name so grep/plan-conformance tooling finds it directly instead of
            # relying on the generic BASELINE_ARTIFACT_TAMPERED row above.
            rows.append(("PRE_BASELINE_MUTATED: w3_4_pre_baseline.json content matches "
                         "its PRE-time pinned digest (no post-freeze mutation)",
                         row_ok, f"live={live_sha} expected={expected_sha_str}"))
    return rows


# ============================================================================
# N-5 (b): PRE-baseline schema validation
# ============================================================================

_PRE_BASELINE_REQUIRED_KEYS = {
    "schema_version": int,
    "kind": str,
    "generated_at": str,
    "frozen_tree_state": str,
    "runtime_files": dict,
    "root_py_inventory": dict,
    "tools_inventory": dict,
    "tools_non_py_allowlist": list,
    "top_level_dirs": list,
    "active_def_counts": dict,
    "approved_boundary_files_pinned": list,
    "manifest": dict,
    "f3_fingerprint_pre": str,
    "manifest_rows_pinned": list,
    "never_auto_permit_whitelist": list,
}

_PRE_RESIDUE_REQUIRED_KEYS = {
    "schema_version": int,
    "kind": str,
    "skip_dirs": list,
    "residue": list,
    "root_level_residue": list,
    "count": int,
}

_MANIFEST_SUBOBJECT_REQUIRED_KEYS = {
    "manifest_path", "manifest_sha256", "sidecar_path", "sidecar_sha256",
    "pinned_sha256_in_w3_2_manifest_integrity",
}


def _verify_schema(doc: dict, required: dict, doc_label: str) -> list:
    rows = []
    for key, typ in required.items():
        present = key in doc
        rows.append((f"BASELINE_SCHEMA_INVALID: {doc_label} has key {key!r}", present, ""))
        if present:
            rows.append((f"BASELINE_SCHEMA_INVALID: {doc_label} {key!r} has type {typ.__name__}",
                         _type_ok(doc[key], typ),
                         f"actual={type(doc[key]).__name__}"))
    extra = sorted(set(doc) - set(required))
    rows.append((f"BASELINE_SCHEMA_UNKNOWN_KEY: {doc_label} has no keys beyond the "
                 "required schema", not extra, f"extra={extra}"))
    return rows


def verify_pre_baseline_schema(pre_baseline: dict) -> list:
    rows = _verify_schema(pre_baseline, _PRE_BASELINE_REQUIRED_KEYS, "PRE baseline")
    rows.append(("BASELINE_SCHEMA_INVALID: PRE baseline kind == PRE_W3_4_AUTHORIZATION",
                 pre_baseline.get("kind") == "PRE_W3_4_AUTHORIZATION",
                 f"kind={pre_baseline.get('kind')!r}"))
    rows.append(("BASELINE_SCHEMA_INVALID: PRE baseline frozen_tree_state == UNTOUCHED",
                 pre_baseline.get("frozen_tree_state") == "UNTOUCHED",
                 f"value={pre_baseline.get('frozen_tree_state')!r}"))
    manifest_sub = pre_baseline.get("manifest")
    if isinstance(manifest_sub, dict):
        for key in sorted(_MANIFEST_SUBOBJECT_REQUIRED_KEYS):
            present = key in manifest_sub
            rows.append((f"BASELINE_SCHEMA_INVALID: PRE baseline manifest{{}} has key {key!r}",
                         present, ""))
            if present:
                rows.append((f"BASELINE_SCHEMA_INVALID: PRE baseline manifest{{}} {key!r} "
                             "has type str", isinstance(manifest_sub[key], str), ""))
        extra = sorted(set(manifest_sub) - _MANIFEST_SUBOBJECT_REQUIRED_KEYS)
        rows.append(("BASELINE_SCHEMA_UNKNOWN_KEY: PRE baseline manifest{} has no keys "
                     "beyond the required schema", not extra, f"extra={extra}"))
    return rows


def verify_pre_residue_schema(residue_baseline: dict) -> list:
    rows = _verify_schema(residue_baseline, _PRE_RESIDUE_REQUIRED_KEYS, "residue baseline")
    rows.append(("BASELINE_SCHEMA_INVALID: residue baseline kind == PRE_W3_4_RESIDUE",
                 residue_baseline.get("kind") == "PRE_W3_4_RESIDUE",
                 f"kind={residue_baseline.get('kind')!r}"))
    return rows


# ============================================================================
# N-5 (c): PRE/RELEASE separation + phase-state support
# ============================================================================

def verify_pre_release_separation() -> list:
    rows = []
    rows.append(("PRE baseline file exists (frozen, phase C onward)",
                 PRE_BASELINE_PATH.is_file(), str(PRE_BASELINE_PATH)))
    release_absent = not RELEASE_BASELINE_PATH.exists()
    rows.append(("phase-state C: RELEASE baseline is absent and this is VALID (not a failure)",
                 release_absent,
                 "RELEASE baseline unexpectedly present in a C-state round -- "
                 "would require phase-B derived_from_pre verification, not built here"
                 if not release_absent else ""))
    if not release_absent:
        try:
            release = load_json(RELEASE_BASELINE_PATH)
            rows.append(("if RELEASE exists: it declares kind POST_W3_4_RELEASE",
                         release.get("kind") == "POST_W3_4_RELEASE", ""))
            rows.append(("if RELEASE exists: derived_from_pre references the live PRE digest sha256",
                         release.get("derived_from_pre") == sha256_of(PRE_BASELINE_PATH)
                         if PRE_BASELINE_PATH.is_file() else False, ""))
        except Exception as exc:  # noqa: BLE001
            rows.append(("if RELEASE exists: it parses as JSON", False, str(exc)))
    # (R1 carried two additional decorative rows here that were hardcoded `True` with
    # no comparison at all -- NB-4 in the independent review. Removed rather than
    # padded with a fake predicate: the two rows above already give this phase-state
    # invariant real, falsifiable coverage.)
    return rows


# ============================================================================
# N-5 (d): tool-boundary partition
# ============================================================================

def verify_tool_boundary_partition(pre_baseline: dict, tools_dir: Path,
                                    approved: frozenset = APPROVED_BOUNDARY_FILES) -> list:
    rows = []
    other_tool_files = {
        name: sha for name, sha in pre_baseline["tools_inventory"]["files"].items()
        if name.endswith(".py") and "/" not in name  # top-level tools/*.py only
    }
    # approved and other must be disjoint
    overlap = approved & set(other_tool_files)
    rows.append(("BOUNDARY_PARTITION_VIOLATION: approved_boundary_files and "
                 "other_tool_files are disjoint", not overlap, f"overlap={sorted(overlap)}"))

    live_tools_py = {p.name for p in sorted(tools_dir.glob("*.py"))}
    union = approved | set(other_tool_files)
    rows.append(("BOUNDARY_PARTITION_VIOLATION: union(approved, other_tool_files) == "
                 "live expected tool inventory",
                 union == live_tools_py,
                 f"missing_from_union={sorted(live_tools_py - union)} "
                 f"stale_in_union={sorted(union - live_tools_py)}"))

    # no silent promotion: every approved file must NOT have been in other_tool_files at
    # PRE time (i.e. it is genuinely new), and every other_tool_files entry that now
    # exists must still be checked against its pinned hash (byte-identical elsewhere).
    rows.append(("BOUNDARY_PARTITION_VIOLATION: no entry silently promoted "
                 "other_tool_files -> approved_boundary_files",
                 not (approved & set(pre_baseline["tools_inventory"]["files"])),
                 f"already_existed_at_PRE={sorted(approved & set(pre_baseline['tools_inventory']['files']))}"))

    # BOUNDARY_ALLOWLIST_BROADENED: the live in-code APPROVED_BOUNDARY_FILES constant
    # must equal an anchor-protected pinned copy recorded in the PRE baseline at
    # generation time. Widening the in-code set (control A12 in the independent
    # review) changes this file's bytes -- caught primarily by
    # verify_guard_self_integrity -- but this gives a second, more specific,
    # independently-sourced diagnostic that does not depend on the self-hash
    # mechanism at all.
    pinned_boundary = set(pre_baseline.get("approved_boundary_files_pinned", []))
    rows.append(("BOUNDARY_ALLOWLIST_BROADENED: live APPROVED_BOUNDARY_FILES equals the "
                 "PRE-pinned approved boundary set (never silently widened)",
                 set(approved) == pinned_boundary,
                 f"live={sorted(approved)} pinned={sorted(pinned_boundary)}"))
    return rows


# ============================================================================
# N-R4: active module-level definition counts
# ============================================================================

PROTECTED_SYMBOLS = {
    "main.py": ["_schedule_summary_if_due", "_tp_pss_parse_date_token"],
    "panel_bot.py": [
        "_pf_eve_within_send_window", "_PF_EVE_SEND_HOUR_START", "_PF_EVE_SEND_MINUTE_START",
        "_PF_EVE_SEND_HOUR_CUTOFF", "_PF_EVE_VERIFY_HOUR_START", "_PF_EVE_VERIFY_MINUTE_START",
        "_PF_EVE_VERIFY_DEADLINE_HOUR", "_PF_EVE_VERIFY_DEADLINE_MINUTE",
        "_sched_pb_kyiv_today",
    ],
    "partner_stat_bot.py": [
        "_pf_within_window", "_PF_EVENING_SEND_HOUR_START", "_PF_EVENING_SEND_MINUTE_START",
        "_PF_EVENING_SEND_HOUR_CUTOFF", "_PF_EVENING_DEFER_HOUR_DEADLINE",
        "_PF_EVENING_DEFER_MINUTE_DEADLINE", "TZ", "_kyiv_now",
    ],
    "manager_bot.py": ["_MBSTAT_TZ", "_SCHED_TZ_MB", "_sched_kyiv_today"],
    "storage.py": [],
}


def module_level_counts(source_text: str) -> dict:
    """AST matching restricted to tree.body ONLY (module top level) -- never
    unrestricted ast.walk -- so nested same-name defs can never mask/satisfy an
    active-definition check (N-R4 req. 1/2/7).

    Also tracks class_defs (module-level ClassDef) and import_aliases (the bound name
    of every module-level Import/ImportFrom alias -- `import x as NAME` or
    `from m import x as NAME` / `from m import NAME`), so a protected symbol shadowed
    by a class or an import alias is visible to verify_protected_name_shadowing() even
    when it would not (yet) show up as a FunctionDef/Assign drift. R1 checked only
    FunctionDef/AsyncFunctionDef/Assign/AnnAssign, so ClassDef and import-alias
    shadowing of a protected name were invisible to N-R4 itself (NB-3, controls
    A13/A14 in the independent review -- in the C round the gap was masked by the
    unrelated runtime-byte-identity check, which will not exist once main.py is
    legally editable in W3.4-A/B)."""
    tree = ast.parse(source_text)
    func_counts = Counter()
    assign_counts = Counter()
    class_counts = Counter()
    import_alias_counts = Counter()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_counts[node.name] += 1
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assign_counts[t.id] += 1
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                assign_counts[node.target.id] += 1
        elif isinstance(node, ast.ClassDef):
            class_counts[node.name] += 1
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound_name = alias.asname or alias.name.split(".")[0]
                import_alias_counts[bound_name] += 1
    return {
        "function_defs": dict(func_counts),
        "assign_targets": dict(assign_counts),
        "class_defs": dict(class_counts),
        "import_aliases": dict(import_alias_counts),
    }


def verify_active_def_counts(pre_baseline: dict, base_dir: Path) -> list:
    rows = []
    baseline_adc = pre_baseline["active_def_counts"]
    for fname in RUNTIME_12:
        p = base_dir / fname
        if not p.is_file():
            rows.append((f"ACTIVE_BINDING_COUNT_DRIFT: {fname} present", False, "missing"))
            continue
        live = module_level_counts(read_text(p))
        base = baseline_adc.get(fname, {"function_defs": {}, "assign_targets": {}})

        # full-dict drift check (preserves legitimate existing counts > 1 exactly)
        for kind in ("function_defs", "assign_targets"):
            live_kind = live[kind]
            base_kind = base.get(kind, {})
            all_names = set(live_kind) | set(base_kind)
            drifted = sorted(n for n in all_names if live_kind.get(n, 0) != base_kind.get(n, 0))
            rows.append((f"ACTIVE_BINDING_COUNT_DRIFT: {fname} {kind} unchanged vs PRE baseline",
                         not drifted,
                         f"drifted={[(n, base_kind.get(n, 0), live_kind.get(n, 0)) for n in drifted]}"))

        # protected single-definition shape checks. Two distinct failure shapes:
        #   SHAPE_TARGET_AMBIGUOUS   -- zero bindings (missing/renamed/nested-only), or
        #                               the name is bound as BOTH a def and a module-level
        #                               assign (genuinely ambiguous which one is "active").
        #   DUPLICATE_ACTIVE_DEFINITION -- a clean second binding of the same kind (>=2
        #                               defs, or >=2 assigns) -- the specific "someone
        #                               re-added a second copy" mutation.
        for name in PROTECTED_SYMBOLS.get(fname, []):
            fcount = live["function_defs"].get(name, 0)
            acount = live["assign_targets"].get(name, 0)
            total = fcount + acount
            if total == 0 or (fcount > 0 and acount > 0):
                rows.append((f"SHAPE_TARGET_AMBIGUOUS: {fname}::{name} exactly one "
                             "active module-level binding of a single kind", False,
                             f"function_defs={fcount} assign_targets={acount}"))
                continue
            if total > 1:
                rows.append((f"DUPLICATE_ACTIVE_DEFINITION: {fname}::{name} stays a single "
                             "active module-level binding (no second binding introduced)",
                             False, f"function_defs={fcount} assign_targets={acount}"))
                continue
            base_total = (base.get("function_defs", {}).get(name, 0)
                          + base.get("assign_targets", {}).get(name, 0))
            row_ok = base_total == 1
            rows.append((f"DUPLICATE_ACTIVE_DEFINITION: {fname}::{name} stays a single "
                         "active module-level binding (no second binding introduced)",
                         row_ok, f"live_total={total} baseline_total={base_total}"))
    return rows


def verify_protected_name_shadowing(base_dir: Path) -> list:
    """A protected symbol name must never be (re)bound by a module-level ClassDef or
    by an Import/ImportFrom alias -- checked against zero, independent of any PRE
    baseline count, because this must never legitimately happen at all (unlike
    function_defs/assign_targets, which have legitimate counts >1 for some names).
    Closes NB-3 (controls A13/A14): R1's N-R4 covered FunctionDef/Assign only."""
    rows = []
    for fname in RUNTIME_12:
        p = base_dir / fname
        names = PROTECTED_SYMBOLS.get(fname, [])
        if not names:
            continue
        if not p.is_file():
            continue  # already reported as missing by verify_active_def_counts
        live = module_level_counts(read_text(p))
        for name in names:
            cls_hits = live["class_defs"].get(name, 0)
            import_hits = live["import_aliases"].get(name, 0)
            rows.append((f"PROTECTED_NAME_SHADOWED: {fname}::{name} not shadowed by a "
                         "module-level ClassDef or Import/ImportFrom alias",
                         cls_hits == 0 and import_hits == 0,
                         f"class_defs={cls_hits} import_aliases={import_hits}"))
    return rows


# ============================================================================
# N-R5: manifest forms pinning
# ============================================================================

def _row_key(row: dict) -> tuple:
    return (row["file"], row["qualified_name"], row["generation_index"])


def verify_manifest_forms(pre_baseline: dict, manifest_path: Path) -> list:
    rows = []
    if not manifest_path.is_file():
        rows.append(("W3.2 manifest present for N-R5 pin check", False, str(manifest_path)))
        return rows
    manifest = load_json(manifest_path)
    live_rows = {_row_key(r): r for r in manifest.get("clock_index", [])}
    pinned_rows = {_row_key(r): r for r in pre_baseline["manifest_rows_pinned"]}

    removed = sorted(set(pinned_rows) - set(live_rows))
    rows.append(("MANIFEST_OWNERSHIP_LOSS: no pinned clock_index row removed",
                 not removed, f"removed={removed}"))

    for key, pinned in sorted(pinned_rows.items()):
        live = live_rows.get(key)
        if live is None:
            continue  # already reported as MANIFEST_OWNERSHIP_LOSS above
        rows.append((f"MANIFEST_ROLE_DRIFT: {key} role unchanged",
                     live.get("role") == pinned["role"],
                     f"live={live.get('role')} pinned={pinned['role']}"))
        live_permitted = sorted(live.get("permitted_forms") or [])
        rows.append((f"FORMS_BROADENED: {key} permitted_forms exact match (no subset/superset)",
                     live_permitted == pinned["permitted_forms"],
                     f"live={live_permitted} pinned={pinned['permitted_forms']}"))
        live_detected = sorted(live.get("detected_forms") or [])
        rows.append((f"DETECTED_FORMS_DRIFT: {key} detected_forms exact match",
                     live_detected == pinned["detected_forms"],
                     f"live={live_detected} pinned={pinned['detected_forms']}"))
        rows.append((f"MANIFEST_ROLE_DRIFT: {key} confidence unchanged",
                     live.get("confidence") == pinned["confidence"],
                     f"live={live.get('confidence')} pinned={pinned['confidence']}"))
        rows.append((f"MANIFEST_ROLE_DRIFT: {key} contract_note unchanged",
                     live.get("contract_note", "") == pinned["contract_note"],
                     ""))
        added_tokens = set(live_permitted) - set(pinned["permitted_forms"])
        never_hit = added_tokens & set(pre_baseline["never_auto_permit_whitelist"])
        rows.append((f"NEVER_AUTO_PERMIT_VIOLATION: {key} no NEVER_AUTO_PERMIT token "
                     "added to permitted_forms", not never_hit, f"added={sorted(never_hit)}"))
    # (R1 carried one further decorative row here labelled "whitelist itself unchanged
    # from PRE pin" hardcoded to `True` -- NB-4. The real comparison for that exact
    # invariant already happens in verify_never_auto_permit_source() below; padding it
    # with a second, vacuous copy added nothing and is removed.)
    return rows


def verify_never_auto_permit_source(pre_baseline: dict, tools_dir: Path) -> list:
    """The whitelist itself must not have been silently narrowed/broadened in the tool
    that defines it (tools/w3_2_whole_scope_clock_index.py, byte-identical elsewhere,
    but checked again here from the semantic angle: the live NEVER_AUTO_PERMIT constant
    equals the PRE-pinned one)."""
    rows = []
    sys.path.insert(0, str(tools_dir))
    try:
        import w3_2_whole_scope_clock_index as ci  # noqa: E402
        live_whitelist = sorted(ci.NEVER_AUTO_PERMIT)
    except Exception as exc:  # noqa: BLE001
        rows.append(("NEVER_AUTO_PERMIT_VIOLATION: whitelist module importable", False, str(exc)))
        return rows
    pinned_whitelist = sorted(pre_baseline["never_auto_permit_whitelist"])
    rows.append(("NEVER_AUTO_PERMIT_VIOLATION: live whitelist == PRE-pinned whitelist "
                 "(never broadened, never narrowed)",
                 live_whitelist == pinned_whitelist,
                 f"live={live_whitelist} pinned={pinned_whitelist}"))
    return rows


# ============================================================================
# N-R6: file inventory
# ============================================================================

def verify_root_py_inventory(pre_baseline: dict, base_dir: Path) -> list:
    rows = []
    baseline_inv = pre_baseline["root_py_inventory"]
    live_inv = {p.name: sha256_of(p) for p in sorted(base_dir.glob("*.py")) if p.is_file()}

    new_files = sorted(set(live_inv) - set(baseline_inv))
    rows.append(("UNDECLARED_ROOT_PY_FILE: no new root-level *.py file",
                 not new_files, f"new={new_files}"))

    removed_files = sorted(set(baseline_inv) - set(live_inv))
    rows.append(("ROOT_PY_INVENTORY_DRIFT: no root-level *.py file removed",
                 not removed_files, f"removed={removed_files}"))

    changed = sorted(n for n, h in baseline_inv.items()
                     if n in live_inv and live_inv[n] != h)
    rows.append(("ROOT_PY_INVENTORY_DRIFT: every pre-existing root-level *.py file "
                 "byte-identical", not changed, f"changed={changed}"))
    return rows


def verify_tools_inventory(pre_baseline: dict, tools_dir: Path,
                            approved: frozenset = APPROVED_BOUNDARY_FILES) -> list:
    rows = []
    baseline_files = pre_baseline["tools_inventory"]["files"]
    baseline_dirs = set(pre_baseline["tools_inventory"]["dirs"])

    live_files = {}
    live_dirs = set()
    for root, dirnames, filenames in os.walk(tools_dir):
        # __pycache__ is regenerated bytecode noise (created merely by importing/
        # compiling any tools\*.py file, including this guard itself during its own
        # py_compile regression check) -- never tracked source content. Excluded here
        # exactly as it is excluded from the PRE baseline's tools_inventory.
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        rel_root = Path(root).relative_to(tools_dir)
        for d in dirnames:
            rp = (rel_root / d) if str(rel_root) != "." else Path(d)
            live_dirs.add(str(rp).replace("\\", "/"))
        for fn in filenames:
            full = Path(root) / fn
            rel = full.relative_to(tools_dir)
            live_files[str(rel).replace("\\", "/")] = sha256_of(full)

    new_subdirs = sorted(live_dirs - baseline_dirs)
    rows.append(("UNEXPECTED_TOOLS_SUBDIR: no new subdirectory under tools\\",
                 not new_subdirs, f"new={new_subdirs}"))

    new_files = sorted(set(live_files) - set(baseline_files) - approved)
    non_py_new = [f for f in new_files if not f.endswith(".py")]
    py_new = [f for f in new_files if f.endswith(".py")]
    rows.append(("UNEXPECTED_NON_PY_IN_TOOLS: no unexpected non-*.py file under tools\\",
                 not non_py_new, f"new_non_py={non_py_new}"))
    rows.append(("UNDECLARED_ROOT_PY_FILE: no *.py file under tools\\ beyond the one "
                 "approved new tool (rejects a second W3.4 tool)",
                 not py_new, f"new_py={py_new}"))

    removed_files = sorted(set(baseline_files) - set(live_files))
    rows.append(("ROOT_PY_INVENTORY_DRIFT: no pre-existing tools\\ file removed",
                 not removed_files, f"removed={removed_files}"))

    changed = sorted(n for n, h in baseline_files.items()
                     if n in live_files and live_files[n] != h)
    rows.append(("ROOT_PY_INVENTORY_DRIFT: every pre-existing tools\\ file byte-identical",
                 not changed, f"changed={changed}"))
    return rows


def verify_top_level_dirs(pre_baseline: dict, base_dir: Path) -> list:
    rows = []
    baseline_dirs = set(pre_baseline["top_level_dirs"])
    live_dirs = {p.name for p in base_dir.iterdir() if p.is_dir()}
    new = sorted(live_dirs - baseline_dirs)
    removed = sorted(baseline_dirs - live_dirs)
    rows.append(("TOP_LEVEL_DIRECTORY_DRIFT: no new top-level directory", not new, f"new={new}"))
    rows.append(("TOP_LEVEL_DIRECTORY_DRIFT: no top-level directory removed",
                 not removed, f"removed={removed}"))
    return rows


# ============================================================================
# N-F3: f3_fingerprint_pre -- actively re-derived and compared, not just schema-typed
# ============================================================================

def compute_f3_fingerprint(base_dir: Path) -> str:
    """sha256(ast.unparse(...)) of the sole module-level def of main.py::
    _tp_pss_parse_date_token. Raises if it is missing or not exactly one
    module-level FunctionDef -- callers must treat that as a hard FAIL, not a
    silently-empty fingerprint."""
    text = read_text(base_dir / "main.py")
    tree = ast.parse(text, filename="main.py")
    matches = [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name == "_tp_pss_parse_date_token"]
    if len(matches) != 1:
        raise ValueError(f"expected exactly 1 module-level def, found {len(matches)}")
    return sha256_bytes(ast.unparse(matches[0]).encode("utf-8"))


def verify_f3_fingerprint(pre_baseline: dict, base_dir: Path) -> list:
    pinned = pre_baseline.get("f3_fingerprint_pre", "")
    try:
        live = compute_f3_fingerprint(base_dir)
    except Exception as exc:  # noqa: BLE001
        return [("F3_PRE_FINGERPRINT_DRIFT: main.py::_tp_pss_parse_date_token "
                 "re-derivable as a single module-level def", False, str(exc))]
    return [("F3_PRE_FINGERPRINT_DRIFT: live sha256(ast.unparse(main.py::"
             "_tp_pss_parse_date_token)) matches f3_fingerprint_pre",
             live == pinned, f"live={live} pinned={pinned}")]


# ============================================================================
# N-R7: residue baseline normalization + new-residue detection
# ============================================================================

RESIDUE_MARKERS_SUBSTR = (".bak", ".tmp", ".orig", ".rej")
RESIDUE_MARKERS_ANY = ("restore", "cursor_copy")
RESIDUE_PREFIX = ("main_beka",)


def is_residue_name(fn: str) -> bool:
    low = fn.lower()
    if any(m in low for m in RESIDUE_MARKERS_SUBSTR):
        return True
    if any(m in low for m in RESIDUE_MARKERS_ANY):
        return True
    if any(low.startswith(p) for p in RESIDUE_PREFIX):
        return True
    return False


def scan_residue(base_dir: Path, skip_dirs: set):
    root_level, nested = [], []
    for root, dirs, files in os.walk(base_dir):
        rel_root = Path(root).relative_to(base_dir)
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            if is_residue_name(fn):
                rel = (rel_root / fn) if str(rel_root) != "." else Path(fn)
                rel_str = str(rel).replace("\\", "/")
                (root_level if str(rel_root) == "." else nested).append(rel_str)
    return sorted(root_level), sorted(nested)


def verify_residue(pre_residue_baseline: dict, base_dir: Path) -> list:
    rows = []
    skip_dirs_baseline = pre_residue_baseline["skip_dirs"]

    # no bare basename anywhere in the baseline (nested "residue" entries must contain a
    # FORWARD-SLASH path separator; a backslash is not an acceptable substitute -- a
    # path smuggled in with "\\" instead of "/" would previously pass the naive "/" not
    # in r check while still being a single unnormalized segment (NB-5, control A05).
    bare = [r for r in pre_residue_baseline["residue"]
            if "/" not in r or "\\" in r]
    rows.append(("RESIDUE_BASELINE_BARE_BASENAME: no bare-basename or backslash-variant "
                 "entry in nested residue[]", not bare, f"bare={bare}"))

    root_entries = pre_residue_baseline["root_level_residue"]
    bad_root = [e for e in root_entries
                if any(sep in (e.get("path", "") if isinstance(e, dict) else str(e))
                       for sep in ("/", "\\"))]
    rows.append(("RESIDUE_BASELINE_BARE_BASENAME: root_level_residue entries are true "
                 "root-level paths (no forward-slash or backslash separator)",
                 not bad_root, f"bad={bad_root}"))

    every_root_has_reason = all(
        isinstance(e, dict) and e.get("reason") for e in root_entries)
    rows.append(("every root_level_residue entry carries an explicit reason",
                 every_root_has_reason, ""))

    live_root, live_nested = scan_residue(base_dir, set(skip_dirs_baseline))

    baseline_root_paths = {e["path"] if isinstance(e, dict) else e for e in root_entries}
    new_root = sorted(set(live_root) - baseline_root_paths)
    rows.append(("UNDECLARED_ROOT_RESIDUE: no new root-level residue file",
                 not new_root, f"new={new_root}"))

    baseline_nested = set(pre_residue_baseline["residue"])
    new_nested = sorted(set(live_nested) - baseline_nested)
    rows.append(("NEW_REPOSITORY_RESIDUE: no new nested residue file",
                 not new_nested, f"new={new_nested}"))

    # Real, non-tautological drift check (R1: set(x) == set(x), always True -- B-4).
    # Compares the BASELINE FILE's skip_dirs (already covered, as file bytes, by the
    # digest anchor) against an INDEPENDENTLY in-code pinned copy -- genuine
    # defense-in-depth, same pattern as the redundant root_py_inventory cross-check
    # that already caught control A10 in the independent review.
    rows.append(("RESIDUE_SKIP_DIRS_DRIFT: residue baseline's skip_dirs matches the "
                 "independently in-code pinned skip_dirs contract",
                 set(skip_dirs_baseline) == PINNED_SKIP_DIRS,
                 f"baseline={sorted(skip_dirs_baseline)} pinned={sorted(PINNED_SKIP_DIRS)}"))
    return rows


# ============================================================================
# Runtime byte-identity / allow_spend / mojibake / historical artifacts
# ============================================================================

def verify_runtime_byte_identity(pre_baseline: dict, base_dir: Path) -> list:
    rows = []
    baseline = pre_baseline["runtime_files"]
    for fname in RUNTIME_12:
        p = base_dir / fname
        if not p.is_file():
            rows.append((f"runtime file present: {fname}", False, "missing"))
            continue
        live = sha256_of(p)
        rows.append((f"runtime file byte-identical to PRE (zero runtime changes in "
                     f"W3.4-C): {fname}", live == baseline.get(fname),
                     f"live={live} baseline={baseline.get(fname)}"))
    return rows


def verify_allow_spend(base_dir: Path) -> list:
    total = 0
    sites = []
    for fname in ("main.py", "storage.py"):
        text = read_text(base_dir / fname)
        tree = ast.parse(text, filename=fname)
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "allow_spend":
                v = node.value
                if isinstance(v, ast.Constant) and v.value is True:
                    total += 1
                    sites.append((fname, getattr(node, "lineno", None)))
    only_main = all(f == "main.py" for f, _ in sites)
    return [("allow_spend=True appears exactly 2 times in main.py + storage.py "
             "(the two files this project's spend-guard invariant is scoped to; see "
             "R2 report for the full 46-root-*.py independent cross-check), both in "
             "main.py", total == 2 and only_main, f"count={total} sites={sites}")]


def verify_mojibake(base_dir: Path) -> list:
    rows = []
    files = ("tools/w3_4_scope_guard_selftest.py",)
    for rel in files:
        # Markers are built via chr()/ordinal concat, never embedded as literal bytes,
        # so this tool's own source is guaranteed never to contain them.
        text = read_text(base_dir / rel)
        hits = {m: text.count(m) for m in MOJIBAKE_MARKERS if m in text}
        rows.append((f"mojibake scan clean: {rel}", not hits, f"hits={hits}"))
    return rows


def verify_historical_artifacts(historical: dict, base_dir: Path) -> list:
    rows = []
    for entry in historical["entries"]:
        p = base_dir / entry["path"]
        if not p.is_file():
            rows.append((f"HISTORICAL_ARTIFACT_MUTATED: {entry['path']} present", False, "missing"))
            continue
        live = sha256_of(p)
        rows.append((f"HISTORICAL_ARTIFACT_MUTATED: {entry['path']} byte-identical "
                     f"(frozen, {entry['expected_status']})",
                     live == entry["sha256"], f"live={live} expected={entry['sha256']}"))
        expected_checks = entry.get("expected_red_checks") or []
        rows.append((f"historical registry entry {entry['path']} declares at least one "
                     "verbatim expected_red_checks[] string (plan section 11)",
                     len(expected_checks) > 0, f"count={len(expected_checks)}"))

    log_info = historical["historical_raw_log"]
    log_path = Path(log_info["path"])
    if not log_path.is_file():
        rows.append(("HISTORICAL_ARTIFACT_MUTATED: historical regression log present",
                     False, str(log_path)))
    else:
        live = sha256_of(log_path)
        rows.append(("HISTORICAL_ARTIFACT_MUTATED: regress_w3_3_scope_guard_selftest.log "
                     "byte-identical (including its own recorded FAILED: 6, never re-run "
                     "or repaired here)", live == log_info["sha256"],
                     f"live={live} expected={log_info['sha256']}"))
        text = log_path.read_text(encoding="utf-8", errors="replace")
        rows.append(("historical log's own recorded FAILED count is disclosed as 6 "
                     "(reported exactly, not hidden)",
                     f"FAILED: {log_info['expected_failed_count']}" in text,
                     "expected literal 'FAILED: 6' substring"))
    return rows


def verify_single_new_tool(pre_baseline: dict, tools_dir: Path,
                            approved: frozenset = APPROVED_BOUNDARY_FILES) -> list:
    baseline_files = set(pre_baseline["tools_inventory"]["files"])
    live_py = {p.name for p in sorted(tools_dir.glob("*.py"))}
    new_files = sorted(live_py - baseline_files)
    return [("only tools\\w3_4_scope_guard_selftest.py is newly created under tools\\",
             set(new_files) == set(approved), f"new_files={new_files}")]


# ============================================================================
# main
# ============================================================================

def run_all(base_dir: Path = BASE_DIR, tools_dir: Path = TOOLS_DIR,
            pre_dir: Path = PRE_DIR, manifest_path: Path = MANIFEST_PATH,
            pinned_sidecar_sha256: str = PINNED_PRE_DIGESTS_SHA256,
            guard_path: Path = None) -> int:
    global FAILURES, TOTAL_CHECKS
    FAILURES = []
    TOTAL_CHECKS = 0
    if guard_path is None:
        guard_path = Path(__file__).resolve()

    for label, ok, detail in verify_guard_self_integrity(pre_dir, guard_path):
        check(label, ok, detail)
    if FAILURES:
        print(f"\nHARD-FAIL: guard self-integrity invalid -- refusing to trust anything "
              f"this (possibly tampered) file computes.")
        print(f"TOTAL: {TOTAL_CHECKS}  FAILED: {len(FAILURES)}")
        return 1

    for label, ok, detail in verify_digest_anchor(pre_dir, pinned_sidecar_sha256):
        check(label, ok, detail)
    if FAILURES:
        print(f"\nHARD-FAIL: PRE triple anchor invalid -- refusing to trust baseline content.")
        print(f"TOTAL: {TOTAL_CHECKS}  FAILED: {len(FAILURES)}")
        return 1

    pre_baseline = load_json(pre_dir / "w3_4_pre_baseline.json")
    pre_residue = load_json(pre_dir / "w3_4_pre_residue_baseline.json")
    historical = load_json(pre_dir / "w3_4_frozen_historical_guards.json")

    for label, ok, detail in verify_pre_baseline_schema(pre_baseline):
        check(label, ok, detail)
    for label, ok, detail in verify_pre_residue_schema(pre_residue):
        check(label, ok, detail)
    for label, ok, detail in verify_pre_release_separation():
        check(label, ok, detail)
    for label, ok, detail in verify_tool_boundary_partition(pre_baseline, tools_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_active_def_counts(pre_baseline, base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_protected_name_shadowing(base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_manifest_forms(pre_baseline, manifest_path):
        check(label, ok, detail)
    for label, ok, detail in verify_never_auto_permit_source(pre_baseline, tools_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_root_py_inventory(pre_baseline, base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_tools_inventory(pre_baseline, tools_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_top_level_dirs(pre_baseline, base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_residue(pre_residue, base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_f3_fingerprint(pre_baseline, base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_runtime_byte_identity(pre_baseline, base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_allow_spend(base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_mojibake(base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_historical_artifacts(historical, base_dir):
        check(label, ok, detail)
    for label, ok, detail in verify_single_new_tool(pre_baseline, tools_dir):
        check(label, ok, detail)

    print()
    print(f"TOTAL: {TOTAL_CHECKS}  FAILED: {len(FAILURES)}")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def main() -> int:
    if not PRE_BASELINE_PATH.exists():
        print(f"[FAIL] PRE baseline artifact present  {PRE_BASELINE_PATH}")
        print("\nTOTAL: 1  FAILED: 1")
        return 1
    return run_all()


if __name__ == "__main__":
    sys.exit(main())
