# -*- coding: utf-8 -*-
"""tools/w3_2_mutation_proof.py -- required mutation proofs for the W3.2 patch
(task spec section 11, M32-1..M32-10).

Follows the W3.1 mutation-proof pattern (tools/w3_1_mutation_proof.py): every mutation
is applied to an ISOLATED TEMPORARY COPY of the relevant source file, never the live
one; each mutation runs a GREEN (unmutated) -> RED (mutated) -> GREEN (restore) control
sequence so a crashing/unrelated assertion can never be misread as a causal proof; the
live source files' SHA256 hashes are asserted unchanged before and after the whole run.

No DB is opened against the real project database; no network; no Telegram.

    python tools\\w3_2_mutation_proof.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import sys
import tempfile
import uuid
from datetime import date, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from w3_2_timezone_contract_selftest import count_bare_datetime_now_today  # noqa: E402
import w3_2_scope_guard_selftest as _sg_mod  # noqa: E402
from w3_2_scope_guard_selftest import (  # noqa: E402
    scan_text_for_markers, run_scope_guard, run_hash_diff_boundary, run_marker_scan,
    TOP_LEVEL_INVENTORY_PATH,
)
from w3_2_panel_static_gate_selftest import run_panel_static_gate  # noqa: E402

STORAGE_PATH = BASE_DIR / "storage.py"
PREFLIGHT_PATH = BASE_DIR / "preflight_check.py"
MAIN_PATH = BASE_DIR / "main.py"
STATS_ENGINE_PATH = BASE_DIR / "stats_engine.py"
PANEL_BOT_PATH = BASE_DIR / "panel_bot.py"
MANAGER_BOT_PATH = BASE_DIR / "manager_bot.py"
BASELINE_PATH = Path(r"C:\ALM_TPilot_AUDIT\20260730\W3_2_CORRECTION\baseline_hashes.json")

# The full watched-runtime-file set the hardened scope guard compares against the
# frozen baseline -- needed to build a realistic temp tree for M32C-4/M32C-5 (the guard
# reads every one of these from base_dir).
WATCHED_RUNTIME_FILES = [
    "storage.py", "main.py", "panel_bot.py", "preflight_check.py", "manager_bot.py",
    "partner_stat_bot.py", "stats_engine.py", "panel_bridge.py", "manager_registry.py",
    "soft_watchdog_pinger.py", "health_server.py", "stats_parity_harness.py",
]

RESULTS: list[dict] = []


def record(mutation_id: str, description: str, expected: str, passed: bool,
           detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {mutation_id}: {description}")
    if detail:
        print(f"        {detail}")
    RESULTS.append({"mutation_id": mutation_id, "description": description,
                     "expected": expected, "status": status, "detail": detail})


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mutate(src: str, pattern: str, replacement: str, count: int = 1) -> str:
    new_src, n = re.subn(pattern, replacement, src, count=count, flags=re.M)
    if n == 0:
        raise RuntimeError(f"mutation pattern not found: {pattern!r}")
    return new_src


def _load_module(path: Path):
    mod_name = f"w32_mut_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fresh_copy(src_path: Path, tmpdir: Path) -> Path:
    dest = tmpdir / f"{src_path.stem}_{uuid.uuid4().hex[:8]}{src_path.suffix}"
    shutil.copyfile(src_path, dest)
    return dest


def run_mutation(base_tmpdir: Path, mutation_id: str, description: str, expected: str,
                  make_mutant, assert_red) -> None:
    """GREEN (unmutated) -> RED (mutated) -> GREEN (restore) control sequence.
    make_mutant(tmpdir) -> mutated_thing; assert_red(thing) -> (is_red: bool, detail: str).
    A mutation PASSES only if: unmutated is green, mutated is red, a second fresh
    unmutated is green again."""
    work = base_tmpdir / mutation_id.replace("-", "_")
    work.mkdir(parents=True, exist_ok=True)
    try:
        neg_thing = make_mutant(work / "neg", mutate=False)
        neg_red, neg_detail = assert_red(neg_thing)
        if neg_red:
            record(mutation_id, description, expected, False,
                   f"NEGATIVE CONTROL already red before mutation (broken test): {neg_detail}")
            return

        mut_thing = make_mutant(work / "mut", mutate=True)
        mut_red, mut_detail = assert_red(mut_thing)
        if not mut_red:
            record(mutation_id, description, expected, False,
                   f"mutation did NOT turn the check red: {mut_detail}")
            return

        restore_thing = make_mutant(work / "restore", mutate=False)
        restore_red, restore_detail = assert_red(restore_thing)
        if restore_red:
            record(mutation_id, description, expected, False,
                   f"RESTORE CONTROL still red after removing mutation (flaky/leaky test): {restore_detail}")
            return

        record(mutation_id, description, expected, True,
               f"green->red->green confirmed. red-leg detail: {mut_detail}")
    except Exception as exc:
        record(mutation_id, description, expected, False,
               f"{type(exc).__name__}: {exc}")


def main() -> int:
    pre_hashes = {p.name: _sha256(p) for p in
                  (STORAGE_PATH, PREFLIGHT_PATH, MAIN_PATH, STATS_ENGINE_PATH, PANEL_BOT_PATH)}

    with tempfile.TemporaryDirectory(prefix="w32_mut_") as tmp:
        base_tmp = Path(tmp)

        # ---- M32-1: replace Europe/Kyiv with host-local datetime.now().
        # Expected: OS-timezone independence test fails (mutated w3_now() returns a
        # NAIVE datetime -- the contract requires always-aware -- and its clock source
        # is no longer tied to the named zone at all).
        def m1_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STORAGE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r"def w3_now\(tz_name=W3_TZ_NAME\):\n(    # type.*\n)?    \"\"\".*?\"\"\"\n    return datetime\.now\(w3_tz\(tz_name\)\)",
                    "def w3_now(tz_name=W3_TZ_NAME):\n    return datetime.now()",
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m1_assert(copy_path):
            mod = _load_module(copy_path)
            result = mod.w3_now()
            is_red = result.tzinfo is None
            return is_red, f"w3_now() tzinfo={result.tzinfo!r} (contract requires always-aware)"

        run_mutation(base_tmp, "M32-1", "replace Europe/Kyiv with host-local datetime.now()",
                     "OS-timezone independence test fails (result becomes naive)", m1_make, m1_assert)

        # ---- M32-2: restore a silent timezone fallback.
        # Expected: zero-fallback guard (w3_tz raising on a bad zone) fails.
        def m2_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STORAGE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r"    try:\n        zi = _W3_ZoneInfo\(name\)\n    except Exception as exc:\n        raise W3TimezoneError\(f\"w3: cannot load timezone \{name!r\}: \{exc\}\"\) from exc\n",
                    "    try:\n        zi = _W3_ZoneInfo(name)\n"
                    "    except Exception:\n        zi = _W3_ZoneInfo(\"UTC\")  # MUTATION: silent fallback restored\n",
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m2_assert(copy_path):
            mod = _load_module(copy_path)
            try:
                mod.w3_tz("Not/A_Real_Zone_xyz")
                return True, "w3_tz() on a bad zone name did NOT raise -- silent fallback present"
            except mod.W3TimezoneError:
                return False, "w3_tz() still raises W3TimezoneError (guard intact)"

        run_mutation(base_tmp, "M32-2", "restore a silent timezone fallback",
                     "zero-fallback guard fails (w3_tz no longer raises)", m2_make, m2_assert)

        # ---- M32-3: force night duration to fixed 15 hours.
        # Expected: spring/fall DST matrix fails (14h/16h no longer produced).
        def m3_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STORAGE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r"            night_start_dt = prev_day_end_dt\n            night_end_dt = day_start_dt\n",
                    "            night_start_dt = prev_day_end_dt\n"
                    # MUTATION: a REAL (UTC-elapsed) 15-hour addition, ignoring the actual
                    # DST-adjusted boundary -- landing on 09:00 instead of 08:00 across the
                    # spring gap (naive wall-clock '+timedelta' would auto-correct back to
                    # the right wall time via zoneinfo's lazy offset resolution and defeat
                    # the mutation, so this goes through UTC explicitly).
                    "            night_end_dt = (night_start_dt.astimezone(_w3_utc_tz) + timedelta(hours=15)).astimezone(tz)\n",
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m3_assert(copy_path):
            mod = _load_module(copy_path)
            spring = _last_sunday(2026, 3)
            r = mod.w3_resolve_schedule("mgr_mut_test3", business_date=spring.isoformat(),
                                         db_path=str(copy_path.parent / f"mut3_{uuid.uuid4().hex[:8]}.db"))
            start = mod.datetime.fromisoformat(r["stats"]["night_start_local"])
            end = mod.datetime.fromisoformat(r["stats"]["night_end_local"])
            hours = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600.0
            is_red = hours != 14.0
            return is_red, f"spring night hours={hours} (contract requires 14.0)"

        run_mutation(base_tmp, "M32-3", "force night duration to fixed 15 hours",
                     "spring/fall DST matrix fails", m3_make, m3_assert)

        # ---- M32-4: use fold=0 for ambiguous END.
        # Expected: fall-back tiling/duration test fails.
        def m4_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STORAGE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, r'fold = 1 if which == "end" else 0', "fold = 0  # MUTATION: always fold=0")
                copy.write_text(src, encoding="utf-8")
            return copy

        def m4_assert(copy_path):
            mod = _load_module(copy_path)
            fall = _last_sunday(2026, 10)
            start = mod.w3_local_at(fall.isoformat(), 3 * 60 + 30, which="start", degraded=[])
            end = mod.w3_local_at(fall.isoformat(), 3 * 60 + 30, which="end", degraded=[])
            diff = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() / 3600.0
            is_red = diff != 1.0
            return is_red, f"fall ambiguous start/end diff={diff}h (contract requires 1.0h)"

        run_mutation(base_tmp, "M32-4", "use fold=0 for ambiguous END",
                     "fall-back tiling/duration test fails", m4_make, m4_assert)

        # ---- M32-5: remove dst-gap snapping.
        # Expected: spring-forward boundary test fails.
        def m5_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STORAGE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r"    if is_gap:\n        if degraded is not None:\n            degraded\.append\(\"dst_gap_snapped\"\)\n        return _w3_snap_dst_gap\(naive, tz\)\n",
                    "    if is_gap:\n        pass  # MUTATION: gap snapping removed\n",
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m5_assert(copy_path):
            mod = _load_module(copy_path)
            spring = _last_sunday(2026, 3)
            deg = []
            dt = mod.w3_local_at(spring.isoformat(), 3 * 60 + 30, which="start", degraded=deg)
            is_red = dt.strftime("%H:%M") != "04:00" or "dst_gap_snapped" not in deg
            return is_red, f"spring gap boundary -> {dt} degraded={deg} (contract requires 04:00 + dst_gap_snapped)"

        run_mutation(base_tmp, "M32-5", "remove dst-gap snapping",
                     "spring-forward boundary test fails", m5_make, m5_assert)

        # ---- M32-6: compute night start independently instead of using resolved day end.
        # Expected: structural tiling test fails.
        def m6_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STORAGE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r'            prev_day_end_dt = w3_local_at\(night_anchor, sday_end, which="end", tz_name=tz_name, degraded=degraded\)\n            night_start_dt = prev_day_end_dt\n',
                    '            prev_day_end_dt = w3_local_at(night_anchor, sday_end, which="end", tz_name=tz_name, degraded=degraded)\n'
                    '            # MUTATION: independently recomputed against TODAY\'s date instead of reusing\n'
                    '            # the previous day\'s resolved day-end instant\n'
                    '            night_start_dt = w3_local_at(bd, sday_end, which="end", tz_name=tz_name, degraded=degraded)\n',
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m6_assert(copy_path):
            mod = _load_module(copy_path)
            fall = _last_sunday(2026, 10)
            r = mod.w3_resolve_schedule("mgr_mut_test", business_date=fall.isoformat(),
                                         db_path=str(copy_path.parent / f"mut6_{uuid.uuid4().hex[:8]}.db"))
            day_end = mod.w3_local_at((fall - timedelta(days=1)).isoformat(), r["stats"]["day_end_min"],
                                       which="end", degraded=[])
            # Compare via the resolver's own reported ISO string against an
            # independently-recomputed day-end (using the untouched which="end" path)
            # -- no reliance on any helper the mutation might also affect.
            is_red = r["stats"]["night_start_local"] != day_end.isoformat()
            return is_red, f"night_start_local={r['stats']['night_start_local']} day_end={day_end.isoformat()}"

        run_mutation(base_tmp, "M32-6", "compute night start independently instead of using resolved day end",
                     "structural tiling test fails", m6_make, m6_assert)

        # ---- M32-7: write aware ISO timestamp into a legacy naive-UTC column fixture.
        # Expected: storage-format test fails.
        def m7_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STORAGE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r"    resolved_at_utc = _now_iso\(\)\n",
                    "    resolved_at_utc = datetime.now(_W3_ZoneInfo(\"Europe/Kyiv\")).isoformat()  # MUTATION: aware ISO into a naive-UTC field\n",
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m7_assert(copy_path):
            mod = _load_module(copy_path)
            r = mod.w3_resolve_schedule("mgr_mut_test7", business_date="2026-07-15",
                                         db_path=str(copy_path.parent / f"mut7_{uuid.uuid4().hex[:8]}.db"))
            resolved = r["resolved_at_utc"]
            is_red = ("+" in resolved) or ("Z" in resolved)
            return is_red, f"resolved_at_utc={resolved!r} (naive-UTC form must have no offset marker)"

        run_mutation(base_tmp, "M32-7", "write aware ISO timestamp into a legacy naive-UTC column fixture",
                     "storage-format test fails", m7_make, m7_assert)

        # ---- M32-8: reintroduce bare datetime.now() into active preflight business-time path.
        # Expected: active-definition/static guard fails.
        def m8_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PREFLIGHT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r"def kyiv_today\(\) -> date:\n    return storage\.w3_now\(\)\.date\(\)\n",
                    "def kyiv_today() -> date:\n    return datetime.now().date()  # MUTATION: bare OS-local call reintroduced\n",
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m8_assert(copy_path):
            hits = count_bare_datetime_now_today(copy_path)
            is_red = len(hits) > 0
            return is_red, f"bare datetime.now()/today() call sites: {hits} (guard requires 0)"

        run_mutation(base_tmp, "M32-8", "reintroduce bare datetime.now() into active preflight business-time path",
                     "active-definition/static guard fails", m8_make, m8_assert)

        # ---- M32-9: redirect a C1 or C2 reader prematurely.
        # Expected: W3.2 scope guard fails.
        def m9_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(MAIN_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(
                    src,
                    r"def _kyiv_now\(\) -> datetime:",
                    "def _prematurely_redirected_reader():\n"
                    "    import storage as _s\n"
                    "    return _s.w3_resolve_schedule(\"x\", business_date=\"2026-01-01\")\n\n\n"
                    "def _kyiv_now() -> datetime:",
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m9_assert(copy_path):
            src = copy_path.read_text(encoding="utf-8")
            hits = scan_text_for_markers(src, ["w3_resolve_schedule("])
            is_red = bool(hits)
            return is_red, f"w3_resolve_schedule( call found in main.py copy: {hits}"

        run_mutation(base_tmp, "M32-9", "redirect a C1 or C2 reader prematurely",
                     "W3.2 scope guard fails", m9_make, m9_assert)

        # ---- M32-10: introduce timezone helper change into an undeclared runtime file.
        # Expected: file-boundary guard fails.
        def m10_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(STATS_ENGINE_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src += (
                    "\n\n# MUTATION: an undeclared runtime file gaining a W3.2 DST helper\n"
                    "def _w3_classify_local(naive, tz):\n"
                    "    return False, False\n"
                )
                copy.write_text(src, encoding="utf-8")
            return copy

        def m10_assert(copy_path):
            src = copy_path.read_text(encoding="utf-8")
            hits = scan_text_for_markers(src, ["_w3_classify_local"])
            is_red = bool(hits)
            return is_red, f"W3.2 marker found in stats_engine.py copy: {hits} (file-boundary guard must catch this)"

        run_mutation(base_tmp, "M32-10", "introduce timezone helper change into an undeclared runtime file",
                     "file-boundary guard fails", m10_make, m10_assert)

        # ==================================================================
        # W3.2 CORRECTION mutations (M32C-2/4/5, task spec section 7 -- retained,
        # still valid) + W3.2 CORRECTION ROUND 2 mutations (M32R2-1..6, R2 task spec
        # section 5)
        # ==================================================================
        #
        # M32C-1 and M32C-6 are REMOVED (not merely renamed) -- the R2 independent
        # re-review (W3_2_CORRECTION_REVIEW/07_MUTATION_CAUSALITY_REVIEW.md) found both
        # invalid as substantive proofs: they asserted "the active-path gate must stay
        # green because branch #1/def#2 is dead", when def#2 (and def#3) are actually
        # SHADOWED_BUT_REACHABLE, not dead (see W3_2_CORRECTION_REVIEW/
        # 03_TZ3_REACHABILITY_REVIEW.md). Two independent controls proved the round-1
        # gate could not tell the difference: making the mutated dead def#1 reachable
        # again (via a new active binding OR its own existing capture) left the gate
        # GREEN either way. M32R2-1/2/5 below are the replacement causal proofs; M32R2-4
        # replaces M32C-1's legitimate half (dead-only mutation must not gate, but must
        # be recorded) without its invalid "this proves dead/reachable classification"
        # claim -- M32R2-5 is what actually proves that.

        M2_PATTERN = (r'    from storage import w3_now as _ti_w3_now\n'
                     r'    return _ti_w3_now\(\)\.date\(\)\.isoformat\(\)\n')
        M2_OLD = ('    try:\n'
                 '        from zoneinfo import ZoneInfo\n'
                 '        return datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()\n'
                 '    except Exception:\n'
                 '        return datetime.now().date().isoformat()\n')

        def m32c2_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, M2_PATTERN, M2_OLD)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32c2_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            fn = result["tz3_functions"]["_today_iso"]
            is_red = not fn["active_ok"]
            gens = [(g["lineno"], g["classification"], g["bare_clock_calls"]) for g in fn["generations"]]
            return is_red, f"_today_iso.active_ok={fn['active_ok']} generations={gens} (gate.ok={result['ok']})"

        run_mutation(base_tmp, "M32C-2", "restore the pre-correction TZ-3 fallback #2 in _today_iso",
                     "frozen panel static gate goes red for _today_iso (ACTIVE_REACHABLE, 13 call sites)",
                     m32c2_make, m32c2_assert)

        # ---- M32R2-1/2/3: restore each of the three genuinely REACHABLE TZ-3
        # fallbacks (def#2, def#3 -- both SHADOWED_BUT_REACHABLE per the corrected
        # reachability graph -- and _tp_visual_now_local, ACTIVE_REACHABLE) in a temp
        # copy of the live, R2-untouched panel_bot.py. All three are standard
        # green->red->green: the corrected gate now scans every reachable generation,
        # not just the last-def-wins active one, so a reachable fallback restored
        # anywhere in the chain must turn the gate red.

        DEF2_ANCHOR = ('def _panel_header() -> str:  # type: ignore[override]\n'
                      '    hp = _tp_visual_health_parts()\n'
                      '    return "\\n".join([\n')
        DEF2_FALLBACK = ('def _panel_header() -> str:  # type: ignore[override]\n'
                        '    hp = _tp_visual_health_parts()\n'
                        '    try:\n'
                        '        _stamp = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%y %H:%M")\n'
                        '    except Exception:\n'
                        '        _stamp = datetime.now().strftime("%d.%m.%y %H:%M")\n'
                        '    return "\\n".join([\n')

        def m32r2_1_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                if DEF2_ANCHOR not in src:
                    raise RuntimeError("M32R2-1: _panel_header def#2 anchor not found")
                src = src.replace(DEF2_ANCHOR, DEF2_FALLBACK, 1)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r2_1_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            fn = result["tz3_functions"]["_panel_header"]
            is_red = not fn["active_ok"]
            gens = [(g["lineno"], g["classification"], g["violates"]) for g in fn["generations"]]
            return is_red, f"_panel_header.active_ok={fn['active_ok']} generations={gens}"

        run_mutation(base_tmp, "M32R2-1", "restore a host-local fallback in reachable "
                     "_panel_header def#2 @4657 (SHADOWED_BUT_REACHABLE)",
                     "frozen panel static gate goes red", m32r2_1_make, m32r2_1_assert)

        DEF3_ANCHOR = ('def _panel_header() -> str:  # type: ignore[override]\n'
                      '    base = _TPAG_PANEL_V2_ORIG_HEADER() if callable(_TPAG_PANEL_V2_ORIG_HEADER) '
                      'else "🟢 TPilot Admin Panel"\n')
        DEF3_FALLBACK = ('def _panel_header() -> str:  # type: ignore[override]\n'
                        '    try:\n'
                        '        _probe_stamp = datetime.now(ZoneInfo("Europe/Kyiv")).isoformat()\n'
                        '    except Exception:\n'
                        '        _probe_stamp = datetime.now().isoformat()\n'
                        '    base = _TPAG_PANEL_V2_ORIG_HEADER() if callable(_TPAG_PANEL_V2_ORIG_HEADER) '
                        'else "🟢 TPilot Admin Panel"\n')

        def m32r2_2_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                if DEF3_ANCHOR not in src:
                    raise RuntimeError("M32R2-2: _panel_header def#3 anchor not found")
                src = src.replace(DEF3_ANCHOR, DEF3_FALLBACK, 1)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r2_2_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            fn = result["tz3_functions"]["_panel_header"]
            is_red = not fn["active_ok"]
            gens = [(g["lineno"], g["classification"], g["violates"]) for g in fn["generations"]]
            return is_red, f"_panel_header.active_ok={fn['active_ok']} generations={gens}"

        run_mutation(base_tmp, "M32R2-2", "restore a host-local fallback in reachable "
                     "_panel_header def#3 @5254 (SHADOWED_BUT_REACHABLE)",
                     "frozen panel static gate goes red", m32r2_2_make, m32r2_2_assert)

        M3_PATTERN = (r'    from storage import w3_now as _tvnl_w3_now\n'
                     r'    return _tvnl_w3_now\(\)\.strftime\("%d\.%m\.%y %H:%M"\)\n')
        M3_OLD = ('    try:\n'
                 '        return datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%y %H:%M")\n'
                 '    except Exception:\n'
                 '        try:\n'
                 '            return datetime.now().strftime("%d.%m.%y %H:%M")\n'
                 '        except Exception:\n'
                 '            return "_"\n')

        def m32r2_3_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, M3_PATTERN, M3_OLD)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r2_3_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            fn = result["tz3_functions"]["_tp_visual_now_local"]
            is_red = not fn["active_ok"]
            gens = [(g["lineno"], g["classification"], g["bare_clock_calls"]) for g in fn["generations"]]
            return is_red, f"_tp_visual_now_local.active_ok={fn['active_ok']} generations={gens}"

        run_mutation(base_tmp, "M32R2-3", "restore the fallback in _tp_visual_now_local "
                     "(ACTIVE_REACHABLE)",
                     "frozen panel static gate goes red", m32r2_3_make, m32r2_3_assert)

        # ---- M32R2-4: restore the fallback ONLY in the genuinely dead def#1 @1057.
        # Expected: the runtime-reachable gate stays GREEN (def#1 has zero external
        # callers and zero surviving capture that is ever invoked -- see
        # 03_TZ3_REACHABILITY_REVIEW.md); the dead-code inventory records the changed
        # dead node; and -- unlike round-1's M32C-1 -- this mutation makes NO claim that
        # staying green proves the dead/reachable distinction was analysed. That proof
        # is M32R2-5, run immediately after, on the SAME mutated bytes.
        DEF1_PATTERN = (r'    from storage import w3_now as _ph_w3_now\n'
                        r'    updated_at = _ph_w3_now\(\)\.strftime\("%d\.%m\.%y %H:%M"\)\n')
        DEF1_OLD = ('    try:\n'
                   '        updated_at = datetime.now(ZoneInfo("Europe/Kyiv")).strftime("%d.%m.%y %H:%M")\n'
                   '    except Exception:\n'
                   '        updated_at = datetime.now().strftime("%d.%m.%y %H:%M")\n')

        def m32r2_4_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, DEF1_PATTERN, DEF1_OLD)
                copy.write_text(src, encoding="utf-8")
            return copy

        def _m32r2_4_probe(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            fn = result["tz3_functions"]["_panel_header"]
            gen1 = next(g for g in fn["generations"] if g["lineno"] == 1057)
            dead_key = f"_panel_header@1057"
            dead_hit = bool(result["dead_code_inventory"].get(dead_key, {}).get("bare_clock_calls"))
            return fn["active_ok"], gen1["classification"], dead_hit

        try:
            m32r2_4_work = base_tmp / "M32R2_4"
            m32r2_4_work.mkdir(parents=True, exist_ok=True)
            c4 = m32r2_4_make(m32r2_4_work / "clean", mutate=False)
            c4_ok, c4_cls, c4_dead_hit = _m32r2_4_probe(c4)
            c4_bytes = c4.read_bytes()

            mut4 = m32r2_4_make(m32r2_4_work / "mutant", mutate=True)
            mut4_ok, mut4_cls, mut4_dead_hit = _m32r2_4_probe(mut4)
            bytes_changed_4 = mut4.read_bytes() != c4_bytes

            r4 = m32r2_4_make(m32r2_4_work / "restored", mutate=False)
            r4_ok, r4_cls, r4_dead_hit = _m32r2_4_probe(r4)
            restore_matches_4 = r4.read_bytes() == c4_bytes

            causal_4 = (
                bytes_changed_4
                and c4_cls == "DEAD_UNREACHABLE" and mut4_cls == "DEAD_UNREACHABLE"
                and r4_cls == "DEAD_UNREACHABLE"
                and c4_ok is True and not c4_dead_hit         # before: clean, no dead-code hit
                and mut4_ok is True and mut4_dead_hit          # mutated: runtime GREEN, but recorded
                and r4_ok is True and not r4_dead_hit           # restore: clean again
                and restore_matches_4
            )
            record("M32R2-4", "restore the fallback ONLY in the genuinely dead "
                   "_panel_header def#1 @1057",
                   "runtime-reachable gate stays GREEN; dead-code inventory records the "
                   "changed dead node; classification stays DEAD_UNREACHABLE throughout "
                   "(no claim that this alone proves dead/reachable analysis -- see M32R2-5)",
                   causal_4,
                   f"bytes_changed={bytes_changed_4} "
                   f"before(active_ok={c4_ok},cls={c4_cls},dead_hit={c4_dead_hit}) "
                   f"mutated(active_ok={mut4_ok},cls={mut4_cls},dead_hit={mut4_dead_hit}) "
                   f"restored(active_ok={r4_ok},cls={r4_cls},dead_hit={r4_dead_hit},"
                   f"bytes_match={restore_matches_4})")
        except Exception as exc:
            record("M32R2-4", "restore the fallback ONLY in the genuinely dead "
                   "_panel_header def#1 @1057",
                   "runtime gate GREEN; dead-code inventory detects it", False,
                   f"{type(exc).__name__}: {exc}")

        # ---- M32R2-5: starting from M32R2-4's mutated bytes (dead def#1, fallback
        # restored), create a temporary captured reference from the ACTIVE chain to
        # that mutated def#1 -- reusing its OWN existing capture
        # (_TP_VISUAL_ORIG_PANEL_HEADER @4583, which already captures def#1 but is
        # never called anywhere in the live file) by giving it a real caller from
        # inside an already ACTIVE_REACHABLE node (_today_iso, one of the two genuine
        # external-root entry points). Expected: def#1's classification changes from
        # DEAD_UNREACHABLE to SHADOWED_BUT_REACHABLE, and the SAME fallback --
        # untouched since M32R2-4 -- now turns the gate RED. This is the actual causal
        # proof that the graph distinguishes dead from reachable, which round-1's
        # M32C-1 asserted but never demonstrated (its own controls M32C1-CTRL/CTRL2 in
        # the independent re-review showed the round-1 gate stayed green even after
        # this exact change).
        TODAY_ISO_ANCHOR = ('    from storage import w3_now as _ti_w3_now\n'
                           '    return _ti_w3_now().date().isoformat()\n')
        TODAY_ISO_WITH_CAPTURE_CALL = (
            '    from storage import w3_now as _ti_w3_now\n'
            '    _TP_VISUAL_ORIG_PANEL_HEADER()  # MUTATION M32R2-5: give the existing\n'
            '    # capture a real caller from an already-reachable node\n'
            '    return _ti_w3_now().date().isoformat()\n'
        )

        def m32r2_5_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            src = copy.read_text(encoding="utf-8")
            src = _mutate(src, DEF1_PATTERN, DEF1_OLD)  # same dead-only mutation as M32R2-4
            if mutate:
                if TODAY_ISO_ANCHOR not in src:
                    raise RuntimeError("M32R2-5: _today_iso anchor not found")
                src = src.replace(TODAY_ISO_ANCHOR, TODAY_ISO_WITH_CAPTURE_CALL, 1)
            copy.write_text(src, encoding="utf-8")
            return copy

        def m32r2_5_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            fn = result["tz3_functions"]["_panel_header"]
            gen1 = next(g for g in fn["generations"] if g["lineno"] == 1057)
            is_red = not fn["active_ok"]
            reclassified = gen1["classification"] == "SHADOWED_BUT_REACHABLE"
            return (is_red and reclassified), (
                f"active_ok={fn['active_ok']} def1_classification={gen1['classification']} "
                f"(expect SHADOWED_BUT_REACHABLE once a later active binding routes to it)")

        run_mutation(base_tmp, "M32R2-5", "make the mutated (still-dead-fallback) def#1 "
                     "reachable again via its own existing globals().get capture",
                     "classification -> SHADOWED_BUT_REACHABLE; same fallback -> RED",
                     m32r2_5_make, m32r2_5_assert)

        # ---- M32R2-6: break the globals().get capture matcher (in a TEMP COPY of the
        # gate tool itself, never the live tool) by reverting is_globals_get_call() to
        # the round-1 shape that required call.func.value to be ast.Name instead of the
        # real ast.Call shape. Expected: the tool's own fixture tests (which exist
        # specifically to catch this) go red.
        GATE_TOOL_PATH = TOOLS_DIR / "w3_2_panel_static_gate_selftest.py"
        BROKEN_MATCHER_PATTERN = (
            r'    return \(\n'
            r'        isinstance\(call, ast\.Call\)\n'
            r'        and isinstance\(call\.func, ast\.Attribute\)\n'
            r'        and call\.func\.attr == "get"\n'
            r'        and isinstance\(call\.func\.value, ast\.Call\)\n'
            r'        and isinstance\(call\.func\.value\.func, ast\.Name\)\n'
            r'        and call\.func\.value\.func\.id == "globals"\n'
            r'        and not call\.func\.value\.args\n'
            r'        and not call\.func\.value\.keywords\n'
            r'    \)\n'
        )
        BROKEN_MATCHER_REPLACEMENT = (
            "    # MUTATION M32R2-6: round-1's broken shape (expects call.func.value to be\n"
            "    # an ast.Name -- it is actually an ast.Call, so this NEVER matches)\n"
            "    return (\n"
            "        isinstance(call, ast.Call)\n"
            "        and isinstance(call.func, ast.Attribute)\n"
            "        and call.func.attr == \"get\"\n"
            "        and isinstance(call.func.value, ast.Name)\n"
            "        and call.func.value.id == \"globals\"\n"
            "    )\n"
        )

        def m32r2_6_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(GATE_TOOL_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, BROKEN_MATCHER_PATTERN, BROKEN_MATCHER_REPLACEMENT)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r2_6_assert(copy_path):
            mod = _load_module(copy_path)
            fixtures = mod.run_globals_get_fixture_tests()
            failing = [f["fixture"] for f in fixtures if not f["ok"]]
            is_red = bool(failing)
            return is_red, f"fixture failures with the broken matcher: {failing}"

        run_mutation(base_tmp, "M32R2-6", "break the globals().get capture matcher "
                     "(in a temp copy of the gate tool)",
                     "the tool's own capture-chain completeness fixtures go red",
                     m32r2_6_make, m32r2_6_assert)

        # ==================================================================
        # W3.2 CORRECTION ROUND 3 mutations (M32R3-1..8, R3 task spec section 8)
        # ==================================================================
        #
        # Round 2's gate resolved every UNRESOLVED edge (a dynamic capture key, a
        # broken/absent capture, an alias it did not follow, an AsyncFunctionDef it
        # could not see) to "no edge" -- an independent re-review
        # (W3_2_CORRECTION_R2_REVIEW/05_PANEL_STATIC_GATE_REVIEW.md, probes J1-J3/K/L/M)
        # proved this let a REAL host-local fallback in a reachable generation report
        # GREEN once its one inbound edge became unresolvable. Round 3's gate replaces
        # that default: any edge sourced from a confirmed-reachable node that cannot be
        # resolved fails the WHOLE GATE closed. M32R3-1..8 are the permanent causal
        # proofs for that new core rule, plus AsyncFunctionDef support, alias chain
        # resolution and the completeness cross-checks.

        # ---- M32R3-1: make the capture reaching a reachable generation non-literal.
        # Expected: RED (the capture's OWN reachability edge becomes unresolvable, so
        # the gate can no longer positively account for what's downstream of it).
        M3R1_ANCHOR = '_N53_HDR_PREV_PANEL_HEADER = globals().get("_panel_header")\n'
        M3R1_MUTANT = ('_M32R3_1_DYNKEY = "_panel_header"\n'
                       '_N53_HDR_PREV_PANEL_HEADER = globals().get(_M32R3_1_DYNKEY)\n')

        def m32r3_1_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R1_ANCHOR), M3R1_MUTANT)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r3_1_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            is_red = not result["ok"]
            return is_red, (f"gate.ok={result['ok']} "
                            f"unresolved_reachable_edges={len(result['unresolved_reachable_edges'])}")

        run_mutation(base_tmp, "M32R3-1", "make a reachable generation's capture key non-literal",
                     "UNKNOWN/RED -- fail-closed core rule", m32r3_1_make, m32r3_1_assert)

        # ---- M32R3-2: remove capture detection for globals().get (in a TEMP COPY of
        # the gate tool). Expected: capture completeness cross-check goes red -- the
        # AST-derived capture count collapses while the independent regex sweep does
        # not, closing the exact silent-regression shape M32R2-6 exposed (the fixture
        # tests catch it too, but this asserts the STRUCTURAL completeness signal
        # specifically, per R3 task spec section 10: "do not hard-code one number as
        # the only criterion").
        M3R2_GOOD = (
            '        and isinstance(call.func.value, ast.Call)\n'
            '        and isinstance(call.func.value.func, ast.Name)\n'
            '        and call.func.value.func.id == "globals"\n'
            '        and not call.func.value.args\n'
            '        and not call.func.value.keywords\n'
        )
        M3R2_BROKEN = (
            '        and isinstance(call.func.value, ast.Name)\n'
            '        and call.func.value.id == "globals"\n'
        )

        def m32r3_2_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(GATE_TOOL_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R2_GOOD), M3R2_BROKEN)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r3_2_assert(copy_path):
            mod = _load_module(copy_path)
            result = mod.run_panel_static_gate(PANEL_BOT_PATH)
            c = result["completeness"]
            is_red = not c["ok"]
            return is_red, f"completeness={c} gate.ok={result['ok']}"

        run_mutation(base_tmp, "M32R3-2", "remove capture detection for globals().get "
                     "(in a temp copy of the gate tool)",
                     "capture completeness cross-check goes red", m32r3_2_make, m32r3_2_assert)

        # ---- M32R3-3: route a frozen path through an alias chain to a fallback helper.
        # Expected: RED. Wires a new module-level alias of the def#1 capture into
        # _today_iso's body (an already-reachable external root) -- the alias chain
        # must be followed (section 5) and def#1's fallback must gate once reachable.
        M3R3_ALIAS_ANCHOR = '_TP_VISUAL_ORIG_PANEL_HEADER = globals().get("_panel_header")\n'
        M3R3_ALIAS_MUTANT = (M3R3_ALIAS_ANCHOR +
                             '_M32R3_3_ALIAS_HDR = _TP_VISUAL_ORIG_PANEL_HEADER\n')
        M3R3_CALL_ANCHOR = ('    from storage import w3_now as _ti_w3_now\n'
                            '    return _ti_w3_now().date().isoformat()\n')
        M3R3_CALL_MUTANT = ('    if callable(_M32R3_3_ALIAS_HDR):\n'
                            '        _M32R3_3_ALIAS_HDR()  # M32R3-3 alias edge\n'
                            '    from storage import w3_now as _ti_w3_now\n'
                            '    return _ti_w3_now().date().isoformat()\n')

        def m32r3_3_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                # def#1 must actually carry a fallback for this to be a real defect --
                # the corrected live source no longer does (F-1), so restore it first
                # (same DEF1_PATTERN/DEF1_OLD as M32R2-4/5), THEN wire the alias chain.
                src = _mutate(src, DEF1_PATTERN, DEF1_OLD)
                src = _mutate(src, re.escape(M3R3_ALIAS_ANCHOR), M3R3_ALIAS_MUTANT)
                src = _mutate(src, re.escape(M3R3_CALL_ANCHOR), M3R3_CALL_MUTANT)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r3_3_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            is_red = not result["ok"]
            fn = result["tz3_functions"]["_panel_header"]
            gen1 = next((g for g in fn["generations"] if g["lineno"] == 1057), None)
            return is_red, f"gate.ok={result['ok']} def1_classification={gen1['classification'] if gen1 else None}"

        run_mutation(base_tmp, "M32R3-3", "route a frozen path through an alias chain "
                     "to a fallback-carrying helper",
                     "RED -- alias chain discovered and gated", m32r3_3_make, m32r3_3_assert)

        # ---- M32R3-4: convert a newly reachable helper to AsyncFunctionDef with a
        # fallback. Expected: RED -- proves AsyncFunctionDef is a first-class node.
        M3R4_DEF4_ANCHOR = ('def _panel_header() -> str:  # type: ignore[override]\n'
                            '    # N5.3.1 exact root/status layout (owner-approved):\n')
        M3R4_DEF4_MUTANT = ('def _panel_header() -> str:  # type: ignore[override]\n'
                            '    _m32r3_4_async_helper()  # M32R3-4 async call\n'
                            '    # N5.3.1 exact root/status layout (owner-approved):\n')
        M3R4_APPEND = ('\n\nasync def _m32r3_4_async_helper():\n'
                      '    return str(datetime.now())\n')

        def m32r3_4_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR), M3R4_DEF4_MUTANT)
                src += M3R4_APPEND
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r3_4_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            is_red = not result["ok"]
            key = [k for k in result["helper_nodes_reachable"] if "_m32r3_4_async_helper" in k]
            return is_red, f"gate.ok={result['ok']} helper_rows={key}"

        run_mutation(base_tmp, "M32R3-4", "convert a newly reachable helper to "
                     "AsyncFunctionDef carrying a fallback",
                     "RED -- async helper discovered and gated", m32r3_4_make, m32r3_4_assert)

        # ---- M32R3-5: introduce an unresolved dynamic callback/call target on a
        # frozen path. Expected: UNKNOWN/RED.
        M3R5_APPEND = ('\n\n_M32R3_5_DYNKEY = "_tp_visual_now_local"\n'
                      '_M32R3_5_DYN = globals().get(_M32R3_5_DYNKEY)\n')
        M3R5_CALL_MUTANT = ('def _panel_header() -> str:  # type: ignore[override]\n'
                           '    _M32R3_5_DYN()  # M32R3-5 dynamic capture call\n'
                           '    # N5.3.1 exact root/status layout (owner-approved):\n')

        def m32r3_5_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR), M3R5_CALL_MUTANT)
                src += M3R5_APPEND
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r3_5_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            is_red = not result["ok"]
            return is_red, (f"gate.ok={result['ok']} "
                            f"unresolved_reachable_edges={len(result['unresolved_reachable_edges'])}")

        run_mutation(base_tmp, "M32R3-5", "introduce an unresolved dynamic call target "
                     "on a frozen TZ-3 path",
                     "UNKNOWN/RED", m32r3_5_make, m32r3_5_assert)

        # ---- M32R3-6: introduce an alias cycle reachable from a frozen path.
        # Expected: AMBIGUOUS/RED.
        M3R6_APPEND = ('\n\n_m32r3_6_c2 = _m32r3_6_c1\n'
                      '_m32r3_6_c1 = _m32r3_6_c2\n\n'
                      'def _m32r3_6_root():\n'
                      '    _m32r3_6_c1()\n')
        M3R6_CALL_MUTANT = ('def _panel_header() -> str:  # type: ignore[override]\n'
                           '    _m32r3_6_root()  # M32R3-6 alias cycle call\n'
                           '    # N5.3.1 exact root/status layout (owner-approved):\n')

        def m32r3_6_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR), M3R6_CALL_MUTANT)
                src += M3R6_APPEND
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r3_6_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            is_red = not result["ok"]
            cyc = [e for e in result["unresolved_reachable_edges"] if e["diagnostic"] == "ALIAS_CYCLE"]
            return is_red, f"gate.ok={result['ok']} alias_cycle_edges={len(cyc)}"

        run_mutation(base_tmp, "M32R3-6", "introduce an alias cycle reachable from a "
                     "frozen TZ-3 path",
                     "AMBIGUOUS/RED", m32r3_6_make, m32r3_6_assert)

        # ---- M32R3-7: a dead fallback helper stays GREEN; the SAME mutated bytes,
        # made reachable through a real edge, flip RED. Mirrors M32R2-4+5's structure
        # (necessary-but-not-sufficient control, then the actual causal proof) for the
        # general (non-TZ3-name) helper case.
        M3R7_APPEND = '\n\ndef _m32r3_7_dead_helper():\n    return str(datetime.now())\n'

        def _m32r3_7_probe(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            key = [k for k in result["helper_nodes_reachable"] if "_m32r3_7_dead_helper" in k]
            return result["ok"], bool(key)

        try:
            m3r7_work = base_tmp / "M32R3_7"
            m3r7_work.mkdir(parents=True, exist_ok=True)

            (m3r7_work / "clean").mkdir(parents=True, exist_ok=True)
            c7 = _fresh_copy(PANEL_BOT_PATH, m3r7_work / "clean")
            c7_ok, c7_reached = _m32r3_7_probe(c7)
            c7_bytes = c7.read_bytes()

            (m3r7_work / "dead").mkdir(parents=True, exist_ok=True)
            dead7 = _fresh_copy(PANEL_BOT_PATH, m3r7_work / "dead")
            dead7.write_text(dead7.read_text(encoding="utf-8") + M3R7_APPEND, encoding="utf-8")
            dead7_ok, dead7_reached = _m32r3_7_probe(dead7)
            bytes_changed_7 = dead7.read_bytes() != c7_bytes

            (m3r7_work / "reachable").mkdir(parents=True, exist_ok=True)
            reach7 = _fresh_copy(PANEL_BOT_PATH, m3r7_work / "reachable")
            src7 = reach7.read_text(encoding="utf-8")
            src7 = _mutate(src7, re.escape(M3R4_DEF4_ANCHOR),
                           'def _panel_header() -> str:  # type: ignore[override]\n'
                           '    _m32r3_7_dead_helper()  # M32R3-7 now reachable\n'
                           '    # N5.3.1 exact root/status layout (owner-approved):\n')
            src7 += M3R7_APPEND
            reach7.write_text(src7, encoding="utf-8")
            reach7_ok, reach7_reached = _m32r3_7_probe(reach7)

            (m3r7_work / "restored").mkdir(parents=True, exist_ok=True)
            restored7 = _fresh_copy(PANEL_BOT_PATH, m3r7_work / "restored")
            restore_matches_7 = restored7.read_bytes() == c7_bytes
            restored7_ok, _ = _m32r3_7_probe(restored7)

            causal_7 = (bytes_changed_7 and c7_ok is True and not c7_reached
                       and dead7_ok is True and not dead7_reached
                       and reach7_ok is False and reach7_reached
                       and restored7_ok is True and restore_matches_7)
            record("M32R3-7", "dead fallback helper stays GREEN; the SAME bytes made "
                   "reachable through a real edge flip RED",
                   "GREEN (dead, unreported) -> RED (reachable, same bytes) -> GREEN (restored)",
                   causal_7,
                   f"bytes_changed={bytes_changed_7} clean(ok={c7_ok}) "
                   f"dead(ok={dead7_ok},reached={dead7_reached}) "
                   f"reachable(ok={reach7_ok},reached={reach7_reached}) "
                   f"restored(ok={restored7_ok},bytes_match={restore_matches_7})")
        except Exception as exc:
            record("M32R3-7", "dead fallback helper stays GREEN; made reachable flips RED",
                   "GREEN -> RED -> GREEN", False, f"{type(exc).__name__}: {exc}")

        # ---- M32R3-8: omit one reachable graph node (break AsyncFunctionDef
        # collection in a TEMP COPY of the gate tool). Expected: graph completeness
        # RED -- the independent def-count cross-check (regex vs AST) catches a
        # collector that silently drops a definition kind, closing the exact shape of
        # the round-2 review's AsyncFunctionDef gap (R2N-3) as a standing regression
        # test.
        M3R8_GOOD = ('    out: dict[str, list] = {}\n'
                    '    for n in tree.body:\n'
                    '        if isinstance(n, DEF_TYPES):\n'
                    '            out.setdefault(n.name, []).append(n)\n'
                    '    return out\n')
        M3R8_BROKEN = ('    out: dict[str, list] = {}\n'
                      '    for n in tree.body:\n'
                      '        if isinstance(n, ast.FunctionDef):  # MUTATION M32R3-8: '
                      'AsyncFunctionDef dropped\n'
                      '            out.setdefault(n.name, []).append(n)\n'
                      '    return out\n')

        def m32r3_8_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(GATE_TOOL_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R8_GOOD), M3R8_BROKEN)
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r3_8_assert(copy_path):
            mod = _load_module(copy_path)
            result = mod.run_panel_static_gate(PANEL_BOT_PATH)
            c = result["completeness"]
            is_red = not c["ok"] and not c["def_count_ok"]
            return is_red, f"completeness={c} gate.ok={result['ok']}"

        run_mutation(base_tmp, "M32R3-8", "omit AsyncFunctionDef from node collection "
                     "(in a temp copy of the gate tool)",
                     "graph/def-count completeness cross-check goes red",
                     m32r3_8_make, m32r3_8_assert)

        # ==================================================================
        # Scope-guard fail-closed probes (SG-R2-1..6, R2 task spec section 6)
        # ==================================================================

        def _sg_build_tree(tmpdir):
            tmpdir.mkdir(parents=True, exist_ok=True)
            for fname in WATCHED_RUNTIME_FILES:
                shutil.copyfile(BASE_DIR / fname, tmpdir / fname)
            return tmpdir

        # ---- SG-R2-1: remove one required file's entry from the baseline map.
        def sgr2_1_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            if mutate:
                baseline["baseline_sha256"].pop("manager_bot.py", None)
            bpath = tmpdir.parent / f"{tmpdir.name}_baseline.json"
            bpath.write_text(json.dumps(baseline), encoding="utf-8")
            return (tmpdir, bpath)

        def sgr2_1_assert(payload):
            tmpdir, bpath = payload
            result = run_scope_guard(base_dir=tmpdir, baseline_path=bpath)
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, f"missing_from_baseline={result['hash_diff_boundary']['missing_from_baseline']}"

        run_mutation(base_tmp, "SG-R2-1", "remove manager_bot.py's entry from the baseline map",
                     "hash/diff boundary goes red (fail-closed)", sgr2_1_make, sgr2_1_assert)

        # ---- SG-R2-2: add an untracked runtime file to the tree.
        def sgr2_2_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                (tmpdir / "sneaky_new_runtime_helper.py").write_text(
                    "# not a declared runtime file\n", encoding="utf-8")
            return tmpdir

        def sgr2_2_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH,
                                     enforce_no_extra_files=True)
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, f"extra_files={result['hash_diff_boundary']['extra_files_in_base_dir']}"

        run_mutation(base_tmp, "SG-R2-2", "add an untracked .py file to the runtime tree "
                     "(enforce_no_extra_files=True)",
                     "hash/diff boundary goes red", sgr2_2_make, sgr2_2_assert)

        # ---- SG-R2-3: remove a runtime file from disk.
        def sgr2_3_make(tmpdir, mutate):
            # each call gets a brand-new tmpdir (run_mutation passes work/"neg",
            # work/"mut", work/"restore" separately), so _sg_build_tree always starts
            # from a complete, freshly-copied 12-file tree before any removal.
            _sg_build_tree(tmpdir)
            if mutate:
                (tmpdir / "storage.py").unlink()
            return tmpdir

        def sgr2_3_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            is_red = not result["hash_diff_boundary_ok"]
            row = [r for r in result["hash_diff_boundary"]["rows"] if r["file"] == "storage.py"]
            return is_red, f"storage.py row={row}"

        run_mutation(base_tmp, "SG-R2-3", "delete storage.py from the runtime tree",
                     "hash/diff boundary goes red", sgr2_3_make, sgr2_3_assert)

        # ---- SG-R2-4: case-alias a required file's path -- must not bypass.
        def sgr2_4_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                src = (tmpdir / "manager_bot.py").read_text(encoding="utf-8")
                (tmpdir / "manager_bot.py").unlink()
                (tmpdir / "Manager_Bot.py").write_text(src, encoding="utf-8")
            return tmpdir

        def sgr2_4_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            is_red = not result["hash_diff_boundary_ok"]
            row = [r for r in result["hash_diff_boundary"]["rows"] if r["file"] == "manager_bot.py"]
            return is_red, f"manager_bot.py row={row}"

        run_mutation(base_tmp, "SG-R2-4", "rename manager_bot.py to Manager_Bot.py "
                     "(identical bytes, case only)",
                     "no bypass -- hash/diff boundary stays/goes red",
                     sgr2_4_make, sgr2_4_assert)

        # ---- SG-R2-5: modify an undeclared file with no W3 marker (re-confirms F-5's
        # original closure survives the R2 rewrite).
        def sgr2_5_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                p = tmpdir / "partner_stat_bot.py"
                p.write_text(p.read_text(encoding="utf-8") + "\n# SG-R2-5 probe, no marker\n",
                             encoding="utf-8")
            return tmpdir

        def sgr2_5_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, f"marker_scan_ok={result['marker_scan_ok']}"

        run_mutation(base_tmp, "SG-R2-5", "modify partner_stat_bot.py (undeclared) with no marker",
                     "hash/diff boundary goes red", sgr2_5_make, sgr2_5_assert)

        # ---- SG-R2-6: negative control -- only the allowed file changes -> GREEN.
        def sgr2_6_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                p = tmpdir / "panel_bot.py"
                p.write_text(p.read_text(encoding="utf-8") + "\n# SG-R2-6 approved-boundary edit\n",
                             encoding="utf-8")
            return tmpdir

        def sgr2_6_assert(tmpdir):
            # NOTE: inverted vs the other SG-R2 probes -- "is_red" here means "the
            # allowlisted change was rejected", which must NEVER happen. run_mutation's
            # green->red->green framework expects the MUTATED state to be the RED one,
            # so this probe is asserted directly rather than through run_mutation.
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            return result["ok"]

        try:
            sgr2_6_work = base_tmp / "SG_R2_6"
            t6 = sgr2_6_make(sgr2_6_work / "mutant", mutate=True)
            green_after_allowed_change = sgr2_6_assert(t6)
            record("SG-R2-6", "modify ONLY panel_bot.py (the allowlisted file)",
                   "scope guard stays GREEN", green_after_allowed_change,
                   f"ok={green_after_allowed_change}")
        except Exception as exc:
            record("SG-R2-6", "modify ONLY panel_bot.py (the allowlisted file)",
                   "scope guard stays GREEN", False, f"{type(exc).__name__}: {exc}")

        # ==================================================================
        # W3.2 CORRECTION ROUND 3 scope-guard hardening mutations (SG-R3-1..8,
        # R3 task spec section 9)
        # ==================================================================
        #
        # Closes three residual gaps the R2 re-review found in the round-2 guard
        # (W3_2_CORRECTION_R2_REVIEW/06_SCOPE_GUARD_FAIL_CLOSED_REVIEW.md): a shrunk
        # REQUIRED_RUNTIME_FILES had nothing to compare itself against (probe 2); a
        # new runtime .py file went undetected under the tool's own default
        # invocation (probe 3b); an extra baseline key was computed but never gated.

        # ---- SG-R3-1: shrink REQUIRED_RUNTIME_FILES itself (monkeypatch, simulating
        # an in-module edit) and modify the dropped file. Expected: RED via the
        # external canonical-list cross-check, regardless of what the in-module tuple
        # says.
        def sgr3_1_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                (tmpdir / "manager_bot.py").write_bytes(
                    (tmpdir / "manager_bot.py").read_bytes() + b"\n# SG-R3-1 probe\n")
            # `mutate` must travel WITH the tree -- the inventory shrink below is the
            # thing under test, and it must apply on the mutated leg only, or the
            # negative control would already be red (a broken-test false FAIL).
            return (tmpdir, mutate)

        def sgr3_1_assert(payload):
            tmpdir, mutate = payload
            if not mutate:
                result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
                is_red = not result["hash_diff_boundary_ok"]
                return is_red, "unmutated leg, REQUIRED_RUNTIME_FILES untouched"
            saved = _sg_mod.REQUIRED_RUNTIME_FILES
            try:
                _sg_mod.REQUIRED_RUNTIME_FILES = tuple(
                    f for f in saved if f != "manager_bot.py")
                result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            finally:
                _sg_mod.REQUIRED_RUNTIME_FILES = saved
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, (f"inventory_mismatch="
                            f"{result['hash_diff_boundary']['inventory_mismatch_vs_canonical']}")

        run_mutation(base_tmp, "SG-R3-1", "omit an expected runtime file from the "
                     "in-module inventory (REQUIRED_RUNTIME_FILES shrunk)",
                     "RED via the external canonical-list cross-check", sgr3_1_make, sgr3_1_assert)

        # ---- SG-R3-2: add an untracked runtime .py file, DEFAULT arguments only (no
        # enforce_no_extra_files). Expected: RED -- the unconditional
        # UNDECLARED_NEW_PY_FILE check, not the opt-in tier.
        def sgr3_2_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                (tmpdir / "sneaky_sg_r3_2.py").write_text("# untracked\n", encoding="utf-8")
            return tmpdir

        def sgr3_2_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, f"undeclared_new={result['hash_diff_boundary']['undeclared_new_py_files']}"

        run_mutation(base_tmp, "SG-R3-2", "add an untracked runtime .py file "
                     "(default arguments, no opt-in flag)",
                     "RED -- unconditional new-file detection", sgr3_2_make, sgr3_2_assert)

        # ---- SG-R3-3: invoke the gate with default arguments only, against the LIVE
        # root. Expected: GREEN, and every hardening field present and active (not a
        # permissive no-op under defaults).
        try:
            r_default = run_scope_guard()
            required_fields = ("undeclared_new_py_files", "inventory_mismatch_vs_canonical",
                               "unclassified_extra_baseline_keys", "case_duplicate_baseline_keys")
            fields_present = all(f in r_default["hash_diff_boundary"] for f in required_fields)
            sg3_ok = r_default["ok"] and fields_present
            record("SG-R3-3", "invoke the gate with default arguments only (live root)",
                   "GREEN, all critical checks structurally active", sg3_ok,
                   f"ok={r_default['ok']} fields_present={fields_present}")
        except Exception as exc:
            record("SG-R3-3", "invoke the gate with default arguments only (live root)",
                   "GREEN, all critical checks structurally active", False,
                   f"{type(exc).__name__}: {exc}")

        # ---- SG-R3-4: partial/corrupt baseline JSON (missing a required top-level
        # key). Expected: RED.
        def sgr3_4_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            if mutate:
                baseline.pop("allowed_correction_changes", None)
            bpath = tmpdir.parent / f"{tmpdir.name}_baseline.json"
            bpath.write_text(json.dumps(baseline), encoding="utf-8")
            return (tmpdir, bpath)

        def sgr3_4_assert(payload):
            tmpdir, bpath = payload
            result = run_scope_guard(base_dir=tmpdir, baseline_path=bpath)
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, f"rows={[r['status'] for r in result['hash_diff_boundary']['rows']]}"

        run_mutation(base_tmp, "SG-R3-4", "partial baseline JSON (missing a required "
                     "top-level key)", "RED", sgr3_4_make, sgr3_4_assert)

        # ---- SG-R3-5: partial/corrupt top-level inventory JSON. Expected: RED.
        def sgr3_5_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            inv_path = tmpdir.parent / f"{tmpdir.name}_inventory.json"
            if mutate:
                inv_path.write_text("{not valid json", encoding="utf-8")
            else:
                inv_path.write_text(TOP_LEVEL_INVENTORY_PATH.read_text(encoding="utf-8"),
                                    encoding="utf-8")
            return (tmpdir, inv_path)

        def sgr3_5_assert(payload):
            tmpdir, inv_path = payload
            saved = _sg_mod.TOP_LEVEL_INVENTORY_PATH
            try:
                _sg_mod.TOP_LEVEL_INVENTORY_PATH = inv_path
                result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            finally:
                _sg_mod.TOP_LEVEL_INVENTORY_PATH = saved
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, f"rows={[r['status'] for r in result['hash_diff_boundary']['rows']]}"

        run_mutation(base_tmp, "SG-R3-5", "partial/corrupt top-level inventory JSON",
                     "RED", sgr3_5_make, sgr3_5_assert)

        # ---- SG-R3-6: extra baseline key, not explicitly classified. Expected: RED.
        def sgr3_6_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
            if mutate:
                baseline["baseline_sha256"]["totally_unrelated_key.py"] = "deadbeef" * 8
            bpath = tmpdir.parent / f"{tmpdir.name}_baseline.json"
            bpath.write_text(json.dumps(baseline), encoding="utf-8")
            return (tmpdir, bpath)

        def sgr3_6_assert(payload):
            tmpdir, bpath = payload
            result = run_scope_guard(base_dir=tmpdir, baseline_path=bpath)
            is_red = not result["hash_diff_boundary_ok"]
            return is_red, (f"unclassified_extra="
                            f"{result['hash_diff_boundary']['unclassified_extra_baseline_keys']}")

        run_mutation(base_tmp, "SG-R3-6", "extra baseline key, not explicitly classified",
                     "RED", sgr3_6_make, sgr3_6_assert)

        # ---- SG-R3-7: case-normalized duplicate path (STORAGE.py vs storage.py).
        # Expected: RED -- no bypass either way.
        def sgr3_7_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                data = (tmpdir / "storage.py").read_bytes()
                (tmpdir / "storage.py").unlink()
                (tmpdir / "STORAGE.py").write_bytes(data)
            return tmpdir

        def sgr3_7_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            is_red = not result["hash_diff_boundary_ok"]
            row = [r for r in result["hash_diff_boundary"]["rows"] if r["file"] == "storage.py"]
            return is_red, f"storage.py row={row}"

        run_mutation(base_tmp, "SG-R3-7", "case-normalized duplicate path "
                     "(STORAGE.py in place of storage.py)",
                     "RED -- no bypass", sgr3_7_make, sgr3_7_assert)

        # ---- SG-R3-8: allowed panel_bot.py delta only. Expected: GREEN (positive
        # control -- asserted directly, matching SG-R2-6's pattern).
        def sgr3_8_make(tmpdir, mutate):
            _sg_build_tree(tmpdir)
            if mutate:
                p = tmpdir / "panel_bot.py"
                p.write_text(p.read_text(encoding="utf-8") + "\n# SG-R3-8 approved edit\n",
                             encoding="utf-8")
            return tmpdir

        def sgr3_8_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            return result["ok"]

        try:
            sgr3_8_work = base_tmp / "SG_R3_8"
            t8 = sgr3_8_make(sgr3_8_work / "mutant", mutate=True)
            green_after_allowed_change = sgr3_8_assert(t8)
            record("SG-R3-8", "modify ONLY panel_bot.py (the allowlisted file)",
                   "scope guard stays GREEN", green_after_allowed_change,
                   f"ok={green_after_allowed_change}")
        except Exception as exc:
            record("SG-R3-8", "modify ONLY panel_bot.py (the allowlisted file)",
                   "scope guard stays GREEN", False, f"{type(exc).__name__}: {exc}")

        # ---- M32C-4: change an UNDECLARED runtime file (manager_bot.py) WITHOUT
        # adding any W3 marker. Expected: hash/diff scope guard (the PRIMARY check)
        # goes red; the marker scan (SECONDARY) stays green, proving the hash boundary
        # -- not the marker grep -- is what caught it (closes F-5).
        def m32c4_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            for fname in WATCHED_RUNTIME_FILES:
                shutil.copyfile(BASE_DIR / fname, tmpdir / fname)
            if mutate:
                p = tmpdir / "manager_bot.py"
                src = p.read_text(encoding="utf-8")
                # a byte change that introduces no W3/W3.2 marker string whatsoever
                src = src.replace("import asyncio", "import asyncio  # M32C-4 probe", 1)
                p.write_text(src, encoding="utf-8")
            return tmpdir

        def m32c4_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            is_red = not result["hash_diff_boundary_ok"]
            marker_still_green = result["marker_scan_ok"]
            detail = (f"hash_diff_boundary_ok={result['hash_diff_boundary_ok']} "
                      f"marker_scan_ok={marker_still_green} "
                      f"(expect hash=red, marker=green -- proves the HASH check, not "
                      f"the marker grep, caught an undeclared marker-free change)")
            # is_red must be true AND the marker scan must NOT have been what caught it
            return (is_red and marker_still_green), detail

        run_mutation(base_tmp, "M32C-4", "change an undeclared runtime file without adding a W3 marker",
                     "hash/diff scope guard goes red (marker scan stays green -- F-5 closed)",
                     m32c4_make, m32c4_assert)

        # ---- M32C-5: add a W3 marker string to an UNDECLARED runtime file. Expected:
        # the marker guard (SECONDARY, retained per spec section 4) also independently
        # fires red.
        def m32c5_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            for fname in WATCHED_RUNTIME_FILES:
                shutil.copyfile(BASE_DIR / fname, tmpdir / fname)
            if mutate:
                p = tmpdir / "manager_bot.py"
                src = p.read_text(encoding="utf-8")
                src += "\n# MUTATION M32C-5: dst_gap_snapped marker injected (undeclared file)\n"
                p.write_text(src, encoding="utf-8")
            return tmpdir

        def m32c5_assert(tmpdir):
            result = run_scope_guard(base_dir=tmpdir, baseline_path=BASELINE_PATH)
            is_red = not result["marker_scan_ok"]
            return is_red, (f"marker_scan_ok={result['marker_scan_ok']} "
                            f"(hash_diff_boundary_ok={result['hash_diff_boundary_ok']}, also "
                            f"red -- any byte change moves the hash too, but this mutation "
                            f"specifically proves the MARKER check independently fires)")

        run_mutation(base_tmp, "M32C-5", "add a W3 marker to an undeclared runtime file",
                     "marker guard goes red", m32c5_make, m32c5_assert)

        # ==================================================================
        # W3.2 CORRECTION ROUND 4 mutations (M32R4-1..10, R4 task spec section 8)
        # ==================================================================
        #
        # Round 3's gate resolved every UNRESOLVED module-level capture/alias edge
        # closed, but an independent final review (W3_2_CORRECTION_R3_REVIEW/
        # 05_PANEL_GATE_REVIEW.md, finding "F-2b") proved the SAME optimistic default
        # survived one layer further out: a function-LOCAL alias, a parameter
        # callback, a dict-dispatch subscript call, an attribute call and a
        # getattr()/call-result-invoked call all produced NO EDGE AND NO DIAGNOSTIC
        # when the callee could not be resolved via the round-3 model. Round 4's gate
        # classifies EVERY Call AST node in a reachable node into one of nine
        # categories (RESOLVED_CALL/RESOLVED_BUILTIN/RESOLVED_IMPORT are safe; the
        # other six always fail the gate). M32R4-1..10 are the permanent causal proofs
        # for that closure.

        BAD_HELPER_APPEND = '\n\ndef _m32r4_unsafe():\n    return datetime.now().isoformat()\n'
        SAFE_HELPER_APPEND = '\n\ndef _m32r4_safe():\n    return 1\n'

        def _m32r4_gate(copy_path):
            return run_panel_static_gate(panel_bot_path=copy_path)

        # ---- M32R4-1: direct call to an unsafe helper from ACTIVE def#4.
        # Expected: RED (the control -- proves the helper itself is a real defect).
        def m32r4_1_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1", "    _m32r4_unsafe()\n    # N5.3.1"))
                src += BAD_HELPER_APPEND
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_1_assert(copy_path):
            result = _m32r4_gate(copy_path)
            is_red = not result["ok"]
            hk = [k for k in result["helper_nodes_reachable"] if "_m32r4_unsafe" in k]
            return is_red, f"gate.ok={result['ok']} helper_reached={hk}"

        run_mutation(base_tmp, "M32R4-1", "direct call to an unsafe helper from ACTIVE def#4",
                     "RED (control)", m32r4_1_make, m32r4_1_assert)

        # ---- M32R4-2: the SAME call converted into a function-LOCAL alias call.
        # Expected: still RED (F-2b's core probe).
        def m32r4_2_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1",
                                  "    _m32r4_x2 = _m32r4_unsafe\n    _m32r4_x2()\n    # N5.3.1"))
                src += BAD_HELPER_APPEND
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_2_assert(copy_path):
            result = _m32r4_gate(copy_path)
            is_red = not result["ok"]
            hk = [k for k in result["helper_nodes_reachable"] if "_m32r4_unsafe" in k]
            return is_red, f"gate.ok={result['ok']} helper_reached={hk}"

        run_mutation(base_tmp, "M32R4-2", "convert the direct call into a function-local alias call",
                     "still RED", m32r4_2_make, m32r4_2_assert)

        # ---- M32R4-3: the alias converted into a two-hop chain.
        # Expected: RED.
        def m32r4_3_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1",
                                  "    _m32r4_a3 = _m32r4_unsafe\n    _m32r4_b3 = _m32r4_a3\n"
                                  "    _m32r4_b3()\n    # N5.3.1"))
                src += BAD_HELPER_APPEND
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_3_assert(copy_path):
            result = _m32r4_gate(copy_path)
            return not result["ok"], f"gate.ok={result['ok']}"

        run_mutation(base_tmp, "M32R4-3", "convert the alias into a two-hop alias chain",
                     "RED", m32r4_3_make, m32r4_3_assert)

        # ---- M32R4-4: the alias replaced with a function-parameter callback whose
        # binding cannot be statically proven (argument is a call result, not a Name).
        # Expected: UNKNOWN/RED.
        def m32r4_4_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1",
                                  "    _m32r4_reg4(_m32r4_compute4())\n    # N5.3.1"))
                src += BAD_HELPER_APPEND
                src += ('\n\ndef _m32r4_compute4():\n    return _m32r4_unsafe\n'
                        '\n\ndef _m32r4_reg4(cb):\n    cb()\n')
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_4_assert(copy_path):
            result = _m32r4_gate(copy_path)
            ue = [e for e in result["unresolved_reachable_edges"]
                  if e.get("call_category") == "UNRESOLVED_PARAMETER_CALLBACK"]
            is_red = (not result["ok"]) and bool(ue)
            return is_red, f"gate.ok={result['ok']} unresolved_param_callback_edges={len(ue)}"

        run_mutation(base_tmp, "M32R4-4", "replace the alias with an unprovable function-"
                     "parameter callback", "UNKNOWN/RED", m32r4_4_make, m32r4_4_assert)

        # ---- M32R4-5: the call replaced with a literal-key Subscript mapping to the
        # unsafe helper. Expected: RED.
        def m32r4_5_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1", '    _M32R4_TBL5["k"]()\n    # N5.3.1'))
                src += BAD_HELPER_APPEND
                src += '\n\n_M32R4_TBL5 = {"k": _m32r4_unsafe}\n'
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_5_assert(copy_path):
            result = _m32r4_gate(copy_path)
            return not result["ok"], f"gate.ok={result['ok']}"

        run_mutation(base_tmp, "M32R4-5", "replace the call with a literal-key Subscript "
                     "mapping to the unsafe helper", "RED", m32r4_5_make, m32r4_5_assert)

        # ---- M32R4-6: the literal key replaced with a dynamic (variable) key.
        # Expected: UNRESOLVED/RED.
        def m32r4_6_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1",
                                  '    _m32r4_k6 = "k"\n    _M32R4_TBL6[_m32r4_k6]()\n'
                                  '    # N5.3.1'))
                src += BAD_HELPER_APPEND
                src += '\n\n_M32R4_TBL6 = {"k": _m32r4_unsafe}\n'
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_6_assert(copy_path):
            result = _m32r4_gate(copy_path)
            ue = [e for e in result["unresolved_reachable_edges"]
                  if e.get("call_category") == "UNRESOLVED_SUBSCRIPT_CALL"]
            is_red = (not result["ok"]) and bool(ue)
            return is_red, f"gate.ok={result['ok']} unresolved_subscript_edges={len(ue)}"

        run_mutation(base_tmp, "M32R4-6", "replace the literal subscript key with a "
                     "dynamic (variable) key", "UNRESOLVED/RED", m32r4_6_make, m32r4_6_assert)

        # ---- M32R4-7: the call replaced with an unresolved Attribute call whose method
        # name collides with a real module-level def name. Expected: RED.
        def m32r4_7_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1",
                                  "    _m32r4_obj7 = _m32r4_make_obj7()\n"
                                  "    _m32r4_obj7._m32r4_unsafe()\n    # N5.3.1"))
                src += BAD_HELPER_APPEND
                src += '\n\ndef _m32r4_make_obj7():\n    return object()\n'
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_7_assert(copy_path):
            result = _m32r4_gate(copy_path)
            ue = [e for e in result["unresolved_reachable_edges"]
                  if e.get("call_category") == "UNRESOLVED_ATTRIBUTE_CALL"]
            is_red = (not result["ok"]) and bool(ue)
            return is_red, f"gate.ok={result['ok']} unresolved_attribute_edges={len(ue)}"

        run_mutation(base_tmp, "M32R4-7", "replace the call with an unresolved Attribute "
                     "call whose method name collides with a real def name",
                     "RED", m32r4_7_make, m32r4_7_assert)

        # ---- M32R4-8: getattr(...)()/dynamic reflection. Expected: RED.
        def m32r4_8_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1",
                                  '    getattr(_m32r4_make_obj8(), "x")()\n    # N5.3.1'))
                src += '\n\ndef _m32r4_make_obj8():\n    return object()\n'
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_8_assert(copy_path):
            result = _m32r4_gate(copy_path)
            ue = [e for e in result["unresolved_reachable_edges"]
                  if e.get("call_category") == "UNRESOLVED_DYNAMIC_CALL"]
            is_red = (not result["ok"]) and bool(ue)
            return is_red, f"gate.ok={result['ok']} unresolved_dynamic_edges={len(ue)}"

        run_mutation(base_tmp, "M32R4-8", "getattr(obj, key)() / call-result-invoked "
                     "dynamic reflection", "RED", m32r4_8_make, m32r4_8_assert)

        # ---- M32R4-9: a dead local alias to the unsafe helper stays GREEN; the SAME
        # bytes, made reachable through a real edge, flip RED. Mirrors M32R2-4+5/
        # M32R3-7's necessary-but-not-sufficient control, then the actual causal proof.
        DEAD_ALIAS_APPEND = ('\n\ndef _m32r4_dead_container9():\n'
                            '    _m32r4_x9 = _m32r4_unsafe\n    _m32r4_x9()\n')

        def _m32r4_9_probe(copy_path):
            result = _m32r4_gate(copy_path)
            key = [k for k in result["helper_nodes_reachable"] if "_m32r4_unsafe" in k]
            return result["ok"], bool(key)

        try:
            m4r9_work = base_tmp / "M32R4_9"
            m4r9_work.mkdir(parents=True, exist_ok=True)

            (m4r9_work / "clean").mkdir(parents=True, exist_ok=True)
            c9 = _fresh_copy(PANEL_BOT_PATH, m4r9_work / "clean")
            c9_ok, c9_reached = _m32r4_9_probe(c9)
            c9_bytes = c9.read_bytes()

            (m4r9_work / "dead").mkdir(parents=True, exist_ok=True)
            dead9 = _fresh_copy(PANEL_BOT_PATH, m4r9_work / "dead")
            src9 = dead9.read_text(encoding="utf-8") + BAD_HELPER_APPEND + DEAD_ALIAS_APPEND
            dead9.write_text(src9, encoding="utf-8")
            dead9_ok, dead9_reached = _m32r4_9_probe(dead9)
            bytes_changed_9 = dead9.read_bytes() != c9_bytes

            (m4r9_work / "reachable").mkdir(parents=True, exist_ok=True)
            reach9 = _fresh_copy(PANEL_BOT_PATH, m4r9_work / "reachable")
            src9r = reach9.read_text(encoding="utf-8")
            src9r = _mutate(src9r, re.escape(M3R4_DEF4_ANCHOR),
                            M3R4_DEF4_ANCHOR.replace(
                                "    # N5.3.1",
                                "    _m32r4_dead_container9()\n    # N5.3.1"))
            src9r += BAD_HELPER_APPEND + DEAD_ALIAS_APPEND
            reach9.write_text(src9r, encoding="utf-8")
            reach9_ok, reach9_reached = _m32r4_9_probe(reach9)

            (m4r9_work / "restored").mkdir(parents=True, exist_ok=True)
            restored9 = _fresh_copy(PANEL_BOT_PATH, m4r9_work / "restored")
            restore_matches_9 = restored9.read_bytes() == c9_bytes
            restored9_ok, _ = _m32r4_9_probe(restored9)

            causal_9 = (bytes_changed_9 and c9_ok is True and not c9_reached
                       and dead9_ok is True and not dead9_reached
                       and reach9_ok is False and reach9_reached
                       and restored9_ok is True and restore_matches_9)
            record("M32R4-9", "dead local alias to unsafe helper stays GREEN; the SAME "
                   "bytes made reachable through a real edge flip RED",
                   "GREEN (dead, unreported) -> RED (reachable, same bytes) -> GREEN (restored)",
                   causal_9,
                   f"bytes_changed={bytes_changed_9} clean(ok={c9_ok}) "
                   f"dead(ok={dead9_ok},reached={dead9_reached}) "
                   f"reachable(ok={reach9_ok},reached={reach9_reached}) "
                   f"restored(ok={restored9_ok},bytes_match={restore_matches_9})")
        except Exception as exc:
            record("M32R4-9", "dead local alias stays GREEN; made reachable flips RED",
                   "GREEN -> RED -> GREEN", False, f"{type(exc).__name__}: {exc}")

        # ---- M32R4-10: an ambiguous rebinding (if/else bind DIFFERENT targets) on a
        # frozen reachable path. Expected: AMBIGUOUS/RED.
        def m32r4_10_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                              M3R4_DEF4_ANCHOR.replace(
                                  "    # N5.3.1",
                                  "    if True:\n        _m32r4_r10 = _m32r4_safe\n"
                                  "    else:\n        _m32r4_r10 = _m32r4_unsafe\n"
                                  "    _m32r4_r10()\n    # N5.3.1"))
                src += BAD_HELPER_APPEND + SAFE_HELPER_APPEND
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r4_10_assert(copy_path):
            result = _m32r4_gate(copy_path)
            ue = [e for e in result["unresolved_reachable_edges"]
                  if e.get("diagnostic") == "LOCAL_REBINDING_AMBIGUOUS"]
            is_red = (not result["ok"]) and bool(ue)
            return is_red, f"gate.ok={result['ok']} ambiguous_rebinding_edges={len(ue)}"

        run_mutation(base_tmp, "M32R4-10", "ambiguous if/else rebinding to different "
                     "targets on a frozen reachable path", "AMBIGUOUS/RED",
                     m32r4_10_make, m32r4_10_assert)

        # ==================================================================
        # W3.2 CORRECTION ROUND 5 mutations (M32R5-1..8, R5 task spec section 7)
        # ==================================================================
        #
        # An independent final review of round 4
        # (C:\ALM_TPilot_AUDIT\20260730\W3_2_CORRECTION_R4_REVIEW\, verdict FAIL) found
        # the LAST residual F-2 shape: `classify_attribute_expr()`'s fallback branch
        # treated ANY attribute call as safe whenever the attribute's NAME did not
        # collide with a module-level def name -- "safe because the name doesn't match
        # a def" is exactly the unsound basis both the round-4 and this round's task
        # spec forbid. Round 5 deletes that fallback branch and replaces it with
        # explicit (receiver, attribute) assignment tracking plus a bounded,
        # auditable set of positive receiver-provenance rules (declared type
        # annotations, proven import/builtin call chains, literal containers,
        # reachable-callsite argument tracing) -- never a name-only shortcut.
        # M32R5-1..8 are the permanent causal proofs for that fix.

        R5_UNSAFE_HELPER = '\n\ndef _m32r5_unsafe():\n    return datetime.now().isoformat()\n'
        R5_SAFE_HELPER = '\n\ndef _m32r5_safe():\n    return "ok"\n'
        R5_BOX = ('\n\nclass _M32R5Box:\n    pass\n'
                 '\n_M32R5_OBJ = _M32R5Box()\n')

        def _m32r5_gate(copy_path):
            return run_panel_static_gate(panel_bot_path=copy_path)

        def _m32r5_inject(copy, extra_header_stmt, module_tail):
            src = copy.read_text(encoding="utf-8")
            src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                          M3R4_DEF4_ANCHOR.replace("    # N5.3.1",
                                                   f"    {extra_header_stmt}\n    # N5.3.1"))
            src += module_tail
            copy.write_text(src, encoding="utf-8")

        # ---- M32R5-1: bind the unsafe helper to an attribute whose name is
        # UNRELATED to anything (no def-name collision anywhere). Expected: RED.
        def m32r5_1_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                _m32r5_inject(copy, "_M32R5_OBJ.totally_unrelated_zzz()",
                              R5_UNSAFE_HELPER + R5_BOX +
                              '\n_M32R5_OBJ.totally_unrelated_zzz = _m32r5_unsafe\n')
            return copy

        def m32r5_1_assert(copy_path):
            result = _m32r5_gate(copy_path)
            hk = [k for k in result["helper_nodes_reachable"] if "_m32r5_unsafe" in k]
            is_red = (not result["ok"]) and bool(hk)
            return is_red, f"gate.ok={result['ok']} helper_reached={hk}"

        run_mutation(base_tmp, "M32R5-1", "bind unsafe helper to an unrelated "
                     "attribute name", "RED", m32r5_1_make, m32r5_1_assert)

        # ---- M32R5-2: rename the SAME attribute while keeping the SAME helper.
        # Expected: still RED -- proves the verdict tracks the assigned value, not
        # the spelling.
        def m32r5_2_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                _m32r5_inject(copy, "_M32R5_OBJ.a_completely_different_name_now()",
                              R5_UNSAFE_HELPER + R5_BOX +
                              '\n_M32R5_OBJ.a_completely_different_name_now = _m32r5_unsafe\n')
            return copy

        def m32r5_2_assert(copy_path):
            result = _m32r5_gate(copy_path)
            hk = [k for k in result["helper_nodes_reachable"] if "_m32r5_unsafe" in k]
            is_red = (not result["ok"]) and bool(hk)
            return is_red, f"gate.ok={result['ok']} helper_reached={hk}"

        run_mutation(base_tmp, "M32R5-2", "rename the attribute, keep the same "
                     "unsafe helper", "RED", m32r5_2_make, m32r5_2_assert)

        # ---- M32R5-3: bind through a local alias before assigning the attribute.
        # Expected: RED.
        def m32r5_3_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                _m32r5_inject(copy, "_M32R5_OBJ.tick()",
                              R5_UNSAFE_HELPER + R5_BOX +
                              '\n_m32r5_alias = _m32r5_unsafe\n'
                              '_M32R5_OBJ.tick = _m32r5_alias\n')
            return copy

        def m32r5_3_assert(copy_path):
            result = _m32r5_gate(copy_path)
            hk = [k for k in result["helper_nodes_reachable"] if "_m32r5_unsafe" in k]
            is_red = (not result["ok"]) and bool(hk)
            return is_red, f"gate.ok={result['ok']} helper_reached={hk}"

        run_mutation(base_tmp, "M32R5-3", "bind the unsafe helper through a local "
                     "alias before the attribute assignment", "RED",
                     m32r5_3_make, m32r5_3_assert)

        # ---- M32R5-4: replace a PROVEN IMPORTED receiver with an unknown local
        # object. Expected: RED.
        def m32r5_4_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                _m32r5_inject(copy, "_m32r5_unknown_receiver_xyz.strip()", "")
            return copy

        def m32r5_4_assert(copy_path):
            result = _m32r5_gate(copy_path)
            ue = [e for e in result["unresolved_reachable_edges"]
                  if e.get("call_category") == "UNRESOLVED_ATTRIBUTE_CALL"]
            is_red = (not result["ok"]) and bool(ue)
            return is_red, f"gate.ok={result['ok']} unresolved_attribute_edges={len(ue)}"

        run_mutation(base_tmp, "M32R5-4", "replace a proven imported receiver with "
                     "an unknown local object", "RED", m32r5_4_make, m32r5_4_assert)

        # ---- M32R5-5: branch between a safe and an unsafe assignment to the SAME
        # attribute. Expected: AMBIGUOUS/RED.
        def m32r5_5_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                _m32r5_inject(
                    copy,
                    "if True:\n        _M32R5_OBJ.tick = _m32r5_safe\n"
                    "    else:\n        _M32R5_OBJ.tick = _m32r5_unsafe\n"
                    "    _M32R5_OBJ.tick()",
                    R5_UNSAFE_HELPER + R5_SAFE_HELPER + R5_BOX)
            return copy

        def m32r5_5_assert(copy_path):
            result = _m32r5_gate(copy_path)
            ue = [e for e in result["unresolved_reachable_edges"]
                  if e.get("diagnostic") == "LOCAL_REBINDING_AMBIGUOUS"]
            is_red = (not result["ok"]) and bool(ue)
            return is_red, f"gate.ok={result['ok']} ambiguous_rebinding_edges={len(ue)}"

        run_mutation(base_tmp, "M32R5-5", "branch between safe and unsafe "
                     "attribute assignment", "AMBIGUOUS/RED", m32r5_5_make, m32r5_5_assert)

        # ---- M32R5-6: unsafe assignment BEFORE invocation, safe assignment AFTER.
        # Expected: RED at the earlier call (sequential/textual order, not
        # retroactive).
        def m32r5_6_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                _m32r5_inject(
                    copy,
                    "_M32R5_OBJ.tick = _m32r5_unsafe\n"
                    "    _M32R5_OBJ.tick()\n"
                    "    _M32R5_OBJ.tick = _m32r5_safe",
                    R5_UNSAFE_HELPER + R5_SAFE_HELPER + R5_BOX)
            return copy

        def m32r5_6_assert(copy_path):
            result = _m32r5_gate(copy_path)
            hk = [k for k in result["helper_nodes_reachable"] if "_m32r5_unsafe" in k]
            is_red = (not result["ok"]) and bool(hk)
            return is_red, f"gate.ok={result['ok']} helper_reached={hk}"

        run_mutation(base_tmp, "M32R5-6", "unsafe assignment before invocation, "
                     "safe assignment after", "RED", m32r5_6_make, m32r5_6_assert)

        # ---- M32R5-7: dead unsafe attribute binding stays GREEN; the SAME bytes
        # made reachable flip RED. Mirrors M32R2-4+5/M32R3-7/M32R4-9's
        # dead-vs-reachable causal pattern for this round's shape.
        R5_DEAD_APPEND = ('\n\ndef _m32r5_dead_container7():\n'
                         '    return _M32R5_OBJ.tick()\n')

        def _m32r5_7_probe(copy_path):
            result = _m32r5_gate(copy_path)
            key = [k for k in result["helper_nodes_reachable"] if "_m32r5_unsafe" in k]
            return result["ok"], bool(key)

        try:
            m5r7_work = base_tmp / "M32R5_7"
            m5r7_work.mkdir(parents=True, exist_ok=True)

            (m5r7_work / "clean").mkdir(parents=True, exist_ok=True)
            c7 = _fresh_copy(PANEL_BOT_PATH, m5r7_work / "clean")
            c7_ok, c7_reached = _m32r5_7_probe(c7)
            c7_bytes = c7.read_bytes()

            (m5r7_work / "dead").mkdir(parents=True, exist_ok=True)
            dead7 = _fresh_copy(PANEL_BOT_PATH, m5r7_work / "dead")
            src7 = (dead7.read_text(encoding="utf-8") + R5_UNSAFE_HELPER + R5_BOX
                    + '\n_M32R5_OBJ.tick = _m32r5_unsafe\n' + R5_DEAD_APPEND)
            dead7.write_text(src7, encoding="utf-8")
            dead7_ok, dead7_reached = _m32r5_7_probe(dead7)
            bytes_changed_7 = dead7.read_bytes() != c7_bytes

            (m5r7_work / "reachable").mkdir(parents=True, exist_ok=True)
            reach7 = _fresh_copy(PANEL_BOT_PATH, m5r7_work / "reachable")
            src7r = reach7.read_text(encoding="utf-8")
            src7r = _mutate(src7r, re.escape(M3R4_DEF4_ANCHOR),
                            M3R4_DEF4_ANCHOR.replace(
                                "    # N5.3.1",
                                "    _m32r5_dead_container7()\n    # N5.3.1"))
            src7r += R5_UNSAFE_HELPER + R5_BOX + '\n_M32R5_OBJ.tick = _m32r5_unsafe\n' + R5_DEAD_APPEND
            reach7.write_text(src7r, encoding="utf-8")
            reach7_ok, reach7_reached = _m32r5_7_probe(reach7)

            (m5r7_work / "restored").mkdir(parents=True, exist_ok=True)
            restored7 = _fresh_copy(PANEL_BOT_PATH, m5r7_work / "restored")
            restore_matches_7 = restored7.read_bytes() == c7_bytes
            restored7_ok, _ = _m32r5_7_probe(restored7)

            causal_7 = (bytes_changed_7 and c7_ok is True and not c7_reached
                       and dead7_ok is True and not dead7_reached
                       and reach7_ok is False and reach7_reached
                       and restored7_ok is True and restore_matches_7)
            record("M32R5-7", "dead unsafe attribute binding stays GREEN; the SAME "
                   "bytes made reachable flip RED",
                   "GREEN (dead, unreported) -> RED (reachable, same bytes) -> GREEN (restored)",
                   causal_7,
                   f"bytes_changed={bytes_changed_7} clean(ok={c7_ok}) "
                   f"dead(ok={dead7_ok},reached={dead7_reached}) "
                   f"reachable(ok={reach7_ok},reached={reach7_reached}) "
                   f"restored(ok={restored7_ok},bytes_match={restore_matches_7})")
        except Exception as exc:
            record("M32R5-7", "dead unsafe attribute binding stays GREEN; made "
                   "reachable flips RED", "GREEN -> RED -> GREEN", False,
                   f"{type(exc).__name__}: {exc}")

        # ---- M32R5-8: reintroduce the R4-era name-only safety rule (the exact
        # defect this round closes) into a TEMPORARY COPY of the gate tool itself,
        # and confirm the permanent A1/A2/A15-style probe detects it. Expected:
        # the focused regression probe turns RED the moment the rule reappears.
        def _m32r5_8_load_mutated_gate(tmpdir):
            gate_src = (TOOLS_DIR / "w3_2_panel_static_gate_selftest.py").read_text(encoding="utf-8")
            # Anchor the FINAL fallback specifically -- priority 1 (explicit
            # (receiver, attribute) assignment tracking) must stay untouched by this
            # mutation, so the probe below deliberately has NO tracked assignment at
            # all (an unresolved bare-name receiver, exactly the R4-review's original
            # shape) and exercises ONLY this fallback branch.
            anchor = (
                '    return _mk("UNRESOLVED_ATTRIBUTE_CALL", None, "UNKNOWN_RECEIVER",\n'
                '              f"receiver {ast.unparse(node.value)[:60]!r} is not proven safe by any "\n'
                '              f"tracked assignment, import chain, or declared/inferred type")\n')
            if anchor not in gate_src:
                raise RuntimeError("M32R5-8 anchor not found in live gate tool")
            reintroduced = (
                '    return _mk("RESOLVED_BUILTIN", None, '
                '"SAFE_ATTRIBUTE_NO_DEF_NAME_COLLISION_REINTRODUCED_M32R5_8", '
                'f"attribute .{node.attr} matches no module-level def name in the file")\n')
            mutated_src = gate_src.replace(anchor, reintroduced, 1)
            if mutated_src == gate_src:
                raise RuntimeError("M32R5-8 mutation did not change gate source")
            mutant_path = tmpdir / "mutated_gate.py"
            mutant_path.write_text(mutated_src, encoding="utf-8")
            mod_name = f"w32r5_8_mut_{uuid.uuid4().hex[:8]}"
            spec = importlib.util.spec_from_file_location(mod_name, mutant_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            # ROUND 6: the gate tool derives BASE_DIR from its OWN __file__ -- correct
            # for real usage (always loaded from tools/), but WRONG once a copy is
            # loaded from this temp directory. M32R5-8's own mutation never touches
            # a BASE_DIR-relative lookup, so this was harmless for it; restoring the
            # real project root here is defensive/future-proofing, matching the fix
            # actually REQUIRED for M32R6's cross-file annotation lookup below.
            mod.BASE_DIR = BASE_DIR
            mod.PANEL_BOT_PATH = BASE_DIR / "panel_bot.py"
            return mod, mutated_src != gate_src

        try:
            m5r8_work = base_tmp / "M32R5_8"
            m5r8_work.mkdir(parents=True, exist_ok=True)
            mutated_gate_mod, bytes_changed_8 = _m32r5_8_load_mutated_gate(m5r8_work)

            # Deliberately NO attribute assignment anywhere for this receiver/name --
            # an unresolved bare-name object, so priority 1 (assignment tracking)
            # finds nothing and this call can ONLY be decided by the final fallback
            # this mutation targets.
            (m5r8_work / "probe").mkdir(parents=True, exist_ok=True)
            probe_copy = _fresh_copy(PANEL_BOT_PATH, m5r8_work / "probe")
            src8 = probe_copy.read_text(encoding="utf-8")
            src8 = _mutate(src8, re.escape(M3R4_DEF4_ANCHOR),
                          M3R4_DEF4_ANCHOR.replace(
                              "    # N5.3.1",
                              "    _m32r5_8_unknown_receiver_xyz.some_common_method_name()\n"
                              "    # N5.3.1"))
            probe_copy.write_text(src8, encoding="utf-8")

            with_bug_result = mutated_gate_mod.run_panel_static_gate(panel_bot_path=probe_copy)
            without_bug_result = run_panel_static_gate(panel_bot_path=probe_copy)

            regression_probe_catches_it = (with_bug_result["ok"] is True
                                           and without_bug_result["ok"] is False)
            record("M32R5-8", "reintroduce the R4-era name-only Attribute safety "
                   "rule into a temporary copy of the gate tool",
                   "the focused probe turns RED the moment the rule reappears "
                   "(i.e. WITHOUT the rule the fixed gate is RED; WITH the "
                   "reintroduced rule it would wrongly go GREEN)",
                   bytes_changed_8 and regression_probe_catches_it,
                   f"bytes_changed={bytes_changed_8} "
                   f"with_reintroduced_rule.ok={with_bug_result['ok']} "
                   f"live_fixed_gate.ok={without_bug_result['ok']}")
        except Exception as exc:
            record("M32R5-8", "reintroduce the R4-era name-only rule, confirm "
                   "detection", "focused probe RED", False, f"{type(exc).__name__}: {exc}")

        # ==================================================================
        # W3.2 CORRECTION ROUND 6 mutations (M32R6-1..10, R6 task spec section 8)
        # ==================================================================
        #
        # An independent review of round 5 (C:\ALM_TPilot_AUDIT\20260731\
        # W3_2_CORRECTION_R5_REVIEW\, verdict FAIL) found the LAST residual F-2
        # regression: `for`/comprehension target binding treated a loop/comprehension
        # element as safe whenever the ITERABLE/CONTAINER it was drawn from classified
        # as a safe VALUE -- "the container is a list" does not prove "an element
        # pulled out of it is safe". Round 6 deletes that inference and replaces it
        # with classify_iterable_element()/bind_for_targets() (task section 3, forms
        # A-G): literal-container/dict elements classified individually, comprehension
        # yield expressions, .keys()/.values()/.items(), enumerate()/zip(), and a
        # bounded set of external/annotation-derived shapes -- never "container safe
        # implies element safe". M32R6-1..10 are the permanent causal proofs.
        #
        # M32R6-1..9 mutate a TEMPORARY COPY of the GATE TOOL itself (mirroring
        # M32R5-8's pattern exactly: a mutated module is loaded standalone and run
        # against the SAME probe copy of panel_bot.py as the live, unmutated gate;
        # the mutation passes causality only when the mutated gate wrongly goes GREEN
        # while the live fixed gate correctly stays RED). M32R6-10 mutates panel_bot.py
        # itself (the ORIGINAL M32-style pattern) to break one of the 21 live rows'
        # actual proof and confirm the LIVE, UNMUTATED gate tool catches it.

        M32R6_BAD_HELPER = '\n\ndef _m32r6_bad():\n    return datetime.now().isoformat()\n'
        M32R6_BOX8 = ('\n\nclass _M32R68Box:\n    pass\n'
                     '\n_M32R6_8_HOLDER = _M32R68Box()\n_M32R6_8_HOLDER.tick = _m32r6_bad\n')

        def _m32r6_load_mutated_gate(work, mutation_id, anchor, replacement):
            gate_src = (TOOLS_DIR / "w3_2_panel_static_gate_selftest.py").read_text(encoding="utf-8")
            if anchor not in gate_src:
                raise RuntimeError(f"{mutation_id} anchor not found in live gate tool")
            mutated_src = gate_src.replace(anchor, replacement, 1)
            if mutated_src == gate_src:
                raise RuntimeError(f"{mutation_id} mutation did not change gate source")
            mutant_path = work / "mutated_gate.py"
            mutant_path.write_text(mutated_src, encoding="utf-8")
            mod_name = f"w32r6_mut_{uuid.uuid4().hex[:8]}"
            spec = importlib.util.spec_from_file_location(mod_name, mutant_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            # The gate tool derives BASE_DIR from its OWN __file__ -- correct for real
            # usage (always loaded from tools/), but WRONG once loaded from this temp
            # directory: `_external_def_element_family`'s cross-file lookup (Form G,
            # used by 2 of the 21 live rows) would then search for manager_registry.py
            # relative to the temp dir and silently find nothing, masking whatever the
            # ACTUAL targeted mutation does with an UNRELATED false RED on BOTH the
            # mutated and (if this fix were skipped) even a correctly-built comparison
            # gate. Restoring the real project root here is a test-harness fix, not a
            # change to the gate tool's real-world behavior (which never loads itself
            # from anywhere but tools/).
            mod.BASE_DIR = BASE_DIR
            mod.PANEL_BOT_PATH = BASE_DIR / "panel_bot.py"
            return mod

        def _m32r6_probe_copy(work, probe_tail, probe_body):
            (work / "probe").mkdir(parents=True, exist_ok=True)
            probe_copy = _fresh_copy(PANEL_BOT_PATH, work / "probe")
            src = probe_copy.read_text(encoding="utf-8")
            injected = "\n".join("    " + l for l in probe_body.split("\n")) + "\n"
            src = _mutate(src, re.escape(M3R4_DEF4_ANCHOR),
                          M3R4_DEF4_ANCHOR.replace("    # N5.3.1", injected + "    # N5.3.1"))
            src += probe_tail
            probe_copy.write_text(src, encoding="utf-8")
            return probe_copy

        def _m32r6_run(mutation_id, description, expected, anchor, replacement,
                       probe_tail, probe_body):
            try:
                work = base_tmp / mutation_id.replace("-", "_")
                work.mkdir(parents=True, exist_ok=True)
                mutated_mod = _m32r6_load_mutated_gate(work, mutation_id, anchor, replacement)
                probe_copy = _m32r6_probe_copy(work, probe_tail, probe_body)
                with_bug_result = mutated_mod.run_panel_static_gate(panel_bot_path=probe_copy)
                without_bug_result = run_panel_static_gate(panel_bot_path=probe_copy)
                passed = with_bug_result["ok"] is True and without_bug_result["ok"] is False
                record(mutation_id, description, expected, passed,
                       f"bytes_changed=True with_reintroduced_rule.ok={with_bug_result['ok']} "
                       f"live_fixed_gate.ok={without_bug_result['ok']}")
            except Exception as exc:
                record(mutation_id, description, expected, False, f"{type(exc).__name__}: {exc}")

        # ---- M32R6-1/2/3: reintroduce "literal container is a safe VALUE, therefore
        # its element is safe" -- the exact R5 regression, at its single shared root
        # (classify_iterable_element()'s List/Set/Tuple branch; the comprehension path
        # routes through the SAME function, so one mutation exercises both E1-style and
        # E2-style probes, plus the E13-style mixed-elements probe).
        _ANCHOR_LITERAL = (
            '    if isinstance(iter_expr, (ast.List, ast.Set, ast.Tuple)):\n'
            '        return _merge_many([classify_value_expr(e, env, mctx) for e in iter_expr.elts])\n')
        _REPL_LITERAL = (
            '    if isinstance(iter_expr, (ast.List, ast.Set, ast.Tuple)):\n'
            '        return _mk("RESOLVED_BUILTIN", None, "M32R6_REINTRODUCED_CONTAINER_SAFE_BUG",\n'
            '                  "container is safe therefore element is safe (reintroduced R5 bug)")\n')

        _m32r6_run("M32R6-1", "reintroduce safe-container-implies-safe-loop-element",
                   "RED (E1-style: for fn in [unsafe]: fn())", _ANCHOR_LITERAL, _REPL_LITERAL,
                   M32R6_BAD_HELPER, "for _m6_1_fn in [_m32r6_bad]:\n    _m6_1_fn()")
        _m32r6_run("M32R6-2", "reintroduce safe-container-implies-safe-comprehension-element",
                   "RED (E2-style: [fn() for fn in [unsafe]])", _ANCHOR_LITERAL, _REPL_LITERAL,
                   M32R6_BAD_HELPER, "_m6_2_r = [_m6_2_fn() for _m6_2_fn in [_m32r6_bad]]")
        _m32r6_run("M32R6-3", "treat literal list elements uniformly from the outer "
                   "list's own safety (ignore per-element disagreement)",
                   "RED (E13-style: mixed safe/unsafe literal elements)",
                   _ANCHOR_LITERAL, _REPL_LITERAL, M32R6_BAD_HELPER,
                   "for _m6_3_x in [{'a': 1}, _m32r6_bad]:\n    _m6_3_x.get('a')")

        # ---- M32R6-4: ignore unsafe .values() element provenance.
        _ANCHOR_VALUES = (
            '        if (isinstance(func, ast.Attribute) and func.attr in ("keys", "values")\n'
            '                and not iter_expr.args and not iter_expr.keywords):\n'
            '            dict_node = _iterable_dict_node(func.value, env, mctx)\n'
            '            if dict_node is not None:\n'
            '                pairs = _dict_node_pure_pairs(dict_node)\n'
            '                if pairs is not None:\n'
            '                    keys, values = pairs\n'
            '                    exprs = keys if func.attr == "keys" else values\n'
            '                    return _merge_many([classify_value_expr(e, env, mctx) for e in exprs])\n')
        _REPL_VALUES = (
            '        if (isinstance(func, ast.Attribute) and func.attr in ("keys", "values")\n'
            '                and not iter_expr.args and not iter_expr.keywords):\n'
            '            dict_node = _iterable_dict_node(func.value, env, mctx)\n'
            '            if dict_node is not None:\n'
            '                return _mk("RESOLVED_BUILTIN", None, "M32R6_IGNORED_VALUES_ELEMENT",\n'
            '                          "ignored actual key/value provenance (reintroduced bug)")\n')
        _m32r6_run("M32R6-4", "ignore unsafe .values() element provenance", "RED (E7-style)",
                   _ANCHOR_VALUES, _REPL_VALUES, M32R6_BAD_HELPER,
                   "for _m6_4_v in {'a': _m32r6_bad}.values():\n    _m6_4_v()")

        # ---- M32R6-5: ignore unsafe value in .items() tuple.
        _ANCHOR_ITEMS = (
            '        dict_node = _iterable_dict_node(iter_expr.func.value, env, mctx)\n'
            '        if dict_node is not None:\n'
            '            pairs = _dict_node_pure_pairs(dict_node)\n'
            '            if pairs is not None:\n'
            '                keys, values = pairs\n'
            '                return {\n'
            '                    elts[0].id: _merge_many([classify_value_expr(k, env, mctx) for k in keys]),\n'
            '                    elts[1].id: _merge_many([classify_value_expr(v, env, mctx) for v in values]),\n'
            '                }\n'
            '        return {}\n')
        _REPL_ITEMS = (
            '        dict_node = _iterable_dict_node(iter_expr.func.value, env, mctx)\n'
            '        if dict_node is not None:\n'
            '            return {\n'
            '                elts[0].id: _mk("RESOLVED_BUILTIN", None, "M32R6_IGNORED_ITEMS_KEY", "bug"),\n'
            '                elts[1].id: _mk("RESOLVED_BUILTIN", None, "M32R6_IGNORED_ITEMS_VALUE", "bug"),\n'
            '            }\n'
            '        return {}\n')
        _m32r6_run("M32R6-5", "ignore unsafe value in .items() tuple", "RED (E8-style)",
                   _ANCHOR_ITEMS, _REPL_ITEMS, M32R6_BAD_HELPER,
                   "for _m6_5_k, _m6_5_v in {'a': _m32r6_bad}.items():\n    _m6_5_v()")

        # ---- M32R6-6: drop enumerate() element provenance.
        _ANCHOR_ENUM = (
            '        out = {elts[0].id: _mk("RESOLVED_BUILTIN", None, "PROVEN_ENUMERATE_INDEX",\n'
            '                              "enumerate()\'s own index is always a plain int")}\n'
            '        out.update(bind_for_targets(elts[1], iter_expr.args[0], env, mctx))\n'
            '        return out\n')
        _REPL_ENUM = (
            '        out = {elts[0].id: _mk("RESOLVED_BUILTIN", None, "PROVEN_ENUMERATE_INDEX",\n'
            '                              "enumerate()\'s own index is always a plain int")}\n'
            '        out[elts[1].id] = _mk("RESOLVED_BUILTIN", None, "M32R6_IGNORED_ENUMERATE_ITEM", "bug")\n'
            '        return out\n')
        _m32r6_run("M32R6-6", "drop enumerate() element provenance", "RED (E9-style)",
                   _ANCHOR_ENUM, _REPL_ENUM, M32R6_BAD_HELPER,
                   "for _m6_6_i, _m6_6_item in enumerate([_m32r6_bad]):\n    _m6_6_item()")

        # ---- M32R6-7: drop one side of zip() provenance.
        _ANCHOR_ZIP = (
            '        return {e.id: classify_iterable_element(a, env, mctx)\n'
            '                for e, a in zip(elts, iter_expr.args)}\n')
        _REPL_ZIP = (
            '        return {e.id: _mk("RESOLVED_BUILTIN", None, "M32R6_IGNORED_ZIP_ELEMENT", "bug")\n'
            '                for e, a in zip(elts, iter_expr.args)}\n')
        _m32r6_run("M32R6-7", "drop one side of zip() provenance", "RED (E10-style)",
                   _ANCHOR_ZIP, _REPL_ZIP, M32R6_BAD_HELPER,
                   "for _m6_7_a, _m6_7_b in zip(['safe'], [_m32r6_bad]):\n    _m6_7_b()")

        # ---- M32R6-8: use outer variable provenance inside comprehension shadowing
        # (the inner, shadowing binding should always win; this mutation makes the
        # OUTER binding win instead when both scopes define the same name). Anchored
        # in `_scan_expr_for_calls_rec` -- the function that ACTUALLY records a call
        # inside a comprehension body for BFS/reachability purposes;
        # `_comprehension_yield_classification` only governs what the comprehension's
        # OWN RESULT looks like as a value elsewhere, a separate, downstream concern.
        _ANCHOR_SHADOW = (
            '            bindings = bind_for_targets(gen.target, gen.iter, comp_env, mctx)\n'
            '            comp_env.update(bindings)\n')
        _REPL_SHADOW = (
            '            bindings = bind_for_targets(gen.target, gen.iter, comp_env, mctx)\n'
            '            comp_env.update({k: v for k, v in bindings.items() if k not in env})\n')
        _m32r6_run("M32R6-8", "use outer variable provenance inside comprehension "
                   "shadowing instead of the inner (correct) binding",
                   "RED (E18-style)", _ANCHOR_SHADOW, _REPL_SHADOW,
                   M32R6_BAD_HELPER + M32R6_BOX8,
                   "_m6_8_row = {'a': 1}\n_m6_8_result = [_m6_8_row.tick() for _m6_8_row in "
                   "[_M32R6_8_HOLDER]]")

        # ---- M32R6-9: force an unknown (untracked) iterable's element to safe --
        # the generic fallback this round's fix relies on when NO positive form (A-G)
        # applies.
        _ANCHOR_UNKNOWN = (
            '    return _mk("UNRESOLVED_LOCAL_ALIAS", None, "UNKNOWN_ELEMENT_PROVENANCE",\n'
            '              f"cannot prove the element shape of {ast.unparse(iter_expr)[:60]!r} -- "\n'
            '              f"the container/value being safe does NOT make an element drawn from "\n'
            '              f"it safe (the exact R5 regression this round closes)")\n')
        _REPL_UNKNOWN = (
            '    return _mk("RESOLVED_BUILTIN", None, "M32R6_FORCED_UNKNOWN_TO_SAFE",\n'
            '              "unknown iterable forced safe by default (reintroduced bug)")\n')
        _m32r6_run("M32R6-9", "force unknown iterable element to safe by default",
                   "RED (E5-style)", _ANCHOR_UNKNOWN, _REPL_UNKNOWN, M32R6_BAD_HELPER,
                   "for _m6_9_cb in _m6_9_unknown_xyz:\n    _m6_9_cb()")

        # ---- M32R6-10: break ONE of the 21 live row proofs directly in panel_bot.py
        # itself (the ORIGINAL M32-style source mutation, not a gate-tool mutation).
        #
        # _manager_rows() carries BOTH a `-> List[dict]` annotation AND its own
        # accumulator idiom (`out = []`; `out.append(rr)`; `return out`) -- removing
        # only the annotation leaves the accumulator fallback covering the SAME rows,
        # so that mutation alone proves nothing (confirmed: it left the gate GREEN).
        # `_get_python_processes()` is different: its own annotation is a BARE
        # `"list | None"` (no usable element family), so rows 988/24116 (its two
        # callers, part of the 21) rely SOLELY on the accumulator idiom, with no
        # redundant proof. Changing `return out` to the behaviorally IDENTICAL
        # `return out or []` (a `list` is truthy when non-empty and `[]` when empty
        # either way -- zero change to runtime behavior) breaks ONLY the accumulator
        # detector's "a bare Name is returned directly" requirement.
        _GPP_RETURN_ANCHOR = '        return out\n'

        def m32r6_10_make(tmpdir, mutate):
            tmpdir.mkdir(parents=True, exist_ok=True)
            copy = _fresh_copy(PANEL_BOT_PATH, tmpdir)
            if mutate:
                src = copy.read_text(encoding="utf-8")
                src = _mutate(src, re.escape(_GPP_RETURN_ANCHOR), '        return out or []\n')
                copy.write_text(src, encoding="utf-8")
            return copy

        def m32r6_10_assert(copy_path):
            result = run_panel_static_gate(panel_bot_path=copy_path)
            is_red = (not result["ok"]) and result["unresolved_call_total"] > 0
            return is_red, (f"gate.ok={result['ok']} "
                           f"unresolved_call_total={result['unresolved_call_total']}")

        run_mutation(base_tmp, "M32R6-10", "break _get_python_processes()'s "
                     "accumulator-return idiom (return out -> return out or []), "
                     "breaking one of the 21 live row proofs",
                     "RED -- live element-inventory gate turns red",
                     m32r6_10_make, m32r6_10_assert)

        # ==================================================================
        # W3.2 CORRECTION ROUND 7 mutations (M32R7-1..8, R7 task spec section 9): the
        # D1 fix -- _merge_classifications()/_merge_element_provenance() reconciling
        # BOTH branches' element_provenance/dict_info at a control-flow join, instead
        # of ever preferring one branch -- and the unique-target inventory fix (task
        # section 6). M32R7-1..6 reuse the ESTABLISHED M32R6 pattern exactly: mutate
        # a TEMPORARY COPY of the gate tool, run it against a synthetic probe
        # alongside the LIVE (fixed) gate on the SAME probe, and require the mutated
        # leg wrongly GREEN (or, for the over-conservatism probe, wrongly RED) while
        # the live leg stays correct. M32R7-7 mutates a temporary gate copy but
        # probes the REAL panel_bot.py (mirroring M32R6-10 -- the defect it proves is
        # about the live file's OWN duplicate-analysis shape, not reproducible with a
        # synthetic single-pass probe). M32R7-8 probes this ROUND's OWN
        # report-consistency check, not the gate tool.

        M32R7_COND_TRUE = "True"

        def _m32r7_if_probe(cond, safe_first, safe_second):
            """One `if/else` assigning `data` to a one-element literal list in each
            branch, then iterating `data` and invoking each element -- the exact D1
            shape (task section 2)."""
            first = "['m32r7_safe']" if safe_first else "[_m32r6_bad]"
            second = "['m32r7_safe']" if safe_second else "[_m32r6_bad]"
            return (f"if {cond}:\n    data = {first}\nelse:\n    data = {second}\n"
                    "for _m32r7_fn in data:\n    _m32r7_fn()")

        def _m32r7_try_probe(safe_body, safe_except):
            first = "['m32r7_safe']" if safe_body else "[_m32r6_bad]"
            second = "['m32r7_safe']" if safe_except else "[_m32r6_bad]"
            return (f"try:\n    data = {first}\nexcept Exception:\n    data = {second}\n"
                    "for _m32r7_fn in data:\n    _m32r7_fn()")

        # anchor shared by M32R7-1, M32R7-3/4 (same insertion point), and M32R7-6 --
        # the TOP of the same-category branch in _merge_classifications().
        _ANCHOR_MERGE_ENTRY = (
            '    if (ra.get("category"), ra.get("target")) == (rb.get("category"), rb.get("target")):\n'
            '        ea, eb = ra.get("element_provenance"), rb.get("element_provenance")\n')
        _REPL_MERGE_RETURN_RA = (
            '    if (ra.get("category"), ra.get("target")) == (rb.get("category"), rb.get("target")):\n'
            '        return ra  # M32R7 REGRESSION: unconditionally prefers ra again\n'
            '        ea, eb = ra.get("element_provenance"), rb.get("element_provenance")\n')
        _REPL_MERGE_RETURN_RB = (
            '    if (ra.get("category"), ra.get("target")) == (rb.get("category"), rb.get("target")):\n'
            '        return rb  # M32R7-4 REGRESSION: unconditionally prefers rb\n'
            '        ea, eb = ra.get("element_provenance"), rb.get("element_provenance")\n')

        # ---- M32R7-1: restore the OLD (pre-round-7) same-category merge rule --
        # "return ra unchanged" -- the exact D1 regression the R6 review found.
        _m32r6_run("M32R7-1", "restore the old merge rule that returns one branch "
                   "unchanged when category/target match",
                   "RED (M1-style: if-branch safe, else-branch unsafe)",
                   _ANCHOR_MERGE_ENTRY, _REPL_MERGE_RETURN_RA, M32R6_BAD_HELPER,
                   _m32r7_if_probe(M32R7_COND_TRUE, safe_first=True, safe_second=False))

        # ---- M32R7-2: ignore element_provenance during merge -- the same-category
        # path still synthesizes a merged classification, but never attaches the
        # reconciled element_provenance, so a name bound to two SAFE-but-different
        # literals across branches loses its element evidence entirely and (correctly
        # fail-closed, but WRONGLY for a genuinely safe probe) becomes unresolved.
        _ANCHOR_MERGE_ELEM_ATTACH = (
            '        if ea is not None or eb is not None:\n'
            '            merged["element_provenance"] = _merge_element_provenance(ea, eb)\n')
        _REPL_MERGE_ELEM_ATTACH = (
            '        if False and (ea is not None or eb is not None):\n'
            '            merged["element_provenance"] = _merge_element_provenance(ea, eb)\n')
        _m32r7_2_work = base_tmp / "M32R7_2"
        _m32r7_2_work.mkdir(parents=True, exist_ok=True)
        try:
            _m32r7_2_mod = _m32r6_load_mutated_gate(
                _m32r7_2_work, "M32R7-2", _ANCHOR_MERGE_ELEM_ATTACH, _REPL_MERGE_ELEM_ATTACH)
            _m32r7_2_probe = _m32r6_probe_copy(
                _m32r7_2_work, M32R6_BAD_HELPER,
                _m32r7_if_probe(M32R7_COND_TRUE, safe_first=True, safe_second=True)
                .replace("_m32r7_fn()", "_m32r7_fn.upper()"))
            _wb = _m32r7_2_mod.run_panel_static_gate(panel_bot_path=_m32r7_2_probe)
            _nb = run_panel_static_gate(panel_bot_path=_m32r7_2_probe)
            _passed = _wb["ok"] is False and _nb["ok"] is True
            record("M32R7-2", "ignore element_provenance during merge (never attach "
                   "it on the same-category path)",
                   "RED (M3-style: both branches equivalent safe strings -- dropping "
                   "element_provenance wrongly fails this closed)", _passed,
                   f"bytes_changed=True with_bug.ok={_wb['ok']} "
                   f"(expected False -- wrongly RED) live_fixed_gate.ok={_nb['ok']} "
                   f"(expected True -- correctly GREEN)")
        except Exception as exc:
            record("M32R7-2", "ignore element_provenance during merge", "RED", False,
                   f"{type(exc).__name__}: {exc}")

        # ---- M32R7-3 / M32R7-4: branch-order symmetry -- "always prefer ra" and
        # "always prefer rb" are each INDIVIDUALLY wrong on only ONE of the two
        # argument orderings _merge_envs() passes for the same logical if/else (`a`
        # = body_env passed first, `b` = orelse_env passed second) -- proven by
        # showing the mutated gate gives DIFFERENT verdicts for the same scenario
        # with the branches swapped, while the live (fixed) gate agrees on both.
        def _m32r7_symmetry_run(mutation_id, description, anchor, replacement):
            try:
                work = base_tmp / mutation_id.replace("-", "_")
                work.mkdir(parents=True, exist_ok=True)
                mutated_mod = _m32r6_load_mutated_gate(work, mutation_id, anchor, replacement)
                probe_a = _m32r6_probe_copy(
                    work.parent / f"{mutation_id}_a", M32R6_BAD_HELPER,
                    _m32r7_if_probe(M32R7_COND_TRUE, safe_first=True, safe_second=False))
                probe_b = _m32r6_probe_copy(
                    work.parent / f"{mutation_id}_b", M32R6_BAD_HELPER,
                    _m32r7_if_probe(M32R7_COND_TRUE, safe_first=False, safe_second=True))
                mut_a = mutated_mod.run_panel_static_gate(panel_bot_path=probe_a)["ok"]
                mut_b = mutated_mod.run_panel_static_gate(panel_bot_path=probe_b)["ok"]
                live_a = run_panel_static_gate(panel_bot_path=probe_a)["ok"]
                live_b = run_panel_static_gate(panel_bot_path=probe_b)["ok"]
                passed = (mut_a != mut_b) and (live_a == live_b)
                record(mutation_id, description,
                       "branch-order symmetry proof RED (mutated gate disagrees "
                       "between the branches swapped; live gate agrees)", passed,
                       f"bytes_changed=True mutated: safe_first={mut_a} "
                       f"unsafe_first={mut_b} (expected to DISAGREE) | "
                       f"live: safe_first={live_a} unsafe_first={live_b} "
                       f"(expected to AGREE, both False/RED)")
            except Exception as exc:
                record(mutation_id, description, "branch-order symmetry proof RED",
                       False, f"{type(exc).__name__}: {exc}")

        _m32r7_symmetry_run("M32R7-3", "make merge select the first branch "
                            "unconditionally", _ANCHOR_MERGE_ENTRY, _REPL_MERGE_RETURN_RA)
        _m32r7_symmetry_run("M32R7-4", "make merge select the second branch "
                            "unconditionally", _ANCHOR_MERGE_ENTRY, _REPL_MERGE_RETURN_RB)

        # ---- M32R7-5: treat "this branch attached no element evidence" as SAFE
        # instead of UNKNOWN -- breaks the safe-vs-unknown-provenance rule (M5).
        #
        # The probe's TWO branches must both classify as the SAME top-level safe
        # category (RESOLVED_BUILTIN/target=None) so the outer merge actually reaches
        # _merge_element_provenance() at all -- a branch whose TOP-LEVEL category
        # already differs (e.g. an unresolved Attribute call) is caught by the
        # ordinary category/target disagreement rule regardless of this mutation, and
        # would never exercise it. `list(x)` is a builtin call -- RESOLVED_BUILTIN/
        # None, like the literal, but attaches NO element_provenance of its own (only
        # a curated set of forms do), so ONE branch has element evidence and the
        # OTHER genuinely has none -- exactly the safe-vs-unknown shape M5 requires.
        _ANCHOR_UNKNOWN_ELEMENT = (
            '_UNKNOWN_ELEMENT_ON_BRANCH = _mk(\n'
            '    "UNRESOLVED_LOCAL_ALIAS", None, "UNKNOWN_ELEMENT_PROVENANCE_ON_BRANCH",\n'
            '    "this branch attached no independent element evidence")\n')
        _REPL_UNKNOWN_ELEMENT = (
            '_UNKNOWN_ELEMENT_ON_BRANCH = _mk(\n'
            '    "RESOLVED_BUILTIN", None, "M32R7_TREATED_UNKNOWN_AS_SAFE",\n'
            '    "reintroduced bug: unknown branch element treated as safe")\n')
        _m32r6_run("M32R7-5", "treat safe + unknown element provenance as safe",
                   "RED (M5-style: safe branch vs unknown-provenance branch, both "
                   "sides RESOLVED_BUILTIN/None so the merge reaches element "
                   "reconciliation)",
                   _ANCHOR_UNKNOWN_ELEMENT, _REPL_UNKNOWN_ELEMENT, M32R6_BAD_HELPER,
                   "if True:\n    data = ['m32r7_safe']\nelse:\n    "
                   "data = list(_m32r7_unknown_source)\n"
                   "for _m32r7_fn in data:\n    _m32r7_fn()")

        # ---- M32R7-6: the SAME root-cause mutation as M32R7-1, probed through
        # try/except instead of if/else -- proves the fix is genuinely SHARED
        # control-flow-join infrastructure (Try already routes through the same
        # _merge_envs()/_merge_classifications() If uses), not an if/else-only patch.
        try:
            work = base_tmp / "M32R7_6"
            work.mkdir(parents=True, exist_ok=True)
            mutated_mod = _m32r6_load_mutated_gate(
                work, "M32R7-6", _ANCHOR_MERGE_ENTRY, _REPL_MERGE_RETURN_RA)
            probe_copy = _m32r6_probe_copy(
                work, M32R6_BAD_HELPER,
                _m32r7_try_probe(safe_body=True, safe_except=False))
            with_bug_result = mutated_mod.run_panel_static_gate(panel_bot_path=probe_copy)
            without_bug_result = run_panel_static_gate(panel_bot_path=probe_copy)
            passed = with_bug_result["ok"] is True and without_bug_result["ok"] is False
            record("M32R7-6", "break try/except merge (same root cause as M32R7-1, "
                   "probed via try/except instead of if/else)",
                   "RED (M8-style: try body safe, except handler unsafe)", passed,
                   f"bytes_changed=True with_reintroduced_rule.ok={with_bug_result['ok']} "
                   f"live_fixed_gate.ok={without_bug_result['ok']}")
        except Exception as exc:
            record("M32R7-6", "break try/except merge", "RED", False,
                   f"{type(exc).__name__}: {exc}")

        # ---- M32R7-7: count raw analysis-log entries as unique targets (the exact
        # round-6 reporting defect, task section 6) -- probed on the REAL
        # panel_bot.py, since the defect only manifests where a def is genuinely
        # analyzed more than once in a single run (_get_python_processes, analyzed
        # once via the main BFS and again via _accumulator_element_family's on-demand
        # pass -- see _current_fn_key's docstring). A synthetic single-pass probe
        # would not exercise this at all.
        _ANCHOR_DEDUP = (
            '    unique_targets = list(_unique_by_identity.values())\n'
            '    unique_target_total = len(unique_targets)\n')
        _REPL_DEDUP = (
            '    unique_targets = list(element_log)  # M32R7-7 REGRESSION: no dedup\n'
            '    unique_target_total = len(unique_targets)\n')
        try:
            work = base_tmp / "M32R7_7"
            work.mkdir(parents=True, exist_ok=True)
            mutated_mod = _m32r6_load_mutated_gate(work, "M32R7-7", _ANCHOR_DEDUP, _REPL_DEDUP)
            with_bug_result = mutated_mod.run_panel_static_gate(panel_bot_path=PANEL_BOT_PATH)
            without_bug_result = run_panel_static_gate(panel_bot_path=PANEL_BOT_PATH)
            passed = (with_bug_result["ok"] is False
                     and with_bug_result.get("unique_target_coverage_ok") is False
                     and without_bug_result["ok"] is True)
            record("M32R7-7", "count raw analysis-log entries as unique targets "
                   "(no dedup) on the REAL panel_bot.py",
                   "RED -- unique-inventory coverage check turns red", passed,
                   f"bytes_changed=True with_bug.ok={with_bug_result['ok']} "
                   f"with_bug.unique_target_coverage_ok="
                   f"{with_bug_result.get('unique_target_coverage_ok')} "
                   f"live_fixed_gate.ok={without_bug_result['ok']}")
        except Exception as exc:
            record("M32R7-7", "count raw analysis-log entries as unique targets",
                   "RED", False, f"{type(exc).__name__}: {exc}")

        # ---- M32R7-8: restore the round-6-era INACCURATE evidence claim for live
        # rows 956-959 ("proven solely by JSON-decode") into this round's OWN
        # report-consistency check -- proving that check is a REAL, falsifiable
        # cross-check against MEASURED mechanism dependency, not decorative prose.
        # Measured fact (independently re-derived here by disabling each candidate
        # mechanism in its own temporary gate copy and re-running the REAL
        # panel_bot.py -- exactly the method report 04 of the R7 review uses): AFTER
        # the D1 fix, BOTH the json-decode element attach AND the literal-container
        # element merge are independently load-bearing for rows 956-959 (removing
        # EITHER one alone turns those 4 rows unresolved) -- so the correct claim is
        # "jointly proven", never "json-decode only".
        def _m32r7_8_measure(tag, anchor, replacement):
            gate_src = (TOOLS_DIR / "w3_2_panel_static_gate_selftest.py").read_text(encoding="utf-8")
            if anchor not in gate_src:
                raise RuntimeError(f"M32R7-8 measurement anchor missing: {tag}")
            mutated_src = gate_src.replace(anchor, replacement, 1)
            if mutated_src == gate_src:
                raise RuntimeError(f"M32R7-8 measurement mutation did not change source: {tag}")
            work = base_tmp / f"M32R7_8_measure_{tag}"
            work.mkdir(parents=True, exist_ok=True)
            mutant_path = work / "mutated_gate.py"
            mutant_path.write_text(mutated_src, encoding="utf-8")
            mod_name = f"w32r7_8_{tag}_{uuid.uuid4().hex[:8]}"
            spec = importlib.util.spec_from_file_location(mod_name, mutant_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            mod.BASE_DIR = BASE_DIR
            mod.PANEL_BOT_PATH = BASE_DIR / "panel_bot.py"
            r = mod.run_panel_static_gate(panel_bot_path=PANEL_BOT_PATH)
            return r["unresolved_call_total"] > 0

        try:
            no_json_breaks_it = _m32r7_8_measure(
                "no_json_element",
                '        if _is_json_decode_call(node.func, env, mctx):\n',
                '        if False and _is_json_decode_call(node.func, env, mctx):\n')
            no_literal_breaks_it = _m32r7_8_measure(
                "no_literal_element",
                '        result["element_provenance"] = _merge_many(\n'
                '            [classify_value_expr(e, env, mctx) for e in expr.elts])\n',
                '        pass\n')
            measured_joint = bool(no_json_breaks_it and no_literal_breaks_it)
            correct_claim = ("jointly_proven_by_json_decode_and_literal_container"
                             if measured_joint else "json_decode_only")

            def _report_consistency_check(claimed_mechanism: str) -> bool:
                """The exact cross-check `report_corrections.json`/report 07 must
                pass: the claimed evidence for rows 956-959 must equal the MEASURED
                mechanism dependency, not a hand-written assertion."""
                return claimed_mechanism == correct_claim

            live_report_claim_ok = _report_consistency_check(
                "jointly_proven_by_json_decode_and_literal_container")
            reintroduced_r6_claim_ok = _report_consistency_check("json_decode_only")
            passed = live_report_claim_ok is True and reintroduced_r6_claim_ok is False
            record("M32R7-8", "restore the round-6-era inaccurate evidence claim for "
                   "rows 956-959 ('json-decode only') into the report-consistency "
                   "check",
                   "report-consistency RED (the reintroduced claim must fail the "
                   "check; the correct claim must pass it)", passed,
                   f"measured_no_json_element_breaks_it={no_json_breaks_it} "
                   f"measured_no_literal_element_breaks_it={no_literal_breaks_it} "
                   f"correct_claim={correct_claim!r} "
                   f"live_report_claim_check={live_report_claim_ok} "
                   f"reintroduced_r6_claim_check={reintroduced_r6_claim_ok}")
        except Exception as exc:
            record("M32R7-8", "restore the round-6-era inaccurate evidence claim for "
                   "rows 956-959 into the report-consistency check",
                   "report-consistency RED", False, f"{type(exc).__name__}: {exc}")

    post_hashes = {p.name: _sha256(p) for p in
                   (STORAGE_PATH, PREFLIGHT_PATH, MAIN_PATH, STATS_ENGINE_PATH, PANEL_BOT_PATH)}
    hashes_unchanged = pre_hashes == post_hashes
    print()
    print(f"live source hashes unchanged across the whole mutation run: {hashes_unchanged}")
    if not hashes_unchanged:
        for fname in pre_hashes:
            if pre_hashes[fname] != post_hashes[fname]:
                print(f"  !! {fname} CHANGED: {pre_hashes[fname][:12]} -> {post_hashes[fname][:12]}")

    n_pass = sum(1 for r in RESULTS if r["status"] == "PASS")
    n_fail = len(RESULTS) - n_pass
    print()
    print(f"Mutation totals: {n_pass}/{len(RESULTS)} PASS, {n_fail} FAIL")

    ok = hashes_unchanged and n_fail == 0
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _last_sunday(year: int, month: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


if __name__ == "__main__":
    sys.exit(main())
