# -*- coding: utf-8 -*-
"""proxy_lifecycle.py -- TPilot proxy-lifecycle bidirectional reconciliation
(PROXY LIFECYCLE SYNC 20260721).

Pure decision logic ONLY. This module:
  * imports NOTHING from telethon/main/panel_bot and never touches the DB or
    the network at module level (mirrors the tdata_import package's isolation
    rule) -- every function is a pure transform over plain dicts/values;
  * decides the DESIRED provider auto_renew state ('Y'/'N') for a proxy from
    the health of the managers that use it;
  * given desired + the OBSERVED provider auto_renew (read read-only from
    proxy/list), derives one of four actions
      CONFIRMED_ON / ENABLE_REQUIRED / DISABLE_REQUIRED / CONFIRMED_OFF
    and the durable lifecycle_status that pairs with it;
  * builds the human-facing AdminBot checklist text.

Proxy-Seller exposes NO API to set auto_renew -- it is a web-cabinet checkbox.
So TPilot never mutates the provider here; it only detects a desired-vs-observed
mismatch and tells the owner exactly which cabinet checkbox to flip. The caller
(main.py) performs the storage CAS, audit-event append, and notification.

Hard invariants encoded here:
  * A proxy used by >=1 HEALTHY manager -> desired='Y' (must keep auto-renew);
    one dead account can never flip a proxy shared with a healthy account to
    'N', because desired is computed over the WHOLE healthy-user set.
  * Terminal death is transient-safe: a single failure / warning / limited /
    manual stop is NEVER terminal. Only an authoritative, durable, admin-
    confirmed 'blocked' (or the get_me deleted flag, or a vanished/hard-deleted
    manager -> zero users) counts.
  * An UNKNOWN observed value (provider read missing/unparsed) never produces
    an action and never advances a "confirmed" state -- '' is never treated
    as 'N'.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

# --- action verbs (returned to the caller for notification routing) ---------
# TPILOT-MANAGED RENEWAL 20260721: the architecture changed -- website provider
# Auto-renewal stays OFF permanently and TPilot renews the proxies it needs
# itself (prolong/make). The action is now driven ONLY by whether TPilot
# renewal is desired (>=1 healthy user), NOT by the provider's website flag.
#   desired renewal Y -> CONFIRMED_ON  (TPilot will renew it)
#   desired renewal N -> CONFIRMED_OFF (free/terminal -> not renewed)
# The website `observed` value is now purely informational (provider_observed_
# anomaly) and NEVER produces an actionable ENABLE/DISABLE REQUIRED.
CONFIRMED_ON = "confirmed_on"        # renewal desired -> TPilot renews
CONFIRMED_OFF = "confirmed_off"      # renewal not desired -> free/terminal, drop from pool
NO_ACTION = "no_action"              # desired unknown -> keep current
# RETIRED (kept only so external imports don't break; derive_action NEVER
# returns these anymore -- website auto-renew is expected OFF and we never ask
# the admin to toggle it):
ENABLE_REQUIRED = "enable_required"
DISABLE_REQUIRED = "disable_required"

# --- durable lifecycle_status paired with each action -----------------------
_ACTION_TO_LIFECYCLE = {
    CONFIRMED_ON: "active_confirmed_on",
    ENABLE_REQUIRED: "enable_required",
    DISABLE_REQUIRED: "disable_required",
    CONFIRMED_OFF: "released_off_confirmed",
}

TERMINAL_HEALTH_STATUS = "blocked"  # matches main.py TP_HG_STATUS_BLOCKED / _AUTH_MARKS verdict


def _norm_yn(value: Any) -> str:
    """Normalize an auto_renew value to 'Y'/'N'/'' (unknown). Kept in sync
    with proxy_provider.normalize_auto_renew but duplicated so this module has
    zero imports."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    s = str(value).strip().lower()
    if s in ("y", "yes", "1", "true", "on", "enabled"):
        return "Y"
    if s in ("n", "no", "0", "false", "off", "disabled"):
        return "N"
    return ""


def manager_is_terminal(
    *,
    row_missing: bool = False,
    status: str = "",
    health_status: str = "",
    grace_elapsed: bool = False,
    admin_confirmed: bool = False,
    incident_open: bool = False,
    me_deleted: bool = False,
) -> bool:
    """Authoritative, transient-safe terminal-death verdict for ONE manager.

    Returns True ONLY for a permanently-dead account:
      * row_missing        -> the manager was hard-deleted (auto-terminal);
      * status == 'archived' -> admin soft-removed it (auto-terminal);
      * me_deleted          -> get_me returned deleted=True (auto-terminal);
      * health 'blocked' AND grace_elapsed AND (admin_confirmed OR incident_open)
        -> durable auth death confirmed past the grace window.

    Explicitly NOT terminal (returns False): a freshly-'blocked' status that
    hasn't cleared the grace window, warning/limited health, a manual stop
    (is_enabled=0 / manual_stopped=1), a single failed probe, proxy/network
    errors, FloodWait. Callers must map those to 'still a healthy user'.
    """
    if row_missing:
        return True
    if str(status or "").strip().lower() == "archived":
        return True
    if me_deleted:
        return True
    if str(health_status or "").strip().lower() == TERMINAL_HEALTH_STATUS and grace_elapsed and (admin_confirmed or incident_open):
        return True
    return False


def count_healthy_users(users: Sequence[Dict[str, Any]]) -> int:
    """Count managers that count as HEALTHY users of a proxy. Each user dict
    must carry a precomputed boolean 'terminal' (from manager_is_terminal).
    A user with terminal=False is healthy for proxy-need purposes even if it
    is transiently degraded/stopped."""
    return sum(1 for u in (users or []) if not bool(u.get("terminal")))


def compute_desired(users: Sequence[Dict[str, Any]]) -> str:
    """'Y' if >=1 healthy user uses the proxy, else 'N'. Shared-proxy
    protection is structural: desired is a property of the WHOLE healthy-user
    set, so one dead account cannot flip a proxy still used by a healthy
    account. (Historic name; compute_renewal_desired is the clearer alias.)"""
    return "Y" if count_healthy_users(users) >= 1 else "N"


def compute_renewal_desired(users: Sequence[Dict[str, Any]]) -> str:
    """TPILOT-MANAGED RENEWAL 20260721: 'Y' if TPilot should renew this proxy
    (>=1 healthy user -- healthy includes a manually-stopped/paused manager,
    which is non-terminal), else 'N' (free/terminal). This drives the local
    proxy_leases.auto_renew_enabled flag that the renewal scheduler gates on.
    Identical rule to compute_desired; separate name to make the call sites
    self-documenting under the new architecture."""
    return compute_desired(users)


def provider_observed_anomaly(observed: Any) -> bool:
    """Informational ONLY: website provider Auto-renewal is expected OFF for
    every proxy. observed == 'Y' is therefore an anomaly worth surfacing (the
    website might auto-charge), but it is NEVER actioned as ENABLE/DISABLE
    REQUIRED -- TPilot does not toggle the website. Unknown/'' is not an
    anomaly (just an unread field)."""
    return _norm_yn(observed) == "Y"


def derive_action(desired: Any, observed: Any) -> str:
    """TPILOT-MANAGED RENEWAL 20260721: renewal-desire-centric. The provider
    website `observed` value no longer drives the action (it is informational
    only -- see provider_observed_anomaly). Returns CONFIRMED_ON when TPilot
    renewal is desired, CONFIRMED_OFF when it is not, NO_ACTION when desire is
    unknown. NEVER returns ENABLE_REQUIRED/DISABLE_REQUIRED anymore."""
    d = _norm_yn(desired)
    if d == "Y":
        return CONFIRMED_ON
    if d == "N":
        return CONFIRMED_OFF
    return NO_ACTION


def target_lifecycle_status(desired: Any, observed: Any = None) -> Optional[str]:
    """The durable lifecycle_status a lease should hold for this desire, or
    None when the action is NO_ACTION (keep current). `observed` is accepted
    for backward-compat but no longer affects the result."""
    return _ACTION_TO_LIFECYCLE.get(derive_action(desired, observed))


def reconcile_lease(lease: Dict[str, Any], desired: Any, observed: Any) -> Dict[str, Any]:
    """Pure reconciliation plan for one lease. The caller performs the storage
    CAS (proxy_lease_set_lifecycle), the audit append, and any notification.

    Returns:
      action                 : one of the four verbs (or NO_ACTION);
      current_lifecycle_status / target_lifecycle_status;
      changed                : True when a CAS transition is needed;
      needs_admin_action     : True for ENABLE_REQUIRED / DISABLE_REQUIRED;
      release                : True when this reaches CONFIRMED_OFF (drop from
                               active pool + record released_at/reason);
      desired / observed     : the normalized values used.
    """
    d = _norm_yn(desired)
    o = _norm_yn(observed)
    action = derive_action(d, o)
    target = _ACTION_TO_LIFECYCLE.get(action)
    current = str((lease or {}).get("lifecycle_status") or "")
    return {
        "action": action,
        "desired": d,
        "observed": o,
        "current_lifecycle_status": current,
        "target_lifecycle_status": target,
        "changed": bool(target) and target != current,
        # TPILOT-MANAGED RENEWAL 20260721: there is no admin cabinet action
        # anymore (website auto-renew is never toggled). Always False; kept in
        # the dict for backward-compat with any caller that reads the key.
        "needs_admin_action": False,
        "release": action == CONFIRMED_OFF,
        # Renewal desire drives the local auto_renew_enabled flag (1 for Y).
        "renewal_desired": d,
        # Informational-only anomaly: website unexpectedly ON.
        "provider_anomaly": provider_observed_anomaly(o),
    }


# --- human-facing AdminBot text ---------------------------------------------

def _lease_label(lease: Dict[str, Any]) -> str:
    """Safe one-line proxy identity for a checklist: provider_proxy_id + host:port.
    NEVER includes login/password."""
    lease = lease or {}
    pid = str(lease.get("provider_proxy_id") or "?")
    host = str(lease.get("host") or "?")
    port = str(lease.get("port") or "?")
    return f"#{pid} {host}:{port}"


def lease_label(lease: Dict[str, Any]) -> str:
    """Public alias of _lease_label for callers outside this module
    (main.py/panel_bot.py) -- credential-free proxy identity line."""
    return _lease_label(lease)


_ACTION_TITLE = {
    CONFIRMED_ON: "✅ Продление включено (TPilot)",
    CONFIRMED_OFF: "ℹ️ Прокси не продлевается",
}


def action_title(action: str) -> str:
    return _ACTION_TITLE.get(action, "Статус прокси")


def build_checklist_text(action: str, leases: Sequence[Dict[str, Any]]) -> str:
    """RETIRED (TPILOT-MANAGED RENEWAL 20260721): the cabinet-checklist
    ENABLE/DISABLE REQUIRED workflow is gone -- TPilot never asks the admin to
    toggle website Auto-renewal (it stays OFF; TPilot renews via prolong/make).
    Kept only so any lingering import doesn't break; returns a neutral,
    credential-free informational list."""
    leases = list(leases or [])
    body = "\n".join(f"• {_lease_label(l)}" for l in leases)
    return f"Прокси:\n\n{body}" if body else "Прокси: —"


def build_confirmation_text(action: str, lease: Dict[str, Any]) -> str:
    """Single-lease informational body for a CONFIRMED_ON/CONFIRMED_OFF
    transition. Credential-free. TPILOT-MANAGED RENEWAL 20260721: reworded --
    website Auto-renewal is OFF everywhere, so money was never being
    auto-charged; the OLD "деньги больше не списываются" wording was false. A
    free proxy simply expires by its term; a needed proxy is renewed by TPilot
    itself (prolong/make), NOT the website."""
    label = _lease_label(lease)
    if action == CONFIRMED_OFF:
        return (
            f"{label}\n\n"
            "Прокси не продлевается (свободен) — истечёт по сроку, снят с "
            "активного пула TPilot. История сохранена."
        )
    if action == CONFIRMED_ON:
        return (
            f"{label}\n\n"
            "Продление включено: TPilot продлит этот прокси сам (prolong) до "
            "истечения. Автопродление на сайте провайдера не используется."
        )
    return label


def build_cleanup_plan(
    target_provider_proxy_ids: Sequence[Any],
    leases_by_provider_id: Dict[str, Dict[str, Any]],
    desired_by_provider_id: Dict[str, str],
) -> Dict[str, Any]:
    """One-time cleanup plan for an explicit allowlist of provider_proxy_ids
    (e.g. the 8 confirmed-unused proxies). Pure: the caller supplies the freshly
    recomputed `desired` per id (from the live usage map). A target is ABORTED
    if it recomputed to desired='Y' (it became used) or has no known lease;
    otherwise it is included for a DISABLE_REQUIRED action. Never selects a
    used proxy."""
    include: List[Dict[str, Any]] = []
    aborted: List[Dict[str, Any]] = []
    for pid in target_provider_proxy_ids or []:
        key = str(pid)
        lease = leases_by_provider_id.get(key)
        desired = _norm_yn(desired_by_provider_id.get(key))
        if not lease:
            aborted.append({"provider_proxy_id": key, "reason": "lease_not_found"})
            continue
        if desired == "Y":
            aborted.append({"provider_proxy_id": key, "reason": "now_in_use"})
            continue
        include.append(lease)
    return {"include": include, "aborted": aborted}
