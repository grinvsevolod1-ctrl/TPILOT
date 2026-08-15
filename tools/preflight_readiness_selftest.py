# -*- coding: utf-8 -*-
"""tools/preflight_readiness_selftest.py -- offline self-test for the
"false evening/morning preflight readiness errors" patch (preflight_check.py).

Business rules under test:
* Readiness (build_report/build_partner_report) must reflect real ACTIVE
  managers only. A scheduled manager_key that resolves to nothing in the
  active `managers` table -- because it is tombstone-only (hard-deleted,
  kept in manager_stats_tombstones for historical STATS retention only) or
  simply missing/stale schedule data -- must be silently excluded: never
  appended to report["managers"], never counted in working_count, never
  inflates the "Без источника" source breakdown, never flips all_ok=False.
* _relevant_managers_for_deep_verify must only enqueue manager_queue
  commands for ACTIVE managers -- a tombstone-only/missing key has no live
  runtime to ever answer, so enqueuing for it just guarantees a wasted,
  permanently-timing-out queue row.
* run_deep_verification's timeout path must fall back to DB evidence: if
  the manager runtime never answers in time but the central DB already has
  expected (or more) created/non-deleted bizlink rows for that manager and
  date, report status="ok" with verified=expected instead of a false
  timeout/red. If the DB has FEWER links than expected, the honest timeout
  status is preserved.
* build_partner_report (source-scoped, PartnerBot-facing) already excludes
  tombstone-only/missing managers correctly (untouched by this patch) --
  locked in here as a regression guard.

Techniques: preflight_check.py is a PURE, directly-importable module (no
Telethon/env side effects at import time, confirmed by its own module
docstring and by a plain `import preflight_check` sanity check) -- so these
tests use REAL execution via direct import, monkeypatching only the
storage.* functions it calls (schedule/jobs/tombstone/manager-queue) plus
preflight_check's own process-scan helpers (to keep tests fast/deterministic
and avoid a real PowerShell subprocess call). Everything DB-related that
preflight_check.py reads directly via sqlite3 (managers, manager_source_
links, traffic_sources, bizlinks) runs against REAL temporary SQLite files.

Pure/offline: no network, no Telegram, no production DB, no external APIs.

    python3.12 tools\\preflight_readiness_selftest.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage
import preflight_check as pf
from manager_registry import normalize_manager_key

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


# ======================================================================
# Test-isolation monkeypatches for preflight_check's own process-scan
# helpers -- keeps tests fast/deterministic and avoids a real PowerShell
# subprocess call. Never touches the real file; restored implicitly by
# being the ONLY value these names ever have within this test process.
# ======================================================================

def _fake_scan_python_processes(timeout_sec: float = 12.0):
    return []


def _fake_check_services(procs=None):
    return {"ok": True, "issues": [], "detail": {}}


pf._scan_python_processes = _fake_scan_python_processes
pf.check_services = _fake_check_services


# ======================================================================
# Temp SQLite helpers -- minimal schema covering exactly what
# preflight_check.py reads directly (managers via manager_registry's
# list_manager_rows_from_db_sync, manager_source_links, traffic_sources,
# bizlinks). storage.*'s own tables (schedule/jobs/tombstones/queue) are
# monkeypatched instead of seeded for real -- see each test group.
# ======================================================================

def _make_temp_db() -> str:
    tmp_db = tempfile.mktemp(suffix="_preflight_readiness_selftest.db")
    con = sqlite3.connect(tmp_db)
    con.executescript(
        """
        CREATE TABLE managers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_key TEXT UNIQUE,
            display_name TEXT DEFAULT '',
            telegram_username TEXT DEFAULT '',
            status TEXT DEFAULT 'active',
            is_enabled INTEGER DEFAULT 1,
            manual_stopped INTEGER DEFAULT 0,
            proxy_enabled INTEGER DEFAULT 0,
            proxy_bypass_allowed INTEGER DEFAULT 0,
            proxy_last_error TEXT DEFAULT ''
        );
        CREATE TABLE manager_source_links(manager_key TEXT PRIMARY KEY, source_key TEXT);
        CREATE TABLE traffic_sources(source_key TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE bizlinks(
            manager_key TEXT, target_date TEXT, slug TEXT, status TEXT,
            link_url TEXT DEFAULT '', last_error_class TEXT DEFAULT '', deleted_at TEXT DEFAULT ''
        );
        CREATE TABLE manager_telegram_health(manager_key TEXT, health_status TEXT, last_check_at TEXT, updated_at TEXT);
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        """
    )
    con.commit()
    con.close()
    return tmp_db


def _insert_manager(db_path: str, key: str, *, source_key: str = "", display_name: str = "") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO managers(manager_key, display_name, telegram_username, status, is_enabled, manual_stopped) "
            "VALUES(?,?,?,?,1,0)",
            (key, display_name or key, "", "active"),
        )
        if source_key:
            con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, source_key))
        con.commit()
    finally:
        con.close()


def _insert_source(db_path: str, source_key: str, name: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO traffic_sources(source_key, name) VALUES(?,?)", (source_key, name))
        con.commit()
    finally:
        con.close()


def _insert_bizlink(db_path: str, manager_key: str, target_date: str, slug: str, *, status: str = "created") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO bizlinks(manager_key, target_date, slug, status, link_url, last_error_class, deleted_at) "
            "VALUES(?,?,?,?,?,?,'')",
            (manager_key, target_date, slug, status, f"https://t.me/{slug}" if status == "created" else "", ""),
        )
        con.commit()
    finally:
        con.close()


class _StoragePatch:
    """Context manager: monkeypatch a set of storage.* attributes, restore
    the originals on exit (even on failure) -- avoids test-order coupling
    from a stray monkeypatch surviving into the next test group."""

    def __init__(self, **overrides):
        self._overrides = overrides
        self._originals: dict = {}

    def __enter__(self):
        for name, fn in self._overrides.items():
            self._originals[name] = getattr(storage, name)
            setattr(storage, name, fn)
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, fn in self._originals.items():
            setattr(storage, name, fn)
        return False


# ======================================================================
# GROUP 1/2 -- build_report excludes tombstone/missing schedule keys,
# active source-linked manager remains included (REAL execution).
# ======================================================================

def run_build_report_checks() -> None:
    print("\n-- preflight_check.py: build_report excludes stale/tombstone/missing schedule keys (REAL execution) --")

    tmp_db = _make_temp_db()
    try:
        _insert_source(tmp_db, "rassylka", "Рассылка")
        _insert_manager(tmp_db, "valeria0863", source_key="rassylka", display_name="Валерия")
        target_date = "2026-07-14"

        def _fake_schedule(work_date, db_path=None):
            return ["valeria0863", "darias", "dariass"]

        def _fake_jobs(date_iso, db_path=None):
            return []

        def _fake_tombstone(mk, db_path=None):
            if normalize_manager_key(mk) == "dariass":
                return {"manager_key": "dariass", "deleted_at": "2026-07-10T17:45:44", "retention_until": "2026-09-08"}
            return None

        with _StoragePatch(
            manager_schedule_list_working_on_date=_fake_schedule,
            bizlink_job_list_for_date=_fake_jobs,
            manager_stats_tombstone_get=_fake_tombstone,
        ):
            report = pf.build_report(today_iso=target_date, db_path=tmp_db)

        check("1. working_count counts only the ACTIVE manager (valeria0863), not darias/dariass",
              report["working_count"] == 1, repr(report.get("working_count")))
        mgr_keys_in_report = {m.get("manager_key") for m in report["managers"]}
        check("1b. tombstone-only manager (dariass) is never appended to report['managers'] as an error",
              "dariass" not in mgr_keys_in_report, repr(mgr_keys_in_report))
        check("1c. genuinely-missing manager (darias) is never appended to report['managers'] as an error",
              "darias" not in mgr_keys_in_report, repr(mgr_keys_in_report))
        check("1d. the active manager IS present in report['managers']",
              "valeria0863" in mgr_keys_in_report, repr(mgr_keys_in_report))

        breakdown = pf._source_breakdown_for_managers(report["managers"])
        breakdown_map = dict(breakdown)
        check("1e. no 'Без источника' entry was created by the missing/tombstone schedule keys",
              "Без источника" not in breakdown_map, repr(breakdown))
        check("2. the active source-linked manager remains in the source breakdown under its real source name",
              breakdown_map.get("Рассылка") == 1, repr(breakdown))

        check("1f. all_ok is NOT falsified merely by stale/missing/tombstone schedule rows",
              report["all_ok"] is True, repr(report))
        check("1g. archived_count reflects the 2 skipped stale schedule keys (informational bookkeeping only)",
              report.get("archived_count") == 2, repr(report.get("archived_count")))
        archived_keys = {a.get("manager_key") for a in (report.get("archived_managers") or [])}
        check("1h. both stale keys are recorded in archived_managers (informational, not an error)",
              archived_keys == {"darias", "dariass"}, repr(archived_keys))
    finally:
        try:
            os.remove(tmp_db)
        except Exception:
            pass


# ======================================================================
# GROUP 3/4 -- run_deep_verification timeout fallback (REAL execution).
# ======================================================================

async def _run_deep_verify_with_links(link_count: int, *, expect_job: bool = True) -> dict:
    """Build a temp DB with `link_count` real created bizlinks for
    valeria0863/rassylka on 2026-07-14, monkeypatch schedule/jobs/queue so
    the manager is relevant but its queue command NEVER answers (deadline
    already in the past -- guarantees the timeout branch, no sleep), and
    return the resulting deep-verify blob."""
    tmp_db = _make_temp_db()
    try:
        _insert_source(tmp_db, "rassylka", "Рассылка")
        _insert_manager(tmp_db, "valeria0863", source_key="rassylka")
        target_date = "2026-07-14"
        for i in range(link_count):
            _insert_bizlink(tmp_db, "valeria0863", target_date, f"slug{i}", status="created")

        def _fake_schedule(work_date, db_path=None):
            return ["valeria0863"]

        def _fake_jobs(date_iso, db_path=None):
            return [{"manager_key": "valeria0863"}] if expect_job else []

        async def _fake_queue_put(*, target_key, command, args, payload_json, created_by, expires_at, db_path=None):
            return f"nonce-{target_key}"

        async def _fake_queue_get(nonce, db_path=None):
            return None  # never answers -> stays pending -> exercises the timeout path

        async def _fake_queue_mark_timeouts(db_path=None):
            return None

        with _StoragePatch(
            manager_schedule_list_working_on_date=_fake_schedule,
            bizlink_job_list_for_date=_fake_jobs,
            manager_queue_put=_fake_queue_put,
            manager_queue_get=_fake_queue_get,
            manager_queue_mark_timeouts=_fake_queue_mark_timeouts,
        ):
            loop = asyncio.get_event_loop()
            deadline_ts = loop.time() - 1.0  # already past -> zero poll iterations, no sleep
            blob = await pf.run_deep_verification(target_date, tmp_db, "evening", deadline_ts)
        return blob
    finally:
        try:
            os.remove(tmp_db)
        except Exception:
            pass


async def run_deep_verify_checks() -> None:
    print("\n-- preflight_check.py: run_deep_verification timeout DB-evidence fallback (REAL execution) --")

    # 3. timeout, but DB already has expected(15)+ created links -> OK.
    blob_ok = await _run_deep_verify_with_links(15)
    m_ok = blob_ok["managers"].get("valeria0863") or {}
    check("3. manager status is OK despite a runtime timeout, because the DB already "
          "has the expected(15) created links",
          m_ok.get("status") == "ok", repr(m_ok))
    check("3b. verified == expected (15) on the DB-evidence fallback path",
          m_ok.get("verified") == 15 and m_ok.get("expected") == 15, repr(m_ok))
    check("3c. failed == 0 on the DB-evidence fallback path", m_ok.get("failed") == 0, repr(m_ok))
    check("3d. reason mentions the links already being created (not a raw timeout message)",
          "уже созданы" in (m_ok.get("reason") or ""), repr(m_ok))
    check("3e. source (rassylka) status is ok, not partial/timeout",
          blob_ok["sources"].get("rassylka", {}).get("status") == "ok", repr(blob_ok.get("sources")))
    check("3f. status_global is ok", blob_ok.get("status_global") == "ok", repr(blob_ok))

    # More links in the DB than expected must still resolve to a clean OK
    # (>= expected, not only ==).
    blob_more = await _run_deep_verify_with_links(20)
    m_more = blob_more["managers"].get("valeria0863") or {}
    check("3g. actual > expected also resolves to OK (>= comparison, not strict equality)",
          m_more.get("status") == "ok" and m_more.get("verified") == m_more.get("expected"), repr(m_more))

    # 4. timeout, DB has FEWER links than expected -> stays a real timeout.
    blob_fail = await _run_deep_verify_with_links(5)
    m_fail = blob_fail["managers"].get("valeria0863") or {}
    check("4. status remains 'timeout' when actual(5) < expected(15)",
          m_fail.get("status") == "timeout", repr(m_fail))
    check("4b. verified does NOT falsely become expected when there isn't enough DB evidence",
          m_fail.get("verified") == 0, repr(m_fail))
    check("4c. reason is the honest 'did not answer in time' message, not the DB-evidence wording",
          "не ответил" in (m_fail.get("reason") or "") and "уже созданы" not in (m_fail.get("reason") or ""),
          repr(m_fail))
    check("4d. source/global status remains a real problem (not silently OK) when evidence is insufficient",
          blob_fail.get("status_global") != "ok", repr(blob_fail))

    # C. tombstone/missing managers are never enqueued for deep verify at
    # all (no live runtime could ever answer) -- exercised directly against
    # _relevant_managers_for_deep_verify.
    tmp_db2 = _make_temp_db()
    try:
        _insert_source(tmp_db2, "rassylka", "Рассылка")
        _insert_manager(tmp_db2, "valeria0863", source_key="rassylka")
        target_date = "2026-07-14"
        _insert_bizlink(tmp_db2, "valeria0863", target_date, "slugX", status="created")

        def _fake_schedule2(work_date, db_path=None):
            return ["valeria0863", "darias", "dariass"]

        def _fake_jobs2(date_iso, db_path=None):
            return [{"manager_key": "valeria0863"}, {"manager_key": "darias"}, {"manager_key": "dariass"}]

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule2, bizlink_job_list_for_date=_fake_jobs2):
            relevant = pf._relevant_managers_for_deep_verify(target_date, tmp_db2)
        check("C. only the ACTIVE manager is eligible for deep-verify enqueue -- "
              "tombstone-only/missing keys are excluded entirely (no wasted queue row)",
              set(relevant.keys()) == {"valeria0863"}, repr(set(relevant.keys())))
    finally:
        try:
            os.remove(tmp_db2)
        except Exception:
            pass


# ======================================================================
# GROUP 5 -- build_partner_report excludes tombstone/missing managers
# (REGRESSION GUARD: this function was already correct, untouched by this
# patch -- locked in here so it can never silently regress).
# ======================================================================

def run_partner_report_checks() -> None:
    print("\n-- preflight_check.py: build_partner_report excludes tombstone/missing managers (REAL execution) --")

    tmp_db = _make_temp_db()
    try:
        _insert_source(tmp_db, "rassylka", "Рассылка")
        _insert_manager(tmp_db, "valeria0863", source_key="rassylka")
        # darias/dariass are deliberately NOT inserted into `managers` at
        # all (mirrors both the tombstone-only and genuinely-missing cases).
        target_date = "2026-07-14"

        def _fake_schedule(work_date, db_path=None):
            return ["valeria0863", "darias", "dariass"]

        def _fake_jobs(date_iso, db_path=None):
            return []

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule, bizlink_job_list_for_date=_fake_jobs):
            report = pf.build_partner_report(target_date, tmp_db, source_key="rassylka")

        check("5. partner report working_count includes only the active manager",
              report["working_count"] == 1, repr(report.get("working_count")))
        mgr_keys = {m["manager_key"] for m in report["managers"]}
        check("5b. tombstone/missing managers are NOT included in the source-scoped partner report",
              "darias" not in mgr_keys and "dariass" not in mgr_keys, repr(mgr_keys))
        check("5c. the active manager IS included", "valeria0863" in mgr_keys, repr(mgr_keys))
        check("5d. send_recommended is True (a real active manager works this source today)",
              report.get("send_recommended") is True, repr(report))
    finally:
        try:
            os.remove(tmp_db)
        except Exception:
            pass


# ======================================================================
# GROUP 6 -- safety / scope (structural)
# ======================================================================

def run_safety_checks() -> None:
    print("\n-- Safety / scope --")
    src = Path(str(BASE_DIR / "preflight_check.py")).read_text(encoding="utf-8-sig")

    check("no allow_spend reference anywhere in preflight_check.py (read-only module, untouched by this patch)",
          "allow_spend" not in src)
    check("no DB write statements (INSERT/UPDATE/DELETE) in preflight_check.py -- stays pure read-only",
          not any(kw in src for kw in ("con.execute(\"INSERT", "con.execute(\"UPDATE", "con.execute(\"DELETE",
                                        "con.execute('INSERT", "con.execute('UPDATE", "con.execute('DELETE")))

    moji = sum(src.count(c) for c in ("Ð", "Ñ")) + src.count("â€")
    check("mojibake clean: preflight_check.py", moji == 0, str(moji))

    self_src = Path(__file__).read_text(encoding="utf-8-sig")
    import re as _re
    check("selftest makes no network/Telethon/API calls (no requests/telethon/urllib/anthropic imports)",
          not _re.search(r"^\s*(import|from)\s+(requests|telethon|urllib|http\.client|anthropic)\b", self_src, _re.MULTILINE))

    real_path = os.path.realpath(tempfile.gettempdir())
    check("selftest's own temp-db helper places files under the OS temp dir (never db/data_tpilot.db or db/data.db)",
          "data_tpilot.db" not in real_path and (os.sep + "db" + os.sep) not in real_path, real_path)


def main() -> int:
    run_build_report_checks()
    asyncio.run(run_deep_verify_checks())
    run_partner_report_checks()
    run_safety_checks()

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
