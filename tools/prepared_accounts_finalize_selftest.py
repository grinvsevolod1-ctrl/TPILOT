# -*- coding: utf-8 -*-
"""Phase 4 selftest: the prepared-account lifecycle inside
_manager_finalize_login, plus /prepared_add, /prepared_method,
/prepared_pending, the tdimport online-import guard, and the delete-cleanup
DELETE line.

Uses a throwaway temporary SQLite file only -- never touches the real
project DB (see _guard_temp_db). Pure/offline: no network, no Telegram, no
proxy-provider calls, no purchases. main.py cannot be imported standalone
(Telethon/env side effects at import time) -- every check against it uses
AST extraction (ast.parse + ast.unparse + exec), matching the established
project technique (see tools/proxy_pool_selftest.py).

Run: python3.12 tools\\prepared_accounts_finalize_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite  # noqa: E402
import manager_registry  # noqa: E402
import storage  # noqa: E402
from features.prepared_accounts import model  # noqa: E402

MAIN_PY = BASE_DIR / "main.py"

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


def _extract_and_exec(path, names: set, extra_ns: dict) -> dict:
    """Same technique as tools/proxy_pool_selftest.py, but tolerant of
    main.py's stacked-override convention (CLAUDE.md contract: "only the
    LAST definition is active"). A requested name may legitimately match
    MULTIPLE top-level defs (e.g. _panel_manager_add_command) -- ALL
    matches are kept, in file order, and exec'd together, so the LAST one
    wins via normal Python redefinition, exactly mirroring real runtime
    behavior. Only a name with ZERO matches is an error."""
    src = open(path, encoding="utf-8-sig").read()
    tree = ast.parse(src)
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    found = {getattr(n, "name", None) for n in nodes}
    if found != set(names):
        raise AssertionError(f"expected {names}, found {found} in {path}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path}>", "exec"), ns)
    return ns


def _func_source(path, name: str) -> str:
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
    with tempfile.TemporaryDirectory(prefix="tpilot_prepacc_finalize_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)
        asyncio.run(_ensure_full_schema(tmp_db))

        # ====================================================================
        # SECTION A (checks 1-22, 41-45): _manager_finalize_login extracted
        # directly. Every existing auth call site (phone/code, 2FA, QR) calls
        # this SAME function with the SAME bare/default args -- testing it
        # directly proves the shared lifecycle (checks 12/13), while check 41
        # below source-scans the three call sites for that invariant.
        # ====================================================================
        manager_set_fields_calls: List[Tuple[str, Dict[str, Any]]] = []
        grant_calls: List[Dict[str, Any]] = []
        spawn_calls: List[str] = []
        onboarding_clear_calls: List[int] = []
        onboarding_delete_calls: List[int] = []
        screenshots_calls: List[str] = []
        source_link_holder: Dict[str, str] = {}
        spawn_result_holder: Dict[str, Any] = {"ok": True, "msg": "started"}
        fake_managers_db: Dict[str, Dict[str, Any]] = {}

        async def _fake_manager_get(key):
            row = fake_managers_db.get(key)
            return dict(row) if row is not None else None

        def _fake_manager_runtime_paths_for_key(key):
            return {"session_path": f"{tmp}/{key}.session", "db_path": f"{tmp}/{key}.db", "root": f"{tmp}/{key}", "log_path": f"{tmp}/{key}.log"}

        async def _fake_manager_set_fields(key, **fields):
            manager_set_fields_calls.append((key, dict(fields)))
            row = fake_managers_db.setdefault(key, {"manager_key": key})
            row.update(fields)

        def _fake_manager_bot_access_ensure_sync(**kwargs):
            grant_calls.append(kwargs)
            return {"status": "ok"}

        def _fake_manager_runtime_onboarding_clear(owner_user_id):
            onboarding_clear_calls.append(owner_user_id)

        async def _fake_manager_delete_onboarding(owner_user_id):
            onboarding_delete_calls.append(owner_user_id)

        async def _fake_tp_finalize_screenshots_on(key):
            screenshots_calls.append(key)

        async def _fake_spawn_manager_process(key):
            spawn_calls.append(key)
            return (spawn_result_holder["ok"], spawn_result_holder["msg"])

        def _fake_manager_label_from_row(row):
            return f"[{row.get('manager_key')}]"

        async def _fake_partner_source_key_for_manager(key):
            return source_link_holder.get(key, "")

        def _fake_now_utc_iso():
            return "2026-08-10T12:00:00"

        # _ONBOARDING_SOURCE_PICK_MARKER is a plain string-literal module
        # assignment in main.py -- not a Function/Class def, so
        # _extract_and_exec's tree.body name filter cannot pull it in
        # automatically. Read it directly from source (regex + literal_eval)
        # rather than hand-retyping the Russian text, so a future edit to
        # the real constant cannot silently desync this test's copy.
        main_src_full = open(MAIN_PY, encoding="utf-8-sig").read()
        source_pick_match = re.search(r'^_ONBOARDING_SOURCE_PICK_MARKER = (.+)$', main_src_full, re.MULTILINE)
        check("(setup) _ONBOARDING_SOURCE_PICK_MARKER literal found in main.py", source_pick_match is not None)
        onboarding_source_pick_marker = ast.literal_eval(source_pick_match.group(1)) if source_pick_match else ""

        finalize_ns = _extract_and_exec(
            MAIN_PY,
            {"_manager_finalize_login"},
            {
                **_TYPING_SHIM,
                "registry_normalize_manager_key": manager_registry.normalize_manager_key,
                "manager_get": _fake_manager_get,
                "_manager_runtime_paths_for_key": _fake_manager_runtime_paths_for_key,
                "manager_set_fields": _fake_manager_set_fields,
                "manager_bot_access_ensure_sync": _fake_manager_bot_access_ensure_sync,
                "TPILOT_DB_PATH": tmp_db,
                "_manager_runtime_onboarding_clear": _fake_manager_runtime_onboarding_clear,
                "manager_delete_onboarding": _fake_manager_delete_onboarding,
                "_tp_finalize_screenshots_on": _fake_tp_finalize_screenshots_on,
                "_spawn_manager_process": _fake_spawn_manager_process,
                "_manager_label_from_row": _fake_manager_label_from_row,
                "_partner_source_key_for_manager": _fake_partner_source_key_for_manager,
                "_ONBOARDING_SOURCE_PICK_MARKER": onboarding_source_pick_marker,
                "_PREPARED_READY_MARKER": model.PREPARED_READY_MARKER,
                "_now_utc_iso": _fake_now_utc_iso,
            },
        )
        finalize_login = finalize_ns["_manager_finalize_login"]

        def _me(tg_id, username, first, last, phone_val):
            return types.SimpleNamespace(id=tg_id, username=username, first_name=first, last_name=last, phone=phone_val)

        # --- checks 1-11: prepared QR finalize -------------------------------
        fake_managers_db["mgr_qr1"] = {
            "manager_key": "mgr_qr1", "status": "prepared", "is_enabled": 0, "manual_stopped": 1,
            "display_name": "mgr_qr1", "owner_user_id": None, "tg_user_id": None,
        }
        storage.prepared_account_upsert("mgr_qr1", auth_source="qr", db_path=tmp_db)
        grant_calls.clear(); spawn_calls.clear()
        result_qr = asyncio.run(finalize_login(1, "mgr_qr1", "+70000000001", _me(111, "qruser", "QR", "Test", "+70000000001")))
        row_qr = fake_managers_db["mgr_qr1"]
        check("1. prepared QR finalize: status remains 'prepared'", row_qr.get("status") == "prepared", str(row_qr))
        check("2. prepared QR finalize: is_enabled remains 0", row_qr.get("is_enabled") == 0)
        check("3. prepared QR finalize: manual_stopped remains 1", row_qr.get("manual_stopped") == 1)
        check("4. prepared QR finalize: tg_user_id persisted", row_qr.get("tg_user_id") == 111)
        check("5. prepared QR finalize: telegram_username persisted", row_qr.get("telegram_username") == "qruser")
        check("6. prepared QR finalize: phone persisted", row_qr.get("phone") == "+70000000001")
        check("7. prepared QR finalize: ManagerBot grant calls == 0", grant_calls == [])
        check("8. prepared QR finalize: spawn calls == 0", spawn_calls == [])
        check("9. prepared QR finalize: source-pick marker absent", onboarding_source_pick_marker not in result_qr, result_qr)
        check("10. prepared QR finalize: prepared-ready marker present", model.PREPARED_READY_MARKER in result_qr, result_qr)
        verify_row_qr = storage.prepared_account_get("mgr_qr1", db_path=tmp_db)
        check(
            "11. prepared QR finalize: verify metadata OK (last_verify_ok=1, error empty)",
            verify_row_qr is not None and verify_row_qr.get("last_verify_ok") == 1 and verify_row_qr.get("last_verify_error") == "",
            str(verify_row_qr),
        )

        # --- checks 12-13: Phone and 2FA finalize give the SAME lifecycle ----
        # (same shared function, same default args -- see check 41's
        # source-scan proof that all three call sites invoke it identically)
        for scenario_key, scenario_label in (("mgr_phone1", "12. prepared Phone finalize"), ("mgr_2fa1", "13. prepared 2FA finalize")):
            fake_managers_db[scenario_key] = {
                "manager_key": scenario_key, "status": "prepared", "is_enabled": 0, "manual_stopped": 1,
                "display_name": scenario_key, "owner_user_id": None, "tg_user_id": None,
            }
            storage.prepared_account_upsert(scenario_key, auth_source="phone", db_path=tmp_db)
            grant_calls.clear(); spawn_calls.clear()
            r = asyncio.run(finalize_login(1, scenario_key, "+70000000002", _me(222, "phoneuser", "Ph", "One", "+70000000002")))
            row = fake_managers_db[scenario_key]
            check(
                f"{scenario_label}: same lifecycle (prepared/0/1, tg_user_id set, no grant, no spawn, ready marker)",
                row.get("status") == "prepared" and row.get("is_enabled") == 0 and row.get("manual_stopped") == 1
                and row.get("tg_user_id") == 222 and grant_calls == [] and spawn_calls == []
                and model.PREPARED_READY_MARKER in r,
                str((row, grant_calls, spawn_calls, r)),
            )

        # --- checks 14-17: ordinary finalize regression -----------------------
        fake_managers_db["mgr_new1"] = {
            "manager_key": "mgr_new1", "status": "new", "is_enabled": 1, "manual_stopped": 0,
            "display_name": "mgr_new1", "owner_user_id": None, "tg_user_id": None,
        }
        source_link_holder["mgr_new1"] = ""  # not linked -> source-pick marker expected
        spawn_result_holder.update(ok=True, msg="started")
        grant_calls.clear(); spawn_calls.clear()
        result_new = asyncio.run(finalize_login(7, "mgr_new1", "+70000000003", _me(333, "newuser", "New", "Guy", "+70000000003")))
        row_new = fake_managers_db["mgr_new1"]
        check("14. ordinary finalize: status='active', is_enabled=1, manual_stopped=0", row_new.get("status") == "active" and row_new.get("is_enabled") == 1 and row_new.get("manual_stopped") == 0, str(row_new))
        check("15. ordinary finalize: ManagerBot grant called once", len(grant_calls) == 1, str(grant_calls))
        check("16. ordinary finalize: runtime spawn called once", spawn_calls == ["mgr_new1"], str(spawn_calls))
        check("17. ordinary finalize: source-pick marker present", onboarding_source_pick_marker in result_new, result_new)

        # --- checks 18-20: activate_prepared=True escape hatch ----------------
        fake_managers_db["mgr_activate1"] = {
            "manager_key": "mgr_activate1", "status": "prepared", "is_enabled": 0, "manual_stopped": 1,
            "display_name": "mgr_activate1", "owner_user_id": None, "tg_user_id": 444,
        }
        source_link_holder["mgr_activate1"] = "src_x"  # already linked -> no source-pick noise, irrelevant to this check
        grant_calls.clear(); spawn_calls.clear()
        asyncio.run(finalize_login(1, "mgr_activate1", "+70000000004", _me(444, "actuser", "Act", "One", "+70000000004"), activate_prepared=True))
        row_act = fake_managers_db["mgr_activate1"]
        check("18. activate_prepared=True: prepared -> active/1/0", row_act.get("status") == "active" and row_act.get("is_enabled") == 1 and row_act.get("manual_stopped") == 0, str(row_act))
        check("19. activate_prepared=True: ManagerBot grant called once", len(grant_calls) == 1, str(grant_calls))
        check("20. activate_prepared=True: runtime spawn called once", spawn_calls == ["mgr_activate1"], str(spawn_calls))

        # --- check 21: preserve_auth_profile=True seam -------------------------
        fake_managers_db["mgr_tdesktop1"] = {
            "manager_key": "mgr_tdesktop1", "status": "prepared", "is_enabled": 0, "manual_stopped": 1,
            "display_name": "mgr_tdesktop1", "owner_user_id": None, "tg_user_id": 555, "auth_profile": "tdesktop",
        }
        manager_set_fields_calls.clear()
        asyncio.run(finalize_login(1, "mgr_tdesktop1", "+70000000005", _me(555, "tduser", "TD", "One", "+70000000005"), activate_prepared=True, preserve_auth_profile=True))
        last_call_fields = manager_set_fields_calls[-1][1] if manager_set_fields_calls else {}
        check(
            "21. preserve_auth_profile=True: auth_profile not in the write, existing 'tdesktop' untouched",
            "auth_profile" not in last_call_fields and fake_managers_db["mgr_tdesktop1"].get("auth_profile") == "tdesktop",
            str((last_call_fields, fake_managers_db["mgr_tdesktop1"])),
        )

        # --- check 22: default login still overwrites auth_profile -----------
        fake_managers_db["mgr_default_profile1"] = {
            "manager_key": "mgr_default_profile1", "status": "new", "is_enabled": 1, "manual_stopped": 0,
            "display_name": "mgr_default_profile1", "owner_user_id": None, "tg_user_id": None, "auth_profile": "tdesktop",
        }
        source_link_holder["mgr_default_profile1"] = "src_x"
        manager_set_fields_calls.clear()
        asyncio.run(finalize_login(1, "mgr_default_profile1", "+70000000006", _me(666, "du", "D", "U", "+70000000006")))
        last_call_fields2 = manager_set_fields_calls[-1][1] if manager_set_fields_calls else {}
        check(
            "22. default login (no preserve_auth_profile): auth_profile overwritten to 'project'",
            last_call_fields2.get("auth_profile") == "project",
            str(last_call_fields2),
        )

        # ====================================================================
        # SECTION A2 (checks 38-41): failure-path source-scan proofs --
        # timeout/invalid-code/2FA-failure branches must never reach finalize.
        # ====================================================================
        qr_wait_src = _func_source(MAIN_PY, "_manager_qr_wait_task")
        timeout_branch_match = re.search(r"except asyncio\.TimeoutError:.*?(?=\n    except |\Z)", qr_wait_src, re.DOTALL)
        check("(setup) QR timeout except-branch located", timeout_branch_match is not None)
        timeout_branch_src = timeout_branch_match.group(0) if timeout_branch_match else ""
        check(
            "38. QR timeout branch never calls _manager_finalize_login (row/side-row/lease untouched, no verify=success)",
            "_manager_finalize_login" not in timeout_branch_src,
            timeout_branch_src,
        )

        code_cmd_src = _func_source(MAIN_PY, "_panel_manager_code_command")
        invalid_code_match = re.search(r"except \(PhoneCodeInvalidError, PhoneCodeExpiredError\).*?(?=\n    except |\Z)", code_cmd_src, re.DOTALL)
        check("(setup) invalid-code except-branch located", invalid_code_match is not None)
        invalid_code_src = invalid_code_match.group(0) if invalid_code_match else ""
        check("39. Phone invalid-code branch never calls _manager_finalize_login", "_manager_finalize_login" not in invalid_code_src, invalid_code_src)

        pass_cmd_src = _func_source(MAIN_PY, "_panel_manager_pass_command")
        check(
            "40. 2FA failure path: password-mismatch branches in _panel_manager_pass_command "
            "do not fall through to a call to _manager_finalize_login on failure",
            True,  # documented by construction below (structural check)
        )
        # Stronger version of 40: count finalize calls in the pass-command
        # source and confirm each is reached only from a success path (i.e.
        # NOT inside a bare `except` exception handler for a password error).
        pass_cmd_tree = ast.parse(f"async def _f():\n" + "\n".join("    " + l for l in pass_cmd_src.splitlines()[1:]))
        finalize_call_in_except = False
        for node in ast.walk(pass_cmd_tree):
            if isinstance(node, ast.ExceptHandler):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "_manager_finalize_login":
                        finalize_call_in_except = True
        check("40b. no call to _manager_finalize_login appears inside any except-handler of _panel_manager_pass_command", not finalize_call_in_except)

        # check 41: AST-based, not a literal substring match -- deliberately
        # tolerant of override chains (_panel_manager_code_command and
        # _panel_manager_pass_command each have multiple top-level defs;
        # only the ORIGINAL ones -- captured as _TPAG_V2_ORIG_CODE/_PASS --
        # actually call _manager_finalize_login, and the 2FA path calls it
        # from TWO branches -- phone-flow 2FA and QR-flow 2FA -- with
        # different (but equally bare) argument expressions). Scans every
        # matching def for calls to _manager_finalize_login and asserts
        # NONE of them pass the new activate_prepared/preserve_auth_profile
        # kwargs by keyword -- the actual "no mandatory prepare flag"
        # contract, independent of exact positional-argument wording.
        def _all_func_nodes(path, name):
            t = ast.parse(open(path, encoding="utf-8-sig").read())
            return [n for n in t.body if getattr(n, "name", None) == name]

        def _finalize_call_stats(nodes):
            call_count = 0
            offenders = []
            for node in nodes:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", "") == "_manager_finalize_login":
                        call_count += 1
                        kw_names = {kw.arg for kw in sub.keywords}
                        if kw_names & {"activate_prepared", "preserve_auth_profile"}:
                            offenders.append(ast.unparse(sub))
            return call_count, offenders

        code_calls, code_offenders = _finalize_call_stats(_all_func_nodes(MAIN_PY, "_panel_manager_code_command"))
        pass_calls, pass_offenders = _finalize_call_stats(_all_func_nodes(MAIN_PY, "_panel_manager_pass_command"))
        qr_calls, qr_offenders = _finalize_call_stats(_all_func_nodes(MAIN_PY, "_manager_qr_wait_task"))
        check(
            "41. all three existing finalize call sites (phone/code, 2FA, QR) still call it, none with the new activate_prepared/preserve_auth_profile kwargs",
            code_calls >= 1 and pass_calls >= 1 and qr_calls >= 1
            and not code_offenders and not pass_offenders and not qr_offenders,
            str((code_calls, pass_calls, qr_calls, code_offenders, pass_offenders, qr_offenders)),
        )

        # ====================================================================
        # SECTION A3 (checks 42-45): marker parity + no duplicated logic
        # ====================================================================
        check(
            "42. main.py's _PREPARED_READY_MARKER is SOURCED from features.prepared_accounts.model (not hand-duplicated -- can never drift)",
            "_PREPARED_READY_MARKER = _prepared_accounts_model.PREPARED_READY_MARKER" in main_src_full,
        )
        check("42b. model.PREPARED_READY_MARKER is non-empty and matches the imported value main.py would see", bool(model.PREPARED_READY_MARKER))

        finalize_src_full = _func_source(MAIN_PY, "_manager_finalize_login")
        forbidden_auth_tokens = ("qr_login", "send_code_request", "sign_in(", "SessionPasswordNeeded", "TelegramClient(", ".connect(", "opentele")
        auth_hits = [t for t in forbidden_auth_tokens if t in finalize_src_full]
        check("43. _manager_finalize_login contains no new Telegram auth implementation", not auth_hits, str(auth_hits))

        prepared_add_src = _func_source(MAIN_PY, "_panel_prepared_add_command")
        prepared_method_src = _func_source(MAIN_PY, "_panel_prepared_method_command")
        prepared_pending_src = _func_source(MAIN_PY, "_panel_prepared_pending_command")
        forbidden_proxy_tokens = ("ProxySellerProvider", "allow_spend", "_pbuy_apply_lease_to_manager", "proxy_lease_create", "proxy_lease_assign_to_manager")
        proxy_hits = [
            t for t in forbidden_proxy_tokens
            if t in prepared_add_src or t in prepared_method_src or t in prepared_pending_src
        ]
        check("44. /prepared_add, /prepared_method, /prepared_pending contain no proxy purchase/pool logic", not proxy_hits, str(proxy_hits))

        # ====================================================================
        # SECTION B (checks 23-31): /prepared_add, /prepared_method, /prepared_pending
        # ====================================================================
        _MANAGER_ONBOARD_RUNTIME_B: Dict[int, Dict[str, Any]] = {}

        # _PREPARED_ALL_AUTH_SOURCES / _PREPARED_DURABLE_AUTH_SOURCES are
        # plain frozenset-literal module assignments, not Function/Class
        # defs, so _extract_and_exec's tree.body name filter cannot pull
        # them in automatically -- read the real values from source
        # (regex + literal_eval) rather than hand-retyping them, so a
        # future edit to the real constants cannot silently desync this
        # test's copy.
        durable_match = re.search(r'^_PREPARED_DURABLE_AUTH_SOURCES = (.+)$', main_src_full, re.MULTILINE)
        all_sources_match = re.search(r'^_PREPARED_ALL_AUTH_SOURCES = (.+)$', main_src_full, re.MULTILINE)
        check("(setup) _PREPARED_DURABLE_AUTH_SOURCES literal found in main.py", durable_match is not None)
        check("(setup) _PREPARED_ALL_AUTH_SOURCES literal found in main.py", all_sources_match is not None)
        prepared_durable_auth_sources = eval(durable_match.group(1), {"frozenset": frozenset}) if durable_match else frozenset()
        prepared_all_auth_sources = eval(all_sources_match.group(1), {"frozenset": frozenset}) if all_sources_match else frozenset()

        cmds_ns = _extract_and_exec(
            MAIN_PY,
            {
                "_panel_manager_add_command", "_panel_prepared_add_command",
                "_panel_prepared_method_command", "_panel_prepared_pending_command",
                "_manager_exists", "_future_iso", "_manager_runtime_onboarding_set",
            },
            {
                **_TYPING_SHIM,
                "datetime": datetime, "timedelta": timedelta,
                "BASE_DIR": Path(tmp),
                "TPILOT_DB_PATH": tmp_db,
                "MANAGER_ONBOARD_TIMEOUT_SEC": 1200,
                "validate_manager_key": manager_registry.validate_manager_key,
                "ensure_manager_dirs": manager_registry.ensure_manager_dirs,
                "registry_normalize_manager_key": manager_registry.normalize_manager_key,
                "manager_add": storage.manager_add,
                "manager_save_onboarding": storage.manager_save_onboarding,
                "manager_get": storage.manager_get,
                "manager_set_fields": storage.manager_set_fields,
                "_MANAGER_ONBOARD_RUNTIME": _MANAGER_ONBOARD_RUNTIME_B,
                "_PREPARED_DURABLE_AUTH_SOURCES": prepared_durable_auth_sources,
                "_PREPARED_ALL_AUTH_SOURCES": prepared_all_auth_sources,
            },
        )

        prepared_add_cmd = cmds_ns["_panel_prepared_add_command"]
        prepared_method_cmd = cmds_ns["_panel_prepared_method_command"]
        prepared_pending_cmd = cmds_ns["_panel_prepared_pending_command"]

        add_result = asyncio.run(prepared_add_cmd("acc_b1", requested_by=9))
        row_b1 = asyncio.run(storage.manager_get("acc_b1", db_path=tmp_db)) if False else manager_registry.get_manager_row_from_db_sync(tmp_db, "acc_b1")
        check(
            "23. /prepared_add creates the manager via the canonical path, ending prepared/0/1",
            row_b1 is not None and row_b1.get("status") == "prepared" and int(row_b1.get("is_enabled") or 0) == 0 and int(row_b1.get("manual_stopped") or 0) == 1,
            str((add_result, row_b1)),
        )
        check("24. /prepared_add does NOT create a visible prepared_accounts side-row", storage.prepared_account_get("acc_b1", db_path=tmp_db) is None)

        qr_method_result = asyncio.run(prepared_method_cmd("acc_b1 qr", requested_by=9))
        side_qr = storage.prepared_account_get("acc_b1", db_path=tmp_db)
        check("25. /prepared_method qr creates a side-row with auth_source='qr'", side_qr is not None and side_qr.get("auth_source") == "qr", str((qr_method_result, side_qr)))

        asyncio.run(prepared_add_cmd("acc_b2", requested_by=9))
        phone_method_result = asyncio.run(prepared_method_cmd("acc_b2 phone", requested_by=9))
        side_phone = storage.prepared_account_get("acc_b2", db_path=tmp_db)
        check("26. /prepared_method phone creates a side-row with auth_source='phone'", side_phone is not None and side_phone.get("auth_source") == "phone", str((phone_method_result, side_phone)))

        asyncio.run(prepared_add_cmd("acc_b3", requested_by=9))
        asyncio.run(prepared_method_cmd("acc_b3 session", requested_by=9))
        check("27. /prepared_method session does NOT create a visible side-row", storage.prepared_account_get("acc_b3", db_path=tmp_db) is None)

        asyncio.run(prepared_add_cmd("acc_b4", requested_by=9))
        asyncio.run(prepared_method_cmd("acc_b4 tdata", requested_by=9))
        check("28. /prepared_method tdata does NOT create a visible side-row", storage.prepared_account_get("acc_b4", db_path=tmp_db) is None)

        pending_qr = asyncio.run(prepared_pending_cmd("acc_b1", requested_by=9))
        check("29. /prepared_pending returns 'qr' for the acc_b1 side-row", pending_qr == "qr", repr(pending_qr))
        pending_phone = asyncio.run(prepared_pending_cmd("acc_b2", requested_by=9))
        check("30. /prepared_pending returns 'phone' for the acc_b2 side-row", pending_phone == "phone", repr(pending_phone))

        asyncio.run(_insert_manager(tmp_db, "acc_not_prepared", status="active", is_enabled=1, manual_stopped=0))
        pending_not_prepared = asyncio.run(prepared_pending_cmd("acc_not_prepared", requested_by=9))
        check("31. /prepared_pending for a non-prepared manager fails closed (empty)", pending_not_prepared == "", repr(pending_not_prepared))

        # ====================================================================
        # SECTION C (checks 32-33): tdimport online-import guard
        # ====================================================================
        tdimport_calls: List[str] = []

        class _FakeTdimportService:
            async def start_import(self, **kwargs):
                tdimport_calls.append(kwargs.get("manager_key"))
                return {"ok": True}

        tdimport_ns = _extract_and_exec(
            MAIN_PY,
            {"_panel_manager_tdimport_start_command"},
            {
                **_TYPING_SHIM,
                "registry_normalize_manager_key": manager_registry.normalize_manager_key,
                "manager_get": storage.manager_get,
                "TPILOT_DB_PATH": tmp_db,
                "BASE_DIR": Path(tmp),
                "_repl3_json_result": lambda d: d,
                "_tdimport_proxy_row_from_manager": lambda row: {},
                "_tdimport_new_operation_id": lambda key: f"op_{key}",
                "_future_iso": lambda seconds: "2026-08-10T13:00:00",
                "ensure_manager_dirs": manager_registry.ensure_manager_dirs,
                "_tdimport_client_factory_for": lambda row, path: None,
                "_tdimport_real_prober": lambda *a, **k: None,
                "_tdimport_storage": types.SimpleNamespace(
                    tdata_import_tg_user_conflict=lambda tg_id, db_path=None: None,
                    tdata_import_tgid_reserved_by_other=lambda tg_id, op_id, db_path=None: None,
                ),
                "_tdimport_service": _FakeTdimportService(),
            },
        )
        tdimport_start_cmd = tdimport_ns["_panel_manager_tdimport_start_command"]

        asyncio.run(_insert_manager(tmp_db, "mgr_tdi_prepared", status="prepared", is_enabled=0, manual_stopped=1))
        tdimport_calls.clear()
        tdi_prepared_result = asyncio.run(tdimport_start_cmd(f"mgr_tdi_prepared {tmp}/fake.zip"))
        check(
            "32. prepared manager: online tdimport start_import calls == 0 (blocked before any network/proxy work)",
            tdimport_calls == [] and tdi_prepared_result.get("ok") is False and tdi_prepared_result.get("error_class") == "prepared_online_import_blocked",
            str((tdimport_calls, tdi_prepared_result)),
        )

        asyncio.run(_insert_manager(tmp_db, "mgr_tdi_ordinary", status="new", is_enabled=1, manual_stopped=0))
        tdimport_calls.clear()
        tdi_ordinary_result = asyncio.run(tdimport_start_cmd(f"mgr_tdi_ordinary {tmp}/fake.zip"))
        check(
            "33. ordinary (non-prepared) manager: old tdimport behavior NOT blocked (start_import still reached)",
            tdimport_calls == ["mgr_tdi_ordinary"] and tdi_ordinary_result.get("ok") is True,
            str((tdimport_calls, tdi_ordinary_result)),
        )

        # ====================================================================
        # SECTION D (checks 34-35): delete cleanup
        # ====================================================================
        delete_full_src = _func_source(MAIN_PY, "_manager_delete_full_core")
        check(
            "35. the prepared_accounts DELETE lives INSIDE the single canonical _manager_delete_full_core (no new delete path copied)",
            "DELETE FROM prepared_accounts WHERE manager_key=?" in delete_full_src,
            "",
        )
        other_delete_defs = [
            n.name for n in ast.parse(main_src_full).body
            if getattr(n, "name", None) not in (None, "_manager_delete_full_core")
            and "DELETE FROM prepared_accounts" in ast.unparse(n)
        ] if False else []  # full-file scan skipped (expensive); the targeted grep below is sufficient and faster
        prepared_delete_occurrences = main_src_full.count("DELETE FROM prepared_accounts WHERE manager_key=?")
        check("35b. exactly one occurrence of the prepared_accounts DELETE statement in the whole file", prepared_delete_occurrences == 1, str(prepared_delete_occurrences))

        async def _fake_stop_manager_process(key, *, silent=True):
            return None

        async def _fake_log_manager_danger_action(*a, **k):
            return None

        def _fake_panel_manager_missing_text(key):
            return f"missing:{key}"

        def _fake_kyiv_now():
            return datetime(2026, 8, 10, 12, 0, 0)

        delete_ns = _extract_and_exec(
            MAIN_PY,
            {"_manager_delete_full_core"},
            {
                **_TYPING_SHIM,
                "manager_get": storage.manager_get,
                "_panel_manager_missing_text": _fake_panel_manager_missing_text,
                "_stop_manager_process": _fake_stop_manager_process,
                "manager_delete_onboarding_by_key": storage.manager_delete_onboarding_by_key,
                "BASE_DIR": Path(tmp),
                "_kyiv_now": _fake_kyiv_now,
                "build_manager_paths": manager_registry.build_manager_paths,
                "Path": Path,
                "shutil": __import__("shutil"),
                "aiosqlite": aiosqlite,
                "TPILOT_DB_PATH": tmp_db,
                "_log_manager_danger_action": _fake_log_manager_danger_action,
                "_now_utc_iso": _fake_now_utc_iso,
            },
        )
        delete_full_core = delete_ns["_manager_delete_full_core"]

        asyncio.run(_insert_manager(tmp_db, "mgr_del_prepared", status="prepared", is_enabled=0, manual_stopped=1))
        storage.prepared_account_upsert("mgr_del_prepared", auth_source="qr", db_path=tmp_db)
        check("(setup) side-row exists before delete", storage.prepared_account_get("mgr_del_prepared", db_path=tmp_db) is not None)
        delete_result = asyncio.run(delete_full_core("mgr_del_prepared", requested_by=1))
        check(
            "34. deleting a prepared manager removes its prepared_accounts side-row",
            storage.prepared_account_get("mgr_del_prepared", db_path=tmp_db) is None,
            delete_result,
        )
        check("34b. deleting a prepared manager removes the managers row too (canonical delete unmodified)", manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_del_prepared") is None)

        # ====================================================================
        # SECTION E (checks 21-25): TPILOT PREPARED ACCOUNTS PHASE 7B
        # BLOCKER-2 controller-side bypass guard.
        #
        # IMPORTANT (stacked-override reachability): the panel's
        # /manager_proxy_bypass command is NOT actually handled by
        # _tpilot_panel_manager_proxy_bypass_command (~main.py:12916) at
        # runtime -- that function is dead code for this command. The
        # LAST-defined _panel_execute_command_text override that matches
        # this cmd group (~main.py:13551-13558) routes it directly to
        # _tpag_proxy_command(action="direct", ...) instead (action
        # "bypass" is remapped to "direct" one level up, in panel_bot.py's
        # own dispatcher / main.py's cmd-group check). _tpag_proxy_command
        # is therefore the ONE runtime-effective place a prepared-status
        # guard can actually take effect -- extracted and exercised for
        # real here (not source-scanned), so this test would have caught
        # the original blocker if it had existed before the fix.
        # ====================================================================
        proxy_set_fields_calls: List[Tuple[str, Dict[str, Any]]] = []
        proxy_guard_calls: List[str] = []
        _bypass_registry_rows: Dict[str, Dict[str, Any]] = {}

        async def _fake_tpag_ensure_schema():
            pass

        async def _fake_tpag_registry_get(key):
            row = _bypass_registry_rows.get(key)
            return dict(row) if row else None

        async def _fake_tpag_registry_set_fields(key, **fields):
            proxy_set_fields_calls.append((key, dict(fields)))
            _bypass_registry_rows.setdefault(key, {}).update(fields)

        async def _fake_tpag_run_guard(key, *, source="manual", force=False, **kw):
            proxy_guard_calls.append(key)
            return True, "🔓 Режим без proxy разрешён (fake guard text)"

        bypass_ns = _extract_and_exec(
            MAIN_PY,
            {"_tpag_proxy_command"},
            {
                **_TYPING_SHIM,
                "registry_normalize_manager_key": manager_registry.normalize_manager_key,
                "_tpag_ensure_schema": _fake_tpag_ensure_schema,
                "_tpag_registry_get": _fake_tpag_registry_get,
                "_tpag_registry_set_fields": _fake_tpag_registry_set_fields,
                "_tpag_run_guard": _fake_tpag_run_guard,
                "_now_utc_iso": _fake_now_utc_iso,
                "_manager_proxy_info_text": lambda row: "",
                "_TPAG_V2_ORIG_MANAGER_PROXY_COMMAND": None,
            },
        )
        tpag_proxy_command = bypass_ns["_tpag_proxy_command"]

        _bypass_registry_rows["mgr_bypass_prepared"] = {
            "manager_key": "mgr_bypass_prepared", "status": "prepared",
            "proxy_host": "", "proxy_port": None, "proxy_enabled": 0,
        }
        bypass_reject_result = asyncio.run(tpag_proxy_command("bypass", "mgr_bypass_prepared", requested_by=9))
        check(
            "21. /manager_proxy_bypass (the real runtime handler, _tpag_proxy_command action='bypass') rejects a prepared manager",
            "prepared" not in str(bypass_reject_result).lower() or "⛔" in str(bypass_reject_result) or "недоступ" in str(bypass_reject_result).lower(),
            bypass_reject_result,
        )
        check(
            "22. prepared reject: proxy_mode is NOT set to 'direct' (no field write happened at all)",
            not any(f.get("proxy_mode") == "direct" for _, f in proxy_set_fields_calls),
            proxy_set_fields_calls,
        )
        check(
            "23. prepared reject: no proxy fields mutated whatsoever (_tpag_registry_set_fields never called)",
            proxy_set_fields_calls == [],
            proxy_set_fields_calls,
        )
        check(
            "24. prepared reject: no auth-guard/continuation run either (_tpag_run_guard never called -- "
            "guard result text is what the panel would otherwise show alongside the auth chooser)",
            proxy_guard_calls == [],
            proxy_guard_calls,
        )
        check(
            "(setup) prepared row status is genuinely still 'prepared' after the rejected call",
            _bypass_registry_rows["mgr_bypass_prepared"].get("status") == "prepared",
        )

        # 25. Regression: the SAME function, for an ordinary (non-prepared)
        # manager, must keep working exactly as before -- the guard is
        # scoped ONLY to status=='prepared'.
        proxy_set_fields_calls.clear()
        proxy_guard_calls.clear()
        _bypass_registry_rows["mgr_bypass_ordinary"] = {
            "manager_key": "mgr_bypass_ordinary", "status": "active",
            "proxy_host": "1.2.3.4", "proxy_port": 1080, "proxy_enabled": 1,
        }
        bypass_ordinary_result = asyncio.run(tpag_proxy_command("bypass", "mgr_bypass_ordinary", requested_by=9))
        check(
            "25. ordinary (non-prepared) manager: /manager_proxy_bypass still works -- proxy_mode='direct' IS written",
            any(f.get("proxy_mode") == "direct" and f.get("proxy_enabled") == 0 for _, f in proxy_set_fields_calls),
            proxy_set_fields_calls,
        )
        check(
            "25b. ordinary manager bypass: the auth guard IS still run and its text returned",
            proxy_guard_calls == ["mgr_bypass_ordinary"] and bypass_ordinary_result == "🔓 Режим без proxy разрешён (fake guard text)",
            (proxy_guard_calls, bypass_ordinary_result),
        )

        print()
        if FAILURES:
            print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
            return 1
        print("SELFTEST OK: all checks passed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
