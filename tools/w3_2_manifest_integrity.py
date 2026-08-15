# -*- coding: utf-8 -*-
"""W3.2 Design C+ -- external manifest loader, integrity verifier and schema validator.

Implements section 7 of the approved redesign plan
(C:\\ALM_TPilot_AUDIT\\20260731\\W3_2_GATE_REDESIGN_PLAN\\).

The manifest is the ONLY copy of the expected values (frozen roots, approved helpers,
permitted instances, role table, whole-scope clock index).  This module -- and the gate
that uses it -- embed only:

  * the manifest path,
  * the manifest schema,
  * the pinned SHA256 digest.

Three integrity anchors must agree:

  1. the sidecar file  ``w3_2_timezone_gate_manifest.json.sha256``
  2. ``PINNED_MANIFEST_SHA256`` in this file
  3. the digest recorded in the implementation acceptance artifacts

Diagnostic codes emitted here (all RED):

  MANIFEST_MISSING          -- manifest file absent
  MANIFEST_DIGEST_MISSING   -- sidecar absent, or the pinned constant is unset
  MANIFEST_INTEGRITY_FAIL   -- computed digest disagrees with sidecar or pinned constant
  MANIFEST_SCHEMA_FAIL      -- manifest present and intact but structurally invalid

Read-only.  No DB, no network, no server, no runtime import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import w3_2_whole_scope_clock_index as ci  # noqa: E402

# --------------------------------------------------------------------------------------
# The three embedded constants permitted by the plan: path, schema, pinned digest.
# --------------------------------------------------------------------------------------

MANIFEST_DIR = Path(r"C:\ALM_TPilot_AUDIT\W3_2_GATE_MANIFEST")
MANIFEST_FILENAME = "w3_2_timezone_gate_manifest.json"
MANIFEST_PATH = MANIFEST_DIR / MANIFEST_FILENAME
SIDECAR_PATH = MANIFEST_DIR / (MANIFEST_FILENAME + ".sha256")

# Pinned at implementation time; must equal the sidecar and the acceptance artifact.
# W3.2 D6/D7 correction (2026-08-01): re-pinned after manifest regeneration -- see
# C:\ALM_TPilot_AUDIT\20260801\W3_2_FINAL_BUSINESS_DATE_FIX_IMPLEMENTATION\.
# W3.3-B (2026-08-03): re-pinned after the panel_bot.py::_sched_pb_kyiv_today timezone
# redirect -- see C:\ALM_TPilot_AUDIT\20260803\W3_3_TZ_REDIRECT_IMPLEMENTATION\.
# HNV2 timezone declaration (2026-08-08): re-pinned after declaring the 8 approved
# HNV2 clock-touching defs (health_incident_v2_observe, _hnv2_collect_evidence,
# _hnv2_count_new_recovery_events, _hnv2_verify_spawn, _hnv2_rate_limited_ready_probe,
# _hnv2_drive_observed_open, _hnv2_advance_recovery,
# _reap_stale_running_panel_notifications) -- see
# C:\ALM_TPilot_AUDIT\20260808\HNV2_TIMEZONE_MANIFEST_DECLARATION\.
PINNED_MANIFEST_SHA256 = "8232807fae4763c04dfe83677d95d88a5633ba07ec18139dd411294c6a2dc2bb"

SCHEMA_VERSION = 1

# Structural schema only -- no root names, no helper names, no permitted instances,
# no role->form table.  All of those live in the manifest itself.
_TOP_LEVEL_REQUIRED = (
    "schema_version",
    "manifest_id",
    "scope_files",
    "closure_budget",
    "unresolved_policy",
    "roles",
    "roots",
    "approved_helpers",
    "wrapper_registry",
    "mixed_clock_scan_targets",
    "clock_index",
    "deferred_entries",
    "escalation_constants",
)

_ROOT_REQUIRED = ("file", "name", "generation_count", "active_index",
                  "active_fingerprint", "generations")
_HELPER_REQUIRED = ("file", "qualified_name", "generation_index", "source_span",
                    "fingerprint", "role", "contract_note")
_CLOCK_REQUIRED = ("file", "qualified_name", "generation_index", "source_span",
                   "fingerprint", "role", "permitted_forms", "contract_note",
                   "detected_forms", "confidence")
_ROLE_REQUIRED = ("meaning", "family", "violation_code")
_WRAPPER_REQUIRED = ("file", "qualified_name", "generation_index", "fingerprint",
                     "produced_family", "contract_note")
_MIXED_TARGET_REQUIRED = ("file", "qualified_name", "generation_index", "fingerprint",
                          "adjudication_mode", "contract_note")
_SITE_CONTRACT_REQUIRED = ("site_id", "form", "site_role", "lineno", "owner")

PERMITTED_ROLES = (
    "BUSINESS_LOCAL",
    "DATE_ONLY_KYIV",
    "UTC_PERSISTENCE",
    "UTC_INSTANT",
    "FRESHNESS_UTC",
    "MONOTONIC",
    "MIXED_CLOCK_CONTRACT",
    "DEFERRED_W3_4",
)

PERMITTED_CONFIDENCE = ("HIGH", "REVIEW_REQUIRED")


class ManifestError(Exception):
    """Raised when the manifest cannot be loaded or trusted."""

    def __init__(self, code: str, detail: str):
        super().__init__("%s: %s" % (code, detail))
        self.code = code
        self.detail = detail


def compute_digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def read_sidecar(sidecar_path: Path) -> str:
    """Sidecar format: the hex digest, optionally followed by whitespace + filename."""
    text = Path(sidecar_path).read_text(encoding="utf-8").strip()
    if not text:
        raise ManifestError("MANIFEST_DIGEST_MISSING", "sidecar %s is empty" % sidecar_path)
    return text.split()[0].lower()


def validate_schema(manifest: dict) -> list:
    """Return a list of human-readable schema problems (empty == valid)."""
    problems = []

    if not isinstance(manifest, dict):
        return ["manifest root is %s, expected object" % type(manifest).__name__]

    for key in _TOP_LEVEL_REQUIRED:
        if key not in manifest:
            problems.append("missing top-level key %r" % key)
    if problems:
        return problems

    if manifest.get("schema_version") != SCHEMA_VERSION:
        problems.append("schema_version %r != %r"
                        % (manifest.get("schema_version"), SCHEMA_VERSION))

    if not isinstance(manifest.get("scope_files"), list) or not manifest["scope_files"]:
        problems.append("scope_files must be a non-empty list")

    if not isinstance(manifest.get("closure_budget"), int) or manifest["closure_budget"] <= 0:
        problems.append("closure_budget must be a positive integer")

    roles = manifest.get("roles")
    if not isinstance(roles, dict) or not roles:
        problems.append("roles must be a non-empty object")
    else:
        for name, spec in roles.items():
            if name not in PERMITTED_ROLES:
                problems.append("role %r is not a permitted role name" % name)
            if not isinstance(spec, dict):
                problems.append("role %r spec must be an object" % name)
                continue
            for key in _ROLE_REQUIRED:
                if key not in spec:
                    problems.append("role %r missing %r" % (name, key))
        for name in PERMITTED_ROLES:
            if name not in roles:
                problems.append("roles table does not declare %r" % name)

    def _check_rows(rows, required, label, allow_role=False):
        if not isinstance(rows, list):
            problems.append("%s must be a list" % label)
            return
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                problems.append("%s[%d] must be an object" % (label, i))
                continue
            for key in required:
                if key not in row:
                    problems.append("%s[%d] missing %r" % (label, i, key))
            if allow_role and row.get("role") not in PERMITTED_ROLES:
                problems.append("%s[%d] role %r not permitted" % (label, i, row.get("role")))

    _check_rows(manifest.get("roots"), _ROOT_REQUIRED, "roots")
    _check_rows(manifest.get("approved_helpers"), _HELPER_REQUIRED, "approved_helpers",
                allow_role=True)
    _check_rows(manifest.get("clock_index"), _CLOCK_REQUIRED, "clock_index", allow_role=True)
    _check_rows(manifest.get("wrapper_registry"), _WRAPPER_REQUIRED, "wrapper_registry")
    _check_rows(manifest.get("mixed_clock_scan_targets"), _MIXED_TARGET_REQUIRED,
               "mixed_clock_scan_targets")

    for i, row in enumerate(manifest.get("clock_index") or []):
        if not isinstance(row, dict):
            continue
        if row.get("confidence") not in PERMITTED_CONFIDENCE:
            problems.append("clock_index[%d] confidence %r not permitted"
                            % (i, row.get("confidence")))
        span = row.get("source_span")
        if not (isinstance(span, list) and len(span) == 2
                and all(isinstance(x, int) for x in span)):
            problems.append("clock_index[%d] source_span must be [lineno, end_lineno]" % i)
        if not isinstance(row.get("permitted_forms"), list):
            problems.append("clock_index[%d] permitted_forms must be a list" % i)

        # H6 (09 sec 7): MIXED_CLOCK_CONTRACT schema -- >=2 site contracts spanning >=2
        # distinct roles, unique site identities, empty row-level permitted_forms, every
        # contract structurally sound (a contract can never bring a site into existence --
        # that a contract's site_id is actually MEASURED is checked at gate runtime,
        # not here).
        if row.get("role") == "MIXED_CLOCK_CONTRACT":
            if row.get("permitted_forms"):
                problems.append("clock_index[%d] MIXED_CLOCK_CONTRACT row-level "
                                "permitted_forms must be empty" % i)
            contracts = row.get("site_contracts")
            if not isinstance(contracts, list) or len(contracts) < 2:
                problems.append("clock_index[%d] MIXED_CLOCK_CONTRACT requires >=2 "
                                "site_contracts" % i)
            else:
                for j, c in enumerate(contracts):
                    if not isinstance(c, dict):
                        problems.append("clock_index[%d].site_contracts[%d] must be an "
                                        "object" % (i, j))
                        continue
                    for key in _SITE_CONTRACT_REQUIRED:
                        if key not in c:
                            problems.append("clock_index[%d].site_contracts[%d] missing "
                                            "%r" % (i, j, key))
                roles_seen = {c.get("site_role") for c in contracts if isinstance(c, dict)}
                if len(roles_seen) < 2:
                    problems.append("clock_index[%d] MIXED_CLOCK_CONTRACT site_contracts "
                                    "must span >=2 distinct site roles" % i)
                site_ids = [c.get("site_id") for c in contracts if isinstance(c, dict)]
                if len(set(site_ids)) != len(site_ids):
                    problems.append("clock_index[%d] MIXED_CLOCK_CONTRACT site_contracts "
                                    "have duplicate site_id" % i)
        elif row.get("site_contracts"):
            problems.append("clock_index[%d] role %r must not carry site_contracts"
                            % (i, row.get("role")))

    # H2 (09 sec 3), enforced here in addition to build_manifest_from_seed so a manifest
    # mutated directly (bypassing the builder) is still caught.
    coherence = ci.check_role_family_coherence(manifest.get("clock_index") or [])
    for f in coherence:
        problems.append("ROLE_CONTRACT_MISMATCH %s::%s form=%s -- %s"
                        % (f["file"], f["qualified_name"], f.get("form"), f["detail"]))

    # H3 (09 sec 4, closes R-8b): identity uniqueness across the three sections.
    for p in ci.check_identity_uniqueness(manifest):
        problems.append(p)

    if not isinstance(manifest.get("deferred_entries"), list):
        problems.append("deferred_entries must be a list")
    else:
        for i, row in enumerate(manifest["deferred_entries"]):
            if not isinstance(row, dict):
                problems.append("deferred_entries[%d] must be an object" % i)
                continue
            for key in ("file", "qualified_name", "finding_id", "role"):
                if key not in row:
                    problems.append("deferred_entries[%d] missing %r" % (i, key))

    # Cross-consistency: every clock_index entry must reference a declared scope file,
    # and every declared deferred entry must exist in the clock index with that role.
    scope = set(manifest.get("scope_files") or [])
    for i, row in enumerate(manifest.get("clock_index") or []):
        if isinstance(row, dict) and row.get("file") not in scope:
            problems.append("clock_index[%d] file %r not in scope_files" % (i, row.get("file")))
    index_keys = {(r.get("file"), r.get("qualified_name"), r.get("generation_index")): r
                  for r in (manifest.get("clock_index") or []) if isinstance(r, dict)}
    for i, row in enumerate(manifest.get("deferred_entries") or []):
        if not isinstance(row, dict):
            continue
        key = (row.get("file"), row.get("qualified_name"), row.get("generation_index"))
        hit = index_keys.get(key)
        if hit is None:
            problems.append("deferred_entries[%d] has no matching clock_index row" % i)
        elif hit.get("role") != row.get("role"):
            problems.append("deferred_entries[%d] role %r != clock_index role %r"
                            % (i, row.get("role"), hit.get("role")))

    return problems


def load_manifest(manifest_path=None, sidecar_path=None, expected_digest=None):
    """Load and verify the manifest.

    Returns ``(manifest_dict, integrity_report)``.

    ``expected_digest`` overrides the pinned constant.  It exists ONLY so the mutation
    harness can point the gate at a temp manifest whose *content* it wants to test; the
    resulting run is stamped ``anchor='explicit-override'`` and is never a production
    verdict.  Production runs pass nothing and are checked against all three anchors.

    Raises ManifestError with a diagnostic code on any failure.
    """
    mpath = Path(manifest_path) if manifest_path else MANIFEST_PATH
    spath = Path(sidecar_path) if sidecar_path else Path(str(mpath) + ".sha256")

    if not mpath.is_file():
        raise ManifestError("MANIFEST_MISSING", "manifest not found at %s" % mpath)

    computed = compute_digest(mpath)

    if not spath.is_file():
        raise ManifestError("MANIFEST_DIGEST_MISSING", "sidecar not found at %s" % spath)
    sidecar = read_sidecar(spath)

    if expected_digest is None:
        anchor = "pinned-constant"
        pinned = (PINNED_MANIFEST_SHA256 or "").strip().lower()
        if not pinned:
            raise ManifestError("MANIFEST_DIGEST_MISSING",
                                "PINNED_MANIFEST_SHA256 is unset in %s" % __file__)
    else:
        anchor = "explicit-override"
        pinned = str(expected_digest).strip().lower()
        if not pinned:
            raise ManifestError("MANIFEST_DIGEST_MISSING", "explicit expected_digest is empty")

    if computed != sidecar:
        raise ManifestError(
            "MANIFEST_INTEGRITY_FAIL",
            "computed %s != sidecar %s" % (computed, sidecar))
    if computed != pinned:
        raise ManifestError(
            "MANIFEST_INTEGRITY_FAIL",
            "computed %s != %s %s" % (computed, anchor, pinned))

    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any parse failure is a schema failure
        raise ManifestError("MANIFEST_SCHEMA_FAIL", "manifest is not valid JSON: %s" % exc)

    problems = validate_schema(manifest)
    if problems:
        raise ManifestError("MANIFEST_SCHEMA_FAIL",
                            "%d schema problem(s): %s" % (len(problems), "; ".join(problems[:8])))

    report = {
        "manifest_path": str(mpath),
        "sidecar_path": str(spath),
        "computed_sha256": computed,
        "sidecar_sha256": sidecar,
        "anchor": anchor,
        "anchor_sha256": pinned,
        "anchors_agree": True,
        "schema_version": manifest.get("schema_version"),
        "schema_problems": [],
        "clock_index_rows": len(manifest.get("clock_index") or []),
        "roots": len(manifest.get("roots") or []),
        "approved_helpers": len(manifest.get("approved_helpers") or []),
        "deferred_entries": len(manifest.get("deferred_entries") or []),
    }
    return manifest, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="W3.2 timezone gate manifest integrity check")
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--sidecar", default=None)
    ap.add_argument("--expected-digest", default=None)
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args(argv)

    try:
        _, report = load_manifest(args.manifest, args.sidecar, args.expected_digest)
    except ManifestError as exc:
        payload = {"ok": False, "code": exc.code, "detail": exc.detail}
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print("[FAIL] %s -- %s" % (exc.code, exc.detail))
        return 1

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(dict(report, ok=True), indent=2), encoding="utf-8")

    print("[OK] manifest integrity")
    print("     path      : %s" % report["manifest_path"])
    print("     sha256    : %s" % report["computed_sha256"])
    print("     anchor    : %s (%s)" % (report["anchor"], report["anchor_sha256"]))
    print("     rows      : clock_index=%d roots=%d helpers=%d deferred=%d"
          % (report["clock_index_rows"], report["roots"],
             report["approved_helpers"], report["deferred_entries"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
