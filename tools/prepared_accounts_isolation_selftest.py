# -*- coding: utf-8 -*-
"""Phase 3 selftest: status='prepared' isolation from every working/runtime
surface, while remaining visible to storage-level enumeration and proxy
ownership logic.

Uses a throwaway temporary SQLite file only -- never touches the real
project DB (see _guard_temp_db). Pure/offline: no network, no Telegram, no
proxy-provider calls, no purchases. main.py/panel_bot.py/manager_bot.py/
partner_stat_bot.py cannot be imported standalone (Telethon/env side
effects at import time) -- every check against them uses AST extraction
(ast.parse + ast.unparse + exec), matching the established project
technique (see tools/proxy_pool_selftest.py).

Run: python3.12 tools\\prepared_accounts_isolation_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import subprocess
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite  # noqa: E402
import manager_registry  # noqa: E402
import storage  # noqa: E402

MAIN_PY = BASE_DIR / "main.py"
PANEL_BOT_PY = BASE_DIR / "panel_bot.py"
MANAGER_BOT_PY = BASE_DIR / "manager_bot.py"
PARTNER_STAT_BOT_PY = BASE_DIR / "partner_stat_bot.py"

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _guard_temp_db(db_path: str) -> None:
    prod_db_dir = os.path.abspath(os.path.join(str(BASE_DIR), "db"))
    target = os.path.abspath(str(db_path))
    assert target != prod_db_dir and not target.startswith(prod_db_dir + os.sep), \
        f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"


# --- established AST-extraction technique (tools/proxy_pool_selftest.py) ---
def _extract_and_exec(path, names: set, extra_ns: dict) -> dict:
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


def _func_source(path, name: str) -> str:
    """Raw unparsed source of ONE top-level function/class -- used for
    source-scan proofs that don't need to actually execute the function
    (e.g. proving an unmodified predicate is still present)."""
    tree = ast.parse(open(path, encoding="utf-8-sig").read())
    node = next((n for n in tree.body if getattr(n, "name", None) == name), None)
    if node is None:
        raise AssertionError(f"{name} not found in {path}")
    return ast.unparse(node)


_TYPING_SHIM = {"Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list}


async def _ensure_full_schema(db_path: str) -> None:
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    await storage.init_db()
    async with aiosqlite.connect(db_path) as db:
        await storage._proxy_leases_table_ready(db)


async def _insert_manager(db_path: str, manager_key: str, **overrides) -> int:
    fields = dict(
        status="new", is_enabled=1, manual_stopped=0, tg_user_id=None,
        phone="", telegram_username="", first_name="", last_name="",
        display_name=manager_key, session_path="",
        proxy_type="", proxy_host="", proxy_port=None, proxy_enabled=0, proxy_lease_id=None,
        auth_profile="project", owner_user_id=None,
    )
    fields.update(overrides)
    async with aiosqlite.connect(db_path) as db:
        cols = ["manager_key"] + list(fields.keys())
        vals = [manager_key] + list(fields.values())
        placeholders = ",".join("?" * len(cols))
        cur = await db.execute(f"INSERT INTO managers({','.join(cols)}) VALUES ({placeholders})", vals)
        await db.commit()
        return cur.lastrowid


def main() -> int:  # noqa: C901
    with tempfile.TemporaryDirectory(prefix="tpilot_prepacc_isolation_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)
        asyncio.run(_ensure_full_schema(tmp_db))

        # Fixture managers shared by most sections below.
        asyncio.run(_insert_manager(tmp_db, "mgr_active", status="active", is_enabled=1, manual_stopped=0, tg_user_id=1001, telegram_username="active_u", display_name="Active Mgr"))
        asyncio.run(_insert_manager(tmp_db, "mgr_prepared", status="prepared", is_enabled=0, manual_stopped=1, tg_user_id=2002, telegram_username="prepared_u", display_name="Prepared Mgr", proxy_enabled=1, proxy_host="9.9.9.9", proxy_port=1080))
        asyncio.run(_insert_manager(tmp_db, "mgr_new", status="new", is_enabled=1, manual_stopped=0))
        asyncio.run(_insert_manager(tmp_db, "mgr_disabled", status="disabled", is_enabled=0, manual_stopped=1))
        asyncio.run(_insert_manager(tmp_db, "mgr_error", status="error", is_enabled=1, manual_stopped=0))

        # ====================================================================
        # SECTION A (checks 1-6): panel_bot.py working-list filter
        # ====================================================================
        panel_ns = _extract_and_exec(
            PANEL_BOT_PY,
            {"_manager_rows", "_manager_rows_all", "_manager_row_by_key", "_manager_row_by_id"},
            {
                **_TYPING_SHIM,
                "TPILOT_DB_PATH": tmp_db,
                "normalize_manager_key": manager_registry.normalize_manager_key,
                "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
            },
        )
        panel_manager_rows = panel_ns["_manager_rows"]
        panel_manager_rows_all = panel_ns["_manager_rows_all"]
        panel_manager_row_by_key = panel_ns["_manager_row_by_key"]
        panel_manager_row_by_id = panel_ns["_manager_row_by_id"]

        default_all = panel_manager_rows_all()
        default_keys = {r["manager_key"] for r in default_all}
        check("1. panel _manager_rows(_all) default excludes prepared", "mgr_prepared" not in default_keys, str(default_keys))

        included_all = panel_manager_rows_all(include_prepared=True)
        included_keys = {r["manager_key"] for r in included_all}
        check("2. include_prepared=True returns prepared", "mgr_prepared" in included_keys, str(included_keys))

        by_key = panel_manager_row_by_key("mgr_prepared")
        check("3. _manager_row_by_key finds prepared", by_key.get("manager_key") == "mgr_prepared", str(by_key))

        prepared_id = next(r["id"] for r in included_all if r["manager_key"] == "mgr_prepared")
        by_id = panel_manager_row_by_id(prepared_id)
        check("4. _manager_row_by_id finds prepared", by_id.get("manager_key") == "mgr_prepared", str(by_id))

        check("5. ordinary active manager still visible by default", "mgr_active" in default_keys, str(default_keys))

        default_active_only = panel_manager_rows()  # only_active=True, only_enabled=True defaults
        check(
            "6. disabled/new/error unaffected by the prepared fence (still excluded from the strict active+enabled view, exactly as before)",
            {r["manager_key"] for r in default_active_only} == {"mgr_active"},
            str({r["manager_key"] for r in default_active_only}),
        )
        check(
            "6b. disabled/new/error still present in the broad admin view (_manager_rows_all), same as before the prepared fence",
            {"mgr_disabled", "mgr_new", "mgr_error"} <= default_keys,
            str(default_keys),
        )

        # ====================================================================
        # SECTION B (checks 7-9): main.py reporting / tghealth
        # ====================================================================
        tghealth_notifications: list = []

        async def _fake_tp_hg_queue_check_for_manager(key, *, user_id=0, timeout_sec=45):
            tghealth_notifications.append(key)
            return True, f"fake-ok:{key}"

        async def _fake_tp_hg_format_status(target="all"):
            return "fake-status"

        main_ns_b = _extract_and_exec(
            MAIN_PY,
            {"_manager_rows_for_reporting", "_tp_hg_norm_key", "_tp_hg_ensure_tables", "_tp_hg_list_rows", "_tp_hg_check_command"},
            {
                **_TYPING_SHIM,
                "os": os, "aiosqlite": aiosqlite, "asyncio": asyncio,
                "TPILOT_DB_PATH": tmp_db,
                "BASE_DIR": BASE_DIR,
                "registry_normalize_manager_key": manager_registry.normalize_manager_key,
                "build_manager_paths": manager_registry.build_manager_paths,
                "manager_list_rows": storage.manager_list_rows,
                "TP_HG_STATUS_UNKNOWN": "unknown",
                "_tp_hg_queue_check_for_manager": _fake_tp_hg_queue_check_for_manager,
                "_tp_hg_format_status": _fake_tp_hg_format_status,
            },
        )
        reporting_rows = asyncio.run(main_ns_b["_manager_rows_for_reporting"]())
        reporting_keys = {r["manager_key"] for r in reporting_rows}
        check("7. _manager_rows_for_reporting excludes prepared", "mgr_prepared" not in reporting_keys and "mgr_active" in reporting_keys, str(reporting_keys))

        hg_rows_all = asyncio.run(main_ns_b["_tp_hg_list_rows"]("all"))
        hg_keys = {r["manager_key"] for r in hg_rows_all}
        check("8. /tghealth status all (_tp_hg_list_rows) excludes prepared", "mgr_prepared" not in hg_keys and "mgr_active" in hg_keys, str(hg_keys))

        tghealth_notifications.clear()
        asyncio.run(main_ns_b["_tp_hg_check_command"]("all", user_id=1))
        check(
            "9. /tghealth check all does not enqueue a runtime check for prepared",
            "mgr_prepared" not in tghealth_notifications and "mgr_active" in tghealth_notifications,
            str(tghealth_notifications),
        )

        # ====================================================================
        # SECTION C (checks 10, 26): manager_bot.py scope_mode='all'
        # ====================================================================
        async def _insert_access_user_all_scope():
            async with aiosqlite.connect(tmp_db) as db:
                await db.execute(
                    "INSERT INTO access_users(tg_user_id, scope_mode, is_enabled, created_at, updated_at) VALUES (?,?,?,?,?)",
                    (555, "all", 1, "", ""),
                )
                await db.commit()

        asyncio.run(_insert_access_user_all_scope())

        class _FakeLog:
            def warning(self, *a, **k):
                pass

        manager_bot_ns = _extract_and_exec(
            MANAGER_BOT_PY,
            {"_all_manager_keys", "_linked_manager_keys", "_access_user", "_connect", "_norm_key"},
            {
                **_TYPING_SHIM,
                "sqlite3": sqlite3,
                "Path": Path,
                "TPILOT_DB_PATH": tmp_db,
                "log": _FakeLog(),
            },
        )
        mb_all_keys = manager_bot_ns["_all_manager_keys"]()
        check("10. manager_bot _all_manager_keys excludes prepared", "mgr_prepared" not in mb_all_keys and "mgr_active" in mb_all_keys, str(mb_all_keys))

        mb_linked_all_scope = manager_bot_ns["_linked_manager_keys"](555)
        check(
            "26. scope_mode='all' ManagerBot access resolution excludes prepared (_linked_manager_keys)",
            "mgr_prepared" not in mb_linked_all_scope and "mgr_active" in mb_linked_all_scope,
            str(mb_linked_all_scope),
        )

        # ====================================================================
        # SECTION D (checks 11-15): startup/recovery/scheduler/bizlink/monitor
        # -- source-scan proofs (no execution): these functions are
        # deliberately UNCHANGED this phase because manager_list_rows(...,
        # only_active=True) / the raw triple-predicate SQL already excludes
        # status='prepared' (only 'active' passes only_active=True). Proven
        # here by asserting the exact call/predicate is still present,
        # unweakened, rather than by re-implementing each function's full
        # dependency graph.
        # ====================================================================
        autostart_src = _func_source(MAIN_PY, "_controller_autostart_registry_managers")
        check(
            "11. _controller_autostart_registry_managers still gated on only_active=True (prepared excluded, unmodified)",
            "manager_list_rows(include_removed=False, only_enabled=True, only_active=True)" in autostart_src,
            autostart_src,
        )

        recovery_src = _func_source(MAIN_PY, "_manager_recovery_once")
        check(
            "12. _manager_recovery_once (original) still gated on only_active=True (prepared excluded, unmodified)",
            "manager_list_rows(include_removed=False, only_enabled=True, only_active=True)" in recovery_src,
            recovery_src[:200],
        )

        schedule_src = _func_source(MAIN_PY, "_schedule_summary_if_due")
        check(
            "13. _schedule_summary_if_due still gated on only_active=True (prepared excluded, unmodified)",
            "manager_list_rows(include_removed=False, only_enabled=True, only_active=True)" in schedule_src,
            schedule_src[:200],
        )

        bizlink_src = _func_source(MAIN_PY, "_bizlink_autocreate_if_due")
        check(
            "14. _bizlink_autocreate_if_due still gated on only_active=True (prepared excluded, unmodified)",
            "manager_list_rows(include_removed=False, only_enabled=True, only_active=True)" in bizlink_src,
            bizlink_src[:200],
        )

        # _tpag_monitor_once has two defs (override chain); the LAST one in
        # the file is active (CLAUDE.md contract) -- take the last match.
        monitor_src_full = open(MAIN_PY, encoding="utf-8-sig").read()
        monitor_tree = ast.parse(monitor_src_full)
        monitor_nodes = [n for n in monitor_tree.body if getattr(n, "name", None) == "_tpag_monitor_once"]
        check("15a. _tpag_monitor_once has at least one def", len(monitor_nodes) >= 1, str(len(monitor_nodes)))
        active_monitor_src = ast.unparse(monitor_nodes[-1]) if monitor_nodes else ""
        check(
            "15. active _tpag_monitor_once (supervisor sweep) still gated on the full active/enabled/not-manual-stopped predicate (prepared excluded, unmodified)",
            "COALESCE(status,'')='active'" in active_monitor_src
            and "COALESCE(is_enabled,1)=1" in active_monitor_src
            and "COALESCE(manual_stopped,0)=0" in active_monitor_src,
            active_monitor_src[:300],
        )

        # ====================================================================
        # SECTION E (check 16): partner_stat_bot.py source-based exclusion
        # ====================================================================
        con = sqlite3.connect(tmp_db)
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES ('mgr_active', 'src_1')")
        con.commit()
        con.close()

        partner_ns = _extract_and_exec(
            PARTNER_STAT_BOT_PY,
            {"_manager_rows_for_source", "_source_links", "_connect", "_norm_key"},
            {
                **_TYPING_SHIM,
                "sqlite3": sqlite3,
                "os": os,
                "re": re,
                "timedelta": __import__("datetime").timedelta,
                "TPILOT_DB_PATH": tmp_db,
            },
        )
        partner_rows = partner_ns["_manager_rows_for_source"]("src_1")
        partner_keys = {_norm for _norm in (r.get("manager_key") for r in partner_rows)}
        check(
            "16. partner-stat source selection: prepared (no source link) is absent; linked active manager present",
            "mgr_prepared" not in partner_keys and "mgr_active" in partner_keys,
            str(partner_keys),
        )

        # ====================================================================
        # SECTION F (checks 17, 27): storage-level primitive stays unfiltered
        # ====================================================================
        raw_rows = asyncio.run(storage.manager_list_rows(include_removed=False))
        raw_keys = {r["manager_key"] for r in raw_rows}
        check("17. storage.manager_list_rows(include_removed=False) STILL returns prepared", "mgr_prepared" in raw_keys, str(raw_keys))

        manager_list_rows_src = _func_source(Path(__file__).resolve().parent.parent / "storage.py", "manager_list_rows")
        check(
            "27. storage.manager_list_rows source has NO 'prepared' status filter (never patched at the primitive)",
            "prepared" not in manager_list_rows_src,
            manager_list_rows_src,
        )

        # ====================================================================
        # SECTION G (checks 18-21, 28): proxy pool / usage map / assign guard
        # ====================================================================
        # Create the prepared-owned lease + link both sides (proxy_leases +
        # managers.proxy_lease_id), exactly mirroring _pbuy_apply_lease_to_
        # manager's real write shape.
        lease_id = asyncio.run(storage.proxy_lease_create(
            provider_type="proxy_seller", host="9.9.9.9", port=1080, provider_proxy_id="P-PREP-1",
            login="u", password="pw", manager_key="mgr_prepared", status="active", db_path=tmp_db,
        ))
        asyncio.run(storage.proxy_lease_assign_to_manager(lease_id, "mgr_prepared", db_path=tmp_db))

        # storage.manager_get reads the live TPILOT_DB_PATH global by
        # default (no db_path kwarg), so _tpag_registry_get is faked here as
        # a small real async shim against the temp DB via manager_registry's
        # own sync reader instead.
        async def _tpag_registry_get_g(key):
            return manager_registry.get_manager_row_from_db_sync(tmp_db, key)

        ppool_ns = _extract_and_exec(
            MAIN_PY,
            {"_ppool_usage_key", "_ppool_manager_usage_map", "_ppool_usage_public", "_ppool_derive_status", "_ppool_parse_expires_at"},
            {
                **_TYPING_SHIM,
                "datetime": datetime,
                "manager_list_rows": storage.manager_list_rows,
                "registry_normalize_manager_key": manager_registry.normalize_manager_key,
                "_tpag_registry_get": _tpag_registry_get_g,
            },
        )

        usage_map = asyncio.run(ppool_ns["_ppool_manager_usage_map"]())
        usage_key = ppool_ns["_ppool_usage_key"]("9.9.9.9", 1080)
        usage_entry_keys = {e["manager_key"] for e in usage_map.get(usage_key, [])}
        check("18. _ppool_manager_usage_map sees prepared (proxy_enabled=1)", "mgr_prepared" in usage_entry_keys, str(usage_map))

        usage_map_src = _func_source(MAIN_PY, "_ppool_manager_usage_map")
        check(
            "28. _ppool_manager_usage_map source still calls the raw storage primitive manager_list_rows(include_removed=False), not a panel-filtered helper",
            "manager_list_rows(include_removed=False)" in usage_map_src and "_manager_rows" not in usage_map_src,
            usage_map_src,
        )

        lease_row = asyncio.run(storage.proxy_lease_get(lease_id, db_path=tmp_db))
        status_for_prepared_lease, _mgr = asyncio.run(ppool_ns["_ppool_derive_status"](lease_row, usage_map=usage_map))
        check("19. _ppool_derive_status for prepared-owned lease == 'assigned'", status_for_prepared_lease == "assigned", status_for_prepared_lease)

        avail_match = re.search(r"_PPOOL_AVAILABLE_STATUSES\s*=\s*(\([^\n]*\))", open(MAIN_PY, encoding="utf-8-sig").read())
        check("20a. _PPOOL_AVAILABLE_STATUSES literal found in main.py", avail_match is not None)
        available_statuses = ast.literal_eval(avail_match.group(1)) if avail_match else ()
        check(
            "20. prepared-owned lease status ('assigned') is NOT in _PPOOL_AVAILABLE_STATUSES",
            status_for_prepared_lease not in available_statuses,
            f"status={status_for_prepared_lease!r} available={available_statuses!r}",
        )

        # check 21: mgr_prepared already OCCUPIES 9.9.9.9:1080 via its own
        # proxy_enabled=1/proxy_host/proxy_port fields (usage-map fact,
        # independent of any one lease's tracked ownership -- see
        # _ppool_manager_usage_map). A genuinely SEPARATE, unrelated lease
        # at the SAME host:port (no tracked owner of its own) must refuse
        # assignment to a THIRD manager without 'multi' -- this is the real
        # double-allocation guard. (Reassigning the SAME already-owned
        # lease_id away from its own current owner is a distinct, INTENDED
        # single-owner transfer and correctly succeeds without 'multi' --
        # not what this check is about.)
        lease_id_2 = asyncio.run(storage.proxy_lease_create(
            provider_type="proxy_seller", host="9.9.9.9", port=1080, provider_proxy_id="P-PREP-2",
            login="u2", password="pw2", manager_key=None, status="active", db_path=tmp_db,
        ))
        asyncio.run(_insert_manager(tmp_db, "mgr_second_target", status="active", is_enabled=1, manual_stopped=0))
        fake_managers_21: Dict[str, Dict[str, Any]] = {
            "mgr_prepared": {"manager_key": "mgr_prepared", "status": "prepared", "display_name": "Prepared Mgr", "proxy_host": "9.9.9.9", "proxy_port": 1080, "proxy_enabled": 1, "telegram_username": "", "proxy_lease_id": lease_id},
            "mgr_second_target": {"manager_key": "mgr_second_target", "status": "active", "display_name": "Second Target", "proxy_host": "", "proxy_port": None, "proxy_enabled": 0, "telegram_username": "", "proxy_lease_id": None},
        }

        async def _fake_tpag_registry_get_21(key):
            return fake_managers_21.get(str(key or "").strip())

        async def _fake_tpag_registry_set_fields_21(key, **fields):
            row = fake_managers_21.get(str(key or "").strip())
            if row is not None:
                row.update(fields)

        async def _fake_tpag_run_guard_21(key, *, source, force=False):
            return True, "ok"

        async def _fake_manager_list_rows_21(include_removed: bool = False):
            return [dict(v) for v in fake_managers_21.values()]

        import json as _json21

        assign_ns = _extract_and_exec(
            MAIN_PY,
            {
                "_ppool_parse_expires_at", "_ppool_derive_status", "_pbuy_apply_lease_to_manager",
                "_ppool_disable_manager_proxy_fields", "_handle_proxy_pool_assign_command",
                "_ppool_manager_usage_map", "_ppool_usage_key", "_ppool_usage_public",
            },
            {
                **_TYPING_SHIM,
                "datetime": datetime,
                "_tpag_registry_get": _fake_tpag_registry_get_21,
                "_tpag_registry_set_fields": _fake_tpag_registry_set_fields_21,
                "_tpag_run_guard": _fake_tpag_run_guard_21,
                "_manager_proxy_info_text": lambda row: f"proxy_host={row.get('proxy_host')}",
                "_pbuy_safe_error": lambda e: str(e),
                "_now_utc_iso": lambda: "2026-08-10T00:00:00",
                "TPILOT_DB_PATH": tmp_db,
                "_pbuy_json": _json21,
                "registry_normalize_manager_key": manager_registry.normalize_manager_key,
                "manager_list_rows": _fake_manager_list_rows_21,
            },
        )
        assign_result_json = asyncio.run(assign_ns["_handle_proxy_pool_assign_command"](f"{lease_id_2} mgr_second_target"))
        assign_result = _json21.loads(assign_result_json)
        check(
            "21. assigning a SEPARATE lease at a host:port already occupied by a prepared account, to a second manager without 'multi' -> proxy_in_use",
            assign_result.get("error") == "proxy_in_use",
            assign_result_json,
        )

        # ====================================================================
        # SECTION H (checks 22-25): proxy renewal role classifier (pure fns)
        # ====================================================================
        prenew_ns = _extract_and_exec(
            MAIN_PY,
            {"_prenew_lease_role", "_prenew_autorenew_gates_ok", "_ppool_usage_key", "_ppool_parse_expires_at"},
            {**_TYPING_SHIM, "datetime": datetime},
        )
        prenew_lease_role = prenew_ns["_prenew_lease_role"]
        prenew_autorenew_gates_ok = prenew_ns["_prenew_autorenew_gates_ok"]

        prepared_manager_row = {"manager_key": "mgr_prepared", "status": "prepared", "proxy_enabled": 1, "proxy_lease_id": lease_id, "proxy_host": "9.9.9.9", "proxy_port": 1080}
        prepared_lease_dict = {"id": lease_id, "manager_key": "mgr_prepared", "host": "9.9.9.9", "port": 1080, "auto_renew_enabled": 1, "provider_proxy_id": "P-PREP-1", "expires_at": ""}

        role_for_prepared = prenew_lease_role(prepared_lease_dict, prepared_manager_row, usage_map={})
        check("22. _prenew_lease_role for prepared-owned lease == 'managed'", role_for_prepared == "managed", role_for_prepared)

        gates_ok = prenew_autorenew_gates_ok(
            {**prepared_lease_dict, "expires_at": "2026-01-02"}, "autorenew_pre", datetime(2026, 1, 1, 12, 0, 0)
        )
        check(
            "23. _prenew_autorenew_gates_ok does not reject a lease solely because its owner's status is 'prepared' (the function never even receives manager status -- only lease fields)",
            gates_ok is True,
            str(gates_ok),
        )

        disabled_manager_row = {**prepared_manager_row, "proxy_enabled": 0}
        role_when_disabled = prenew_lease_role(prepared_lease_dict, disabled_manager_row, usage_map={})
        check("24. prepared + proxy_enabled=0 -> NOT managed", role_when_disabled != "managed", role_when_disabled)

        archived_manager_row = {**prepared_manager_row, "status": "archived"}
        role_when_archived = prenew_lease_role(prepared_lease_dict, archived_manager_row, usage_map={})
        check("25. prepared->archived -> NOT managed", role_when_archived != "managed", role_when_archived)

        # ====================================================================
        # SECTION I (check 29): no new SQL/Telegram/proxy logic in the
        # feature package (proven by re-running the same forbidden-token
        # scan Phase 1/2 already established, AST-aware so docstrings/plain
        # data string literals cannot trigger a false positive).
        #
        # TPILOT PREPARED ACCOUNTS PHASE 5: features/prepared_accounts/
        # service.py is now a real, approved file (this phase's write
        # scope) that is EXPLICITLY required to call connect()/is_user_
        # authorized()/get_me() on an INJECTED, already-authenticated
        # client (the "LIVE VERIFY SEQUENCE") -- that is orchestration of a
        # read-only identity re-check, not a duplicated Telegram auth
        # implementation. Only these three method-call tokens are excepted,
        # and ONLY for service.py; constructing a client, qr_login,
        # send_code_request, sign_in, and SessionPasswordNeeded remain
        # fully forbidden there too, and every token stays fully enforced
        # for every other file (in particular import_offline.py, which the
        # approved plan's zero-network contract scopes these same three
        # tokens to outright).
        # ====================================================================
        forbidden_tokens = (
            "qr_login", "send_code_request", "sign_in", "SessionPasswordNeeded",
            "TelegramClient", "connect(", "get_me", "is_user_authorized",
            "opentele", "ProxySellerProvider", "allow_spend", "sqlite3", "aiosqlite",
            "manager_add", "manager_set_fields", "_spawn_manager_process",
        )
        per_file_token_exceptions = {
            str(Path("features") / "prepared_accounts" / "service.py"): {"connect(", "get_me", "is_user_authorized"},
        }

        def _code_without_string_literals(source: str) -> str:
            t = ast.parse(source)
            for node in ast.walk(t):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    node.value = ""
            ast.fix_missing_locations(t)
            return ast.unparse(t)

        feature_root = BASE_DIR / "features"
        offenders = []
        for path in sorted(p for p in feature_root.rglob("*.py") if "__pycache__" not in p.parts):
            cleaned = _code_without_string_literals(path.read_text(encoding="utf-8-sig"))
            rel = str(path.relative_to(BASE_DIR))
            exceptions = per_file_token_exceptions.get(rel, set())
            hits = [tok for tok in forbidden_tokens if tok in cleaned and tok not in exceptions]
            if hits:
                offenders.append((rel, hits))
        check("29. features/prepared_accounts/* still has no forbidden duplicated-implementation markers (per-file exception: service.py's injected-client live-verify calls)", not offenders, str(offenders))

        # ====================================================================
        # SECTION J (checks 30-31): existing Phase 1/2 selftests still GREEN
        # ====================================================================
        pkg_selftest = BASE_DIR / "tools" / "prepared_accounts_pkg_selftest.py"
        storage_selftest = BASE_DIR / "tools" / "prepared_accounts_storage_selftest.py"
        proc_pkg = subprocess.run([sys.executable, str(pkg_selftest)], capture_output=True, text=True, timeout=60)
        check("30. Phase 1 package selftest GREEN", proc_pkg.returncode == 0, f"returncode={proc_pkg.returncode} tail={proc_pkg.stdout[-400:]}")
        proc_storage = subprocess.run([sys.executable, str(storage_selftest)], capture_output=True, text=True, timeout=60)
        check("31. Phase 2 storage selftest GREEN", proc_storage.returncode == 0, f"returncode={proc_storage.returncode} tail={proc_storage.stdout[-400:]}")

        print()
        if FAILURES:
            print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
            return 1
        print("SELFTEST OK: all checks passed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
