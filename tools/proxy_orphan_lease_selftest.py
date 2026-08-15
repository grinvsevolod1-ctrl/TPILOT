# -*- coding: utf-8 -*-
"""tools/proxy_orphan_lease_selftest.py -- offline selftest for "CORRECTION B
20260810": ORPHAN PROXY / LEASE SUPPRESSION.

Owner's rule: a proxy_leases row with no real, currently-assigned manager is
NOT a working TPilot proxy. It must NEVER: auto-renew, initiate a renewal,
initiate/repeat a spend, hit allow_spend, retry-spend, or produce an AdminBot
alert ("⚠️ Продление прокси требует проверки" and friends). Real, exact
production bug reproduced here: lease_id=15, manager_key="_", a previous
renewal attempt unconfirmed -- must resolve to silence, not a review alert.

Design (all reused, nothing duplicated): the pure classifier
_prenew_lease_role (main.py, R1 20260808) already implements the FULL
canonical orphan/managed/free model -- this correction only (a) flips
_PRENEW_ROLE_ENFORCEMENT_ENABLED from observe-only to enforced (activating
two ALREADY-STAGED gates in the warn loop and engine-1 autorenew loop), and
(b) adds ONE new thin async wrapper, _prenew_lease_is_orphan, wired as a
PRIMARY gate inside _renewal_wrapped_execute (the single spend choke point
shared by BOTH renewal engines and both manual confirm commands) and as a
DEFENSIVE guard directly inside the three notification-producing functions
(_prenew_autorenew_one, _renewal_auto_tick_report_outcome,
_prenew_send_lease_warning).

Technique: main.py cannot be imported standalone -- AST-extraction against
REAL functions (same idiom as tools/proxy_renewal_retry_policy_selftest.py),
against a REAL temp SQLite DB (storage runs for real, managers + proxy_leases
tables). A real ProxySellerProvider is driven through a FakeSession so
"provider was never called" is a genuine HTTP-layer fact, not a mocked
return value. No network, no Telegram, no real spend.

Run:  python tools\\proxy_orphan_lease_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage
from proxy_provider import ProxyProviderError, ProxySellerProvider

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAIN_PY = BASE_DIR / "main.py"

FAILURES = []


def check(label: str, condition: bool, detail=""):
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


NAMES = {
    "_prenew_preflight_check", "_prenew_execute_renewal", "_renewal_wrapped_execute",
    "_renewal_reconcile_pending_on_start", "_renewal_guard_check", "_renewal_calc_preview",
    "_renewal_float", "_renewal_balance", "_renewal_kyiv_today_str", "_handle_proxy_renewal_confirm_command",
    "_handle_proxy_renew_confirm_command", "_prenew_display_name",
    "_prenew_verify_renewal_via_refresh", "_prenew_find_provider_entry", "_prenew_classify_provider_error",
    "_ppool_parse_expires_at", "_pbuy_safe_error", "_prenew_autorenew_one", "_renewal_auto_tick_report_outcome",
    "_prenew_manager_identity_line", "_prenew_format_expires_display",
    "_prenew_lease_role", "_prenew_lease_is_orphan", "_prenew_role_diagnostic",
    "_renewal_auto_tick", "_renewal_eligible_leases", "_prenew_collect_active_seller_leases",
    "_ppool_manager_usage_map", "_ppool_usage_key",
    "_RENEWAL_ELIGIBLE_MIN_DELTA_DAYS", "_PRENEW_ROLE_ENFORCEMENT_ENABLED",
    "_prenew_send_lease_warning", "_prenew_warn_action_needed",
    "_tpag_registry_get",
}


def extract_and_exec(tree, names, extra_ns):
    picked = {}
    for node in tree.body:
        nm = getattr(node, "name", None)
        if nm in names:
            picked[nm] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            picked[node.targets[0].id] = node
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"missing {missing}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in names if n in picked)
    ns = dict(extra_ns)
    exec(compile(module_src, "<main.py orphan-lease extract>", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Parser-grounded fakes (same pattern as proxy_renewal_retry_policy_selftest):
# a real ProxySellerProvider driven through a fake HTTP session, so "the
# provider was never called" is an HTTP-layer fact, not a mock assumption.
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


class FakeSession:
    def __init__(self):
        self._queue = []
        self.calls = []

    def queue_response(self, status_code, json_data):
        self._queue.append((status_code, json_data))

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append(url)
        if not self._queue:
            raise RuntimeError(f"FakeSession: no queued response for {method} {url} -- ORPHAN LEASE MUST NEVER REACH THE PROVIDER")
        status_code, body = self._queue.pop(0)
        return FakeResponse(status_code, body)

    def prolong_make_call_count(self) -> int:
        return sum(1 for u in self.calls if "prolong/make" in u)

    def reference_list_call_count(self) -> int:
        return sum(1 for u in self.calls if "reference/list" in u)


_REF_OK = {"data": [{"id": "1m", "name": "1 month"}]}
_CALC_OK = {"data": {"total": "1.8", "currency": "USD", "price": "1.8"}}
_MAKE_OK = {"data": {"total": "1.8", "currency": "USD", "date_end": "2026-09-08"}}


def _real_provider(session: FakeSession, api_key: str = "fake-nospend-orphan-test") -> ProxySellerProvider:
    return ProxySellerProvider(api_key, allow_spend=True, session=session)


async def _make_lease(tmp_db: str, **overrides) -> int:
    kwargs = dict(
        provider_type="proxy_seller", host="10.9.0.1", port=50101, manager_key=None,
        provider_proxy_id="PXY-orphan-1", proxy_type="ipv4", scheme="socks5",
        login="u1", password="pw1", status="active",
        # Default OFF -- P1-P8/P13/P14 call wrapped_execute directly and
        # don't need this; leaving it True by default would sweep every
        # fixture lease created earlier in the file into P15's engine-2
        # eligibility scan (_renewal_eligible_leases), corrupting that
        # scenario's isolation. Only P15's own two leases opt in explicitly.
        auto_renew_enabled=False,
        db_path=tmp_db, expires_at="2026-08-11",
    )
    kwargs.update(overrides)
    return await storage.proxy_lease_create(**kwargs)


async def _make_manager(db_path: str, manager_key: str, **overrides) -> None:
    fields = dict(
        status="active", is_enabled=1, manual_stopped=0, proxy_enabled=1,
        proxy_host="", proxy_port=None, proxy_lease_id=None,
        display_name=manager_key,
    )
    fields.update(overrides)
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        cols = ["manager_key"] + list(fields.keys())
        vals = [manager_key] + list(fields.values())
        placeholders = ",".join("?" * len(cols))
        await db.execute(f"INSERT INTO managers({','.join(cols)}) VALUES ({placeholders})", vals)
        await db.commit()


async def main_async() -> int:
    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))

    notifications: list = []
    fake_provider_holder: dict = {}

    def _fake_pbuy_provider():
        return fake_provider_holder.get("instance")

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_create_panel_notification(kind, title, body):
        notifications.append({"kind": kind, "title": title, "body": body})

    async def _noop_ensure_schema():
        # The real _tpag_ensure_schema is a stacked-override chain of
        # idempotent ALTER TABLE noise (auth_guard_* columns) unrelated to
        # this correction; storage.init_db() below already creates the full
        # managers schema this test actually reads (proxy_host/port/enabled).
        return None

    from manager_registry import normalize_manager_key as _real_normalize_key

    ns = extract_and_exec(
        main_tree,
        NAMES,
        {
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "datetime": datetime.datetime, "asyncio": asyncio, "os": os, "traceback": __import__("traceback"),
            "_pbuy_provider": _fake_pbuy_provider,
            "_pbuy_call": _fake_pbuy_call,
            "_PRENEW_PROXY_TYPE": "ipv4",
            "_PRENEW_PAYMENT_ID": 1,
            "_PBUY_PERIOD_ID": "1m",
            "_PBUY_PERIOD_NAME": "1 month",
            "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 1,
            "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0,
            "_PRENEW_LEAD_DAYS_DEFAULT": 7,
            "_now_utc_iso": lambda: "2026-08-10T12:00:00",
            "_kyiv_now": lambda: datetime.datetime(2026, 8, 10, 12, 0, 0),
            "CONTROLLER_MODE": True,
            "_pbuy_json": __import__("json"),
            "_create_panel_notification": _fake_create_panel_notification,
            "registry_normalize_manager_key": _real_normalize_key,
            "aiosqlite": __import__("aiosqlite"),
            "_tpag_ensure_schema": _noop_ensure_schema,
        },
    )
    wrapped_execute = ns["_renewal_wrapped_execute"]
    is_orphan = ns["_prenew_lease_is_orphan"]
    lease_role = ns["_prenew_lease_role"]
    autorenew_one = ns["_prenew_autorenew_one"]
    auto_tick = ns["_renewal_auto_tick"]
    eligible_leases_fn = ns["_renewal_eligible_leases"]
    confirm_cmd_renew = ns["_handle_proxy_renew_confirm_command"]
    confirm_cmd_renewal = ns["_handle_proxy_renewal_confirm_command"]

    with tempfile.TemporaryDirectory(prefix="tpilot_orphan_lease_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)
        ns["TPILOT_DB_PATH"] = tmp_db
        storage.DB_PATH = tmp_db
        storage.QUEUE_DB_PATH = tmp_db
        await storage.init_db()

        # ==================================================================
        # P1-P8: pure classification (_prenew_lease_role / _prenew_lease_is_orphan)
        # ==================================================================
        lid_p1 = await _make_lease(tmp_db, manager_key=None)
        lease_p1 = await storage.proxy_lease_get(lid_p1, db_path=tmp_db)
        check("P1. manager_key NULL -> orphan", await is_orphan(lease_p1), lease_p1.get("manager_key"))

        lid_p2 = await _make_lease(tmp_db, manager_key="")
        lease_p2 = await storage.proxy_lease_get(lid_p2, db_path=tmp_db)
        check("P2. manager_key '' -> orphan", await is_orphan(lease_p2), lease_p2.get("manager_key"))

        lid_p3 = await _make_lease(tmp_db, manager_key="   ")
        lease_p3 = await storage.proxy_lease_get(lid_p3, db_path=tmp_db)
        check("P3. manager_key whitespace -> orphan", await is_orphan(lease_p3), repr(lease_p3.get("manager_key")))

        lid_p4 = await _make_lease(tmp_db, manager_key="_")
        lease_p4 = await storage.proxy_lease_get(lid_p4, db_path=tmp_db)
        check("P4. manager_key '_' (placeholder) -> orphan", await is_orphan(lease_p4), lease_p4.get("manager_key"))

        lid_p5 = await _make_lease(tmp_db, manager_key="ghost_mgr")
        lease_p5 = await storage.proxy_lease_get(lid_p5, db_path=tmp_db)
        check("P5. manager_key textually present, managers row missing -> orphan", await is_orphan(lease_p5), lease_p5.get("manager_key"))

        await _make_manager(tmp_db, "mgr_p6", proxy_lease_id=99999)  # points at a DIFFERENT lease
        lid_p6 = await _make_lease(tmp_db, manager_key="mgr_p6", host="10.9.0.6", port=50106)
        lease_p6 = await storage.proxy_lease_get(lid_p6, db_path=tmp_db)
        check("P6. manager exists, proxy assignment mismatch -> orphan", await is_orphan(lease_p6), lease_p6.get("manager_key"))

        lid_p7 = await _make_lease(tmp_db, manager_key="mgr_p7", host="10.9.0.7", port=50107)
        await _make_manager(tmp_db, "mgr_p7", proxy_lease_id=lid_p7)
        lease_p7 = await storage.proxy_lease_get(lid_p7, db_path=tmp_db)
        check("P7. manager exists + assignment valid -> NOT orphan", not await is_orphan(lease_p7), lease_p7.get("manager_key"))

        lid_p8 = await _make_lease(tmp_db, manager_key="mgr_p8", host="10.9.0.8", port=50108)
        await _make_manager(tmp_db, "mgr_p8", proxy_lease_id=lid_p8, status="disabled", is_enabled=0, manual_stopped=1)
        lease_p8 = await storage.proxy_lease_get(lid_p8, db_path=tmp_db)
        check("P8. manager disabled/manual_stopped but still canonical owner -> NOT orphan solely because disabled",
              not await is_orphan(lease_p8), lease_p8.get("manager_key"))

        # ==================================================================
        # P9-P12: an orphan due-and-unconfirmed lease must reach the
        # provider ZERO times, spend ZERO times, retry ZERO times, and
        # produce ZERO AdminBot notifications -- all via the REAL shared
        # spend choke point (_renewal_wrapped_execute).
        # ==================================================================
        lid_p9 = await _make_lease(tmp_db, manager_key="_", host="10.9.0.9", port=50109, provider_proxy_id="PXY-P9", expires_at="2026-08-11")
        lease_p9 = await storage.proxy_lease_get(lid_p9, db_path=tmp_db)
        session_p9 = FakeSession()  # deliberately empty queue -- ANY call raises
        fake_provider_holder["instance"] = _real_provider(session_p9)
        res_p9 = await wrapped_execute(lease_p9, source="autorenew")
        check("P9. orphan due lease -> provider renew calls = 0", len(session_p9.calls) == 0, session_p9.calls)
        check("P9b. orphan lease -> wrapped_execute returns skipped, error=orphan_lease", res_p9.get("skipped") is True and res_p9.get("error") == "orphan_lease", res_p9)

        # P10: previous-attempt-unconfirmed shape (an existing terminal
        # 'failed' op occupying the idempotency_key) PLUS orphan -- must
        # still be caught by the orphan gate before even reaching the
        # idempotency-key collision logic (retry/spend calls = 0).
        lid_p10 = await _make_lease(tmp_db, manager_key="_", host="10.9.0.10", port=50110, provider_proxy_id="PXY-P10", expires_at="2026-08-10")
        lease_p10 = await storage.proxy_lease_get(lid_p10, db_path=tmp_db)
        await storage.proxy_renewal_op_create(
            lease_id=lid_p10, provider_proxy_id="PXY-P10",
            idempotency_key=storage.proxy_renewal_idempotency_key("PXY-P10", "2026-08-10", "1m"),
            source="autorenew", status="failed", period_id="1m",
            expires_before="2026-08-10", actor_user_id=None, db_path=tmp_db,
        )
        session_p10 = FakeSession()
        fake_provider_holder["instance"] = _real_provider(session_p10)
        res_p10 = await wrapped_execute(lease_p10, source="autorenew")
        check("P10. orphan + previous-attempt-unconfirmed -> retry/spend calls = 0", len(session_p10.calls) == 0, session_p10.calls)
        check("P10b. result is orphan_lease, NOT blocked_unresolved (orphan gate wins)", res_p10.get("error") == "orphan_lease", res_p10)

        check("P11. orphan lease path never reaches allow_spend (zero HTTP calls of any kind, P9+P10 combined)",
              len(session_p9.calls) == 0 and len(session_p10.calls) == 0, (session_p9.calls, session_p10.calls))

        notifications.clear()
        await autorenew_one(lease_p9)
        await ns["_renewal_auto_tick_report_outcome"](lease_p10, {"ok": False, "error": "blocked_unresolved", "message": "x"})
        check("P12. orphan lease -> AdminBot notification calls = 0 (defensive guard inside the report functions)",
              len(notifications) == 0, notifications)

        # ==================================================================
        # P13-P14: a VALID managed lease must keep working exactly as before
        # -- due renewal still calls the provider, and a failed/unverified
        # outcome can still legitimately alert.
        # ==================================================================
        lid_p13 = await _make_lease(tmp_db, manager_key="mgr_p13", host="10.9.0.13", port=50113, provider_proxy_id="PXY-P13", expires_at="2026-08-11")
        await _make_manager(tmp_db, "mgr_p13", proxy_lease_id=lid_p13)
        lease_p13 = await storage.proxy_lease_get(lid_p13, db_path=tmp_db)
        session_p13 = FakeSession()
        session_p13.queue_response(200, _REF_OK)
        session_p13.queue_response(200, _MAKE_OK)
        fake_provider_holder["instance"] = _real_provider(session_p13)
        res_p13 = await wrapped_execute(lease_p13, source="autorenew")
        check("P13. valid manager + due lease -> existing renewal path still called (provider reached)",
              session_p13.prolong_make_call_count() == 1 and res_p13.get("ok") is True, (session_p13.calls, res_p13))

        lid_p14 = await _make_lease(tmp_db, manager_key="mgr_p14", host="10.9.0.14", port=50114, provider_proxy_id="PXY-P14", expires_at="2026-08-11")
        await _make_manager(tmp_db, "mgr_p14", proxy_lease_id=lid_p14)
        lease_p14 = await storage.proxy_lease_get(lid_p14, db_path=tmp_db)
        notifications.clear()
        await ns["_renewal_auto_tick_report_outcome"](lease_p14, {"ok": False, "error": "unverified", "message": "not confirmed yet"})
        check("P14. valid manager + failed/unverified outcome -> existing legitimate alert still possible",
              len(notifications) == 1, notifications)

        # ==================================================================
        # P15: one orphan + one valid lease in the SAME engine-2 tick --
        # valid processes normally, orphan is skipped independently, neither
        # affects the other.
        # ==================================================================
        lid_p15v = await _make_lease(tmp_db, manager_key="mgr_p15", host="10.9.0.15", port=50115, provider_proxy_id="PXY-P15V", expires_at="2026-08-11", auto_renew_enabled=True)
        await _make_manager(tmp_db, "mgr_p15", proxy_lease_id=lid_p15v)
        lid_p15o = await _make_lease(tmp_db, manager_key="_", host="10.9.0.16", port=50116, provider_proxy_id="PXY-P15O", expires_at="2026-08-11", auto_renew_enabled=True)

        session_p15 = FakeSession()
        session_p15.queue_response(200, _REF_OK)    # calc preview's reference_list
        session_p15.queue_response(200, _CALC_OK)   # calc preview's prolong_calc
        session_p15.queue_response(200, _REF_OK)    # wrapped_execute's own preflight
        session_p15.queue_response(200, _MAKE_OK)   # the actual spend
        fake_provider_holder["instance"] = _real_provider(session_p15)
        notifications.clear()
        await storage.proxy_renewal_config_set(automation_enabled=1, db_path=tmp_db)
        await auto_tick()
        check("P15. mixed tick: valid lease renewed (provider reached exactly once)",
              session_p15.prolong_make_call_count() == 1, session_p15.calls)
        check("P15b. mixed tick: orphan lease produced zero notifications, zero extra provider calls",
              len(notifications) == 0 and session_p15.prolong_make_call_count() == 1, (notifications, session_p15.calls))
        lease_p15v_after = await storage.proxy_lease_get(lid_p15v, db_path=tmp_db)
        check("P15c. valid lease's own expiry actually advanced (real renewal happened, not a no-op)",
              lease_p15v_after.get("expires_at") != "2026-08-11", lease_p15v_after.get("expires_at"))

        # ==================================================================
        # EXACT USER REGRESSION: lease_id-shaped fixture matching the real
        # production bug -- manager_key="_", previous attempt unconfirmed.
        # ==================================================================
        lid_real = await _make_lease(
            tmp_db, manager_key="_", host="146.247.113.69", port=50101,
            provider_proxy_id="PXY-REAL-15", expires_at="2026-08-09",
        )
        lease_real = await storage.proxy_lease_get(lid_real, db_path=tmp_db)
        await storage.proxy_renewal_op_create(
            lease_id=lid_real, provider_proxy_id="PXY-REAL-15",
            idempotency_key=storage.proxy_renewal_idempotency_key("PXY-REAL-15", "2026-08-09", "1m"),
            source="autorenew", status="failed", period_id="1m",
            expires_before="2026-08-09", actor_user_id=None, db_path=tmp_db,
        )
        session_real = FakeSession()
        fake_provider_holder["instance"] = _real_provider(session_real)
        notifications.clear()
        real_result = await wrapped_execute(lease_real, source="autorenew")
        check("REAL. exact user regression (lease manager_key='_', unconfirmed prior attempt) -> ORPHAN", await is_orphan(lease_real), lease_real)
        check("REAL. RENEW_CALLS = 0", len(session_real.calls) == 0, session_real.calls)
        check("REAL. SPEND_CALLS = 0 (no prolong/make)", session_real.prolong_make_call_count() == 0, session_real.calls)
        check("REAL. RETRY_CALLS = 0", session_real.reference_list_call_count() == 0, session_real.calls)
        await autorenew_one(lease_real)
        await ns["_renewal_auto_tick_report_outcome"](lease_real, {"ok": False, "error": "blocked_unresolved", "message": "x"})
        check("REAL. ADMINBOT_ALERTS = 0", len(notifications) == 0, notifications)
        all_bodies = " ".join(str(n.get("body") or "") + str(n.get("title") or "") for n in notifications)
        check("REAL. '⚠️ Продление прокси требует проверки' never produced", "требует проверки" not in all_bodies, all_bodies)

        # ==================================================================
        # P16/P17 (checkpoint follow-up 20260810): the FULL manual command
        # entry points -- not _renewal_wrapped_execute called directly --
        # must also make ZERO provider calls for the exact regression lease.
        # Both manual commands (/proxy_renew_confirm and /proxy_renewal_
        # confirm) used to call the no-spend _renewal_calc_preview BEFORE
        # ever reaching wrapped_execute's gate -- an admin confirming an
        # orphan lease still triggered two live provider round-trips before
        # being rejected. Both commands were given their own gate at entry;
        # this proves it end-to-end through the REAL command handler, not
        # just the inner function.
        # ==================================================================
        session_p16 = FakeSession()
        fake_provider_holder["instance"] = _real_provider(session_p16)
        import json as _json_p16
        res_p16 = await confirm_cmd_renew(str(lid_real))
        data_p16 = _json_p16.loads(res_p16)
        check("P16. /proxy_renew_confirm on the exact orphan regression lease -> ZERO provider calls of any kind",
              len(session_p16.calls) == 0, session_p16.calls)
        check("P16b. /proxy_renew_confirm returns orphan_lease, not a generic/leaked error", data_p16.get("error") == "orphan_lease", data_p16)

        session_p17 = FakeSession()
        fake_provider_holder["instance"] = _real_provider(session_p17)
        res_p17 = await confirm_cmd_renewal(str(lid_real))
        data_p17 = _json_p16.loads(res_p17)
        check("P17. /proxy_renewal_confirm on the exact orphan regression lease -> ZERO provider calls of any kind",
              len(session_p17.calls) == 0, session_p17.calls)
        check("P17b. /proxy_renewal_confirm returns orphan_lease, not a generic/leaked error", data_p17.get("error") == "orphan_lease", data_p17)

        # Control: the SAME two manual commands on a VALID managed lease
        # must still work exactly as before (provider reached, real spend).
        lid_p17v = await _make_lease(tmp_db, manager_key="mgr_p17v", host="10.9.0.17", port=50117, provider_proxy_id="PXY-P17V", expires_at="2026-08-11")
        await _make_manager(tmp_db, "mgr_p17v", proxy_lease_id=lid_p17v)
        session_p17v = FakeSession()
        session_p17v.queue_response(200, _REF_OK)    # command's own no-spend calc preview
        session_p17v.queue_response(200, _CALC_OK)
        session_p17v.queue_response(200, _REF_OK)    # wrapped_execute's own preflight
        session_p17v.queue_response(200, _MAKE_OK)   # the actual spend
        fake_provider_holder["instance"] = _real_provider(session_p17v)
        res_p17v = await confirm_cmd_renew(str(lid_p17v))
        data_p17v = _json_p16.loads(res_p17v)
        check("P17v. /proxy_renew_confirm on a VALID managed lease still spends normally (unchanged behavior)",
              session_p17v.prolong_make_call_count() == 1 and data_p17v.get("ok") is True, (session_p17v.calls, data_p17v))

        # ==================================================================
        # MUTATION PROOFS
        # ==================================================================
        nodes_mut = {n: node for n in NAMES for node in [None]}
        picked = {}
        for node in main_tree.body:
            nm = getattr(node, "name", None)
            if nm in NAMES:
                picked[nm] = node
            elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in NAMES:
                picked[node.targets[0].id] = node
        real_src = "\n\n".join(ast.unparse(picked[n]) for n in NAMES if n in picked)

        base_extra_ns = {
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "datetime": datetime.datetime, "asyncio": asyncio, "os": os, "traceback": __import__("traceback"),
            "_pbuy_provider": _fake_pbuy_provider, "_pbuy_call": _fake_pbuy_call,
            "_PRENEW_PROXY_TYPE": "ipv4", "_PRENEW_PAYMENT_ID": 1, "_PBUY_PERIOD_ID": "1m",
            "_PBUY_PERIOD_NAME": "1 month", "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 1,
            "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0, "_PRENEW_LEAD_DAYS_DEFAULT": 7,
            "_now_utc_iso": lambda: "2026-08-10T12:00:00",
            "_kyiv_now": lambda: datetime.datetime(2026, 8, 10, 12, 0, 0),
            "CONTROLLER_MODE": True, "_pbuy_json": __import__("json"),
            "_create_panel_notification": _fake_create_panel_notification,
            "registry_normalize_manager_key": _real_normalize_key,
            "aiosqlite": __import__("aiosqlite"),
            "_tpag_ensure_schema": _noop_ensure_schema,
            "TPILOT_DB_PATH": tmp_db,
        }

        def exec_mutant(src):
            m_ns = dict(base_extra_ns)
            exec(compile(src, "<mutant>", "exec"), m_ns)
            return m_ns

        # M1: remove the '_' placeholder check -- concretely, force
        # _prenew_lease_role to treat manager_key='_' as a normal (non-
        # empty, non-placeholder) key with no special handling. Since '_'
        # already correctly resolves to 'orphan' via the manager-row-missing
        # branch (there IS no manager literally keyed '_'), the mutation
        # that actually matters is bypassing the WHOLE "does the manager
        # exist" check for this exact key -- simulate by short-circuiting
        # the function to always return 'managed' whenever manager_key=='_'.
        anchor_m1 = "if manager_key:"
        check("M1 setup: anchor found", anchor_m1 in real_src)
        mut1_src = real_src.replace(
            anchor_m1,
            "if manager_key == '_':\n            return 'managed'  # MUTATION: placeholder key treated as a real key\n        if manager_key:",
            1,
        )
        check("M1 setup: mutation changed the source", mut1_src != real_src)
        m1_ns = exec_mutant(mut1_src)
        m1_orphan = await m1_ns["_prenew_lease_is_orphan"](lease_real)
        check("M1. removing the placeholder '_' special-case makes the EXACT user regression lease WRONGLY 'managed' -- "
              "proves REAL.'ORPHAN'/P4 are load-bearing, not tautological", m1_orphan is False, m1_orphan)

        # M2: remove the manager-existence check -- treat a missing manager
        # row as if it were found (empty dict) instead of returning 'orphan'.
        anchor_m2 = "if manager_row is None:\n                return 'orphan'"
        check("M2 setup: anchor found", anchor_m2 in real_src)
        mut2_src = real_src.replace(
            anchor_m2,
            "if manager_row is None:\n"
            "                manager_row = {'status': 'active', 'proxy_enabled': 1, 'proxy_lease_id': lease.get('id')}"
            "  # MUTATION: missing manager fabricated as a fully valid one",
            1,
        )
        check("M2 setup: mutation changed the source", mut2_src != real_src)
        m2_ns = exec_mutant(mut2_src)
        lease_p5_fresh = await storage.proxy_lease_get(lid_p5, db_path=tmp_db)
        m2_role = m2_ns["_prenew_lease_role"](lease_p5_fresh, None, None)
        check("M2. removing the manager-existence check makes P5's missing-manager lease WRONGLY NOT 'orphan' -- "
              "proves P5 is load-bearing", m2_role != "orphan", m2_role)

        # M3: remove the assignment-match check -- treat every existing,
        # live, proxy-enabled manager as the current owner regardless of
        # proxy_lease_id/host:port.
        anchor_m3 = "return 'managed' if is_current else 'orphan'"
        check("M3 setup: anchor found", anchor_m3 in real_src)
        mut3_src = real_src.replace(anchor_m3, "return 'managed'  # MUTATION: assignment match never checked", 1)
        check("M3 setup: mutation changed the source", mut3_src != real_src)
        m3_ns = exec_mutant(mut3_src)
        mgr_p6_row = await storage.get_manager_row_from_db_sync(tmp_db, "mgr_p6") if hasattr(storage, "get_manager_row_from_db_sync") else None
        lease_p6_fresh = await storage.proxy_lease_get(lid_p6, db_path=tmp_db)
        # Fetch the manager row the same way _prenew_lease_is_orphan would.
        import aiosqlite as _m3_aiosqlite
        async with _m3_aiosqlite.connect(tmp_db) as _m3db:
            _m3db.row_factory = _m3_aiosqlite.Row
            _cur = await _m3db.execute("SELECT * FROM managers WHERE manager_key=?", ("mgr_p6",))
            _row = await _cur.fetchone()
            mgr_p6_row = dict(_row) if _row else None
        m3_role = m3_ns["_prenew_lease_role"](lease_p6_fresh, mgr_p6_row, None)
        check("M3. removing the assignment-match check makes P6's stale/mismatched lease WRONGLY 'managed' -- "
              "proves P6 is load-bearing", m3_role != "orphan", m3_role)

        # M4: move the orphan guard to AFTER the provider renew call inside
        # _renewal_wrapped_execute -- proves the renew-call-count test (P9)
        # is load-bearing, not vacuously green because nothing was ever
        # exercised.
        anchor_m4 = "if await _prenew_lease_is_orphan(lease, context=f'wrapped_execute:{source}'):"
        check("M4 setup: anchor found", anchor_m4 in real_src)
        mut4_src = real_src.replace(anchor_m4, "if False:  # MUTATION: orphan gate disabled in wrapped_execute", 1)
        check("M4 setup: mutation changed the source", mut4_src != real_src)
        m4_ns = exec_mutant(mut4_src)
        session_m4 = FakeSession()
        session_m4.queue_response(200, _REF_OK)
        session_m4.queue_response(200, _MAKE_OK)
        fake_provider_holder["instance"] = _real_provider(session_m4)
        lease_p9_fresh = await storage.proxy_lease_get(lid_p9, db_path=tmp_db)
        await m4_ns["_renewal_wrapped_execute"](lease_p9_fresh, source="autorenew")
        check("M4. disabling the wrapped_execute orphan gate lets an orphan lease WRONGLY reach the provider -- "
              "proves P9 (renew calls = 0) is load-bearing", len(session_m4.calls) > 0, session_m4.calls)

        # M5: remove the defensive notification guard inside
        # _renewal_auto_tick_report_outcome -- proves P12's alert-count
        # assertion is load-bearing.
        anchor_m5 = "if await _prenew_lease_is_orphan(lease, context='auto_tick_report'):\n        return"
        check("M5 setup: anchor found", anchor_m5 in real_src)
        mut5_src = real_src.replace(anchor_m5, "if False:\n        return  # MUTATION: defensive notify guard disabled", 1)
        check("M5 setup: mutation changed the source", mut5_src != real_src)
        m5_ns = exec_mutant(mut5_src)
        notifications.clear()
        lease_p10_fresh = await storage.proxy_lease_get(lid_p10, db_path=tmp_db)
        await m5_ns["_renewal_auto_tick_report_outcome"](lease_p10_fresh, {"ok": False, "error": "blocked_unresolved", "message": "требует проверки"})
        check("M5. disabling the defensive notify guard lets an orphan lease WRONGLY produce an alert -- "
              "proves P12/section-11 two-barrier design is load-bearing", len(notifications) > 0, notifications)

        # M6: mistakenly treat a disabled-but-canonical-owner manager as
        # orphan -- proves P8 is load-bearing.
        anchor_m6 = "if int(manager_row.get('proxy_enabled') or 0) != 1:\n                return 'orphan'"
        check("M6 setup: anchor found", anchor_m6 in real_src)
        mut6_src = real_src.replace(
            anchor_m6,
            "if int(manager_row.get('proxy_enabled') or 0) != 1:\n                return 'orphan'\n"
            "            if int(manager_row.get('is_enabled') or 1) == 0 or int(manager_row.get('manual_stopped') or 0) == 1:\n"
            "                return 'orphan'  # MUTATION: disabled treated as orphan",
            1,
        )
        check("M6 setup: mutation changed the source", mut6_src != real_src)
        m6_ns = exec_mutant(mut6_src)
        import aiosqlite as _m6_aiosqlite
        async with _m6_aiosqlite.connect(tmp_db) as _m6db:
            _m6db.row_factory = _m6_aiosqlite.Row
            _cur = await _m6db.execute("SELECT * FROM managers WHERE manager_key=?", ("mgr_p8",))
            _row = await _cur.fetchone()
            mgr_p8_row = dict(_row) if _row else None
        lease_p8_fresh = await storage.proxy_lease_get(lid_p8, db_path=tmp_db)
        m6_role = m6_ns["_prenew_lease_role"](lease_p8_fresh, mgr_p8_row, None)
        check("M6. treating disabled-but-canonical-owner as orphan WRONGLY flags P8's lease -- "
              "proves P8 (disabled != orphan) is load-bearing", m6_role == "orphan", m6_role)

        # M7/M8 (checkpoint follow-up 20260810): disable the two manual-
        # command entry gates (P16/P17) one at a time -- proves those checks
        # are load-bearing, not just re-testing wrapped_execute's own gate
        # by coincidence.
        anchor_m7 = "if await _prenew_lease_is_orphan(lease, context='panel_renew_confirm_entry'):"
        check("M7 setup: anchor found", anchor_m7 in real_src)
        mut7_src = real_src.replace(anchor_m7, "if False:  # MUTATION: manual /proxy_renew_confirm entry gate disabled", 1)
        check("M7 setup: mutation changed the source", mut7_src != real_src)
        m7_ns = exec_mutant(mut7_src)
        session_m7 = FakeSession()
        fake_provider_holder["instance"] = _real_provider(session_m7)
        lease_real_fresh = await storage.proxy_lease_get(lid_real, db_path=tmp_db)
        await m7_ns["_handle_proxy_renew_confirm_command"](str(lid_real))
        check("M7. disabling /proxy_renew_confirm's own entry gate lets the exact regression lease WRONGLY "
              "reach the provider (via its no-spend preview) before wrapped_execute's gate -- proves P16 is load-bearing",
              len(session_m7.calls) > 0, session_m7.calls)

        anchor_m8 = "if await _prenew_lease_is_orphan(lease, context='manual_renewal_confirm_entry'):"
        check("M8 setup: anchor found", anchor_m8 in real_src)
        mut8_src = real_src.replace(anchor_m8, "if False:  # MUTATION: manual /proxy_renewal_confirm entry gate disabled", 1)
        check("M8 setup: mutation changed the source", mut8_src != real_src)
        m8_ns = exec_mutant(mut8_src)
        session_m8 = FakeSession()
        fake_provider_holder["instance"] = _real_provider(session_m8)
        await m8_ns["_handle_proxy_renewal_confirm_command"](str(lid_real))
        check("M8. disabling /proxy_renewal_confirm's own entry gate lets the exact regression lease WRONGLY "
              "reach the provider before wrapped_execute's gate -- proves P17 is load-bearing",
              len(session_m8.calls) > 0, session_m8.calls)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PROXY ORPHAN LEASE SELFTESTS PASSED")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
