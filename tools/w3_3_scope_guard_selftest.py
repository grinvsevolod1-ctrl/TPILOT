# -*- coding: utf-8 -*-
"""tools/w3_3_scope_guard_selftest.py -- the LIVE W3.3 scope/file boundary (D-3).

tools/w3_2_scope_guard_selftest.py stays frozen byte-identical as a historical
artifact (its own "W3.3 not started" assertions are now expected to go RED --
disclosed, byte-identical, HISTORICAL_SCOPE_DRIFT_EXPECTED). THIS tool is the live
boundary for W3.3-A: it proves that

  * only storage.py changed among the 12 frozen runtime files;
  * storage.py itself DID change (sanity: the migration actually happened);
  * only the two approved new tool files were created under tools/ -- no other file
    there is new, and every pre-existing tools/*.py file stays byte-identical;
  * tools/w3_2_scope_guard_selftest.py and the W3.2 timezone gate manifest + its
    sidecar SHA stay byte-identical (D6/D7 are CLOSED, not reopened by W3.3-A);
  * storage.w3_resolve_schedule has exactly one active (module-level) definition;
  * allow_spend=True appears as a real call argument in exactly 2 places project-wide;
  * no mojibake marker in any file changed/created this round;
  * the repository residue scan is clean against the frozen pre-existing snapshot --
    any NEW `.bak*`/`.tmp`/`.orig`/`.rej`/restore/cursor-copy file inside
    C:\\ALM_TPilot is RED (D-4: no such file may ever be created there);
  * the external pre-edit backup of storage.py exists under this round's BACKUP dir;
  * no W3.3-B file (panel_bot.py/manager_bot.py/partner_stat_bot.py tz redirects) was
    touched in this round -- implied by the byte-identical runtime-file check above.

Baselines consumed (external, written once during the pre-work snapshot, never
inside C:\\ALM_TPilot):
  C:\\ALM_TPilot_AUDIT\\20260803\\W3_3_C1_READERS_IMPLEMENTATION\\w3_3_baseline_hashes.json
  C:\\ALM_TPilot_AUDIT\\20260803\\W3_3_C1_READERS_IMPLEMENTATION\\pre_existing_residue_snapshot.json

    python tools\\w3_3_scope_guard_selftest.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = BASE_DIR / "tools"

AUDIT_DIR = Path(r"C:\ALM_TPilot_AUDIT\20260803\W3_3_C1_READERS_IMPLEMENTATION")
BASELINE_PATH = AUDIT_DIR / "w3_3_baseline_hashes.json"
RESIDUE_BASELINE_PATH = AUDIT_DIR / "pre_existing_residue_snapshot.json"
BACKUP_DIR = AUDIT_DIR / "BACKUP"

RUNTIME_12 = (
    "storage.py", "main.py", "panel_bot.py", "preflight_check.py", "manager_bot.py",
    "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py", "manager_registry.py",
    "soft_watchdog_pinger.py", "health_server.py", "stats_parity_harness.py",
)

APPROVED_NEW_TOOLS = ("w3_3_c1_reader_parity_selftest.py", "w3_3_scope_guard_selftest.py")

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_baseline() -> dict:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def check_runtime_files(baseline: dict) -> None:
    runtime_baseline = baseline.get("runtime_12", {})
    for fname in RUNTIME_12:
        live = BASE_DIR / fname
        if not live.exists():
            check(f"runtime file present: {fname}", False, "missing")
            continue
        live_hash = sha256_of(live)
        base_hash = runtime_baseline.get(fname)
        if fname == "storage.py":
            check("storage.py DID change from the pre-edit baseline (migration applied)",
                  live_hash != base_hash, f"{live_hash} == baseline (no change detected)")
        else:
            check(f"runtime file byte-identical to pre-edit: {fname}",
                  live_hash == base_hash, f"live={live_hash} baseline={base_hash}")


def check_tools_directory(baseline: dict) -> None:
    baseline_tools = dict(baseline.get("tools_all", {}))
    live_tools = {p.name: sha256_of(p) for p in sorted(TOOLS_DIR.glob("*.py"))}

    new_files = sorted(set(live_tools) - set(baseline_tools))
    check("tools/: only the two approved new files were created",
          set(new_files) == set(APPROVED_NEW_TOOLS), f"new_files={new_files}")

    removed_files = sorted(set(baseline_tools) - set(live_tools))
    check("tools/: no pre-existing tool file was removed", not removed_files, f"removed={removed_files}")

    changed = []
    for fname, base_hash in baseline_tools.items():
        live_hash = live_tools.get(fname)
        if live_hash is not None and live_hash != base_hash:
            changed.append(fname)
    check("tools/: every pre-existing tool file stays byte-identical",
          not changed, f"changed={changed}")


def check_w3_2_frozen(baseline: dict) -> None:
    live_guard = TOOLS_DIR / "w3_2_scope_guard_selftest.py"
    base_guard_hash = baseline.get("w3_2_guard_and_baseline", {}).get("tools/w3_2_scope_guard_selftest.py")
    check("tools/w3_2_scope_guard_selftest.py stays byte-identical (D-3, frozen historical artifact)",
          sha256_of(live_guard) == base_guard_hash)

    manifest_dir = Path(r"C:\ALM_TPilot_AUDIT\W3_2_GATE_MANIFEST")
    base_manifest = baseline.get("w3_2_manifest", {})
    for fname in ("w3_2_timezone_gate_manifest.json", "w3_2_timezone_gate_manifest.json.sha256"):
        live_path = manifest_dir / fname
        check(f"W3.2 gate manifest byte-identical (not re-pinned): {fname}",
              live_path.exists() and sha256_of(live_path) == base_manifest.get(fname),
              f"missing or changed: {live_path}")


def check_single_resolver_definition() -> None:
    text = (BASE_DIR / "storage.py").read_text(encoding="utf-8-sig")
    tree = ast.parse(text)
    names = ("w3_resolve_schedule", "manager_effective_is_working",
             "manager_schedule_list_working_on_date", "_manager_effective_is_working_row",
             "_w3_historical_source_key", "_w3_resolve_work",
             "_w3_c1_ensure_validated", "_w3_c1_is_working", "_c1_legacy_list_working_on_date")
    counts = {n: 0 for n in names}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in counts:
            counts[node.name] += 1
    for n, c in counts.items():
        check(f"exactly one module-level def: {n}", c == 1, f"count={c}")


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
    # Built via chr() rather than embedded as literal characters, so this tool's
    # own source file never contains the corruption bytes it searches for.
    markers = (chr(0xd0), chr(0xd1), chr(0xe2) + chr(0x20ac))
    changed_files = ("storage.py", "tools/w3_3_c1_reader_parity_selftest.py",
                      "tools/w3_3_scope_guard_selftest.py")
    for rel in changed_files:
        path = BASE_DIR / rel
        text = path.read_text(encoding="utf-8-sig")
        hits = {m: text.count(m) for m in markers if m in text}
        check(f"mojibake scan clean: {rel}", not hits, f"hits={hits}")


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
        check("pre-existing residue snapshot present", False, str(RESIDUE_BASELINE_PATH))
        return
    baseline_residue = set(json.loads(RESIDUE_BASELINE_PATH.read_text(encoding="utf-8"))["residue"])
    live_residue = scan_residue()
    new_residue = sorted(live_residue - baseline_residue)
    check("residue scan clean: no NEW .bak/.tmp/.orig/.rej/restore/cursor-copy file inside C:\\ALM_TPilot",
          not new_residue, f"new_residue={new_residue}")


def check_backups_exist() -> None:
    matches = sorted(BACKUP_DIR.glob("storage.py.bak_w3_3a_*"))
    check("external pre-edit backup of storage.py exists under this round's BACKUP dir",
          bool(matches), f"BACKUP_DIR={BACKUP_DIR}")


def main() -> int:
    if not BASELINE_PATH.exists():
        check("baseline hashes artifact present", False, str(BASELINE_PATH))
        print(f"\nTOTAL: 1  FAILED: 1")
        return 1
    baseline = load_baseline()

    check_runtime_files(baseline)
    check_tools_directory(baseline)
    check_w3_2_frozen(baseline)
    check_single_resolver_definition()
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
