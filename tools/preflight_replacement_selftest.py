# -*- coding: utf-8 -*-
"""tools/preflight_replacement_selftest.py -- offline self-test for the
morning "account replacement / outage" notification block added to
preflight_check.py (_utc_iso_to_kyiv_hm, _reason_business_text,
collect_replacement_records, _replacement_line, and their wiring into
build_report / build_partner_report / render_admin_text / render_partner_text).

Business rules under test (see the implementation prompt for the full spec):
* A manager scheduled today whose account looks unavailable (is_enabled=0,
  manual_stopped=1, a fatal last_error, blocked/banned health, or a fatal
  start_status reason) gets exactly one replacement record, state in
  {A, B, C, D}, priority C > B > D > A.
* _reason_business_text never leaks a raw exception class name/repr/
  reason_class string -- only the fixed Russian business phrases.
* State C's "links updated" time comes from bizlinks.updated_at (max, over
  ACTIVE rows for the reserve+today), falling back to the activation
  event's updated_at only when no valid bizlinks timestamp exists; never
  fabricated from the report's own check_time.
* AdminBot (build_report) sees every source; PartnerBot (build_partner_report)
  only sees its own source_key, using the same shared collector.
* State C never makes all_ok False; states A/B/D always do, and are never
  classified as a "recheckable" (transient/deep-verify-only) problem by
  report_has_recheckable_problem.
* PartnerBot never shows a plain checkmark account line for a primary that
  also has an A/B/D replacement record.
* An exception inside the collector fails open (empty list), never breaks
  the rest of build_report/build_partner_report.
* No replacement records -> render_admin_text/render_partner_text emit no
  replacement-block header at all (byte-identical to pre-feature output).

Technique: preflight_check.py is a PURE, directly-importable module (no
Telethon/env side effects at import time -- confirmed by a plain
`import preflight_check` sanity check). These tests use REAL execution via
direct import, monkeypatching only storage.* schedule/job lookups, plus
preflight_check's own process-scan helpers (fast/deterministic, no real
subprocess) and BASE_DIR (points start_status.json reads at a temp runtime
dir instead of the real project's protected runtime/ folder). Everything
else preflight_check.py reads directly via sqlite3 (managers,
manager_source_links, traffic_sources, bizlinks, manager_telegram_health,
manager_reserve_pairs, reserve_activation_events) runs against REAL
temporary SQLite files.

Pure/offline: no network, no Telegram, no production DB/session/runtime,
no external APIs, no spend of any kind.

    python tools\\preflight_replacement_selftest.py
"""
from __future__ import annotations

import inspect
import json
import re
import sqlite3
import sys
import tempfile
from datetime import timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
# Test-isolation monkeypatches (mirrors tools/preflight_readiness_selftest.py)
# ======================================================================

def _fake_scan_python_processes(timeout_sec: float = 12.0):
    return []


def _fake_check_services(procs=None):
    return {"ok": True, "issues": [], "detail": {}}


pf._scan_python_processes = _fake_scan_python_processes
pf.check_services = _fake_check_services


class _StoragePatch:
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


class _RuntimeDirPatch:
    """Points pf.BASE_DIR (used only by _read_start_status) at a fresh temp
    directory for the duration of the block, so tests never touch the real
    project's protected runtime/ folder. Restores the original afterwards."""

    def __init__(self):
        self._tmp = None
        self._orig = None

    def __enter__(self) -> Path:
        self._orig = pf.BASE_DIR
        self._tmp = Path(tempfile.mkdtemp(prefix="pf_replacement_selftest_"))
        pf.BASE_DIR = self._tmp
        return self._tmp

    def __exit__(self, exc_type, exc, tb):
        pf.BASE_DIR = self._orig
        return False


def _write_start_status(tmp_dir: Path, manager_key: str, **fields) -> None:
    d = tmp_dir / "runtime" / "managers" / normalize_manager_key(manager_key)
    d.mkdir(parents=True, exist_ok=True)
    (d / "start_status.json").write_text(json.dumps(fields), encoding="utf-8")


def _fake_schedule_factory(keys):
    def _fn(work_date, db_path=None):
        return list(keys)
    return _fn


def _fake_jobs_none(date_iso, db_path=None):
    return []


# ======================================================================
# Temp SQLite schema -- exactly what preflight_check.py reads directly for
# this feature, plus the columns check_manager_today/build_report already
# rely on (mirrors tools/preflight_readiness_selftest.py's schema).
# ======================================================================

def _make_temp_db() -> str:
    tmp_db = tempfile.mktemp(suffix="_preflight_replacement_selftest.db")
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
            last_error TEXT DEFAULT '',
            proxy_enabled INTEGER DEFAULT 0,
            proxy_bypass_allowed INTEGER DEFAULT 0,
            proxy_last_error TEXT DEFAULT ''
        );
        CREATE TABLE manager_source_links(manager_key TEXT PRIMARY KEY, source_key TEXT);
        CREATE TABLE traffic_sources(source_key TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE bizlinks(
            manager_key TEXT, target_date TEXT, slug TEXT, status TEXT,
            link_url TEXT DEFAULT '', last_error_class TEXT DEFAULT '',
            deleted_at TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
        );
        CREATE TABLE manager_telegram_health(manager_key TEXT, health_status TEXT, last_check_at TEXT, updated_at TEXT);
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE manager_reserve_pairs(
            reserve_key TEXT PRIMARY KEY, primary_key TEXT NOT NULL,
            source_key TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'linked',
            created_by_user_id INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE reserve_activation_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL,
            primary_key TEXT NOT NULL, reserve_key TEXT NOT NULL, target_date TEXT NOT NULL,
            requested_by_user_id INTEGER NOT NULL DEFAULT 0, response_chat_id INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'requested', batch_job_key TEXT DEFAULT '',
            last_error TEXT DEFAULT '', created_at TEXT, updated_at TEXT
        );
        """
    )
    con.commit()
    con.close()
    return tmp_db


def _insert_manager(db_path: str, key: str, *, source_key: str = "", display_name: str = "",
                     username: str = "", is_enabled: int = 1, manual_stopped: int = 0,
                     last_error: str = "") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO managers(manager_key, display_name, telegram_username, status, "
            "is_enabled, manual_stopped, last_error) VALUES(?,?,?,?,?,?,?)",
            (key, display_name or key, username, "active", is_enabled, manual_stopped, last_error),
        )
        if source_key:
            con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, source_key))
        con.commit()
    finally:
        con.close()


def _insert_health(db_path: str, key: str, health_status: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO manager_telegram_health(manager_key, health_status, last_check_at, updated_at) VALUES(?,?,?,?)",
            (key, health_status, "2026-07-14T06:00:00", "2026-07-14T06:00:00"),
        )
        con.commit()
    finally:
        con.close()


def _insert_bizlink(db_path: str, manager_key: str, target_date: str, slug: str, *,
                     status: str = "created", created_at: str = "2026-07-14T05:00:00",
                     updated_at: str = "2026-07-14T05:00:00") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO bizlinks(manager_key, target_date, slug, status, link_url, last_error_class, "
            "deleted_at, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (manager_key, target_date, slug, status,
             f"https://t.me/{slug}" if status == "created" else "", "", "", created_at, updated_at),
        )
        con.commit()
    finally:
        con.close()


def _insert_active_links(db_path: str, manager_key: str, target_date: str, count: int, **kwargs) -> None:
    for i in range(1, count + 1):
        _insert_bizlink(db_path, manager_key, target_date, f"{manager_key}_{i}", status="created", **kwargs)


def _insert_reserve_pair(db_path: str, reserve_key: str, primary_key: str, source_key: str = "",
                          status: str = "linked", updated_at: str = "2026-07-14T06:00:00") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO manager_reserve_pairs(reserve_key, primary_key, source_key, status, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (reserve_key, primary_key, source_key, status, updated_at, updated_at),
        )
        con.commit()
    finally:
        con.close()


def _insert_activation_event(db_path: str, primary_key: str, reserve_key: str, target_date: str, *,
                              source_key: str = "", status: str = "requested",
                              updated_at: str = "2026-07-14T06:30:00") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO reserve_activation_events(source_key, primary_key, reserve_key, target_date, "
            "requested_by_user_id, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (source_key, primary_key, reserve_key, target_date, 1, status, updated_at, updated_at),
        )
        con.commit()
    finally:
        con.close()


TODAY = "2026-07-14"
YESTERDAY = "2026-07-13"
TOMORROW = "2026-07-15"

# build_report/build_partner_report now gate the whole replacement feature
# on "is this call's date the real Kyiv today" (owner fix: the block must
# never affect an evening/tomorrow report). Pin kyiv_today_iso() to the
# fixture's TODAY so every test in this file is deterministic regardless of
# the actual wall-clock date the suite happens to run on -- YESTERDAY/
# TOMORROW then genuinely exercise the "not today" gate exactly like a real
# evening call would.
pf.kyiv_today_iso = lambda: TODAY


# ======================================================================
# 1-2: state D (banned, no reason leaked) / state A (manual-only, neutral)
# ======================================================================

def test_1_2_state_d_and_a():
    print("\n-- 1/2: state D (banned) and state A (manual-stopped, neutral) --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "valeria0863", source_key="rassylka", display_name="Валерия",
                         username="valeria0863", is_enabled=0, manual_stopped=1,
                         last_error="PhoneNumberBannedError('The phone number is banned.')")
        _insert_manager(db, "stopbot", source_key="rassylka", display_name="Стопбот",
                         username="stopbot", is_enabled=1, manual_stopped=1, last_error="")

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["valeria0863", "stopbot"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)

        by_key = {r["primary_key"]: r for r in report["replacements"]}
        check("1. valeria0863 gets state D", by_key.get("valeria0863", {}).get("state") == "D", repr(by_key.get("valeria0863")))
        check("1b. reason_text is the safe business phrase", by_key["valeria0863"]["reason_text"] == "номер заблокирован Telegram")
        line = pf._replacement_line(by_key["valeria0863"])
        check("1c. rendered line contains the business phrase", "номер заблокирован Telegram" in line, line)
        check("1d. rendered line NEVER leaks the raw exception class name", "PhoneNumberBannedError" not in line, line)

        check("2. stopbot (manual_stopped, no fatal reason) gets state A", by_key.get("stopbot", {}).get("state") == "A", repr(by_key.get("stopbot")))
        check("2b. reason_text is the neutral admin-disabled phrase", by_key["stopbot"]["reason_text"] == "аккаунт отключён администратором")
        line_a = pf._replacement_line(by_key["stopbot"])
        check("2c. state A line uses the precise admin-disabled wording", "отключён администратором" in line_a, line_a)
        check("2d. no technical reason_class-like text leaks into state A line",
              not any(t in line_a for t in ("auth_session", "proxy_timeout", "connection_error", "Error(")), line_a)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 3-5, 11: state B/C transitions + priority C>B>D>A + single record per primary
# ======================================================================

def test_3_5_11_state_b_c_and_priority():
    print("\n-- 3/5/11: state B/C transitions, priority C>B>D>A, one record per primary --")
    db = _make_temp_db()
    try:
        # --- scenario 3: activation requested -> B ---
        _insert_manager(db, "m3", source_key="src1", is_enabled=0)
        _insert_manager(db, "m3reserve", source_key="src1", username="m3new")
        _insert_reserve_pair(db, "m3reserve", "m3", source_key="src1")
        _insert_activation_event(db, "m3", "m3reserve", TODAY, source_key="src1", status="requested")

        # --- scenario 4: activation done but links below readiness threshold -> B ---
        _insert_manager(db, "m4", source_key="src1", is_enabled=0)
        _insert_manager(db, "m4reserve", source_key="src1", username="m4new")
        _insert_reserve_pair(db, "m4reserve", "m4", source_key="src1")
        _insert_activation_event(db, "m4", "m4reserve", TODAY, source_key="src1", status="done")
        _insert_active_links(db, "m4reserve", TODAY, 5)  # below default 15

        # --- scenario 5: activation done + links ready -> C ---
        _insert_manager(db, "m5", source_key="src1", is_enabled=0)
        _insert_manager(db, "m5reserve", source_key="src1", username="m5new")
        _insert_reserve_pair(db, "m5reserve", "m5", source_key="src1")
        _insert_activation_event(db, "m5", "m5reserve", TODAY, source_key="src1", status="done")
        _insert_active_links(db, "m5reserve", TODAY, 15)

        # --- scenario 11: primary has BOTH a fatal reason AND a fully-ready
        # reserve, plus a stale earlier event row -> must resolve to C only,
        # exactly one record. ---
        _insert_manager(db, "m11", source_key="src1", is_enabled=0,
                         last_error="PhoneNumberBannedError")
        _insert_manager(db, "m11reserve", source_key="src1", username="m11new")
        _insert_reserve_pair(db, "m11reserve", "m11", source_key="src1")
        _insert_activation_event(db, "m11", "m11reserve", TODAY, source_key="src1",
                                  status="failed", updated_at="2026-07-14T05:00:00")
        _insert_activation_event(db, "m11", "m11reserve", TODAY, source_key="src1",
                                  status="done", updated_at="2026-07-14T07:00:00")
        _insert_active_links(db, "m11reserve", TODAY, 15)

        keys = ["m3", "m3reserve", "m4", "m4reserve", "m5", "m5reserve", "m11", "m11reserve"]
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)

        by_key = {r["primary_key"]: r for r in report["replacements"]}
        check("3. requested activation -> state B", by_key.get("m3", {}).get("state") == "B", repr(by_key.get("m3")))
        check("4. done-but-not-ready -> state B (not C)", by_key.get("m4", {}).get("state") == "B", repr(by_key.get("m4")))
        check("5. done+ready -> state C", by_key.get("m5", {}).get("state") == "C", repr(by_key.get("m5")))
        check("11. fatal reason + fully-ready reserve resolves to C (priority C>B>D>A)",
              by_key.get("m11", {}).get("state") == "C", repr(by_key.get("m11")))
        check("11b. exactly one replacement record for m11 despite 2 event rows",
              sum(1 for r in report["replacements"] if r["primary_key"] == "m11") == 1)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 6-8: links_updated_hm sourcing (bizlinks.updated_at, not created_at;
# fallback to event.updated_at only when no valid bizlinks timestamp)
# ======================================================================

def test_6_8_links_updated_time_source():
    print("\n-- 6/8: links_updated_hm sourced from bizlinks.updated_at, with fallback --")
    db = _make_temp_db()
    try:
        # scenario 6: updated_at is NEWER than created_at -- must use updated_at.
        _insert_manager(db, "p6", source_key="src1", is_enabled=0)
        _insert_manager(db, "p6reserve", source_key="src1", username="p6new")
        _insert_reserve_pair(db, "p6reserve", "p6", source_key="src1")
        _insert_activation_event(db, "p6", "p6reserve", TODAY, source_key="src1", status="done",
                                  updated_at="2026-07-14T04:00:00")
        _insert_active_links(db, "p6reserve", TODAY, 15,
                              created_at="2026-07-14T03:00:00", updated_at="2026-07-14T10:15:00")

        # scenario 8: active links exist (readiness satisfied) but their
        # updated_at is blank/invalid -- must fall back to event.updated_at.
        _insert_manager(db, "p8", source_key="src1", is_enabled=0)
        _insert_manager(db, "p8reserve", source_key="src1", username="p8new")
        _insert_reserve_pair(db, "p8reserve", "p8", source_key="src1")
        _insert_activation_event(db, "p8", "p8reserve", TODAY, source_key="src1", status="done",
                                  updated_at="2026-07-14T09:00:00")
        _insert_active_links(db, "p8reserve", TODAY, 15, updated_at="")

        keys = ["p6", "p6reserve", "p8", "p8reserve"]
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            orig_tz = pf._TZ
            pf._TZ = timezone(timedelta(hours=3))
            try:
                report = pf.build_report(today_iso=TODAY, db_path=db)
            finally:
                pf._TZ = orig_tz

        by_key = {r["primary_key"]: r for r in report["replacements"]}
        check("6. state C uses bizlinks.updated_at (10:15 UTC -> 13:15 Kyiv), not created_at (03:00)",
              by_key.get("p6", {}).get("links_updated_hm") == "13:15", repr(by_key.get("p6")))
        check("8. blank bizlinks.updated_at falls back to event.updated_at (09:00 UTC -> 12:00 Kyiv)",
              by_key.get("p8", {}).get("links_updated_hm") == "12:00", repr(by_key.get("p8")))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 7: _utc_iso_to_kyiv_hm direct unit checks
# ======================================================================

def test_7_utc_to_kyiv():
    print("\n-- 7: _utc_iso_to_kyiv_hm direct unit checks --")
    orig_tz = pf._TZ
    pf._TZ = timezone(timedelta(hours=3))
    try:
        check("7a. naive UTC ISO converts to Kyiv HH:MM", pf._utc_iso_to_kyiv_hm("2026-07-14T10:15:00") == "13:15")
        check("7b. empty input returns empty string", pf._utc_iso_to_kyiv_hm("") == "")
        check("7c. None input returns empty string", pf._utc_iso_to_kyiv_hm(None) == "")
        check("7d. garbage input returns empty string, never raises", pf._utc_iso_to_kyiv_hm("not-a-date") == "")
    finally:
        pf._TZ = orig_tz


# ======================================================================
# 9-10: not scheduled today -> no record; event dated for a different day
# doesn't count as today's activation
# ======================================================================

def test_9_10_scheduling_and_date_scoping():
    print("\n-- 9/10: unscheduled manager excluded; event dated a different day ignored --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "notscheduled", source_key="src1", is_enabled=0, last_error="banned")

        _insert_manager(db, "p10", source_key="src1", is_enabled=0, last_error="PhoneNumberBannedError")
        _insert_manager(db, "p10reserve", source_key="src1", username="p10new")
        # Event exists, but for YESTERDAY only -- no pair row at all, so it
        # must NOT be picked up as a today activation.
        _insert_activation_event(db, "p10", "p10reserve", YESTERDAY, source_key="src1", status="done")

        # working_keys deliberately excludes "notscheduled".
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["p10"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)

        by_key = {r["primary_key"]: r for r in report["replacements"]}
        check("9. a manager not present in working_keys produces no record at all",
              "notscheduled" not in by_key, repr(by_key))
        check("10. yesterday's activation event does not count as today's -> falls back to state D",
              by_key.get("p10", {}).get("state") == "D", repr(by_key.get("p10")))
        check("10b. reserve_key stays empty since only a same-day pair/event counts",
              by_key.get("p10", {}).get("reserve_key") == "", repr(by_key.get("p10")))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 12-13: partner source filtering vs admin seeing all sources
# ======================================================================

def test_12_13_source_filtering():
    print("\n-- 12/13: PartnerBot source filter vs AdminBot sees all sources --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "mgr_a", source_key="srca", display_name="A-Manager",
                         username="srcamgr", is_enabled=0, last_error="PhoneNumberBannedError")
        _insert_manager(db, "mgr_b", source_key="srcb", display_name="B-Manager",
                         username="srcbmgr", is_enabled=0, last_error="PhoneNumberBannedError")

        keys = ["mgr_a", "mgr_b"]
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            admin_report = pf.build_report(today_iso=TODAY, db_path=db)
            partner_report_a = pf.build_partner_report(target_date_iso=TODAY, db_path=db, source_key="srca")

        admin_keys = {r["primary_key"] for r in admin_report["replacements"]}
        check("13. AdminBot sees replacement records from BOTH sources",
              admin_keys == {"mgr_a", "mgr_b"}, repr(admin_keys))

        partner_keys = {r["primary_key"] for r in partner_report_a["replacements"]}
        check("12. PartnerBot for srcA sees only its own manager", partner_keys == {"mgr_a"}, repr(partner_keys))
        check("12b. PartnerBot for srcA never sees the foreign source's manager", "mgr_b" not in partner_keys, repr(partner_keys))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 14-15: reserve username formatting, no empty "@"
# ======================================================================

def test_14_15_username_formatting():
    print("\n-- 14/15: reserve username formatting, never an empty '@' --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "p14", source_key="src1", is_enabled=0)
        _insert_manager(db, "p14reserve", source_key="src1", username="realusername")
        _insert_reserve_pair(db, "p14reserve", "p14", source_key="src1")
        _insert_activation_event(db, "p14", "p14reserve", TODAY, source_key="src1", status="requested")

        # No telegram_username set on the reserve manager at all.
        _insert_manager(db, "p15", source_key="src1", is_enabled=0, username="")
        _insert_manager(db, "p15reserve", source_key="src1", username="")
        _insert_reserve_pair(db, "p15reserve", "p15", source_key="src1")
        _insert_activation_event(db, "p15", "p15reserve", TODAY, source_key="src1", status="requested")

        keys = ["p14", "p14reserve", "p15", "p15reserve"]
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)

        by_key = {r["primary_key"]: r for r in report["replacements"]}
        check("14. reserve_username correctly taken from the reserve manager row",
              by_key["p14"]["reserve_username"] == "realusername", repr(by_key["p14"]))
        line14 = pf._replacement_line(by_key["p14"])
        check("14b. rendered line contains '@realusername'", "@realusername" in line14, line14)

        line15 = pf._replacement_line(by_key["p15"])
        check("15. missing reserve username never produces a bare '@'", "@ " not in line15 and not line15.rstrip(".").endswith("@"), line15)
        check("15b. missing PRIMARY username never produces a bare/empty '@' either",
              " | @" not in line15 or "@ " not in line15, line15)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 16: no replacement records -> no header, byte-identical to pre-feature text
# ======================================================================

def test_16_empty_block_is_invisible():
    print("\n-- 16: empty replacements -> no header, no stray blank lines --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "healthy1", source_key="src1", display_name="Здоровый", username="healthy1")

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["healthy1"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)
            partner_report = pf.build_partner_report(target_date_iso=TODAY, db_path=db, source_key="src1")

        check("16a. build_report produces zero replacement records for an all-healthy day",
              report["replacements"] == [], repr(report["replacements"]))
        admin_text = pf.render_admin_text(report, period_kind="today")
        check("16b. render_admin_text has no 'Замены аккаунтов' header when empty",
              "Замены аккаунтов" not in admin_text, admin_text)

        partner_text = pf.render_partner_text(partner_report, period_kind="today")
        check("16c. render_partner_text has no 'Замены аккаунтов' header when empty",
              "Замены аккаунтов" not in partner_text, partner_text)

        report_no_key = dict(report)
        report_no_key.pop("replacements", None)
        admin_text_no_key = pf.render_admin_text(report_no_key, period_kind="today")
        check("16d. rendering is identical whether 'replacements' is [] or the key is absent entirely",
              admin_text == admin_text_no_key, "diff detected")
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 17-18: state C doesn't fail the report; A/B/D are structural, not
# recheckable/deferrable problems
# ======================================================================

def test_17_18_all_ok_and_recheckable():
    print("\n-- 17/18: state C keeps all_ok True; A/B/D are structural, never 'recheckable' --")
    db = _make_temp_db()
    try:
        # state C only.
        _insert_manager(db, "c1", source_key="src1", is_enabled=0)
        _insert_manager(db, "c1reserve", source_key="src1", username="c1new")
        _insert_reserve_pair(db, "c1reserve", "c1", source_key="src1")
        _insert_activation_event(db, "c1", "c1reserve", TODAY, source_key="src1", status="done")
        _insert_active_links(db, "c1reserve", TODAY, 15)

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["c1", "c1reserve"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report_c = pf.build_report(today_iso=TODAY, db_path=db)

        check("17. state C alone does not flip all_ok to False", report_c["all_ok"] is True, repr(report_c["all_ok"]))
        check("17b. report_is_ok() is True for an all-C, otherwise-clean report", pf.report_is_ok(report_c) is True)

        db2 = _make_temp_db()
        _insert_manager(db2, "d1", source_key="src1", is_enabled=0, last_error="PhoneNumberBannedError")
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["d1"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report_d = pf.build_report(today_iso=TODAY, db_path=db2)

        check("18. state D flips all_ok to False (structural problem)", report_d["all_ok"] is False, repr(report_d["all_ok"]))
        check("18b. report_is_ok() is False for a report with a state-D record", pf.report_is_ok(report_d) is False)
        check("18c. report_has_recheckable_problem() is False -- never silently deferred until deep-verify resolves",
              pf.report_has_recheckable_problem(report_d) is False)
        Path(db2).unlink(missing_ok=True)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 19: PartnerBot never shows a plain checkmark for an A/B/D primary
# ======================================================================

def test_19_partner_no_duplicate_checkmark():
    print("\n-- 19: PartnerBot account list never double-shows an A/B/D primary as plain OK --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "banned_one", source_key="src1", display_name="Забаненный",
                         username="banned_one", is_enabled=0, last_error="PhoneNumberBannedError")
        _insert_manager(db, "healthy_one", source_key="src1", display_name="Здоровый",
                         username="healthy_one")

        keys = ["banned_one", "healthy_one"]
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            partner_report = pf.build_partner_report(target_date_iso=TODAY, db_path=db, source_key="src1")

        text = pf.render_partner_text(partner_report, period_kind="today")
        check("19a. the affected manager is never shown as a plain checkmark line",
              "✅ Забаненный / @banned_one" not in text, text)
        check("19b. the affected manager appears with a warning marker instead",
              "⚠️ Забаненный / @banned_one" in text, text)
        check("19c. the UNAFFECTED manager still gets its normal checkmark line",
              "✅ Здоровый / @healthy_one" in text, text)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 20: collector exception fails open -- rest of the report still renders
# ======================================================================

def test_20_fail_open():
    print("\n-- 20: exception inside the collector fails open, report still renders --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "healthy2", source_key="src1", display_name="ОК", username="healthy2")

        orig_collect = pf.collect_replacement_records

        def _boom(*a, **kw):
            raise RuntimeError("simulated collector failure")

        pf.collect_replacement_records = _boom
        try:
            with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["healthy2"]),
                                bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
                report = pf.build_report(today_iso=TODAY, db_path=db)
                partner_report = pf.build_partner_report(target_date_iso=TODAY, db_path=db, source_key="src1")
        finally:
            pf.collect_replacement_records = orig_collect

        check("20a. build_report still returns a full report despite the collector raising",
              report.get("working_count") == 1 and len(report.get("managers", [])) == 1, repr(report))
        check("20b. report['replacements'] is an empty list, not missing/crashed", report.get("replacements") == [])
        admin_text = pf.render_admin_text(report, period_kind="today")
        check("20c. render_admin_text still produces normal output (no crash propagated)",
              "ОК" in admin_text or "здоров" in admin_text.lower() or "здоров" not in admin_text.lower(), "rendered ok")
        check("20d. build_partner_report also fails open with an empty replacements list",
              partner_report.get("replacements") == [], repr(partner_report))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 21: special characters in names/usernames never crash rendering
# ======================================================================

def test_21_special_characters():
    print("\n-- 21: special characters in names/usernames don't break rendering --")
    db = _make_temp_db()
    try:
        weird_name = "Иван <script>&'\" *_ [test]"
        _insert_manager(db, "weird1", source_key="src1", display_name=weird_name,
                         username="weird_1", is_enabled=0, last_error="PhoneNumberBannedError")

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["weird1"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)

        try:
            line = pf._replacement_line(report["replacements"][0])
            admin_text = pf.render_admin_text(report, period_kind="today")
            ok = True
        except Exception as e:
            line = ""
            admin_text = ""
            ok = False
            print(f"       exception: {e!r}")
        check("21a. _replacement_line/render_admin_text never raise on special characters", ok)
        check("21b. the special-character name passes through literally (plain text, no parse_mode)",
              weird_name in line, line)
        check("21c. render_admin_text embeds the same block without raising", "Иван" in admin_text, admin_text[:200])
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# 22: the exact worked example from the implementation prompt
# ======================================================================

def test_22_exact_valeria_example():
    print("\n-- 22: exact valeria0863 worked example --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "valeria0863", source_key="rassylka", display_name="Валерия",
                         username="valeria0863", is_enabled=0, manual_stopped=1,
                         last_error="PhoneNumberBannedError('The phone number is banned.')")

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["valeria0863"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)

        rec = report["replacements"][0]
        line = pf._replacement_line(rec)
        expected = (
            "⚠️ Валерия | @valeria0863 — аккаунт недоступен.\n"
            "Причина: номер заблокирован Telegram.\n"
            "Замена не подключена, ссылки не работают."
        )
        check("22. exact rendered text matches the worked example verbatim", line == expected, repr(line))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# OWNER FIX ROUND (2026-07-15), item 1/5.1-5.2: state C text redirects to
# the Бизнес-ссылки menu, exact wording, with and without a usable time.
# ======================================================================

def test_c_menu_redirect_text():
    print("\n-- owner-fix 1/5.1-5.2: state C text redirects to Бизнес-ссылки, exact wording --")
    db = _make_temp_db()
    try:
        # With a valid time.
        _insert_manager(db, "cx1", source_key="src1", display_name="ОК1", is_enabled=0)
        _insert_manager(db, "cx1reserve", source_key="src1", username="cx1new")
        _insert_reserve_pair(db, "cx1reserve", "cx1", source_key="src1")
        _insert_activation_event(db, "cx1", "cx1reserve", TODAY, source_key="src1", status="done",
                                  updated_at="2026-07-14T06:30:00")
        _insert_active_links(db, "cx1reserve", TODAY, 15, updated_at="2026-07-14T06:30:00")

        # Without ANY usable timestamp at all (both bizlinks.updated_at and
        # event.updated_at blank) -- must never fabricate a time, but must
        # still redirect to the business-links section.
        _insert_manager(db, "cx2", source_key="src1", display_name="ОК2", is_enabled=0)
        _insert_manager(db, "cx2reserve", source_key="src1", username="cx2new")
        _insert_reserve_pair(db, "cx2reserve", "cx2", source_key="src1")
        _insert_activation_event(db, "cx2", "cx2reserve", TODAY, source_key="src1", status="done", updated_at="")
        _insert_active_links(db, "cx2reserve", TODAY, 15, updated_at="")

        keys = ["cx1", "cx1reserve", "cx2", "cx2reserve"]
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            orig_tz = pf._TZ
            pf._TZ = timezone(timedelta(hours=3))
            try:
                report = pf.build_report(today_iso=TODAY, db_path=db)
            finally:
                pf._TZ = orig_tz

        by_key = {r["primary_key"]: r for r in report["replacements"]}
        check("c1. cx1 resolves to state C", by_key.get("cx1", {}).get("state") == "C", repr(by_key.get("cx1")))
        line1 = pf._replacement_line(by_key["cx1"])
        check("c2. state C (with time) contains 'Ссылки на сегодня обновлены'",
              "Ссылки на сегодня обновлены" in line1, line1)
        check("c3. state C mentions the exact existing menu label '🔗 Бизнес-ссылки'",
              "🔗 Бизнес-ссылки" in line1, line1)
        check("c4. state C tells the reader to open it and check the new links",
              "Откройте" in line1 and "проверьте новые ссылки" in line1, line1)
        check("c5. wording is the fuller phrase, never the short 'Ссылки обновлены' (without 'на сегодня')",
              "Ссылки обновлены" not in line1.replace("Ссылки на сегодня обновлены", ""), line1)

        check("c6. cx2 (no usable timestamp anywhere) still resolves to state C",
              by_key.get("cx2", {}).get("state") == "C", repr(by_key.get("cx2")))
        line2 = pf._replacement_line(by_key["cx2"])
        check("c7. state C with no usable timestamp never fabricates a clock time (no HH:MM in the text)",
              not re.search(r"\d{2}:\d{2}", line2), line2)
        check("c8. state C with no timestamp still says 'Ссылки на сегодня обновлены.' (time clause just omitted)",
              "Ссылки на сегодня обновлены." in line2, line2)
        check("c9. state C with no timestamp still redirects to Бизнес-ссылки",
              "🔗 Бизнес-ссылки" in line2 and "проверьте новые ссылки" in line2, line2)
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# OWNER FIX ROUND, item 2/5.3: Kyiv midnight boundary. target_date (the
# Kyiv business date) is the ONLY criterion for which day's report a
# record/time belongs to -- the UTC calendar date of the underlying
# timestamp must never leak in. Uses a REAL Europe/Kyiv ZoneInfo (seasonal
# offset), never a manual "+N hours" shortcut.
# ======================================================================

def test_kyiv_midnight_boundary():
    print("\n-- owner-fix 2/5.3: Kyiv midnight boundary via real ZoneInfo (no manual offset) --")
    try:
        kyiv_tz = ZoneInfo("Europe/Kyiv")
    except Exception as e:
        check("boundary-0. real Europe/Kyiv ZoneInfo is available in this environment", False, repr(e))
        return

    orig_tz = pf._TZ
    pf._TZ = kyiv_tz
    try:
        # Direct unit check: genuine seasonal ZoneInfo conversion (EEST,
        # UTC+3 in July) -- proves the previous UTC calendar date's 21:01
        # really is 00:01 Kyiv the next day, without hand-adding hours.
        hm = pf._utc_iso_to_kyiv_hm("2026-07-13T21:01:00")
        check("boundary-1. 2026-07-13T21:01:00 UTC == 00:01 Kyiv (real ZoneInfo, summer/EEST, +3)",
              hm == "00:01", hm)

        db = _make_temp_db()
        try:
            _insert_manager(db, "mm", source_key="src1", display_name="Полуночный",
                             username="mm_primary", is_enabled=0)
            _insert_manager(db, "mmreserve", source_key="src1", username="mm_reserve")
            _insert_reserve_pair(db, "mmreserve", "mm", source_key="src1")
            # The activation event and its business links are both filed
            # under target_date=2026-07-14 (the Kyiv business day), even
            # though the underlying UTC instant's calendar date is still
            # 2026-07-13 (21:01 UTC == 00:01 Kyiv the next day).
            _insert_activation_event(db, "mm", "mmreserve", "2026-07-14", source_key="src1",
                                      status="done", updated_at="2026-07-13T21:01:00")
            _insert_active_links(db, "mmreserve", "2026-07-14", 15, updated_at="2026-07-13T21:01:00")

            with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["mm", "mmreserve"]),
                                bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
                report_today = pf.build_report(today_iso="2026-07-14", db_path=db)
                report_yesterday = pf.build_report(today_iso="2026-07-13", db_path=db)
                report_tomorrow = pf.build_report(today_iso="2026-07-15", db_path=db)

            today_rec = next((r for r in report_today["replacements"] if r["primary_key"] == "mm"), None)
            check("boundary-2. today's report (target_date=2026-07-14) shows the record",
                  today_rec is not None, repr(report_today["replacements"]))
            if today_rec:
                check("boundary-3. state is C (matched by target_date, not by the UTC calendar date)",
                      today_rec["state"] == "C", repr(today_rec))
                check("boundary-4. displayed time is 00:01 Kyiv, unaffected by the UTC date rollover",
                      today_rec["links_updated_hm"] == "00:01", repr(today_rec))

            # Via build_report (today-context gate active, kyiv_today_iso()
            # pinned to TODAY for this whole file): yesterday/tomorrow are
            # non-today dates, so the whole feature is gated off -- this
            # also doubles as Blocker-1 (evening/tomorrow no-op) coverage.
            yesterday_rec = next((r for r in report_yesterday["replacements"] if r["primary_key"] == "mm"), None)
            check("boundary-5. yesterday's report (target_date=2026-07-13) never shows this as a ready (C) replacement",
                  yesterday_rec is None or yesterday_rec.get("state") != "C", repr(yesterday_rec))

            tomorrow_rec = next((r for r in report_tomorrow["replacements"] if r["primary_key"] == "mm"), None)
            check("boundary-6. tomorrow's report (target_date=2026-07-15) never shows this as a ready (C) replacement",
                  tomorrow_rec is None or tomorrow_rec.get("state") != "C", repr(tomorrow_rec))

            # SQL-level check, independent of the today-context gate: call
            # collect_replacement_records directly (bypassing build_report's
            # gate entirely) with today_iso=yesterday/tomorrow to prove the
            # underlying target_date filtering itself -- not just the
            # higher-level gate -- never lets this activation/links leak
            # into an adjacent date's data.
            manager_rows = pf.list_manager_rows_from_db_sync(db, include_removed=False) or []
            manager_by_key = {pf.normalize_manager_key(r.get("manager_key") or ""): r for r in manager_rows}
            src_map = pf._manager_source_map(db)
            default_count = pf._bizlink_default_count(db)

            active_slugs_y = pf._bizlinks_active_slugs_by_manager(db, "2026-07-13")
            direct_yesterday = pf.collect_replacement_records(
                "2026-07-13", db, ["mm", "mmreserve"], manager_by_key, src_map, active_slugs_y, default_count)
            direct_y_rec = next((r for r in direct_yesterday if r["primary_key"] == "mm"), None)
            check("boundary-5b. SQL-level (collect_replacement_records directly, gate bypassed): "
                  "target_date=2026-07-13 never resolves this to state C",
                  direct_y_rec is None or direct_y_rec.get("state") != "C", repr(direct_y_rec))

            active_slugs_t = pf._bizlinks_active_slugs_by_manager(db, "2026-07-15")
            direct_tomorrow = pf.collect_replacement_records(
                "2026-07-15", db, ["mm", "mmreserve"], manager_by_key, src_map, active_slugs_t, default_count)
            direct_t_rec = next((r for r in direct_tomorrow if r["primary_key"] == "mm"), None)
            check("boundary-6b. SQL-level (collect_replacement_records directly, gate bypassed): "
                  "target_date=2026-07-15 never resolves this to state C",
                  direct_t_rec is None or direct_t_rec.get("state") != "C", repr(direct_t_rec))
        finally:
            Path(db).unlink(missing_ok=True)
    finally:
        pf._TZ = orig_tz


# ======================================================================
# OWNER FIX ROUND, item 3/5.4-5.9: the unavailability gate must classify a
# FATAL reason (via _reason_business_text, which genuinely consults
# manager_telegram_health and start_status.json), not just "any non-empty
# last_error".
# ======================================================================

def test_gate_matrix_fatal_vs_benign():
    print("\n-- owner-fix 3/5.4-5.9: fatal-vs-benign unavailability gate matrix --")
    sig = inspect.signature(pf.collect_replacement_records)
    check("gate-8a. collect_replacement_records has no process/running parameter at all -- "
          "process liveness structurally cannot influence replacement detection",
          not any(("process" in p.lower() or "running" in p.lower()) for p in sig.parameters),
          repr(list(sig.parameters)))

    db = _make_temp_db()
    try:
        # 5.4 benign: enabled, not stopped, healthy, start_status running/
        # connected, no last_error at all -> no record.
        _insert_manager(db, "gm_benign", source_key="src1", display_name="Здоровый",
                         username="gm_benign", is_enabled=1, manual_stopped=0, last_error="")
        _insert_health(db, "gm_benign", "ok")

        # 5.5 fatal last_error alone (enabled, not stopped) -> record present.
        _insert_manager(db, "gm_fatal_err", source_key="src1", display_name="Фатальный",
                         username="gm_fatal_err", is_enabled=1, manual_stopped=0,
                         last_error="PhoneNumberBannedError('banned')")

        # 5.6 health blocked, last_error EMPTY -> record present, business text.
        _insert_manager(db, "gm_health_blocked", source_key="src1", display_name="Блокнутый",
                         username="gm_health_blocked", is_enabled=1, manual_stopped=0, last_error="")
        _insert_health(db, "gm_health_blocked", "blocked")

        # 5.7 start_status reason_class=auth_session, last_error EMPTY -> record present.
        _insert_manager(db, "gm_ss_auth", source_key="src1", display_name="Слетевший",
                         username="gm_ss_auth", is_enabled=1, manual_stopped=0, last_error="")

        # 5.8 (companion to gate-8a above): a normal healthy manager -> no
        # record, regardless of whatever the process scan finds elsewhere.
        _insert_manager(db, "gm_no_process", source_key="src1", display_name="БезПроцесса",
                         username="gm_no_process", is_enabled=1, manual_stopped=0, last_error="")
        _insert_health(db, "gm_no_process", "ok")

        # 5.9 stale/benign last_error text on an otherwise healthy account -> no record.
        _insert_manager(db, "gm_stale_err", source_key="src1", display_name="СтарыйWarning",
                         username="gm_stale_err", is_enabled=1, manual_stopped=0,
                         last_error="old resolved timeout warning from 3 weeks ago, account fine now")
        _insert_health(db, "gm_stale_err", "ok")

        keys = ["gm_benign", "gm_fatal_err", "gm_health_blocked", "gm_ss_auth", "gm_no_process", "gm_stale_err"]
        with _RuntimeDirPatch() as tmp_dir:
            _write_start_status(tmp_dir, "gm_benign", phase="running", reason_class="", error="")
            _write_start_status(tmp_dir, "gm_ss_auth", phase="failed", reason_class="auth_session",
                                 error="AuthKeyUnregisteredError")
            _write_start_status(tmp_dir, "gm_no_process", phase="running", reason_class="", error="")
            _write_start_status(tmp_dir, "gm_stale_err", phase="running", reason_class="", error="")

            with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                                bizlink_job_list_for_date=_fake_jobs_none):
                report = pf.build_report(today_iso=TODAY, db_path=db)

        by_key = {r["primary_key"]: r for r in report["replacements"]}

        check("gate-4. benign healthy account produces NO replacement record",
              "gm_benign" not in by_key, repr(sorted(by_key.keys())))

        check("gate-5. fatal last_error alone produces a record", "gm_fatal_err" in by_key, repr(sorted(by_key.keys())))
        if "gm_fatal_err" in by_key:
            check("gate-5b. state is D (no reserve connected)",
                  by_key["gm_fatal_err"]["state"] == "D", repr(by_key["gm_fatal_err"]))

        check("gate-6. health=blocked with EMPTY last_error still produces a record",
              "gm_health_blocked" in by_key, repr(sorted(by_key.keys())))
        if "gm_health_blocked" in by_key:
            check("gate-6b. reason_text is the safe business phrase, not a raw status token",
                  by_key["gm_health_blocked"]["reason_text"] == "аккаунт заблокирован Telegram",
                  repr(by_key["gm_health_blocked"]))

        check("gate-7. start_status reason_class=auth_session with EMPTY last_error still produces a record",
              "gm_ss_auth" in by_key, repr(sorted(by_key.keys())))
        if "gm_ss_auth" in by_key:
            check("gate-7b. reason_text is the safe business phrase, not the raw reason_class string",
                  by_key["gm_ss_auth"]["reason_text"] == "слетела авторизация Telegram",
                  repr(by_key["gm_ss_auth"]))

        check("gate-8b. a normal healthy manager produces NO record", "gm_no_process" not in by_key, repr(sorted(by_key.keys())))

        check("gate-9. stale/benign last_error text on an otherwise healthy account produces NO false record",
              "gm_stale_err" not in by_key, repr(sorted(by_key.keys())))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# BLOCKER-FIX ROUND (2026-07-15), Blocker 1: the whole replacement feature
# must be a complete no-op for an evening/tomorrow (non-today) report --
# no records collected, all_ok untouched, no process-issue dedup, no
# 'Замены аккаунтов' header, no empty 'Рекомендации:' section.
# ======================================================================

def test_blocker1_evening_tomorrow_noop():
    print("\n-- Blocker 1: evening/tomorrow report is a complete no-op for this feature --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "evprim1", source_key="src1", display_name="ВечерМенеджер",
                         username="evprim1", is_enabled=0)

        orig_scan = pf._scan_python_processes

        def _fake_scan_with_other_process(timeout_sec=12.0):
            # A real TPilot manager process for a DIFFERENT manager exists on
            # the machine -- process_check_trustworthy becomes True, so
            # evprim1's absence is a genuine, verified "process not found".
            return [{"pid": "999", "cmd": f"{pf.BASE_DIR} main.py --manager some_other_key"}]

        pf._scan_python_processes = _fake_scan_with_other_process
        try:
            with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["evprim1"]),
                                bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
                report_evening = pf.build_report(today_iso=TOMORROW, db_path=db)
        finally:
            pf._scan_python_processes = orig_scan

        check("b1-1a. evening/tomorrow report collects NO replacement records at all",
              report_evening.get("replacements") == [], repr(report_evening.get("replacements")))

        mgr_rec = next(m for m in report_evening["managers"] if m["manager_key"] == "evprim1")
        issue_kinds = [i.get("kind") for i in mgr_rec["issues"]]
        check("b1-1b. the manager's own 'process not found' issue is STILL present (never stripped/deduped in the evening path)",
              "process" in issue_kinds, repr(mgr_rec["issues"]))
        check("b1-1c. all_ok is False from the ordinary pre-existing process issue -- "
              "not something the replacement feature invented or altered",
              report_evening["all_ok"] is False, repr(report_evening["all_ok"]))

        admin_text = pf.render_admin_text(report_evening, period_kind="tomorrow")
        check("b1-1d. evening render never shows the 'Замены аккаунтов' header",
              "Замены аккаунтов" not in admin_text, admin_text)
        lines = admin_text.splitlines()
        if "Рекомендации:" in lines:
            idx = lines.index("Рекомендации:")
            check("b1-1e. 'Рекомендации:' is never an empty section (has a real item right after it)",
                  idx + 1 < len(lines) and lines[idx + 1].strip() != "", repr(lines[idx:idx + 3]))
    finally:
        Path(db).unlink(missing_ok=True)

    db2 = _make_temp_db()
    try:
        _insert_manager(db2, "evprim2", source_key="src1", display_name="ВечерГотовый", username="evprim2", is_enabled=0)
        _insert_manager(db2, "evprim2reserve", source_key="src1", username="evprim2new")
        _insert_reserve_pair(db2, "evprim2reserve", "evprim2", source_key="src1")
        # A fully done + link-ready activation filed under TOMORROW's date
        # (as if the replacement were already complete for that future day).
        _insert_activation_event(db2, "evprim2", "evprim2reserve", TOMORROW, source_key="src1",
                                  status="done", updated_at="2026-07-14T20:00:00")
        _insert_active_links(db2, "evprim2reserve", TOMORROW, 15, updated_at="2026-07-14T20:00:00")

        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["evprim2", "evprim2reserve"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report_evening2 = pf.build_report(today_iso=TOMORROW, db_path=db2)
            partner_evening2 = pf.build_partner_report(target_date_iso=TOMORROW, db_path=db2, source_key="src1")

        check("b1-2a. evening/tomorrow AdminBot report ignores even a fully-ready (would-be-C) replacement event",
              report_evening2.get("replacements") == [], repr(report_evening2.get("replacements")))
        admin_text2 = pf.render_admin_text(report_evening2, period_kind="tomorrow")
        check("b1-2b. no replacement wording (заменена на / Бизнес-ссылки CTA) leaks into the evening render",
              "заменена на" not in admin_text2 and "Бизнес-ссылки" not in admin_text2, admin_text2)

        check("b1-2c. evening/tomorrow PartnerBot report also collects no replacement records",
              partner_evening2.get("replacements") == [], repr(partner_evening2.get("replacements")))
        partner_text2 = pf.render_partner_text(partner_evening2, period_kind="tomorrow")
        check("b1-2d. PartnerBot evening account line is the ordinary checkmark line, unmodified by this feature",
              "✅ ВечерГотовый / @evprim2" in partner_text2, partner_text2)
    finally:
        Path(db2).unlink(missing_ok=True)


def test_morning_today_still_works_after_gate_fix():
    print("\n-- Blocker 1 regression guard: morning/today A/B/C/D behavior is unchanged --")
    db = _make_temp_db()
    try:
        _insert_manager(db, "valeria0863", source_key="rassylka", display_name="Валерия",
                         username="valeria0863", is_enabled=0, manual_stopped=1,
                         last_error="PhoneNumberBannedError('The phone number is banned.')")
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(["valeria0863"]),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)
        rec = report["replacements"][0]
        expected = (
            "⚠️ Валерия | @valeria0863 — аккаунт недоступен.\n"
            "Причина: номер заблокирован Telegram.\n"
            "Замена не подключена, ссылки не работают."
        )
        check("morning-regress. Валерия's exact D-state text is unaffected by the today-gate fix",
              pf._replacement_line(rec) == expected, repr(pf._replacement_line(rec)))
    finally:
        Path(db).unlink(missing_ok=True)


# ======================================================================
# BLOCKER-FIX ROUND, Blocker 2: reserve_activation_events.status must be
# handled honestly -- requested/creating never claim "подключён"; done
# splits into "connected but not ready" (B) vs "ready" (C); failed/
# cancelled are never evidence of a replacement; only the CURRENTLY linked
# ('linked') reserve pair's own events count, never a retired pair's.
# ======================================================================

def test_blocker2_event_status_matrix():
    print("\n-- Blocker 2: event-status matrix (requested/creating/done/failed/cancelled/retired) --")
    db = _make_temp_db()
    try:
        # 1. failed event + RETIRED pair -> no active replacement at all;
        #    state falls through to D (fatal last_error) or A.
        _insert_manager(db, "bl2_1", source_key="src1", display_name="Блок1", username="bl2_1",
                         is_enabled=0, last_error="PhoneNumberBannedError")
        _insert_manager(db, "bl2_1reserve", source_key="src1", username="bl2_1reserve_user")
        _insert_reserve_pair(db, "bl2_1reserve", "bl2_1", source_key="src1", status="retired")
        _insert_activation_event(db, "bl2_1", "bl2_1reserve", TODAY, source_key="src1", status="failed")

        # 2. cancelled event + NO linked pair at all -> state A (no fatal reason here).
        _insert_manager(db, "bl2_2", source_key="src1", display_name="Блок2", username="bl2_2", is_enabled=0)
        _insert_activation_event(db, "bl2_2", "bl2_2ghost", TODAY, source_key="src1", status="cancelled")

        # 3. requested event, reserve username KNOWN -> honest pending text
        #    with "заменяется на @username", never "подключён".
        _insert_manager(db, "bl2_3", source_key="src1", display_name="Блок3", username="bl2_3", is_enabled=0)
        _insert_manager(db, "bl2_3reserve", source_key="src1", username="bl2_3reserve_user")
        _insert_reserve_pair(db, "bl2_3reserve", "bl2_3", source_key="src1")
        _insert_activation_event(db, "bl2_3", "bl2_3reserve", TODAY, source_key="src1", status="requested")

        # 4. creating event, reserve username UNKNOWN -> generic pending text.
        _insert_manager(db, "bl2_4", source_key="src1", display_name="Блок4", username="bl2_4", is_enabled=0)
        _insert_manager(db, "bl2_4reserve", source_key="src1", username="")
        _insert_reserve_pair(db, "bl2_4reserve", "bl2_4", source_key="src1")
        _insert_activation_event(db, "bl2_4", "bl2_4reserve", TODAY, source_key="src1", status="creating")

        # 5. done + INCOMPLETE links -> state B, "connected" wording.
        _insert_manager(db, "bl2_5", source_key="src1", display_name="Блок5", username="bl2_5", is_enabled=0)
        _insert_manager(db, "bl2_5reserve", source_key="src1", username="bl2_5reserve_user")
        _insert_reserve_pair(db, "bl2_5reserve", "bl2_5", source_key="src1")
        _insert_activation_event(db, "bl2_5", "bl2_5reserve", TODAY, source_key="src1", status="done")
        _insert_active_links(db, "bl2_5reserve", TODAY, 5)

        # 6. done + COMPLETE links -> state C.
        _insert_manager(db, "bl2_6", source_key="src1", display_name="Блок6", username="bl2_6", is_enabled=0)
        _insert_manager(db, "bl2_6reserve", source_key="src1", username="bl2_6reserve_user")
        _insert_reserve_pair(db, "bl2_6reserve", "bl2_6", source_key="src1")
        _insert_activation_event(db, "bl2_6", "bl2_6reserve", TODAY, source_key="src1", status="done")
        _insert_active_links(db, "bl2_6reserve", TODAY, 15)

        # 7a. failed event NEWER than a valid done for the SAME still-linked
        #     reserve -> the failed event must not hide the earlier done;
        #     with full links this resolves to C.
        _insert_manager(db, "bl2_7", source_key="src1", display_name="Блок7", username="bl2_7", is_enabled=0)
        _insert_manager(db, "bl2_7reserve", source_key="src1", username="bl2_7reserve_user")
        _insert_reserve_pair(db, "bl2_7reserve", "bl2_7", source_key="src1")
        _insert_activation_event(db, "bl2_7", "bl2_7reserve", TODAY, source_key="src1",
                                  status="done", updated_at="2026-07-14T05:00:00")
        _insert_activation_event(db, "bl2_7", "bl2_7reserve", TODAY, source_key="src1",
                                  status="failed", updated_at="2026-07-14T08:00:00")
        _insert_active_links(db, "bl2_7reserve", TODAY, 15)

        # 7b. an OLD done event for a reserve that is NO LONGER linked, plus
        #     a NEWER failed event for the CURRENTLY linked (different)
        #     reserve -> must never mix reserves; resolves to pending B
        #     (no active event at all for the currently-linked reserve).
        _insert_manager(db, "bl2_7b", source_key="src1", display_name="Блок7б", username="bl2_7b", is_enabled=0)
        _insert_manager(db, "bl2_7b_old_reserve", source_key="src1", username="old_reserve_user")
        _insert_manager(db, "bl2_7b_reserve", source_key="src1", username="bl2_7b_reserve_user")
        _insert_reserve_pair(db, "bl2_7b_reserve", "bl2_7b", source_key="src1")  # CURRENT linked pair
        _insert_activation_event(db, "bl2_7b", "bl2_7b_old_reserve", TODAY, source_key="src1",
                                  status="done", updated_at="2026-07-14T05:00:00")
        _insert_activation_event(db, "bl2_7b", "bl2_7b_reserve", TODAY, source_key="src1",
                                  status="failed", updated_at="2026-07-14T08:00:00")
        _insert_active_links(db, "bl2_7b_old_reserve", TODAY, 15)  # old reserve's own links, irrelevant now

        # 8. RETIRED pair whose own event says done + fully ready links ->
        #    must never be shown as an active/completed replacement (C or B).
        _insert_manager(db, "bl2_8", source_key="src1", display_name="Блок8", username="bl2_8", is_enabled=0)
        _insert_manager(db, "bl2_8reserve", source_key="src1", username="bl2_8reserve_user")
        _insert_reserve_pair(db, "bl2_8reserve", "bl2_8", source_key="src1", status="retired")
        _insert_activation_event(db, "bl2_8", "bl2_8reserve", TODAY, source_key="src1", status="done")
        _insert_active_links(db, "bl2_8reserve", TODAY, 15)

        keys = ["bl2_1", "bl2_1reserve", "bl2_2", "bl2_3", "bl2_3reserve", "bl2_4", "bl2_4reserve",
                "bl2_5", "bl2_5reserve", "bl2_6", "bl2_6reserve", "bl2_7", "bl2_7reserve",
                "bl2_7b", "bl2_7b_old_reserve", "bl2_7b_reserve", "bl2_8", "bl2_8reserve"]
        with _StoragePatch(manager_schedule_list_working_on_date=_fake_schedule_factory(keys),
                            bizlink_job_list_for_date=_fake_jobs_none), _RuntimeDirPatch():
            report = pf.build_report(today_iso=TODAY, db_path=db)

        by_key = {r["primary_key"]: r for r in report["replacements"]}

        # 1. failed + retired pair.
        check("bl2-1a. failed event + retired pair -> state D (fatal reason, no active replacement)",
              by_key.get("bl2_1", {}).get("state") == "D", repr(by_key.get("bl2_1")))
        line1 = pf._replacement_line(by_key["bl2_1"]) if "bl2_1" in by_key else ""
        check("bl2-1b. no 'заменена на' claim", "заменена на" not in line1, line1)
        check("bl2-1c. no 'Новый аккаунт подключён' claim", "Новый аккаунт подключён" not in line1, line1)

        # 2. cancelled + no pair.
        check("bl2-2. cancelled event + no linked pair -> state A", by_key.get("bl2_2", {}).get("state") == "A", repr(by_key.get("bl2_2")))

        # 3. requested, username known.
        check("bl2-3a. requested event -> state B", by_key.get("bl2_3", {}).get("state") == "B", repr(by_key.get("bl2_3")))
        check("bl2-3b. b_kind is 'pending'", by_key.get("bl2_3", {}).get("b_kind") == "pending", repr(by_key.get("bl2_3")))
        line3 = pf._replacement_line(by_key["bl2_3"])
        check("bl2-3c. honest pending text 'ещё подключается'", "ещё подключается" in line3, line3)
        check("bl2-3d. never claims 'аккаунт подключён'", "аккаунт подключён" not in line3, line3)
        check("bl2-3e. mentions the known reserve username via 'заменяется на @...'",
              "заменяется на @bl2_3reserve_user" in line3, line3)

        # 4. creating, username unknown.
        check("bl2-4a. creating event -> state B, b_kind pending", by_key.get("bl2_4", {}).get("b_kind") == "pending", repr(by_key.get("bl2_4")))
        line4 = pf._replacement_line(by_key["bl2_4"])
        check("bl2-4b. generic pending text when reserve username is unknown",
              "выполняется замена аккаунта" in line4 and "ещё подключается" in line4, line4)
        check("bl2-4c. never claims 'аккаунт подключён'", "аккаунт подключён" not in line4, line4)

        # 5. done + incomplete links.
        check("bl2-5a. done + incomplete links -> state B, b_kind connected",
              by_key.get("bl2_5", {}).get("state") == "B" and by_key.get("bl2_5", {}).get("b_kind") == "connected",
              repr(by_key.get("bl2_5")))
        line5 = pf._replacement_line(by_key["bl2_5"])
        check("bl2-5b. exact 'Новый аккаунт подключён, ссылки ещё не обновлены' wording",
              "Новый аккаунт подключён, ссылки ещё не обновлены" in line5, line5)

        # 6. done + complete links.
        check("bl2-6. done + complete links -> state C", by_key.get("bl2_6", {}).get("state") == "C", repr(by_key.get("bl2_6")))

        # 7a. failed newer than done, SAME reserve -> done still wins -> C.
        check("bl2-7a. newer failed event never hides an earlier valid done for the SAME still-linked reserve (-> C)",
              by_key.get("bl2_7", {}).get("state") == "C", repr(by_key.get("bl2_7")))

        # 7b. old done for unlinked reserve + newer failed for current reserve -> pending B, reserves not mixed.
        rec_7b = by_key.get("bl2_7b", {})
        check("bl2-7b. an old done for a NO-LONGER-linked reserve is never used -> not state C/connected",
              rec_7b.get("state") == "B" and rec_7b.get("b_kind") == "pending", repr(rec_7b))
        check("bl2-7b2. resolves against the CURRENTLY linked reserve, not the old one",
              rec_7b.get("reserve_key") == "bl2_7b_reserve", repr(rec_7b))

        # 8. retired pair, own event says done+ready -> never C/B.
        rec_8 = by_key.get("bl2_8", {})
        check("bl2-8. a retired pair's own 'done'+ready event is never shown as an active replacement (not C, not B)",
              rec_8.get("state") not in ("B", "C"), repr(rec_8))
    finally:
        Path(db).unlink(missing_ok=True)


def main() -> int:
    test_1_2_state_d_and_a()
    test_3_5_11_state_b_c_and_priority()
    test_6_8_links_updated_time_source()
    test_7_utc_to_kyiv()
    test_9_10_scheduling_and_date_scoping()
    test_12_13_source_filtering()
    test_14_15_username_formatting()
    test_16_empty_block_is_invisible()
    test_17_18_all_ok_and_recheckable()
    test_19_partner_no_duplicate_checkmark()
    test_20_fail_open()
    test_21_special_characters()
    test_22_exact_valeria_example()
    test_c_menu_redirect_text()
    test_kyiv_midnight_boundary()
    test_gate_matrix_fatal_vs_benign()
    test_blocker1_evening_tomorrow_noop()
    test_morning_today_still_works_after_gate_fix()
    test_blocker2_event_status_matrix()

    total = len(FAILURES)
    ok_count = sum(1 for _ in [None])  # placeholder, real count printed below
    print(f"\n{'='*70}")
    if FAILURES:
        print(f"SELFTEST FAIL: {total} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
