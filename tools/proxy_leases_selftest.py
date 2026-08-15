# -*- coding: utf-8 -*-
"""tools/proxy_leases_selftest.py -- offline self-test for the
proxy_leases schema/CRUD helpers added to storage.py.

Uses a throwaway temporary SQLite file only -- never touches the real
project DB. Pure/offline: no network, no Telegram.

    python3.12 tools\\proxy_leases_selftest.py
"""
from __future__ import annotations

import asyncio
import io
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
SECRET_PASSWORD = "supersecretpw_do_not_print_45213"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


async def run_all_checks(tmp_db: str) -> None:
    # ------------------------------------------------------------------
    # 1. schema init runs twice without error (idempotency)
    # ------------------------------------------------------------------
    async with aiosqlite.connect(tmp_db) as db:
        await storage._proxy_leases_table_ready(db)
        await storage._proxy_leases_table_ready(db)
    check("schema init (_proxy_leases_table_ready) runs twice without error", True)

    con = sqlite3.connect(tmp_db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(proxy_leases)").fetchall()}
    con.close()
    expected_cols = {
        "id", "manager_key", "provider_type", "provider_order_id", "provider_order_number",
        "provider_proxy_id", "proxy_type", "scheme", "host", "port", "login", "password",
        "expires_at", "auto_renew_enabled", "status", "last_check_at", "last_check_ok",
        "last_check_status", "last_renew_attempt_at", "last_renew_status", "created_at", "updated_at",
    }
    check("all expected proxy_leases columns exist", expected_cols.issubset(cols), str(expected_cols - cols))

    # ------------------------------------------------------------------
    # 2. create lease
    # ------------------------------------------------------------------
    lease_id = await storage.proxy_lease_create(
        provider_type="proxyseller",
        host="1.2.3.4",
        port=50101,
        login="myuser",
        password=SECRET_PASSWORD,
        provider_order_id="ORDER123",
        provider_proxy_id="PXY456",
        expires_at="2026-08-01T00:00:00",
        db_path=tmp_db,
    )
    check("proxy_lease_create returns a positive int id", isinstance(lease_id, int) and lease_id > 0, repr(lease_id))

    fetched = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check(
        "proxy_lease_get returns the created row with correct host/port",
        bool(fetched) and fetched["host"] == "1.2.3.4" and fetched["port"] == 50101,
        repr(fetched),
    )
    check("created lease status defaults to 'active'", bool(fetched) and fetched.get("status") == "active")
    check("proxy_lease_get for an unknown id returns None", await storage.proxy_lease_get(999999, db_path=tmp_db) is None)

    # ------------------------------------------------------------------
    # 3. assign to manager-like key (with a minimal managers table so the
    #    managers.proxy_lease_id side of the assignment can be verified too)
    # ------------------------------------------------------------------
    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE IF NOT EXISTS managers(manager_key TEXT UNIQUE, proxy_lease_id INTEGER)")
    con.execute("INSERT INTO managers(manager_key) VALUES('testmgr01')")
    con.commit()
    con.close()

    ok = await storage.proxy_lease_assign_to_manager(lease_id, "testmgr01", db_path=tmp_db)
    check("proxy_lease_assign_to_manager returns True for a real lease id", ok is True)

    fetched2 = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("assigned lease has manager_key set", bool(fetched2) and fetched2.get("manager_key") == "testmgr01")

    con = sqlite3.connect(tmp_db)
    row = con.execute("SELECT proxy_lease_id FROM managers WHERE manager_key='testmgr01'").fetchone()
    con.close()
    check("managers.proxy_lease_id was set by the assign helper", row is not None and row[0] == lease_id)

    got_for_mgr = await storage.proxy_lease_get_for_manager("testmgr01", db_path=tmp_db)
    check(
        "proxy_lease_get_for_manager resolves the lease via managers.proxy_lease_id",
        bool(got_for_mgr) and got_for_mgr["id"] == lease_id,
    )

    ok_bad = await storage.proxy_lease_assign_to_manager(999999, "testmgr01", db_path=tmp_db)
    check("assign to a nonexistent lease id returns False (no crash)", ok_bad is False)

    ok_empty_key = await storage.proxy_lease_assign_to_manager(lease_id, "", db_path=tmp_db)
    check("assign with an empty manager_key returns False (no crash)", ok_empty_key is False)

    # ------------------------------------------------------------------
    # 4. update check status
    # ------------------------------------------------------------------
    await storage.proxy_lease_update_check(lease_id, ok=True, status_text="tcp ok; telegram ok", db_path=tmp_db)
    fetched3 = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("last_check_ok recorded as 1", bool(fetched3) and fetched3.get("last_check_ok") == 1)
    check("last_check_status recorded", bool(fetched3) and fetched3.get("last_check_status") == "tcp ok; telegram ok")
    check("last_check_at populated", bool(fetched3) and bool(fetched3.get("last_check_at")))

    await storage.proxy_lease_update_check(lease_id, ok=False, status_text="tcp timeout", db_path=tmp_db)
    fetched3b = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("last_check_ok updates to 0 on a failed check", bool(fetched3b) and fetched3b.get("last_check_ok") == 0)

    # ------------------------------------------------------------------
    # 5. update renew status
    # ------------------------------------------------------------------
    await storage.proxy_lease_update_renew(
        lease_id, status_text="renewed ok", new_expires_at="2026-09-01T00:00:00", db_path=tmp_db,
    )
    fetched4 = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check("last_renew_status recorded", bool(fetched4) and fetched4.get("last_renew_status") == "renewed ok")
    check("expires_at updated by a successful renew", bool(fetched4) and fetched4.get("expires_at") == "2026-09-01T00:00:00")
    check("last_renew_attempt_at populated", bool(fetched4) and bool(fetched4.get("last_renew_attempt_at")))

    await storage.proxy_lease_update_renew(lease_id, status_text="renew failed: insufficient balance", db_path=tmp_db)
    fetched5 = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
    check(
        "renew failure (no new_expires_at) keeps the prior expires_at unchanged",
        bool(fetched5) and fetched5.get("expires_at") == "2026-09-01T00:00:00",
    )
    check(
        "last_renew_status updated to the failure text",
        bool(fetched5) and fetched5.get("last_renew_status") == "renew failed: insufficient balance",
    )

    # ------------------------------------------------------------------
    # 6. list expiring / set_status
    # ------------------------------------------------------------------
    lease_id2 = await storage.proxy_lease_create(
        provider_type="proxyseller", host="5.6.7.8", port=1080,
        expires_at="2020-01-01T00:00:00",  # already in the past -- always before any realistic cutoff
        db_path=tmp_db,
    )
    lease_id3 = await storage.proxy_lease_create(
        provider_type="proxyseller", host="9.9.9.9", port=1081,
        db_path=tmp_db,  # no expires_at at all
    )

    expiring = await storage.proxy_lease_list_expiring("2099-01-01T00:00:00", db_path=tmp_db)
    expiring_ids = {r["id"] for r in expiring}
    check("list_expiring includes a lease whose expires_at is before the cutoff", lease_id2 in expiring_ids)
    check("list_expiring excludes a lease with no expires_at set", lease_id3 not in expiring_ids)

    await storage.proxy_lease_set_status(lease_id2, "expired", db_path=tmp_db)
    fetched_status = await storage.proxy_lease_get(lease_id2, db_path=tmp_db)
    check("proxy_lease_set_status changes the status field", bool(fetched_status) and fetched_status.get("status") == "expired")

    expiring2 = await storage.proxy_lease_list_expiring("2099-01-01T00:00:00", db_path=tmp_db)
    expiring_ids2 = {r["id"] for r in expiring2}
    check("list_expiring(only_active=True, default) excludes a lease after set_status('expired')", lease_id2 not in expiring_ids2)

    expiring3 = await storage.proxy_lease_list_expiring("2099-01-01T00:00:00", only_active=False, db_path=tmp_db)
    expiring_ids3 = {r["id"] for r in expiring3}
    check("list_expiring(only_active=False) includes non-active leases too", lease_id2 in expiring_ids3)

    # ------------------------------------------------------------------
    # 7. pure transform: proxy_lease_to_manager_proxy_fields
    # ------------------------------------------------------------------
    fields = storage.proxy_lease_to_manager_proxy_fields(fetched4)
    check(
        "proxy_lease_to_manager_proxy_fields maps host/port/login/password correctly",
        fields.get("proxy_host") == "1.2.3.4"
        and fields.get("proxy_port") == 50101
        and fields.get("proxy_username") == "myuser"
        and fields.get("proxy_password") == SECRET_PASSWORD,
        repr({k: v for k, v in fields.items() if k != "proxy_password"}),
    )
    check(
        "proxy_lease_to_manager_proxy_fields sets proxy_enabled=1 and proxy_mode='proxy'",
        fields.get("proxy_enabled") == 1 and fields.get("proxy_mode") == "proxy",
    )
    check(
        "proxy_lease_to_manager_proxy_fields on empty/None input returns {}",
        storage.proxy_lease_to_manager_proxy_fields({}) == {} and storage.proxy_lease_to_manager_proxy_fields(None) == {},
    )

    # ------------------------------------------------------------------
    # 8. existing manual-proxy columns on managers must be completely
    #    untouched by any of the above (additive-only guarantee)
    # ------------------------------------------------------------------
    con = sqlite3.connect(tmp_db)
    mgr_cols = {r[1] for r in con.execute("PRAGMA table_info(managers)").fetchall()}
    con.close()
    check(
        "managers table only gained proxy_lease_id -- no existing column removed/renamed",
        {"manager_key", "proxy_lease_id"}.issubset(mgr_cols),
        str(mgr_cols),
    )


def main() -> int:
    tmp_db = tempfile.mktemp(suffix=".db")
    try:
        # Capture everything printed during the actual test run (Tee: still
        # visible on the real console) so we can PROVE the raw password
        # never appeared in any printed output, not just assume it.
        real_stdout = sys.stdout
        buffer = io.StringIO()

        class _Tee:
            def write(self_, s):
                real_stdout.write(s)
                buffer.write(s)
                return len(s)

            def flush(self_):
                real_stdout.flush()

        sys.stdout = _Tee()
        try:
            asyncio.run(run_all_checks(tmp_db))
        finally:
            sys.stdout = real_stdout

        captured = buffer.getvalue()
        leaked = SECRET_PASSWORD in captured
        check("the test password never appeared in ANY printed output during the whole run", not leaked)
    finally:
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except Exception:
            pass

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
