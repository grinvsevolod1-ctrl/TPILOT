# -*- coding: utf-8 -*-
"""tools/proxy_lifecycle_logic_selftest.py -- offline self-test for the pure
decision logic in proxy_lifecycle.py (PROXY LIFECYCLE SYNC 20260721).

No DB, no network, no provider. Proves the terminal-state policy is transient-
safe, the desired computation implements shared-proxy protection, the four-way
bidirectional derive_action table is correct in BOTH directions, unknown
observed never acts, and the cleanup-plan builder aborts now-used targets.

    python tools\\proxy_lifecycle_logic_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import proxy_lifecycle as pl

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def main() -> int:
    # --- terminal policy (transient-safe) ---------------------------------
    check("hard-deleted manager (row missing) is terminal", pl.manager_is_terminal(row_missing=True) is True)
    check("archived manager is terminal", pl.manager_is_terminal(status="archived") is True)
    check("get_me deleted flag is terminal", pl.manager_is_terminal(me_deleted=True) is True)
    check("blocked + grace elapsed + admin confirmed is terminal",
          pl.manager_is_terminal(health_status="blocked", grace_elapsed=True, admin_confirmed=True) is True)
    check("blocked + grace elapsed + open incident is terminal",
          pl.manager_is_terminal(health_status="blocked", grace_elapsed=True, incident_open=True) is True)
    # transient-safe negatives
    check("freshly blocked (no grace) is NOT terminal", pl.manager_is_terminal(health_status="blocked", grace_elapsed=False, admin_confirmed=True) is False)
    check("blocked past grace but NOT confirmed/incident is NOT terminal", pl.manager_is_terminal(health_status="blocked", grace_elapsed=True) is False)
    check("warning health is NOT terminal", pl.manager_is_terminal(health_status="warning", grace_elapsed=True, admin_confirmed=True) is False)
    check("limited health is NOT terminal", pl.manager_is_terminal(health_status="limited", grace_elapsed=True, admin_confirmed=True) is False)
    check("ok health / manual stop is NOT terminal", pl.manager_is_terminal(status="active", health_status="ok") is False)

    # --- desired computation + shared-proxy protection --------------------
    healthy = {"terminal": False}
    dead = {"terminal": True}
    check("no users -> desired N", pl.compute_desired([]) == "N")
    check("one healthy user -> desired Y", pl.compute_desired([healthy]) == "Y")
    check("all users dead -> desired N", pl.compute_desired([dead, dead]) == "N")
    check("SHARED proxy: one dead + one healthy -> desired STAYS Y (protection)",
          pl.compute_desired([dead, healthy]) == "Y")
    check("count_healthy_users ignores terminal users", pl.count_healthy_users([dead, healthy, dead]) == 1)

    # --- compute_renewal_desired (alias) + provider anomaly (informational) --
    check("compute_renewal_desired mirrors compute_desired (>=1 healthy -> Y)", pl.compute_renewal_desired([healthy]) == "Y")
    check("compute_renewal_desired: shared one-dead-one-healthy stays Y", pl.compute_renewal_desired([dead, healthy]) == "Y")
    check("compute_renewal_desired: all dead -> N", pl.compute_renewal_desired([dead, dead]) == "N")
    check("provider_observed_anomaly: website ON is an anomaly (informational)", pl.provider_observed_anomaly("Y") is True)
    check("provider_observed_anomaly: website OFF is expected, not an anomaly", pl.provider_observed_anomaly("N") is False)
    check("provider_observed_anomaly: unknown/'' is not an anomaly", pl.provider_observed_anomaly("") is False)

    # --- derive_action: TPILOT-MANAGED RENEWAL 20260721 renewal-centric -----
    # Website observed no longer drives the action -- only renewal desire does.
    check("renewal desired Y -> CONFIRMED_ON regardless of observed N", pl.derive_action("Y", "N") == pl.CONFIRMED_ON)
    check("renewal desired Y -> CONFIRMED_ON regardless of observed Y", pl.derive_action("Y", "Y") == pl.CONFIRMED_ON)
    check("renewal desired N -> CONFIRMED_OFF regardless of observed Y", pl.derive_action("N", "Y") == pl.CONFIRMED_OFF)
    check("renewal desired N -> CONFIRMED_OFF regardless of observed N", pl.derive_action("N", "N") == pl.CONFIRMED_OFF)
    check("derive_action NEVER returns ENABLE_REQUIRED anymore", pl.derive_action("Y", "N") != pl.ENABLE_REQUIRED)
    check("derive_action NEVER returns DISABLE_REQUIRED anymore", pl.derive_action("N", "Y") != pl.DISABLE_REQUIRED)
    check("unknown desired -> NO_ACTION", pl.derive_action("", "N") == pl.NO_ACTION)

    # --- target lifecycle_status mapping (renewal-centric) ------------------
    check("desired Y -> active_confirmed_on", pl.target_lifecycle_status("Y") == "active_confirmed_on")
    check("desired N -> released_off_confirmed", pl.target_lifecycle_status("N") == "released_off_confirmed")
    check("unknown desired -> target None (keep current)", pl.target_lifecycle_status("") is None)

    # --- reconcile_lease plan (renewal-centric) ----------------------------
    lease_on = {"lifecycle_status": "active_confirmed_on", "provider_proxy_id": "P1", "host": "1.2.3.4", "port": 50101}
    # healthy user, website observed OFF (the NEW normal) -> CONFIRMED_ON, no admin action, no release
    plan = pl.reconcile_lease(lease_on, "Y", "N")
    check("reconcile: healthy + website OFF -> CONFIRMED_ON (renewal by TPilot)", plan["action"] == pl.CONFIRMED_ON)
    check("reconcile: healthy + website OFF is NOT a change (already active_confirmed_on)", plan["changed"] is False)
    check("reconcile: NEVER needs admin action anymore", plan["needs_admin_action"] is False)
    check("reconcile: renewal_desired surfaced as Y", plan["renewal_desired"] == "Y")
    # free/terminal (no healthy user) -> CONFIRMED_OFF release
    plan2 = pl.reconcile_lease(lease_on, "N", "N")
    check("reconcile: no healthy user -> CONFIRMED_OFF release", plan2["action"] == pl.CONFIRMED_OFF and plan2["release"] is True)
    check("reconcile: CONFIRMED_OFF renewal_desired is N", plan2["renewal_desired"] == "N")
    # website unexpectedly ON on a free proxy -> still CONFIRMED_OFF, but anomaly flagged (informational)
    plan3 = pl.reconcile_lease({"lifecycle_status": "disable_required"}, "N", "Y")
    check("reconcile: free + website anomaly ON -> still CONFIRMED_OFF (never DISABLE REQUIRED)", plan3["action"] == pl.CONFIRMED_OFF)
    check("reconcile: website ON surfaced as an informational anomaly", plan3["provider_anomaly"] is True)
    check("reconcile: anomaly still needs no admin toggle action", plan3["needs_admin_action"] is False)
    # unknown desired -> no change, no action
    plan5 = pl.reconcile_lease(lease_on, "", "N")
    check("reconcile: unknown desired -> NO_ACTION, no change", plan5["action"] == pl.NO_ACTION and plan5["changed"] is False)

    # --- confirmation text: reworded away from the false 'деньги...' wording -
    leasec = {"provider_proxy_id": "36701027", "host": "86.38.177.250", "port": 50101, "login": "SECRET_L", "password": "SECRET_P"}
    txt_off = pl.build_confirmation_text(pl.CONFIRMED_OFF, leasec)
    check("CONFIRMED_OFF text lists provider_proxy_id + host:port", "36701027" in txt_off and "86.38.177.250:50101" in txt_off)
    check("CONFIRMED_OFF text no longer claims 'деньги больше не списываются'", "деньги больше не списываются" not in txt_off)
    check("CONFIRMED_OFF text says the proxy is not renewed / expires by term", "не продлевается" in txt_off)
    check("CONFIRMED_OFF text never contains proxy login/password", "SECRET_L" not in txt_off and "SECRET_P" not in txt_off)
    txt_on = pl.build_confirmation_text(pl.CONFIRMED_ON, leasec)
    check("CONFIRMED_ON text says TPilot renews it (not website)", "TPilot" in txt_on and "SECRET_P" not in txt_on)
    # build_checklist_text is retired (returns a neutral credential-free list); no ENABLE wording.
    txt_retired = pl.build_checklist_text(pl.CONFIRMED_OFF, [leasec])
    check("retired build_checklist_text is credential-free + has no ENABLE wording",
          "SECRET_L" not in txt_retired and "SECRET_P" not in txt_retired and "Включите" not in txt_retired)

    # --- cleanup plan (aborts now-used targets, never a used proxy) -------
    leases_by_id = {
        "P-UNUSED-A": {"provider_proxy_id": "P-UNUSED-A", "host": "1.1.1.1", "port": 50101},
        "P-UNUSED-B": {"provider_proxy_id": "P-UNUSED-B", "host": "2.2.2.2", "port": 50101},
        "P-NOWUSED": {"provider_proxy_id": "P-NOWUSED", "host": "3.3.3.3", "port": 50101},
    }
    desired_by_id = {"P-UNUSED-A": "N", "P-UNUSED-B": "N", "P-NOWUSED": "Y", "P-MISSING": "N"}
    plan_c = pl.build_cleanup_plan(["P-UNUSED-A", "P-UNUSED-B", "P-NOWUSED", "P-MISSING"], leases_by_id, desired_by_id)
    inc_ids = {l["provider_proxy_id"] for l in plan_c["include"]}
    abort_reasons = {a["provider_proxy_id"]: a["reason"] for a in plan_c["aborted"]}
    check("cleanup includes the two still-unused targets", inc_ids == {"P-UNUSED-A", "P-UNUSED-B"}, str(inc_ids))
    check("cleanup aborts a target that became used (desired recomputed Y)", abort_reasons.get("P-NOWUSED") == "now_in_use")
    check("cleanup aborts a target with no known lease", abort_reasons.get("P-MISSING") == "lease_not_found")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PROXY LIFECYCLE LOGIC SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
