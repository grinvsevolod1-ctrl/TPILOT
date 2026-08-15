# -*- coding: utf-8 -*-
"""tools/w3_3_c1_reader_parity_selftest.py -- W3.3-A permanent proof suite.

Proves that the two migrated storage.py C1 (work-day) readers --
manager_effective_is_working (R2) and manager_schedule_list_working_on_date (R1) --
route through the W3.1 canonical resolver's clock-free work-dimension tier chain with
ZERO observable behavior change versus the pre-edit bodies, per the W3.3 plan freeze
(~/.claude/plans/model-opus-5-mode-vast-bachman.md) S6/S9/S10/S14.

Covers, in one file (D-1/budget cap: <=2 new tool files in W3.3-A):
  * P1-P4 -- S9.2.1 normalized semantic-preservation proof of the relocated legacy R1
    body, against the pre-edit active definition extracted from the external backup.
  * The S6 scenario matrix rows 1-24, 27-31 (25/26 are W3.3-B, out of scope here).
  * Equivalence: _w3_c1_is_working(...) == w3_resolve_schedule(...)["is_working_day"].
  * >=2000 generated parity cases, new body vs the real pre-edit body (loaded from the
    external backup as an independent module -- not a reconstruction).
  * Uniqueness: the legacy R1 SQL/decision logic exists exactly once post-change.
  * Preflight (R1) consumer integration (direct import, real functions).
  * The M33-1..M33-16 mutation legs, green->red->green control sequence (same
    technique as tools/w3_1_mutation_proof.py).

Uses throwaway temporary SQLite files only -- a process-level guard aborts the run if
any code path under test tries to open the real project DB. Never opens
C:\\ALM_TPilot\\db\\. Never writes inside C:\\ALM_TPilot (D-4) -- all scratch files live
under tempfile.mkdtemp().

    python tools\\w3_3_c1_reader_parity_selftest.py
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_PATH = BASE_DIR / "storage.py"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

BACKUP_DIR = Path(r"C:\ALM_TPilot_AUDIT\20260803\W3_3_C1_READERS_IMPLEMENTATION\BACKUP")

FAILURES: list[str] = []
RESULTS: list[dict] = []
_REAL_DB_MARKERS = ("data_tpilot.db", os.path.join("ALM_TPilot", "db"))


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)
    RESULTS.append({"label": label, "ok": bool(condition), "detail": detail})


class _RealDbGuard:
    """Aborts the run if any sqlite3.connect call (through storage's own connect
    helper) resolves to the real project DB. All test DBs live under a
    tempfile.mkdtemp() root, which never matches these markers."""

    def __enter__(self):
        self._orig_connect = storage._bsl_sqlite3.connect

        def guarded_connect(path, *a, **kw):
            spath = str(path)
            for marker in _REAL_DB_MARKERS:
                if marker.lower() in spath.lower():
                    raise RuntimeError(f"REAL DB GUARD TRIPPED: refusing to open {spath!r}")
            return self._orig_connect(path, *a, **kw)

        storage._bsl_sqlite3.connect = guarded_connect
        return self

    def __exit__(self, *exc):
        storage._bsl_sqlite3.connect = self._orig_connect


def _new_db(tmpdir: str, name: str = None) -> str:
    return os.path.join(tmpdir, name or f"w33_{uuid.uuid4().hex[:8]}.db")


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------

def setup_fixture(db_path, *, explicit=(), links=(), sources=()):
    """explicit: iterable of (manager_key, work_date, is_working)
    links: iterable of (manager_key, source_key)
    sources: iterable of (source_key, {weekday: 0/1, ...}, enabled)"""
    storage.ensure_manager_schedule_tables(db_path)
    storage.ensure_source_work_schedule(db_path)
    con = storage._bsl_connect(db_path)
    try:
        for mk, wd, iw in explicit:
            con.execute(
                "INSERT OR REPLACE INTO manager_work_schedule_days"
                "(manager_key, work_date, is_working, source, updated_at) VALUES(?,?,?,?,?)",
                (mk, wd, int(iw), "test", ""),
            )
        for mk, sk in links:
            con.execute(
                "INSERT OR REPLACE INTO manager_source_links"
                "(manager_key, source_key, created_at, updated_at) VALUES(?,?,?,?)",
                (mk, sk, "", ""),
            )
        cols = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        for sk, flags, enabled in sources:
            vals = [int((flags or {}).get(c, 1)) for c in cols]
            con.execute(
                "INSERT OR REPLACE INTO source_work_schedule"
                "(source_key, mon,tue,wed,thu,fri,sat,sun, enabled, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sk, *vals, int(enabled), ""),
            )
        con.commit()
    finally:
        con.close()


ALL_ON = {"mon": 1, "tue": 1, "wed": 1, "thu": 1, "fri": 1, "sat": 1, "sun": 1}
ALL_OFF = {"mon": 0, "tue": 0, "wed": 0, "thu": 0, "fri": 0, "sat": 0, "sun": 0}


# ---------------------------------------------------------------------------
# Legacy oracle: the REAL pre-edit storage.py, loaded from the external backup as an
# independent module. Not a reconstruction -- the exact code that ran before W3.3-A.
# ---------------------------------------------------------------------------

def find_backup() -> Path:
    matches = sorted(BACKUP_DIR.glob("storage.py.bak_w3_3a_*"))
    if not matches:
        raise RuntimeError(f"no storage.py.bak_w3_3a_* backup found under {BACKUP_DIR}")
    return matches[-1]


def load_module_copy(src_path: Path, tmpdir: Path, tag: str):
    dest = tmpdir / f"{tag}_{uuid.uuid4().hex[:8]}.py"
    shutil.copyfile(src_path, dest)
    mod_name = f"{tag}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(mod_name, dest)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod, dest


# ---------------------------------------------------------------------------
# AST helpers -- active (last) module-level def extraction
# ---------------------------------------------------------------------------

def extract_last_funcdef(source_text: str, name: str) -> ast.FunctionDef:
    tree = ast.parse(source_text)
    found = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            found = node  # last one wins
    if found is None:
        raise RuntimeError(f"no module-level def {name} found")
    return found


def _strip_docstring(body: list) -> list:
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        return body[1:]
    return body


def normalized_dump(node: ast.FunctionDef) -> str:
    body = _strip_docstring(list(node.body))
    dummy = ast.FunctionDef(
        name="_NORMALIZED_", args=node.args, body=body, decorator_list=[],
        returns=None, type_comment=None,
    )
    ast.fix_missing_locations(dummy)
    return ast.dump(dummy, include_attributes=False, annotate_fields=True)


def collect_sql_strings(node: ast.FunctionDef) -> list:
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            up = n.value.upper()
            if "SELECT" in up or ("FROM" in up and "WHERE" in up):
                out.append(n.value)
    return sorted(out)


def branch_inventory(node: ast.FunctionDef) -> dict:
    counts = {"If": 0, "For": 0, "Try": 0, "ExceptHandler": 0, "Return": 0}
    return_shapes = []
    for n in ast.walk(node):
        cls = type(n).__name__
        if cls in counts:
            counts[cls] += 1
        if isinstance(n, ast.Return):
            v = n.value
            if v is None:
                return_shapes.append("None")
            elif isinstance(v, ast.List) and not v.elts:
                return_shapes.append("[]")
            elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "sorted":
                return_shapes.append("sorted(...)")
            else:
                return_shapes.append(type(v).__name__)
    counts["return_shapes"] = sorted(return_shapes)
    return counts


def run_p1_p4(tmpdir: Path) -> None:
    backup_path = find_backup()
    check("P0: backup file found", True, str(backup_path))

    backup_text = backup_path.read_text(encoding="utf-8-sig")
    live_text = STORAGE_PATH.read_text(encoding="utf-8-sig")

    pre_edit_node = extract_last_funcdef(backup_text, "manager_schedule_list_working_on_date")
    new_node = extract_last_funcdef(live_text, "_c1_legacy_list_working_on_date")

    # P1 -- normalized AST equality
    d1, d2 = normalized_dump(pre_edit_node), normalized_dump(new_node)
    check("P1: normalized-AST equality (pre-edit R1 body vs _c1_legacy_list_working_on_date)",
          d1 == d2, "" if d1 == d2 else "AST dumps differ")

    # P2 -- exact SQL-string multiset equality
    sql1, sql2 = collect_sql_strings(pre_edit_node), collect_sql_strings(new_node)
    check("P2: exact SQL-string multiset equality", sql1 == sql2,
          f"pre_edit={sql1!r} new={sql2!r}" if sql1 != sql2 else "")

    # P3 -- branch/return-shape inventory
    inv1, inv2 = branch_inventory(pre_edit_node), branch_inventory(new_node)
    check("P3: branch/return-shape inventory identical", inv1 == inv2, f"{inv1} vs {inv2}")

    # P4 -- behavioral parity, pre-edit body (loaded as a real independent module) vs
    # the new relocated body, across a generated matrix.
    legacy_mod, _ = load_module_copy(backup_path, tmpdir, "storage_p4_legacy")
    mismatches = 0
    total = 0
    with _RealDbGuard():
        rng = random.Random(20260803)
        weekdays = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
                    "2026-08-07", "2026-08-08", "2026-08-09"]
        for i in range(60):
            db_path = _new_db(str(tmpdir))
            explicit = []
            links = []
            sources = []
            managers = [f"m{i}_{j}" for j in range(5)]
            for mgr in managers:
                if rng.random() < 0.4:
                    explicit.append((mgr, rng.choice(weekdays), rng.choice([0, 1])))
                if rng.random() < 0.5:
                    sk = f"s{i}"
                    links.append((mgr, sk))
            if links:
                sources.append((f"s{i}", rng.choice([ALL_ON, ALL_OFF, {"mon": 1, "tue": 0, "wed": 1,
                                                                        "thu": 0, "fri": 1, "sat": 0, "sun": 1}]),
                                 rng.choice([0, 1])))
            setup_fixture(db_path, explicit=explicit, links=links, sources=sources)
            for wd in weekdays + ["", "bad-date", "2026-13-40"]:
                total += 1
                legacy_out = legacy_mod.manager_schedule_list_working_on_date(wd, db_path=db_path)
                new_out = storage._c1_legacy_list_working_on_date(wd, db_path=db_path)
                if legacy_out != new_out:
                    mismatches += 1
    check("P4: behavioral parity (pre-edit body vs relocated legacy body)", mismatches == 0,
          f"{mismatches}/{total} mismatches")

    # Uniqueness: the legacy SQL literals appear exactly once in post-change storage.py,
    # and the new public body contains none of them.
    live_tree = ast.parse(live_text)
    new_public_node = None
    for node in live_tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "manager_schedule_list_working_on_date":
            new_public_node = node
    new_public_sql = set(collect_sql_strings(new_public_node)) if new_public_node else set()
    legacy_sql_set = set(sql2)
    overlap = new_public_sql & legacy_sql_set
    check("Step 5: new public R1 body contains none of the legacy SQL literals",
          not overlap, f"overlap={overlap}")

    # Each legacy SQL literal (as a parsed, already-concatenated AST string constant --
    # source text search would not match since the literal is split across multiple
    # quoted fragments) must appear in exactly ONE module-level function body.
    module_wide_counts: dict = {}
    for node in live_tree.body:
        if isinstance(node, ast.FunctionDef):
            for lit in set(collect_sql_strings(node)):
                module_wide_counts[lit] = module_wide_counts.get(lit, 0) + 1
    per_literal_ok = True
    detail_bits = []
    for lit in legacy_sql_set:
        n = module_wide_counts.get(lit, 0)
        if n != 1:
            per_literal_ok = False
            detail_bits.append(f"{lit[:40]!r} in {n} function(s)")
    check("Step 5: each legacy SQL literal appears in exactly one function in storage.py",
          per_literal_ok, "; ".join(detail_bits))


# ---------------------------------------------------------------------------
# Scenario matrix S6 rows 1-24, 27-31
# ---------------------------------------------------------------------------

def run_scenarios(tmpdir: Path, legacy_mod) -> None:
    D = "2026-08-10"  # a Monday

    with _RealDbGuard():
        # 1. explicit ON
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", D, 1)])
        check("S1: explicit ON -> R2 True", storage.manager_effective_is_working("m1", D, db_path=db) is True)
        check("S1: explicit ON -> R1 contains", "m1" in storage.manager_schedule_list_working_on_date(D, db_path=db))

        # 2. explicit OFF
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", D, 0)])
        check("S2: explicit OFF -> R2 False", storage.manager_effective_is_working("m1", D, db_path=db) is False)
        check("S2: explicit OFF -> R1 excludes", "m1" not in storage.manager_schedule_list_working_on_date(D, db_path=db))

        # 3. inherits enabled source weekday ON
        db = _new_db(str(tmpdir))
        setup_fixture(db, links=[("m1", "s1")], sources=[("s1", ALL_ON, 1)])
        check("S3: inherited ON -> R2 True", storage.manager_effective_is_working("m1", D, db_path=db) is True)
        check("S3: inherited ON -> R1 contains", "m1" in storage.manager_schedule_list_working_on_date(D, db_path=db))

        # 4. inherits source weekday OFF
        db = _new_db(str(tmpdir))
        setup_fixture(db, links=[("m1", "s1")], sources=[("s1", ALL_OFF, 1)])
        check("S4: inherited OFF -> R2 False", storage.manager_effective_is_working("m1", D, db_path=db) is False)
        check("S4: inherited OFF -> R1 excludes", "m1" not in storage.manager_schedule_list_working_on_date(D, db_path=db))

        # 5. source disabled
        db = _new_db(str(tmpdir))
        setup_fixture(db, links=[("m1", "s1")], sources=[("s1", ALL_ON, 0)])
        check("S5: source disabled -> R2 False", storage.manager_effective_is_working("m1", D, db_path=db) is False)
        check("S5: source disabled -> R1 excludes", "m1" not in storage.manager_schedule_list_working_on_date(D, db_path=db))

        # 6. no source link
        db = _new_db(str(tmpdir))
        setup_fixture(db)
        check("S6: no link -> R2 False (fail-closed)", storage.manager_effective_is_working("m1", D, db_path=db) is False)

        # 7. explicit OFF beats inherited ON
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", D, 0)], links=[("m1", "s1")], sources=[("s1", ALL_ON, 1)])
        check("S7: explicit OFF beats inherited ON -> R2 False", storage.manager_effective_is_working("m1", D, db_path=db) is False)
        check("S7: explicit OFF beats inherited ON -> R1 excludes", "m1" not in storage.manager_schedule_list_working_on_date(D, db_path=db))

        # 8. absent manager key
        db = _new_db(str(tmpdir))
        setup_fixture(db)
        check("S8: absent manager -> R2 False", storage.manager_effective_is_working("nobody", D, db_path=db) is False)
        check("S8: absent manager -> not in R1", "nobody" not in storage.manager_schedule_list_working_on_date(D, db_path=db))

        # 9. "deleted manager" (no managers-table dependency by construction)
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("ghost", D, 1)])
        check("S9: resolver has no managers-table dependency -- retention unaffected",
              storage.manager_effective_is_working("ghost", D, db_path=db) is True)
        src_text = STORAGE_PATH.read_text(encoding="utf-8-sig")
        c1_block = src_text[src_text.index("_W3_C1_ENSURE_TABLES ="):src_text.index("_c1_legacy_list_working_on_date(work_date, db_path=None)")]
        check("S10: C1 block references no 'managers' table", "FROM managers" not in c1_block and "JOIN managers" not in c1_block)

        # 11. source with no linked managers
        db = _new_db(str(tmpdir))
        setup_fixture(db, sources=[("s1", ALL_ON, 1)])
        check("S11: source with no managers -> R1 []", storage.manager_schedule_list_working_on_date(D, db_path=db) == [])

        # 12/14. 30 consecutive historical dates, byte-identical vs the real pre-edit body
        db = _new_db(str(tmpdir))
        dates = [(datetime.strptime(D, "%Y-%m-%d") + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(30)]
        explicit = [(f"m{i}", dates[i % 30], i % 2) for i in range(15)]
        links = [(f"m{i}", "sA") for i in range(15, 25)]
        setup_fixture(db, explicit=explicit, links=links, sources=[("sA", ALL_ON, 1)])
        all_match = True
        for wd in dates:
            a = legacy_mod.manager_schedule_list_working_on_date(wd, db_path=db)
            b = storage.manager_schedule_list_working_on_date(wd, db_path=db)
            if a != b:
                all_match = False
        check("S12/S14: R1 lists byte-identical over 30 dates (== 16:50/14:00 loop inputs)", all_match)

        # 15. manual /bizlink_create_one fail-open (empty R1 must not raise)
        db = _new_db(str(tmpdir))
        setup_fixture(db)
        try:
            out = storage.manager_schedule_list_working_on_date(D, db_path=db)
            check("S15: empty R1 does not raise (fail-open contract preserved by main.py, untouched)", out == [])
        except Exception as exc:
            check("S15: empty R1 does not raise", False, repr(exc))

        # 19. resolver failure degrades, no exception, no silent True
        db = _new_db(str(tmpdir))
        setup_fixture(db)
        orig = storage._w3_c1_is_working
        storage._w3_c1_is_working = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected"))
        try:
            out = storage.manager_effective_is_working("m1", D, db_path=db)
            check("S19: resolver exception -> degrades, no raise, no silent True", out is False, f"got {out!r}")
        finally:
            storage._w3_c1_is_working = orig

        # 20. unparseable / empty business date
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", "2026-13-99", 1)])
        check("S20: R1 malformed date -> explicit-ON-only", storage.manager_schedule_list_working_on_date("2026-13-99", db_path=db) == ["m1"])
        check("S20: R1 empty date -> []", storage.manager_schedule_list_working_on_date("", db_path=db) == [])
        # R2 with NO matching config for the malformed date: weekday parsing fails ->
        # fail-closed False (a manager with an explicit row LITERALLY equal to the bad
        # date string is a different, legitimate case -- covered by the R1 check above).
        check("S20: R2 malformed date (no config) -> False",
              storage.manager_effective_is_working("m_no_config", "2026-13-99", db_path=db) is False)

        # 21. Kyiv-midnight boundary regression guard: caller-supplied date, no clock read
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", "2026-08-09", 1), ("m1", "2026-08-11", 0)])
        check("S21: D-1/D/D+1 resolved purely from the supplied string",
              storage.manager_effective_is_working("m1", "2026-08-09", db_path=db) is True and
              storage.manager_effective_is_working("m1", "2026-08-11", db_path=db) is False)

        # 22. DST transition dates
        db = _new_db(str(tmpdir))
        for dst_date in ("2026-03-29", "2026-10-25"):
            setup_fixture(db, explicit=[("mdst", dst_date, 1)])
            check(f"S22: DST date {dst_date} unaffected", storage.manager_effective_is_working("mdst", dst_date, db_path=db) is True)

        # 23. repeated identical calls -- idempotent, 0 rows written anywhere
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", D, 1)], links=[("m2", "s1")], sources=[("s1", ALL_ON, 1)])
        r1 = storage.manager_schedule_list_working_on_date(D, db_path=db)
        r2 = storage.manager_schedule_list_working_on_date(D, db_path=db)
        r3 = storage.manager_schedule_list_working_on_date(D, db_path=db)
        check("S23: repeated calls idempotent", r1 == r2 == r3)
        con = storage._bsl_connect(db)
        try:
            w3_rows = sum(con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ("w3_schedule_exception", "w3_manager_schedule_version",
                                    "w3_source_schedule_version", "w3_manager_source_history",
                                    "w3_schedule_audit"))
        finally:
            con.close()
        check("S23: 0 rows written to any w3_* table", w3_rows == 0, f"w3_rows={w3_rows}")

        # 24. pre-W3 data only -- fresh legacy DB, w3_* tables created, 0 rows after
        db = _new_db(str(tmpdir))
        storage.ensure_manager_schedule_tables(db)
        storage.ensure_source_work_schedule(db)
        con = storage._bsl_connect(db)
        try:
            con.execute("INSERT INTO manager_work_schedule_days(manager_key, work_date, is_working, "
                        "source, updated_at) VALUES(?,?,?,?,?)", ("legacy_only", D, 1, "test", ""))
            con.commit()
        finally:
            con.close()
        out = storage.manager_schedule_list_working_on_date(D, db_path=db)
        check("S24: pre-W3-only DB -> R1 still resolves", out == ["legacy_only"])
        con = storage._bsl_connect(db)
        try:
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            con.close()
        check("S24: w3_* tables created", set(storage._W3_C1_ENSURE_TABLES) <= names, f"missing={set(storage._W3_C1_ENSURE_TABLES) - names}")

        # 27. one candidate's _w3_c1_is_working raises -> degrades that candidate only
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[(f"m{i}", D, 1) for i in range(6)])
        target = "m3"
        orig = storage._w3_c1_is_working

        def _fail_one(con, mk, bd, _orig=orig, _target=target):
            if mk == _target:
                raise RuntimeError("injected single-candidate failure")
            return _orig(con, mk, bd)

        storage._w3_c1_is_working = _fail_one
        try:
            out = storage.manager_schedule_list_working_on_date(D, db_path=db)
            legacy_out = legacy_mod.manager_schedule_list_working_on_date(D, db_path=db)
            check("S27: one candidate failure -> list identical to legacy, others unaffected",
                  out == legacy_out == [f"m{i}" for i in range(6)], f"out={out}")
        finally:
            storage._w3_c1_is_working = orig

        # 28. candidate-set enumeration itself raises -> whole-function legacy fallback
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", D, 1)], links=[("m2", "s1")], sources=[("s1", ALL_ON, 1)])
        orig_ev = storage._w3_c1_ensure_validated
        storage._w3_c1_ensure_validated = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected enumeration failure"))
        connect_calls = []
        orig_connect = storage._bsl_connect

        def _counting_connect(*a, **k):
            connect_calls.append(1)
            return orig_connect(*a, **k)

        storage._bsl_connect = _counting_connect
        try:
            out = storage.manager_schedule_list_working_on_date(D, db_path=db)
            legacy_out = legacy_mod.manager_schedule_list_working_on_date(D, db_path=db)
            check("S28: candidate-query failure -> legacy fallback produces legacy list",
                  out == legacy_out, f"out={out} legacy={legacy_out}")
        finally:
            storage._w3_c1_ensure_validated = orig_ev
            storage._bsl_connect = orig_connect

        # 29. both resolver and row helper fail for one candidate -> excluded, rest kept
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[(f"m{i}", D, 1) for i in range(6)])
        target = "m4"
        orig_is_working = storage._w3_c1_is_working
        orig_row = storage._manager_effective_is_working_row

        def _fail_resolver(con, mk, bd, _orig=orig_is_working, _t=target):
            if mk == _t:
                raise RuntimeError("resolver fail")
            return _orig(con, mk, bd)

        def _fail_row(con, mk, wd, _orig=orig_row, _t=target):
            if mk == _t:
                raise RuntimeError("row fail")
            return _orig(con, mk, wd)

        storage._w3_c1_is_working = _fail_resolver
        storage._manager_effective_is_working_row = _fail_row
        try:
            out = storage.manager_schedule_list_working_on_date(D, db_path=db)
            expected = sorted(f"m{i}" for i in range(6) if i != 4)
            check("S29: double failure for one candidate -> excluded, rest kept, sorted",
                  out == expected, f"out={out}")
        finally:
            storage._w3_c1_is_working = orig_is_working
            storage._manager_effective_is_working_row = orig_row

        # 30. no exception escapes under any injected failure point
        injection_points = ["_w3_c1_ensure_validated", "_w3_c1_is_working", "_manager_effective_is_working_row"]
        no_escape = True
        for attr in injection_points:
            db = _new_db(str(tmpdir))
            setup_fixture(db, explicit=[("m1", D, 1)])
            orig_attr = getattr(storage, attr)
            setattr(storage, attr, lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"injected@{attr}")))
            try:
                storage.manager_effective_is_working("m1", D, db_path=db)
                storage.manager_schedule_list_working_on_date(D, db_path=db)
            except Exception as exc:
                no_escape = False
                print(f"        S30 leak at {attr}: {exc!r}")
            finally:
                setattr(storage, attr, orig_attr)
        check("S30: no exception escapes R1/R2 at any injection point", no_escape)

        # 31. DB replaced at the same path after the memo is warm
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("m1", D, 1)])
        storage.manager_effective_is_working("m1", D, db_path=db)  # warms the memo for `db`
        warm_key = storage._bsl_db_path(db)
        check("S31: memo warmed for this path", storage._W3_C1_ENSURE_MEMO.get(warm_key) is True)
        os.remove(db)
        # fresh legacy-only DB at the SAME path, missing all four W3.3 table groups
        storage.ensure_manager_schedule_tables(db)
        storage.ensure_source_work_schedule(db)
        con = storage._bsl_connect(db)
        try:
            con.execute("INSERT INTO manager_work_schedule_days(manager_key, work_date, is_working, "
                        "source, updated_at) VALUES(?,?,?,?,?)", ("m2", D, 1, "test", ""))
            con.commit()
        finally:
            con.close()
        out = storage.manager_effective_is_working("m2", D, db_path=db)
        check("S31: next call after replacement re-runs the four ensures and resolves correctly", out is True)
        con = storage._bsl_connect(db)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        finally:
            con.close()
        check("S31: the four tables exist in the replaced DB", set(storage._W3_C1_ENSURE_TABLES) <= names)

        db2 = _new_db(str(tmpdir))
        setup_fixture(db2, explicit=[("m3", D, 1)])
        storage.manager_effective_is_working("m3", D, db_path=db2)
        key2 = storage._bsl_db_path(db2)
        check("S31: a second, different path stays independently cached",
              key2 != warm_key and storage._W3_C1_ENSURE_MEMO.get(key2) is True)


def run_equivalence(tmpdir: Path) -> None:
    """_w3_c1_is_working must equal w3_resolve_schedule(...)["is_working_day"] over the
    whole matrix -- prevents a second, drifting implementation."""
    with _RealDbGuard():
        rng = random.Random(99)
        mismatches = 0
        total = 0
        for i in range(40):
            db = _new_db(str(tmpdir))
            mgrs = [f"em{i}_{j}" for j in range(4)]
            explicit = [(m, "2026-08-1" + str(j % 7), rng.choice([0, 1])) for j, m in enumerate(mgrs) if rng.random() < 0.5]
            links = [(m, f"es{i}") for m in mgrs if rng.random() < 0.5]
            sources = [(f"es{i}", rng.choice([ALL_ON, ALL_OFF]), rng.choice([0, 1]))] if links else []
            setup_fixture(db, explicit=explicit, links=links, sources=sources)
            con = storage._bsl_connect(db)
            try:
                storage._w3_c1_ensure_validated(con, db)
                for m in mgrs:
                    for wd in ("2026-08-10", "2026-08-11", "2026-08-16"):
                        total += 1
                        c1_val, _tier, _prov = storage._w3_c1_is_working(con, m, wd)
                        full = storage.w3_resolve_schedule(m, wd, db_path=db, _con=con)
                        if bool(c1_val) != bool(full["is_working_day"]):
                            mismatches += 1
            finally:
                con.close()
        check("EQUIV: _w3_c1_is_working == w3_resolve_schedule(...).is_working_day over the matrix",
              mismatches == 0, f"{mismatches}/{total} mismatches")


def run_parity_2000(tmpdir: Path, legacy_mod) -> None:
    """>=2000 generated cases: link/no-link x source-enabled/disabled x explicit
    ON/OFF/none x 7 weekdays x valid/invalid date, R2 new vs the real pre-edit body."""
    weekdays = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06",
                "2026-08-07", "2026-08-08", "2026-08-09"]
    bad_dates = ["", "2026-13-40", "not-a-date"]
    mismatches = 0
    total = 0
    with _RealDbGuard():
        rng = random.Random(2000)
        case_id = 0
        for trial in range(7):
            for has_link in (False, True):
                for src_enabled in (0, 1):
                    for explicit_mode in ("on", "off", "none"):
                        for wd in weekdays:
                            for bad in (False, True):
                                case_id += 1
                                db = _new_db(str(tmpdir))
                                mk = f"gm{case_id}"
                                explicit = []
                                if explicit_mode == "on":
                                    explicit = [(mk, wd, 1)]
                                elif explicit_mode == "off":
                                    explicit = [(mk, wd, 0)]
                                links = [(mk, "gs1")] if has_link else []
                                sources = [("gs1", ALL_ON, src_enabled)] if has_link else []
                                setup_fixture(db, explicit=explicit, links=links, sources=sources)
                                date_to_use = rng.choice(bad_dates) if bad else wd
                                total += 1
                                legacy_out = legacy_mod.manager_effective_is_working(mk, date_to_use, db_path=db)
                                new_out = storage.manager_effective_is_working(mk, date_to_use, db_path=db)
                                if legacy_out != new_out:
                                    mismatches += 1
                                # also drive R1 parity for the same fixture/date (adds
                                # coverage without extra fixtures)
                                total += 1
                                legacy_r1 = legacy_mod.manager_schedule_list_working_on_date(date_to_use, db_path=db)
                                new_r1 = storage.manager_schedule_list_working_on_date(date_to_use, db_path=db)
                                if legacy_r1 != new_r1:
                                    mismatches += 1
    check(f"PARITY: >=2000 generated cases, sum(mismatch)==0 (total={total})",
          total >= 2000 and mismatches == 0, f"total={total} mismatches={mismatches}")


# ---------------------------------------------------------------------------
# Preflight (R1) consumer integration -- direct import, real functions
# ---------------------------------------------------------------------------

def run_preflight_integration(tmpdir: Path, legacy_mod) -> None:
    try:
        import preflight_check
    except Exception as exc:
        check("PREFLIGHT: module imports standalone", False, repr(exc))
        return
    check("PREFLIGHT: module imports standalone", True)

    with _RealDbGuard():
        db = _new_db(str(tmpdir))
        setup_fixture(db, explicit=[("pm1", "2026-08-10", 1)], links=[("pm2", "ps1")],
                      sources=[("ps1", ALL_ON, 1)])

        def _swap(target_mod_working_fn):
            storage.manager_schedule_list_working_on_date = target_mod_working_fn

        orig_new = storage.manager_schedule_list_working_on_date
        try:
            _swap(legacy_mod.manager_schedule_list_working_on_date)
            report_legacy = preflight_check.build_report("2026-08-10", db_path=db)
            deep_legacy = preflight_check._relevant_managers_for_deep_verify("2026-08-10", db)
            sources_legacy = preflight_check.relevant_source_keys_for_date("2026-08-10", db_path=db)
            partner_legacy = preflight_check.build_partner_report("2026-08-10", db_path=db, source_key="ps1")

            _swap(orig_new)
            report_new = preflight_check.build_report("2026-08-10", db_path=db)
            deep_new = preflight_check._relevant_managers_for_deep_verify("2026-08-10", db)
            sources_new = preflight_check.relevant_source_keys_for_date("2026-08-10", db_path=db)
            partner_new = preflight_check.build_partner_report("2026-08-10", db_path=db, source_key="ps1")

            check("PREFLIGHT: build_report working_count/managers identical",
                  report_legacy.get("working_count") == report_new.get("working_count")
                  and [m.get("manager_key") for m in report_legacy.get("managers", [])]
                  == [m.get("manager_key") for m in report_new.get("managers", [])])
            check("PREFLIGHT: _relevant_managers_for_deep_verify identical", deep_legacy == deep_new)
            check("PREFLIGHT: relevant_source_keys_for_date identical", sources_legacy == sources_new)
            check("PREFLIGHT: build_partner_report working_count identical",
                  partner_legacy.get("working_count") == partner_new.get("working_count"))

            # empty-day "0 managers today" branch preserved
            db_empty = _new_db(str(tmpdir))
            setup_fixture(db_empty)
            _swap(legacy_mod.manager_schedule_list_working_on_date)
            rep_legacy_empty = preflight_check.build_report("2026-08-10", db_path=db_empty)
            _swap(orig_new)
            rep_new_empty = preflight_check.build_report("2026-08-10", db_path=db_empty)
            check("PREFLIGHT: empty-day report branch identical (0 managers today)",
                  rep_legacy_empty.get("working_count") == rep_new_empty.get("working_count") == 0)
        finally:
            storage.manager_schedule_list_working_on_date = orig_new


# ---------------------------------------------------------------------------
# Consumer file byte-identity (proxy for "no consumer file edited for C1")
# ---------------------------------------------------------------------------

def run_consumer_byte_identity() -> None:
    import hashlib
    baseline_path = Path(r"C:\ALM_TPilot_AUDIT\20260803\W3_3_C1_READERS_IMPLEMENTATION\w3_3_baseline_hashes.json")
    if not baseline_path.exists():
        check("CONSUMERS: pre-edit baseline hashes present", False, str(baseline_path))
        return
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    unchanged = ("main.py", "panel_bot.py", "manager_bot.py", "partner_stat_bot.py",
                 "preflight_check.py", "stats_engine.py", "panel_bridge.py", "manager_registry.py")
    for fname in unchanged:
        live = BASE_DIR / fname
        h = hashlib.sha256(live.read_bytes()).hexdigest()
        expected = baseline["runtime_12"].get(fname)
        check(f"CONSUMERS: {fname} byte-identical to pre-edit", h == expected, f"{h} vs {expected}")
    live_storage_hash = hashlib.sha256(STORAGE_PATH.read_bytes()).hexdigest()
    check("CONSUMERS: storage.py DID change from pre-edit (sanity)",
          live_storage_hash != baseline["runtime_12"].get("storage.py"))


# ---------------------------------------------------------------------------
# Mutation legs M33-1 .. M33-16
# ---------------------------------------------------------------------------

def _mutate_unique(src: str, old: str, new: str) -> str:
    n = src.count(old)
    if n != 1:
        raise RuntimeError(f"mutation anchor not unique (n={n}): {old[:80]!r}")
    return src.replace(old, new, 1)


def _fresh_mutant(tmpdir: Path, mutate_fn=None):
    import time
    tmpdir.mkdir(parents=True, exist_ok=True)
    text = STORAGE_PATH.read_text(encoding="utf-8-sig")
    if mutate_fn is not None:
        text = mutate_fn(text)
    dest = tmpdir / f"storage_mut_{uuid.uuid4().hex[:8]}.py"
    last_exc = None
    for attempt in range(5):
        try:
            dest.write_text(text, encoding="utf-8")
            last_exc = None
            break
        except PermissionError as exc:
            last_exc = exc
            time.sleep(0.2 * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    mod_name = f"storage_mut_{uuid.uuid4().hex[:8]}"
    last_exc = None
    for attempt in range(5):
        try:
            spec = importlib.util.spec_from_file_location(mod_name, dest)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            spec.loader.exec_module(mod)
            return mod
        except PermissionError as exc:
            last_exc = exc
            sys.modules.pop(mod_name, None)
            time.sleep(0.2 * (attempt + 1))
    raise last_exc


def run_mutation(work: Path, mutation_id: str, description: str, expected: str,
                  mutate_fn, assert_fn) -> None:
    """green(clean1) -> red(mutant) -> green(clean2) control sequence."""
    try:
        clean1 = _fresh_mutant(work / f"{mutation_id}_neg")
        red1, d1 = assert_fn(clean1)
    except Exception as exc:
        check(f"{mutation_id} (neg control): {description}", False, f"NEG_ERROR {exc!r}")
        return
    if red1:
        check(f"{mutation_id} (neg control): clean copy is GREEN", False, f"clean copy already RED: {d1}")
        return

    try:
        mutant = _fresh_mutant(work / f"{mutation_id}_mut", mutate_fn)
    except Exception as exc:
        check(f"{mutation_id}: {description}", False, f"MUT_SETUP_FAIL {exc!r}")
        return
    try:
        red2, d2 = assert_fn(mutant)
    except Exception as exc:
        check(f"{mutation_id}: {description}", False, f"MUT_ERROR {exc!r}")
        return
    if not red2:
        check(f"{mutation_id}: {description}", False, f"mutation did not turn RED: {d2}")
        return

    try:
        clean2 = _fresh_mutant(work / f"{mutation_id}_restore")
        red3, d3 = assert_fn(clean2)
    except Exception as exc:
        check(f"{mutation_id} (restore control): {description}", False, f"RESTORE_ERROR {exc!r}")
        return
    if red3:
        check(f"{mutation_id} (restore control): {description}", False, f"restore copy still RED: {d3}")
        return

    check(f"{mutation_id}: {description} [expected: {expected}]", True)


def run_all_mutations(work: Path) -> None:
    D = "2026-08-10"

    def fixture(mod, tmpdir, **kw):
        db = _new_db(str(tmpdir))
        setup_fixture(db, **kw)
        return db

    # M33-1: R2 body reverted to legacy-only
    def m1_mutate(src):
        old = ('        try:\n'
               '            _w3_c1_ensure_validated(con, db_path)\n'
               '            is_working, _tier, _prov = _w3_c1_is_working(con, mk, wd)\n'
               '            return bool(is_working)\n'
               '        except Exception:\n'
               '            return _manager_effective_is_working_row(con, mk, wd)\n')
        new = '        return _manager_effective_is_working_row(con, mk, wd)\n'
        return _mutate_unique(src, old, new)

    def m1_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td)  # no config -> legacy False
                mod._w3_resolve_work = lambda con, mk, sk, bd, prov: (True, "forced", None)
                out = mod.manager_effective_is_working("nobody", D, db_path=db)
                # clean: resolver forced-True is used -> out True (RED means mutation removed the call)
                is_red = (out is not True)
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-1", "R2 body reverted to legacy-only",
                 "migration silently not applied", m1_mutate, m1_assert)

    # M33-2: tier order effectively swapped (explicit-day tier neutralized so
    # source-weekly runs unopposed) -> explicit OFF stops winning
    def m2_mutate(src):
        old = ('    if row is not None:\n'
               '        provenance.append({"dimension": "work", "tier": "legacy_manager_day",\n'
               '                            "table": "manager_work_schedule_days", "row_id": None,\n'
               '                            "effective_from": business_date, "effective_to": None, "decided": True})\n'
               '        return int(row[0] or 0) == 1, "legacy_manager_day", None\n')
        new = ('    if False and row is not None:\n'
               '        provenance.append({"dimension": "work", "tier": "legacy_manager_day",\n'
               '                            "table": "manager_work_schedule_days", "row_id": None,\n'
               '                            "effective_from": business_date, "effective_to": None, "decided": True})\n'
               '        return int(row[0] or 0) == 1, "legacy_manager_day", None\n')
        return _mutate_unique(src, old, new)

    def m2_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, explicit=[("m1", D, 0)], links=[("m1", "s1")],
                             sources=[("s1", ALL_ON, 1)])
                out = mod.manager_effective_is_working("m1", D, db_path=db)
                is_red = (out is not False)  # explicit OFF should still win on clean control
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-2", "tier order swapped (source weekly before explicit day)",
                 "explicit OFF stops winning", m2_mutate, m2_assert)

    # M33-3: fail-closed flipped to True
    def m3_mutate(src):
        old = ('    provenance.append({"dimension": "work", "tier": "fail_closed", "table": "", "row_id": None,\n'
               '                        "effective_from": business_date, "effective_to": None, "decided": True})\n'
               '    return False, "none", None\n')
        new = ('    provenance.append({"dimension": "work", "tier": "fail_closed", "table": "", "row_id": None,\n'
               '                        "effective_from": business_date, "effective_to": None, "decided": True})\n'
               '    return True, "none", None\n')
        return _mutate_unique(src, old, new)

    def m3_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td)  # absent link -> should be False
                out = mod.manager_effective_is_working("nobody", D, db_path=db)
                is_red = (out is not False)
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-3", "fail-closed flipped to True",
                 "absent link starts a manager's day", m3_mutate, m3_assert)

    # M33-4: degrade wrapper removed (exception propagates)
    def m4_mutate(src):
        old = ('        try:\n'
               '            _w3_c1_ensure_validated(con, db_path)\n'
               '            is_working, _tier, _prov = _w3_c1_is_working(con, mk, wd)\n'
               '            return bool(is_working)\n'
               '        except Exception:\n'
               '            return _manager_effective_is_working_row(con, mk, wd)\n')
        new = ('        _w3_c1_ensure_validated(con, db_path)\n'
               '        is_working, _tier, _prov = _w3_c1_is_working(con, mk, wd)\n'
               '        return bool(is_working)\n')
        return _mutate_unique(src, old, new)

    def m4_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, explicit=[("m1", D, 1)])
                mod._w3_c1_is_working = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected"))
                try:
                    mod.manager_effective_is_working("m1", D, db_path=db)
                    return False, "no exception (expected escape on mutant)"
                except RuntimeError:
                    return True, "exception escaped"

    run_mutation(work, "M33-4", "degrade wrapper removed",
                 "resolver failure becomes an outage in 5 processes", m4_mutate, m4_assert)

    # M33-5: degrade wrapper swallows into True
    def m5_mutate(src):
        old = ('        except Exception:\n'
               '            return _manager_effective_is_working_row(con, mk, wd)\n'
               '    finally:\n'
               '        con.close()')
        new = ('        except Exception:\n'
               '            return True\n'
               '    finally:\n'
               '        con.close()')
        return _mutate_unique(src, old, new)

    def m5_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td)  # no config -> legacy False
                mod._w3_c1_is_working = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected"))
                out = mod.manager_effective_is_working("nobody", D, db_path=db)
                is_red = (out is not False)
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-5", "degrade wrapper swallows into True",
                 "fail-open masquerading as resilience", m5_mutate, m5_assert)

    # M33-6: R1 candidate set drops the inheritance leg
    def m6_mutate(src):
        old = ('        candidate_rows = con.execute(\n'
               '            "SELECT manager_key FROM manager_work_schedule_days WHERE work_date=? "\n'
               '            "UNION "\n'
               '            "SELECT manager_key FROM manager_source_links WHERE COALESCE(source_key,\'\') <> \'\'",\n'
               '            (wd,),\n'
               '        ).fetchall()\n')
        new = ('        candidate_rows = con.execute(\n'
               '            "SELECT manager_key FROM manager_work_schedule_days WHERE work_date=?",\n'
               '            (wd,),\n'
               '        ).fetchall()\n')
        return _mutate_unique(src, old, new)

    def m6_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, links=[("m1", "s1")], sources=[("s1", ALL_ON, 1)])
                out = mod.manager_schedule_list_working_on_date(D, db_path=db)
                is_red = ("m1" not in out)
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-6", "R1 candidate set drops the inheritance leg",
                 "inherited managers vanish from 16:50/14:00/preflight", m6_mutate, m6_assert)

    # M33-7: R1 candidate set drops the explicit leg
    def m7_mutate(src):
        old = ('        candidate_rows = con.execute(\n'
               '            "SELECT manager_key FROM manager_work_schedule_days WHERE work_date=? "\n'
               '            "UNION "\n'
               '            "SELECT manager_key FROM manager_source_links WHERE COALESCE(source_key,\'\') <> \'\'",\n'
               '            (wd,),\n'
               '        ).fetchall()\n')
        new = ('        candidate_rows = con.execute(\n'
               '            "SELECT manager_key FROM manager_source_links WHERE COALESCE(source_key,\'\') <> \'\'"\n'
               '        ).fetchall()\n')
        return _mutate_unique(src, old, new)

    def m7_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, explicit=[("m1", D, 1)])
                out = mod.manager_schedule_list_working_on_date(D, db_path=db)
                is_red = ("m1" not in out)
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-7", "R1 candidate set drops the explicit leg",
                 "explicit-only managers vanish", m7_mutate, m7_assert)

    # M33-8: bad-date branch delegated instead of short-circuited
    def m8_mutate(src):
        old = ('    try:\n'
               '        datetime.strptime(wd, "%Y-%m-%d")\n'
               '    except Exception:\n'
               '        return _c1_legacy_list_working_on_date(wd, db_path)\n'
               '\n'
               '    con = None\n')
        new = ('    try:\n'
               '        datetime.strptime(wd, "%Y-%m-%d")\n'
               '    except Exception:\n'
               '        return []\n'
               '\n'
               '    con = None\n')
        return _mutate_unique(src, old, new)

    def m8_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                bad = "2026-13-40"
                db = fixture(mod, td, explicit=[("m1", bad, 1)])
                out = mod.manager_schedule_list_working_on_date(bad, db_path=db)
                is_red = (out == [])  # clean control should still return ["m1"] (explicit-ON-only)
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-8", "bad-date branch delegated instead of short-circuited",
                 "R1 silently returns [] on a malformed date", m8_mutate, m8_assert)

    # M33-9: sorted() dropped
    def m9_mutate(src):
        old = ('            if ok:\n'
               '                result.add(mk)\n'
               '        return sorted(result)\n'
               '    except Exception:\n'
               '        return _c1_legacy_list_working_on_date(wd, db_path)\n')
        new = ('            if ok:\n'
               '                result.add(mk)\n'
               '        return list(result)\n'
               '    except Exception:\n'
               '        return _c1_legacy_list_working_on_date(wd, db_path)\n')
        return _mutate_unique(src, old, new)

    def m9_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                mgrs = [f"z{i:02d}" for i in range(15)]
                random.Random(7).shuffle(mgrs)
                db = fixture(mod, td, explicit=[(m, D, 1) for m in mgrs])
                out = mod.manager_schedule_list_working_on_date(D, db_path=db)
                is_red = (out != sorted(out))
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-9", "sorted() dropped",
                 "non-deterministic report ordering", m9_mutate, m9_assert)

    # M33-10: memo made global instead of per resolved db_path (white-box key check)
    def m10_mutate(src):
        old = '    key = _bsl_db_path(db_path)\n'
        new = '    key = "GLOBAL"\n'
        return _mutate_unique(src, old, new)

    def m10_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db_a = fixture(mod, td, explicit=[("m1", D, 1)])
                db_b = fixture(mod, td, explicit=[("m2", D, 1)])
                mod.manager_effective_is_working("m1", D, db_path=db_a)
                mod.manager_effective_is_working("m2", D, db_path=db_b)
                is_red = (len(mod._W3_C1_ENSURE_MEMO) != 2)
                return is_red, f"memo_keys={list(mod._W3_C1_ENSURE_MEMO.keys())}"

    run_mutation(work, "M33-10", "memo made global instead of per resolved db_path",
                 "a second database silently misses its w3_* tables", m10_mutate, m10_assert)

    # M33-11: per-candidate except INCLUDES the manager anyway
    def m11_mutate(src):
        old = ('            try:\n'
               '                ok, _tier, _prov = _w3_c1_is_working(con, mk, wd)\n'
               '            except Exception:\n'
               '                try:\n'
               '                    ok = _manager_effective_is_working_row(con, mk, wd)\n'
               '                except Exception:\n'
               '                    ok = False\n')
        new = ('            try:\n'
               '                ok, _tier, _prov = _w3_c1_is_working(con, mk, wd)\n'
               '            except Exception:\n'
               '                ok = True\n')
        return _mutate_unique(src, old, new)

    def m11_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td)  # candidate present only via source link, no config -> False expected
                setup_fixture(db, links=[("m1", "s1")], sources=[("s1", ALL_OFF, 1)])
                orig = mod._w3_c1_is_working
                mod._w3_c1_is_working = lambda con, mk, wd, _o=orig: (_ for _ in ()).throw(RuntimeError("x")) if mk == "m1" else _o(con, mk, wd)
                out = mod.manager_schedule_list_working_on_date(D, db_path=db)
                is_red = ("m1" in out)
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-11", "per-candidate except includes the manager anyway",
                 "an exception silently starting someone's working day", m11_mutate, m11_assert)

    # M33-12: per-candidate except returns [] (truncates whole list)
    def m12_mutate(src):
        old = ('            try:\n'
               '                ok, _tier, _prov = _w3_c1_is_working(con, mk, wd)\n'
               '            except Exception:\n'
               '                try:\n'
               '                    ok = _manager_effective_is_working_row(con, mk, wd)\n'
               '                except Exception:\n'
               '                    ok = False\n')
        new = ('            try:\n'
               '                ok, _tier, _prov = _w3_c1_is_working(con, mk, wd)\n'
               '            except Exception:\n'
               '                return []\n')
        return _mutate_unique(src, old, new)

    def m12_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, explicit=[("m1", D, 1), ("m2", D, 1)])
                orig = mod._w3_c1_is_working
                mod._w3_c1_is_working = lambda con, mk, wd, _o=orig: (_ for _ in ()).throw(RuntimeError("x")) if mk == "m1" else _o(con, mk, wd)
                out = mod.manager_schedule_list_working_on_date(D, db_path=db)
                is_red = (out == [])
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-12", "per-candidate except break/return []s",
                 "one failing candidate emptying the whole working-manager list", m12_mutate, m12_assert)

    # M33-13: whole-function fallback replaced by [] instead of legacy body
    def m13_mutate(src):
        old = '        return _c1_legacy_list_working_on_date(wd, db_path)\n    finally:\n        if con is not None:'
        new = '        return []\n    finally:\n        if con is not None:'
        return _mutate_unique(src, old, new)

    def m13_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, explicit=[("m1", D, 1)])
                mod._w3_c1_ensure_validated = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
                out = mod.manager_schedule_list_working_on_date(D, db_path=db)
                is_red = (out == [])  # clean control should still resolve ["m1"] via legacy fallback
                return is_red, f"out={out!r}"

    run_mutation(work, "M33-13", "whole-function fallback replaced by return [] instead of legacy",
                 "candidate-query failure silently reporting nobody works today", m13_mutate, m13_assert)

    # M33-14: per-candidate fallback opens its own connection
    def m14_mutate(src):
        old = '                    ok = _manager_effective_is_working_row(con, mk, wd)\n'
        new = '                    ok = _manager_effective_is_working_row(_bsl_connect(db_path), mk, wd)\n'
        return _mutate_unique(src, old, new)

    def m14_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                # Separate DB files per run: the mutation under test intentionally leaks
                # a connection on the failure path, and reusing one SQLite file across
                # both runs risks a transient Windows file-lock false failure from that
                # leak rather than from the mutation's real, intended effect.
                def _count_connects(inject_failure):
                    db = fixture(mod, td, explicit=[("m1", D, 1)])
                    mod.manager_schedule_list_working_on_date(D, db_path=db)  # warm the memo
                    calls = []
                    orig_connect = mod._bsl_connect
                    orig_is_working = mod._w3_c1_is_working

                    def counting(*a, **k):
                        calls.append(1)
                        return orig_connect(*a, **k)

                    def maybe_failing(con, mk, wd, _o=orig_is_working):
                        if inject_failure and mk == "m1":
                            raise RuntimeError("injected")
                        return _o(con, mk, wd)

                    mod._bsl_connect = counting
                    mod._w3_c1_is_working = maybe_failing
                    try:
                        mod.manager_schedule_list_working_on_date(D, db_path=db)
                    finally:
                        mod._bsl_connect = orig_connect
                        mod._w3_c1_is_working = orig_is_working
                    return len(calls)

                baseline = _count_connects(False)
                with_failure = _count_connects(True)
                # On the mutant, the failure path leaks a connection with no Python
                # reference left -- force it closed via GC before this function's
                # tempdir is torn down, or Windows refuses to delete the locked file.
                import gc
                gc.collect()
                # clean control: the per-candidate fallback reuses the shared connection,
                # so injecting a failure adds no connections; the mutant opens one more.
                is_red = with_failure > baseline
                return is_red, f"baseline={baseline} with_failure={with_failure}"

    run_mutation(work, "M33-14", "per-candidate fallback opens its own connection",
                 "reintroducing per-manager connections in the degraded path", m14_mutate, m14_assert)

    # M33-15: memo validation step deleted (cached entry trusted blindly)
    def m15_mutate(src):
        old = ('    if _W3_C1_ENSURE_MEMO.get(key):\n'
               '        try:\n'
               '            if _present_count() == len(_W3_C1_ENSURE_TABLES):\n'
               '                return\n'
               '        except Exception:\n'
               '            pass\n'
               '        _W3_C1_ENSURE_MEMO.pop(key, None)\n')
        new = ('    if _W3_C1_ENSURE_MEMO.get(key):\n'
               '        return\n')
        return _mutate_unique(src, old, new)

    def m15_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, explicit=[("m1", D, 1)])
                mod.manager_effective_is_working("m1", D, db_path=db)  # warms memo
                os.remove(db)
                mod.ensure_manager_schedule_tables(db)
                mod.ensure_source_work_schedule(db)
                con = mod._bsl_connect(db)
                try:
                    con.execute("INSERT INTO manager_work_schedule_days(manager_key, work_date, "
                                "is_working, source, updated_at) VALUES(?,?,?,?,?)", ("m2", D, 1, "t", ""))
                    con.commit()
                finally:
                    con.close()
                mod.manager_effective_is_working("m2", D, db_path=db)
                con = mod._bsl_connect(db)
                try:
                    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                finally:
                    con.close()
                is_red = not (set(mod._W3_C1_ENSURE_TABLES) <= names)
                return is_red, f"missing={set(mod._W3_C1_ENSURE_TABLES) - names}"

    run_mutation(work, "M33-15", "memo validation step deleted (cached entry trusted blindly)",
                 "a replaced/restored DB file served from stale memo state", m15_mutate, m15_assert)

    # M33-16: memo validation failure treated as "valid"
    def m16_mutate(src):
        old = ('        try:\n'
               '            if _present_count() == len(_W3_C1_ENSURE_TABLES):\n'
               '                return\n'
               '        except Exception:\n'
               '            pass\n'
               '        _W3_C1_ENSURE_MEMO.pop(key, None)\n')
        new = ('        try:\n'
               '            if _present_count() == len(_W3_C1_ENSURE_TABLES):\n'
               '                return\n'
               '        except Exception:\n'
               '            return\n'
               '        _W3_C1_ENSURE_MEMO.pop(key, None)\n')
        return _mutate_unique(src, old, new)

    def m16_assert(mod):
        with tempfile.TemporaryDirectory() as td:
            with _RealDbGuard():
                db = fixture(mod, td, explicit=[("m1", D, 1)])
                con = mod._bsl_connect(db)
                try:
                    mod._w3_c1_ensure_validated(con, db)  # warms memo
                finally:
                    con.close()
                ensure_calls = []
                orig_ensure = mod.ensure_source_work_windows

                def counting_ensure(*a, **k):
                    ensure_calls.append(1)
                    return orig_ensure(*a, **k)

                mod.ensure_source_work_windows = counting_ensure
                con2 = mod._bsl_connect(db)
                call_state = {"n": 0}

                # sqlite3.Connection.execute is a read-only C-level attribute -- it
                # cannot be monkeypatched on the instance. Use a thin forwarding proxy
                # instead (the ensure-validated function only ever calls .execute on
                # the object it is given).
                class _ExecuteInjector:
                    def execute(self_inner, sql, *a, **k):
                        if "sqlite_master" in sql and call_state["n"] == 0:
                            call_state["n"] += 1
                            raise sqlite3.OperationalError("injected validation failure")
                        return con2.execute(sql, *a, **k)

                    def __getattr__(self_inner, name):
                        return getattr(con2, name)

                proxy = _ExecuteInjector()
                try:
                    mod._w3_c1_ensure_validated(proxy, db)
                finally:
                    con2.close()
                    mod.ensure_source_work_windows = orig_ensure
                is_red = (len(ensure_calls) == 0)  # clean control re-runs ensures after a validation failure
                return is_red, f"ensure_calls={len(ensure_calls)}"

    run_mutation(work, "M33-16", "memo validation failure treated as valid",
                 "fail-open on a partially-initialized database", m16_mutate, m16_assert)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="w33_parity_") as td:
        tmpdir = Path(td)
        print("== P1-P4 legacy R1 preservation ==")
        run_p1_p4(tmpdir)

        backup_path = find_backup()
        legacy_mod, _ = load_module_copy(backup_path, tmpdir, "storage_scn_legacy")

        print("== S6 scenario matrix (1-24, 27-31) ==")
        run_scenarios(tmpdir, legacy_mod)

        print("== Equivalence vs full resolver ==")
        run_equivalence(tmpdir)

        print("== >=2000 generated parity cases ==")
        run_parity_2000(tmpdir, legacy_mod)

        print("== Preflight (R1) consumer integration ==")
        run_preflight_integration(tmpdir, legacy_mod)

        print("== Consumer file byte-identity ==")
        run_consumer_byte_identity()

        print("== Mutation legs M33-1..M33-16 ==")
        work = tmpdir / "mutations"
        work.mkdir(parents=True, exist_ok=True)
        run_all_mutations(work)

    print()
    print(f"TOTAL: {len(RESULTS)}  FAILED: {len(FAILURES)}")
    if FAILURES:
        print("FAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
