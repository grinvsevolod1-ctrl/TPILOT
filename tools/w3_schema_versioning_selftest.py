# -*- coding: utf-8 -*-
"""tools/w3_schema_versioning_selftest.py -- offline self-test for the W3.1 additive
schedule-versioning schema (ensure_w3_schedule_versioning) and writer-helper foundation
(w3_source_version_set / w3_manager_version_set / w3_source_history_record /
w3_exception_add / w3_exception_revoke / w3_message_schedule_write).

Uses throwaway temporary SQLite files only. See w3_resolver_foundation_selftest.py for
the real-DB guard convention re-used here.

    python tools\\w3_schema_versioning_selftest.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

FAILURES: list[str] = []
_REAL_DB_MARKERS = ("data_tpilot.db", os.path.join("ALM_TPilot", "db"))


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


class _RealDbGuard:
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


def _tables(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def _indexes(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def _triggers(con):
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()}


def run_all_checks(tmpdir: str) -> None:
    db = os.path.join(tmpdir, "schema.db")

    # schema creation twice succeeds
    storage.ensure_w3_schedule_versioning(db)
    storage.ensure_w3_schedule_versioning(db)
    check("ensure_w3_schedule_versioning runs twice without error", True)

    con = sqlite3.connect(db)
    tbls = _tables(con)
    for t in ("w3_source_schedule_version", "w3_manager_schedule_version",
              "w3_manager_source_history", "w3_schedule_exception", "w3_schedule_audit"):
        check(f"table {t} exists", t in tbls, str(tbls))

    idxs = _indexes(con)
    for i in ("w3_ssv_lookup_idx", "w3_ssv_one_open_idx", "w3_ssv_from_uniq_idx",
              "w3_msv_lookup_idx", "w3_msv_one_open_idx", "w3_msv_from_uniq_idx",
              "w3_msh_lookup_idx", "w3_msh_one_open_idx", "w3_msh_from_uniq_idx",
              "w3_exc_lookup_idx", "w3_audit_entity_idx"):
        check(f"index {i} exists", i in idxs, str(idxs))

    trgs = _triggers(con)
    for tg in ("w3_ssv_no_overlap_ins", "w3_ssv_no_overlap_upd", "w3_msv_no_overlap_ins",
               "w3_msv_no_overlap_upd", "w3_msh_no_overlap_ins", "w3_msh_no_overlap_upd"):
        check(f"trigger {tg} exists", tg in trgs, str(trgs))
    con.close()

    # unique-index layer: overlapping RAW insert aborts (bypassing the writer helper on
    # purpose, to prove layer 1+2 hold even against a careless direct insert)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO w3_source_schedule_version(source_key, dimension, effective_from, effective_to,"
        " created_at) VALUES('srcX','work','2026-01-01',NULL,'')"
    )
    con.commit()
    try:
        con.execute(
            "INSERT INTO w3_source_schedule_version(source_key, dimension, effective_from, effective_to,"
            " created_at) VALUES('srcX','work','2026-02-01',NULL,'')"
        )
        con.commit()
        check("overlapping raw insert (second open version) aborts", False, "no exception raised")
    except sqlite3.Error as exc:
        check("overlapping raw insert (second open version) aborts", True, str(exc))
        con.rollback()
    # same effective_from for the same (key, dimension) also aborts (from-uniq index)
    try:
        con.execute(
            "UPDATE w3_source_schedule_version SET effective_to='2026-06-01' WHERE source_key='srcX'"
        )
        con.execute(
            "INSERT INTO w3_source_schedule_version(source_key, dimension, effective_from, effective_to,"
            " created_at) VALUES('srcX','work','2026-01-01',NULL,'')"
        )
        con.commit()
        check("duplicate effective_from for same (key,dimension) aborts", False, "no exception raised")
    except sqlite3.Error as exc:
        check("duplicate effective_from for same (key,dimension) aborts", True, str(exc))
        con.rollback()
    con.close()

    # close-then-insert via the real writer succeeds and supersedes cleanly
    cfg1 = {"mon": 1, "tue": 1, "wed": 1, "thu": 1, "fri": 1, "sat": 0, "sun": 0}
    ok1, res1 = storage.w3_source_version_set("srcY", "work", cfg1, effective_from="2026-01-01",
                                               actor_user_id=7, actor_role="admin", db_path=db)
    check("w3_source_version_set first insert succeeds", ok1 is True, str(res1))
    cfg2 = dict(cfg1, sat=1)
    ok2, res2 = storage.w3_source_version_set("srcY", "work", cfg2, effective_from="2026-02-01",
                                               actor_user_id=7, actor_role="admin", db_path=db)
    check("w3_source_version_set close-then-insert succeeds", ok2 is True, str(res2))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    old_row = con.execute("SELECT * FROM w3_source_schedule_version WHERE id=?", (res1["id"],)).fetchone()
    check("closed row has effective_to == new effective_from",
          old_row["effective_to"] == "2026-02-01", str(dict(old_row)))
    check("closed row close_reason == 'superseded'", old_row["close_reason"] == "superseded")
    check("closed row superseded_by_id == new id", old_row["superseded_by_id"] == res2["id"])
    con.close()

    # unchanged payload creates no new version (idempotency)
    audit_before = sqlite3.connect(db).execute("SELECT COUNT(*) FROM w3_schedule_audit").fetchone()[0]
    ok3, res3 = storage.w3_source_version_set("srcY", "work", cfg2, effective_from="2026-02-01",
                                               actor_user_id=7, actor_role="admin", db_path=db)
    audit_after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM w3_schedule_audit").fetchone()[0]
    check("unchanged payload -> changed=False", ok3 is False, str(res3))
    check("unchanged payload -> no new audit row", audit_after == audit_before, f"{audit_before} -> {audit_after}")
    count_versions = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM w3_source_schedule_version WHERE source_key='srcY'"
    ).fetchone()[0]
    check("unchanged payload -> no new version row", count_versions == 2, str(count_versions))

    # audit row created exactly once for a real change
    audit_before2 = sqlite3.connect(db).execute("SELECT COUNT(*) FROM w3_schedule_audit").fetchone()[0]
    cfg3 = dict(cfg2, sun=1)
    storage.w3_source_version_set("srcY", "work", cfg3, effective_from="2026-03-01",
                                   actor_user_id=7, actor_role="admin", db_path=db)
    audit_after2 = sqlite3.connect(db).execute("SELECT COUNT(*) FROM w3_schedule_audit").fetchone()[0]
    check("real change -> exactly one new audit row", audit_after2 - audit_before2 == 1,
          f"{audit_before2} -> {audit_after2}")

    # atomic rollback: force a version-write failure inside w3_message_schedule_write
    # (CHECK effective_to > effective_from fails when closing an open version to an
    # earlier date) and prove the legacy dual-write did NOT happen either
    db_rb = os.path.join(tmpdir, "rollback.db")
    storage.w3_manager_version_set("mgrRB", "message",
                                    {"day_start_min": 480, "day_end_min": 1020,
                                     "night_start_min": 1020, "night_end_min": 480},
                                    effective_from="2026-06-01", db_path=db_rb)
    legacy_count_before = 0
    try:
        con = sqlite3.connect(db_rb)
        storage._w3_ensure_manager_client_message_schedule(con)
        con.commit()
        legacy_count_before = con.execute(
            "SELECT COUNT(*) FROM manager_client_message_schedule WHERE manager_key='mgrRB'"
        ).fetchone()[0]
        con.close()
    except Exception:
        pass
    raised = False
    try:
        storage.w3_message_schedule_write("mgrRB", "07:00", "16:00", "16:00", "07:00",
                                           effective_from="2020-01-01", db_path=db_rb)
    except sqlite3.IntegrityError:
        raised = True
    check("version-write CHECK failure propagates as IntegrityError", raised is True)
    con = sqlite3.connect(db_rb)
    legacy_count_after = con.execute(
        "SELECT COUNT(*) FROM manager_client_message_schedule WHERE manager_key='mgrRB'"
    ).fetchone()[0]
    version_count_after = con.execute(
        "SELECT COUNT(*) FROM w3_manager_schedule_version WHERE manager_key='mgrRB'"
    ).fetchone()[0]
    audit_count_after = con.execute(
        "SELECT COUNT(*) FROM w3_schedule_audit WHERE scope_key='mgrRB'"
    ).fetchone()[0]
    con.close()
    check("failed version insert rolls back the legacy compatibility write",
          legacy_count_after == legacy_count_before, f"{legacy_count_before} -> {legacy_count_after}")
    check("failed version insert leaves exactly the original single open version",
          version_count_after == 1, str(version_count_after))
    check("failed version insert writes no audit row", audit_count_after == 1, str(audit_count_after))

    # explicit exception date ranges are inclusive
    db_exc = os.path.join(tmpdir, "exc.db")
    ok, res = storage.w3_exception_add("manager", "mgrExc", "2026-05-01", "2026-05-03", "off",
                                        actor_user_id=1, actor_role="admin", db_path=db_exc)
    check("w3_exception_add succeeds", ok is True, str(res))
    con = sqlite3.connect(db_exc)
    con.row_factory = sqlite3.Row
    for d, expect in (("2026-04-30", 0), ("2026-05-01", 1), ("2026-05-02", 1),
                      ("2026-05-03", 1), ("2026-05-04", 0)):
        n = con.execute(
            "SELECT COUNT(*) FROM w3_schedule_exception WHERE scope='manager' AND scope_key='mgrExc'"
            " AND date_from<=? AND date_to>=? AND revoked=0", (d, d),
        ).fetchone()[0]
        check(f"exception inclusive range: {d} -> matches={bool(expect)}", (n > 0) == bool(expect), str(n))
    con.close()

    # revoke is soft, never DELETE
    ok, res = storage.w3_exception_revoke(1, reason="test revoke", actor_user_id=1, actor_role="admin",
                                           db_path=db_exc)
    check("w3_exception_revoke succeeds", ok is True, str(res))
    con = sqlite3.connect(db_exc)
    row = con.execute("SELECT revoked FROM w3_schedule_exception WHERE id=1").fetchone()
    check("revoke sets revoked=1, row still present (no DELETE)", row is not None and row[0] == 1, str(row))
    con.close()

    # version intervals are half-open: [effective_from, effective_to)
    db_hi = os.path.join(tmpdir, "halfopen.db")
    storage.w3_source_version_set("srcHO", "work", {"mon": 1}, effective_from="2026-01-01", db_path=db_hi)
    storage.w3_source_version_set("srcHO", "work", {"mon": 0}, effective_from="2026-02-01", db_path=db_hi)
    con = storage._bsl_connect(db_hi)
    v_on_boundary = storage._w3_open_version(con, "w3_source_schedule_version", "source_key", "srcHO",
                                              "work", "2026-02-01")
    v_before_boundary = storage._w3_open_version(con, "w3_source_schedule_version", "source_key", "srcHO",
                                                  "work", "2026-01-31")
    con.close()
    check("half-open: business_date == effective_to boundary belongs to the NEW version",
          v_on_boundary is not None and v_on_boundary["mon"] == 0, str(v_on_boundary))
    check("half-open: business_date == day before boundary belongs to the OLD version",
          v_before_boundary is not None and v_before_boundary["mon"] == 1, str(v_before_boundary))

    # deterministic JSON serialization
    a = storage._w3_json_dumps({"b": 1, "a": 2})
    b = storage._w3_json_dumps({"a": 2, "b": 1})
    check("deterministic JSON serialization (key order independent)", a == b, f"{a!r} vs {b!r}")

    # no history DELETE helper exists anywhere in storage.py's W3 block
    src = Path(BASE_DIR, "storage.py").read_text(encoding="utf-8")
    w3_start = src.index("TPILOT W3.1 SCHEDULE RESOLVER FOUNDATION")
    w3_block = src[w3_start:]
    for forbidden in ("DELETE FROM w3_source_schedule_version", "DELETE FROM w3_manager_schedule_version",
                       "DELETE FROM w3_manager_source_history"):
        check(f"no '{forbidden}' literal in the W3.1 block", forbidden not in w3_block)

    # source_history_record: append-only, closing contract (close_default closes bound
    # manager versions)
    db_hist = os.path.join(tmpdir, "hist.db")
    ok, res = storage.w3_source_history_record("mgrH", "srcOld", effective_from="2026-01-01",
                                                change_kind="link", db_path=db_hist)
    check("w3_source_history_record first link succeeds", ok is True, str(res))
    first_hist_id = res["id"]
    ok_v, res_v = storage.w3_manager_version_set(
        "mgrH", "work", {"mon": 1, "tue": 1, "wed": 1, "thu": 1, "fri": 1, "sat": 0, "sun": 0},
        effective_from="2026-01-05", source_link_version_id=first_hist_id, db_path=db_hist,
    )
    check("manager version bound to source_link_version_id succeeds", ok_v is True, str(res_v))
    ok2, res2 = storage.w3_source_history_record(
        "mgrH", "srcNew", effective_from="2026-03-01", change_kind="relink",
        override_decision="close_default", db_path=db_hist,
    )
    check("w3_source_history_record relink with close_default succeeds", ok2 is True, str(res2))
    check("close_default closed exactly one bound manager override", res2.get("closed_override_count") == 1,
          str(res2))
    con = sqlite3.connect(db_hist)
    v = con.execute("SELECT effective_to, close_reason FROM w3_manager_schedule_version WHERE id=?",
                     (res_v["id"],)).fetchone()
    con.close()
    check("bound manager version was closed at the reassignment date",
          v == ("2026-03-01", "source_reassignment"), str(v))
    # history is append-only: first interval still present, not deleted, effective_to set
    con = sqlite3.connect(db_hist)
    n_hist = con.execute("SELECT COUNT(*) FROM w3_manager_source_history WHERE manager_key='mgrH'").fetchone()[0]
    con.close()
    check("source history append-only: 2 rows present after relink", n_hist == 2, str(n_hist))

    # batch resolver returns stable ordering already covered in the resolver selftest;
    # here we only assert schema creation is idempotent under a shared connection reuse
    # scenario (no crash, no duplicate DDL error) via w3_resolve_schedule_batch.
    storage.w3_resolve_schedule_batch(["mgrH"], "2026-01-05", db_path=db_hist)
    check("w3_resolve_schedule_batch runs cleanly against a populated w3 schema", True)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="w3_schema_selftest_") as tmpdir:
        with _RealDbGuard():
            run_all_checks(tmpdir)
    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
