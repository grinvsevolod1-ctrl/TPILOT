# -*- coding: utf-8 -*-
"""tools/proxy_renewal_storage_selftest.py -- offline selftest for the
PROXY RENEWAL (TPilot-managed prolong) 20260721 storage layer.

Temp SQLite only; no network, no Telegram, no provider, no spend. Proves:
  - proxy_renewal_ops + proxy_renewal_config migrate idempotently (fresh + legacy);
  - config seeds exactly one row, automation OFF by default;
  - idempotency_key UNIQUE: a second create with the same key -> None (skip);
  - one-active-op-per-lease partial unique: a second active op for the same
    lease -> None (skip), preventing concurrent prolong/make;
  - proxy_renewal_op_advance CAS: correct from_status wins, stale loses;
  - a lease with a finished (success/failed) op CAN start a new active op;
  - daily-spend total sums only success ops on the given day;
  - config_set writes only allowed keys.

    python tools\\proxy_renewal_storage_selftest.py
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


async def run(tmp_db: str) -> None:
    # 1. migration idempotent + tables/config present
    async with aiosqlite.connect(tmp_db) as db:
        await storage._proxy_renewal_tables_ready(db)
        await storage._proxy_renewal_tables_ready(db)
    con = sqlite3.connect(tmp_db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    check("proxy_renewal_ops table exists", "proxy_renewal_ops" in tables)
    check("proxy_renewal_config table exists", "proxy_renewal_config" in tables)

    cfg = await storage.proxy_renewal_config_get(db_path=tmp_db)
    check("config seeds exactly one row", bool(cfg) and cfg.get("id") == 1)
    check("automation is OFF by default", int(cfg.get("automation_enabled") or 0) == 0)
    check("default lead_days is 7", int(cfg.get("lead_days") or 0) == 7)
    check("default renewal_period_id is 1m", cfg.get("renewal_period_id") == "1m")
    # re-seeding does not duplicate/overwrite
    async with aiosqlite.connect(tmp_db) as db:
        await storage._proxy_renewal_tables_ready(db)
    con = sqlite3.connect(tmp_db)
    n = con.execute("SELECT COUNT(*) FROM proxy_renewal_config").fetchone()[0]
    con.close()
    check("re-migration keeps exactly one config row", n == 1, str(n))

    # 2. idempotency_key UNIQUE
    key1 = storage.proxy_renewal_idempotency_key("PXY-1", "2026-09-01T00:00:00", "1m")
    op1 = await storage.proxy_renewal_op_create(lease_id=1, provider_proxy_id="PXY-1", idempotency_key=key1, source="auto", status="make_pending", period_id="1m", expires_before="2026-09-01", db_path=tmp_db)
    check("first op create succeeds", isinstance(op1, int) and op1 > 0)
    op1b = await storage.proxy_renewal_op_create(lease_id=1, provider_proxy_id="PXY-1", idempotency_key=key1, source="auto", status="make_pending", db_path=tmp_db)
    check("duplicate idempotency_key create returns None (skip, no double-spend)", op1b is None)

    # 3. one-active-op-per-lease (different key, same lease, still active) -> None
    key1c = storage.proxy_renewal_idempotency_key("PXY-1", "2026-10-01T00:00:00", "1m")
    op1c = await storage.proxy_renewal_op_create(lease_id=1, provider_proxy_id="PXY-1", idempotency_key=key1c, source="auto", status="pending", db_path=tmp_db)
    check("second ACTIVE op for same lease is rejected (partial unique)", op1c is None)

    # 4. CAS advance
    ok = await storage.proxy_renewal_op_advance(op1, "success", from_status="make_pending", make_at="2026-08-25T12:00:00", calc_total="3.50", currency="USD", expires_after="2026-10-01", db_path=tmp_db)
    check("CAS advance make_pending->success wins", ok is True)
    ok2 = await storage.proxy_renewal_op_advance(op1, "success", from_status="make_pending", db_path=tmp_db)
    check("stale CAS advance (already success) loses (False)", ok2 is False)
    row = await storage.proxy_renewal_op_get(op1, db_path=tmp_db)
    check("op recorded success + total + expires_after", row["status"] == "success" and row["calc_total"] == "3.50" and row["expires_after"] == "2026-10-01")

    # 5. after finish, a NEW active op for the same lease is allowed
    op1d = await storage.proxy_renewal_op_create(lease_id=1, provider_proxy_id="PXY-1", idempotency_key=key1c, source="auto", status="pending", db_path=tmp_db)
    check("after the prior op finished, a fresh active op for the lease is allowed", isinstance(op1d, int) and op1d > 0)
    active = await storage.proxy_renewal_op_active_for_lease(1, db_path=tmp_db)
    check("active-op lookup returns the fresh pending op", active and active["id"] == op1d)

    # 6. daily spend total (only success on that day)
    op_ok2 = await storage.proxy_renewal_op_create(lease_id=2, provider_proxy_id="PXY-2", idempotency_key="PXY-2:2026-09-01:1m", source="auto", status="make_pending", db_path=tmp_db)
    await storage.proxy_renewal_op_advance(op_ok2, "success", from_status="make_pending", make_at="2026-08-25T13:00:00", calc_total="4.00", db_path=tmp_db)
    total = await storage.proxy_renewal_daily_spend_total("2026-08-25", db_path=tmp_db)
    check("daily spend total sums both successful ops on 2026-08-25 (3.50 + 4.00)", abs(total - 7.5) < 1e-6, str(total))
    total_other = await storage.proxy_renewal_daily_spend_total("2026-08-26", db_path=tmp_db)
    check("daily spend total is 0 on a day with no success ops", total_other == 0.0, str(total_other))

    # 7. config_set writes only allowed keys
    await storage.proxy_renewal_config_set(automation_enabled=1, enabled_by_user_id=42, max_daily_spend="20.00", bogus_key="x", db_path=tmp_db)
    cfg2 = await storage.proxy_renewal_config_get(db_path=tmp_db)
    check("config_set enables automation + writes guard", int(cfg2.get("automation_enabled") or 0) == 1 and cfg2.get("max_daily_spend") == "20.00" and int(cfg2.get("enabled_by_user_id") or 0) == 42)
    check("config_set ignored the disallowed key (no crash, not written)", "bogus_key" not in cfg2)

    # 8. idempotency key uses date part only
    ka = storage.proxy_renewal_idempotency_key("PXY-9", "2026-09-01T05:00:00", "1m")
    kb = storage.proxy_renewal_idempotency_key("PXY-9", "2026-09-01T23:59:59", "1m")
    check("idempotency key ignores intra-day time (same date -> same key)", ka == kb == "PXY-9:2026-09-01:1m")


async def run_legacy(tmp_db: str) -> None:
    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE some_other(id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    async with aiosqlite.connect(tmp_db) as db:
        await storage._proxy_renewal_tables_ready(db)
    cfg = await storage.proxy_renewal_config_get(db_path=tmp_db)
    check("legacy DB (no renewal tables) migrates cleanly + seeds config", bool(cfg) and int(cfg.get("automation_enabled") or 0) == 0)


def main() -> int:
    tmpd = tempfile.mkdtemp(prefix="proxy_renewal_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)
    asyncio.run(run(db))
    legacy = os.path.join(tmpd, "legacy.db")
    _guard_temp_db(legacy)
    asyncio.run(run_legacy(legacy))
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PROXY RENEWAL STORAGE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
