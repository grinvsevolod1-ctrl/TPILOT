# -*- coding: utf-8 -*-
"""tools/w3_1_mutation_proof.py -- required mutation proofs for the W3.1 patch
(task spec section 7, M31-1..M31-10).

Each mutation is applied to an ISOLATED TEMPORARY COPY of storage.py (never the live
source file), the mutated module is imported under a throwaway module name, targeted
assertions are run against it, and the run records whether the expected test(s) turned
red. The temp copy is discarded after every mutation; nothing here ever writes back to
C:\\ALM_TPilot\\storage.py or C:\\ALM_TPilot\\main.py.

2026-07-29 CORRECTION (independent-review findings B-1/H-1/H-2, owner decision
LOCKED). Three changes from the pre-correction version:

  1. GREEN -> RED -> GREEN CONTROL SEQUENCE (closes H-2, "no control group"). Every
     mutation now runs assert_fn three times: once against a freshly loaded UNMUTATED
     copy of storage.py (must stay green -- the NEGATIVE CONTROL), once against the
     mutated copy (must turn red -- the POSITIVE MUTATION), and once more against a
     SECOND freshly loaded unmutated copy (must be green again -- the RESTORE
     CONTROL, proving the negative result was not a fluke). A mutation is recorded as
     PASS only if all three legs behave as expected.

  2. NO MORE FALSE-PASS CHANNEL (closes H-1). The previous run_mutation() caught any
     exception raised inside assert_fn and recorded it as `turned_red=True` (PASS) --
     a cosmetic mutation plus a crashing/unrelated assertion could therefore read as a
     causal proof. Every leg of the control sequence now records its own distinct
     non-PASS status on an unexpected exception (NEG_ERROR / MUT_SETUP_FAIL /
     MUT_IMPORT_FAIL / MUT_ERROR / RESTORE_ERROR) -- an exception is NEVER silently
     treated as a passing red result again.

  3. M31-10 REWRITTEN AS A REAL, CAUSAL MUTATION (closes B-1). The previous version
     mutated nothing (`lambda src: src`) and asserted on a hand-written 3-line
     synthetic file that never resembled main.py, so its "expected_red" was
     tautologically True regardless of project state. The new version copies the
     LIVE main.py into an isolated temp workspace, inserts one real W3 resolver
     symbol reference into that temp copy ONLY, and runs the REAL scope-guard marker
     function (w3_1_scope_guard_selftest.scan_runtime_file_for_w3_markers -- the exact
     function run_all_checks() uses against every live runtime file, imported here,
     not duplicated) against both the clean and the mutated temp copy. The live
     main.py's SHA256 is hashed before and after and asserted unchanged.

    python tools\\w3_1_mutation_proof.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCE_PATH = BASE_DIR / "storage.py"
MAIN_PATH = BASE_DIR / "main.py"
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

# The REAL scope-guard marker function -- M31-10 below calls this exact function
# (not a copy/simulation of it) against a mutated temp copy of main.py.
from w3_1_scope_guard_selftest import scan_runtime_file_for_w3_markers  # noqa: E402

RESULTS: list[dict] = []


def record(mutation_id: str, description: str, expected: str, turned_red: bool,
           detail: str = "", control: str = "") -> None:
    status = "PASS" if turned_red else "FAIL"
    print(f"[{status}] {mutation_id}: {description}")
    if control:
        print(f"        control={control}")
    if detail:
        print(f"        {detail}")
    RESULTS.append({
        "mutation_id": mutation_id, "description": description, "expected": expected,
        "turned_red": turned_red, "status": status, "control": control, "detail": detail,
    })


def _load_mutated(tmp_storage_path: Path):
    """Import a (mutated OR unmutated) temp copy of storage.py under a unique module
    name so it never collides with (or contaminates) the real `storage` module cached
    in sys.modules."""
    mod_name = f"storage_ctrl_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(mod_name, tmp_storage_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _mutate(src: str, pattern: str, replacement: str, count: int = 1) -> str:
    new_src, n = re.subn(pattern, replacement, src, count=count)
    if n == 0:
        raise RuntimeError(f"mutation pattern not found: {pattern!r}")
    return new_src


def _fresh_copy(tmpdir: Path) -> Path:
    dest = tmpdir / f"storage_{uuid.uuid4().hex[:8]}.py"
    shutil.copyfile(SOURCE_PATH, dest)
    return dest


def run_mutation_with_control(base_tmpdir: Path, mutation_id: str, description: str,
                               expected: str, mutate_fn, assert_fn) -> None:
    """The green -> red -> green control sequence (closes independent-review H-2).
    assert_fn(mod, tmpdir_inner) -> (bool_is_red, detail_str), same contract as
    before. Each of the three legs gets its OWN isolated tmpdir subtree so DB-backed
    assertions (M31-6/7/8) never collide across legs."""
    work = base_tmpdir / mutation_id.replace("-", "_")
    work.mkdir(parents=True, exist_ok=True)

    # ---- leg 1: NEGATIVE CONTROL -- a freshly loaded, UNMUTATED copy must be green.
    neg_dir = work / "neg"
    neg_dir.mkdir(parents=True, exist_ok=True)
    try:
        clean_mod_1 = _load_mutated(_fresh_copy(neg_dir))
    except Exception as exc:
        record(mutation_id, description, expected, False,
               f"NEGATIVE CONTROL SETUP FAILED: unmutated copy did not even import: {exc!r}",
               control="NEG_ERROR")
        return
    try:
        neg_red, neg_detail = assert_fn(clean_mod_1, neg_dir)
    except Exception as exc:
        record(mutation_id, description, expected, False,
               f"NEGATIVE CONTROL ERROR: assert_fn raised on UNMUTATED code: {exc!r} "
               f"(an exception is never silently counted as a passing red result -- H-1 fix)",
               control="NEG_ERROR")
        return
    if neg_red:
        record(mutation_id, description, expected, False,
               f"NEGATIVE CONTROL FAILED: assert_fn is already 'red' on unmutated code -- "
               f"{neg_detail} (vacuous/tautological check -- cannot prove the mutation causes anything)",
               control="NEG_FAIL")
        return

    # ---- leg 2: POSITIVE MUTATION -- must turn red.
    src = SOURCE_PATH.read_text(encoding="utf-8")
    try:
        mutated_src = mutate_fn(src)
    except Exception as exc:
        record(mutation_id, description, expected, False, f"MUTATION SETUP FAILED: {exc}",
               control="MUT_SETUP_FAIL")
        return
    pos_dir = work / "pos"
    pos_dir.mkdir(parents=True, exist_ok=True)
    mut_path = _fresh_copy(pos_dir)
    mut_path.write_text(mutated_src, encoding="utf-8")
    try:
        mut_mod = _load_mutated(mut_path)
    except Exception as exc:
        record(mutation_id, description, expected, False, f"MUTATED MODULE FAILED TO IMPORT: {exc}",
               control="MUT_IMPORT_FAIL")
        return
    try:
        pos_red, pos_detail = assert_fn(mut_mod, pos_dir)
    except Exception as exc:
        record(mutation_id, description, expected, False,
               f"MUTATION ERROR (NOT counted as red -- H-1 fix): assert_fn raised {exc!r}",
               control="MUT_ERROR")
        return
    if not pos_red:
        record(mutation_id, description, expected, False,
               f"POSITIVE MUTATION FAILED: mutation did not turn assert_fn red -- {pos_detail}",
               control="POS_FAIL")
        return

    # ---- leg 3: RESTORE CONTROL -- a SECOND freshly loaded, UNMUTATED copy must be
    # green again, proving the negative-control result was not a fluke.
    restore_dir = work / "restore"
    restore_dir.mkdir(parents=True, exist_ok=True)
    try:
        clean_mod_2 = _load_mutated(_fresh_copy(restore_dir))
        restore_red, restore_detail = assert_fn(clean_mod_2, restore_dir)
    except Exception as exc:
        record(mutation_id, description, expected, False, f"RESTORE CONTROL ERROR: {exc!r}",
               control="RESTORE_ERROR")
        return
    if restore_red:
        record(mutation_id, description, expected, False,
               f"RESTORE CONTROL FAILED: assert_fn is red again on a second unmutated copy -- "
               f"{restore_detail} (flaky/non-deterministic check)", control="RESTORE_FAIL")
        return

    record(mutation_id, description, expected, True,
           f"green->red->green control sequence PASSED. "
           f"negative={neg_detail} | positive={pos_detail} | restore={restore_detail}",
           control="OK")


def run_m31_10(base_tmpdir: Path) -> None:
    """M31-10, rewritten 2026-07-29 (closes independent-review finding B-1). A REAL
    mutation against a temp copy of the LIVE main.py, checked with the REAL scope
    guard's scan_runtime_file_for_w3_markers -- never against live main.py itself."""
    mutation_id = "M31-10"
    description = "introduce a real W3 marker into an isolated temp copy of main.py"
    expected = "the REAL scope-guard marker function flags the mutated copy; live main.py never touched"

    if not MAIN_PATH.exists():
        record(mutation_id, description, expected, False, "main.py not found", control="SETUP_FAIL")
        return

    live_sha_before = hashlib.sha256(MAIN_PATH.read_bytes()).hexdigest()
    work = base_tmpdir / "M31_10"
    work.mkdir(parents=True, exist_ok=True)

    # ---- leg 1: NEGATIVE CONTROL -- an unmutated copy of the real main.py must be
    # clean under the REAL guard function.
    neg_dir = work / "neg"
    neg_dir.mkdir(parents=True, exist_ok=True)
    neg_main = neg_dir / "main.py"
    shutil.copyfile(MAIN_PATH, neg_main)
    neg_sha = hashlib.sha256(neg_main.read_bytes()).hexdigest()
    neg_hits = scan_runtime_file_for_w3_markers(neg_main)
    if neg_hits:
        record(mutation_id, description, expected, False,
               f"NEGATIVE CONTROL FAILED: unmutated main.py copy is already flagged by the real guard: "
               f"{neg_hits} (main.py is already dirty -- cannot attribute this to a mutation)",
               control="NEG_FAIL")
        return

    # ---- leg 2: POSITIVE MUTATION -- insert ONE real, unambiguous W3 resolver
    # symbol reference into the temp copy ONLY.
    pos_dir = work / "pos"
    pos_dir.mkdir(parents=True, exist_ok=True)
    pos_main = pos_dir / "main.py"
    shutil.copyfile(MAIN_PATH, pos_main)
    marker = "w3_resolve_schedule"
    injected = f"\n# W3.1 MUTATION-PROOF PROBE (M31-10): storage.{marker}('probe_manager', '2026-01-01')\n"
    with pos_main.open("a", encoding="utf-8") as f:
        f.write(injected)
    pos_sha = hashlib.sha256(pos_main.read_bytes()).hexdigest()
    pos_hits = scan_runtime_file_for_w3_markers(pos_main)
    turned_red = marker in pos_hits
    live_sha_mid = hashlib.sha256(MAIN_PATH.read_bytes()).hexdigest()
    if not turned_red:
        record(mutation_id, description, expected, False,
               f"POSITIVE MUTATION FAILED: real guard did not flag the mutated copy -- hits={pos_hits}",
               control="POS_FAIL")
        return
    if pos_sha == neg_sha:
        record(mutation_id, description, expected, False,
               "POSITIVE MUTATION FAILED: mutated copy's SHA256 did not change -- the write did not happen",
               control="POS_FAIL")
        return

    # ---- leg 3: RESTORE CONTROL -- overwrite the temp copy with the original bytes
    # again; must go green under the same real guard function.
    shutil.copyfile(MAIN_PATH, pos_main)
    restore_sha = hashlib.sha256(pos_main.read_bytes()).hexdigest()
    restore_hits = scan_runtime_file_for_w3_markers(pos_main)

    # ---- SAFETY: the LIVE main.py itself must never be touched, at any point.
    live_sha_after = hashlib.sha256(MAIN_PATH.read_bytes()).hexdigest()

    ok = (
        not neg_hits
        and turned_red
        and (pos_sha != neg_sha)
        and (not restore_hits)
        and (restore_sha == neg_sha)
        and (live_sha_before == live_sha_mid == live_sha_after)
    )
    failing_assertion = (
        f"check(f\"no {marker!r} marker in main.py\", {marker!r} not in "
        f"scan_runtime_file_for_w3_markers(BASE_DIR / 'main.py'))"
        " -- the exact assertion shape w3_1_scope_guard_selftest.run_all_checks() evaluates for every RUNTIME_FILES entry"
    )
    detail = (
        f"inserted_marker={marker!r} | "
        f"live_main.py sha256: before={live_sha_before[:12]} mid={live_sha_mid[:12]} "
        f"after={live_sha_after[:12]} (all three must match -- live main.py never touched) | "
        f"temp copy sha256: pre-mutation={neg_sha[:12]} post-mutation={pos_sha[:12]} "
        f"(differ -> mutation genuinely applied) restored={restore_sha[:12]} "
        f"(matches pre-mutation -> clean restore) | "
        f"guard hits: negative(clean)={neg_hits} positive(mutated)={pos_hits} restore(reverted)={restore_hits} | "
        f"failing scope-guard assertion this reproduces: {failing_assertion}"
    )
    record(mutation_id, description, expected, ok, detail, control="OK" if ok else "MIXED_FAIL")


def run_m31_9(base_tmpdir: Path) -> None:
    """M31-9: make W3 schema initialize automatically on import/startup. STATIC ONLY
    (task spec safety requirement) -- the mutated source is never dynamically
    imported/executed, since a real top-level ensure_w3_schedule_versioning() call
    would run at module load time against whatever QUEUE_DB_PATH resolves to in THIS
    process's environment. Uses the exact import-time-call detector
    w3_1_scope_guard_selftest.py uses (a call is import-time if its line falls outside
    every FunctionDef/AsyncFunctionDef range).

    2026-07-29 CORRECTION: added a third leg (RESTORE) that re-parses the clean
    source a second, independent time, proving the negative-control result is
    deterministic and not an artifact of caching -- the green->red->green shape
    applied to a static check (closes H-2 for this mutation too)."""
    mutation_id = "M31-9"
    description = "make W3 schema initialize automatically on import/startup"
    expected = "startup safety test fails (import-time call site detected)"

    import ast as _ast

    def _import_time_call_sites(src: str, func_name: str) -> list[int]:
        tree = _ast.parse(src)
        func_ranges = [
            (n.lineno, getattr(n, "end_lineno", n.lineno))
            for n in _ast.walk(tree)
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        ]
        calls = [n.lineno for n in _ast.walk(tree)
                 if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id == func_name]
        return [ln for ln in calls if not any(lo <= ln <= hi for lo, hi in func_ranges)]

    clean_src = SOURCE_PATH.read_text(encoding="utf-8")

    # leg 1: negative control
    neg_sites = _import_time_call_sites(clean_src, "ensure_w3_schedule_versioning")
    if neg_sites:
        record(mutation_id, description, expected, False,
               f"NEGATIVE CONTROL FAILED: clean source already has import-time call sites: {neg_sites}",
               control="NEG_FAIL")
        return

    # leg 2: positive mutation
    mutated_src = clean_src.replace(
        'W3_TZ_NAME = "Europe/Kyiv"\n',
        'W3_TZ_NAME = "Europe/Kyiv"\n'
        'try:\n    ensure_w3_schedule_versioning()  # MUTATED M31-9: real startup auto-init\n'
        'except Exception:\n    pass\n',
        1,
    )
    pos_sites = _import_time_call_sites(mutated_src, "ensure_w3_schedule_versioning")
    if not pos_sites:
        record(mutation_id, description, expected, False,
               "POSITIVE MUTATION FAILED: mutated source still has zero import-time call sites",
               control="POS_FAIL")
        return

    # leg 3: restore control -- re-parse the clean source again, independently
    restore_sites = _import_time_call_sites(clean_src, "ensure_w3_schedule_versioning")
    if restore_sites:
        record(mutation_id, description, expected, False,
               f"RESTORE CONTROL FAILED: re-parsing the clean source a second time found "
               f"call sites: {restore_sites} (non-deterministic detector)", control="RESTORE_FAIL")
        return

    record(mutation_id, description, expected, True,
           f"green->red->green PASSED (static analysis only, mutated source never imported/executed). "
           f"negative(clean) sites={neg_sites} | positive(mutated) sites={pos_sites} | "
           f"restore(clean, re-parsed) sites={restore_sites}",
           control="OK")


def main() -> int:
    storage_sha_before = hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
    main_sha_before = hashlib.sha256(MAIN_PATH.read_bytes()).hexdigest()

    with tempfile.TemporaryDirectory(prefix="w3_1_mutation_") as tmpdir_s:
        tmpdir = Path(tmpdir_s)

        # M31-1: remove duplicate==1 authoritative handling -> duplicate tests fail
        def mut_m31_1(src):
            return _mutate(
                src,
                r'    if duplicate:\n        is_duplicate = True\n        authoritative_signal = "DUP_AUTHORITATIVE_FLAG"\n',
                '    if False:  # MUTATED M31-1: duplicate==1 handling removed\n        is_duplicate = True\n        authoritative_signal = "DUP_AUTHORITATIVE_FLAG"\n',
            )

        def assert_m31_1(mod, _tmpdir):
            r = mod.w3_duplicate_status_eligibility({"duplicate": 1, "lead_countable": 1})
            expected_red = not (r["is_duplicate"] is True and r["countable"] is False)
            return expected_red, f"result: is_duplicate={r['is_duplicate']} countable={r['countable']}"

        run_mutation_with_control(tmpdir, "M31-1", "remove duplicate==1 authoritative handling",
                                   "duplicate tests (J14-style) fail", mut_m31_1, assert_m31_1)

        # M31-2: treat provisional (tier-2) signals as authoritative
        def mut_m31_2(src):
            return _mutate(
                src,
                r'    elif event_type == "duplicate_card":\n        is_duplicate = True\n        authoritative_signal = "DUP_AUTHORITATIVE_EVENT"\n',
                '    elif event_type == "duplicate_card":\n        is_duplicate = True\n        authoritative_signal = "DUP_AUTHORITATIVE_EVENT"\n    elif contact_kind in _W3_DUP_PENDING_CONTACT_KINDS:  # MUTATED M31-2\n        is_duplicate = True\n        authoritative_signal = "DUP_AUTHORITATIVE_EVENT"\n',
            )

        def assert_m31_2(mod, _tmpdir):
            r = mod.w3_duplicate_status_eligibility({"duplicate": 0, "duplicate_checked": 0, "contact_kind": "returning"})
            expected_red = r["is_duplicate"] is True  # false-positive protection test (J16-style) must fail
            return expected_red, f"result: is_duplicate={r['is_duplicate']}"

        run_mutation_with_control(tmpdir, "M31-2", "treat provisional signals as authoritative",
                                   "false-positive protection tests (J15-J17-style) fail", mut_m31_2, assert_m31_2)

        # M31-3: apply provisional signals even when duplicate_checked==1 (remove P5)
        def mut_m31_3(src):
            return _mutate(
                src,
                r'    if not is_duplicate and not duplicate_checked:\n',
                '    if not is_duplicate:  # MUTATED M31-3: P5 fallback removed (duplicate_checked ignored)\n',
            )

        def assert_m31_3(mod, _tmpdir):
            r = mod.w3_duplicate_status_eligibility({
                "duplicate": 0, "duplicate_checked": 1, "lead_countable": 0,
                "contact_kind": "returning", "dedupe_reason": "other_manager",
            })
            expected_red = r["pending_block"] is True  # J19-style: should be False if unmutated
            return expected_red, f"result: pending_block={r['pending_block']}"

        run_mutation_with_control(tmpdir, "M31-3", "apply provisional signals even when duplicate_checked==1",
                                   "J19/J22-style tests fail", mut_m31_3, assert_m31_3)

        # M31-4: allow authoritative duplicate to be countable
        def mut_m31_4(src):
            return _mutate(
                src,
                r'    countable = \(not is_duplicate\) and \(not lead_countable_zero\)\n',
                '    countable = not lead_countable_zero  # MUTATED M31-4: is_duplicate no longer forces False\n',
            )

        def assert_m31_4(mod, _tmpdir):
            r = mod.w3_duplicate_status_eligibility({"duplicate": 1, "lead_countable": 1})
            expected_red = r["countable"] is True  # C-11 (J14-style): should be False if unmutated
            return expected_red, f"result: countable={r['countable']}"

        run_mutation_with_control(tmpdir, "M31-4", "allow authoritative duplicate to be countable",
                                   "C-11 tests (J14/J18-style) fail", mut_m31_4, assert_m31_4)

        # M31-4b (NEW, 2026-07-29 correction): allow an S-2-only (event_type=
        # 'duplicate_card' with no daily_leads row) authoritative duplicate to be
        # countable -- the EXACT independent-review F-1 defect. Mutating `is_duplicate`
        # back out of the countable formula would also be caught by M31-4 above; this
        # mutation instead targets the corrected formula's use of `is_duplicate`
        # specifically for the S-2 (event-only) path, by re-introducing the original
        # (pre-correction) raw-`duplicate`-only formula.
        def mut_m31_4b(src):
            return _mutate(
                src,
                r'    countable = \(not is_duplicate\) and \(not lead_countable_zero\)\n',
                '    countable = (not duplicate) and (not lead_countable_zero)  # MUTATED M31-4b: F-1 regression reintroduced\n',
            )

        def assert_m31_4b(mod, _tmpdir):
            r = mod.w3_duplicate_status_eligibility({"event_type": "duplicate_card"})
            expected_red = (r["is_duplicate"] is True) and (r["countable"] is True)
            return expected_red, f"result: is_duplicate={r['is_duplicate']} countable={r['countable']}"

        run_mutation_with_control(tmpdir, "M31-4b", "reintroduce the F-1 regression (S-2-only row countable=True)",
                                   "F1_AUTHORITATIVE_EVENT_MUST_NOT_BE_COUNTABLE fails", mut_m31_4b, assert_m31_4b)

        # M31-5: delete/scrub stale status in the eligibility result
        def mut_m31_5(src):
            return _mutate(
                src,
                r'    stale_fields = \[\n        f for f in \("status", "manual_status_override", "quality_status", "quality_bucket"\)\n        if row\.get\(f\) not in \(None, "", 0\)\n    \]\n',
                '    stale_fields = []  # MUTATED M31-5: stale status scrubbed instead of preserved\n',
            )

        def assert_m31_5(mod, _tmpdir):
            row = {"duplicate": 1, "status": "liquid"}
            row_copy = dict(row)
            r = mod.w3_duplicate_status_eligibility(row)
            preserved = row == row_copy and bool(r["stale_status_fields"])
            expected_red = not preserved  # C-12 preservation test should fail under mutation
            return expected_red, f"result: stale_status_fields={r['stale_status_fields']}"

        run_mutation_with_control(tmpdir, "M31-5", "delete/scrub stale status in eligibility result",
                                   "C-12 preservation tests fail", mut_m31_5, assert_m31_5)

        # M31-6: remove the overlap trigger (schema mutation, not the duplicate gate).
        # Renaming a CREATE TRIGGER statement would NOT disable it (SQLite still
        # creates and fires it under the new name) -- the effective mutation is to
        # stop the trigger DDL from ever being executed at all, by skipping the loop
        # that runs _W3_OVERLAP_TRIGGER_DDL.
        def mut_m31_6(src):
            return _mutate(
                src,
                r"        for stmt in _W3_OVERLAP_TRIGGER_DDL:\n            con\.execute\(stmt\)\n",
                "        for stmt in ():  # MUTATED M31-6: overlap triggers never created\n            con.execute(stmt)\n",
            )

        def assert_m31_6(mod, tmpdir_inner):
            db = tmpdir_inner / "m31_6.db"
            mod.ensure_w3_schedule_versioning(str(db))
            con = sqlite3.connect(str(db))
            # Open version: 2026-01-01 .. (open-ended). A second row that ALSO overlaps
            # this window but is itself already-closed (effective_to set, and a
            # different effective_from) does NOT trip either partial UNIQUE index
            # (w3_..._one_open_idx only fires on two simultaneously-open rows;
            # w3_..._from_uniq_idx only fires on an identical effective_from) -- it is
            # caught ONLY by the BEFORE INSERT overlap trigger (layer 2). This isolates
            # the trigger's contribution from the index layers so disabling only the
            # trigger is provably detected.
            con.execute(
                "INSERT INTO w3_manager_schedule_version(manager_key, dimension, effective_from, effective_to, created_at)"
                " VALUES('mgrOverlap','work','2026-01-01',NULL,'')"
            )
            con.commit()
            overlap_raised = False
            try:
                con.execute(
                    "INSERT INTO w3_manager_schedule_version(manager_key, dimension, effective_from, effective_to, created_at)"
                    " VALUES('mgrOverlap','work','2025-06-01','2026-06-01','')"
                )
                con.commit()
            except sqlite3.IntegrityError:
                overlap_raised = True
            con.close()
            expected_red = not overlap_raised  # schema overlap test should fail (no abort) under mutation
            return expected_red, f"overlap_raised={overlap_raised}"

        run_mutation_with_control(tmpdir, "M31-6", "remove overlap trigger",
                                   "schema overlap test fails", mut_m31_6, assert_m31_6)

        # M31-7: commit legacy compatibility write BEFORE version failure (breaks atomicity)
        def mut_m31_7(src):
            return _mutate(
                src,
                r'            _w3_ensure_manager_client_message_schedule\(con\)\n            legacy_old = con\.execute\(',
                '            _w3_ensure_manager_client_message_schedule(con)\n            con.execute(\n                "INSERT INTO manager_client_message_schedule(manager_key, day_start, day_end, night_start, night_end, updated_by_user_id, updated_at)"\n                " VALUES(?,?,?,?,?,?,?)"\n                " ON CONFLICT(manager_key) DO UPDATE SET day_start=excluded.day_start, day_end=excluded.day_end,"\n                " night_start=excluded.night_start, night_end=excluded.night_end,"\n                " updated_by_user_id=excluded.updated_by_user_id, updated_at=excluded.updated_at",\n                (mk, times["day_start"], times["day_end"], times["night_start"], times["night_end"], actor_user_id, _now_iso()),\n            )\n            con.execute("COMMIT")  # MUTATED M31-7: early premature commit before version write\n            con.execute("BEGIN IMMEDIATE")\n            legacy_old = con.execute(',
            )

        def assert_m31_7(mod, tmpdir_inner):
            db = tmpdir_inner / "m31_7.db"
            mod.w3_manager_version_set(
                "mgrRB7", "message",
                {"day_start_min": 480, "day_end_min": 1020, "night_start_min": 1020, "night_end_min": 480},
                effective_from="2026-06-01", db_path=str(db),
            )
            try:
                mod.w3_message_schedule_write("mgrRB7", "07:00", "16:00", "16:00", "07:00",
                                               effective_from="2020-01-01", db_path=str(db))
            except sqlite3.IntegrityError:
                pass
            except Exception:
                pass
            con = sqlite3.connect(str(db))
            try:
                n = con.execute(
                    "SELECT COUNT(*) FROM manager_client_message_schedule WHERE manager_key='mgrRB7'"
                ).fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            con.close()
            leaked = n > 0  # legacy row survives the failed version write -> atomicity broken
            expected_red = leaked
            return expected_red, f"legacy rows after failed version write: {n}"

        run_mutation_with_control(tmpdir, "M31-7", "commit legacy compatibility write before version failure",
                                   "atomic rollback test fails", mut_m31_7, assert_m31_7)

        # M31-8: populate W3 tables automatically during a resolver READ
        def mut_m31_8(src):
            return _mutate(
                src,
                r'            ensure_w3_manager_client_message_schedule_table\(db_path\)\n            ensure_w3_schedule_versioning\(db_path\)\n\n        if source_key is not None:',
                '            ensure_w3_manager_client_message_schedule_table(db_path)\n            ensure_w3_schedule_versioning(db_path)\n            con.execute(\n                "INSERT INTO w3_schedule_audit(entity, entity_id, action, updated_at) VALUES(\'MUTATED_M31_8\', 0, \'create\', \'\')"\n            )\n            con.commit()\n\n        if source_key is not None:',
            )

        def assert_m31_8(mod, tmpdir_inner):
            db = tmpdir_inner / "m31_8.db"
            mod.w3_resolve_schedule("mgrRead", "2026-07-29", db_path=str(db))
            con = sqlite3.connect(str(db))
            n = con.execute("SELECT COUNT(*) FROM w3_schedule_audit").fetchone()[0]
            con.close()
            expected_red = n > 0  # inertness test should fail: a READ populated a w3_* table
            return expected_red, f"w3_schedule_audit rows after a pure READ: {n}"

        run_mutation_with_control(tmpdir, "M31-8", "populate W3 tables automatically during resolver read",
                                   "inertness test fails", mut_m31_8, assert_m31_8)

        # M31-9: static-only startup-safety proof (see run_m31_9 docstring for why).
        run_m31_9(tmpdir)

        # M31-10: REWRITTEN 2026-07-29 -- real mutation against a temp copy of the
        # live main.py, checked with the real scope-guard marker function (see
        # run_m31_10 docstring; closes independent-review finding B-1).
        run_m31_10(tmpdir)

        # ------------------------------------------------------------------
        # Write results
        # ------------------------------------------------------------------
        storage_sha_after = hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest()
        main_sha_after = hashlib.sha256(MAIN_PATH.read_bytes()).hexdigest()
        self_check = {
            "storage_py_sha256_before": storage_sha_before,
            "storage_py_sha256_after": storage_sha_after,
            "storage_py_unchanged": storage_sha_before == storage_sha_after,
            "main_py_sha256_before": main_sha_before,
            "main_py_sha256_after": main_sha_after,
            "main_py_unchanged": main_sha_before == main_sha_after,
        }

        out_dir = Path(r"C:\ALM_TPilot_AUDIT\20260729\W3_1_CORRECTION")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "mutation_results.json").write_text(
            json.dumps({"mutations": RESULTS, "self_check": self_check}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print()
    if not self_check["storage_py_unchanged"] or not self_check["main_py_unchanged"]:
        print(f"RESULT: FAIL (SELF-CHECK VIOLATION -- a live file changed during the run: {self_check})")
        return 1
    failed = [r for r in RESULTS if r["status"] != "PASS"]
    if failed:
        print(f"RESULT: FAIL ({len(failed)} mutation(s) did NOT complete the green->red->green control sequence)")
        for r in failed:
            print(f"  - {r['mutation_id']} [{r['control']}]: {r['description']}")
        return 1
    print(f"RESULT: PASS (all {len(RESULTS)} mutations completed green->red->green; "
          f"live storage.py and main.py unchanged)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
