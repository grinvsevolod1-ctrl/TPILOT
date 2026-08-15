# -*- coding: utf-8 -*-
"""tools/proxy_renewal_crash_window_selftest.py -- end-to-end selftest for
the crash-window fix (independent re-review correction, 20260809), on top of
the already-closed BL-1 fix (see proxy_renewal_idempotency_selftest.py).

BEFORE this fix: _renewal_wrapped_execute created the proxy_renewal_ops row
with status='make_pending' from the moment of proxy_renewal_op_create's
commit -- i.e. BEFORE _prenew_execute_renewal had even started, let alone
called prolong_make. A crash in that narrow window (op row committed, spend
never attempted) permanently burned the idempotency_key: restart reconcile
saw a 'make_pending' row, could not confirm a renewal via the provider
(there was nothing to confirm -- the provider was never called), and marked
it 'failed' -- which leaves proxy_renewal_ops_idem_idx (a GLOBAL unique
index, not scoped to active statuses) permanently blocking any future
prolong_make for that (provider_proxy_id, pre-renewal expiry, period)
triple. The proxy silently expires with zero spend and no recovery path.

AFTER this fix: op_create writes status='pending' (RESERVED -- idempotency
key claimed, spend NOT started). _prenew_execute_renewal atomically CAS-
advances 'pending' -> 'make_pending' (SPEND_STARTED) immediately before
calling prolong_make. A row still found in 'pending' at restart is PROVABLY
pre-spend and is freed (audit-logged, then deleted -- the only way to
release a GLOBAL unique index) by proxy_renewal_op_delete_pending, both
inline (same-process, if the marker write itself fails) and via restart
reconcile (_renewal_reconcile_pending_on_start). A row found in
'make_pending' is handled exactly as before this fix (read-only provider-
refresh reconcile, never a blind re-make).

Cases (task spec):
  G. Crash BEFORE spend-start marker (op row exists, still 'pending') ->
     restart reconcile frees it with NO provider call -> the SAME lease can
     retry and spends EXACTLY once.
  H. Crash AFTER SPEND_STARTED, before the response was processed
     ('make_pending', provider expiry UNCHANGED) -> restart reconcile marks
     'failed', NEVER re-calls prolong_make, and the idempotency_key stays
     permanently consumed for that pre-renewal expiry (retry is SKIPPED).
  I. Crash AFTER SPEND_STARTED and the provider actually renewed
     ('make_pending', provider expiry ADVANCED) -> restart reconcile marks
     'success' from the provider's own state, NEVER re-calls prolong_make.
  J. Cross-engine concurrency: engine 1 (source='autorenew') and engine 2
     (source='auto', the dormant-by-default TPilot-managed engine) racing
     for the SAME lease/expiry -> total spend <= 1. Also manual + auto.

main.py cannot be imported standalone -- AST-extraction idiom (same as
proxy_renewal_idempotency_selftest.py) + a real temp SQLite DB (storage runs
for real) + a fake ProxySellerProvider the test fully controls. No network,
no Telegram, no real provider writes.

Run:  python tools\\proxy_renewal_crash_window_selftest.py
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

MAIN_PY = BASE_DIR / "main.py"

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
    exec(compile(module_src, "<main.py crash-window extract>", "exec"), ns)
    return ns


class FakeProvider:
    """In-memory Proxy-Seller stand-in, fully test-controlled. Never
    touches the network. Tracks prolong_make/list_proxies call counts so
    tests can assert exact call counts (in particular: ZERO provider calls
    to resolve a 'pending' row, and ZERO prolong_make calls from reconcile
    no matter what it finds)."""

    def __init__(self):
        self.prolong_make_calls = 0
        self.list_proxies_calls = 0
        self.reference_list_raises: Exception | None = None
        self.period_available = True
        self.make_response = {"data": {"total": "1.8", "currency": "USD", "date_end": "2026-09-08"}}
        self._list_responses: list[list[dict]] = []

    def reference_list(self, proxy_type):
        if self.reference_list_raises is not None:
            raise self.reference_list_raises
        if self.period_available:
            return {"data": [{"id": "1m", "name": "1 month"}]}
        return {"data": []}

    def prolong_calc(self, proxy_type, ids, period_id, payment_id):
        return {"data": {"total": "1.8", "currency": "USD", "price": "1.8"}}

    def prolong_make(self, proxy_type, ids, period_id, payment_id, *, allow_spend=False):
        assert allow_spend is True, "prolong_make must only ever be called with allow_spend=True"
        self.prolong_make_calls += 1
        return dict(self.make_response)

    def list_proxies(self, proxy_type, **kwargs):
        self.list_proxies_calls += 1
        if self._list_responses:
            return {"data": self._list_responses.pop(0)}
        return {"data": []}

    def queue_list_response(self, entries: list[dict]) -> None:
        self._list_responses.append(entries)

    def balance(self):
        return {"data": {"summ": "100.0"}, "summ": "100.0"}


async def _make_lease(tmp_db: str, **overrides) -> int:
    kwargs = dict(
        provider_type="proxy_seller", host="1.2.3.4", port=50101, manager_key="mgr_a",
        provider_proxy_id="PXY-crash-1", proxy_type="ipv4", scheme="socks5",
        login="u1", password="pw1", status="active", db_path=tmp_db,
        expires_at="2026-08-08",
    )
    kwargs.update(overrides)
    return await storage.proxy_lease_create(**kwargs)


async def main_async() -> int:
    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))

    fake_provider_holder: dict = {}

    def _fake_pbuy_provider():
        return fake_provider_holder.get("instance")

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_never_orphan(lease, usage_map=None, *, context="gate"):
        return False

    ns = extract_and_exec(
        main_tree,
        {
            "_prenew_preflight_check",
            "_prenew_execute_renewal",
            "_renewal_wrapped_execute",
            "_renewal_reconcile_pending_on_start",
            "_renewal_guard_check",
            "_renewal_calc_preview",
            "_renewal_float",
            "_renewal_kyiv_today_str",
            "_handle_proxy_renewal_confirm_command",
            "_prenew_verify_renewal_via_refresh",
            "_prenew_find_provider_entry",
            "_prenew_classify_provider_error",
            "_ppool_parse_expires_at",
            "_pbuy_safe_error",
        },
        {
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "datetime": datetime.datetime, "asyncio": asyncio,
            "_pbuy_provider": _fake_pbuy_provider,
            "_pbuy_call": _fake_pbuy_call,
            "_PRENEW_PROXY_TYPE": "ipv4",
            "_PRENEW_PAYMENT_ID": 1,
            "_PBUY_PERIOD_ID": "1m",
            "_PBUY_PERIOD_NAME": "1 month",
            "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 1,
            "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0,
            "_now_utc_iso": lambda: "2026-08-08T12:00:00",
            "_kyiv_now": lambda: datetime.datetime(2026, 8, 8, 12, 0, 0),
            "CONTROLLER_MODE": True,
            "_pbuy_json": __import__("json"),
            # CORRECTION B 20260810: see the identical comment in
            # proxy_renewal_wiring_selftest.py -- this file predates the
            # orphan gate and tests unrelated crash-window invariants.
            "_prenew_lease_is_orphan": _fake_never_orphan,
        },
    )
    wrapped_execute = ns["_renewal_wrapped_execute"]
    reconcile_fn = ns["_renewal_reconcile_pending_on_start"]
    confirm_cmd = ns["_handle_proxy_renewal_confirm_command"]

    with tempfile.TemporaryDirectory(prefix="tpilot_renewal_crash_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)
        ns["TPILOT_DB_PATH"] = tmp_db

        # ------------------------------------------------------------------
        # CASE G: crash BEFORE the SPEND_STARTED marker -- op row exists,
        # still 'pending' (simulates proxy_renewal_op_create's commit
        # succeeding, then the process dying before _prenew_execute_renewal
        # even ran). No provider call should be needed to resolve this.
        # ------------------------------------------------------------------
        lease_g_id = await _make_lease(tmp_db, provider_proxy_id="PXY-G", host="10.1.0.1", port=52001, expires_at="2026-08-08")
        lease_g = await storage.proxy_lease_get(lease_g_id, db_path=tmp_db)
        idem_g = storage.proxy_renewal_idempotency_key("PXY-G", "2026-08-08", "1m")
        op_g_id = await storage.proxy_renewal_op_create(
            lease_id=lease_g_id, provider_proxy_id="PXY-G", idempotency_key=idem_g,
            source="autorenew", status="pending", period_id="1m",
            expires_before="2026-08-08", db_path=tmp_db,
        )
        check("G0. (setup) orphaned 'pending' op created directly, simulating a pre-marker crash", op_g_id is not None, op_g_id)
        active_before = await storage.proxy_renewal_op_active_for_lease(lease_g_id, db_path=tmp_db)
        check("G1. (setup) the orphaned 'pending' op is still counted as 'active' (blocks a 2nd op)", active_before is not None and active_before["id"] == op_g_id)

        provider_g = FakeProvider()
        fake_provider_holder["instance"] = provider_g
        await reconcile_fn()

        check("G2. restart reconcile made NO provider call to resolve the 'pending' row", provider_g.list_proxies_calls == 0, provider_g.list_proxies_calls)
        check("G3. restart reconcile NEVER calls prolong_make", provider_g.prolong_make_calls == 0, provider_g.prolong_make_calls)
        op_g_after = await storage.proxy_renewal_op_get(op_g_id, db_path=tmp_db)
        check("G4. the orphaned 'pending' op row is GONE (idempotency_key freed)", op_g_after is None, op_g_after)
        active_after = await storage.proxy_renewal_op_active_for_lease(lease_g_id, db_path=tmp_db)
        check("G5. the lease no longer has an active op (free to retry)", active_after is None, active_after)
        events_g = await storage.proxy_lifecycle_events_for_lease(lease_g_id, db_path=tmp_db)
        check("G6. an audit trail was written before the row was deleted (renewal_reservation_released)",
              any(e.get("event") == "renewal_reservation_released" for e in events_g), events_g)

        # the SAME lease/expiry now retries and spends EXACTLY once.
        res_g_retry = await wrapped_execute(lease_g, source="autorenew")
        check("G7. retry after the freed reservation succeeds", res_g_retry.get("ok") is True, res_g_retry)
        check("G8. retry after the freed reservation spends EXACTLY ONCE", provider_g.prolong_make_calls == 1, provider_g.prolong_make_calls)

        # ------------------------------------------------------------------
        # CASE H: crash AFTER SPEND_STARTED, before the response was
        # processed -- op row is 'make_pending', provider expiry UNCHANGED
        # (the provider never actually received/executed the call, or its
        # response never reached this process). Must NEVER re-call
        # prolong_make, and the idempotency_key must stay burned.
        # ------------------------------------------------------------------
        lease_h_id = await _make_lease(tmp_db, provider_proxy_id="PXY-H", host="10.1.0.2", port=52002, expires_at="2026-08-08")
        lease_h = await storage.proxy_lease_get(lease_h_id, db_path=tmp_db)
        idem_h = storage.proxy_renewal_idempotency_key("PXY-H", "2026-08-08", "1m")
        op_h_id = await storage.proxy_renewal_op_create(
            lease_id=lease_h_id, provider_proxy_id="PXY-H", idempotency_key=idem_h,
            source="autorenew", status="pending", period_id="1m",
            expires_before="2026-08-08", db_path=tmp_db,
        )
        marked_h = await storage.proxy_renewal_op_advance(op_h_id, "make_pending", from_status="pending", db_path=tmp_db)
        check("H0. (setup) op advanced to 'make_pending' (simulates the marker write landing)", marked_h is True)

        provider_h = FakeProvider()
        provider_h.queue_list_response([{"id": "PXY-H", "ip": "10.1.0.2", "port_socks": 52002, "date_end": "2026-08-08", "order_id": "ORD-H"}])
        fake_provider_holder["instance"] = provider_h
        await reconcile_fn()

        check("H1. restart reconcile DID make a provider refresh call for the 'make_pending' row", provider_h.list_proxies_calls == 1, provider_h.list_proxies_calls)
        check("H2. restart reconcile NEVER re-calls prolong_make", provider_h.prolong_make_calls == 0, provider_h.prolong_make_calls)
        op_h_after = await storage.proxy_renewal_op_get(op_h_id, db_path=tmp_db)
        check("H3. unchanged provider expiry -> op marked 'failed' (safe, not a guess)", op_h_after is not None and op_h_after["status"] == "failed", op_h_after)

        # retry for the SAME lease/expiry is SKIPPED -- idempotency_key stays consumed.
        res_h_retry = await wrapped_execute(lease_h, source="autorenew")
        check("H4. retry for the SAME expiry after an unresolved 'make_pending' is SKIPPED", res_h_retry.get("skipped") is True, res_h_retry)
        check("H5. retry spends ZERO additional times (no double-spend risk)", provider_h.prolong_make_calls == 0, provider_h.prolong_make_calls)

        # ------------------------------------------------------------------
        # CASE I: crash AFTER SPEND_STARTED, and the provider's own state
        # shows the renewal actually went through (expiry advanced). Must
        # mark success from that evidence alone, NEVER re-call prolong_make.
        # ------------------------------------------------------------------
        lease_i_id = await _make_lease(tmp_db, provider_proxy_id="PXY-I", host="10.1.0.3", port=52003, expires_at="2026-08-08")
        lease_i = await storage.proxy_lease_get(lease_i_id, db_path=tmp_db)
        idem_i = storage.proxy_renewal_idempotency_key("PXY-I", "2026-08-08", "1m")
        op_i_id = await storage.proxy_renewal_op_create(
            lease_id=lease_i_id, provider_proxy_id="PXY-I", idempotency_key=idem_i,
            source="autorenew", status="pending", period_id="1m",
            expires_before="2026-08-08", db_path=tmp_db,
        )
        await storage.proxy_renewal_op_advance(op_i_id, "make_pending", from_status="pending", db_path=tmp_db)

        provider_i = FakeProvider()
        provider_i.queue_list_response([{"id": "PXY-I", "ip": "10.1.0.3", "port_socks": 52003, "date_end": "2026-09-08", "order_id": "ORD-I"}])
        fake_provider_holder["instance"] = provider_i
        await reconcile_fn()

        check("I1. restart reconcile NEVER calls prolong_make even when it confirms success", provider_i.prolong_make_calls == 0, provider_i.prolong_make_calls)
        op_i_after = await storage.proxy_renewal_op_get(op_i_id, db_path=tmp_db)
        check("I2. advanced provider expiry -> op marked 'success' from provider evidence alone",
              op_i_after is not None and op_i_after["status"] == "success", op_i_after)
        check("I3. op's expires_after reflects the provider-observed new date",
              op_i_after is not None and op_i_after.get("expires_after") == "2026-09-08", op_i_after)

        # ------------------------------------------------------------------
        # CASE J: cross-engine concurrency -- engine 1 ('autorenew') and
        # engine 2 ('auto', the dormant-by-default TPilot-managed engine)
        # racing for the SAME lease/expiry -> total spend <= 1.
        # ------------------------------------------------------------------
        lease_j_id = await _make_lease(tmp_db, provider_proxy_id="PXY-J", host="10.1.0.4", port=52004, expires_at="2026-08-08")
        lease_j = await storage.proxy_lease_get(lease_j_id, db_path=tmp_db)
        provider_j = FakeProvider()
        fake_provider_holder["instance"] = provider_j
        results_j = await asyncio.gather(
            wrapped_execute(lease_j, source="autorenew"),
            wrapped_execute(lease_j, source="auto"),
        )
        ok_j = sum(1 for r in results_j if r.get("ok"))
        skipped_j = sum(1 for r in results_j if r.get("skipped"))
        check("J1. engine 1 vs engine 2: exactly one caller succeeds", ok_j == 1, results_j)
        check("J2. engine 1 vs engine 2: exactly one caller is skipped", skipped_j == 1, results_j)
        check("J3. engine 1 vs engine 2: total spend is exactly 1 (never 2)", provider_j.prolong_make_calls == 1, provider_j.prolong_make_calls)

        # manual + auto (engine 2)
        lease_k_id = await _make_lease(tmp_db, provider_proxy_id="PXY-K", host="10.1.0.5", port=52005, expires_at="2026-08-08")
        lease_k = await storage.proxy_lease_get(lease_k_id, db_path=tmp_db)
        provider_k = FakeProvider()
        fake_provider_holder["instance"] = provider_k
        results_k = await asyncio.gather(
            wrapped_execute(lease_k, source="manual"),
            wrapped_execute(lease_k, source="auto"),
        )
        ok_k = sum(1 for r in results_k if r.get("ok"))
        skipped_k = sum(1 for r in results_k if r.get("skipped"))
        check("K1. manual vs auto: exactly one caller succeeds", ok_k == 1, results_k)
        check("K2. manual vs auto: exactly one caller is skipped", skipped_k == 1, results_k)
        check("K3. manual vs auto: total spend is exactly 1 (never 2)", provider_k.prolong_make_calls == 1, provider_k.prolong_make_calls)

        # ------------------------------------------------------------------
        # GUARD: manual prn:confirm (/proxy_renewal_confirm) must be gated
        # by the SAME _renewal_guard_check every other spend path uses
        # (second-engine-manual-guard fix, independent re-review correction,
        # 20260809) -- before this fix it called _renewal_wrapped_execute
        # directly, bypassing emergency stop / pause / cap / daily budget /
        # min balance entirely.
        # ------------------------------------------------------------------
        lease_guard_id = await _make_lease(tmp_db, provider_proxy_id="PXY-GUARD", host="10.1.0.6", port=52006, expires_at="2026-08-08")
        provider_guard = FakeProvider()
        fake_provider_holder["instance"] = provider_guard

        await storage.proxy_renewal_config_set(emergency_stop=1, db_path=tmp_db)
        out_stop = await confirm_cmd(f"{lease_guard_id} 555")
        import json as _json_mod
        data_stop = _json_mod.loads(out_stop)
        check("GUARD1. manual confirm with emergency_stop=1 is BLOCKED", data_stop.get("ok") is False and data_stop.get("error") == "blocked", data_stop)
        check("GUARD2. manual confirm blocked by emergency_stop spends ZERO", provider_guard.prolong_make_calls == 0, provider_guard.prolong_make_calls)

        await storage.proxy_renewal_config_set(emergency_stop=0, max_amount_per_op="0.01", db_path=tmp_db)
        out_cap = await confirm_cmd(f"{lease_guard_id} 555")
        data_cap = _json_mod.loads(out_cap)
        check("GUARD3. manual confirm over the per-op cap is BLOCKED", data_cap.get("ok") is False and data_cap.get("error") == "blocked", data_cap)
        check("GUARD4. manual confirm blocked by the cap spends ZERO", provider_guard.prolong_make_calls == 0, provider_guard.prolong_make_calls)

        await storage.proxy_renewal_config_set(max_amount_per_op=None, db_path=tmp_db)
        out_ok = await confirm_cmd(f"{lease_guard_id} 555")
        data_ok = _json_mod.loads(out_ok)
        check("GUARD5. manual confirm with guards OFF succeeds normally", data_ok.get("ok") is True, data_ok)
        check("GUARD6. manual confirm with guards OFF spends EXACTLY ONCE", provider_guard.prolong_make_calls == 1, provider_guard.prolong_make_calls)

        # non-'active' lease is treated as lease_not_found, not silently spent.
        lease_disabled_id = await _make_lease(
            tmp_db, provider_proxy_id="PXY-DISABLED", host="10.1.0.7", port=52007,
            expires_at="2026-08-08", status="disabled",
        )
        provider_disabled = FakeProvider()
        fake_provider_holder["instance"] = provider_disabled
        out_disabled = await confirm_cmd(f"{lease_disabled_id} 555")
        data_disabled = _json_mod.loads(out_disabled)
        check("GUARD7. manual confirm on a non-'active' lease is rejected (lease_not_found)",
              data_disabled.get("ok") is False and data_disabled.get("error") == "lease_not_found", data_disabled)
        check("GUARD8. manual confirm on a non-'active' lease spends ZERO", provider_disabled.prolong_make_calls == 0, provider_disabled.prolong_make_calls)

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\n" + "=" * 70)
    print("SELFTEST OK: all checks passed.")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
