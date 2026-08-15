# -*- coding: utf-8 -*-
"""tools/rc_freeze_manifest.py -- Golden Baseline generator (Master Plan P2).

Produces the RELEASE CONTROL artefacts that every later phase compares against:

    <baseline>/original/<relative_path>      exact byte copy of accepted state
    <baseline>/control/golden_runtime_manifest.json
    <baseline>/control/p2_tooling_manifest.json
    <baseline>/control/golden_symbol_manifest.json
    <baseline>/control/active_binding_inventory.json

IMPORTANT DISTINCTION (Master Plan section 8 / 31):
  * GOLDEN BASELINE MANIFEST  = accepted CURRENT state (this file, P2).
  * FINAL RELEASE CONTENT MANIFEST = frozen at P12, produced separately.
They share the schema in rc_release_control.make_content_entry() but are NOT
the same artefact and must never be confused.

The runtime manifest is kept SEPARATE from the P2 tooling manifest so that
later edits to tools/ can never retroactively alter the P1-accepted runtime
baseline.

Usage:
    python tools/rc_freeze_manifest.py --baseline <dir> [--root <project>] [--no-copy]
"""
from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rc_release_control import (  # noqa: E402
    MANIFEST_SCHEMA_VERSION, ModuleIndex, dumps, first_party_modules,
    is_archival, make_content_entry, rel, role_for, sha256_file, walk_active,
)

BASE_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# What belongs to the tracked runtime baseline.
# The Master Plan list is the EXPECTED MINIMUM; the real tree is authoritative
# and is verified below (extras are reported, never silently dropped).
# --------------------------------------------------------------------------

EXPECTED_MINIMUM_RUNTIME = [
    "main.py", "storage.py", "manager_bot.py", "panel_bot.py",
    "partner_stat_bot.py", "stats_engine.py", "manager_registry.py",
    "panel_bridge.py", "soft_watchdog_pinger.py", "health_server.py",
    "proxy_provider.py", "proxy_parser.py", "preflight_check.py", "router.py",
    "profile_extractor.py", "profile_dialog.py", "profile_texts.py",
    "post_followup_texts.py", "texts.py", "geo_lexicon.py", "llm_supervisor.py",
    "login_code_parser.py", "stats_parity_harness.py",
]

RUNTIME_PACKAGES = ("features", "tdata_import")

#: Protected/closed symbols whose AST hash is frozen. Discovered names are
#: added on top of this expected minimum (section 7: "do not use only this list").
PROTECTED_EXPECTED: Dict[str, List[str]] = {
    "main.py": [
        "_create_partner_lead_event_from_daily", "_is_duplicate_systemwide",
        "_mirror_noncountable_partner_event", "_prenew_execute_renewal",
        "_handle_manager_proxy_buy_confirm_command", "_tp_ci_upsert_contact_sync",
        "_mb_enqueue_after_pipeline", "_tp_ae_fallback_record_and_reply",
        "_mb_backfill_daily_leads_from_events", "_make_client",
    ],
    "manager_bot.py": [
        "_send_event_to_user", "_upsert_card_placeholder", "_mb_claim_event",
        "_mark_send_attempt", "_mb_recover_stale_claims", "_save_card_and_sent",
        "_apply_manual_status", "_write_manual_status_to_db",
        "_handle_status_callback", "_mb_stats_authoritative_db_path",
        "_mbstat_db_candidates_for_manager", "_mbstat_leads_for_manager",
        "_mb_send_with_fallback", "_mb_classify_delivery_error",
        "_mb_is_query_invalid_error", "_touch_card_action",
        "_fetch_unsent_events_for_user",
    ],
    "stats_engine.py": ["se_manager_db_path", "se_windowed_leads"],
    "partner_stat_bot.py": [
        "_pending_events_for_source", "_pse_mark_sent", "_pse_classify_error",
        "_pse_send_notification", "_pse_is_formatting_error",
        "_pse_backoff_seconds", "_notify_live_leads_once",
    ],
    "panel_bot.py": [
        "_pb_safe_answer", "_pb_track_task", "_pb_is_query_invalid_error",
        "_tp_panel_v5_authorized", "_tp_panel_v5_remove_auth",
        "_tp_panel_v5_logout", "_tp_panel_v5_ensure_auth_table",
        "_connect_panel_db", "_take_panel_notifications", "_is_allowed",
    ],
    "soft_watchdog_pinger.py": ["_collapse_venv_children"],
    "storage.py": ["manager_queue_take_next", "_bsl_connect"],
    "panel_bridge.py": ["take_next_panel_command"],
    "features/prepared_accounts/model.py": [],   # filled by discovery below
    "features/prepared_accounts/service.py": [],
    "features/prepared_accounts/repository.py": [],
}


def discover_runtime(root: Path):
    """Authoritative tracked-runtime set.

    The Master Plan list is the EXPECTED MINIMUM only. The real tree plus the
    dependency closure are authoritative -- P2 self-review found that
    `proxy_lifecycle.py` is a MODULE-LEVEL (hard) dependency that the expected
    minimum omitted. Deriving the set from the closure prevents a hand-list
    from silently under-covering the release again.

    Returns (tracked, missing_expected, added_by_closure).
    """
    tracked: List[str] = []
    missing: List[str] = []
    for name in EXPECTED_MINIMUM_RUNTIME:
        if (root / name).is_file():
            tracked.append(name)
        else:
            missing.append(name)
    for pkg in RUNTIME_PACKAGES:
        pdir = root / pkg
        if pdir.is_dir():
            for p in walk_active(pdir, suffixes={".py"}):
                tracked.append(rel(root, p))
    # lifecycle scripts are release-relevant (stop_everything.ps1 etc.)
    for p in sorted(root.glob("*.ps1")) + sorted(root.glob("*.bat")):
        if not is_archival(p.name):
            tracked.append(rel(root, p))

    # --- authoritative completion from the dependency closure -------------
    added_by_closure: List[str] = []
    try:
        import rc_dependency_closure as dep
        res = dep.closure(root)
        for rp in res["required_package_paths"]:
            if rp not in tracked and (root / rp).is_file():
                tracked.append(rp)
                added_by_closure.append(rp)
        # SOFT first-party modules are still shipped runtime code; track them so
        # the baseline records their bytes (packaging policy is decided at P12,
        # e.g. the 20MB geo tables may be excluded per gate G5).
        for m in res["soft_first_party_modules"]:
            cand = f"{m}.py"
            if (root / cand).is_file() and cand not in tracked:
                tracked.append(cand)
                added_by_closure.append(cand)
    except Exception as exc:  # closure must never break baseline creation
        print(f"[WARN] dependency closure unavailable for tracked-set completion: {exc!r}")

    return sorted(set(tracked)), sorted(missing), sorted(set(added_by_closure))


def discover_public_symbols(root: Path, relpath: str) -> List[str]:
    """Public entrypoints of a first-party package module (section 7)."""
    p = root / relpath
    if not p.is_file():
        return []
    try:
        mi = ModuleIndex(p, root)
    except Exception:
        return []
    out = [n for n in mi.defs if not n.startswith("_")]
    out += [n for n in mi.classes if not n.startswith("_")]
    return sorted(set(out))


def build_content_entry(root: Path, relpath: str, phase: str) -> Dict[str, Any]:
    p = root / relpath
    role = role_for(relpath)
    is_py = relpath.endswith(".py")
    protected = list(PROTECTED_EXPECTED.get(relpath, []))
    if relpath.startswith("features/") or relpath.startswith("tdata_import/"):
        protected = sorted(set(protected) | set(discover_public_symbols(root, relpath)))

    sym_hashes: Dict[str, str] = {}
    if is_py and protected:
        try:
            mi = ModuleIndex(p, root)
            for s in protected:
                h = mi.symbol_ast_sha256(s)
                if h:
                    sym_hashes[s] = h
        except SyntaxError:
            pass

    entry = make_content_entry(
        relative_path=relpath,
        sha256=sha256_file(p),
        size=p.stat().st_size,
        role=role,
        phase=phase,
        compile_required=is_py and role in ("runtime", "package"),
        import_smoke_group=("entrypoint" if relpath in (
            "main.py", "panel_bot.py", "manager_bot.py", "partner_stat_bot.py",
            "soft_watchdog_pinger.py", "health_server.py", "preflight_check.py",
            "manager_registry.py") else ("package" if role == "package" else None)),
        protected_symbols=[s for s in protected if s in sym_hashes] if is_py else [],
        symbol_ast_sha256=sym_hashes,
    )
    return entry


def build_symbol_manifest(root: Path, tracked: List[str]) -> Dict[str, Any]:
    mods: Dict[str, Any] = {}
    for relpath in tracked:
        if not relpath.endswith(".py"):
            continue
        try:
            mi = ModuleIndex(root / relpath, root)
        except SyntaxError as e:
            mods[relpath] = {"error": f"SyntaxError: {e}"}
            continue
        syms: Dict[str, Any] = {}
        for name, locs in sorted(mi.defs.items()):
            caps = mi.captures.get(name, [])
            syms[name] = {
                "definition_lines": locs,
                "final_binding_line": locs[-1],
                "definition_count": len(locs),
                "shadowed_count": len(locs) - 1,
                "captures": [{"variable": cv, "line": cl,
                              "uses": mi.capture_uses.get(cv, 0)} for cv, cl in caps],
                "handler_decorators": mi.handlers.get(name, []),
                "dynamically_referenced": name in mi.dynamic,
                "final_ast_sha256": mi.symbol_ast_sha256(name),
                "classifications": {str(l): mi.classify_definition(name, l) for l in locs},
            }
        mods[relpath] = {
            "top_level_defs": sum(len(v) for v in mi.defs.values()),
            "unique_names": len(mi.defs),
            "duplicate_names": sum(1 for v in mi.defs.values() if len(v) > 1),
            "non_final_defs": sum(len(v) - 1 for v in mi.defs.values()),
            "handler_registrations": sum(len(v) for v in mi.handlers.values()),
            "dynamic_symbol_refs": len(mi.dynamic),
            "symbols": syms,
        }
    return {"schema_version": MANIFEST_SCHEMA_VERSION, "modules": mods}


def build_binding_inventory(symbol_manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Aggregated classification counts -- input for the P11-C cleanup phase."""
    counts: Dict[str, int] = {}
    per_file: Dict[str, Dict[str, int]] = {}
    rows: List[Dict[str, Any]] = []
    for relpath, mod in sorted(symbol_manifest["modules"].items()):
        if "symbols" not in mod:
            continue
        fc: Dict[str, int] = {}
        for name, s in sorted(mod["symbols"].items()):
            for line, cls in sorted(s["classifications"].items(), key=lambda kv: int(kv[0])):
                counts[cls] = counts.get(cls, 0) + 1
                fc[cls] = fc.get(cls, 0) + 1
                if cls != "ACTIVE_AUTHORITATIVE":
                    rows.append({
                        "file": relpath, "symbol": name, "line": int(line),
                        "classification": cls,
                        "final_binding_line": s["final_binding_line"],
                        "captures": s["captures"],
                        "handler_decorators": s["handler_decorators"],
                        "dynamically_referenced": s["dynamically_referenced"],
                    })
        per_file[relpath] = fc
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "totals": dict(sorted(counts.items())),
        "per_file": per_file,
        "non_authoritative_definitions": rows,
        "note": "P2 NEVER deletes. This inventory is input for phase P11-C.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", required=True)
    ap.add_argument("--root", default=str(BASE_DIR))
    ap.add_argument("--no-copy", action="store_true")
    ap.add_argument("--tooling-only", action="store_true",
                    help="regenerate only p2_tooling_manifest.json (section 31)")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    baseline = Path(a.baseline).resolve()
    control = baseline / "control"
    original = baseline / "original"
    reports = baseline / "reports"
    for d in (control, original, reports):
        d.mkdir(parents=True, exist_ok=True)

    tracked, missing_expected, added_by_closure = discover_runtime(root)

    # ---- P2 tooling manifest (kept SEPARATE from the runtime baseline) ----
    tools_dir = root / "tools"
    tooling = sorted(rel(root, p) for p in walk_active(tools_dir, suffixes={".py"}))
    tooling_entries = [build_content_entry(root, r, phase="P2") for r in tooling]
    (control / "p2_tooling_manifest.json").write_text(dumps({
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact": "P2_TOOLING_MANIFEST",
        "note": "Release-control tooling. NOT part of the P1-accepted runtime baseline.",
        "project_root": str(root),
        "file_count": len(tooling_entries),
        "files": tooling_entries,
    }), encoding="utf-8")
    if a.tooling_only:
        print(f"p2_tooling_manifest.json regenerated: {len(tooling_entries)} files")
        return 0

    # ---- golden runtime manifest (P1-accepted state) ---------------------
    entries = [build_content_entry(root, r, phase="P1-accepted") for r in tracked]
    runtime_manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "artifact": "GOLDEN_BASELINE_MANIFEST",
        "not_a_release_content_manifest": (
            "The FINAL RELEASE CONTENT MANIFEST is frozen at P12. This artefact "
            "records the P1-accepted current state only."
        ),
        "project_root": str(root),
        "file_count": len(entries),
        "expected_minimum_missing": missing_expected,
        "added_by_dependency_closure": added_by_closure,
        "files": entries,
    }
    (control / "golden_runtime_manifest.json").write_text(dumps(runtime_manifest), encoding="utf-8")

    sym = build_symbol_manifest(root, tracked)
    (control / "golden_symbol_manifest.json").write_text(dumps(sym), encoding="utf-8")
    inv = build_binding_inventory(sym)
    (control / "active_binding_inventory.json").write_text(dumps(inv), encoding="utf-8")

    # ---- copy originals, preserving directory structure ------------------
    copied = mismatched = 0
    if not a.no_copy:
        for e in entries:
            src = root / e["relative_path"]
            dst = original / e["relative_path"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
            if sha256_file(dst) != e["sha256"]:
                mismatched += 1
                print(f"[FAIL] copy SHA mismatch: {e['relative_path']}")

    protected_total = sum(len(e["protected_symbols"]) for e in entries)
    print(f"tracked runtime files : {len(entries)}")
    print(f"copied to original/   : {copied}   SHA mismatches: {mismatched}")
    print(f"protected symbols     : {protected_total}")
    print(f"tooling files         : {len(tooling_entries)}")
    print(f"binding totals        : {inv['totals']}")
    if missing_expected:
        print(f"[WARN] expected-minimum files absent from tree: {missing_expected}")
    if added_by_closure:
        print(f"[INFO] added by dependency closure (expected-minimum was incomplete): {added_by_closure}")
    return 1 if mismatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
