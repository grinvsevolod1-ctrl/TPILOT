# -*- coding: utf-8 -*-
"""Phase 5 selftest: features.prepared_accounts.service.verify()/activate()
-- the live-verification and prepared->active activation lifecycle.

Uses a throwaway temporary SQLite file only -- never touches the real
project DB (see _guard_temp_db). Pure/offline: no network, no Telegram, no
proxy-provider calls, no purchases. service.py is a plain, directly-
importable module (no Telethon/env side effects at call time -- it never
constructs a TelegramClient itself), so this selftest calls it directly
with an injected FAKE Telegram client and REAL storage.py primitives
against a temp DB (claim/mark_verify/mark_activated), following the
project's established injection-based testing pattern.

finalize_login is injected as a SPY (not the real _manager_finalize_login):
that function's OWN internal behavior (identity write, ManagerBot grant,
screenshots, runtime spawn, source-pick marker) is already exhaustively
covered by tools/prepared_accounts_finalize_selftest.py (Phase 4, 52
checks) -- re-testing it here would duplicate that coverage. What THIS
file proves is service.py's OWN contract: it calls finalize_login exactly
once, with exactly the activation kwargs, and does not itself perform any
grant/spawn/SQL/Telegram-client work (proven by source-scan, since
service.py's own code has nowhere to call those from).

Run: python3.12 tools\\prepared_accounts_activate_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import inspect
import os
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite  # noqa: E402
import manager_registry  # noqa: E402
import storage  # noqa: E402
from features.prepared_accounts import model, repository, service  # noqa: E402

SERVICE_PY = BASE_DIR / "features" / "prepared_accounts" / "service.py"

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


# --- deterministic fake clock (no real sleep needed for TTL-claim tests) ---
_BASE_DT = _dt.datetime(2026, 1, 1, 0, 0, 0)


class _FrozenDateTime(_dt.datetime):
    _offset_seconds = 0

    @classmethod
    def utcnow(cls):
        return _BASE_DT + _dt.timedelta(seconds=cls._offset_seconds)


def _set_fake_offset(seconds: int) -> None:
    _FrozenDateTime._offset_seconds = seconds


async def _ensure_full_schema(db_path: str) -> None:
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    await storage.init_db()
    async with aiosqlite.connect(db_path) as db:
        await storage._proxy_leases_table_ready(db)


async def _insert_manager(db_path: str, manager_key: str, **overrides) -> int:
    fields = dict(
        status="prepared", is_enabled=0, manual_stopped=1, tg_user_id=None,
        phone="", telegram_username="", first_name="", last_name="",
        display_name=manager_key, session_path=f"{manager_key}.session",
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


class _FakeClient:
    def __init__(self, *, authorized=True, me=None, connect_raises=None, event_log=None):
        self._authorized = authorized
        self._me = me
        self._connect_raises = connect_raises
        self.event_log = event_log if event_log is not None else []

    async def connect(self):
        self.event_log.append("connect")
        if self._connect_raises is not None:
            raise self._connect_raises

    async def is_user_authorized(self):
        self.event_log.append("is_user_authorized")
        return self._authorized

    async def get_me(self):
        self.event_log.append("get_me")
        return self._me

    async def disconnect(self):
        self.event_log.append("disconnect")


SOURCE_PICK_SENTINEL = "SOURCE-PICK-SENTINEL"


def main() -> int:  # noqa: C901
    with tempfile.TemporaryDirectory(prefix="tpilot_prepacc_activate_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)
        asyncio.run(_ensure_full_schema(tmp_db))
        storage.datetime = _FrozenDateTime
        _set_fake_offset(0)

        async def _get_manager_row(k):
            return manager_registry.get_manager_row_from_db_sync(tmp_db, k)

        def _get_prepared_metadata(k):
            return repository.get_prepared(k, db_path=tmp_db)

        def _claim_activation(k, ttl_seconds=300):
            return storage.prepared_account_claim_activating(k, ttl_seconds=ttl_seconds, db_path=tmp_db)

        def _check_duplicate_tg_user(tg_id):
            return storage.tdata_import_tg_user_conflict(tg_id, db_path=tmp_db)

        def _mark_verify(k, ok, err=""):
            return storage.prepared_account_mark_verify(k, ok, err, db_path=tmp_db)

        def _mark_activated(k):
            return storage.prepared_account_mark_activated(k, db_path=tmp_db)

        build_client_calls: List[Any] = []
        client_holder: Dict[str, Any] = {}

        async def _build_client(key, session_path, source):
            build_client_calls.append((key, session_path, source))
            return client_holder["client"]

        finalize_calls: List[Dict[str, Any]] = []

        async def _fake_finalize_login(owner_user_id, manager_key, phone, me, **kwargs):
            finalize_calls.append({"owner_user_id": owner_user_id, "manager_key": manager_key, "phone": phone, "me": me, **kwargs})
            # Simulates ONLY the lifecycle-field write the real
            # _manager_finalize_login performs -- grant/spawn/screenshots
            # are that function's OWN behavior (Phase 4, already tested),
            # never re-implemented here.
            await storage.manager_set_fields(
                manager_key, status="active", is_enabled=1, manual_stopped=0,
                tg_user_id=int(getattr(me, "id", 0) or 0),
            )
            return f"✅ activated. {SOURCE_PICK_SENTINEL}"

        # ====================================================================
        # checks 1-2: PREPARED_STORED without proxy -> NEED_PROXY, connect=0
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_noproxy", proxy_enabled=0))
        storage.prepared_account_upsert("mgr_noproxy", auth_source="session", db_path=tmp_db)
        build_client_calls.clear()
        r1 = asyncio.run(service.activate(
            "mgr_noproxy", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        check("1. PREPARED_STORED without proxy -> NEED_PROXY", r1.get("status") == model.ActivationResult.NEED_PROXY, str(r1))
        check("2. without proxy: connect count == 0", build_client_calls == [], str(build_client_calls))

        # ====================================================================
        # checks 3-6: proxy exists -> connect occurs, correct ordering, disconnect always
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_ok1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=None))
        storage.prepared_account_upsert("mgr_ok1", auth_source="session", db_path=tmp_db)
        event_log: List[str] = []
        me_obj = types.SimpleNamespace(id=9001, username="okuser", first_name="Ok", last_name="One", phone="+70000000009")
        client_holder["client"] = _FakeClient(authorized=True, me=me_obj, event_log=event_log)
        build_client_calls.clear()
        r3 = asyncio.run(service.activate(
            "mgr_ok1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        check("3. proxy exists: connect occurs", build_client_calls != [], str(build_client_calls))
        check("4. connect before get_me", event_log.index("connect") < event_log.index("get_me"), str(event_log))
        check("5. is_user_authorized before get_me", event_log.index("is_user_authorized") < event_log.index("get_me"), str(event_log))
        check("6. disconnect always called", "disconnect" in event_log, str(event_log))
        check("(setup) mgr_ok1 activation succeeded", r3.get("status") == model.ActivationResult.OK, str(r3))

        # ====================================================================
        # checks 7-9: unauthorized / connect failure -> stays prepared, no finalize
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_unauth1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080))
        storage.prepared_account_upsert("mgr_unauth1", auth_source="session", db_path=tmp_db)
        client_holder["client"] = _FakeClient(authorized=False, event_log=[])
        finalize_calls.clear()
        r7 = asyncio.run(service.activate(
            "mgr_unauth1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        row_unauth = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_unauth1")
        check("7. unauthorized: status stays prepared", row_unauth.get("status") == "prepared", str(row_unauth))
        check("8. unauthorized: finalize calls == 0", finalize_calls == [], str(finalize_calls))
        check("(setup) unauthorized status class", r7.get("status") == model.ActivationResult.UNAUTHORIZED, str(r7))

        asyncio.run(_insert_manager(tmp_db, "mgr_connfail1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080))
        storage.prepared_account_upsert("mgr_connfail1", auth_source="session", db_path=tmp_db)
        client_holder["client"] = _FakeClient(connect_raises=ConnectionError("simulated"), event_log=[])
        r9 = asyncio.run(service.activate(
            "mgr_connfail1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        row_connfail = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_connfail1")
        check("9. connect failure: status stays prepared", row_connfail.get("status") == "prepared", str(row_connfail))
        check("(setup) connect failure status class", r9.get("status") == model.ActivationResult.CONNECT_FAILED, str(r9))

        # ====================================================================
        # checks 26-31: TPILOT PREPARED ACCOUNTS PHASE 7B HARDENING --
        # get_me() returns None (or an object with id<=0), even though
        # is_user_authorized() already returned True. Must fail closed
        # BEFORE either the VERIFIED identity-mismatch compare or the
        # STORED duplicate-tg_user_id lookup runs against a bogus id=0.
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_getme_none", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080))
        storage.prepared_account_upsert("mgr_getme_none", auth_source="session", db_path=tmp_db)
        event_log_none: List[str] = []
        client_holder["client"] = _FakeClient(authorized=True, me=None, event_log=event_log_none)
        finalize_calls.clear()
        r26 = asyncio.run(service.activate(
            "mgr_getme_none", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        row_getme_none = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_getme_none")
        check("26. get_me() returns None: activation fails closed (not OK)", r26.get("ok") is not True, str(r26))
        check("27. get_me() None: finalize calls == 0", finalize_calls == [], str(finalize_calls))
        check("28. get_me() None: status remains prepared", row_getme_none.get("status") == "prepared", str(row_getme_none))
        check("29. get_me() None: disconnect called", "disconnect" in event_log_none, str(event_log_none))
        check(
            "30. get_me() None: safe error class only (an existing model.ActivationResult value, no traceback/exception text)",
            r26.get("status") in model.ActivationResult.ALL and "error_text" not in r26,
            str(r26),
        )
        meta_getme_none = repository.get_prepared("mgr_getme_none", db_path=tmp_db)
        check(
            "30b. get_me() None: mark_verify recorded the specific 'identity_unavailable' reason (not the generic 'connect_failed' text)",
            meta_getme_none is not None and meta_getme_none.get("last_verify_ok") == 0 and meta_getme_none.get("last_verify_error") == "identity_unavailable",
            meta_getme_none,
        )

        # 31. Same fail-closed behavior for a technically-non-None `me`
        # object whose id is 0 (or otherwise <= 0) -- a different way the
        # same underlying anomaly (authorized session, unusable identity)
        # could manifest.
        asyncio.run(_insert_manager(tmp_db, "mgr_getme_zeroid", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080))
        storage.prepared_account_upsert("mgr_getme_zeroid", auth_source="session", db_path=tmp_db)
        event_log_zero: List[str] = []
        client_holder["client"] = _FakeClient(authorized=True, me=types.SimpleNamespace(id=0, username="", first_name="", last_name="", phone=""), event_log=event_log_zero)
        finalize_calls.clear()
        r31 = asyncio.run(service.activate(
            "mgr_getme_zeroid", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        row_getme_zero = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_getme_zeroid")
        check("31. get_me() returns id=0: same fail-closed behavior (not OK)", r31.get("ok") is not True, str(r31))
        check("31b. get_me() id=0: finalize calls == 0", finalize_calls == [], str(finalize_calls))
        check("31c. get_me() id=0: status remains prepared", row_getme_zero.get("status") == "prepared", str(row_getme_zero))
        check("31d. get_me() id=0: disconnect called", "disconnect" in event_log_zero, str(event_log_zero))

        # ====================================================================
        # checks 10-11: identity mismatch VERIFIED -> fail closed, row unchanged
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_verified1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=12345))
        storage.prepared_account_upsert("mgr_verified1", auth_source="qr", db_path=tmp_db)
        before_row = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_verified1")
        client_holder["client"] = _FakeClient(authorized=True, me=types.SimpleNamespace(id=99999, username="wrong", first_name="", last_name="", phone=""), event_log=[])
        finalize_calls.clear()
        r10 = asyncio.run(service.activate(
            "mgr_verified1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        after_row = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_verified1")
        check("10. identity mismatch (VERIFIED): status stays prepared", after_row.get("status") == "prepared", str(after_row))
        check("(setup) identity mismatch status class", r10.get("status") == model.ActivationResult.IDENTITY_MISMATCH, str(r10))
        fields_to_compare = ("status", "is_enabled", "manual_stopped", "tg_user_id", "phone", "telegram_username", "auth_profile")
        check(
            "11. identity mismatch: manager row's lifecycle fields are fieldwise unchanged",
            all(before_row.get(f) == after_row.get(f) for f in fields_to_compare),
            str({f: (before_row.get(f), after_row.get(f)) for f in fields_to_compare}),
        )

        # ====================================================================
        # checks 12-13: duplicate tg_user_id STORED -> fail closed
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_owner_of_id", status="active", is_enabled=1, manual_stopped=0, tg_user_id=55555))
        asyncio.run(_insert_manager(tmp_db, "mgr_stored_dup1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=None))
        storage.prepared_account_upsert("mgr_stored_dup1", auth_source="tdata", db_path=tmp_db)
        client_holder["client"] = _FakeClient(authorized=True, me=types.SimpleNamespace(id=55555, username="dup", first_name="", last_name="", phone=""), event_log=[])
        finalize_calls.clear()
        r12 = asyncio.run(service.activate(
            "mgr_stored_dup1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        check("12. duplicate tg_user_id (STORED): fail closed", r12.get("status") == model.ActivationResult.DUPLICATE, str(r12))
        check("13. duplicate: finalize calls == 0", finalize_calls == [], str(finalize_calls))

        # ====================================================================
        # checks 14-30: success paths (VERIFIED + STORED)
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_success_verified", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=7001, owner_user_id=42))
        storage.prepared_account_upsert("mgr_success_verified", auth_source="qr", db_path=tmp_db)
        me_v = types.SimpleNamespace(id=7001, username="verifieduser", first_name="Ver", last_name="One", phone="+70000000070")
        client_holder["client"] = _FakeClient(authorized=True, me=me_v, event_log=[])
        finalize_calls.clear()
        r14 = asyncio.run(service.activate(
            "mgr_success_verified", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        check("14. success (VERIFIED): saved tg_user_id == me.id (identity check passed)", r14.get("status") == model.ActivationResult.OK, str(r14))
        check("16a. success: finalize called exactly once (VERIFIED scenario)", len(finalize_calls) == 1, str(finalize_calls))

        dup_check_calls: List[int] = []

        def _check_duplicate_tg_user_spy(tg_id):
            dup_check_calls.append(tg_id)
            return storage.tdata_import_tg_user_conflict(tg_id, db_path=tmp_db)

        asyncio.run(_insert_manager(tmp_db, "mgr_success_stored", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=None, owner_user_id=42, auth_profile="tdesktop"))
        storage.prepared_account_upsert("mgr_success_stored", auth_source="tdata", db_path=tmp_db)
        me_s = types.SimpleNamespace(id=7002, username="storeduser", first_name="St", last_name="Two", phone="+70000000071")
        client_holder["client"] = _FakeClient(authorized=True, me=me_s, event_log=[])
        finalize_calls.clear()
        r15 = asyncio.run(service.activate(
            "mgr_success_stored", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user_spy, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        check("15. success (STORED): collision helper (check_duplicate_tg_user) called", dup_check_calls == [7002], str(dup_check_calls))
        check("16. success: finalize called exactly once (STORED scenario)", len(finalize_calls) == 1, str(finalize_calls))

        fc = finalize_calls[0]
        check("17. success: activate_prepared=True passed to finalize", fc.get("activate_prepared") is True, str(fc))
        check("18. success: preserve_auth_profile=True passed to finalize", fc.get("preserve_auth_profile") is True, str(fc))
        check("19. success: preserve_owner_user_id=True passed to finalize", fc.get("preserve_owner_user_id") is True, str(fc))
        check("20. success: spawn_after_login=True passed to finalize", fc.get("spawn_after_login") is True, str(fc))

        row_after_success = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_success_stored")
        check("21. success: manager becomes active", row_after_success.get("status") == "active", str(row_after_success))
        check("22. success: is_enabled=1", int(row_after_success.get("is_enabled") or 0) == 1, str(row_after_success))
        check("23. success: manual_stopped=0", int(row_after_success.get("manual_stopped") or 0) == 0, str(row_after_success))

        service_src_raw = SERVICE_PY.read_text(encoding="utf-8-sig")
        check("24. ManagerBot grant happens exactly once, THROUGH finalize (1 finalize call + service.py itself never calls manager_bot_access_ensure_sync)", len(finalize_calls) == 1 and "manager_bot_access_ensure_sync" not in service_src_raw, str(len(finalize_calls)))
        check("25. runtime spawn happens exactly once, THROUGH finalize (1 finalize call + service.py itself never calls _spawn_manager_process)", len(finalize_calls) == 1 and "_spawn_manager_process" not in service_src_raw, str(len(finalize_calls)))
        check("26. success: finalize's return text (incl. source-pick marker) is passed through unchanged", r15.get("result_text") == f"✅ activated. {SOURCE_PICK_SENTINEL}", repr(r15.get("result_text")))

        meta_after = storage.prepared_account_get("mgr_success_stored", db_path=tmp_db)
        check("27/28. success: mark_activated called -> activated_at set", meta_after is not None and bool(meta_after.get("activated_at")), str(meta_after))
        check("29. success: activating_at cleared", meta_after is not None and meta_after.get("activating_at") == "", str(meta_after))

        visible_after_activation = [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)]
        check("30. success: repository no longer lists the (now active) object", "mgr_success_stored" not in visible_after_activation, str(visible_after_activation))

        # ====================================================================
        # checks 31-36: verify-only (does not activate)
        # ====================================================================
        verify_sig = inspect.signature(service.verify)
        check("32/33. verify() signature has no finalize_login/mark_activated params -- structurally cannot spawn runtime or grant ManagerBot access", "finalize_login" not in verify_sig.parameters and "mark_activated" not in verify_sig.parameters, str(list(verify_sig.parameters)))

        asyncio.run(_insert_manager(tmp_db, "mgr_verify_ok1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=None))
        storage.prepared_account_upsert("mgr_verify_ok1", auth_source="session", db_path=tmp_db)
        client_holder["client"] = _FakeClient(authorized=True, me=types.SimpleNamespace(id=8001, username="vok", first_name="", last_name="", phone=""), event_log=[])
        rv_ok = asyncio.run(service.verify(
            "mgr_verify_ok1",
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
        ))
        row_verify_ok = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_verify_ok1")
        check("31. verify-only: status remains prepared", row_verify_ok.get("status") == "prepared", str(row_verify_ok))
        meta_verify_ok = storage.prepared_account_get("mgr_verify_ok1", db_path=tmp_db)
        check("34. verify-only success: mark_verify(ok=1)", meta_verify_ok is not None and meta_verify_ok.get("last_verify_ok") == 1, str(meta_verify_ok))
        check("(setup) verify-only success result", rv_ok.get("ok") is True, str(rv_ok))

        asyncio.run(_insert_manager(tmp_db, "mgr_verify_fail1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=9999))
        storage.prepared_account_upsert("mgr_verify_fail1", auth_source="qr", db_path=tmp_db)
        client_holder["client"] = _FakeClient(authorized=False, event_log=[])
        rv_fail = asyncio.run(service.verify(
            "mgr_verify_fail1",
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
        ))
        meta_verify_fail = storage.prepared_account_get("mgr_verify_fail1", db_path=tmp_db)
        check("35. verify failure: mark_verify(ok=0)", meta_verify_fail is not None and meta_verify_fail.get("last_verify_ok") == 0, str(meta_verify_fail))
        check(
            "36. verify failure error is a short safe class (no raw exception text/secrets)",
            meta_verify_fail is not None and meta_verify_fail.get("last_verify_error") in model.ActivationResult.ALL | {"unauthorized"} and len(meta_verify_fail.get("last_verify_error") or "") < 40,
            str(meta_verify_fail),
        )
        check("37. failure clears activating_at (verify's own failure path)", meta_verify_fail is not None and meta_verify_fail.get("activating_at") == "", str(meta_verify_fail))

        # ====================================================================
        # checks 38-40: claim atomicity + stale recovery
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_claim1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080))
        storage.prepared_account_upsert("mgr_claim1", auth_source="session", db_path=tmp_db)
        _set_fake_offset(5000)
        claim1 = _claim_activation("mgr_claim1", ttl_seconds=60)
        check("38. first activation claim PASS", claim1 is True)
        claim2 = _claim_activation("mgr_claim1", ttl_seconds=60)
        check("39. second simultaneous claim FAIL/BUSY", claim2 is False)
        _set_fake_offset(5000 + 61)
        claim3 = _claim_activation("mgr_claim1", ttl_seconds=60)
        check("40. stale claim can recover", claim3 is True)
        _set_fake_offset(0)

        # ====================================================================
        # checks 41-42: proxy is never auto-released on activation failure
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_proxy_keep1", proxy_enabled=1, proxy_host="9.9.9.9", proxy_port=1080, proxy_lease_id=777, tg_user_id=4321))
        storage.prepared_account_upsert("mgr_proxy_keep1", auth_source="qr", db_path=tmp_db)
        client_holder["client"] = _FakeClient(authorized=False, event_log=[])
        asyncio.run(service.activate(
            "mgr_proxy_keep1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        row_proxy_keep = manager_registry.get_manager_row_from_db_sync(tmp_db, "mgr_proxy_keep1")
        check(
            "41. assigned proxy remains assigned after activation failure",
            int(row_proxy_keep.get("proxy_enabled") or 0) == 1 and row_proxy_keep.get("proxy_host") == "9.9.9.9" and row_proxy_keep.get("proxy_lease_id") == 777,
            str(row_proxy_keep),
        )
        check("42. no proxy auto-unassign call anywhere in service.py", "proxy_lease_unassign" not in service_src_raw and "_ppool_disable_manager_proxy_fields" not in service_src_raw)

        # ====================================================================
        # checks 43-48: service.py source-scan (no SQL/Telegram/proxy/spawn/grant)
        # ====================================================================
        def _code_without_string_literals(source: str) -> str:
            t = ast.parse(source)
            for node in ast.walk(t):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    node.value = ""
            ast.fix_missing_locations(t)
            return ast.unparse(t)

        service_cleaned = _code_without_string_literals(service_src_raw)
        check("43. service.py contains no SQL", not any(t in service_cleaned for t in ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "CREATE TABLE", "sqlite3", "aiosqlite")))
        check("44. service.py constructs no TelegramClient", "TelegramClient(" not in service_cleaned)
        check("45. service.py contains no proxy provider implementation", not any(t in service_cleaned for t in ("ProxySellerProvider", "allow_spend", "proxy_lease_create", "proxy_lease_assign_to_manager")))
        check("46. service.py contains no _spawn_manager_process call", "_spawn_manager_process" not in service_cleaned)
        check("47. service.py contains no manager_bot_access_ensure_sync call", "manager_bot_access_ensure_sync" not in service_cleaned)
        check(
            "48. service.py never calls qr_login/send_code_request/sign_in/confirm_install/install_session",
            not any(t in service_cleaned for t in ("qr_login", "send_code_request", "sign_in(", "confirm_install", "install_session")),
            "",
        )

        # ====================================================================
        # checks 49-50: PREPARED_DRAFT cannot activate
        # ====================================================================
        asyncio.run(_insert_manager(tmp_db, "mgr_draft1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=None))
        storage.prepared_account_upsert("mgr_draft1", auth_source="qr", db_path=tmp_db)  # qr + no tg_user_id -> DRAFT
        build_client_calls.clear()
        r49 = asyncio.run(service.activate(
            "mgr_draft1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_claim_activation, build_client=_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_mark_verify,
            finalize_login=_fake_finalize_login, mark_activated=_mark_activated,
        ))
        check("49. PREPARED_DRAFT cannot activate", r49.get("status") == model.ActivationResult.DRAFT_NOT_READY, str(r49))
        check("50. PREPARED_DRAFT: live connect count == 0", build_client_calls == [], str(build_client_calls))

        # ====================================================================
        # SECTION 31: critical ordering assertion (STORED success scenario)
        # ====================================================================
        order_log: List[str] = []

        def _ordered_claim(k, ttl_seconds=300):
            ok = storage.prepared_account_claim_activating(k, ttl_seconds=ttl_seconds, db_path=tmp_db)
            order_log.append("claim")
            return ok

        def _ordered_mark_verify(k, ok, err=""):
            order_log.append("mark_verify")
            return storage.prepared_account_mark_verify(k, ok, err, db_path=tmp_db)

        def _ordered_mark_activated(k):
            order_log.append("mark_activated")
            return storage.prepared_account_mark_activated(k, db_path=tmp_db)

        async def _ordered_build_client(key, session_path, source):
            order_log.append("client_create")
            return client_holder["client"]

        async def _ordered_finalize(owner_user_id, manager_key, phone, me, **kwargs):
            order_log.append("finalize")
            await storage.manager_set_fields(manager_key, status="active", is_enabled=1, manual_stopped=0, tg_user_id=int(getattr(me, "id", 0) or 0))
            return "ok"

        asyncio.run(_insert_manager(tmp_db, "mgr_order1", proxy_enabled=1, proxy_host="1.2.3.4", proxy_port=1080, tg_user_id=None))
        storage.prepared_account_upsert("mgr_order1", auth_source="tdata", db_path=tmp_db)
        ordered_event_log: List[str] = []
        client_holder["client"] = _FakeClient(authorized=True, me=types.SimpleNamespace(id=6001, username="orderuser", first_name="", last_name="", phone=""), event_log=ordered_event_log)

        asyncio.run(service.activate(
            "mgr_order1", requested_by=1,
            get_manager_row=_get_manager_row, get_prepared_metadata=_get_prepared_metadata,
            claim_activation=_ordered_claim, build_client=_ordered_build_client,
            check_duplicate_tg_user=_check_duplicate_tg_user, mark_verify=_ordered_mark_verify,
            finalize_login=_ordered_finalize, mark_activated=_ordered_mark_activated,
        ))
        full_order = []
        for tag in order_log:
            if tag == "client_create":
                full_order.extend(["client_create"] + ordered_event_log)
                ordered_event_log = []
            else:
                full_order.append(tag)
        # Reconstruct precisely: claim -> client_create -> connect ->
        # is_user_authorized -> get_me -> disconnect -> finalize -> mark_activated
        # (mark_verify is only called on FAILURE paths inside _live_verify --
        # a clean success never calls it, matching service.py's own design:
        # mark_activated is the success-side terminal write instead).
        expected_order = ["claim", "client_create", "connect", "is_user_authorized", "get_me", "disconnect", "finalize", "mark_activated"]
        check(
            "31. FIRST_TELEGRAM_CONNECT happens AFTER proxy-guard+claim success, and the full event order matches claim->client_create->connect->is_user_authorized->get_me->disconnect->finalize->mark_activated",
            full_order == expected_order,
            str(full_order),
        )

        print()
        if FAILURES:
            print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
            return 1
        print("SELFTEST OK: all checks passed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
