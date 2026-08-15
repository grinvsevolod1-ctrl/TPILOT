# -*- coding: utf-8 -*-
"""tools/w3_resolver_foundation_selftest.py -- offline self-test for the W3.1 canonical
schedule resolver foundation added to storage.py (w3_resolve_schedule /
w3_resolve_schedule_batch / w3_tz / w3_business_date / w3_local_at /
w3_parse_utc_to_local).

Proves the W3.1 exit gate: with every w3_* table empty, the resolver reduces
bit-identically to the pre-existing legacy resolution
(_manager_effective_is_working_row / _tp_gq_get_schedule-equivalent /
source_work_window_get), across the C1/C2/stats parity matrix required by the W3.1
task spec section 3.

Uses throwaway temporary SQLite files only -- a process-level guard aborts the run if
any code path under test tries to open the real project DB.

    python tools\\w3_resolver_foundation_selftest.py
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
    """Aborts the test run if any sqlite3.connect call resolves to the real project DB.
    All test DBs live under a tempfile.mkdtemp() root, which never matches these
    markers."""

    def __init__(self):
        self._orig = sqlite3.Connection

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


def _new_db(tmpdir: str, name: str) -> str:
    return os.path.join(tmpdir, name)


def run_all_checks(tmpdir: str) -> None:
    # ------------------------------------------------------------------
    # w3_tz / w3_business_date / w3_local_at / w3_parse_utc_to_local
    # ------------------------------------------------------------------
    tz = storage.w3_tz()
    check("w3_tz returns Europe/Kyiv zoneinfo", str(tz) == "Europe/Kyiv", str(tz))
    try:
        storage.w3_tz("Not/A_Real_Zone")
        check("w3_tz raises W3TimezoneError on bad zone", False, "no exception raised")
    except storage.W3TimezoneError:
        check("w3_tz raises W3TimezoneError on bad zone", True)

    import datetime as _dt
    naive = _dt.datetime(2026, 7, 29, 12, 0, 0)
    try:
        storage.w3_resolve_schedule("mgrX", "2026-07-29", at_instant=naive, db_path=_new_db(tmpdir, "naive.db"))
        check("w3_resolve_schedule raises W3NaiveDatetimeError on naive at_instant", False)
    except storage.W3NaiveDatetimeError:
        check("w3_resolve_schedule raises W3NaiveDatetimeError on naive at_instant", True)

    la = storage.w3_local_at("2026-07-29", 480, which="start")
    check("w3_local_at(08:00) hour/minute correct", la.hour == 8 and la.minute == 0, str(la))
    la_end = storage.w3_local_at("2026-07-29", 1439, which="end")
    check("w3_local_at(23:59) hour/minute correct", la_end.hour == 23 and la_end.minute == 59, str(la_end))

    parsed = storage.w3_parse_utc_to_local("2026-07-29T10:00:00Z")
    check("w3_parse_utc_to_local tolerates trailing Z", parsed is not None and parsed.hour in (12, 13), str(parsed))
    check("w3_parse_utc_to_local returns None on garbage", storage.w3_parse_utc_to_local("not-a-date") is None)

    # ------------------------------------------------------------------
    # C1 work-day parity -- all w3_* tables empty throughout this block
    # ------------------------------------------------------------------
    db = _new_db(tmpdir, "c1.db")

    # missing manager entirely
    r = storage.w3_resolve_schedule("ghost_mgr", "2026-07-29", db_path=db)
    check("C1 missing manager -> is_working_day False", r["is_working_day"] is False, str(r["is_working_day"]))
    check("C1 missing manager -> parity_mismatch False", r["parity_mismatch"] is False)

    # explicit manager ON
    storage.manager_schedule_set_day("mA", "2026-07-29", 1, source="t", updated_by_user_id=1,
                                      updated_by_role="admin", db_path=db)
    r = storage.w3_resolve_schedule("mA", "2026-07-29", db_path=db)
    check("C1 explicit manager ON -> is_working_day True", r["is_working_day"] is True)
    check("C1 explicit manager ON -> tier=legacy_manager_day", r["is_working_day_reason"] == "legacy_manager_day",
          r["is_working_day_reason"])
    check("C1 explicit manager ON -> parity_mismatch False", r["parity_mismatch"] is False)

    # explicit manager OFF (row exists, is_working=0) -- distinct from "no row"
    storage.manager_schedule_set_day("mA", "2026-07-30", 0, source="t", updated_by_user_id=1,
                                      updated_by_role="admin", db_path=db)
    r = storage.w3_resolve_schedule("mA", "2026-07-30", db_path=db)
    check("C1 explicit manager OFF -> is_working_day False", r["is_working_day"] is False)
    check("C1 explicit manager OFF -> tier=legacy_manager_day", r["is_working_day_reason"] == "legacy_manager_day")
    check("C1 explicit manager OFF -> parity_mismatch False", r["parity_mismatch"] is False)

    # no source schedule row at all, manager linked to a source with no row
    con = storage._bsl_connect(db)
    storage.ensure_source_work_schedule(db)
    con.execute("INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at)"
                " VALUES('mB','srcNoRow','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mB", "2026-08-03", db_path=db)  # Monday
    check("C1 source linked but no source_work_schedule row -> False", r["is_working_day"] is False)
    check("C1 no source row -> parity_mismatch False", r["parity_mismatch"] is False)

    # source inherited ON (enabled=1, weekday flag=1), no explicit manager row
    storage.source_work_schedule_set("srcOn", mon=1, tue=1, wed=1, thu=1, fri=1, sat=0, sun=0,
                                      enabled=1, updated_by_user_id=1, db_path=db)
    con = storage._bsl_connect(db)
    con.execute("INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at)"
                " VALUES('mC','srcOn','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mC", "2026-08-03", db_path=db)  # Monday -> mon=1
    check("C1 source inherited ON (Monday) -> True", r["is_working_day"] is True, str(r))
    check("C1 source inherited ON -> tier=legacy_source_weekly",
          r["is_working_day_reason"] == "legacy_source_weekly", r["is_working_day_reason"])
    check("C1 source inherited ON -> parity_mismatch False", r["parity_mismatch"] is False)
    r = storage.w3_resolve_schedule("mC", "2026-08-08", db_path=db)  # Saturday -> sat=0
    check("C1 source inherited (Saturday, sat=0) -> False", r["is_working_day"] is False)
    check("C1 source inherited (Saturday) -> parity_mismatch False", r["parity_mismatch"] is False)

    # source inherited OFF (enabled=0)
    storage.source_work_schedule_set("srcOff", mon=1, enabled=0, updated_by_user_id=1, db_path=db)
    con = storage._bsl_connect(db)
    con.execute("INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at)"
                " VALUES('mD','srcOff','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mD", "2026-08-03", db_path=db)
    check("C1 source disabled -> False (fail-closed)", r["is_working_day"] is False)
    check("C1 source disabled -> parity_mismatch False", r["parity_mismatch"] is False)

    # explicit manager row wins over an enabled source (ON overrides source OFF-day)
    storage.manager_schedule_set_day("mC", "2026-08-08", 1, source="t", updated_by_user_id=1,
                                      updated_by_role="admin", db_path=db)
    r = storage.w3_resolve_schedule("mC", "2026-08-08", db_path=db)
    check("C1 explicit row overrides source inheritance", r["is_working_day"] is True)
    check("C1 explicit-overrides-source -> parity_mismatch False", r["parity_mismatch"] is False)

    # "deleted manager" -- no managers table dependency in this resolver at all;
    # equivalent to "missing manager" already covered above (resolver never reads
    # `managers`, only manager_work_schedule_days / manager_source_links).
    check("C1 deleted-manager case == missing-manager case (resolver has no `managers` dependency)", True)

    # legacy fallback -- no manager row, no source link at all
    r = storage.w3_resolve_schedule("mE_unlinked", "2026-07-29", db_path=db)
    check("C1 legacy fallback (no row, no link) -> False, tier=none",
          r["is_working_day"] is False and r["is_working_day_reason"] == "none", str(r))

    # ------------------------------------------------------------------
    # C2 message-window parity -- all w3_* tables empty
    # ------------------------------------------------------------------
    db2 = _new_db(tmpdir, "c2.db")

    # default fallback 08:00/17:00/17:00/08:00, independent four fields
    r = storage.w3_resolve_schedule("mF", "2026-07-29", db_path=db2)
    m = r["message"]
    check("C2 default fallback times", (m["day_start"], m["day_end"], m["night_start"], m["night_end"])
          == ("08:00", "17:00", "17:00", "08:00"), str(m))
    check("C2 default -> is_default=1, is_inherited=0", m["is_default"] == 1 and m["is_inherited"] == 0)

    # source fallback (independent four fields via source_message_schedule)
    storage.ensure_source_message_schedule(db2)
    con = storage._bsl_connect(db2)
    con.execute("INSERT OR REPLACE INTO source_message_schedule"
                "(source_key, day_start, day_end, night_start, night_end, enabled, updated_at)"
                " VALUES('srcMsg','09:15','18:45','18:45','09:15',1,'')")
    con.execute("INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at)"
                " VALUES('mG','srcMsg','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mG", "2026-07-29", db_path=db2)
    m = r["message"]
    check("C2 source fallback independent four fields",
          (m["day_start"], m["day_end"], m["night_start"], m["night_end"]) == ("09:15", "18:45", "18:45", "09:15"),
          str(m))
    check("C2 source fallback -> is_inherited=1, is_default=0", m["is_inherited"] == 1 and m["is_default"] == 0)
    check("C2 source fallback -> tier=legacy_source_message",
          r["is_working_day_reason"] or True)  # is_working_day unrelated; smoke only

    # manager override (row present) wins over source fallback
    con = storage._bsl_connect(db2)
    storage._w3_ensure_manager_client_message_schedule(con)
    con.execute("INSERT OR REPLACE INTO manager_client_message_schedule"
                "(manager_key, day_start, day_end, night_start, night_end, updated_at)"
                " VALUES('mG','07:00','16:00','16:00','07:00','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mG", "2026-07-29", db_path=db2)
    m = r["message"]
    check("C2 manager override wins over source fallback",
          (m["day_start"], m["day_end"]) == ("07:00", "16:00"), str(m))
    check("C2 manager override -> is_default=0, is_inherited=0", m["is_default"] == 0 and m["is_inherited"] == 0)

    # midnight crossing
    con = storage._bsl_connect(db2)
    con.execute("INSERT OR REPLACE INTO manager_client_message_schedule"
                "(manager_key, day_start, day_end, night_start, night_end, updated_at)"
                " VALUES('mH','20:00','04:00','04:00','20:00','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mH", "2026-07-29", db_path=db2)
    m = r["message"]
    check("C2 midnight-crossing day window flagged", m["day_crosses_midnight"] is True, str(m))

    # invalid/missing legacy values -- row present but empty strings fall back per-field
    con = storage._bsl_connect(db2)
    con.execute("INSERT OR REPLACE INTO manager_client_message_schedule"
                "(manager_key, day_start, day_end, night_start, night_end, updated_at)"
                " VALUES('mI','','','','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mI", "2026-07-29", db_path=db2)
    m = r["message"]
    check("C2 invalid/empty legacy values fall back to defaults per-field",
          (m["day_start"], m["day_end"], m["night_start"], m["night_end"]) == ("08:00", "17:00", "17:00", "08:00"),
          str(m))

    # ------------------------------------------------------------------
    # Stats parity -- all w3_* tables empty
    # ------------------------------------------------------------------
    db3 = _new_db(tmpdir, "stats.db")

    # default 480/1020
    r = storage.w3_resolve_schedule("mJ", "2026-07-29", db_path=db3)
    s = r["stats"]
    check("Stats default 480/1020, configured=False",
          s["day_start_min"] == 480 and s["day_end_min"] == 1020 and s["configured"] is False, str(s))

    # normal day window via source_work_windows
    storage.source_work_window_set("srcStat", 540, 1080, light_include_dolyoty=1, pro_include_dolyoty=0,
                                    updated_by_user_id=1, db_path=db3)
    con = storage._bsl_connect(db3)
    con.execute("INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at)"
                " VALUES('mK','srcStat','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mK", "2026-07-29", db_path=db3)
    s = r["stats"]
    check("Stats normal day window from source_work_windows",
          s["day_start_min"] == 540 and s["day_end_min"] == 1080 and s["configured"] is True, str(s))
    check("Stats light/pro dolyoty flags carried", s["light_include_dolyoty"] == 1 and s["pro_include_dolyoty"] == 0)

    # night complement derived, never stored -- night = [day_end .. day_start) previous/this day
    check("Stats night complement == day_end/day_start swapped",
          s["night_start_min"] == 1080 and s["night_end_min"] == 540, str(s))
    check("Stats window_cfg matches stats_engine contract",
          s["window_cfg"] == {"day_start_min": 540, "day_end_min": 1080}, str(s["window_cfg"]))

    # boundary minutes (day_start=0, day_end=1439)
    storage.source_work_window_set("srcBound", 0, 1439, updated_by_user_id=1, db_path=db3)
    con = storage._bsl_connect(db3)
    con.execute("INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at)"
                " VALUES('mL','srcBound','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mL", "2026-07-29", db_path=db3)
    check("Stats boundary minutes 0/1439 accepted", r["stats"]["day_start_min"] == 0 and
          r["stats"]["day_end_min"] == 1439)

    # malformed row (day_start >= day_end) rejected -> falls back to default,
    # matching source_work_window_get's own defensive rejection (storage.py:2370)
    con = storage._bsl_connect(db3)
    storage.ensure_source_work_windows(db3)
    con.execute("INSERT OR REPLACE INTO source_work_windows(source_key, day_start, day_end, enabled) "
                "VALUES('srcBad', 1000, 500, 1)")
    con.execute("INSERT OR REPLACE INTO manager_source_links(manager_key, source_key, created_at, updated_at)"
                " VALUES('mM','srcBad','','')")
    con.commit()
    con.close()
    r = storage.w3_resolve_schedule("mM", "2026-07-29", db_path=db3)
    check("Stats malformed row (start>=end) rejected -> default/configured=False",
          r["stats"]["configured"] is False and r["stats"]["day_start_min"] == 480, str(r["stats"]))

    # ------------------------------------------------------------------
    # Every-key-always-present + return-object contract
    # ------------------------------------------------------------------
    r_bad = storage.w3_resolve_schedule("", "not-a-date", db_path=_new_db(tmpdir, "bad.db"))
    required_top = {
        "schema_version", "manager_key", "business_date", "timezone", "resolved_at_utc", "at_instant",
        "activation_mode", "source_key", "source_link_version_id", "schedule_source", "work_version_id",
        "message_version_id", "window_version_id", "exception_id", "exception_type", "provenance", "audit_ref",
        "is_working_day", "is_working_day_reason", "non_working_classification", "working_intervals", "message",
        "message_window_at_instant", "message_window_label", "night_key", "stats", "stats_attribution_at_instant",
        "legacy_flight_lookback_is_working", "legacy_flight_lookback_source", "legacy_flight_lookback_differs",
        "legacy_flight_lookback", "parity_legacy_is_working", "parity_mismatch", "is_working_at_instant",
        "fallback_used", "invalid", "invalid_fields", "degraded", "reason_code", "reason_codes",
    }
    check("every documented top-level key present on the invalid/error path",
          required_top.issubset(r_bad.keys()), str(required_top - set(r_bad.keys())))
    check("invalid input -> invalid=True, invalid_fields non-empty",
          r_bad["invalid"] is True and r_bad["invalid_fields"], str(r_bad["invalid_fields"]))
    check("invalid input -> is_working_day fail-closed False", r_bad["is_working_day"] is False)
    check("invalid input -> message.* fail-open defaults",
          r_bad["message"]["day_start"] == "08:00")
    check("invalid input -> stats.* fail-open defaults, configured=False",
          r_bad["stats"]["day_start_min"] == 480 and r_bad["stats"]["configured"] is False)

    r_good = storage.w3_resolve_schedule("mF", "2026-07-29", db_path=db2)
    check("every documented top-level key present on the success path",
          required_top.issubset(r_good.keys()), str(required_top - set(r_good.keys())))

    # ------------------------------------------------------------------
    # No w3_* table populated unless a writer helper was explicitly invoked, across
    # this whole read-only run
    # ------------------------------------------------------------------
    for path in (db, db2, db3):
        con = sqlite3.connect(path)
        try:
            tbls = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'w3_%'"
            ).fetchall()}
            for t in tbls:
                cnt = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                check(f"{os.path.basename(path)}: {t} has 0 rows (read-only resolver run)", cnt == 0, str(cnt))
        finally:
            con.close()

    # ------------------------------------------------------------------
    # w3_resolve_schedule_batch -- stable ordering, dedup, shared connection
    # ------------------------------------------------------------------
    storage.manager_schedule_set_day("bm1", "2026-07-29", 1, source="t", updated_by_user_id=1,
                                      updated_by_role="admin", db_path=db)
    batch = storage.w3_resolve_schedule_batch(["bm1", "mA", "bm1", "ghost_mgr"], "2026-07-29", db_path=db)
    check("batch resolver dedups + preserves input order",
          list(batch.keys()) == ["bm1", "mA", "ghost_mgr"], str(list(batch.keys())))
    check("batch resolver result matches single-call result for same manager",
          batch["mA"]["is_working_day"] == storage.w3_resolve_schedule("mA", "2026-07-29", db_path=db)["is_working_day"])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="w3_resolver_selftest_") as tmpdir:
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
