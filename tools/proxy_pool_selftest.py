# -*- coding: utf-8 -*-
"""tools/proxy_pool_selftest.py -- offline self-test for the Stage 6 P1
Proxy Pool storage helpers: proxy_lease_list_all,
proxy_lease_get_by_provider_proxy_id, proxy_lease_unassign,
proxy_lease_upsert_from_provider.

Uses a throwaway temporary SQLite file only -- never touches the real
project DB. Pure/offline: no network, no Telegram, no provider calls, no
buy/renew.

    python3.12 tools\\proxy_pool_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import re as _re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage

FAILURES: list[str] = []
SECRET_PASSWORD = "pool_selftest_secret_pw_20260709"
SECRET_API_KEY = "FAKE_POOL_P2_TEST_KEY_xyz"


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


async def _make_lease(tmp_db: str, **overrides) -> int:
    kwargs = dict(
        provider_type="proxy_seller",
        host="1.2.3.4",
        port=50101,
        manager_key=None,
        provider_proxy_id="PXY-BASE",
        proxy_type="ipv4",
        scheme="socks5",
        login="u1",
        password="pw1",
        status="active",
        db_path=tmp_db,
    )
    kwargs.update(overrides)
    return await storage.proxy_lease_create(**kwargs)


async def run_all_checks(tmp_db: str) -> None:
    # ------------------------------------------------------------------
    # 1. proxy_lease_list_all returns all leases, ordered by id ASC
    # ------------------------------------------------------------------
    id_a = await _make_lease(tmp_db, host="1.1.1.1", provider_proxy_id="PXY-A")
    id_b = await _make_lease(tmp_db, host="2.2.2.2", provider_proxy_id="PXY-B")
    id_c = await _make_lease(tmp_db, host="3.3.3.3", provider_proxy_id="PXY-C")

    all_leases = await storage.proxy_lease_list_all(db_path=tmp_db)
    check("1a. proxy_lease_list_all returns all created leases", len(all_leases) == 3, str(len(all_leases)))
    ids_in_order = [r["id"] for r in all_leases]
    check("1b. proxy_lease_list_all is ordered by id ASC", ids_in_order == sorted(ids_in_order), str(ids_in_order))
    check("1c. proxy_lease_list_all includes the expected ids", {id_a, id_b, id_c}.issubset(set(ids_in_order)))

    # ------------------------------------------------------------------
    # 2/3/4. get_by_provider_proxy_id: finds by identity, None for
    #    missing/empty, never confused with the internal lease id.
    # ------------------------------------------------------------------
    found = await storage.proxy_lease_get_by_provider_proxy_id("proxy_seller", "PXY-B", db_path=tmp_db)
    check("2. get_by_provider_proxy_id finds the lease by provider identity", found is not None and found["id"] == id_b, repr(found))

    missing = await storage.proxy_lease_get_by_provider_proxy_id("proxy_seller", "PXY-DOES-NOT-EXIST", db_path=tmp_db)
    check("3a. get_by_provider_proxy_id returns None for a missing provider_proxy_id", missing is None)
    empty = await storage.proxy_lease_get_by_provider_proxy_id("proxy_seller", "", db_path=tmp_db)
    check("3b. get_by_provider_proxy_id returns None for an empty provider_proxy_id", empty is None)

    # 4: provider_proxy_id is a distinct string identity, not the internal
    # lease id -- looking up by the stringified internal id of a DIFFERENT
    # lease must not accidentally match (proves the lookup filters on the
    # provider_proxy_id column, not on id).
    by_wrong_key = await storage.proxy_lease_get_by_provider_proxy_id("proxy_seller", str(id_a), db_path=tmp_db)
    check(
        "4. provider_proxy_id is never confused with the internal lease id",
        by_wrong_key is None,
        repr(by_wrong_key),
    )

    # ------------------------------------------------------------------
    # 5/6/7/8. unassign
    # ------------------------------------------------------------------
    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE IF NOT EXISTS managers(manager_key TEXT UNIQUE, proxy_lease_id INTEGER)")
    con.execute("INSERT INTO managers(manager_key) VALUES('mgr_a')")
    con.commit()
    con.close()

    assigned = await storage.proxy_lease_assign_to_manager(id_a, "mgr_a", db_path=tmp_db)
    check("(setup) lease A assigned to mgr_a", assigned is True)

    unassign_ok = await storage.proxy_lease_unassign(id_a, db_path=tmp_db)
    check("5/8a. unassign returns True for an existing lease", unassign_ok is True)

    after_unassign = await storage.proxy_lease_get(id_a, db_path=tmp_db)
    check("5. unassign clears proxy_leases.manager_key", not after_unassign.get("manager_key"), repr(after_unassign.get("manager_key")))
    check("7a. unassign keeps the lease row (not deleted)", after_unassign is not None)
    check("7b. unassign keeps provider_proxy_id untouched", after_unassign.get("provider_proxy_id") == "PXY-A", repr(after_unassign.get("provider_proxy_id")))
    check(
        "7c. unassign keeps host/port/login/password untouched",
        after_unassign.get("host") == "1.1.1.1" and after_unassign.get("login") == "u1" and after_unassign.get("password") == "pw1",
        repr({k: v for k, v in after_unassign.items() if k in ("host", "login", "password")}),
    )

    con = sqlite3.connect(tmp_db)
    mgr_row = con.execute("SELECT proxy_lease_id FROM managers WHERE manager_key='mgr_a'").fetchone()
    con.close()
    check("6. unassign clears managers.proxy_lease_id for the old manager", mgr_row is not None and mgr_row[0] is None, repr(mgr_row))

    unassign_missing = await storage.proxy_lease_unassign(999999, db_path=tmp_db)
    check("8b. unassign returns False for a non-existent lease id", unassign_missing is False)

    # ------------------------------------------------------------------
    # 9/10/11. upsert_from_provider: create, idempotent id, field refresh
    # ------------------------------------------------------------------
    new_id = await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller",
        provider_proxy_id="PXY-NEW-1",
        host="9.9.9.9",
        port=50111,
        login="new_user",
        password=SECRET_PASSWORD,
        expires_at="2026-09-01T00:00:00",
        provider_order_id="ORD-1",
        provider_order_number="BN-1",
        db_path=tmp_db,
    )
    check("9a. upsert creates a new lease for a new provider_proxy_id", isinstance(new_id, int) and new_id > 0, repr(new_id))

    new_lease = await storage.proxy_lease_get(new_id, db_path=tmp_db)
    check("9b. new lease has manager_key NULL (free/unassigned)", not new_lease.get("manager_key"))
    check("9c. new lease has status='active'", new_lease.get("status") == "active", repr(new_lease.get("status")))

    same_id = await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller",
        provider_proxy_id="PXY-NEW-1",
        host="9.9.9.9",
        port=50111,
        login="new_user",
        password=SECRET_PASSWORD,
        expires_at="2026-09-01T00:00:00",
        db_path=tmp_db,
    )
    check("10. upsert returns the SAME lease id on a repeated sync (same provider_proxy_id)", same_id == new_id, repr((same_id, new_id)))

    updated_id = await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller",
        provider_proxy_id="PXY-NEW-1",
        host="10.10.10.10",
        port=60222,
        login="updated_user",
        password="updated_pw",
        expires_at="2026-10-01T00:00:00",
        db_path=tmp_db,
    )
    updated_lease = await storage.proxy_lease_get(updated_id, db_path=tmp_db)
    check("11a. upsert updates host on an existing lease", updated_lease.get("host") == "10.10.10.10", repr(updated_lease.get("host")))
    check("11b. upsert updates port on an existing lease", updated_lease.get("port") == 60222, repr(updated_lease.get("port")))
    check("11c. upsert updates login on an existing lease", updated_lease.get("login") == "updated_user", repr(updated_lease.get("login")))
    check("11d. upsert updates password on an existing lease", updated_lease.get("password") == "updated_pw", repr(updated_lease.get("password")))
    check("11e. upsert updates expires_at on an existing lease", updated_lease.get("expires_at") == "2026-10-01T00:00:00", repr(updated_lease.get("expires_at")))

    # ------------------------------------------------------------------
    # 12. upsert must NEVER overwrite an existing manager_key
    # ------------------------------------------------------------------
    con = sqlite3.connect(tmp_db)
    con.execute("INSERT OR IGNORE INTO managers(manager_key) VALUES('mgr_b')")
    con.commit()
    con.close()
    await storage.proxy_lease_assign_to_manager(updated_id, "mgr_b", db_path=tmp_db)
    lease_before_sync = await storage.proxy_lease_get(updated_id, db_path=tmp_db)
    check("(setup) lease is now assigned to mgr_b before the next sync", lease_before_sync.get("manager_key") == "mgr_b")

    await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller",
        provider_proxy_id="PXY-NEW-1",
        host="10.10.10.10",
        port=60222,
        login="updated_user",
        password="updated_pw",
        expires_at="2026-11-01T00:00:00",
        db_path=tmp_db,
    )
    lease_after_sync = await storage.proxy_lease_get(updated_id, db_path=tmp_db)
    check("12. upsert does NOT overwrite an existing manager_key", lease_after_sync.get("manager_key") == "mgr_b", repr(lease_after_sync.get("manager_key")))
    check(
        "12b. (sanity) upsert still refreshes provider-sourced fields (expires_at) on an assigned lease",
        lease_after_sync.get("expires_at") == "2026-11-01T00:00:00",
        repr(lease_after_sync.get("expires_at")),
    )

    # ------------------------------------------------------------------
    # 13. upsert must NEVER overwrite an existing status
    # ------------------------------------------------------------------
    await storage.proxy_lease_set_status(updated_id, "disabled", db_path=tmp_db)
    await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller",
        provider_proxy_id="PXY-NEW-1",
        host="10.10.10.10",
        port=60222,
        db_path=tmp_db,
    )
    lease_after_status_sync = await storage.proxy_lease_get(updated_id, db_path=tmp_db)
    check("13. upsert does NOT overwrite an existing status", lease_after_status_sync.get("status") == "disabled", repr(lease_after_status_sync.get("status")))

    # ------------------------------------------------------------------
    # 14. repeated sync of the SAME provider_proxy_id is idempotent --
    #    no duplicate rows are ever created.
    # ------------------------------------------------------------------
    before_count = len(await storage.proxy_lease_list_all(db_path=tmp_db))
    idempotent_ids = set()
    for _ in range(3):
        rid = await storage.proxy_lease_upsert_from_provider(
            provider_type="proxy_seller", provider_proxy_id="PXY-IDEMPOTENT", host="8.8.8.8", port=50188, db_path=tmp_db,
        )
        idempotent_ids.add(rid)
    after_count = len(await storage.proxy_lease_list_all(db_path=tmp_db))
    check("14a. repeated sync of the same provider_proxy_id returns the same id every time", len(idempotent_ids) == 1, str(idempotent_ids))
    check("14b. repeated sync of the same provider_proxy_id adds exactly one row, not three", after_count == before_count + 1, str((before_count, after_count)))

    # ------------------------------------------------------------------
    # 15. a local lease absent from a given sync round (i.e. simply not
    #    passed to upsert) is never deleted.
    # ------------------------------------------------------------------
    all_before_partial_sync = {r["id"] for r in await storage.proxy_lease_list_all(db_path=tmp_db)}
    await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller", provider_proxy_id="PXY-ONLY-THIS-ONE", host="7.7.7.7", port=50177, db_path=tmp_db,
    )
    all_after_partial_sync = {r["id"] for r in await storage.proxy_lease_list_all(db_path=tmp_db)}
    check(
        "15. syncing only one proxy does not delete unrelated local leases",
        all_before_partial_sync.issubset(all_after_partial_sync),
        str((len(all_before_partial_sync), len(all_after_partial_sync))),
    )

    # ------------------------------------------------------------------
    # (extra) empty provider_proxy_id is rejected, not silently accepted
    # as a new/ambiguous row.
    # ------------------------------------------------------------------
    upsert_empty_id_raised = None
    try:
        await storage.proxy_lease_upsert_from_provider(provider_type="proxy_seller", provider_proxy_id="", host="0.0.0.0", port=1, db_path=tmp_db)
    except ValueError as e:
        upsert_empty_id_raised = e
    check("(extra) upsert_from_provider rejects an empty provider_proxy_id instead of silently creating an ambiguous row", upsert_empty_id_raised is not None)

    # ------------------------------------------------------------------
    # NB-1 fix (Proxy Renewal Reliability review, 20260808) + NB-1
    # follow-up fix (independent re-review correction, 20260809): a
    # periodic provider list-refresh that omits login/password/expires_at
    # must NEVER blank out already-known values -- "empty this round" means
    # "unknown this round", not "removed". This was never directly
    # selftested before (only the manager_key/status preservation above was
    # -- a separate, always-tested guarantee).
    # ------------------------------------------------------------------
    nb1_id = await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller", provider_proxy_id="PXY-NB1",
        host="20.20.20.20", port=50120, login="abc", password="xyz",
        expires_at="2026-08-10", db_path=tmp_db,
    )
    nb1_before = await storage.proxy_lease_get(nb1_id, db_path=tmp_db)
    check("(setup) NB-1 lease seeded with expires_at/login/password", nb1_before.get("expires_at") == "2026-08-10" and nb1_before.get("login") == "abc" and nb1_before.get("password") == "xyz")

    # a provider refresh that sends None/empty for all three -- host/port
    # are still refreshed normally (they always come from the provider).
    await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller", provider_proxy_id="PXY-NB1",
        host="20.20.20.21", port=50121, login=None, password="",
        expires_at=None, db_path=tmp_db,
    )
    nb1_after_none = await storage.proxy_lease_get(nb1_id, db_path=tmp_db)
    check("NB-1: expires_at is preserved when the provider refresh sends None", nb1_after_none.get("expires_at") == "2026-08-10", repr(nb1_after_none.get("expires_at")))
    check("NB-1: login is preserved when the provider refresh sends None", nb1_after_none.get("login") == "abc", repr(nb1_after_none.get("login")))
    check("NB-1: password is preserved when the provider refresh sends empty string", nb1_after_none.get("password") == "xyz", repr(nb1_after_none.get("password")))
    check("(sanity) host/port ARE refreshed even while expires_at/login/password are preserved", nb1_after_none.get("host") == "20.20.20.21" and nb1_after_none.get("port") == 50121)

    # a provider refresh that sends a WHITESPACE-ONLY expires_at -- treated
    # as empty (not a literal date), never written as-is.
    await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller", provider_proxy_id="PXY-NB1",
        host="20.20.20.21", port=50121, expires_at="   ", db_path=tmp_db,
    )
    nb1_after_ws = await storage.proxy_lease_get(nb1_id, db_path=tmp_db)
    check("NB-1: whitespace-only expires_at is also treated as empty (preserved, not stored verbatim)", nb1_after_ws.get("expires_at") == "2026-08-10", repr(nb1_after_ws.get("expires_at")))

    # a provider refresh that sends a GENUINELY new expires_at DOES update it.
    await storage.proxy_lease_upsert_from_provider(
        provider_type="proxy_seller", provider_proxy_id="PXY-NB1",
        host="20.20.20.21", port=50121, expires_at="2026-09-10", db_path=tmp_db,
    )
    nb1_after_real = await storage.proxy_lease_get(nb1_id, db_path=tmp_db)
    check("NB-1: a genuinely new expires_at from the provider DOES update the lease", nb1_after_real.get("expires_at") == "2026-09-10", repr(nb1_after_real.get("expires_at")))

    # ------------------------------------------------------------------
    # 16. no password ever printed -- checked by the stdout Tee wrapper
    #    in main() below (mirrors proxy_leases_selftest.py's technique).
    # ------------------------------------------------------------------


def _extract_and_exec(path: str, names: set, extra_ns: dict) -> dict:
    src = open(path, encoding="utf-8-sig").read()
    tree = ast.parse(src)
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    if len(nodes) != len(names):
        found = {getattr(n, "name", None) for n in nodes}
        raise AssertionError(f"expected {names}, found {found} in {path}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path}>", "exec"), ns)
    return ns


async def run_p2_checks(tmp_db: str) -> None:
    """Stage 6 P2: read-only pool screens. Exercises main.py's
    _ppool_parse_expires_at / _ppool_derive_status / _ppool_lease_summary
    / _handle_proxy_pool_list_command / _handle_proxy_pool_card_command
    via AST extraction (main.py cannot be imported standalone -- heavy
    import-time side effects). _tpag_registry_get is faked with a plain
    in-memory dict so this stays pure/offline and doesn't depend on the
    full manager-registry machinery."""
    import json as _json

    fake_managers: dict = {}

    async def _fake_tpag_registry_get(key):
        return fake_managers.get(str(key or "").strip())

    # PROXY USAGE DETECTION 20260711: the list/card commands now build a
    # usage map via manager_list_rows() -- fake it against the SAME
    # fake_managers dict this test already populates, so a manager's
    # proxy_host/proxy_port/proxy_enabled (when a test sets them) are
    # visible to the usage map exactly like the real system's single
    # managers table would show them.
    async def _fake_manager_list_rows(include_removed: bool = False):
        return [dict(v) for v in fake_managers.values()]

    def _fake_registry_normalize(raw):
        return _re.sub(r"\s+", "", str(raw or "")).casefold().replace("ё", "е")

    ns = _extract_and_exec(
        str(BASE_DIR / "main.py"),
        {
            "_ppool_parse_expires_at",
            "_ppool_derive_status",
            "_ppool_lease_summary",
            "_handle_proxy_pool_list_command",
            "_handle_proxy_pool_card_command",
            "_ppool_manager_usage_map",
            "_ppool_usage_key",
            "_ppool_usage_public",
            "_kyiv_now",  # R5b: _ppool_derive_status now compares Kyiv calendar dates, not UTC
        },
        {
            "datetime": datetime,
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "_tpag_registry_get": _fake_tpag_registry_get,
            "manager_list_rows": _fake_manager_list_rows,
            "registry_normalize_manager_key": _fake_registry_normalize,
            "TPILOT_DB_PATH": tmp_db,
            "_pbuy_json": _json,
            "_PPOOL_VALID_FILTERS": ("all", "free", "occupied", "assigned", "orphaned", "expired", "disabled", "external", "available"),
        },
    )
    parse_expires_fn = ns["_ppool_parse_expires_at"]
    derive_status_fn = ns["_ppool_derive_status"]
    list_cmd_fn = ns["_handle_proxy_pool_list_command"]
    card_cmd_fn = ns["_handle_proxy_pool_card_command"]

    now = datetime.utcnow()
    past_ddmmyyyy = (now - timedelta(days=30)).strftime("%d.%m.%Y")

    # ------------------------------------------------------------------
    # 6 (parser). unparseable expires_at never crashes, returns None.
    # ------------------------------------------------------------------
    check("6a. _ppool_parse_expires_at returns None for garbage input", parse_expires_fn("not-a-date") is None)
    check("6b. _ppool_parse_expires_at returns None for empty input", parse_expires_fn("") is None)
    check("6c. _ppool_parse_expires_at returns None for None input", parse_expires_fn(None) is None)
    check("6d. _ppool_parse_expires_at parses ISO YYYY-MM-DD", parse_expires_fn("2026-09-09") is not None)
    check("6e. _ppool_parse_expires_at parses ISO YYYY-MM-DDTHH:MM:SS", parse_expires_fn("2026-09-09T00:00:00") is not None)
    check("6f. _ppool_parse_expires_at parses DD.MM.YYYY", parse_expires_fn("09.09.2026") is not None)

    # ------------------------------------------------------------------
    # 1. derived status free -- no manager_key, active, no expiry
    # ------------------------------------------------------------------
    lease_free = {"status": "active", "manager_key": None, "expires_at": None}
    status_free, mgr_free = await derive_status_fn(lease_free)
    check("1. derived status is 'free' for an unassigned, active, non-expired lease", status_free == "free", status_free)
    check("1b. free lease has no manager_row", mgr_free is None)

    # ------------------------------------------------------------------
    # 2. derived status assigned -- manager_key points at an ACTIVE manager
    # ------------------------------------------------------------------
    fake_managers["active_mgr"] = {"manager_key": "active_mgr", "status": "active", "display_name": "Active Mgr"}
    lease_assigned = {"status": "active", "manager_key": "active_mgr", "expires_at": None}
    status_assigned, mgr_assigned = await derive_status_fn(lease_assigned)
    check("2. derived status is 'assigned' for a lease linked to an active manager", status_assigned == "assigned", status_assigned)
    check("2b. assigned lease's manager_row is the active manager", mgr_assigned is not None and mgr_assigned.get("status") == "active")

    # ------------------------------------------------------------------
    # 3. derived status orphaned -- manager_key points at a DELETED
    #    (missing) manager.
    # ------------------------------------------------------------------
    lease_deleted_mgr = {"status": "active", "manager_key": "no_such_manager", "expires_at": None}
    status_deleted, mgr_deleted = await derive_status_fn(lease_deleted_mgr)
    check("3. derived status is 'orphaned' when manager_key points to a deleted manager", status_deleted == "orphaned", status_deleted)
    check("3b. orphaned-by-deletion lease has no manager_row", mgr_deleted is None)

    # ------------------------------------------------------------------
    # 4. derived status orphaned -- manager exists but is ARCHIVED.
    # ------------------------------------------------------------------
    fake_managers["archived_mgr"] = {"manager_key": "archived_mgr", "status": "archived", "display_name": "Archived Mgr"}
    lease_archived_mgr = {"status": "active", "manager_key": "archived_mgr", "expires_at": None}
    status_archived, mgr_archived = await derive_status_fn(lease_archived_mgr)
    check("4. derived status is 'orphaned' (not a separate 'assigned_archived') when the manager is archived", status_archived == "orphaned", status_archived)
    check("4b. orphaned-by-archive lease's manager_row IS returned (archived, not missing)", mgr_archived is not None and mgr_archived.get("status") == "archived")

    # ------------------------------------------------------------------
    # 5. derived status expired -- DD.MM.YYYY in the past, unassigned.
    # ------------------------------------------------------------------
    lease_expired = {"status": "active", "manager_key": None, "expires_at": past_ddmmyyyy}
    status_expired, _mgr_expired = await derive_status_fn(lease_expired)
    check(f"5. derived status is 'expired' for expires_at={past_ddmmyyyy!r} (DD.MM.YYYY, in the past)", status_expired == "expired", status_expired)

    # ------------------------------------------------------------------
    # 6 (integration). unparseable expires_at on a real lease dict never
    #    crashes and never gets marked expired.
    # ------------------------------------------------------------------
    lease_garbage_date = {"status": "active", "manager_key": None, "expires_at": "definitely not a date"}
    status_garbage, _mgr_garbage = await derive_status_fn(lease_garbage_date)
    check("6g. a lease with an unparseable expires_at does not crash derive_status", status_garbage in ("free", "assigned", "orphaned"))
    check("6h. a lease with an unparseable expires_at is never marked 'expired' (can't confirm it, don't guess)", status_garbage != "expired", status_garbage)

    # ------------------------------------------------------------------
    # 7/8/9/10. /proxy_pool_list <filter> -- real leases via storage.py,
    #    real _handle_proxy_pool_list_command, fake registry only.
    # ------------------------------------------------------------------
    id_free = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="20.0.0.1", port=51001, provider_proxy_id="P2-FREE",
        manager_key=None, status="active", db_path=tmp_db,
    )
    id_assigned = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="20.0.0.2", port=51002, provider_proxy_id="P2-ASSIGNED",
        manager_key="active_mgr", status="active", db_path=tmp_db,
    )
    id_orphaned = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="20.0.0.3", port=51003, provider_proxy_id="P2-ORPHANED",
        manager_key="no_such_manager", status="active", db_path=tmp_db,
    )

    list_all_json = await list_cmd_fn("all")
    list_all = _json.loads(list_all_json)
    check("7a. /proxy_pool_list all succeeds", list_all.get("ok") is True, list_all_json)
    all_ids = {it.get("lease_id") for it in (list_all.get("items") or [])}
    check("7b. /proxy_pool_list all includes every created lease", {id_free, id_assigned, id_orphaned}.issubset(all_ids), str(all_ids))

    list_free_json = await list_cmd_fn("free")
    list_free = _json.loads(list_free_json)
    free_ids = {it.get("lease_id") for it in (list_free.get("items") or [])}
    check("8. /proxy_pool_list free includes the free lease and excludes assigned/orphaned", id_free in free_ids and id_assigned not in free_ids and id_orphaned not in free_ids, str(free_ids))

    list_assigned_json = await list_cmd_fn("assigned")
    list_assigned = _json.loads(list_assigned_json)
    assigned_ids = {it.get("lease_id") for it in (list_assigned.get("items") or [])}
    check("9. /proxy_pool_list assigned includes the assigned lease and excludes free/orphaned", id_assigned in assigned_ids and id_free not in assigned_ids and id_orphaned not in assigned_ids, str(assigned_ids))

    list_orphaned_json = await list_cmd_fn("orphaned")
    list_orphaned = _json.loads(list_orphaned_json)
    orphaned_ids = {it.get("lease_id") for it in (list_orphaned.get("items") or [])}
    check("10. /proxy_pool_list orphaned includes the orphaned lease and excludes free/assigned", id_orphaned in orphaned_ids and id_free not in orphaned_ids and id_assigned not in orphaned_ids, str(orphaned_ids))

    # ------------------------------------------------------------------
    # 11/12/13. /proxy_pool_card -- one lease, password masked (never
    #    raw), no secret markers anywhere in either command's output.
    # ------------------------------------------------------------------
    id_with_secret = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="20.0.0.9", port=51009, provider_proxy_id="P2-SECRET",
        login="pool_user", password=SECRET_PASSWORD, manager_key=None, status="active", db_path=tmp_db,
    )
    card_json = await card_cmd_fn(str(id_with_secret))
    card = _json.loads(card_json)
    check("11a. /proxy_pool_card succeeds for an existing lease", card.get("ok") is True, card_json)
    check("11b. /proxy_pool_card returns exactly the requested lease_id", card.get("lease_id") == id_with_secret, repr(card.get("lease_id")))
    check("12a. /proxy_pool_card reports has_password=True but never the raw password value", card.get("has_password") is True and "password" not in card, card_json)
    check("12b. /proxy_pool_card JSON text never contains the raw password", SECRET_PASSWORD not in card_json, card_json)

    card_missing_json = await card_cmd_fn("999999999")
    card_missing = _json.loads(card_missing_json)
    check("11c. /proxy_pool_card returns ok=False for a non-existent lease_id", card_missing.get("ok") is False)

    check("13a. /proxy_pool_list JSON text never contains the raw password", SECRET_PASSWORD not in list_all_json, list_all_json[:300])
    check("13b. /proxy_pool_list/card JSON text never contains a fake API key marker", SECRET_API_KEY not in list_all_json and SECRET_API_KEY not in card_json)


def _src_without_docstring(node) -> str:
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(n) for n in body)


async def run_p3_checks(tmp_db: str) -> None:
    """Stage 6 P3: assign / unassign / reassign. Exercises the REAL
    main.py handlers (_handle_proxy_pool_assign_command /
    _handle_proxy_pool_unassign_command / _pbuy_apply_lease_to_manager /
    _ppool_disable_manager_proxy_fields) via AST extraction, against a
    REAL temp SQLite DB for proxy_leases + a minimal raw `managers` table
    (so storage.proxy_lease_assign_to_manager/unassign's SQL writes are
    checkable), and a fake in-memory manager registry standing in for the
    real (Telethon-dependent) registry machinery."""
    import json as _json

    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE IF NOT EXISTS managers(manager_key TEXT UNIQUE, proxy_lease_id INTEGER)")
    con.commit()
    con.close()

    fake_managers: dict = {}
    guard_calls: list = []

    def _seed_manager(key: str, status: str = "active") -> None:
        fake_managers[key] = {"manager_key": key, "status": status, "display_name": key, "proxy_host": "", "proxy_port": None, "proxy_enabled": 0}
        con2 = sqlite3.connect(tmp_db)
        con2.execute("INSERT OR IGNORE INTO managers(manager_key) VALUES(?)", (key,))
        con2.commit()
        con2.close()

    async def _fake_tpag_registry_get(key):
        key = str(key or "").strip()
        row = fake_managers.get(key)
        if not row:
            return None
        merged = dict(row)
        # proxy_lease_id lives on the REAL managers table (written by
        # storage.proxy_lease_assign_to_manager/proxy_lease_unassign, not
        # by the fake _tpag_registry_set_fields below) -- merge it in so
        # the P3.1 consistency-fix logic (which reads
        # new_manager_row.get("proxy_lease_id")) sees the true DB state.
        try:
            con3 = sqlite3.connect(tmp_db)
            r = con3.execute("SELECT proxy_lease_id FROM managers WHERE manager_key=?", (key,)).fetchone()
            con3.close()
            if r:
                merged["proxy_lease_id"] = r[0]
        except Exception:
            pass
        return merged

    async def _fake_tpag_registry_set_fields(key, **fields):
        key = str(key or "").strip()
        if key in fake_managers:
            fake_managers[key].update(fields)

    async def _fake_tpag_run_guard(key, *, source, force=False):
        guard_calls.append({"key": key, "source": source, "force": force})
        return True, "ok"

    def _fake_manager_proxy_info_text(row):
        return f"proxy_host={row.get('proxy_host')}"

    def _fake_registry_normalize(raw):
        return _re.sub(r"\s+", "", str(raw or "")).casefold().replace("ё", "е")

    def _mgr_proxy_lease_id(key: str):
        con2 = sqlite3.connect(tmp_db)
        row = con2.execute("SELECT proxy_lease_id FROM managers WHERE manager_key=?", (key,)).fetchone()
        con2.close()
        return row[0] if row else None

    # PROXY USAGE DETECTION 20260711: _handle_proxy_pool_assign_command now
    # builds a usage map via manager_list_rows() -- fake it against the
    # SAME fake_managers dict this test already mutates (proxy_host/port/
    # enabled), so the conflict/multi-assign checks agree with the P3.1
    # cleanup logic and the _fake_tpag_registry_get/_set_fields fakes above.
    async def _fake_manager_list_rows(include_removed: bool = False):
        return [dict(v) for v in fake_managers.values()]

    ns = _extract_and_exec(
        str(BASE_DIR / "main.py"),
        {
            "_ppool_parse_expires_at",
            "_ppool_derive_status",
            "_pbuy_apply_lease_to_manager",
            "_ppool_disable_manager_proxy_fields",
            "_handle_proxy_pool_assign_command",
            "_handle_proxy_pool_unassign_command",
            "_ppool_manager_usage_map",
            "_ppool_usage_key",
            "_ppool_usage_public",
        },
        {
            "datetime": datetime,
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "_tpag_registry_get": _fake_tpag_registry_get,
            "_tpag_registry_set_fields": _fake_tpag_registry_set_fields,
            "_tpag_run_guard": _fake_tpag_run_guard,
            "_manager_proxy_info_text": _fake_manager_proxy_info_text,
            "_pbuy_safe_error": lambda e: str(e),
            "_now_utc_iso": lambda: "2026-07-09T00:00:00",
            "TPILOT_DB_PATH": tmp_db,
            "_pbuy_json": _json,
            "registry_normalize_manager_key": _fake_registry_normalize,
            "manager_list_rows": _fake_manager_list_rows,
        },
    )
    assign_fn = ns["_handle_proxy_pool_assign_command"]
    unassign_fn = ns["_handle_proxy_pool_unassign_command"]

    # ------------------------------------------------------------------
    # 1/2/3/4. assign a FREE lease to an active manager.
    # ------------------------------------------------------------------
    _seed_manager("mgr_free_target")
    lease_free_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="30.0.0.1", port=52001, provider_proxy_id="P3-FREE",
        login="u_free", password=SECRET_PASSWORD, manager_key=None, status="active", db_path=tmp_db,
    )
    guard_calls.clear()
    assign_json = await assign_fn(f"{lease_free_id} mgr_free_target")
    assign_result = _json.loads(assign_json)
    check("1a. assign of a free lease succeeds", assign_result.get("ok") is True, assign_json)

    lease_after_assign = await storage.proxy_lease_get(lease_free_id, db_path=tmp_db)
    check("1. assign sets proxy_leases.manager_key", lease_after_assign.get("manager_key") == "mgr_free_target", repr(lease_after_assign.get("manager_key")))
    check("2. assign sets managers.proxy_lease_id", _mgr_proxy_lease_id("mgr_free_target") == lease_free_id, repr(_mgr_proxy_lease_id("mgr_free_target")))
    check(
        "3. assign applies proxy fields to the manager (host + enabled via the registry helper)",
        fake_managers["mgr_free_target"].get("proxy_host") == "30.0.0.1" and fake_managers["mgr_free_target"].get("proxy_enabled") == 1,
        repr(fake_managers["mgr_free_target"]),
    )
    check("4. assign calls the guard helper", any(c["key"] == "mgr_free_target" for c in guard_calls), str(guard_calls))

    # ------------------------------------------------------------------
    # 5. assign already-assigned-to-the-SAME-manager is idempotent.
    # ------------------------------------------------------------------
    guard_calls.clear()
    assign_again_json = await assign_fn(f"{lease_free_id} mgr_free_target")
    assign_again = _json.loads(assign_again_json)
    check("5a. re-assigning the same lease to the SAME manager still succeeds (idempotent)", assign_again.get("ok") is True, assign_again_json)
    check("5b. idempotent re-assign reports no old_manager_key (not a reassignment from itself)", assign_again.get("old_manager_key") is None, assign_again_json)
    check("5c. idempotent re-assign still re-runs the guard", len(guard_calls) >= 1, str(guard_calls))

    # ------------------------------------------------------------------
    # 6/7. reassign from an old manager to a new manager.
    # ------------------------------------------------------------------
    _seed_manager("mgr_new_target")
    reassign_json = await assign_fn(f"{lease_free_id} mgr_new_target")
    reassign_result = _json.loads(reassign_json)
    check("6a. reassign to a different manager succeeds", reassign_result.get("ok") is True, reassign_json)
    check("6b. reassign reports the OLD manager_key", reassign_result.get("old_manager_key") == "mgr_free_target", reassign_json)
    check("6. reassign clears the OLD manager's managers.proxy_lease_id", _mgr_proxy_lease_id("mgr_free_target") is None, repr(_mgr_proxy_lease_id("mgr_free_target")))
    check("6c. reassign disables the OLD manager's proxy_enabled", fake_managers["mgr_free_target"].get("proxy_enabled") == 0, repr(fake_managers["mgr_free_target"]))
    check(
        "7. reassign applies proxy fields to the NEW manager",
        fake_managers["mgr_new_target"].get("proxy_host") == "30.0.0.1" and fake_managers["mgr_new_target"].get("proxy_enabled") == 1,
        repr(fake_managers["mgr_new_target"]),
    )
    lease_after_reassign = await storage.proxy_lease_get(lease_free_id, db_path=tmp_db)
    check("7b. reassign links proxy_leases.manager_key to the NEW manager", lease_after_reassign.get("manager_key") == "mgr_new_target")
    check("7c. reassign sets the NEW manager's managers.proxy_lease_id", _mgr_proxy_lease_id("mgr_new_target") == lease_free_id)

    # ------------------------------------------------------------------
    # 8. an orphaned lease (manager_key points at a DELETED manager) can
    #    be assigned to an active manager.
    # ------------------------------------------------------------------
    _seed_manager("mgr_for_orphan")
    lease_orphan_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="30.0.0.2", port=52002, provider_proxy_id="P3-ORPHAN",
        manager_key="deleted_manager_xyz", status="active", db_path=tmp_db,
    )
    orphan_assign_json = await assign_fn(f"{lease_orphan_id} mgr_for_orphan")
    orphan_assign = _json.loads(orphan_assign_json)
    check("8. an orphaned lease (manager_key pointing at a deleted manager) can be assigned to an active manager", orphan_assign.get("ok") is True, orphan_assign_json)
    lease_after_orphan_assign = await storage.proxy_lease_get(lease_orphan_id, db_path=tmp_db)
    check("8b. orphaned-lease assign links proxy_leases.manager_key to the new manager", lease_after_orphan_assign.get("manager_key") == "mgr_for_orphan")

    # ------------------------------------------------------------------
    # 9. assign to a MISSING manager stops cleanly.
    # ------------------------------------------------------------------
    lease_for_missing_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="30.0.0.3", port=52003, provider_proxy_id="P3-MISSING-TARGET",
        manager_key=None, status="active", db_path=tmp_db,
    )
    missing_json = await assign_fn(f"{lease_for_missing_id} no_such_manager_at_all")
    missing_result = _json.loads(missing_json)
    check("9. assign to a missing manager stops cleanly (ok=False, manager_not_found)", missing_result.get("ok") is False and missing_result.get("error") == "manager_not_found", missing_json)
    lease_after_missing = await storage.proxy_lease_get(lease_for_missing_id, db_path=tmp_db)
    check("9b. lease is NOT modified when the target manager is missing", not lease_after_missing.get("manager_key"))

    # ------------------------------------------------------------------
    # 10. assign to an ARCHIVED manager stops cleanly.
    # ------------------------------------------------------------------
    _seed_manager("mgr_archived_target", status="archived")
    archived_json = await assign_fn(f"{lease_for_missing_id} mgr_archived_target")
    archived_result = _json.loads(archived_json)
    check("10. assign to an archived manager stops cleanly (ok=False, manager_archived)", archived_result.get("ok") is False and archived_result.get("error") == "manager_archived", archived_json)
    lease_after_archived = await storage.proxy_lease_get(lease_for_missing_id, db_path=tmp_db)
    check("10b. lease is NOT modified when the target manager is archived", not lease_after_archived.get("manager_key"))

    # ------------------------------------------------------------------
    # 11/12/13. unassign.
    # ------------------------------------------------------------------
    lease_for_unassign_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="30.0.0.4", port=52004, provider_proxy_id="P3-UNASSIGN",
        login="u_unassign", password=SECRET_PASSWORD, manager_key=None, status="active", db_path=tmp_db,
    )
    _seed_manager("mgr_to_unassign")
    await assign_fn(f"{lease_for_unassign_id} mgr_to_unassign")
    check("(setup) lease is assigned before testing unassign", (await storage.proxy_lease_get(lease_for_unassign_id, db_path=tmp_db)).get("manager_key") == "mgr_to_unassign")

    unassign_json = await unassign_fn(str(lease_for_unassign_id))
    unassign_result = _json.loads(unassign_json)
    check("(setup) unassign command succeeds", unassign_result.get("ok") is True, unassign_json)
    check("11c. unassign reports the old_manager_key", unassign_result.get("old_manager_key") == "mgr_to_unassign", unassign_json)

    lease_after_unassign = await storage.proxy_lease_get(lease_for_unassign_id, db_path=tmp_db)
    check("11. unassign clears proxy_leases.manager_key", not lease_after_unassign.get("manager_key"), repr(lease_after_unassign.get("manager_key")))
    check("12. unassign clears managers.proxy_lease_id", _mgr_proxy_lease_id("mgr_to_unassign") is None)
    check(
        "13. unassign keeps the lease row + provider_proxy_id/host/password intact",
        lease_after_unassign is not None
        and lease_after_unassign.get("provider_proxy_id") == "P3-UNASSIGN"
        and lease_after_unassign.get("host") == "30.0.0.4"
        and lease_after_unassign.get("password") == SECRET_PASSWORD,
        repr({k: v for k, v in lease_after_unassign.items() if k in ("provider_proxy_id", "host", "password")}),
    )
    check("13b. unassign disables the old manager's proxy_enabled (best-effort side effect)", fake_managers["mgr_to_unassign"].get("proxy_enabled") == 0)

    # ------------------------------------------------------------------
    # 14/17. neither assign nor unassign ever calls the provider or
    #    spends -- verified by scanning the ACTUAL unparsed source of
    #    both handlers (not just the fakes they happened to be tested
    #    against), plus a project-wide count of the real allow_spend=True
    #    call sites.
    # ------------------------------------------------------------------
    main_src_p3 = Path(str(BASE_DIR / "main.py")).read_text(encoding="utf-8-sig")
    tree_p3 = ast.parse(main_src_p3)
    assign_node = next(n for n in tree_p3.body if getattr(n, "name", None) == "_handle_proxy_pool_assign_command")
    unassign_node = next(n for n in tree_p3.body if getattr(n, "name", None) == "_handle_proxy_pool_unassign_command")
    apply_node = next(n for n in tree_p3.body if getattr(n, "name", None) == "_pbuy_apply_lease_to_manager")
    assign_src = _src_without_docstring(assign_node)
    unassign_src = _src_without_docstring(unassign_node)
    apply_src = _src_without_docstring(apply_node)

    provider_markers = (
        "ProxySellerProvider", "reference_list", "calc_ipv4", "make_ipv4", "prolong_calc", "prolong_make",
        "list_proxies", "download_proxies", "check_proxy", "allow_spend", "_pbuy_provider(", "_pbuy_call(",
    )
    for marker in provider_markers:
        check(f"14a. /proxy_pool_assign source never references provider marker {marker!r}", marker not in assign_src, "")
        check(f"14b. /proxy_pool_unassign source never references provider marker {marker!r}", marker not in unassign_src, "")
        check(f"14c. _pbuy_apply_lease_to_manager source never references provider marker {marker!r}", marker not in apply_src, "")

    real_allow_spend_lines = _re.findall(r"^[ \t]*allow_spend=True,[ \t]*$", main_src_p3, _re.MULTILINE)
    check(
        "17. allow_spend=True appears as a call argument in exactly 2 places project-wide (buy-confirm, renew-confirm)",
        len(real_allow_spend_lines) == 2,
        str(len(real_allow_spend_lines)),
    )

    # ------------------------------------------------------------------
    # P3.1 consistency fix: assigning a lease to a manager who already has
    # a DIFFERENT lease (via managers.proxy_lease_id, or via a stale
    # proxy_leases.manager_key that was never reflected in
    # managers.proxy_lease_id) must free that other lease first, so a
    # manager never ends up "assigned" to more than one pool lease.
    # ------------------------------------------------------------------

    # P3.1 tests 1/2/3: target manager already has an old lease (linked
    # the normal way, so managers.proxy_lease_id genuinely points at it).
    _seed_manager("mgr_p31_target")
    old_lease_for_target_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="31.0.0.1", port=53001, provider_proxy_id="P31-OLD",
        login="u_old", password=SECRET_PASSWORD, manager_key=None, status="active", db_path=tmp_db,
    )
    setup_json = await assign_fn(f"{old_lease_for_target_id} mgr_p31_target")
    check("(P3.1 setup) target manager's first lease assigned normally", _json.loads(setup_json).get("ok") is True, setup_json)
    check("(P3.1 setup) managers.proxy_lease_id points at the first lease", _mgr_proxy_lease_id("mgr_p31_target") == old_lease_for_target_id)

    new_lease_for_target_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="31.0.0.2", port=53002, provider_proxy_id="P31-NEW",
        manager_key=None, status="active", db_path=tmp_db,
    )
    p31_assign_json = await assign_fn(f"{new_lease_for_target_id} mgr_p31_target")
    p31_assign_result = _json.loads(p31_assign_json)
    check("P3.1-1a. assigning a second lease to a manager who already has one succeeds", p31_assign_result.get("ok") is True, p31_assign_json)
    check("P3.1-1b. the assign result reports the freed old lease id", old_lease_for_target_id in (p31_assign_result.get("freed_lease_ids") or []), p31_assign_json)

    old_lease_after = await storage.proxy_lease_get(old_lease_for_target_id, db_path=tmp_db)
    check("P3.1-1. old lease's manager_key becomes empty after the new lease is assigned", not old_lease_after.get("manager_key"), repr(old_lease_after.get("manager_key")))
    check("P3.1-2. managers.proxy_lease_id now points at the NEW lease", _mgr_proxy_lease_id("mgr_p31_target") == new_lease_for_target_id, repr(_mgr_proxy_lease_id("mgr_p31_target")))
    check(
        "P3.1-3. old lease's provider_proxy_id/host/password are preserved (not cleared, not deleted)",
        old_lease_after is not None
        and old_lease_after.get("provider_proxy_id") == "P31-OLD"
        and old_lease_after.get("host") == "31.0.0.1"
        and old_lease_after.get("password") == SECRET_PASSWORD,
        repr({k: v for k, v in (old_lease_after or {}).items() if k in ("provider_proxy_id", "host", "password")}),
    )

    # P3.1 test 4: a stale extra lease has manager_key == target manager
    # but was never reflected in managers.proxy_lease_id (e.g. written
    # directly, or leftover from before this fix). Assigning a fresh
    # lease to that manager must clear the stale one too.
    _seed_manager("mgr_p31_stale")
    stale_lease_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="31.0.0.3", port=53003, provider_proxy_id="P31-STALE",
        manager_key="mgr_p31_stale", status="active", db_path=tmp_db,  # manager_key set directly, bypassing assign
    )
    check("(P3.1 setup) managers.proxy_lease_id was never set for the stale lease", _mgr_proxy_lease_id("mgr_p31_stale") is None)
    fresh_lease_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="31.0.0.4", port=53004, provider_proxy_id="P31-FRESH",
        manager_key=None, status="active", db_path=tmp_db,
    )
    p31_stale_json = await assign_fn(f"{fresh_lease_id} mgr_p31_stale")
    p31_stale_result = _json.loads(p31_stale_json)
    check("P3.1-4a. assigning a fresh lease when a stale manager_key-only lease exists succeeds", p31_stale_result.get("ok") is True, p31_stale_json)
    stale_lease_after = await storage.proxy_lease_get(stale_lease_id, db_path=tmp_db)
    check("P3.1-4. the stale lease's manager_key is cleared by the assign", not stale_lease_after.get("manager_key"), repr(stale_lease_after.get("manager_key")))
    fresh_lease_after = await storage.proxy_lease_get(fresh_lease_id, db_path=tmp_db)
    check("P3.1-4b. the fresh lease is now linked to the manager", fresh_lease_after.get("manager_key") == "mgr_p31_stale")
    check("P3.1-4c. managers.proxy_lease_id points at the fresh lease", _mgr_proxy_lease_id("mgr_p31_stale") == fresh_lease_id)

    # P3.1 test 5: full reassign -- a lease currently owned by an OLD
    # ACTIVE manager gets reassigned to a manager who ALREADY has another
    # lease. Final state must have exactly one lease per manager.
    _seed_manager("mgr_p31_old_owner")
    _seed_manager("mgr_p31_new_owner")
    lease_owned_by_old_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="31.0.0.5", port=53005, provider_proxy_id="P31-OWNED",
        manager_key=None, status="active", db_path=tmp_db,
    )
    await assign_fn(f"{lease_owned_by_old_id} mgr_p31_old_owner")
    lease_already_at_new_owner_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="31.0.0.6", port=53006, provider_proxy_id="P31-AT-NEW-OWNER",
        manager_key=None, status="active", db_path=tmp_db,
    )
    await assign_fn(f"{lease_already_at_new_owner_id} mgr_p31_new_owner")
    check("(P3.1-5 setup) old owner has its lease", _mgr_proxy_lease_id("mgr_p31_old_owner") == lease_owned_by_old_id)
    check("(P3.1-5 setup) new owner already has a different lease", _mgr_proxy_lease_id("mgr_p31_new_owner") == lease_already_at_new_owner_id)

    p31_full_reassign_json = await assign_fn(f"{lease_owned_by_old_id} mgr_p31_new_owner")
    p31_full_reassign = _json.loads(p31_full_reassign_json)
    check("P3.1-5a. full reassign (old active owner -> manager who already has another lease) succeeds", p31_full_reassign.get("ok") is True, p31_full_reassign_json)
    check("P3.1-5. old owner's managers.proxy_lease_id is cleared", _mgr_proxy_lease_id("mgr_p31_old_owner") is None)
    check("P3.1-5b. new owner's managers.proxy_lease_id points at the reassigned lease", _mgr_proxy_lease_id("mgr_p31_new_owner") == lease_owned_by_old_id)
    lease_already_at_new_owner_after = await storage.proxy_lease_get(lease_already_at_new_owner_id, db_path=tmp_db)
    check("P3.1-5c. new owner's PREVIOUS lease is now free (manager_key cleared)", not lease_already_at_new_owner_after.get("manager_key"))
    lease_owned_by_old_after = await storage.proxy_lease_get(lease_owned_by_old_id, db_path=tmp_db)
    check("P3.1-5. reassigned lease's manager_key == the new manager", lease_owned_by_old_after.get("manager_key") == "mgr_p31_new_owner")

    # P3.1 test 6: assign the SAME lease to the SAME (now-target) manager
    # remains idempotent even with the consistency fix active.
    p31_idem_json = await assign_fn(f"{lease_owned_by_old_id} mgr_p31_new_owner")
    p31_idem = _json.loads(p31_idem_json)
    check("P3.1-6a. re-assigning the same lease to the same manager still succeeds", p31_idem.get("ok") is True, p31_idem_json)
    check("P3.1-6b. idempotent re-assign reports no freed_lease_ids (nothing extra to free)", not p31_idem.get("freed_lease_ids"), p31_idem_json)
    check("P3.1-6. idempotent re-assign leaves managers.proxy_lease_id unchanged", _mgr_proxy_lease_id("mgr_p31_new_owner") == lease_owned_by_old_id)

    # P3.1 tests 7/8: no provider calls in the assign path -- already
    # covered by the 14a/14b/14c source scan above (it scans the CURRENT
    # main.py source, which includes this P3.1 addition); no secret leaks
    # in any of the new result JSONs.
    for label, j in (
        ("P3.1 assign(second lease)", p31_assign_json), ("P3.1 assign(stale cleanup)", p31_stale_json),
        ("P3.1 full reassign", p31_full_reassign_json), ("P3.1 idempotent", p31_idem_json),
    ):
        check(f"P3.1-8. {label} result JSON never contains the raw password", SECRET_PASSWORD not in j, j)
        check(f"P3.1-8b. {label} result JSON never contains the fake API key marker", SECRET_API_KEY not in j, j)

    # ------------------------------------------------------------------
    # 15. assign/unassign results never contain the raw password or a
    #    fake API key marker, across every P3 scenario exercised above.
    #    (10. existing P3 assign/unassign checks above this point still
    #    passing proves the P3.1 change didn't regress them.)
    # ------------------------------------------------------------------
    for label, j in (
        ("assign(free)", assign_json), ("assign(idempotent)", assign_again_json), ("reassign", reassign_json),
        ("assign(orphan)", orphan_assign_json), ("assign(missing)", missing_json), ("assign(archived)", archived_json),
        ("unassign", unassign_json),
    ):
        check(f"15. {label} result JSON never contains the raw password", SECRET_PASSWORD not in j, j)
        check(f"15b. {label} result JSON never contains the fake API key marker", SECRET_API_KEY not in j, j)


class _FakeCheckSyncProvider:
    """Stand-in for a ProxySellerProvider exposing only check_proxy() and
    list_proxies() -- the two read-only, no-spend endpoints Stage 6 P4
    uses. Never touches real HTTP; records every call for assertions."""

    def __init__(self, check_result=None, check_error=None, list_result=None, list_error=None):
        self._check_result = check_result
        self._check_error = check_error
        self._list_result = list_result
        self._list_error = list_error
        self.check_calls: list = []
        self.list_calls: list = []

    def check_proxy(self, proxy_string):
        self.check_calls.append(proxy_string)
        if self._check_error is not None:
            raise self._check_error
        return self._check_result

    def list_proxies(self, proxy_type, order_id=None, **kwargs):
        self.list_calls.append((proxy_type, order_id))
        if self._list_error is not None:
            raise self._list_error
        return self._list_result


class _FakeLocalSocksChecker:
    """LOCAL PROXY CHECK 20260711: stand-in for _tpag_v4_proxy_geo_via_socks
    -- the real local SOCKS5 connectivity check _handle_proxy_pool_check_
    command now uses instead of provider.check_proxy. Never touches a real
    socket; records every call (incl. the rdns flag and credentials passed
    through, exactly as the real function would receive them) so tests can
    assert both the socks5h-first/socks5-fallback call order and that the
    real credentials never leak beyond this boundary into the JSON result."""

    def __init__(self, *, rdns_true_result=None, rdns_true_error=None, rdns_false_result=None, rdns_false_error=None):
        self.rdns_true_result = rdns_true_result
        self.rdns_true_error = rdns_true_error
        self.rdns_false_result = rdns_false_result
        self.rdns_false_error = rdns_false_error
        self.calls: list = []

    def __call__(self, host, port, username="", password="", *, rdns=True):
        self.calls.append({"host": host, "port": port, "username": username, "password": password, "rdns": rdns})
        if rdns:
            if self.rdns_true_error is not None:
                raise self.rdns_true_error
            return self.rdns_true_result, 42
        if self.rdns_false_error is not None:
            raise self.rdns_false_error
        return self.rdns_false_result, 43


async def run_p4_checks(tmp_db: str) -> None:
    """Stage 6 P4: check / sync. Exercises the REAL main.py handlers
    (_handle_proxy_pool_check_command / _handle_proxy_pool_sync_command)
    via AST extraction, against a REAL temp SQLite DB for proxy_leases.

    LOCAL PROXY CHECK 20260711: /proxy_pool_check no longer calls
    provider.check_proxy at all (Proxy-Seller's tools/proxy/check endpoint
    proved unreliable for our SOCKS5 leases -- live diagnosis returned
    errors=[{"message": "Empty result!", ...}] for every tested request
    shape). It now runs a LOCAL socks5h-then-socks5 connectivity check via
    _tpag_v4_proxy_geo_via_socks (same mechanism the existing auth guard
    already uses in production), faked here via _FakeLocalSocksChecker so
    these tests stay pure/offline. /proxy_pool_sync still uses the
    provider's list_proxies() (unaffected, unrelated endpoint) -- that
    path keeps using _pbuy_provider()/_FakeCheckSyncProvider as before.
    The REAL _tpag_classify_error_reason is extracted alongside so the
    failure-message classification is genuinely exercised, not assumed."""
    import json as _json

    fake_provider_holder: dict = {"instance": None}
    local_checker_holder: dict = {"instance": None}

    def _fake_pbuy_provider():
        return fake_provider_holder["instance"]

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _fake_tpag_v4_proxy_geo_via_socks(host, port, username="", password="", *, rdns=True):
        return local_checker_holder["instance"](host, port, username, password, rdns=rdns)

    # PROXY LIFECYCLE SYNC 20260721: _handle_proxy_pool_sync_command now also
    # calls _plc_reconcile_all (real reconciliation covered by its own
    # dedicated tools/proxy_lifecycle_wiring_selftest.py) -- faked here as a
    # no-op so THIS test stays focused on sync's own upsert behavior, and
    # _pbuy_safe_error (already used by sync's list_proxies-failure branch,
    # just never exercised by this test's prior scenarios).
    async def _fake_plc_reconcile_all(entries):
        return {}

    ns_p4 = _extract_and_exec(
        str(BASE_DIR / "main.py"),
        {
            "_handle_proxy_pool_check_command",
            "_handle_proxy_pool_sync_command",
            "_ppool_sync_entries_to_pool",  # R5c: sync's upsert loop, extracted into its own shared helper
            "_tpag_classify_error_reason",
        },
        {
            "Any": object, "Dict": dict, "List": list, "Optional": object,
            "TPILOT_DB_PATH": tmp_db,
            "_pbuy_json": _json,
            "_pbuy_provider": _fake_pbuy_provider,
            "_pbuy_call": _fake_pbuy_call,
            "_tpag_v4_proxy_geo_via_socks": _fake_tpag_v4_proxy_geo_via_socks,
            "_PBUY_PROXY_TYPE": "ipv4",
            "_plc_reconcile_all": _fake_plc_reconcile_all,
            "_pbuy_safe_error": lambda e: str(e),
        },
    )
    check_fn = ns_p4["_handle_proxy_pool_check_command"]
    sync_fn = ns_p4["_handle_proxy_pool_sync_command"]

    # ------------------------------------------------------------------
    # 1. successful LOCAL socks5h check (first attempt succeeds) builds an
    #    admin-safe result and updates last_check_ok=1.
    # ------------------------------------------------------------------
    lease_check_ok_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="40.0.0.1", port=54001, provider_proxy_id="P4-CHECK-OK",
        login="chk_user", password=SECRET_PASSWORD, manager_key=None, status="active", db_path=tmp_db,
    )
    local_checker_holder["instance"] = _FakeLocalSocksChecker(rdns_true_result={"ip": "31.59.238.203"})
    check1_json = await check_fn(str(lease_check_ok_id))
    check1 = _json.loads(check1_json)
    check("1a. check command succeeds structurally (ok=True)", check1.get("ok") is True, check1_json)
    check("1. check_ok=True for a successful LOCAL socks5h check", check1.get("check_ok") is True, check1_json)
    check("1g. protocol is socks5h when the first (rdns=True) attempt succeeds", check1.get("protocol") == "socks5h", check1_json)
    check("1h. ip is the observed exit IP from the local check", check1.get("ip") == "31.59.238.203", check1_json)
    check("1b. check result never contains the raw proxy password", SECRET_PASSWORD not in check1_json, check1_json)
    check("1c. check result never contains the login (admin-safe)", "chk_user" not in check1_json, check1_json)
    check("1i. check result never contains the fake API key", SECRET_API_KEY not in check1_json, check1_json)

    lease_after_check1 = await storage.proxy_lease_get(lease_check_ok_id, db_path=tmp_db)
    check("1d. storage last_check_ok is 1 after a successful check", lease_after_check1.get("last_check_ok") == 1, repr(lease_after_check1.get("last_check_ok")))
    check("1e. storage last_check_at is set after a check", bool(lease_after_check1.get("last_check_at")))
    check(
        "1f. the LOCAL socks5 checker was called with the real credentials (needed for the actual "
        "socket connect, never exposed in the result)",
        any(c["username"] == "chk_user" and c["password"] == SECRET_PASSWORD for c in local_checker_holder["instance"].calls),
        str(local_checker_holder["instance"].calls),
    )
    check(
        "1j. only ONE local attempt (rdns=True / socks5h) was made since it succeeded on the first try",
        len(local_checker_holder["instance"].calls) == 1 and local_checker_holder["instance"].calls[0]["rdns"] is True,
        str(local_checker_holder["instance"].calls),
    )

    # ------------------------------------------------------------------
    # 1k. FALLBACK: socks5h (rdns=True) fails, socks5 (rdns=False)
    #     succeeds -- mirrors the live diagnostic where both modes worked.
    # ------------------------------------------------------------------
    lease_fallback_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="40.0.0.3", port=54003, provider_proxy_id="P4-CHECK-FALLBACK",
        login="chk_user3", password="chk_pass_3_secret", manager_key=None, status="active", db_path=tmp_db,
    )
    local_checker_holder["instance"] = _FakeLocalSocksChecker(
        rdns_true_error=RuntimeError("SOCKS5 handshake failed"),
        rdns_false_result={"ip": "31.59.238.203"},
    )
    check3_json = await check_fn(str(lease_fallback_id))
    check3 = _json.loads(check3_json)
    check("3. check_ok=True via the socks5 FALLBACK after socks5h fails", check3.get("check_ok") is True, check3_json)
    check("3b. protocol is socks5 for the fallback path", check3.get("protocol") == "socks5", check3_json)
    check("3c. check result never contains the raw proxy password", "chk_pass_3_secret" not in check3_json, check3_json)
    check(
        "3d. both rdns=True and rdns=False were attempted, in that order (socks5h first, socks5 fallback second)",
        len(local_checker_holder["instance"].calls) == 2
        and local_checker_holder["instance"].calls[0]["rdns"] is True
        and local_checker_holder["instance"].calls[1]["rdns"] is False,
        str(local_checker_holder["instance"].calls),
    )

    # ------------------------------------------------------------------
    # 2. BOTH local attempts fail -> check_ok=False, last_check_ok=0, and
    #    the failure message is a SHORT, SAFE classified reason (from the
    #    REAL _tpag_classify_error_reason) -- never the raw exception text,
    #    even when that raw text DELIBERATELY embeds the real password (to
    #    prove this stays safe end-to-end, not just by convention).
    # ------------------------------------------------------------------
    lease_check_fail_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="40.0.0.2", port=54002, provider_proxy_id="P4-CHECK-FAIL",
        login="chk_user2", password="chk_pass_2_secret", manager_key=None, status="active", db_path=tmp_db,
    )
    local_checker_holder["instance"] = _FakeLocalSocksChecker(
        rdns_true_error=RuntimeError("TCP FAIL after 8000 ms: leaked chk_pass_2_secret"),
        rdns_false_error=RuntimeError("TCP FAIL after 8000 ms: leaked chk_pass_2_secret"),
    )
    check2_json = await check_fn(str(lease_check_fail_id))
    check2 = _json.loads(check2_json)
    check("2a. check command still succeeds structurally (ok=True) even when BOTH local attempts fail", check2.get("ok") is True, check2_json)
    check("2. check_ok=False when both socks5h and socks5 local attempts fail", check2.get("check_ok") is False, check2_json)
    check(
        "2b. check failure message never contains the raw password, even from a leaky underlying exception",
        "chk_pass_2_secret" not in check2_json,
        check2_json,
    )
    check("2c. check failure message never contains the fake API key", SECRET_API_KEY not in check2_json, check2_json)
    check(
        "2e. failure message is a short (<=200 char), classified reason -- never the verbatim exception text",
        check2.get("message") == "TCP connect failed" and len(check2.get("message") or "") <= 200,
        check2_json,
    )

    lease_after_check2 = await storage.proxy_lease_get(lease_check_fail_id, db_path=tmp_db)
    check("2d. storage last_check_ok is 0 after a failed check", lease_after_check2.get("last_check_ok") == 0, repr(lease_after_check2.get("last_check_ok")))
    check(
        "2f. both rdns attempts were made on total failure (socks5h then socks5, no early success)",
        len(local_checker_holder["instance"].calls) == 2,
        str(local_checker_holder["instance"].calls),
    )

    # ------------------------------------------------------------------
    # 2g. generic/unclassified failures fall back to the safe generic
    #     "connection failed" message (never propagate raw exception text).
    # ------------------------------------------------------------------
    lease_generic_fail_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="40.0.0.4", port=54004, provider_proxy_id="P4-CHECK-GENERIC-FAIL",
        login="chk_user4", password="chk_pass_4_secret", manager_key=None, status="active", db_path=tmp_db,
    )
    local_checker_holder["instance"] = _FakeLocalSocksChecker(
        rdns_true_error=Exception("some weird unclassified failure"),
        rdns_false_error=Exception("another weird unclassified failure"),
    )
    check4_json = await check_fn(str(lease_generic_fail_id))
    check4 = _json.loads(check4_json)
    check(
        "2h. an unclassified failure falls back to the generic safe 'connection failed' message",
        check4.get("message") == "connection failed",
        check4_json,
    )
    check("2i. generic-failure result never contains the raw proxy password", "chk_pass_4_secret" not in check4_json, check4_json)

    # ------------------------------------------------------------------
    # 3. check command source no longer calls provider.check_proxy at all
    #    (regression guard for the "Empty result!" Proxy-Seller bug), and
    #    never calls order/make or prolong/make.
    # ------------------------------------------------------------------
    main_src_p4 = Path(str(BASE_DIR / "main.py")).read_text(encoding="utf-8-sig")
    tree_p4 = ast.parse(main_src_p4)
    check_node = next(n for n in tree_p4.body if getattr(n, "name", None) == "_handle_proxy_pool_check_command")
    sync_node = next(n for n in tree_p4.body if getattr(n, "name", None) == "_handle_proxy_pool_sync_command")
    check_src = _src_without_docstring(check_node)
    sync_src = _src_without_docstring(sync_node)
    check(
        "3a. LOCAL PROXY CHECK: active _handle_proxy_pool_check_command source no longer calls "
        "provider.check_proxy (Proxy-Seller's tools/proxy/check proved unreliable for our SOCKS5 leases)",
        "check_proxy" not in check_src, check_src[:200],
    )
    check(
        "3e. active _handle_proxy_pool_check_command source DOES call the local "
        "_tpag_v4_proxy_geo_via_socks check instead",
        "_tpag_v4_proxy_geo_via_socks" in check_src, check_src[:200],
    )
    spend_markers = ("make_ipv4", "prolong_make", "allow_spend", "calc_ipv4", "prolong_calc")
    for marker in spend_markers:
        check(f"3. /proxy_pool_check source never references spend marker {marker!r}", marker not in check_src, "")
        check(f"(sync) /proxy_pool_sync source never references spend marker {marker!r}", marker not in sync_src, "")

    # ------------------------------------------------------------------
    # 4/5/6/12. sync creates a new lease, then a re-sync of the SAME
    #    provider_proxy_id updates it in place (no duplicate row) --
    #    proving identity is provider_proxy_id, not the internal lease id
    #    (which stays constant across both syncs).
    # ------------------------------------------------------------------
    fake_provider_holder["instance"] = _FakeCheckSyncProvider(list_result={
        "status": "success", "errors": [],
        "data": {"items": [
            {"id": "P4-SYNC-A", "ip": "41.0.0.1", "port_socks": 55001, "login": "sync_a", "password": "sync_pw_a", "date_end": "2026-10-01"},
        ]},
    })
    sync1_json = await sync_fn("")
    sync1 = _json.loads(sync1_json)
    check("4a. sync command succeeds", sync1.get("ok") is True, sync1_json)
    check("4. sync created_count == 1 for a brand-new provider_proxy_id", sync1.get("created_count") == 1, sync1_json)
    check("4b. sync total_seen == 1", sync1.get("total_seen") == 1, sync1_json)

    lease_from_sync = await storage.proxy_lease_get_by_provider_proxy_id("proxy_seller", "P4-SYNC-A", db_path=tmp_db)
    check(
        "4c. the synced lease exists in storage with the right host/port",
        lease_from_sync is not None and lease_from_sync.get("host") == "41.0.0.1" and lease_from_sync.get("port") == 55001,
        repr(lease_from_sync),
    )
    lease_from_sync_id = lease_from_sync.get("id")

    fake_provider_holder["instance"] = _FakeCheckSyncProvider(list_result={
        "status": "success", "errors": [],
        "data": {"items": [
            {"id": "P4-SYNC-A", "ip": "41.0.0.99", "port_socks": 55099, "login": "sync_a", "password": "sync_pw_a", "date_end": "2026-11-01"},
        ]},
    })
    sync2_json = await sync_fn("")
    sync2 = _json.loads(sync2_json)
    check("5a. re-sync of the SAME provider_proxy_id succeeds", sync2.get("ok") is True, sync2_json)
    check("5. re-sync of the SAME provider_proxy_id updates (updated_count==1, created_count==0)", sync2.get("updated_count") == 1 and sync2.get("created_count") == 0, sync2_json)

    lease_after_resync = await storage.proxy_lease_get(lease_from_sync_id, db_path=tmp_db)
    check("5b. re-sync updated host/port on the SAME lease row", lease_after_resync.get("host") == "41.0.0.99" and lease_after_resync.get("port") == 55099, repr(lease_after_resync))

    all_leases_after_resync = await storage.proxy_lease_list_all(db_path=tmp_db)
    matching = [r for r in all_leases_after_resync if r.get("provider_proxy_id") == "P4-SYNC-A"]
    check("6. re-sync does not duplicate the provider_proxy_id (exactly one row)", len(matching) == 1, str(len(matching)))
    check(
        "12. sync uses provider_proxy_id as identity -- the internal lease id stayed the same across both syncs",
        lease_after_resync.get("id") == lease_from_sync_id,
        repr((lease_after_resync.get("id"), lease_from_sync_id)),
    )

    # ------------------------------------------------------------------
    # 7/8. sync does NOT overwrite manager_key/status -- assign the
    #    synced lease to a manager, disable it, then re-sync.
    # ------------------------------------------------------------------
    await storage.proxy_lease_assign_to_manager(lease_from_sync_id, "mgr_p4_sync_target", db_path=tmp_db)
    await storage.proxy_lease_set_status(lease_from_sync_id, "disabled", db_path=tmp_db)

    fake_provider_holder["instance"] = _FakeCheckSyncProvider(list_result={
        "status": "success", "errors": [],
        "data": {"items": [
            {"id": "P4-SYNC-A", "ip": "41.0.0.100", "port_socks": 55100, "login": "sync_a", "password": "sync_pw_a", "date_end": "2026-12-01"},
        ]},
    })
    sync3_json = await sync_fn("")
    lease_after_sync3 = await storage.proxy_lease_get(lease_from_sync_id, db_path=tmp_db)
    check("7. sync does NOT overwrite manager_key", lease_after_sync3.get("manager_key") == "mgr_p4_sync_target", repr(lease_after_sync3.get("manager_key")))
    check("8. sync does NOT overwrite status", lease_after_sync3.get("status") == "disabled", repr(lease_after_sync3.get("status")))
    check("(sanity) sync still refreshed host on the assigned+disabled lease", lease_after_sync3.get("host") == "41.0.0.100")

    # ------------------------------------------------------------------
    # 9. sync does not delete a local lease absent from the provider
    #    response.
    # ------------------------------------------------------------------
    unrelated_lease_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="42.0.0.1", port=56001, provider_proxy_id="P4-UNRELATED",
        manager_key=None, status="active", db_path=tmp_db,
    )
    fake_provider_holder["instance"] = _FakeCheckSyncProvider(list_result={
        "status": "success", "errors": [],
        "data": {"items": [
            {"id": "P4-SYNC-A", "ip": "41.0.0.100", "port_socks": 55100, "login": "sync_a", "password": "sync_pw_a"},
        ]},
    })
    await sync_fn("")
    unrelated_after = await storage.proxy_lease_get(unrelated_lease_id, db_path=tmp_db)
    check("9. sync does not delete a local lease absent from the provider response", unrelated_after is not None, repr(unrelated_after))

    # ------------------------------------------------------------------
    # 10. sync skips malformed provider entries without crashing (missing
    #    provider_proxy_id / unparseable port), while still syncing the
    #    well-formed entry among them.
    # ------------------------------------------------------------------
    fake_provider_holder["instance"] = _FakeCheckSyncProvider(list_result={
        "status": "success", "errors": [],
        "data": {"items": [
            {"ip": "43.0.0.1", "port_socks": 57001},  # no "id" -> provider_proxy_id missing
            {"id": "P4-MALFORMED-2", "ip": "43.0.0.2", "port_socks": "not-a-number"},  # unparseable port
            {"id": "P4-GOOD", "ip": "43.0.0.3", "port_socks": 57003},  # well-formed
        ]},
    })
    sync_malformed_json = await sync_fn("")
    sync_malformed = _json.loads(sync_malformed_json)
    check("10a. sync with malformed entries does not crash (ok=True)", sync_malformed.get("ok") is True, sync_malformed_json)
    check(
        "10. sync skips malformed entries (skipped_count >= 2) while still processing the well-formed one",
        sync_malformed.get("skipped_count", 0) >= 2 and sync_malformed.get("created_count", 0) >= 1,
        sync_malformed_json,
    )
    good_lease = await storage.proxy_lease_get_by_provider_proxy_id("proxy_seller", "P4-GOOD", db_path=tmp_db)
    check("10b. the well-formed entry among the malformed ones was still synced", good_lease is not None)

    # ------------------------------------------------------------------
    # 11. sync result never contains a raw password or fake API key,
    #    across every scenario exercised above (even though the fake
    #    provider's entries carried real-looking passwords).
    # ------------------------------------------------------------------
    for label, j in (
        ("sync(create)", sync1_json), ("sync(update)", sync2_json), ("sync(assigned+disabled)", sync3_json),
        ("sync(malformed)", sync_malformed_json), ("check(ok)", check1_json), ("check(fail)", check2_json),
    ):
        check(f"11. {label} result JSON never contains a raw password marker", "sync_pw_a" not in j and SECRET_PASSWORD not in j, j)
        check(f"11b. {label} result JSON never contains the fake API key marker", SECRET_API_KEY not in j, j)


async def run_p61_checks(tmp_db: str) -> None:
    """Stage 6.1E/F: renewal notification schedule/dedupe + guarded
    per-proxy auto-renew. Exercises the REAL main.py functions via AST
    extraction against a REAL temp SQLite DB (proxy_leases +
    proxy_renew_notify_log) and a fake provider/registry. No real
    network, no real spend -- prolong_make is only ever reached through
    an injected fake provider object."""
    import json as _json

    fake_managers: dict = {}
    notifications: list = []  # each: {"kind":..., "title":..., "body":...}
    fake_provider_holder: dict = {"instance": None}

    async def _fake_tpag_registry_get(key):
        row = fake_managers.get(str(key or "").strip())
        return dict(row) if row else None

    async def _fake_create_panel_notification(kind, title, body):
        notifications.append({"kind": kind, "title": title, "body": body})

    async def _fake_never_orphan(lease, usage_map=None, *, context="gate"):
        # CORRECTION B 20260810: _prenew_send_lease_warning/_renewal_
        # wrapped_execute/_prenew_autorenew_one now gate on _prenew_lease_
        # is_orphan -- this test group predates that correction and its
        # fixtures are not set up with real manager assignments (its focus
        # is slot/dedup timing and renewal mechanics, not manager binding,
        # which is covered by tools/proxy_orphan_lease_selftest.py and
        # tools/proxy_role_gate_selftest.py instead).
        return False

    def _fake_pbuy_provider():
        return fake_provider_holder["instance"]

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    ns = _extract_and_exec(
        str(BASE_DIR / "main.py"),
        {
            "_ppool_parse_expires_at",
            "_prenew_slot_for_now",
            "_prenew_warn_eligible",
            "_prenew_autorenew_gates_ok",
            "_prenew_display_name",
            "_prenew_manager_identity_line",
            "_prenew_format_expires_display",
            "_prenew_warn_action_needed",
            "_prenew_send_lease_warning",
            "_prenew_collect_active_seller_leases",
            "_ppool_derive_status",
            "_prenew_preflight_check",
            "_prenew_execute_renewal",
            "_prenew_autorenew_one",
            "_handle_proxy_renew_confirm_command",
            "_handle_proxy_pool_autorenew_command",
            "_pbuy_safe_error",  # the REAL scrubbing function, not a naive lambda --
            # this is what makes test 25b/16b a genuine proof rather than
            # testing our own mock.
            # R2/R3 (Proxy Renewal Reliability, 20260808): _prenew_execute_
            # renewal now verifies post-spend via a bounded read-only
            # refresh, and both spend call sites (_prenew_autorenew_one /
            # _handle_proxy_renew_confirm_command) now route through the
            # SAME idempotent+guarded path -- all of these are REAL
            # extracted main.py functions, not reimplemented here.
            "_prenew_verify_renewal_via_refresh",
            "_prenew_find_provider_entry",
            "_prenew_classify_provider_error",
            "_renewal_guard_check",
            "_renewal_wrapped_execute",
            "_renewal_calc_preview",
            "_renewal_kyiv_today_str",
            "_renewal_float",
            "_renewal_balance",
            "_kyiv_now",
            "_now_utc_iso",
        },
        {
            "datetime": datetime, "timedelta": timedelta,
            "Any": object, "Dict": dict, "List": list, "Optional": object, "Tuple": tuple,
            "TPILOT_DB_PATH": tmp_db,
            "_pbuy_json": _json,
            "_pbuy_provider": _fake_pbuy_provider,
            "_pbuy_call": _fake_pbuy_call,
            "_tpag_registry_get": _fake_tpag_registry_get,
            "_create_panel_notification": _fake_create_panel_notification,
            "_prenew_lease_is_orphan": _fake_never_orphan,
            "_PRENEW_PROXY_TYPE": "ipv4",
            "_PRENEW_PAYMENT_ID": 1,
            "_PBUY_PERIOD_ID": "1m",
            "_PBUY_PERIOD_NAME": "1 month",
            "_PRENEW_SLOT_WINDOW_MIN": 30,
            # fast-test overrides for R2's bounded refresh (not extracted
            # from main.py, so the production 60s/2-attempt values are never
            # shadowed back in) -- unused by this file's own checks since
            # every fake make_result here already carries an advancing
            # date_end, but required for _prenew_execute_renewal to compile.
            "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 1,
            "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0,
        },
    )
    parse_fn = ns["_ppool_parse_expires_at"]
    slot_for_now_fn = ns["_prenew_slot_for_now"]
    warn_eligible_fn = ns["_prenew_warn_eligible"]
    autorenew_gates_fn = ns["_prenew_autorenew_gates_ok"]
    warn_action_needed_fn = ns["_prenew_warn_action_needed"]
    send_lease_warning_fn = ns["_prenew_send_lease_warning"]
    collect_active_fn = ns["_prenew_collect_active_seller_leases"]
    execute_renewal_fn = ns["_prenew_execute_renewal"]
    autorenew_one_fn = ns["_prenew_autorenew_one"]
    confirm_cmd_fn = ns["_handle_proxy_renew_confirm_command"]
    autorenew_cmd_fn = ns["_handle_proxy_pool_autorenew_command"]

    now = datetime(2026, 7, 10, 12, 0, 0)
    tomorrow_ddmmyyyy = (now.date() + timedelta(days=1)).strftime("%d.%m.%Y")
    today_ddmmyyyy = now.date().strftime("%d.%m.%Y")

    # ------------------------------------------------------------------
    # Storage dedupe: proxy_renew_notify_mark_once idempotency (P6.1E,
    # tests 14/16 -- "same slot sends nothing new" / "no 30-min spam").
    # ------------------------------------------------------------------
    first = await storage.proxy_renew_notify_mark_once(9101, "2026-07-10", "tomorrow_noon", db_path=tmp_db)
    second = await storage.proxy_renew_notify_mark_once(9101, "2026-07-10", "tomorrow_noon", db_path=tmp_db)
    third_diff_slot = await storage.proxy_renew_notify_mark_once(9101, "2026-07-10", "today_morning", db_path=tmp_db)
    fourth_diff_date = await storage.proxy_renew_notify_mark_once(9101, "2026-07-11", "tomorrow_noon", db_path=tmp_db)
    check("E1a. proxy_renew_notify_mark_once returns True the first time for a (lease,date,slot) triple", first is True)
    check("14/16. re-running the same (lease,date,slot) sends nothing new -- mark_once returns False", second is False)
    check("E1c. a DIFFERENT slot for the same lease/date is an independent key (True)", third_diff_slot is True)
    check("E1d. a DIFFERENT date for the same lease/slot is an independent key (True)", fourth_diff_date is True)

    purged = await storage.proxy_renew_notify_purge_old("2026-07-11", db_path=tmp_db)
    check("proxy_renew_notify_purge_old removes only strictly-older rows", purged >= 2, str(purged))

    # ------------------------------------------------------------------
    # Slot-window resolution (shared by both the warn and autorenew
    # loops).
    # ------------------------------------------------------------------
    warn_windows = {"tomorrow_noon": (12, 0), "today_morning": (9, 0), "today_day": (13, 0), "today_evening": (17, 0)}
    check("slot_for_now resolves 12:05 to tomorrow_noon", slot_for_now_fn(datetime(2026, 7, 10, 12, 5), warn_windows) == "tomorrow_noon")
    check("slot_for_now resolves 09:10 to today_morning", slot_for_now_fn(datetime(2026, 7, 10, 9, 10), warn_windows) == "today_morning")
    check("slot_for_now returns None well outside any window (03:00)", slot_for_now_fn(datetime(2026, 7, 10, 3, 0), warn_windows) is None)

    # ------------------------------------------------------------------
    # 13/15/6d/6g,6h. tomorrow-expiry -> only tomorrow_noon eligible;
    # today-expiry -> only today_morning eligible (R5, 20260808: down from
    # 3 today_* slots to 1 -- today_day/today_evening were dropped, so
    # _prenew_warn_eligible no longer recognizes them at all); DD.MM.YYYY
    # parses; unparseable expires_at is safely skipped (never eligible,
    # never raises).
    # ------------------------------------------------------------------
    lease_tomorrow = {"expires_at": tomorrow_ddmmyyyy}
    lease_today = {"expires_at": today_ddmmyyyy}
    lease_bad = {"expires_at": "not-a-real-date"}

    check("DD.MM.YYYY parses correctly for day-delta math", parse_fn(tomorrow_ddmmyyyy) is not None)
    check("13. tomorrow-expiry lease is warn-eligible ONLY at tomorrow_noon", warn_eligible_fn(lease_tomorrow, "tomorrow_noon", now) is True)
    check("13b. tomorrow-expiry lease is NOT warn-eligible at today_morning", warn_eligible_fn(lease_tomorrow, "today_morning", now) is False)
    check("15. today-expiry lease is warn-eligible at today_morning", warn_eligible_fn(lease_today, "today_morning", now) is True)
    check(
        "15c. R5: the retired today_day/today_evening slots are no longer recognized at all (not just deduped away)",
        warn_eligible_fn(lease_today, "today_day", now) is False and warn_eligible_fn(lease_today, "today_evening", now) is False,
    )
    check("15b. today-expiry lease is NOT warn-eligible at tomorrow_noon", warn_eligible_fn(lease_today, "tomorrow_noon", now) is False)
    check("invalid expires_at is skipped safely by the warn-eligibility check (never eligible, never raises)", warn_eligible_fn(lease_bad, "tomorrow_noon", now) is False)

    # ------------------------------------------------------------------
    # R5 (Proxy Renewal Reliability, 20260808): grouped multi-lease
    # messages are GONE -- every reminder is now per-lease, and 'no action
    # needed' (auto-renew configured & healthy) means NO card at all.
    # ------------------------------------------------------------------
    fake_managers["mgr_e_1"] = {"manager_key": "mgr_e_1", "status": "active", "display_name": "Mgr E1", "telegram_username": "mgr_e1_user"}

    # A lease with auto-renew OFF needs action -> gets a card.
    lease_needs_action = {"id": 501, "host": "50.0.0.1", "port": 58001, "manager_key": "mgr_e_1", "expires_at": tomorrow_ddmmyyyy, "status": "active", "auto_renew_enabled": 0, "provider_proxy_id": None}
    need_501 = warn_action_needed_fn(lease_needs_action)
    check("R5: auto-renew OFF -> action needed (not silent)", need_501 is not None, need_501)
    notifications.clear()
    await send_lease_warning_fn(lease_needs_action, need_501, is_today=False)
    check("E-single. per-lease T-1 card keeps the 'lease_id:' marker (backward-compat buttons)", len(notifications) == 1 and "lease_id: 501" in notifications[0]["body"], notifications)
    check("R5: T-1 card title says 'завтра'", "завтра" in notifications[0]["title"], notifications)
    check("R5: card carries the manager identity WITH @username", "@mgr_e1_user" in notifications[0]["body"], notifications)
    check("R5: card carries Причина/Что делать", "Причина:" in notifications[0]["body"] and "Что делать:" in notifications[0]["body"], notifications)
    check("25a. per-lease card body never contains the fake API key marker", SECRET_API_KEY not in notifications[0]["body"])

    # A lease with auto-renew ON, a provider identity, and no recent
    # failure needs NO action -> _prenew_warn_action_needed returns None,
    # and the loop (not exercised directly here, see the pure-function
    # check below) would never even call send_lease_warning_fn for it.
    lease_silent = {"id": 502, "host": "50.0.0.2", "port": 58002, "manager_key": "mgr_e_1", "expires_at": tomorrow_ddmmyyyy, "status": "active", "auto_renew_enabled": 1, "provider_proxy_id": "PXY-SILENT-1"}
    need_502 = warn_action_needed_fn(lease_silent)
    check("R5: auto-renew ON + provider identity + no recent failure -> SILENT (None, no alert)", need_502 is None, need_502)

    # A lease that recently failed/went unverified DOES need action, even
    # though auto-renew is configured -- the admin should hear about it.
    # NOTE: _prenew_warn_action_needed compares last_renew_attempt_at
    # against the REAL datetime.utcnow() (production wall-clock), NOT the
    # fixed fake `now` used above for slot-eligibility day-delta math --
    # these two fixtures must anchor to real current time, not `now`.
    real_utcnow = datetime.utcnow()
    lease_recent_failure = {
        "id": 503, "host": "50.0.0.3", "port": 58003, "manager_key": "mgr_e_1", "expires_at": today_ddmmyyyy, "status": "active",
        "auto_renew_enabled": 1, "provider_proxy_id": "PXY-FAILED-1",
        "last_renew_status": "renew_unverified", "last_renew_attempt_at": (real_utcnow - timedelta(hours=2)).isoformat(),
    }
    need_503 = warn_action_needed_fn(lease_recent_failure)
    check("R5: recent unverified attempt -> action needed even though auto-renew is configured", need_503 is not None, need_503)
    notifications.clear()
    await send_lease_warning_fn(lease_recent_failure, need_503 or {}, is_today=True)
    check("R5: T=0 card title says 'сегодня'", notifications and "сегодня" in notifications[0]["title"], notifications)

    # An OLD (stale) failure marker outside the recent-failure window must
    # NOT keep re-triggering a reminder forever.
    lease_stale_failure = {
        "id": 504, "host": "50.0.0.4", "port": 58004, "manager_key": "mgr_e_1", "expires_at": tomorrow_ddmmyyyy, "status": "active",
        "auto_renew_enabled": 1, "provider_proxy_id": "PXY-STALEFAIL-1",
        "last_renew_status": "renew_failed:old provider error", "last_renew_attempt_at": (real_utcnow - timedelta(days=30)).isoformat(),
    }
    need_504 = warn_action_needed_fn(lease_stale_failure)
    check("R5: a STALE (30-day-old) failure marker no longer forces a reminder -- back to silent", need_504 is None, need_504)

    # ------------------------------------------------------------------
    # 22/23/24 (+18/19). auto-renew gates: pure, no I/O. disabled
    # (auto_renew_enabled=0), missing provider_proxy_id, and wrong
    # day-delta must all be ineligible; only a fully-qualified lease at
    # the right slot passes.
    # ------------------------------------------------------------------
    lease_ar_ready = {"auto_renew_enabled": 1, "provider_proxy_id": "PXY-AR-1", "expires_at": tomorrow_ddmmyyyy}
    lease_ar_disabled = {"auto_renew_enabled": 0, "provider_proxy_id": "PXY-AR-2", "expires_at": tomorrow_ddmmyyyy}
    lease_ar_no_pid = {"auto_renew_enabled": 1, "provider_proxy_id": None, "expires_at": tomorrow_ddmmyyyy}
    lease_ar_wrong_day = {"auto_renew_enabled": 1, "provider_proxy_id": "PXY-AR-3", "expires_at": today_ddmmyyyy}

    check("19. auto_renew_enabled=1 + provider_proxy_id + right day-delta -> gates pass", autorenew_gates_fn(lease_ar_ready, "autorenew_pre", now) is True)
    check("18/22. auto_renew_enabled=0 -> gates fail (prolong_make is unreachable)", autorenew_gates_fn(lease_ar_disabled, "autorenew_pre", now) is False)
    check("23. missing provider_proxy_id -> gates fail", autorenew_gates_fn(lease_ar_no_pid, "autorenew_pre", now) is False)
    check("wrong day-delta for autorenew_pre (lease expires today, not tomorrow) -> gates fail", autorenew_gates_fn(lease_ar_wrong_day, "autorenew_pre", now) is False)
    check("today-expiry lease IS eligible at an autorenew_today_* slot", autorenew_gates_fn(lease_ar_wrong_day, "autorenew_today_morning", now) is True)

    # ------------------------------------------------------------------
    # 24. unknown provider_type is excluded by the collector itself
    # (before gates are ever evaluated).
    # ------------------------------------------------------------------
    await storage.proxy_lease_create(provider_type="other_provider", host="61.0.0.1", port=59101, provider_proxy_id="OTHER-1", status="active", db_path=tmp_db)
    seller_lease_id = await storage.proxy_lease_create(provider_type="proxy_seller", host="61.0.0.2", port=59102, provider_proxy_id="SELLER-1", status="active", db_path=tmp_db)
    collected = await collect_active_fn()
    collected_ids = {l.get("id") for l in collected}
    collected_provider_types = {l.get("provider_type") for l in collected}
    check("24. unknown-provider leases are excluded from the active-seller collection", "other_provider" not in collected_provider_types, str(collected_provider_types))
    check("24b. the proxy_seller lease IS included", seller_lease_id in collected_ids)

    # ------------------------------------------------------------------
    # 19/20/21/26. _prenew_execute_renewal (shared spend tail) + manual
    # renew confirm still works via it.
    # ------------------------------------------------------------------
    lease_for_renewal_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="60.0.0.1", port=59001, provider_proxy_id="PXY-RENEW-1",
        manager_key=None, status="active", expires_at="2026-08-01", db_path=tmp_db,
    )
    lease_for_renewal = await storage.proxy_lease_get(lease_for_renewal_id, db_path=tmp_db)

    class _FakeRenewProvider:
        def __init__(self, reference_result=None, make_result=None, make_error=None):
            self._reference_result = reference_result
            self._make_result = make_result
            self._make_error = make_error
            self.make_calls: list = []

        def reference_list(self, proxy_type):
            return self._reference_result

        def prolong_make(self, proxy_type, ids, period_id, payment_id, *, allow_spend=False):
            self.make_calls.append({"ids": ids, "period_id": period_id, "allow_spend": allow_spend})
            if self._make_error is not None:
                raise self._make_error
            return self._make_result

    reference_with_1m = {
        "status": "success", "errors": [],
        "data": {"items": [{"id": 1, "name": "Germany", "alpha3": "DEU", "period": [{"id": "1m", "name": "1 month"}]}]},
    }
    reference_no_1m = {"data": {"items": [{"id": 1, "name": "Germany", "alpha3": "DEU", "period": [{"id": "3m", "name": "3 months"}]}]}}

    fake_provider_holder["instance"] = _FakeRenewProvider(
        reference_result=reference_with_1m,
        make_result={"status": "success", "errors": [], "data": {"date_end": "2026-09-01"}},
    )
    result_success = await execute_renewal_fn(lease_for_renewal, source="test")
    check(
        "19a. _prenew_execute_renewal succeeds and passes allow_spend=True to prolong_make",
        result_success.get("ok") is True and fake_provider_holder["instance"].make_calls[0]["allow_spend"] is True,
        result_success,
    )
    check("20. success updates expires_at from the provider's new date_end", result_success.get("expires_at") == "2026-09-01", result_success)
    lease_after_success = await storage.proxy_lease_get(lease_for_renewal_id, db_path=tmp_db)
    check("20b. success updates last_renew_status to renew_ok in storage", lease_after_success.get("last_renew_status") == "renew_ok", repr(lease_after_success.get("last_renew_status")))
    check("20c. success updates storage expires_at too", lease_after_success.get("expires_at") == "2026-09-01")

    fake_provider_holder["instance"] = _FakeRenewProvider(
        reference_result=reference_with_1m,
        make_error=RuntimeError("simulated provider failure, key leak test: " + SECRET_API_KEY),
    )
    result_failure = await execute_renewal_fn(lease_for_renewal, source="test")
    check("21a. _prenew_execute_renewal reports failure (ok=False) on a provider error", result_failure.get("ok") is False, result_failure)
    lease_after_failure = await storage.proxy_lease_get(lease_for_renewal_id, db_path=tmp_db)
    check(
        "21. failure records a last_renew_status starting with renew_failed",
        str(lease_after_failure.get("last_renew_status") or "").startswith("renew_failed"),
        repr(lease_after_failure.get("last_renew_status")),
    )
    check("25b. failure result never contains the fake API key (scrubbed)", SECRET_API_KEY not in str(result_failure), result_failure)

    fake_provider_holder["instance"] = _FakeRenewProvider(reference_result=reference_no_1m)
    result_no_period = await execute_renewal_fn(lease_for_renewal, source="test")
    check("period_not_found stops cleanly before any prolong_make call", result_no_period.get("ok") is False and result_no_period.get("error") == "period_not_found", result_no_period)
    check("period_not_found -> ZERO prolong_make calls were made", len(fake_provider_holder["instance"].make_calls) == 0, fake_provider_holder["instance"].make_calls)

    fake_provider_holder["instance"] = _FakeRenewProvider(
        reference_result=reference_with_1m,
        make_result={"status": "success", "errors": [], "data": {"date_end": "2026-10-01"}},
    )
    confirm_json = await confirm_cmd_fn(str(lease_for_renewal_id))
    confirm_result = _json.loads(confirm_json)
    check("26. manual /proxy_renew_confirm still succeeds via the shared executor", confirm_result.get("ok") is True, confirm_json)
    check("26b. manual confirm result includes display_name (backward-compat JSON shape)", "display_name" in confirm_result, confirm_json)

    # ------------------------------------------------------------------
    # 9(F)/20/21. auto-renew success/failure admin notifications.
    #
    # R5 (Proxy Renewal Reliability, 20260808): SUCCESS is now SILENT (no
    # Telegram at all -- NO ACTION REQUIRED = NO ALERT) and instead records
    # a proxy_lifecycle_events audit row. FAILURE now carries a classified
    # RU reason/action (R4) and is deduped to at most one card per lease
    # per Kyiv day (R5d) -- a second failed attempt the same day must NOT
    # send a second notification.
    # ------------------------------------------------------------------
    fake_managers["mgr_ar_notify"] = {"manager_key": "mgr_ar_notify", "status": "active", "display_name": "AR Notify Mgr", "telegram_username": "ar_notify_user"}
    lease_for_notify_ok = dict(lease_for_renewal)
    lease_for_notify_ok["manager_key"] = "mgr_ar_notify"

    fake_provider_holder["instance"] = _FakeRenewProvider(
        reference_result=reference_with_1m,
        make_result={"status": "success", "errors": [], "data": {"date_end": "2026-11-01"}},
    )
    notifications.clear()
    await autorenew_one_fn(lease_for_notify_ok)
    check("R5: auto-renew SUCCESS sends ZERO Telegram notifications (silent)", len(notifications) == 0, notifications)
    audit_events = await storage.proxy_lifecycle_events_for_lease(lease_for_notify_ok["id"], db_path=tmp_db)
    check("R5: auto-renew success is recorded as a 'renew_ok' lifecycle audit event instead", any(e.get("event") == "renew_ok" for e in audit_events), audit_events)

    # R3 (Proxy Renewal Reliability, 20260808): auto-renew now goes through
    # _renewal_wrapped_execute's idempotency (proxy_renewal_ops.idempotency_
    # key is unique project-wide, regardless of the op's final status) --
    # reusing lease_for_notify_ok here would collide with the op the
    # success test above just created for the SAME (provider_proxy_id,
    # expires_at, period_id) and be silently skipped (correct new behavior,
    # but not what THIS check is testing), so this uses its OWN distinct
    # lease/provider_proxy_id.
    lease_for_notify_fail_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="60.0.0.2", port=59002, provider_proxy_id="PXY-RENEW-FAIL-1",
        manager_key="mgr_ar_notify", status="active", expires_at="2026-08-01", db_path=tmp_db,
    )
    lease_for_notify_fail = await storage.proxy_lease_get(lease_for_notify_fail_id, db_path=tmp_db)

    fake_provider_holder["instance"] = _FakeRenewProvider(reference_result=reference_no_1m)
    notifications.clear()
    await autorenew_one_fn(lease_for_notify_fail)
    check("21b. auto-renew failure sends exactly one 'proxy_autorenew_failed' notification", len(notifications) == 1 and notifications[0]["kind"] == "proxy_autorenew_failed", notifications)
    check("R4: failure card carries the classified RU reason (invalid_period), not raw provider text", "Период" in notifications[0]["body"] or "период" in notifications[0]["body"], notifications)
    check("R5: failure card carries the manager identity WITH @username", "@ar_notify_user" in notifications[0]["body"], notifications)
    check("R5: lease_id stays the LAST technical line, separated from the human text", notifications[0]["body"].strip().endswith(f"lease_id: {lease_for_notify_fail_id}"), notifications)
    check(
        "25c. auto-renew failure notification never contains a password/API key marker",
        SECRET_API_KEY not in notifications[0]["body"] and SECRET_PASSWORD not in notifications[0]["body"],
        notifications,
    )

    # R5d dedup (direct proof, independent of the idempotency-op skip
    # tested above): the "outcome_notify:<error_category>" per-lease/day/
    # category dedup slot in proxy_renew_notify_log is what protects the
    # ONE case idempotency does NOT cover -- a local guard block, which
    # never creates an op at all and could otherwise re-notify on every
    # single retry. NB-6 fix (review 20260808): the slot is scoped by
    # error_category (not a single flat "outcome_notify") since BL-1 makes
    # pre-spend failures freely retryable, so DIFFERENT real causes can
    # legitimately occur for the same lease on the same day and must NOT
    # all collapse into one dedup slot. This lease's failure is period_
    # not_found (reference_no_1m), which classifies as error_category
    # 'invalid_period' -- pre-mark exactly that scoped slot, exactly as
    # _prenew_autorenew_one itself would on a first send, then prove a
    # FRESH lease (own provider_proxy_id/expiry, so the idempotency-op
    # layer would happily allow a new attempt) still sends nothing once
    # that SAME-category slot is already claimed for today.
    day_str_fn = ns["_renewal_kyiv_today_str"]
    today_kyiv_str = day_str_fn()
    lease_for_dedup_id = await storage.proxy_lease_create(
        provider_type="proxy_seller", host="60.0.0.3", port=59003, provider_proxy_id="PXY-RENEW-DEDUP-1",
        manager_key="mgr_ar_notify", status="active", expires_at="2026-08-01", db_path=tmp_db,
    )
    pre_marked = await storage.proxy_renew_notify_mark_once(lease_for_dedup_id, today_kyiv_str, "outcome_notify:invalid_period", db_path=tmp_db)
    check("(setup) outcome_notify:invalid_period slot pre-claimed for the dedup lease", pre_marked is True)
    lease_for_dedup = await storage.proxy_lease_get(lease_for_dedup_id, db_path=tmp_db)
    fake_provider_holder["instance"] = _FakeRenewProvider(reference_result=reference_no_1m)
    notifications.clear()
    await autorenew_one_fn(lease_for_dedup)
    check(
        "R5d: outcome_notify dedup (same error_category) suppresses the card even though the idempotency-op layer would have allowed this (fresh) attempt",
        len(notifications) == 0,
        notifications,
    )

    # NB-6 companion proof: a DIFFERENT error_category for the SAME lease on
    # the SAME day is NOT suppressed by the invalid_period slot above --
    # BL-1 makes pre-spend failures retryable, so a materially different
    # real cause must still reach the admin exactly once.
    fake_provider_holder["instance"] = None  # provider unset -> not_configured (different category)
    notifications.clear()
    await autorenew_one_fn(lease_for_dedup)
    check(
        "NB-6: a DIFFERENT error_category (not_configured) for the same lease/day is still shown",
        len(notifications) == 1,
        notifications,
    )

    # ------------------------------------------------------------------
    # auto-renew toggle command: updates auto_renew_enabled, never
    # touches the provider (no spend possible from the toggle itself).
    # ------------------------------------------------------------------
    toggle_on_json = await autorenew_cmd_fn(f"{lease_for_renewal_id} 1")
    toggle_on = _json.loads(toggle_on_json)
    check("toggle ON succeeds and reports auto_renew_enabled=True", toggle_on.get("ok") is True and toggle_on.get("auto_renew_enabled") is True, toggle_on_json)
    lease_toggled_on = await storage.proxy_lease_get(lease_for_renewal_id, db_path=tmp_db)
    check("toggle ON persists auto_renew_enabled=1 in storage", int(lease_toggled_on.get("auto_renew_enabled") or 0) == 1)

    toggle_off_json = await autorenew_cmd_fn(f"{lease_for_renewal_id} 0")
    toggle_off = _json.loads(toggle_off_json)
    check("toggle OFF succeeds and reports auto_renew_enabled=False", toggle_off.get("ok") is True and toggle_off.get("auto_renew_enabled") is False, toggle_off_json)
    lease_toggled_off = await storage.proxy_lease_get(lease_for_renewal_id, db_path=tmp_db)
    check("toggle OFF persists auto_renew_enabled=0 in storage", int(lease_toggled_off.get("auto_renew_enabled") or 0) == 0)

    main_src_p61 = Path(str(BASE_DIR / "main.py")).read_text(encoding="utf-8-sig")
    tree_p61 = ast.parse(main_src_p61)
    toggle_node = next(n for n in tree_p61.body if getattr(n, "name", None) == "_handle_proxy_pool_autorenew_command")
    toggle_src = _src_without_docstring(toggle_node)
    for marker in ("_pbuy_provider(", "_pbuy_call(", "prolong_make", "make_ipv4", "allow_spend"):
        check(f"toggle command source never references {marker!r} (pure storage write, no spend)", marker not in toggle_src, "")

    real_allow_spend_lines_p61 = _re.findall(r"^[ \t]*allow_spend=True,[ \t]*$", main_src_p61, _re.MULTILINE)
    check(
        "30. allow_spend=True still appears as a call argument in exactly 2 places project-wide after the Stage 6.1F refactor",
        len(real_allow_spend_lines_p61) == 2,
        str(len(real_allow_spend_lines_p61)),
    )


def run_prefix_checks() -> None:
    """13/18. ppool callback prefixes (including the new P4 check/sync
    ones) exist in panel_bot.py and cannot collide with the buy/renew
    callback namespaces -- a plain source scan (panel_bot.py cannot be
    imported standalone either). Also 14/15: per-command panel timeouts
    for /proxy_pool_check and /proxy_pool_sync."""
    panel_src = Path(str(BASE_DIR / "panel_bot.py")).read_text(encoding="utf-8-sig")
    check("18a. panel_bot.py defines the ppool: callback prefix", 'data.startswith("ppool:")' in panel_src, "")
    check("18b. panel_bot.py routes ppool:list: ", 'data.startswith("ppool:list:")' in panel_src)
    check("18c. panel_bot.py routes ppool:card: ", 'data.startswith("ppool:card:")' in panel_src)
    check("18d. panel_bot.py routes ppool:assign: ", 'data.startswith("ppool:assign:")' in panel_src)
    check("18e. panel_bot.py routes ppool:assign_to: ", 'data.startswith("ppool:assign_to:")' in panel_src)
    check("18f. panel_bot.py routes ppool:assign_confirm: ", 'data.startswith("ppool:assign_confirm:")' in panel_src)
    check("18g. panel_bot.py routes ppool:unassign: ", 'data.startswith("ppool:unassign:")' in panel_src)
    check("18h. panel_bot.py routes ppool:unassign_confirm: ", 'data.startswith("ppool:unassign_confirm:")' in panel_src)
    check("13a. panel_bot.py routes ppool:check: ", 'data.startswith("ppool:check:")' in panel_src)
    check("13b. panel_bot.py routes ppool:sync_confirm (exact match)", 'data == "ppool:sync_confirm"' in panel_src)
    check("13c. panel_bot.py routes ppool:sync (exact match)", 'data == "ppool:sync"' in panel_src)
    # Distinctness by construction: none of these literal prefixes is a
    # prefix of another (character right after the shared stem always
    # differs), so .startswith() routing can never cross-match. ppool:sync
    # and ppool:sync_confirm ARE a real prefix pair (sync IS a prefix of
    # sync_confirm) -- panel_bot.py deliberately uses exact `==` matching
    # for both (checked above), not .startswith(), so this is excluded
    # from the .startswith()-collision matrix below.
    prefixes = (
        "ppool:list:", "ppool:card:", "ppool:assign:", "ppool:assign_to:", "ppool:assign_confirm:",
        "ppool:unassign:", "ppool:unassign_confirm:", "ppool:check:", "wiz:buyproxy:", "renew:",
    )
    for a in prefixes:
        for b in prefixes:
            if a is b:
                continue
            check(f"18i. callback prefix {a!r} does not collide with {b!r}", not a.startswith(b) and not b.startswith(a))
    check("18j. panel_bot.py still defines the Stage 4 buy-confirm prefix (no accidental removal)", 'data.startswith("wiz:buyproxy:confirm:")' in panel_src)
    check("18k. panel_bot.py still defines the Stage 5 renew-confirm prefix (no accidental removal)", 'data.startswith("renew:confirm:")' in panel_src)
    check("18l. panel_bot.py's assign confirm uses a one-time wizard token check (_ppool_assign_confirm_state_ok)", "_ppool_assign_confirm_state_ok" in panel_src)
    check("18m. panel_bot.py's unassign confirm uses a one-time wizard token check (_ppool_unassign_confirm_state_ok)", "_ppool_unassign_confirm_state_ok" in panel_src)
    check("13d. panel_bot.py's sync confirm uses a one-time wizard token check (_ppool_sync_confirm_state_ok)", "_ppool_sync_confirm_state_ok" in panel_src)

    m_check = _re.search(r'command_text\.startswith\("/proxy_pool_check"\).*?\n\s*timeout_s\s*=\s*(\d+)', panel_src, _re.S)
    m_sync = _re.search(r'command_text\.startswith\("/proxy_pool_sync"\).*?\n\s*timeout_s\s*=\s*(\d+)', panel_src, _re.S)
    check(
        "14. panel timeout for /proxy_pool_check is command-specific and >= 60s",
        m_check is not None and int(m_check.group(1)) >= 60,
        m_check.group(0) if m_check else None,
    )
    check(
        "15. panel timeout for /proxy_pool_sync is command-specific and >= 120s",
        m_sync is not None and int(m_sync.group(1)) >= 120,
        m_sync.group(0) if m_sync else None,
    )


def run_p61_ui_checks() -> None:
    """Stage 6.1A/B/C/D: onboarding pool-pick, buy/assign result wording,
    cleaner card + PIN reveal, edit-in-place cleanup. panel_bot.py cannot
    be imported standalone (Telethon client construction at import time),
    so pure helper functions are exercised via AST extraction and the
    rest is verified via source scan -- the same technique used
    throughout this file for main.py."""
    panel_src = Path(str(BASE_DIR / "panel_bot.py")).read_text(encoding="utf-8-sig")
    main_src = Path(str(BASE_DIR / "main.py")).read_text(encoding="utf-8-sig")

    # ------------------------------------------------------------------
    # P6.1A: onboarding pool-pick entry point + no-spend + phone-step
    # continuation.
    # ------------------------------------------------------------------
    check("A1. onboarding proxy-choice screen offers 'Выбрать proxy из пула'", "wiz:add_manager:frompool:" in panel_src)
    check("A2. onboarding picker queries the no-spend 'available' pool filter", '"/proxy_pool_list available"' in panel_src)

    tree_panel = ast.parse(panel_src)

    def _panel_node(name):
        return next(n for n in tree_panel.body if getattr(n, "name", None) == name)

    frompool_assign_src = _src_without_docstring(_panel_node("_frompool_run_assign"))
    spend_markers_ui = ("make_ipv4", "prolong_make", "allow_spend", "calc_ipv4", "prolong_calc", "reference_list", "list_proxies", "check_proxy")
    for marker in spend_markers_ui:
        check(f"A4. onboarding pool-pick assign source never references provider marker {marker!r}", marker not in frompool_assign_src, "")
    # SUPERSEDED by Ф4 (unified auth UX, independently proven in tools/
    # auth_chooser_selftest.py): pool-pick success used to auto-set
    # step="phone" directly, same as the (also now-superseded) buy-success
    # flow. Every proxy-completion site -- buy, from-pool, manual entry --
    # now lands on the unified QR/Phone/Session/TData chooser instead;
    # phone only becomes the active step after an explicit "📱 Номер
    # телефона" tap. Re-derived as the new correct invariant.
    _a35_re = _re.compile(r"""_wizard_set\(\s*chat_id\s*,\s*user_id\s*,\s*['"]add_manager['"]\s*,\s*['"]auth_choice['"]""")
    check("A3/5. onboarding pool-pick success advances the wizard to the unified auth chooser "
          "(auth_choice step, not an auto-started phone step)", bool(_a35_re.search(frompool_assign_src)))
    check("A3/5b. pool-pick success shows the actual chooser screen (_auth_chooser_buttons('add', ...))",
          "_auth_chooser_buttons(\"add\"" in frompool_assign_src or "_auth_chooser_buttons('add'" in frompool_assign_src)

    # ------------------------------------------------------------------
    # P6.1B: buy/assign result wording is conditional on check_ok, and a
    # dedicated no-spend recovery-buttons helper exists.
    # ------------------------------------------------------------------
    pbuy_result_src = _src_without_docstring(_panel_node("_pbuy_result_text"))
    check("B1. buy result text has a success header", "Прокси куплен и настроен" in pbuy_result_src)
    check("B1b. buy result text ALSO has a distinct warning header for a failed guard", "проверка не прошла" in pbuy_result_src)
    check("B1c. buy result header is conditioned on check_ok (not hardcoded)", "check_ok" in pbuy_result_src)

    assign_result_src = _src_without_docstring(_panel_node("_ppool_assign_result_text"))
    check("B (assign). pool-assign result text is ALSO conditioned on check_ok", "check_ok" in assign_result_src and "проверка не прошла" in assign_result_src)

    guard_buttons_src = _src_without_docstring(_panel_node("_ppool_guard_failed_buttons"))
    check("B4. failed-guard buttons include check-again", "ppool:check:" in guard_buttons_src)
    check("B4b. failed-guard buttons include a no-spend refresh (sync)", "ppool:sync" in guard_buttons_src)
    check("B4c. failed-guard buttons include opening the card", "ppool:card:" in guard_buttons_src)
    check("B4d. failed-guard buttons include picking another proxy from the pool", "wiz:add_manager:frompool:" in guard_buttons_src)
    for marker in spend_markers_ui:
        check(f"B5. failed-guard buttons helper source never references provider marker {marker!r}", marker not in guard_buttons_src, "")

    # ------------------------------------------------------------------
    # P6.1C: cleaner card (pure function, called directly with real
    # data) + PIN reveal never uses panel_commands.
    # ------------------------------------------------------------------
    ns_card = _extract_and_exec(
        str(BASE_DIR / "panel_bot.py"),
        # PROXY LIFECYCLE SYNC 20260721: _ppool_card_text now also calls the
        # real _plc_lifecycle_line (desired-vs-observed display) -- extracted
        # alongside it. Its own dict constant is injected as a fake here
        # (same pattern as _PPOOL_STATUS_LABELS below) since this helper's
        # _extract_and_exec only pulls Function/Class defs, not module-level
        # Assign constants.
        # NB-4 fix (Proxy Renewal Reliability review, 20260808): _ppool_card_text's
        # details block now calls _prenew_last_status_display instead of
        # inlining the raw last_renew_status -- extracted alongside it; its
        # own dict constant is injected as a fake here, same pattern as
        # _PPOOL_STATUS_LABELS/_PLC_LIFECYCLE_LABELS above.
        {"_ppool_card_text", "_plc_lifecycle_line", "_prenew_last_status_display"},
        {
            "Optional": object,
            "Any": object,
            "_PPOOL_STATUS_LABELS": {
                "free": "Свободен", "assigned": "Назначен", "orphaned": "Осиротевший",
                "expired": "Истёк", "disabled": "Отключён",
            },
            "_PLC_LIFECYCLE_LABELS": {
                "enable_required": "⚠️ Требуется включить автопродление в кабинете Proxy-Seller",
                "disable_required": "⚠️ Требуется выключить автопродление в кабинете Proxy-Seller",
                "active_confirmed_on": "✅ Подтверждено провайдером (ВКЛ)",
                "released_off_confirmed": "✅ Подтверждено провайдером (ВЫКЛ) — снят с активного пула",
                "expired": "⏳ Истёк",
            },
            "_PRENEW_LAST_STATUS_LABELS": {
                "renew_ok": "успешно",
                "renew_unverified": "требуется проверка",
                "deferred": "отложено администратором",
            },
        },
    )
    card_text_fn = ns_card["_ppool_card_text"]
    sample_card_data = {
        "lease_id": 77, "status": "assigned", "host": "1.2.3.4", "port": 50101,
        "login": "u1", "has_password": True, "manager_key": "mgr_x", "manager_display_name": "Mgr X",
        "expires_at": "2026-12-01", "provider_proxy_id": "PXY-1", "provider_order_id": "ORD-1",
        "auto_renew_enabled": False, "scheme": "socks5", "proxy_type": "ipv4",
        "provider_order_number": "BN-1", "last_check_status": "ok", "last_renew_attempt_at": "2026-11-01",
        "last_renew_status": "renew_ok", "created_at": "2026-01-01",
    }
    short_text = card_text_fn(sample_card_data, details=False)
    details_text = card_text_fn(sample_card_data, details=True)
    check("8. short card includes lease_id/status/host/login", all(s in short_text for s in ("lease_id: 77", "1.2.3.4:50101", "Логин: u1")))
    check("8b. short card masks the password (never the raw value)", "••••" in short_text or "****" in short_text)
    check("8c. short card does NOT include the 'Схема' (scheme) technical field", "Схема" not in short_text)
    check("details mode DOES include 'Схема' and provider order number", "Схема" in details_text and "BN-1" in details_text)
    check("card text never contains the literal string 'password' as a raw value indicator beyond the masked line", "u1" not in details_text.split("Пароль:")[1].split("\n")[0] if "Пароль:" in details_text else True)

    # ONBOARDING SOURCE PICK 20260711: _ppool_reveal_password_once was renamed to
    # _ppool_reveal_creds_once (now reads host/port/login/password for the one-line
    # host:port:login:password reveal format, not just the password) -- same
    # security properties still apply, checked against the new name.
    reveal_src = _src_without_docstring(_panel_node("_ppool_reveal_creds_once"))
    check("9/11(design). credentials reveal source NEVER uses _submit_and_wait (bypasses panel_commands entirely)", "_submit_and_wait" not in reveal_src)
    check("reveal source reads directly from the local sqlite connection helper", "_connect_panel_db" in reveal_src)

    check(
        "11. /proxy_pool_card's main.py source never emits a raw 'password' JSON key (only has_password)",
        '"password": lease.get("password")' not in main_src and '"password": str(lease' not in main_src,
    )

    ns_pin = _extract_and_exec(str(BASE_DIR / "panel_bot.py"), {"_ppool_pin_env_value"}, {})
    pin_fn = ns_pin["_ppool_pin_env_value"]
    check("PIN helper source-extracted and callable without crashing (env state depends on the real process env)", isinstance(pin_fn(), str))

    from manager_registry import normalize_manager_key as _real_normalize_manager_key

    ns_frompool_token = _extract_and_exec(
        str(BASE_DIR / "panel_bot.py"),
        {"_frompool_confirm_state_ok"},
        {"Optional": object, "normalize_manager_key": _real_normalize_manager_key},
    )
    frompool_token_fn = ns_frompool_token["_frompool_confirm_state_ok"]
    check("frompool one-time token: matching state is accepted", frompool_token_fn({"wizard": "frompool", "step": "confirm", "payload": {"manager_key": "te", "lease_id": 9}}, "te", 9) is True)
    check("frompool one-time token: no state is rejected", frompool_token_fn(None, "te", 9) is False)
    check("frompool one-time token: mismatched lease_id is rejected", frompool_token_fn({"wizard": "frompool", "step": "confirm", "payload": {"manager_key": "te", "lease_id": 1}}, "te", 9) is False)

    # ------------------------------------------------------------------
    # P6.1D: edit-in-place used for the no-side-effect pool screens; buy/
    # renew results are deliberately left as their own persisted messages.
    # ------------------------------------------------------------------
    for fn_name in ("_ppool_run_list", "_ppool_run_card", "_ppool_run_check", "_ppool_run_sync"):
        fn_src = _src_without_docstring(_panel_node(fn_name))
        check(f"12. {fn_name} uses the edit-in-place helper (_ppool_edit_or_send)", "_ppool_edit_or_send" in fn_src)

    for fn_name in ("_pbuy_run_confirm", "_pbuy_run_recover"):
        fn_src = _src_without_docstring(_panel_node(fn_name))
        check(f"buy/recover result ({fn_name}) is NOT edited in place -- stays its own persisted message", "_ppool_edit_or_send" not in fn_src)


def main() -> int:
    tmp_db = tempfile.mktemp(suffix=".db")
    tmp_db_p2 = tempfile.mktemp(suffix=".db")
    tmp_db_p3 = tempfile.mktemp(suffix=".db")
    tmp_db_p4 = tempfile.mktemp(suffix=".db")
    tmp_db_p61 = tempfile.mktemp(suffix=".db")
    try:
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
            asyncio.run(run_p2_checks(tmp_db_p2))
            asyncio.run(run_p3_checks(tmp_db_p3))
            asyncio.run(run_p4_checks(tmp_db_p4))
            asyncio.run(run_p61_checks(tmp_db_p61))
            run_prefix_checks()
            run_p61_ui_checks()
        finally:
            sys.stdout = real_stdout

        captured = buffer.getvalue()
        check("16. the test password never appeared in ANY printed output during the whole run", SECRET_PASSWORD not in captured)
        check("16b. the fake API key never appeared in ANY printed output during the whole run", SECRET_API_KEY not in captured)
    finally:
        try:
            if os.path.exists(tmp_db_p61):
                os.remove(tmp_db_p61)
        except Exception:
            pass
        try:
            if os.path.exists(tmp_db_p4):
                os.remove(tmp_db_p4)
        except Exception:
            pass
        try:
            if os.path.exists(tmp_db_p3):
                os.remove(tmp_db_p3)
        except Exception:
            pass
        try:
            if os.path.exists(tmp_db_p2):
                os.remove(tmp_db_p2)
        except Exception:
            pass
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
