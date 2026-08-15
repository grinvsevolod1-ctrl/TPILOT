# -*- coding: utf-8 -*-
"""tools/proxy_lifecycle_storage_selftest.py -- offline self-test for the
PROXY LIFECYCLE SYNC 20260721 storage layer (bidirectional desired/observed
provider auto_renew reconciliation).

Uses a throwaway temporary SQLite file only -- never touches the real project
DB. Pure/offline: no network, no Telegram, no provider, no spend.

Proves:
  - the additive migration columns + proxy_lifecycle_events table appear,
    idempotently, on both a fresh DB and a legacy proxy_leases DB that predates
    them;
  - proxy_lease_set_provider_state writes only the supplied desired/observed/
    provider_sync_at fields;
  - proxy_lease_set_lifecycle CAS: from_state guard grants exactly one winner,
    a losing/duplicate transition is a safe no-op (False), from_state=None
    forces the set;
  - proxy_lifecycle_event_add is append-only and never stores a proxy password;
  - proxy_lifecycle_list_actionable returns only enable_required/disable_required;
  - proxy_lifecycle_events_for_lease returns the full ordered history.

    python tools\\proxy_lifecycle_storage_selftest.py
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

import aiosqlite
import storage

FAILURES: list[str] = []
SECRET_PASSWORD = "SECRET_PROXY_PW_do_not_persist_88231"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _guard_temp_db(db_path: str) -> None:
    rp = os.path.realpath(db_path)
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"db must live under tempdir, got {rp}"
    assert "data_tpilot.db" not in rp and os.sep + "db" + os.sep not in rp, rp


async def _mk_lease(tmp_db: str, host: str = "1.2.3.4", pid: str = "PXY1") -> int:
    return await storage.proxy_lease_create(
        provider_type="proxy_seller", host=host, port=50101,
        login="u", password=SECRET_PASSWORD, provider_proxy_id=pid,
        expires_at="2026-09-01T00:00:00", db_path=tmp_db,
    )


async def run_all_checks(tmp_db: str) -> None:
    # 1. migration columns + audit table present on a fresh DB, idempotent
    async with aiosqlite.connect(tmp_db) as db:
        await storage._proxy_leases_table_ready(db)
        await storage._proxy_leases_table_ready(db)
    con = sqlite3.connect(tmp_db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(proxy_leases)").fetchall()}
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    new_cols = {"lifecycle_status", "desired_provider_auto_renew", "observed_provider_auto_renew",
                "provider_sync_at", "released_at", "release_reason"}
    check("migration adds all lifecycle columns to proxy_leases", new_cols.issubset(cols), str(new_cols - cols))
    check("proxy_lifecycle_events audit table exists", "proxy_lifecycle_events" in tables)

    # 2. a new lease defaults to active_confirmed_on
    lease_id = await _mk_lease(tmp_db)
    lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("new lease defaults lifecycle_status='active_confirmed_on'",
          lease.get("lifecycle_status") == "active_confirmed_on", repr(lease.get("lifecycle_status")))

    # 3. set_provider_state writes only supplied fields
    await storage.proxy_lease_set_provider_state(lease_id, desired="Y", observed="N", provider_sync_at="2026-07-21T00:00:00", db_path=tmp_db)
    lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("set_provider_state writes desired", lease.get("desired_provider_auto_renew") == "Y")
    check("set_provider_state writes observed", lease.get("observed_provider_auto_renew") == "N")
    check("set_provider_state writes provider_sync_at", lease.get("provider_sync_at") == "2026-07-21T00:00:00")
    # updating only observed leaves desired untouched
    await storage.proxy_lease_set_provider_state(lease_id, observed="Y", db_path=tmp_db)
    lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("partial set_provider_state leaves desired intact", lease.get("desired_provider_auto_renew") == "Y")
    check("partial set_provider_state updates observed", lease.get("observed_provider_auto_renew") == "Y")

    # 4. CAS transition: correct from_state wins exactly once
    ok1 = await storage.proxy_lease_set_lifecycle(lease_id, "active_confirmed_on", "enable_required", desired="Y", observed="N", db_path=tmp_db)
    check("CAS transition with correct from_state succeeds", ok1 is True)
    ok2 = await storage.proxy_lease_set_lifecycle(lease_id, "active_confirmed_on", "enable_required", db_path=tmp_db)
    check("duplicate CAS from the now-stale from_state is a safe no-op (False)", ok2 is False)
    lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("lifecycle_status advanced to enable_required", lease.get("lifecycle_status") == "enable_required")

    # 5. from_state=None forces the set
    okf = await storage.proxy_lease_set_lifecycle(lease_id, None, "disable_required", desired="N", observed="Y", db_path=tmp_db)
    check("from_state=None forces the transition", okf is True)
    lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("forced transition applied", lease.get("lifecycle_status") == "disable_required")

    # 6. release transition writes released_at + release_reason
    await storage.proxy_lease_set_lifecycle(lease_id, "disable_required", "released_off_confirmed",
                                            desired="N", observed="N", released_at="2026-07-21T01:00:00",
                                            release_reason="unused_cleanup", db_path=tmp_db)
    lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("released lease records released_at", lease.get("released_at") == "2026-07-21T01:00:00")
    check("released lease records release_reason", lease.get("release_reason") == "unused_cleanup")

    # 7. audit events are append-only and secret-free
    await storage.proxy_lifecycle_event_add(lease_id, "enable_required", provider_proxy_id="PXY1", actor="reconcile", desired="Y", observed="N", detail="test", db_path=tmp_db)
    await storage.proxy_lifecycle_event_add(lease_id, "disable_required", provider_proxy_id="PXY1", actor="reconcile", desired="N", observed="Y", db_path=tmp_db)
    events = await storage.proxy_lifecycle_events_for_lease(lease_id, db_path=tmp_db)
    check("audit history returns both events in order",
          len(events) == 2 and events[0]["event"] == "enable_required" and events[1]["event"] == "disable_required",
          str([e["event"] for e in events]))
    con = sqlite3.connect(tmp_db)
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        con.close()
    for suffix in ("", "-wal", "-journal"):
        p = tmp_db + suffix
        if os.path.isfile(p):
            with open(p, "rb") as f:
                blob = f.read()
            # The password IS allowed to appear (canonical proxy_leases.password
            # column) -- but NEVER inside a proxy_lifecycle_events row. Assert
            # the events table's own serialized rows carry no password.
    con = sqlite3.connect(tmp_db)
    ev_blob = str(con.execute("SELECT * FROM proxy_lifecycle_events").fetchall())
    con.close()
    check("audit rows never contain the proxy password", SECRET_PASSWORD not in ev_blob)

    # 8. actionable list returns only enable_required/disable_required
    healthy = await _mk_lease(tmp_db, host="9.9.9.9", pid="PXY2")  # stays active_confirmed_on
    other = await _mk_lease(tmp_db, host="8.8.8.8", pid="PXY3")
    await storage.proxy_lease_set_lifecycle(other, "active_confirmed_on", "enable_required", db_path=tmp_db)
    actionable = await storage.proxy_lifecycle_list_actionable(db_path=tmp_db)
    ids = {a["id"] for a in actionable}
    check("actionable list includes the enable_required lease", other in ids)
    check("actionable list excludes the active_confirmed_on lease", healthy not in ids)
    check("actionable list excludes the released lease", lease_id not in ids)


async def run_legacy_migration_check(tmp_db: str) -> None:
    # A proxy_leases table created WITHOUT the lifecycle columns (an older
    # deployed DB) must be migrated in place by _proxy_leases_table_ready.
    con = sqlite3.connect(tmp_db)
    con.execute(
        "CREATE TABLE proxy_leases(id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT, "
        "provider_type TEXT NOT NULL, provider_proxy_id TEXT, host TEXT NOT NULL, port INTEGER NOT NULL, "
        "status TEXT DEFAULT 'active', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.execute("INSERT INTO proxy_leases(provider_type, host, port, created_at, updated_at) VALUES('proxy_seller','1.1.1.1',50101,'t','t')")
    con.commit()
    con.close()
    async with aiosqlite.connect(tmp_db) as db:
        await storage._proxy_leases_table_ready(db)
    con = sqlite3.connect(tmp_db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(proxy_leases)").fetchall()}
    con.close()
    new_cols = {"lifecycle_status", "desired_provider_auto_renew", "observed_provider_auto_renew",
                "provider_sync_at", "released_at", "release_reason"}
    check("legacy proxy_leases DB is migrated in place (columns added)", new_cols.issubset(cols), str(new_cols - cols))


def main() -> int:
    tmpd = tempfile.mkdtemp(prefix="proxy_lifecycle_")
    tmp_db = os.path.join(tmpd, "q.db")
    _guard_temp_db(tmp_db)
    asyncio.run(run_all_checks(tmp_db))
    legacy_db = os.path.join(tmpd, "legacy.db")
    _guard_temp_db(legacy_db)
    asyncio.run(run_legacy_migration_check(legacy_db))
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PROXY LIFECYCLE STORAGE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
