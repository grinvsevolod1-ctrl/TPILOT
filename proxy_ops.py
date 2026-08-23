"""Proxy buy / renewal / pool subsystem, extracted from main.py (R2 stage 1).

Controller-side only: PanelBot is UI + panel_commands submission -- all
provider HTTP calls, storage writes, and manager-field application happen
here, never in panel_bot.py.

SPEND SAFETY (AGENTS.md section 4): allow_spend=True appears as a real call
argument in EXACTLY 2 places project-wide, and both live in this module:
  1. make_ipv4(...) inside _handle_manager_proxy_buy_confirm_command --
     only reachable via the "/manager_proxy_buy_confirm <key>" panel command,
     which PanelBot submits only after the admin explicitly confirms a
     no-spend calc_ipv4() price preview.
  2. prolong_make(...) inside _prenew_execute_renewal -- the renewal
     executor, likewise behind an explicit admin confirm (or the autorenew
     gates in _prenew_autorenew_gates_ok).
_pbuy_provider() always constructs the provider with allow_spend=False;
spend calls opt in per-call. Raw proxy passwords never enter
panel_commands/result_text -- only a has_password boolean.

Late-bound main.py dependencies (notification, renewal-guard and registry
helpers) are injected via bind(globals()) at main.py module bottom; every
function here resolves them at call time, exactly as it did pre-extraction.

This module is import-safe: no Telethon, no DB access, no network at import
time.
"""
from __future__ import annotations

import asyncio
import json as _pbuy_json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import proxy_parser
from manager_registry import (
    build_manager_paths,
    ensure_manager_dirs,
    get_manager_row_from_db_sync,
    mask_phone,
    normalize_manager_key as registry_normalize_manager_key,
    validate_manager_key,
)
from storage import (  # noqa: E402
    DB_PATH,
    init_db,
    get_setting,
    get_setting_from_db,
    set_setting,
    manager_add,
    manager_clear_expired_onboarding,
    manager_create_auth_session,
    manager_delete_onboarding,
    manager_delete_onboarding_by_key,
    manager_get,
    manager_get_onboarding,
    manager_has_auth_session,
    manager_list_pending,
    manager_list_rows,
    manager_rename,
    manager_save_onboarding,
    manager_set_fields,
    manager_soft_remove,
    manager_sync_telegram_profile_in_db,
    manager_update_profile_in_db,
    manager_bot_access_ensure_sync,
)


# ---------------------------------------------------------------------------
# Late-bound main.py dependencies. main.py calls bind(globals()) once at
# module bottom (after every dependency is defined, before the event loop
# starts). Until then each slot is a loud placeholder, never a silent None.
_BIND_DEPS = (
    'TPILOT_DB_PATH',
    '_create_panel_notification',
    '_kyiv_now',
    '_manager_proxy_info_text',
    '_now_utc_iso',
    '_plc_reconcile_all',
    '_renewal_calc_preview',
    '_renewal_guard_check',
    '_renewal_kyiv_today_str',
    '_renewal_wrapped_execute',
    '_tp_utc_now',
    '_tpag_classify_error_reason',
    '_tpag_registry_get',
    '_tpag_registry_set_fields',
    '_tpag_run_guard',
    '_tpag_v4_proxy_geo_via_socks',
)


def _unbound(name):
    def _raise(*a, **k):
        raise RuntimeError(f"proxy_ops dependency {name!r} used before bind()")
    return _raise


for _dep in _BIND_DEPS:
    globals()[_dep] = _unbound(_dep)
del _dep


def bind(ns):
    """Inject late dependencies from main.py globals. Call once at startup."""
    g = globals()
    missing = [n for n in _BIND_DEPS if n not in ns]
    if missing:
        raise RuntimeError(f"proxy_ops.bind: missing deps: {missing}")
    for n in _BIND_DEPS:
        g[n] = ns[n]


PROXY_SELLER_API_KEY = (os.getenv("PROXY_SELLER_API_KEY") or "").strip()


_PBUY_COUNTRY_ALPHA3 = "DEU"


_PBUY_COUNTRY_NAME = "Germany"


_PBUY_PERIOD_ID = "1m"          # preferred period id (also the admin-facing display label)


_PBUY_PERIOD_NAME = "1 month"   # preferred period name -- name-fallback + admin-facing display label


_PBUY_PAYMENT_ID = 1


_PBUY_QUANTITY = 1


_PBUY_PROXY_TYPE = "ipv4"


_PBUY_PROVISION_POLL_ATTEMPTS = 60


_PBUY_PROVISION_POLL_INTERVAL_SEC = 5.0


_PBUY_RECOVER_POLL_ATTEMPTS = 6


_PBUY_RECOVER_POLL_INTERVAL_SEC = 5.0


def _pbuy_provider():
    """Return a ProxySellerProvider (allow_spend is always False at the
    provider/constructor level here -- the confirm handler passes
    allow_spend=True per-call only) or None if no API key is configured.
    Never raises; never makes a network call itself."""
    if not PROXY_SELLER_API_KEY:
        return None
    try:
        from proxy_provider import ProxySellerProvider
        return ProxySellerProvider(PROXY_SELLER_API_KEY, allow_spend=False)
    except Exception:
        return None


async def _pbuy_call(fn, *args, **kwargs):
    """Run a synchronous proxy_provider.py method off the event loop
    thread, per that module's own documented executor-offload contract."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


def _pbuy_safe_error(exc: Exception) -> str:
    """proxy_provider.py's own exceptions (ProxyProviderError/
    SpendGuardError) are already scrubbed of the API key and submitted
    credentials -- pass those through as-is. Guard against any other
    exception type leaking something unexpected (e.g. a raw stdlib
    exception) by never including its str() in the admin-facing message."""
    try:
        from proxy_provider import ProxyProviderError
        if isinstance(exc, ProxyProviderError):
            return str(exc)
    except Exception:
        pass
    return f"{type(exc).__name__}: непредвиденная ошибка провайдера"


def _prenew_classify_provider_error(*, error_code: str = "", message: str = "", kind: Optional[str] = None, post_spend: bool = False) -> Dict[str, str]:
    """Map a provider/spend-tail failure to one of a small set of RU
    categories. NEVER echoes raw provider structure (errors[]/code/
    customData/dict-repr) into its output -- message/kind are inspected
    ONLY to pick a category; the returned reason/action always come from
    the fixed table below. `kind` is proxy_provider.ProxyProviderError.kind
    (transport/provider_errors/http/response_shape/auth) when available --
    a coarse hint used before falling back to text sniffing on the (already
    API-key-scrubbed) message. `error_code` is the short internal code the
    renewal call sites already produce (not_configured/period_not_found/
    unverified/...) and short-circuits to an unambiguous category when it
    already pins one. Never raises -- anything unrecognized falls back to
    'unknown'.

    `post_spend` (BLOCKER-1 fix, retry-policy correction 20260809; NARROWED
    by the money-safety correction of the same day -- see
    _renewal_wrapped_execute's docstring): the fixed action text for
    network_timeout/rate_limit/provider_unavailable below claims "TPilot
    will retry automatically" -- true when the failure happened BEFORE
    prolong_make was ever called (reference_list/period lookup in
    _prenew_preflight_check: no spend attempted, freely retryable), but
    FALSE when the SAME kind of transport/HTTP failure happens AFTER
    prolong_make was called (_prenew_execute_renewal's own except-block).
    ANY failure classified after prolong_make has been called -- including
    an explicit provider business-rule rejection (PROVIDER_ERRORS/errors[])
    or an auth reject (401/403) -- is spend-AMBIGUOUS and never auto-
    retried: proxy_provider.py's own _request() checks errors[]/envelope-
    status BEFORE the HTTP status code, so the SAME kind/category can be
    produced by an HTTP 5xx/429 response that also carries a JSON errors[]
    body (the provider may already have processed the spend before failing
    to report success cleanly) -- kind alone is not sufficient structural
    proof that no charge occurred. Callers in a post-spend context (only
    _prenew_execute_renewal's prolong_make except-block today) must pass
    post_spend=True so the admin is told the truth instead of a promise
    that can't be kept. Categories with proof-of-no-spend action text
    (ip_not_found/insufficient_balance/provider_auth/invalid_period/
    not_configured) never claimed auto-retry to begin with and are
    unaffected by this flag -- but note their action text is UI-only; it
    does NOT imply the idempotency_key is freed (see
    _renewal_wrapped_execute)."""
    categories = {
        "ip_not_found": {
            "reason": "Провайдер больше не находит этот прокси.",
            "action": "Проверьте прокси и при необходимости замените его.",
        },
        "insufficient_balance": {
            "reason": "Недостаточно средств на балансе Proxy-Seller.",
            "action": "Пополните баланс Proxy-Seller.",
        },
        "provider_auth": {
            "reason": "Proxy-Seller отклонил доступ к API.",
            "action": "Проверьте API-ключ.",
        },
        "rate_limit": {
            "reason": "Proxy-Seller временно ограничил запросы.",
            "action": "Ничего — TPilot повторит попытку автоматически.",
        },
        "network_timeout": {
            "reason": "Proxy-Seller временно недоступен.",
            "action": "Ничего — TPilot повторит попытку автоматически.",
        },
        "invalid_period": {
            "reason": "Период продления недоступен у провайдера.",
            "action": "Проверьте настройки периода продления.",
        },
        "provider_unavailable": {
            "reason": "Proxy-Seller временно недоступен.",
            "action": "Ничего — TPilot повторит попытку автоматически.",
        },
        "not_configured": {
            "reason": "Не настроен API-ключ Proxy-Seller.",
            "action": "Обратитесь к администратору TPilot.",
        },
        "unverified": {
            "reason": "Новый срок прокси не подтверждён.",
            "action": "Синхронизируйте пул и проверьте прокси.",
        },
        "unknown": {
            "reason": "Не удалось определить причину ошибки провайдера.",
            "action": "Откройте карточку прокси и проверьте вручную.",
        },
    }
    text = str(message or "").lower()
    code = str(error_code or "").strip().lower()

    category = "unknown"
    if code == "not_configured":
        category = "not_configured"
    elif code == "unverified":
        category = "unverified"
    elif code == "period_not_found":
        category = "invalid_period"
    elif kind == "auth" or any(t in text for t in ("unauthorized", " 401", " 403", "access denied", "invalid key", "api key rejected")):
        category = "provider_auth"
    elif any(t in text for t in ("ip not found", "proxy not found", "не найден")):
        category = "ip_not_found"
    elif any(t in text for t in ("insufficient", "not enough", "balance too low", "недостаточно", "низкий баланс")):
        category = "insufficient_balance"
    elif any(t in text for t in ("429", "too many requests", "rate limit")):
        category = "rate_limit"
    elif "period" in text:
        category = "invalid_period"
    elif kind == "transport" or any(t in text for t in ("timeout", "timed out", "network error", "connection")):
        category = "network_timeout"
    elif kind == "http" or any(t in text for t in ("http 5", "provider unavailable", "bad gateway", "service unavailable")):
        category = "provider_unavailable"

    tpl = categories.get(category) or categories["unknown"]
    action = tpl["action"]
    if post_spend and category in ("network_timeout", "rate_limit", "provider_unavailable"):
        # BLOCKER-1 fix: this failure happened AFTER prolong_make was
        # called -- spend is ambiguous, never auto-retried. Never promise
        # a retry this project won't perform.
        action = "Повторное списание автоматически не выполняется, пока результат предыдущей попытки не подтверждён."
    return {"category": category, "reason": tpl["reason"], "action": action}


def _pbuy_custom_target_name(manager_key: str) -> str:
    """Build a safe, non-secret customTargetName for order/calc + order/make
    -- Proxy-Seller rejects both endpoints with errors[] "Set
    [customTargetName]" when this field is empty. Contains only a
    sanitized manager_key; never an API key, proxy password, phone
    number, session path, or token."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(manager_key or "").strip())
    safe = re.sub(r"_+", "_", safe).strip("_-")
    if not safe:
        safe = "manager"
    return f"TPilot_{safe}"[:64]


async def _handle_manager_proxy_buy_calc_command(args: str) -> str:
    """No-spend price preview (reference_list + calc_ipv4 + balance only).
    Returns a JSON string -- panel_commands has no result_json column, so
    structured data travels in result_text (same convention _panel_
    execute_command_text already uses for other JSON-returning commands)."""
    key = registry_normalize_manager_key(str(args or "").strip().split()[0] if str(args or "").strip() else "")
    if not key:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой manager_key"}, ensure_ascii=False)

    row = await _tpag_registry_get(key)
    if not row or str(row.get("status") or "") == "archived":
        return _pbuy_json.dumps({"ok": False, "error": "manager_not_found", "message": f"Менеджер не найден: {key}"}, ensure_ascii=False)

    provider = _pbuy_provider()
    if provider is None:
        return _pbuy_json.dumps({"ok": False, "error": "not_configured", "message": "Proxy-Seller API key не настроен"}, ensure_ascii=False)

    from proxy_provider import find_country_id, find_period_id, extract_price_info

    try:
        ref = await _pbuy_call(provider.reference_list, _PBUY_PROXY_TYPE)
    except Exception as e:
        return _pbuy_json.dumps({"ok": False, "error": "reference_list_failed", "message": _pbuy_safe_error(e)}, ensure_ascii=False)

    country_id = find_country_id(ref, alpha3=_PBUY_COUNTRY_ALPHA3, name=_PBUY_COUNTRY_NAME)
    if country_id is None:
        return _pbuy_json.dumps({
            "ok": False, "error": "country_not_found",
            "message": f"Страна {_PBUY_COUNTRY_NAME} ({_PBUY_COUNTRY_ALPHA3}) не найдена в справочнике провайдера.",
        }, ensure_ascii=False)

    period_id = find_period_id(ref, preferred_id=_PBUY_PERIOD_ID, preferred_name=_PBUY_PERIOD_NAME)
    if period_id is None:
        return _pbuy_json.dumps({
            "ok": False, "error": "period_not_found",
            "message": f"Период {_PBUY_PERIOD_NAME} ({_PBUY_PERIOD_ID}) не найден в справочнике провайдера.",
        }, ensure_ascii=False)

    custom_target_name = _pbuy_custom_target_name(key)
    try:
        calc = await _pbuy_call(
            provider.calc_ipv4,
            country_id, period_id, _PBUY_QUANTITY,
            "", "", custom_target_name, _PBUY_PAYMENT_ID,
        )
    except Exception as e:
        return _pbuy_json.dumps({"ok": False, "error": "calc_failed", "message": _pbuy_safe_error(e)}, ensure_ascii=False)

    try:
        bal = await _pbuy_call(provider.balance)
    except Exception:
        bal = {}

    price_info = extract_price_info(calc)
    return _pbuy_json.dumps({
        "ok": True,
        "manager_key": key,
        "display_name": str(row.get("display_name") or "").strip() or key,
        "country_id": country_id,
        "country_name": _PBUY_COUNTRY_NAME,
        "country_alpha3": _PBUY_COUNTRY_ALPHA3,
        "period_id": _PBUY_PERIOD_ID,
        "quantity": _PBUY_QUANTITY,
        "price": price_info.get("price"),
        "total": price_info.get("total"),
        "currency": price_info.get("currency"),
        "balance": bal.get("summ") if isinstance(bal, dict) else None,
    }, ensure_ascii=False)


async def _pbuy_poll_for_proxy(provider, order_id, *, attempts: int, interval_sec: float) -> Optional[Dict[str, Any]]:
    """Poll list_proxies(order_id) up to `attempts` times, `interval_sec`
    apart, then fall back to the raw download endpoint (parsed through the
    central Stage 1 proxy_parser rather than trusting provider field
    names). Returns a proxy_entry dict (host/port/login/password/
    provider_proxy_id/expires_at) or None if the provider still hasn't
    provisioned anything. Read-only provider calls only -- never spends,
    never calls order/make. Shared by buy-confirm (right after order/make)
    and buy-recover (re-checking an already-placed order)."""
    from proxy_provider import extract_proxy_entries, extract_download_lines

    proxy_entry: Optional[Dict[str, Any]] = None
    for _attempt in range(attempts):
        try:
            lst = await _pbuy_call(provider.list_proxies, _PBUY_PROXY_TYPE, order_id)
        except Exception:
            lst = None
        entries = extract_proxy_entries(lst) if lst is not None else []
        if entries:
            proxy_entry = entries[0]
            break
        if _attempt < attempts - 1:
            await asyncio.sleep(interval_sec)

    if proxy_entry is None:
        try:
            dl = await _pbuy_call(provider.download_proxies, _PBUY_PROXY_TYPE, "socks5", "txt")
        except Exception:
            dl = None
        if dl is not None:
            for line in extract_download_lines(dl):
                parsed_line = proxy_parser.parse_proxy_line(line)
                if parsed_line:
                    proxy_entry = {
                        "host": parsed_line.get("host"),
                        "port": parsed_line.get("port"),
                        "login": parsed_line.get("login"),
                        "password": parsed_line.get("password"),
                        "provider_proxy_id": None,
                        "expires_at": None,
                        "order_id": order_id,
                    }
                    break

    return proxy_entry


async def _pbuy_apply_lease_to_manager(
    lease: Dict[str, Any], manager_key: str, *, source: str, link_lease: bool = True
) -> Dict[str, Any]:
    """Shared tail: link an EXISTING lease (lease["id"] must already exist
    in proxy_leases) to a manager, apply the manager's proxy fields, and
    run the guard. Used by:
    - _pbuy_finalize_lease (Stage 4 buy/recover, right after creating a
      brand-new lease row)
    - /proxy_pool_assign (Stage 6 P3, linking/reassigning an EXISTING pool
      lease)
    Never spends, never calls the provider, never creates/deletes a lease
    row itself -- purely storage writes + one guard check. Returns a plain
    dict (caller does the JSON encoding); never includes the raw
    password.

    PROXY USAGE DETECTION 20260711: link_lease=False is the multi-assign
    path (see _handle_proxy_pool_assign_command's 'multi' token) -- the
    SAME host/port/login/password fields are still applied to this
    manager's own proxy_* columns, but proxy_leases.manager_key /
    managers.proxy_lease_id are left untouched, so the lease keeps its
    original owner as the pool's single tracked owner. The new manager's
    usage is still correctly detected afterwards via their own
    proxy_host/proxy_port/proxy_enabled fields (same mechanism as any
    external proxy). Every existing caller omits this kwarg and gets the
    exact original link-always behavior."""
    from storage import (
        proxy_lease_assign_to_manager as _pbuy_lease_assign,
        proxy_lease_get as _pbuy_lease_get,
        proxy_lease_to_manager_proxy_fields as _pbuy_lease_to_fields,
    )

    lease_id = lease.get("id")
    if link_lease:
        await _pbuy_lease_assign(lease_id, manager_key, db_path=TPILOT_DB_PATH)
    lease2 = await _pbuy_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    fields = _pbuy_lease_to_fields(lease2 or {})
    # Consistent with the existing manual SOCKS5 flow's semantics -- an
    # actively-assigned pool proxy is required, not optional/bypassable.
    fields["proxy_required"] = 1
    await _tpag_registry_set_fields(manager_key, **fields)

    try:
        guard_ok, guard_text = await _tpag_run_guard(manager_key, source=source, force=True)
    except Exception as e:
        guard_ok, guard_text = False, f"Проверка не выполнена: {_pbuy_safe_error(e)}"

    row2 = await _tpag_registry_get(manager_key) or {}
    try:
        info_text = _manager_proxy_info_text(row2)
    except Exception:
        info_text = ""

    return {
        "ok": True,
        "manager_key": manager_key,
        "display_name": str(row2.get("display_name") or "").strip() or manager_key,
        "telegram_username": str(row2.get("telegram_username") or "").strip(),
        "lease_id": lease_id,
        "provider_order_id": (lease2 or {}).get("provider_order_id"),
        "provider_order_number": (lease2 or {}).get("provider_order_number"),
        "provider_proxy_id": (lease2 or {}).get("provider_proxy_id"),
        "expires_at": (lease2 or {}).get("expires_at"),
        "check_ok": bool(guard_ok),
        "info_text": info_text,
        "guard_text": guard_text,
    }


async def _pbuy_finalize_lease(key: str, order_id, order_number, proxy_entry: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    """Shared tail for both buy-confirm and buy-recover once a proxy_entry
    has been found: create the lease row, then delegate the assign
    +apply-fields+guard tail to _pbuy_apply_lease_to_manager (also used by
    Stage 6 P3's /proxy_pool_assign). Never spends -- storage writes + one
    guard check only."""
    host = str(proxy_entry.get("host") or "").strip()
    try:
        port_i = int(str(proxy_entry.get("port")).strip())
    except Exception:
        port_i = None
    login = proxy_entry.get("login")
    password = proxy_entry.get("password")

    if not host or not port_i:
        return {
            "ok": False, "error": "unparseable_proxy",
            "order_id": order_id, "order_number": order_number,
            "message": (
                f"Прокси найден (Order ID: {order_id}), но не удалось распознать "
                "host/port в ответе провайдера. Проверьте вручную у провайдера."
            ),
        }

    from storage import proxy_lease_create as _pbuy_lease_create

    lease_id = await _pbuy_lease_create(
        provider_type="proxy_seller",
        host=host,
        port=port_i,
        manager_key=key,
        provider_order_id=str(order_id) if order_id is not None else None,
        provider_order_number=str(order_number) if order_number is not None else None,
        provider_proxy_id=str(proxy_entry.get("provider_proxy_id")) if proxy_entry.get("provider_proxy_id") is not None else None,
        proxy_type=_PBUY_PROXY_TYPE,
        scheme="socks5",
        login=str(login) if login else None,
        password=str(password) if password else None,
        expires_at=str(proxy_entry.get("expires_at")) if proxy_entry.get("expires_at") else None,
        auto_renew_enabled=False,
        status="active",
        db_path=TPILOT_DB_PATH,
    )

    result = await _pbuy_apply_lease_to_manager({"id": lease_id}, key, source=source)
    if result.get("ok"):
        result["country_name"] = _PBUY_COUNTRY_NAME
    return result


async def _handle_manager_proxy_buy_confirm_command(args: str) -> str:
    """Real purchase. See the module-level SPEND SAFETY note above -- this
    is the ONLY place in the project that ever passes allow_spend=True to
    make_ipv4()."""
    key = registry_normalize_manager_key(str(args or "").strip().split()[0] if str(args or "").strip() else "")
    if not key:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой manager_key"}, ensure_ascii=False)

    row = await _tpag_registry_get(key)
    if not row or str(row.get("status") or "") == "archived":
        return _pbuy_json.dumps({"ok": False, "error": "manager_not_found", "message": f"Менеджер не найден: {key}"}, ensure_ascii=False)

    provider = _pbuy_provider()
    if provider is None:
        return _pbuy_json.dumps({"ok": False, "error": "not_configured", "message": "Proxy-Seller API key не настроен"}, ensure_ascii=False)

    from proxy_provider import find_country_id, find_period_id, extract_order_info

    try:
        ref = await _pbuy_call(provider.reference_list, _PBUY_PROXY_TYPE)
    except Exception as e:
        return _pbuy_json.dumps({"ok": False, "error": "reference_list_failed", "message": _pbuy_safe_error(e)}, ensure_ascii=False)

    country_id = find_country_id(ref, alpha3=_PBUY_COUNTRY_ALPHA3, name=_PBUY_COUNTRY_NAME)
    if country_id is None:
        return _pbuy_json.dumps({
            "ok": False, "error": "country_not_found",
            "message": f"Страна {_PBUY_COUNTRY_NAME} ({_PBUY_COUNTRY_ALPHA3}) не найдена в справочнике провайдера.",
        }, ensure_ascii=False)

    period_id = find_period_id(ref, preferred_id=_PBUY_PERIOD_ID, preferred_name=_PBUY_PERIOD_NAME)
    if period_id is None:
        return _pbuy_json.dumps({
            "ok": False, "error": "period_not_found",
            "message": f"Период {_PBUY_PERIOD_NAME} ({_PBUY_PERIOD_ID}) не найден в справочнике провайдера.",
        }, ensure_ascii=False)

    custom_target_name = _pbuy_custom_target_name(key)
    try:
        make_res = await _pbuy_call(
            provider.make_ipv4,
            country_id, period_id, _PBUY_QUANTITY,
            "", "", custom_target_name, _PBUY_PAYMENT_ID,
            allow_spend=True,
        )
    except Exception as e:
        return _pbuy_json.dumps({"ok": False, "error": "make_failed", "message": _pbuy_safe_error(e)}, ensure_ascii=False)

    order_info = extract_order_info(make_res)
    order_id = order_info.get("order_id")
    order_number = order_info.get("order_number")

    # Money is already spent past this point -- provisioning is read-only
    # (list_proxies / download_proxies), never order/make again.
    proxy_entry = await _pbuy_poll_for_proxy(
        provider, order_id,
        attempts=_PBUY_PROVISION_POLL_ATTEMPTS,
        interval_sec=_PBUY_PROVISION_POLL_INTERVAL_SEC,
    )

    if proxy_entry is None:
        # Live incident 2026-07-09: never point the admin at manual
        # /manager_proxy_set as the primary path here -- the order is
        # valid and paid for, it just isn't provisioned yet. Recovery
        # (read-only, no spend) is the correct next step, exposed via
        # /manager_proxy_buy_recover and the "🔄 Дозабрать proxy" button.
        return _pbuy_json.dumps({
            "ok": False, "error": "provisioning_pending",
            "order_id": order_id, "order_number": order_number,
            "manager_key": key,
            "message": (
                f"Покупка совершена, orderId сохранён ({order_id}), но proxy ещё не выдан "
                "провайдером. Нажмите «🔄 Дозабрать proxy» позже."
            ),
        }, ensure_ascii=False)

    result = await _pbuy_finalize_lease(key, order_id, order_number, proxy_entry, source="panel_buy_confirm")
    return _pbuy_json.dumps(result, ensure_ascii=False)


async def _handle_manager_proxy_buy_recover_command(args: str) -> str:
    """Recover an already-purchased order whose proxy wasn't ready during
    the original buy-confirm's poll window. NEVER calls order/make, NEVER
    passes allow_spend=True -- read-only provider calls only (list_proxies
    / download_proxies), same as buy-confirm's provisioning poll but with
    a shorter, admin-repeatable wait."""
    parts = str(args or "").strip().split()
    key = registry_normalize_manager_key(parts[0]) if parts else ""
    order_id = parts[1].strip() if len(parts) > 1 else ""
    if not key:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой manager_key"}, ensure_ascii=False)
    if not order_id:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой order_id"}, ensure_ascii=False)

    row = await _tpag_registry_get(key)
    if not row or str(row.get("status") or "") == "archived":
        return _pbuy_json.dumps({"ok": False, "error": "manager_not_found", "message": f"Менеджер не найден: {key}"}, ensure_ascii=False)

    provider = _pbuy_provider()
    if provider is None:
        return _pbuy_json.dumps({"ok": False, "error": "not_configured", "message": "Proxy-Seller API key не настроен"}, ensure_ascii=False)

    proxy_entry = await _pbuy_poll_for_proxy(
        provider, order_id,
        attempts=_PBUY_RECOVER_POLL_ATTEMPTS,
        interval_sec=_PBUY_RECOVER_POLL_INTERVAL_SEC,
    )
    if proxy_entry is None:
        return _pbuy_json.dumps({
            "ok": False, "error": "still_pending",
            "order_id": order_id, "manager_key": key,
            "message": "Proxy ещё не готов у провайдера, попробуйте позже.",
        }, ensure_ascii=False)

    result = await _pbuy_finalize_lease(key, order_id, None, proxy_entry, source="panel_buy_recover")
    return _pbuy_json.dumps(result, ensure_ascii=False)


_PRENEW_PROXY_TYPE = "ipv4"


_PRENEW_PAYMENT_ID = 1


_PRENEW_LOOP_TICK_SEC = 600  # 10 minutes -- well under the +/-30min slot window so no slot is ever skipped


_PRENEW_WARN_SLOTS = {
    "tomorrow_noon": (12, 0),
    "today_morning": (9, 0),
}


_PRENEW_AUTORENEW_SLOTS = {
    "autorenew_pre": (12, 0),
    "autorenew_today_morning": (9, 0),
    "autorenew_today_day": (13, 0),
    "autorenew_today_evening": (17, 0),
}


_PRENEW_SLOT_WINDOW_MIN = 30


def _prenew_slot_for_now(now_local: datetime, windows: Dict[str, Tuple[int, int]]) -> Optional[str]:
    """Return the slot name whose target (hour, minute) is within
    _PRENEW_SLOT_WINDOW_MIN of `now_local`, or None. `now_local` must
    already be a naive Kyiv-local datetime (tzinfo stripped)."""
    for slot, (h, m) in windows.items():
        target = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        delta_min = abs((now_local - target).total_seconds()) / 60.0
        if delta_min <= _PRENEW_SLOT_WINDOW_MIN:
            return slot
    return None


async def _prenew_display_name(manager_key: str) -> str:
    manager_key = str(manager_key or "").strip()
    if not manager_key:
        return ""
    try:
        row = await _tpag_registry_get(manager_key)
        if row:
            return str(row.get("display_name") or "").strip() or manager_key
    except Exception:
        pass
    return manager_key


async def _prenew_manager_identity_line(manager_key: str) -> str:
    """'{display_name} | @{username}' / '{display_name} | без username',
    deliberately matching panel_bot.py's _pxm_manager_identity wording
    (panel_bot.py:428) -- an admin should see the SAME identity phrasing on
    a push notification as on the pool/method-menu screens. main.py cannot
    import panel_bot.py (separate process concerns), so this is kept in
    sync by convention, not by shared code. Returns 'не назначен' for an
    empty manager_key (unassigned/orphan lease) -- panel_bot.py's helper has
    no equivalent case since it is always called with a real manager row."""
    manager_key = str(manager_key or "").strip()
    if not manager_key:
        return "не назначен"
    try:
        row = await _tpag_registry_get(manager_key)
    except Exception:
        row = None
    if not row:
        return manager_key
    name = str(row.get("display_name") or manager_key).strip() or manager_key
    user = str(row.get("telegram_username") or "").strip()
    return f"{name} | @{user}" if user else f"{name} | без username"


def _prenew_format_expires_display(raw: Any) -> str:
    """DD.MM.YYYY for any expires_at shape this subsystem has seen
    (YYYY-MM-DD / YYYY-MM-DDTHH:MM:SS / DD.MM.YYYY / ISO with offset) --
    falls back to the raw stored string if unparseable (never guesses,
    never blanks it out)."""
    dt = _ppool_parse_expires_at(raw)
    if dt is not None:
        return dt.strftime("%d.%m.%Y")
    return str(raw or "_")


async def _prenew_collect_active_seller_leases() -> List[Dict[str, Any]]:
    """All active proxy_seller leases, read fresh each tick. Bypasses
    proxy_lease_list_expiring()'s string-comparison horizon (unreliable
    for DD.MM.YYYY expires_at) -- precise day-math happens in Python via
    _ppool_parse_expires_at on the caller side."""
    from storage import proxy_lease_list_all as _prenew_list_all
    leases = await _prenew_list_all(db_path=TPILOT_DB_PATH)
    return [
        l for l in leases
        if str(l.get("status") or "").strip().lower() == "active"
        and str(l.get("provider_type") or "") == "proxy_seller"
    ]


_PRENEW_ROLE_ENFORCEMENT_ENABLED = True


def _prenew_lease_role(
    lease: Dict[str, Any],
    manager_row: Optional[Dict[str, Any]],
    usage_map: Optional[Dict[Tuple[str, int], List[Dict[str, Any]]]] = None,
) -> str:
    """Pure (no I/O), never raises. Returns 'managed' | 'orphan' | 'free'.

    managed: lease.manager_key points at a live (status != 'archived'),
      proxy-enabled (proxy_enabled==1) manager, AND this lease is that
      manager's CURRENT proxy -- either by managers.proxy_lease_id, or (for
      rows predating that column / manual entry) by host:port matching the
      manager's own proxy_host:proxy_port. A manager_key-less lease whose
      host:port is nonetheless found in the live usage map (proxy applied
      outside the pool) also counts as managed -- never silently let an
      in-use proxy look 'free' and expire unnoticed.
    orphan: manager_key set but the manager is missing/archived/proxy_
      disabled, OR the lease is a replaced/old lease of a still-live
      manager (usage/proxy_lease_id now point elsewhere).
    free: no manager_key and the host:port is not in use anywhere.
    """
    try:
        manager_key = str(lease.get("manager_key") or "").strip()
        usage_key = _ppool_usage_key(lease.get("host"), lease.get("port"))
        used_by_someone = bool(usage_map and usage_key and usage_map.get(usage_key))

        if manager_key:
            if manager_row is None:
                return "orphan"
            if str(manager_row.get("status") or "").strip().lower() == "archived":
                return "orphan"
            if int(manager_row.get("proxy_enabled") or 0) != 1:
                return "orphan"

            is_current = False
            lease_id = lease.get("id")
            mgr_lease_id = manager_row.get("proxy_lease_id")
            if lease_id is not None and mgr_lease_id is not None:
                try:
                    is_current = int(lease_id) == int(mgr_lease_id)
                except Exception:
                    is_current = False
            if not is_current:
                mgr_usage_key = _ppool_usage_key(manager_row.get("proxy_host"), manager_row.get("proxy_port"))
                is_current = bool(usage_key is not None and mgr_usage_key is not None and usage_key == mgr_usage_key)

            return "managed" if is_current else "orphan"

        return "managed" if used_by_someone else "free"
    except Exception:
        # Never crash a caller over an unclassifiable row -- 'orphan' is the
        # safe default (skip this tick), same convention as
        # _prenew_warn_eligible/_prenew_autorenew_gates_ok returning False.
        return "orphan"


async def _prenew_role_diagnostic(lease: Dict[str, Any], usage_map: Optional[Dict[Tuple[str, int], List[Dict[str, Any]]]], *, context: str) -> str:
    """OBSERVE-mode diagnostic: compute the lease's role and log ONE line
    only when it diverges from the legacy (role-blind) eligibility that let
    the caller reach this point -- i.e. only when an enforced gate WOULD
    have skipped this lease. Never raises, never blocks. Returns the role so
    callers can also use it once _PRENEW_ROLE_ENFORCEMENT_ENABLED is True."""
    try:
        manager_key = str(lease.get("manager_key") or "").strip()
        manager_row = await _tpag_registry_get(manager_key) if manager_key else None
        role = _prenew_lease_role(lease, manager_row, usage_map)
        if role != "managed":
            try:
                print(
                    f"[prenew-role-observe] context={context} lease_id={lease.get('id')} "
                    f"role={role} manager_key={manager_key or '_'} "
                    f"host={lease.get('host') or '_'}:{lease.get('port') or '_'} "
                    f"enforcement={_PRENEW_ROLE_ENFORCEMENT_ENABLED}"
                )
            except Exception:
                pass
        return role
    except Exception:
        return "orphan"


async def _prenew_lease_is_orphan(
    lease: Dict[str, Any],
    usage_map: Optional[Dict[Tuple[str, int], List[Dict[str, Any]]]] = None,
    *, context: str = "gate",
) -> bool:
    """Single reused orphan gate for the renewal entry points that don't
    already have one (_renewal_wrapped_execute -- the shared spend choke
    point for BOTH renewal engines plus both manual confirm paths -- and
    engine 2's _renewal_auto_tick loop). Read-only, deterministic, no
    Telegram/network, no DB writes -- delegates entirely to the SAME pure
    classifier (_prenew_lease_role, R1 20260808) engine 1 and the warn loop
    already use via _prenew_role_diagnostic; this is NOT a second orphan
    definition, only a second thin async wrapper around the one pure
    function, for callers that don't have a per-tick usage_map handy
    (usage_map defaults to None -- _prenew_lease_role's own documented
    fallback: a manager_key match is still decided correctly via
    proxy_lease_id/host:port against the manager row; only the extra
    'used outside pool' signal for a manager_key-less lease is skipped).

    Both 'orphan' (assigned but broken -- manager missing/archived/disabled/
    assignment mismatch) and 'free' (never assigned) roles return True here
    -- neither has a real manager who should ever be billed for this lease,
    regardless of which of the two applies. A manager that merely exists but
    is disabled/manual_stopped is NOT orphan by itself (_prenew_lease_role's
    own 'orphan' condition is status=='archived' or proxy_enabled!=1 --
    disabled/manual_stopped is neither).

    Fails CLOSED: any lookup error is treated as orphan (never spend/notify
    on a lease this gate could not confidently classify)."""
    try:
        lease_id = lease.get("id")
        manager_key = str(lease.get("manager_key") or "").strip()
        manager_row = await _tpag_registry_get(manager_key) if manager_key else None
        role = _prenew_lease_role(lease, manager_row, usage_map)
        is_orphan = role != "managed"
        if is_orphan:
            reason = "manager_not_found" if (manager_key and manager_row is None) else (
                "assignment_mismatch" if (manager_key and manager_row is not None) else (
                    "placeholder_key" if manager_key == "_" else "missing_manager_key"
                )
            )
            try:
                print(
                    f"[proxy-orphan] context={context} lease_id={lease_id} "
                    f"proxy={lease.get('host') or '_'}:{lease.get('port') or '_'} "
                    f"provider_proxy_id={lease.get('provider_proxy_id') or '_'} "
                    f"raw_manager_key={manager_key or '_'} role={role} reason={reason}"
                )
            except Exception:
                pass
        return is_orphan
    except Exception:
        return True


def _prenew_warn_action_needed(lease: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Decide whether a warn-eligible (T-1 or T=0) lease actually needs a
    reminder. Returns None (silent) when auto-renew is configured, has a
    provider identity, and hasn't just failed/gone unverified -- TPilot
    will (or already did) handle it. Otherwise returns {'reason','action'}
    for the reminder body. Pure, no I/O, never raises."""
    recent_failure_window_sec = 36 * 3600  # covers the T-1 -> T=0 gap plus a buffer
    try:
        if not str(lease.get("provider_proxy_id") or "").strip():
            return {
                "reason": "У прокси нет идентификатора провайдера — автопродление невозможно.",
                "action": "Обратитесь к провайдеру вручную или замените прокси.",
            }
        if int(lease.get("auto_renew_enabled") or 0) != 1:
            return {
                "reason": "Автопродление для этого прокси выключено.",
                "action": "Включите автопродление или продлите вручную.",
            }
        last_status = str(lease.get("last_renew_status") or "")
        last_attempt = lease.get("last_renew_attempt_at")
        recently_failed = last_status.startswith("renew_failed") or last_status == "renew_unverified"
        if recently_failed and last_attempt:
            try:
                elapsed = (_tp_utc_now() - datetime.fromisoformat(str(last_attempt))).total_seconds()
            except Exception:
                elapsed = None
            if elapsed is None or elapsed <= recent_failure_window_sec:
                return {
                    "reason": "Последняя попытка автопродления не удалась.",
                    "action": "Проверьте прокси и продлите вручную.",
                }
        return None
    except Exception:
        return {
            "reason": "Не удалось определить состояние автопродления.",
            "action": "Откройте карточку прокси и проверьте вручную.",
        }


async def _prenew_send_lease_warning(lease: Dict[str, Any], need: Dict[str, str], *, is_today: bool) -> None:
    """Per-lease T-1/T=0 reminder -- replaces the old grouped multi-lease
    format entirely (R5). Only ever called for a lease that IS warn-eligible
    AND needs action (_prenew_warn_action_needed returned non-None); a
    normally-auto-renewing lease never reaches this function. `lease_id` is
    kept as the LAST technical line (blank-line separated from the human
    text) purely for panel_bot.py's existing lease_id: regex button parser
    -- never mixed into the human-readable body."""
    lease_id = lease.get("id")
    # CORRECTION B 20260810: defensive barrier, same rationale as the
    # identical check in _prenew_autorenew_one/_renewal_auto_tick_report_
    # outcome -- the caller (_prenew_notify_expiring_loop) already gates on
    # role before calling this function, but re-checking here means a
    # future direct caller can never reintroduce an orphan T-1/T=0 alert by
    # forgetting that gate.
    if await _prenew_lease_is_orphan(lease, context="warn_send"):
        return
    identity = await _prenew_manager_identity_line(str(lease.get("manager_key") or ""))
    title = "⚠️ Прокси заканчивается сегодня" if is_today else "⚠️ Прокси заканчивается завтра"
    lines = [
        title,
        "",
        f"Менеджер: {identity}",
        f"Ключ: {lease.get('manager_key') or '_'}",
        f"Прокси: {lease.get('host') or '_'}:{lease.get('port') or '_'}",
        f"Действует до: {_prenew_format_expires_display(lease.get('expires_at'))}",
        "",
        f"Причина: {need.get('reason') or 'Причина не определена.'}",
        f"Что делать: {need.get('action') or 'Проверьте прокси.'}",
        "",
        f"lease_id: {lease_id}",
    ]
    body = "\n".join(lines)
    try:
        await _create_panel_notification("proxy_renew_warning", title, body)
    except Exception as exc:
        print(f"[prenew] lease warning send error: {exc!r}")


def _prenew_warn_eligible(lease: Dict[str, Any], slot: str, now_local: datetime) -> bool:
    """Pure (no I/O), independently-testable warning-eligibility check
    for ONE lease at the CURRENT slot -- mirrors _prenew_autorenew_gates_
    ok's structure. Caller has already filtered to status=='active' and
    provider_type=='proxy_seller'. Does NOT check per-slot dedupe (that's
    proxy_renew_notify_mark_once, checked separately). Never raises."""
    try:
        expires_dt = _ppool_parse_expires_at(lease.get("expires_at"))
        if expires_dt is None:
            return False
        day_delta = (expires_dt.date() - now_local.date()).days
        if slot == "tomorrow_noon":
            return day_delta == 1
        if slot == "today_morning":
            return day_delta == 0
        return False
    except Exception:
        return False


_PRENEW_NOTIFY_LOG_RETENTION_DAYS = 90


_prenew_last_purge_date: Optional[str] = None  # in-memory, once/day best-effort -- harmless if it re-fires after a restart


async def _prenew_notify_expiring_loop() -> None:
    """Kyiv-local, slot-based, per-lease/date/slot deduped expiry
    reminders. Tomorrow-expiry -> at most one reminder at tomorrow_noon;
    today-expiry -> at most one reminder at today_morning (R5, 20260808:
    down from up to 4/day -- see _PRENEW_WARN_SLOTS). Never spends, never
    calls the provider, never writes last_renew_attempt_at -- that column
    is reserved for actual renewal attempts, not mere notifications
    (dedupe lives entirely in proxy_renew_notify_log).

    R5 (20260808): a lease that's warn-eligible AND already claimed its
    per-slot dedupe key is THEN checked against _prenew_warn_action_needed
    -- 'no action needed' leases (auto-renew configured & healthy) are
    silently skipped, no Telegram card at all. Grouped multi-lease
    messages are gone -- every reminder is per-lease now (also fixes the
    old grouped card never carrying a lease_id: marker, so it had no
    action buttons)."""
    global _prenew_last_purge_date
    while True:
        try:
            now_local = _kyiv_now().replace(tzinfo=None)
            today_str = now_local.strftime("%Y-%m-%d")
            slot = _prenew_slot_for_now(now_local, _PRENEW_WARN_SLOTS)
            if slot:
                from storage import proxy_renew_notify_mark_once as _prenew_mark_once
                leases = await _prenew_collect_active_seller_leases()
                usage_map = await _ppool_manager_usage_map()  # R1: observe-mode role diagnostic only
                for lease in leases:
                    try:
                        if not _prenew_warn_eligible(lease, slot, now_local):
                            continue
                        # R1 (observe mode): compute+log role, never gate on it unless enforcement is on.
                        role = await _prenew_role_diagnostic(lease, usage_map, context="warn")
                        if _PRENEW_ROLE_ENFORCEMENT_ENABLED and role != "managed":
                            continue
                        lease_id = int(lease.get("id") or 0)
                        if not lease_id:
                            continue
                        should_consider = await _prenew_mark_once(lease_id, today_str, slot, db_path=TPILOT_DB_PATH)
                        if not should_consider:
                            continue
                        need = _prenew_warn_action_needed(lease)
                        if need is None:
                            continue  # R5: auto-renew configured & healthy -- silence
                        await _prenew_send_lease_warning(lease, need, is_today=(slot == "today_morning"))
                    except Exception as exc:
                        print(f"[prenew] per-lease slot-eval error: {exc!r}")
            # R5d: housekeeping -- purge notify-log rows older than the
            # retention window, at most once per Kyiv-day. Best-effort;
            # never blocks/breaks the warn tick itself.
            if _prenew_last_purge_date != today_str:
                try:
                    from storage import proxy_renew_notify_purge_old as _prenew_purge_old
                    cutoff = (now_local - timedelta(days=_PRENEW_NOTIFY_LOG_RETENTION_DAYS)).strftime("%Y-%m-%d")
                    await _prenew_purge_old(cutoff, db_path=TPILOT_DB_PATH)
                    _prenew_last_purge_date = today_str
                except Exception as exc:
                    print(f"[prenew] notify-log purge error: {exc!r}")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            print(f"[prenew] notify loop error: {exc!r}")
        await asyncio.sleep(_PRENEW_LOOP_TICK_SEC)


async def _handle_proxy_renew_calc_command(args: str) -> str:
    """No-spend renewal price preview (reference_list + prolong_calc +
    balance only). Returns a JSON string (panel_commands has no
    result_json column, same convention as Stage 4).

    BL-2 fix (Proxy Renewal Reliability review, 20260808): every failure
    branch now also carries error_category/reason/action (same shape as
    _prenew_preflight_check/_prenew_execute_renewal) so panel_bot.py's
    calc-preview screen never has to fall back to showing this dict's raw
    `message` (which for the two provider-facing branches below is only a
    best-effort _pbuy_safe_error scrub, not a guaranteed-safe user text)."""
    try:
        lease_id = int(str(args or "").strip().split()[0])
    except Exception:
        return _pbuy_json.dumps({
            "ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id",
            "reason": "Некорректный запрос.", "action": "Повторите действие из карточки прокси.",
        }, ensure_ascii=False)

    from storage import proxy_lease_get as _prenew_lease_get

    lease = await _prenew_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease or str(lease.get("status") or "") != "active":
        return _pbuy_json.dumps({
            "ok": False, "error": "lease_not_found", "message": f"Активный lease не найден: {lease_id}",
            "reason": "Активная аренда прокси не найдена.", "action": "Обновите список прокси и повторите попытку.",
        }, ensure_ascii=False)

    provider_proxy_id = lease.get("provider_proxy_id")
    if not provider_proxy_id:
        return _pbuy_json.dumps({
            "ok": False, "error": "missing_provider_proxy_id",
            "message": "У этого прокси нет provider_proxy_id — автоматическое продление невозможно. Обратитесь к провайдеру вручную.",
            "reason": "У прокси нет привязки к провайдеру.", "action": "Обратитесь к провайдеру вручную.",
        }, ensure_ascii=False)

    provider = _pbuy_provider()
    if provider is None:
        cat = _prenew_classify_provider_error(error_code="not_configured")
        return _pbuy_json.dumps({
            "ok": False, "error": "not_configured", "message": "Proxy-Seller API key не настроен",
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }, ensure_ascii=False)

    from proxy_provider import find_period_id, extract_renewal_info

    try:
        ref = await _pbuy_call(provider.reference_list, _PRENEW_PROXY_TYPE)
    except Exception as e:
        err_text = _pbuy_safe_error(e)
        cat = _prenew_classify_provider_error(message=err_text, kind=getattr(e, "kind", None))
        return _pbuy_json.dumps({
            "ok": False, "error": "reference_list_failed", "message": err_text,
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }, ensure_ascii=False)

    period_id = find_period_id(ref, preferred_id=_PBUY_PERIOD_ID, preferred_name=_PBUY_PERIOD_NAME)
    if period_id is None:
        cat = _prenew_classify_provider_error(error_code="period_not_found")
        return _pbuy_json.dumps({
            "ok": False, "error": "period_not_found",
            "message": f"Период {_PBUY_PERIOD_NAME} ({_PBUY_PERIOD_ID}) не найден в справочнике провайдера.",
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }, ensure_ascii=False)

    try:
        calc = await _pbuy_call(provider.prolong_calc, _PRENEW_PROXY_TYPE, [provider_proxy_id], period_id, _PRENEW_PAYMENT_ID)
    except Exception as e:
        err_text = _pbuy_safe_error(e)
        cat = _prenew_classify_provider_error(message=err_text, kind=getattr(e, "kind", None))
        return _pbuy_json.dumps({
            "ok": False, "error": "calc_failed", "message": err_text,
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }, ensure_ascii=False)

    try:
        bal = await _pbuy_call(provider.balance)
    except Exception:
        bal = {}

    display_name = await _prenew_display_name(str(lease.get("manager_key") or ""))
    info = extract_renewal_info(calc)
    return _pbuy_json.dumps({
        "ok": True,
        "lease_id": lease_id,
        "manager_key": lease.get("manager_key"),
        "display_name": display_name,
        "host": lease.get("host"),
        "port": lease.get("port"),
        "expires_at": lease.get("expires_at"),
        "period_id": _PBUY_PERIOD_ID,
        "price": info.get("price"),
        "total": info.get("total"),
        "currency": info.get("currency"),
        "balance": bal.get("summ") if isinstance(bal, dict) else None,
    }, ensure_ascii=False)


_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS = 2


_PRENEW_VERIFY_REFRESH_BACKOFF_SEC = 60.0


def _prenew_find_provider_entry(
    entries: List[Dict[str, Any]], provider_proxy_id: Any, *, host: Any = None, port: Any = None,
) -> Optional[Dict[str, Any]]:
    """Match a proxy/list entry to our lease: by provider_proxy_id first
    (string compare -- ids may arrive as int or str depending on the
    endpoint), falling back to host:port if the id doesn't match anything
    (the provider may rotate the internal id on renewal while keeping the
    same host:port). Never raises."""
    try:
        pid = str(provider_proxy_id or "").strip()
        if pid:
            for e in entries or []:
                if str(e.get("provider_proxy_id") or "").strip() == pid:
                    return e
        if host and port:
            h = str(host).strip().lower()
            p = str(port).strip()
            for e in entries or []:
                if str(e.get("host") or "").strip().lower() == h and str(e.get("port") or "").strip() == p:
                    return e
    except Exception:
        pass
    return None


async def _prenew_verify_renewal_via_refresh(
    provider: Any, provider_proxy_id: Any, before_dt: Optional[datetime], *,
    host: Any = None, port: Any = None,
    max_attempts: int = _PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS,
    backoff_sec: float = _PRENEW_VERIFY_REFRESH_BACKOFF_SEC,
) -> Dict[str, Any]:
    """READ-ONLY provider refresh used ONLY to confirm a renewal whose make
    response didn't carry a usable newer date_end. list_proxies is a
    read-only endpoint (same one _plc_reconcile_loop/proxy_pool_sync already
    call) -- this NEVER spends. Bounded: at most max_attempts reads, with
    backoff_sec between attempts -- never an unbounded retry. Returns
    {"confirmed": bool, "new_expires_at": str|None, "entry": dict|None}."""
    from proxy_provider import extract_proxy_entries as _verify_extract
    last_entry: Optional[Dict[str, Any]] = None
    for attempt in range(max(1, int(max_attempts))):
        if attempt > 0:
            try:
                await asyncio.sleep(backoff_sec)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        try:
            lst = await _pbuy_call(provider.list_proxies, _PRENEW_PROXY_TYPE)
            entries = _verify_extract(lst)
        except Exception:
            continue
        entry = _prenew_find_provider_entry(entries, provider_proxy_id, host=host, port=port)
        if entry is None:
            continue
        last_entry = entry
        new_dt = _ppool_parse_expires_at(entry.get("expires_at"))
        if new_dt is not None and (before_dt is None or new_dt > before_dt):
            return {"confirmed": True, "new_expires_at": entry.get("expires_at"), "entry": entry}
    return {"confirmed": False, "new_expires_at": (last_entry or {}).get("expires_at"), "entry": last_entry}


async def _prenew_preflight_check(lease: Dict[str, Any]) -> Dict[str, Any]:
    """PRE-SPEND stage (BL-1 fix, Proxy Renewal Reliability review
    20260808): safe READ/PRECHECK steps only -- provider configuration,
    reference_list, period lookup. Never calls prolong_make, never touches
    proxy_renewal_ops/idempotency_key. Safe to call and retry an unlimited
    number of times (a scheduler tick, a manual retry, a restart) without
    ever risking a spend. On success returns the provider handle + resolved
    period_id + parsed pre-renewal expiry for the caller to hand straight
    to the SPEND-PROTECTED stage (_prenew_execute_renewal) so the same
    reference_list/period lookup isn't repeated. On failure returns the
    SAME structured error shape _prenew_execute_renewal has always
    returned for these three cases (not_configured/reference_list_failed/
    period_not_found) -- callers must treat ok=False here as freely
    retryable (no idempotency key was ever created for it)."""
    provider = _pbuy_provider()
    if provider is None:
        cat = _prenew_classify_provider_error(error_code="not_configured")
        return {
            "ok": False, "error": "not_configured", "message": "Proxy-Seller API key не настроен",
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }

    from proxy_provider import find_period_id

    try:
        ref = await _pbuy_call(provider.reference_list, _PRENEW_PROXY_TYPE)
    except Exception as e:
        err_text = _pbuy_safe_error(e)
        cat = _prenew_classify_provider_error(message=err_text, kind=getattr(e, "kind", None))
        return {
            "ok": False, "error": "reference_list_failed", "message": err_text,
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }

    period_id = find_period_id(ref, preferred_id=_PBUY_PERIOD_ID, preferred_name=_PBUY_PERIOD_NAME)
    if period_id is None:
        cat = _prenew_classify_provider_error(error_code="period_not_found")
        return {
            "ok": False, "error": "period_not_found",
            "message": f"Период {_PBUY_PERIOD_NAME} ({_PBUY_PERIOD_ID}) не найден в справочнике провайдера.",
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }

    before_dt = _ppool_parse_expires_at(lease.get("expires_at"))
    return {"ok": True, "provider": provider, "period_id": period_id, "before_dt": before_dt}


async def _prenew_execute_renewal(
    lease: Dict[str, Any], *, source: str, _preflight: Optional[Dict[str, Any]] = None,
    _op_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Shared spend tail. This is the ONLY place in the entire project
    that ever passes allow_spend=True to prolong_make() (mirrors
    _pbuy_apply_lease_to_manager being the only make_ipv4() call site for
    buys). Called by:
    - _handle_proxy_renew_confirm_command (manual, after the admin has
      already seen a no-spend prolong_calc() preview and explicitly
      tapped confirm)
    - _prenew_autorenew_one (automatic, only after the caller has already
      verified ALL auto-renew gates: status==active, auto_renew_enabled
      ==1, provider_type==proxy_seller, provider_proxy_id present, and
      per-slot dedupe)
    This function itself does NOT check auto_renew_enabled -- that gate
    belongs entirely to the caller. Returns a plain dict (never a JSON
    string); never includes the raw password or API key.

    R2 (Proxy Renewal Reliability, 20260808): a make response that doesn't
    raise is NOT treated as proof of success anymore. The new expiry is only
    ever written to storage once it is CONFIRMED (either the make response
    itself carries a usable newer date, or a bounded read-only provider
    refresh shows one). If it can't be confirmed, the outcome is
    'renew_unverified' -- money may have been charged, but expires_at is
    left untouched (never guessed) and the caller must NOT re-spend for
    this same pre-renewal expiry (proxy_renewal_ops.idempotency_key, keyed
    on the UNCHANGED expires_at, already makes a second prolong/make for the
    same cycle impossible once this returns -- see _renewal_wrapped_execute).

    BL-1 fix (Proxy Renewal Reliability review, 20260808): the PRE-SPEND
    checks (provider configured / reference_list / period lookup) now live
    in _prenew_preflight_check, called here on-demand when a caller invokes
    this function directly (e.g. a selftest, or _handle_proxy_renew_confirm_
    command's no-spend preview path) without ALSO passing `_preflight`.
    _renewal_wrapped_execute -- the ONLY caller that actually creates a
    proxy_renewal_ops row / burns the idempotency_key -- runs preflight
    FIRST, and only reaches proxy_renewal_op_create (and then this
    function, passing its already-computed `_preflight`) once preflight has
    succeeded, so a pre-spend failure never consumes the idempotency_key
    and stays freely retryable.

    Crash-window fix (independent re-review correction, 20260809): `_op_id`,
    when passed by _renewal_wrapped_execute, is the freshly-created op row's
    id, still in status='pending' (RESERVED -- spend definitely not started).
    Immediately before the prolong_make call below, that row is CAS-advanced
    to 'make_pending' (SPEND_STARTED) via a single atomic UPDATE ... WHERE
    status='pending' -- the durable, explicit boundary the review asked for:
    once that UPDATE's commit() returns, any later crash must be treated as
    POTENTIALLY spent (ordinary provider-refresh reconcile handles it, same
    as before this fix). If the marker write itself fails to land (exception
    or CAS mismatch), prolong_make is NEVER called -- the op is provably
    still pre-spend, so the caller (_renewal_wrapped_execute) frees it via
    proxy_renewal_op_delete_pending instead of leaving a permanently-stuck
    'pending' row. This is the ONLY new write on the spend path; no new
    allow_spend site, no new table."""
    lease_id = lease.get("id")
    provider_proxy_id = lease.get("provider_proxy_id")

    pre = _preflight if _preflight is not None else await _prenew_preflight_check(lease)
    if not pre.get("ok"):
        return pre
    provider = pre["provider"]
    period_id = pre["period_id"]
    before_dt = pre["before_dt"]

    from proxy_provider import extract_renewal_info
    from storage import (
        proxy_lease_get as _prenew_lease_get2,
        proxy_lease_update_renew as _prenew_update_renew2,
        proxy_lease_update_provider_identity as _prenew_update_identity2,
    )

    if _op_id is not None:
        from storage import proxy_renewal_op_advance as _prenew_mark_spend_started
        marked = False
        try:
            marked = await _prenew_mark_spend_started(
                _op_id, "make_pending", from_status="pending", db_path=TPILOT_DB_PATH,
            )
        except Exception as exc:
            print(f"[proxy_renewal] spend-started marker write error op_id={_op_id}: {exc!r}")
        if not marked:
            # The durable SPEND_STARTED write did not land -- prolong_make
            # is NOT called. The op stays 'pending' (provably pre-spend);
            # the caller frees it.
            return {
                "ok": False, "error": "spend_marker_failed",
                "message": "internal: could not durably mark spend-started before calling the provider",
                "error_category": "unknown",
                "reason": "Внутренняя ошибка перед списанием — провайдер не вызывался.",
                "action": "Повторите попытку позже.",
            }

    try:
        make_res = await _pbuy_call(
            provider.prolong_make, _PRENEW_PROXY_TYPE, [provider_proxy_id], period_id, _PRENEW_PAYMENT_ID,
            allow_spend=True,
        )
    except Exception as e:
        err_text = _pbuy_safe_error(e)
        exc_kind = getattr(e, "kind", None)
        cat = _prenew_classify_provider_error(message=err_text, kind=exc_kind, post_spend=True)
        try:
            await _prenew_update_renew2(lease_id, status_text=f"renew_failed:{err_text}", db_path=TPILOT_DB_PATH)
        except Exception:
            pass
        # Money-safety correction (independent re-review, 20260809, second
        # pass): a prior version of this fix distinguished PROVIDER_ERRORS/
        # AUTH from TRANSPORT/HTTP/RESPONSE_SHAPE and freed the idempotency_
        # key for the former two, reasoning that proxy_provider.py's own
        # _request() checks errors[]/envelope-status BEFORE the HTTP status
        # code, so an explicit business-rule rejection or an auth reject
        # must mean the operation was refused pre-billing. That reasoning
        # does not hold: _request() checks errors[] BEFORE the HTTP status
        # on EVERY response, so an HTTP 500/502/429 whose body ALSO happens
        # to carry a JSON errors[] list (a very ordinary shape for a
        # provider-side failure) produces the exact SAME kind=PROVIDER_ERRORS
        # as a clean pre-billing rejection -- the provider may already have
        # processed prolong_make before failing to report success cleanly.
        # ProxyProviderError.kind carries no HTTP status and no separate
        # signal (e.g. request_not_sent/spend_not_attempted) that could
        # disambiguate these -- so kind alone is NOT sufficient structural
        # proof that no charge occurred. Until proxy_provider.py's contract
        # is verified against live Proxy-Seller documentation/traffic and
        # extended with an explicit no-spend signal, EVERY failure reaching
        # this except-block (any kind, any category) is spend-AMBIGUOUS:
        # the caller (_renewal_wrapped_execute) always keeps the
        # idempotency_key burned (status='failed'), never auto-retries, and
        # never frees this op row. Money safety over convenience.
        return {
            "ok": False, "error": "make_failed", "message": err_text,
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }

    # --- R2 verify-after-renew: don't trust "call didn't raise" alone ---
    info = extract_renewal_info(make_res)
    make_new_expires_at = info.get("new_expires_at")
    make_new_dt = _ppool_parse_expires_at(make_new_expires_at) if make_new_expires_at else None

    confirmed_value: Optional[str] = None
    if make_new_dt is not None and (before_dt is None or make_new_dt > before_dt):
        confirmed_value = str(make_new_expires_at)
    else:
        refresh = await _prenew_verify_renewal_via_refresh(
            provider, provider_proxy_id, before_dt, host=lease.get("host"), port=lease.get("port"),
        )
        if refresh.get("confirmed"):
            confirmed_value = str(refresh.get("new_expires_at"))
            entry = refresh.get("entry") or {}
            entry_pid = str(entry.get("provider_proxy_id") or "").strip()
            if entry_pid and entry_pid != str(provider_proxy_id or "").strip():
                # Provider rotated the identity on renewal -- resync THIS
                # lease row in place (never creates a second/orphan row).
                try:
                    await _prenew_update_identity2(
                        lease_id, provider_proxy_id=entry_pid, provider_order_id=entry.get("order_id"),
                        db_path=TPILOT_DB_PATH,
                    )
                    provider_proxy_id = entry_pid
                except Exception:
                    pass

    if confirmed_value is None:
        # Spend may have happened, but the new expiry could not be
        # confirmed. NEVER write expires_at on a guess; NEVER retry the
        # spend for this cycle (the idempotency_key above stays pinned to
        # the unchanged pre-renewal expiry, so a later tick cannot re-spend).
        try:
            await _prenew_update_renew2(lease_id, status_text="renew_unverified", db_path=TPILOT_DB_PATH)
        except Exception:
            pass
        cat = _prenew_classify_provider_error(error_code="unverified")
        return {
            "ok": False, "error": "unverified",
            "message": "Списание могло пройти, но новый срок действия не подтверждён провайдером.",
            "lease_id": lease_id, "manager_key": lease.get("manager_key"),
            "host": lease.get("host"), "port": lease.get("port"),
            "old_expires_at": lease.get("expires_at"), "provider_proxy_id": provider_proxy_id,
            "price": info.get("price"), "total": info.get("total"), "currency": info.get("currency"),
            "error_category": cat["category"], "reason": cat["reason"], "action": cat["action"],
        }

    await _prenew_update_renew2(
        lease_id, status_text="renew_ok", new_expires_at=confirmed_value, db_path=TPILOT_DB_PATH,
    )
    lease2 = await _prenew_lease_get2(lease_id, db_path=TPILOT_DB_PATH) or lease

    return {
        "ok": True,
        "lease_id": lease_id,
        "manager_key": lease.get("manager_key"),
        "host": lease2.get("host"),
        "port": lease2.get("port"),
        "old_expires_at": lease.get("expires_at"),
        "expires_at": lease2.get("expires_at"),
        "provider_proxy_id": provider_proxy_id,
        "price": info.get("price"),
        "total": info.get("total"),
        "currency": info.get("currency"),
    }


async def _handle_proxy_renew_confirm_command(args: str) -> str:
    """Real renewal (manual, admin-confirmed path). Validates the lease,
    then delegates to the SAME idempotent+guarded spend path as the
    auto-renew scheduler (R3, 20260808): _renewal_guard_check ->
    _renewal_wrapped_execute -> _prenew_execute_renewal. There is only ONE
    protected spend path in the project now, manual and automatic alike --
    a double-tap or a manual retry for the SAME pre-renewal expiry can no
    longer double-spend (proxy_renewal_ops idempotency), and an admin-set
    emergency stop/pause/cap/budget/balance floor now applies here too."""
    try:
        lease_id = int(str(args or "").strip().split()[0])
    except Exception:
        return _pbuy_json.dumps({
            "ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id",
            "reason": "Некорректный запрос.", "action": "Повторите действие из карточки прокси.",
        }, ensure_ascii=False)

    from storage import proxy_lease_get as _prenew_lease_get

    lease = await _prenew_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease or str(lease.get("status") or "") != "active":
        return _pbuy_json.dumps({
            "ok": False, "error": "lease_not_found", "message": f"Активный lease не найден: {lease_id}",
            "reason": "Активная аренда прокси не найдена.", "action": "Обновите список прокси и повторите попытку.",
        }, ensure_ascii=False)

    provider_proxy_id = lease.get("provider_proxy_id")
    if not provider_proxy_id:
        return _pbuy_json.dumps({
            "ok": False, "error": "missing_provider_proxy_id",
            "message": "У этого прокси нет provider_proxy_id — автоматическое продление невозможно. Обратитесь к провайдеру вручную.",
            "reason": "У прокси нет привязки к провайдеру.", "action": "Обратитесь к провайдеру вручную.",
        }, ensure_ascii=False)

    # CORRECTION B checkpoint follow-up 20260810: this manual command called
    # _renewal_calc_preview (a real, if no-spend, provider reference_list +
    # prolong_calc round trip) BEFORE ever reaching _renewal_wrapped_
    # execute's own orphan gate -- an admin manually confirming an orphan
    # lease still made two live provider calls before being rejected. Gate
    # here too, before ANY provider interaction, not just before spend.
    if await _prenew_lease_is_orphan(lease, context="panel_renew_confirm_entry"):
        return _pbuy_json.dumps({
            "ok": False, "skipped": True, "error": "orphan_lease",
            "message": "У этого прокси нет реального назначенного менеджера — автопродление и списание не выполняются.",
            "reason": "Прокси не привязан к активному менеджеру (orphan lease).",
            "action": "Проверьте привязку прокси в пуле (синхронизация/переназначение).",
        }, ensure_ascii=False)

    import storage as _rn_confirm_storage
    cfg = await _rn_confirm_storage.proxy_renewal_config_get(db_path=TPILOT_DB_PATH)
    day_str = _renewal_kyiv_today_str()
    preview = await _renewal_calc_preview(lease)
    calc_total = preview.get("total") if preview.get("ok") else None
    block = await _renewal_guard_check(cfg, calc_total, day_str)
    if block:
        return _pbuy_json.dumps({
            "ok": False, "error": "blocked", "message": block,
            "reason": block, "action": "Проверьте настройки автопродления (лимиты/баланс/пауза).",
        }, ensure_ascii=False)

    result = await _renewal_wrapped_execute(lease, source="panel_renew_confirm", actor_user_id=None)
    if result.get("skipped"):
        return _pbuy_json.dumps(result, ensure_ascii=False)
    if result.get("ok"):
        result["display_name"] = await _prenew_display_name(str(lease.get("manager_key") or ""))
    return _pbuy_json.dumps(result, ensure_ascii=False)


async def _handle_proxy_renew_defer_command(args: str) -> str:
    """Pure bookkeeping (last_renew_status='deferred'). Since Stage 6.1E,
    the warning loop's dedupe is per-lease/date/slot (proxy_renew_notify_
    log), not a last_renew_attempt_at cooldown -- deferring no longer
    suppresses the next slot's warning, it only records that the admin
    saw and dismissed this one. Never touches the provider, never
    spends."""
    try:
        lease_id = int(str(args or "").strip().split()[0])
    except Exception:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id"}, ensure_ascii=False)

    from storage import proxy_lease_get as _prenew_lease_get, proxy_lease_update_renew as _prenew_update_renew

    lease = await _prenew_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease:
        return _pbuy_json.dumps({"ok": False, "error": "lease_not_found", "message": f"Lease не найден: {lease_id}"}, ensure_ascii=False)
    await _prenew_update_renew(lease_id, status_text="deferred", db_path=TPILOT_DB_PATH)
    return _pbuy_json.dumps({"ok": True, "lease_id": lease_id, "message": "Напоминание отложено."}, ensure_ascii=False)


async def _handle_proxy_pool_autorenew_command(args: str) -> str:
    """Toggle proxy_leases.auto_renew_enabled for one lease (Stage 6.1F).
    Pure storage write -- never calls the provider, never spends."""
    parts = str(args or "").strip().split()
    try:
        lease_id = int(parts[0])
    except Exception:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id"}, ensure_ascii=False)
    if len(parts) < 2 or parts[1] not in ("0", "1"):
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Формат: /proxy_pool_autorenew <lease_id> <0|1>"}, ensure_ascii=False)
    enabled = parts[1] == "1"

    from storage import proxy_lease_get as _ar_lease_get, proxy_lease_set_auto_renew as _ar_set_autorenew

    lease = await _ar_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease:
        return _pbuy_json.dumps({"ok": False, "error": "lease_not_found", "message": f"Lease не найден: {lease_id}"}, ensure_ascii=False)

    ok = await _ar_set_autorenew(lease_id, enabled, db_path=TPILOT_DB_PATH)
    if not ok:
        return _pbuy_json.dumps({"ok": False, "error": "update_failed", "message": "Не удалось изменить автопродление."}, ensure_ascii=False)

    return _pbuy_json.dumps({"ok": True, "lease_id": lease_id, "auto_renew_enabled": enabled}, ensure_ascii=False)


async def _prenew_autorenew_one(lease: Dict[str, Any]) -> None:
    """Attempt one guarded auto-renewal for a lease that has ALREADY
    passed every gate in the caller (_prenew_autorenew_loop): status ==
    'active', provider_type == 'proxy_seller', provider_proxy_id present,
    auto_renew_enabled == 1, and not already attempted in this slot/date.
    Sends exactly one admin message reporting success or failure. Never
    exposes the password/API key (only scrubbed provider errors, host,
    lease_id, dates, and a possible price are included).

    R3 (Proxy Renewal Reliability, 20260808): the spend itself is no longer
    a direct call to _prenew_execute_renewal. It goes through the SAME
    idempotent+guarded path as the (previously dormant) TPilot-managed
    engine: _renewal_guard_check (emergency stop/pause/per-op cap/daily
    budget/min balance -- all no-ops with the default unconfigured values,
    so this changes nothing for an admin who never touched those settings)
    and _renewal_wrapped_execute (proxy_renewal_ops idempotency -- a second
    attempt for the SAME pre-renewal expiry is rejected before it can ever
    reach the provider). This intentionally does NOT gate on
    proxy_renewal_config.automation_enabled -- that flag is the OTHER
    (still-dormant) engine's separate one-time-consent switch for running
    UNPROMPTED background renewals on its own 6h cadence; this scheduler
    has always run on its own slot cadence regardless of that flag, and
    gating on it here would silently stop all auto-renewal in production."""
    lease_id = lease.get("id")
    # CORRECTION B 20260810: defensive barrier, same rationale as the
    # identical check in _renewal_auto_tick_report_outcome -- the caller
    # (_prenew_autorenew_loop) already gates on role before calling this
    # function at all, but re-checking here with the SAME classifier means a
    # future direct caller can never reintroduce an orphan alert/spend by
    # forgetting that gate.
    if await _prenew_lease_is_orphan(lease, context="autorenew_one"):
        return
    import storage as _ar_storage
    cfg = await _ar_storage.proxy_renewal_config_get(db_path=TPILOT_DB_PATH)
    day_str = _renewal_kyiv_today_str()
    preview = await _renewal_calc_preview(lease)
    calc_total = preview.get("total") if preview.get("ok") else None
    block = await _renewal_guard_check(cfg, calc_total, day_str)
    if block:
        result: Dict[str, Any] = {"ok": False, "error": "blocked", "message": block}
    else:
        result = await _renewal_wrapped_execute(lease, source="autorenew", actor_user_id=None)
        if result.get("skipped"):
            if result.get("error") != "blocked_unresolved":
                # Already in flight -- the FIRST attempt for this
                # pre-renewal expiry already reported its outcome; nothing
                # new happened, so nothing new to tell the admin.
                return
            # BLOCKER-1 fix (retry-policy correction, 20260809):
            # 'blocked_unresolved' means a durably-stuck ambiguous op from
            # an EARLIER attempt is occupying this lease's idempotency_key
            # -- previously this fell through the same silent `return` as
            # a benign in-flight skip, so the admin was never told a lease
            # near expiry could no longer renew at all ("тихая смерть").
            # Falls through into the shared non-success reporting block
            # below (same dedup/notify machinery as every other outcome).
    # R5 (Proxy Renewal Reliability, 20260808): SUCCESS is now SILENT (no
    # Telegram at all -- NO ACTION REQUIRED = NO ALERT). It's recorded as a
    # safe lifecycle/audit event instead (proxy_lifecycle_events, already
    # used by the lifecycle-sync subsystem for exactly this purpose --
    # append-only, never stores secrets).
    if result.get("ok"):
        try:
            total = result.get("total")
            price = result.get("price")
            currency = str(result.get("currency") or "").strip()
            amount_text = f"{total} {currency}".strip() if total is not None else (f"{price} {currency}".strip() if price is not None else "")
            await _ar_storage.proxy_lifecycle_event_add(
                lease_id, "renew_ok", provider_proxy_id=result.get("provider_proxy_id"),
                actor="autorenew",
                detail=f"old={result.get('old_expires_at')} new={result.get('expires_at')} amount={amount_text}".strip(),
                db_path=TPILOT_DB_PATH,
            )
        except Exception as exc:
            print(f"[prenew-autorenew] success audit-log error: {exc!r}")
        return

    # --- Non-success: blocked / unverified / classified provider failure.
    # R5d dedup: at most ONE non-success card per lease per Kyiv day PER
    # error_category (NB-6 fix, review 20260808) -- BL-1 means a pre-spend
    # failure (e.g. provider not_configured) is now freely retryable, so
    # several DIFFERENT real causes can legitimately occur for the same
    # lease on the same day; deduping on a single flat "outcome_notify" slot
    # would silently swallow every category after the first one to fire.
    # Genuine provider failures/unverified outcomes that DID reach the spend
    # stage are ALSO naturally deduped by proxy_renewal_ops.idempotency_key
    # (it stays pinned to the unchanged pre-renewal expiry), so this is a
    # belt-and-suspenders guarantee, not the only one -- and a materially
    # DIFFERENT category (e.g. insufficient_balance today after a
    # not_configured card yesterday morning) is still shown, exactly once.
    error_code = str(result.get("error") or "")
    if error_code == "unverified":
        notify_category = "unverified"
    elif error_code == "blocked":
        notify_category = "blocked"
    elif error_code == "blocked_unresolved":
        notify_category = "blocked_unresolved"
    else:
        notify_category = str(result.get("error_category") or "unknown")
    should_notify = await _ar_storage.proxy_renew_notify_mark_once(
        lease_id, day_str, f"outcome_notify:{notify_category}", db_path=TPILOT_DB_PATH,
    )
    if not should_notify:
        return

    header = [
        f"Менеджер: {await _prenew_manager_identity_line(str(lease.get('manager_key') or ''))}",
        f"Ключ: {lease.get('manager_key') or '_'}",
        f"Прокси: {lease.get('host') or '_'}:{lease.get('port') or '_'}",
        f"Действует до: {_prenew_format_expires_display(lease.get('expires_at'))}",
    ]

    if error_code == "unverified":
        title = "⚠️ Продление прокси не подтверждено"
        lines = [title, "", *header, "",
                 "Списание могло пройти, но Proxy-Seller пока не подтвердил новый срок.",
                 "Повторное списание автоматически не выполняется.",
                 "",
                 "Что делать: синхронизируйте пул и проверьте прокси.",
                 "", f"lease_id: {lease_id}"]
        kind = "proxy_autorenew_unverified"
    elif error_code == "blocked_unresolved":
        # BLOCKER-1 fix (retry-policy correction, 20260809): this lease's
        # idempotency_key is durably occupied by an earlier ambiguous
        # attempt (proxy_renewal_ops status='failed') -- no automatic
        # retry is possible until either a later provider-refresh reconcile
        # confirms the renewal actually went through, or the admin
        # verifies via sync. NO spend button (proxy_autorenew_unverified's
        # existing buttons: sync pool + open card only).
        title = "⚠️ Продление прокси требует проверки"
        lines = [title, "", *header, "",
                 "Причина: предыдущая попытка продления не подтверждена.",
                 "Повторное списание автоматически не выполняется.",
                 "",
                 "Что делать: синхронизируйте пул и проверьте прокси.",
                 "", f"lease_id: {lease_id}"]
        kind = "proxy_autorenew_unverified"
    elif error_code == "blocked":
        title = "⚠️ Автопродление заблокировано"
        lines = [title, "", *header, "",
                 f"Причина: {result.get('message') or 'автопродление заблокировано настройками'}",
                 "Что делать: проверьте настройки автопродления (лимиты/баланс/пауза).",
                 "", f"lease_id: {lease_id}"]
        kind = "proxy_autorenew_blocked"
    else:
        title = "⚠️ Не удалось продлить прокси"
        reason = result.get("reason") or "Не удалось определить причину ошибки провайдера."
        action = result.get("action") or "Откройте карточку прокси и проверьте вручную."
        lines = [title, "", *header, "",
                 f"Причина: {reason}",
                 f"Что делать: {action}",
                 "", f"lease_id: {lease_id}"]
        kind = "proxy_autorenew_failed"

    body = "\n".join(lines)
    try:
        await _create_panel_notification(kind, title, body)
    except Exception as exc:
        print(f"[prenew-autorenew] {kind} notify error: {exc!r}")


def _prenew_autorenew_gates_ok(lease: Dict[str, Any], slot: str, now_local: datetime) -> bool:
    """Pure (no I/O), independently-testable auto-renew eligibility check
    for ONE lease at the CURRENT slot. Caller (_prenew_autorenew_loop) has
    already filtered to status=='active' and provider_type=='proxy_seller'
    via _prenew_collect_active_seller_leases -- this checks the remaining
    gates: auto_renew_enabled==1, provider_proxy_id present, and the
    day-delta matches this slot's bucket (tomorrow for autorenew_pre,
    today for the 3 today_* slots). Does NOT check per-slot dedupe (that
    is proxy_renew_notify_mark_once, a DB side effect the caller checks
    separately). Never raises."""
    try:
        if int(lease.get("auto_renew_enabled") or 0) != 1:
            return False
        if not lease.get("provider_proxy_id"):
            return False
        expires_dt = _ppool_parse_expires_at(lease.get("expires_at"))
        if expires_dt is None:
            return False
        day_delta = (expires_dt.date() - now_local.date()).days
        if slot == "autorenew_pre":
            return day_delta == 1
        if slot in ("autorenew_today_morning", "autorenew_today_day", "autorenew_today_evening"):
            return day_delta == 0
        return False
    except Exception:
        return False


async def _prenew_autorenew_loop() -> None:
    """Kyiv-local, slot-based, per-lease/date/slot deduped auto-renewal.
    First attempt at the autorenew_pre slot (day before expiry, ~12:00);
    on failure, retries at autorenew_today_morning/day/evening. Spends
    ONLY when ALL gates pass: status active, provider_type proxy_seller,
    provider_proxy_id present, auto_renew_enabled==1, and this exact
    (lease_id, date, slot) hasn't already been attempted."""
    while True:
        try:
            now_local = _kyiv_now().replace(tzinfo=None)
            slot = _prenew_slot_for_now(now_local, _PRENEW_AUTORENEW_SLOTS)
            if slot:
                notify_date = now_local.strftime("%Y-%m-%d")
                from storage import proxy_renew_notify_mark_once as _ar_mark_once
                leases = await _prenew_collect_active_seller_leases()
                usage_map = await _ppool_manager_usage_map()  # R1: observe-mode role diagnostic only
                for lease in leases:
                    try:
                        if not _prenew_autorenew_gates_ok(lease, slot, now_local):
                            continue
                        # R1 (observe mode): compute+log role, never gate on it unless enforcement is on.
                        role = await _prenew_role_diagnostic(lease, usage_map, context="autorenew")
                        if _PRENEW_ROLE_ENFORCEMENT_ENABLED and role != "managed":
                            continue
                        lease_id = int(lease.get("id") or 0)
                        if not lease_id:
                            continue
                        should_attempt = await _ar_mark_once(lease_id, notify_date, slot, db_path=TPILOT_DB_PATH)
                        if not should_attempt:
                            continue
                        await _prenew_autorenew_one(lease)
                    except Exception as exc:
                        print(f"[prenew-autorenew] per-lease error: {exc!r}")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            print(f"[prenew-autorenew] loop error: {exc!r}")
        await asyncio.sleep(_PRENEW_LOOP_TICK_SEC)


def _ppool_parse_expires_at(raw: Any) -> Optional[datetime]:
    """Defensive expires_at parser. Live Proxy-Seller data has been seen
    as ISO ("2026-09-09" / "2026-09-09T00:00:00") and as "DD.MM.YYYY"
    ("07.08.2026"). Returns None (never raises) if the format isn't
    recognized -- callers must treat None as "cannot determine, don't
    mark expired", never guess."""
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    except Exception:
        return None


def _ppool_usage_key(host: Any, port: Any) -> Optional[Tuple[str, int]]:
    """Normalize a host+port pair into the map key: host lower/strip, port
    coerced to a positive int. Returns None if either is missing/invalid."""
    h = str(host or "").strip().lower()
    if not h:
        return None
    try:
        p = int(str(port).strip())
    except Exception:
        return None
    if p <= 0:
        return None
    return (h, p)


async def _ppool_manager_usage_map() -> Dict[Tuple[str, int], List[Dict[str, Any]]]:
    """Read-only map of (host_norm, port) -> live managers whose OWN
    proxy_enabled=1 fields point at that host+port. Built ONCE per command
    (never per-lease) from manager_list_rows(include_removed=False) --
    real manager configuration, not proxy_leases -- so this also surfaces
    proxies never bought through the pool ("external"). A manager with
    proxy_enabled=0 (a saved-but-disabled config) is never counted as
    actively occupying its proxy (owner decision)."""
    usage: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
    try:
        rows = await manager_list_rows(include_removed=False)
    except Exception:
        return usage
    for r in rows or []:
        if int(r.get("proxy_enabled") or 0) != 1:
            continue
        key = _ppool_usage_key(r.get("proxy_host"), r.get("proxy_port"))
        if not key:
            continue
        mk = registry_normalize_manager_key(r.get("manager_key") or "")
        if not mk:
            continue
        usage.setdefault(key, []).append({
            "manager_key": mk,
            "display_name": str(r.get("display_name") or "").strip() or mk,
            "telegram_username": str(r.get("telegram_username") or "").strip(),
            "proxy_login": str(r.get("proxy_username") or "").strip(),
            "proxy_enabled": 1,
        })
    return usage


def _ppool_usage_public(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strip a usage-map entry list down to the public used_by shape --
    identity fields only, never login/password."""
    return [
        {"manager_key": e.get("manager_key"), "display_name": e.get("display_name"), "telegram_username": e.get("telegram_username")}
        for e in (entries or [])
    ]


async def _ppool_derive_status(
    lease: Dict[str, Any], usage_map: Optional[Dict[Tuple[str, int], List[Dict[str, Any]]]] = None
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Derive a pool status label without any schema change. Priority:
    disabled (explicit lease.status) > orphaned (manager_key points at a
    missing or archived manager) > expired > assigned > occupied (real
    usage-map match, PROXY USAGE DETECTION 20260711) > free. Returns
    (status, manager_row) -- manager_row is None when there's no
    manager_key, or the manager wasn't found. `usage_map` is optional and
    backward-compatible: omitting it (existing callers) simply skips the
    occupied check and preserves the original free/assigned/... behavior
    byte-for-byte."""
    status_col = str(lease.get("status") or "").strip().lower()
    manager_key = str(lease.get("manager_key") or "").strip()
    manager_row: Optional[Dict[str, Any]] = None
    if manager_key:
        manager_row = await _tpag_registry_get(manager_key)

    if status_col and status_col != "active":
        return "disabled", manager_row

    expires_dt = _ppool_parse_expires_at(lease.get("expires_at"))
    # R5b (Proxy Renewal Reliability, 20260808): compare Kyiv CALENDAR DATES,
    # not a naive expiry datetime against UTC now -- day-math elsewhere in
    # this subsystem (_prenew_warn_eligible/_prenew_autorenew_gates_ok/
    # _renewal_eligible_leases) is already Kyiv-based, and a date-only
    # expires_at parses to midnight, so comparing it to _tp_utc_now()
    # mislabelled a lease expiring TODAY as already "(expired)" for several
    # hours every morning (Kyiv is ahead of UTC).
    is_expired = expires_dt is not None and expires_dt.date() < _kyiv_now().date()

    if manager_key:
        if manager_row is None or str(manager_row.get("status") or "").strip().lower() == "archived":
            return "orphaned", manager_row
        if is_expired:
            return "expired", manager_row
        return "assigned", manager_row

    if is_expired:
        return "expired", None

    if usage_map:
        key = _ppool_usage_key(lease.get("host"), lease.get("port"))
        if key and usage_map.get(key):
            return "occupied", None

    return "free", None


async def _ppool_lease_summary(
    lease: Dict[str, Any], usage_map: Optional[Dict[Tuple[str, int], List[Dict[str, Any]]]] = None
) -> Dict[str, Any]:
    status, manager_row = await _ppool_derive_status(lease, usage_map=usage_map)
    manager_key = str(lease.get("manager_key") or "").strip() or None
    usage_key = _ppool_usage_key(lease.get("host"), lease.get("port"))
    used_by = _ppool_usage_public((usage_map or {}).get(usage_key, [])) if usage_key else []
    return {
        "lease_id": lease.get("id"),
        "host": lease.get("host"),
        "port": lease.get("port"),
        "scheme": lease.get("scheme"),
        "proxy_type": lease.get("proxy_type"),
        "manager_key": manager_key,
        "manager_display_name": (str(manager_row.get("display_name") or "").strip() or manager_key) if manager_row else manager_key,
        "status": status,
        "used_by": used_by,
        "expires_at": lease.get("expires_at"),
        "provider_order_id": lease.get("provider_order_id"),
        "provider_proxy_id": lease.get("provider_proxy_id"),
    }


_PPOOL_VALID_FILTERS = ("all", "free", "occupied", "assigned", "orphaned", "expired", "disabled", "external", "available")


_PPOOL_AVAILABLE_STATUSES = ("free", "orphaned")


async def _handle_proxy_pool_list_command(args: str) -> str:
    """Read-only: list every lease (optionally filtered by derived
    status). Never includes raw login/password. Never calls the
    provider.

    PROXY USAGE DETECTION 20260711: builds the manager-usage map ONCE
    (not per-lease) so "occupied" leases are correctly excluded from
    free/available, and each item carries a used_by list. filt="external"
    switches to an entirely different result shape: manager-used host+port
    pairs that have NO matching proxy_leases row at all (a "сторонний"
    proxy never bought through the pool) -- host/port/used_by only, no
    login/password beyond what each used_by entry's own manager already
    exposes about itself (still identity-only, per _ppool_usage_public)."""
    raw_filt = str(args or "").strip().lower().split()[0] if str(args or "").strip() else "all"
    filt = raw_filt if raw_filt in _PPOOL_VALID_FILTERS else "all"

    from storage import proxy_lease_list_all as _ppool_list_all
    leases = await _ppool_list_all(db_path=TPILOT_DB_PATH)
    usage_map = await _ppool_manager_usage_map()

    if filt == "external":
        lease_keys = {k for k in (_ppool_usage_key(l.get("host"), l.get("port")) for l in leases) if k}
        items = []
        for key, entries in usage_map.items():
            if key in lease_keys:
                continue
            host, port = key
            items.append({
                "host": host,
                "port": port,
                "status": "external",
                "used_by": [
                    {"manager_key": e.get("manager_key"), "display_name": e.get("display_name"),
                     "telegram_username": e.get("telegram_username"), "proxy_login": e.get("proxy_login") or ""}
                    for e in entries
                ],
            })
        items.sort(key=lambda it: (it["host"], it["port"]))
        return _pbuy_json.dumps({"ok": True, "filter": filt, "count": len(items), "items": items}, ensure_ascii=False)

    items = []
    for lease in leases:
        summary = await _ppool_lease_summary(lease, usage_map=usage_map)
        if filt == "available":
            if summary["status"] not in _PPOOL_AVAILABLE_STATUSES:
                continue
        elif filt != "all" and summary["status"] != filt:
            continue
        items.append(summary)

    return _pbuy_json.dumps({"ok": True, "filter": filt, "count": len(items), "items": items}, ensure_ascii=False)


async def _handle_proxy_pool_card_command(args: str) -> str:
    """Read-only: one lease's full card. Password is NEVER included, even
    masked -- only a has_password boolean travels through result_text;
    the UI shows "****" for True. Never calls the provider."""
    try:
        lease_id = int(str(args or "").strip().split()[0])
    except Exception:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id"}, ensure_ascii=False)

    from storage import proxy_lease_get as _ppool_lease_get
    lease = await _ppool_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease:
        return _pbuy_json.dumps({"ok": False, "error": "lease_not_found", "message": f"Lease не найден: {lease_id}"}, ensure_ascii=False)

    # PROXY USAGE DETECTION 20260711: same usage-aware status + used_by list
    # as the list command, so the card can show an "occupied" warning too.
    usage_map = await _ppool_manager_usage_map()
    status, manager_row = await _ppool_derive_status(lease, usage_map=usage_map)
    manager_key = str(lease.get("manager_key") or "").strip() or None
    usage_key = _ppool_usage_key(lease.get("host"), lease.get("port"))
    used_by = _ppool_usage_public(usage_map.get(usage_key, [])) if usage_key else []

    return _pbuy_json.dumps({
        "ok": True,
        "lease_id": lease.get("id"),
        "host": lease.get("host"),
        "port": lease.get("port"),
        "scheme": lease.get("scheme"),
        "proxy_type": lease.get("proxy_type"),
        "status": status,
        "used_by": used_by,
        "login": lease.get("login") or None,
        "has_password": bool(str(lease.get("password") or "").strip()),
        "manager_key": manager_key,
        "manager_display_name": (str(manager_row.get("display_name") or "").strip() or manager_key) if manager_row else None,
        "expires_at": lease.get("expires_at"),
        "provider_order_id": lease.get("provider_order_id"),
        "provider_order_number": lease.get("provider_order_number"),
        "provider_proxy_id": lease.get("provider_proxy_id"),
        "auto_renew_enabled": bool(int(lease.get("auto_renew_enabled") or 0)),
        "last_check_at": lease.get("last_check_at"),
        "last_check_ok": lease.get("last_check_ok"),
        "last_check_status": lease.get("last_check_status"),
        "last_renew_attempt_at": lease.get("last_renew_attempt_at"),
        "last_renew_status": lease.get("last_renew_status"),
        "created_at": lease.get("created_at"),
        # PROXY LIFECYCLE SYNC 20260721: bidirectional desired-vs-observed
        # provider auto_renew state, for the card's informational display.
        "lifecycle_status": lease.get("lifecycle_status"),
        "desired_provider_auto_renew": lease.get("desired_provider_auto_renew"),
        "observed_provider_auto_renew": lease.get("observed_provider_auto_renew"),
        "provider_sync_at": lease.get("provider_sync_at"),
    }, ensure_ascii=False)


async def _ppool_disable_manager_proxy_fields(manager_key: str) -> None:
    """Disable a manager's proxy fields after their lease is unassigned.
    Mirrors the EXISTING /manager_proxy_bypass convention (proxy_enabled=0,
    proxy_bypass_allowed=1) plus the one-time-migration's proxy_mode=
    'direct' pairing -- see _tpilot_panel_manager_proxy_bypass_command and
    the tpag_v2_mode_migration SQL. Also resets proxy_required=0 so the
    manager isn't gated on a proxy it no longer has. Does NOT clear
    proxy_host/port/username/password (same as the existing bypass
    command) -- the proxy_leases row (not these columns) is the pool's
    source of truth, and preserving them keeps a debugging trail. Never
    raises (best-effort, same convention as _tpag_registry_set_fields)."""
    await _tpag_registry_set_fields(
        manager_key,
        proxy_enabled=0,
        proxy_mode="direct",
        proxy_required=0,
        proxy_bypass_allowed=1,
        proxy_bypass_reason="pool_unassign",
        proxy_bypass_at=_now_utc_iso(),
    )


async def _handle_proxy_pool_assign_command(args: str) -> str:
    """Link an EXISTING pool lease to a manager (assign, or reassign if it
    was already linked to a different manager). Idempotent when the lease
    is already linked to the SAME manager. Never calls the provider, never
    spends, never creates/deletes a lease row.

    PROXY USAGE DETECTION 20260711: format is now
    '<lease_id> <manager_key> [multi]'. Before applying, checks whether
    this lease's host+port is actively used (proxy_enabled=1) by any live
    manager OTHER than the target and OTHER than this lease's own current
    owner (a plain reassign FROM the current owner TO someone else remains
    the existing, unmodified single-owner transfer below). Without the
    trailing 'multi' token, a real conflict refuses with error=proxy_in_use
    and a used_by list (identity only, never a password) instead of
    silently detaching/overwriting anyone. With 'multi', the caller has
    explicitly confirmed 1 proxy -> N managers: the conflicting occupant(s)
    are left completely untouched (no detach, no field clearing), and if
    the lease already had a tracked owner, that owner's link is preserved
    too (proxy_leases.manager_key / managers.proxy_lease_id are NOT
    reassigned) -- see _pbuy_apply_lease_to_manager's link_lease=False. A
    lease with NO prior owner is still linked to the new manager even under
    multi (nothing to protect by refusing to)."""
    parts = str(args or "").strip().split()
    try:
        lease_id = int(parts[0])
    except Exception:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id"}, ensure_ascii=False)
    manager_key = registry_normalize_manager_key(parts[1]) if len(parts) > 1 else ""
    if not manager_key:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой manager_key"}, ensure_ascii=False)
    multi = len(parts) > 2 and str(parts[2]).strip().lower() == "multi"

    from storage import proxy_lease_get as _ppool_lease_get, proxy_lease_unassign as _ppool_unassign_lease

    lease = await _ppool_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease:
        return _pbuy_json.dumps({"ok": False, "error": "lease_not_found", "message": f"Lease не найден: {lease_id}"}, ensure_ascii=False)

    lease_status_col = str(lease.get("status") or "").strip().lower()
    if lease_status_col and lease_status_col != "active":
        return _pbuy_json.dumps({
            "ok": False, "error": "lease_not_active",
            "message": f"Прокси #{lease_id} отключён (status={lease_status_col}) и не может быть назначен.",
        }, ensure_ascii=False)

    new_manager_row = await _tpag_registry_get(manager_key)
    if not new_manager_row:
        return _pbuy_json.dumps({"ok": False, "error": "manager_not_found", "message": f"Менеджер не найден: {manager_key}"}, ensure_ascii=False)
    if str(new_manager_row.get("status") or "").strip().lower() == "archived":
        return _pbuy_json.dumps({"ok": False, "error": "manager_archived", "message": f"Менеджер архивирован: {manager_key}"}, ensure_ascii=False)

    old_manager_key = str(lease.get("manager_key") or "").strip() or None

    # PROXY USAGE DETECTION 20260711: block on a real conflict unless the
    # caller explicitly opted into multi-assign. The lease's own CURRENT
    # owner (old_manager_key) is excluded from the conflict set -- a plain
    # reassign FROM the current owner TO someone else is the existing,
    # already-supported single-owner transfer (the old owner is cleanly
    # detached a few lines below); it is not a "proxy already used
    # elsewhere" situation. A conflict is any OTHER live manager (neither
    # the target nor the lease's own current owner) whose own proxy_*
    # fields independently point at this same host+port.
    if not multi:
        usage_key = _ppool_usage_key(lease.get("host"), lease.get("port"))
        if usage_key:
            usage_map = await _ppool_manager_usage_map()
            conflict = [
                u for u in usage_map.get(usage_key, [])
                if u.get("manager_key") != manager_key and u.get("manager_key") != old_manager_key
            ]
            if conflict:
                return _pbuy_json.dumps({
                    "ok": False, "error": "proxy_in_use",
                    "lease_id": lease_id, "manager_key": manager_key,
                    "used_by": _ppool_usage_public(conflict),
                    "message": f"Прокси #{lease_id} уже используется другим менеджером.",
                }, ensure_ascii=False)

    if old_manager_key and old_manager_key != manager_key and not multi:
        # Reassign: detach from the OLD manager first (clears both sides
        # of the old link + disables the old manager's proxy fields) only
        # if that manager still exists -- an orphaned lease (old manager
        # deleted) has nothing to detach; _pbuy_apply_lease_to_manager
        # below overwrites proxy_leases.manager_key to the new value
        # directly in that case. Skipped entirely under 'multi' -- the
        # old owner keeps using this proxy untouched.
        old_manager_row = await _tpag_registry_get(old_manager_key)
        if old_manager_row:
            await _ppool_unassign_lease(lease_id, db_path=TPILOT_DB_PATH)
            try:
                await _ppool_disable_manager_proxy_fields(old_manager_key)
            except Exception:
                pass

    # --- Stage 6 P3.1 consistency fix -----------------------------------
    # Still runs under 'multi' -- this frees the TARGET manager's own
    # stale PREVIOUS lease/proxy_lease_id, which is unrelated to the
    # shared-proxy scenario above (a manager should never be left pointing
    # at two different stale lease ids just because they're now sharing
    # someone else's proxy).
    # The TARGET manager may already have a DIFFERENT lease linked -- via
    # managers.proxy_lease_id, or via a stale proxy_leases.manager_key
    # that never got a matching proxy_lease_id (e.g. leftover/inconsistent
    # pool state). Free every such lease BEFORE linking the new one, so a
    # manager never ends up "assigned" to more than one lease at once.
    # Never touches unrelated managers; never calls the provider; never
    # deletes a lease row or clears its provider_proxy_id/host/password --
    # proxy_lease_unassign() only clears the manager link (P1 contract).
    from storage import proxy_lease_list_all as _ppool_list_all_for_cleanup

    stale_lease_ids: set = set()
    current_proxy_lease_id = new_manager_row.get("proxy_lease_id")
    if current_proxy_lease_id not in (None, ""):
        try:
            current_proxy_lease_id_i = int(current_proxy_lease_id)
            if current_proxy_lease_id_i != lease_id:
                stale_lease_ids.add(current_proxy_lease_id_i)
        except Exception:
            pass
    try:
        all_leases = await _ppool_list_all_for_cleanup(db_path=TPILOT_DB_PATH)
        for other in all_leases:
            other_id = other.get("id")
            if other_id is None or int(other_id) == lease_id:
                continue
            if str(other.get("manager_key") or "").strip() == manager_key:
                stale_lease_ids.add(int(other_id))
    except Exception:
        pass

    for stale_id in stale_lease_ids:
        try:
            await _ppool_unassign_lease(stale_id, db_path=TPILOT_DB_PATH)
        except Exception:
            pass
    # --- end Stage 6 P3.1 consistency fix --------------------------------

    # PROXY USAGE DETECTION 20260711: under multi, keep the lease's own
    # CURRENT owner unchanged (link_lease=False) only when one actually
    # exists -- a genuinely FREE lease (old_manager_key is None) has no
    # prior owner to protect, so linking the new manager as its tracked
    # owner is safe and avoids leaving the lease permanently ownerless in
    # the pool even though it is now in active use.
    link_lease = (not multi) or (old_manager_key is None)
    result = await _pbuy_apply_lease_to_manager(lease, manager_key, source="panel_pool_assign", link_lease=link_lease)
    if result.get("ok"):
        result["old_manager_key"] = old_manager_key if (old_manager_key != manager_key and not multi) else None
        result["multi_assigned"] = multi
        if stale_lease_ids:
            result["freed_lease_ids"] = sorted(stale_lease_ids)
        lease_after = await _ppool_lease_get(lease_id, db_path=TPILOT_DB_PATH)
        usage_map_after = await _ppool_manager_usage_map()
        status_after, _ = await _ppool_derive_status(lease_after or {}, usage_map=usage_map_after)
        result["status"] = status_after
    return _pbuy_json.dumps(result, ensure_ascii=False)


async def _handle_proxy_pool_unassign_command(args: str) -> str:
    """Detach a lease from its manager, keeping it in the pool. Never
    deletes the lease, never touches the provider's own proxy. Returns
    ok=True with old_manager_key even if the lease had no manager (no-op
    unassign is not an error)."""
    try:
        lease_id = int(str(args or "").strip().split()[0])
    except Exception:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id"}, ensure_ascii=False)

    from storage import proxy_lease_get as _ppool_lease_get, proxy_lease_unassign as _ppool_unassign_lease

    lease = await _ppool_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease:
        return _pbuy_json.dumps({"ok": False, "error": "lease_not_found", "message": f"Lease не найден: {lease_id}"}, ensure_ascii=False)

    old_manager_key = str(lease.get("manager_key") or "").strip() or None

    unassigned = await _ppool_unassign_lease(lease_id, db_path=TPILOT_DB_PATH)
    if not unassigned:
        return _pbuy_json.dumps({"ok": False, "error": "unassign_failed", "message": f"Не удалось отвязать lease {lease_id}."}, ensure_ascii=False)

    if old_manager_key:
        old_manager_row = await _tpag_registry_get(old_manager_key)
        if old_manager_row:
            try:
                await _ppool_disable_manager_proxy_fields(old_manager_key)
            except Exception:
                pass

    return _pbuy_json.dumps({"ok": True, "lease_id": lease_id, "old_manager_key": old_manager_key}, ensure_ascii=False)


def _ppool_extract_check_info(data: Any) -> Dict[str, Any]:
    """Best-effort extraction of validity/protocol/time/ip from a
    tools/proxy/check response. The real response shape has not been
    verified against live traffic -- defensive digging only, never
    guesses when nothing is recognized, never raises."""
    out: Dict[str, Any] = {"valid": None, "protocol": None, "time": None, "ip": None}

    def _dig(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if kl in ("valid", "is_valid", "isvalid") and out["valid"] is None and isinstance(v, bool):
                    out["valid"] = v
                elif kl in ("protocol", "proto", "scheme") and out["protocol"] is None and not isinstance(v, (dict, list)):
                    out["protocol"] = v
                elif kl in ("time", "response_time", "responsetime", "latency", "ping") and out["time"] is None and not isinstance(v, (dict, list)):
                    out["time"] = v
                elif kl in ("ip", "external_ip", "externalip", "proxy_ip", "proxyip") and out["ip"] is None and not isinstance(v, (dict, list)):
                    out["ip"] = v
                else:
                    _dig(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _dig(item)

    try:
        _dig(data)
    except Exception:
        pass
    return out


async def _handle_proxy_pool_check_command(args: str) -> str:
    """Check one pool lease's liveness with a LOCAL server-side SOCKS5
    connectivity check (read-only, no spend, no provider call) and record
    the result via storage.proxy_lease_update_check. Never buys, never
    renews. The returned JSON never includes the raw proxy string, login,
    or password -- only booleans/scrubbed text.

    LOCAL PROXY CHECK 20260711: Proxy-Seller's tools/proxy/check endpoint
    proved unreliable for our SOCKS5 leases -- live diagnosis returned
    errors=[{"message": "Empty result!", ...}] for every tested request
    shape/method, even though the proxy itself works. Replaced with the
    same local connectivity check the existing auth guard already runs
    (_tpag_v4_proxy_geo_via_socks: a real PySocks SOCKS5 connect + a plain
    HTTP GET to ip-api.com through the tunnel). Tries socks5h (rdns=True,
    DNS resolved through the proxy) first, then falls back to socks5
    (rdns=False, DNS resolved locally) -- mirrors the live diagnostic that
    proved both modes work for this provider's leases. provider.check_proxy
    is no longer called from this command at all."""
    try:
        lease_id = int(str(args or "").strip().split()[0])
    except Exception:
        return _pbuy_json.dumps({"ok": False, "error": "bad_args", "message": "Пустой или некорректный lease_id"}, ensure_ascii=False)

    from storage import proxy_lease_get as _ppool_lease_get, proxy_lease_update_check as _ppool_update_check

    lease = await _ppool_lease_get(lease_id, db_path=TPILOT_DB_PATH)
    if not lease:
        return _pbuy_json.dumps({"ok": False, "error": "lease_not_found", "message": f"Lease не найден: {lease_id}"}, ensure_ascii=False)

    host = str(lease.get("host") or "").strip()
    port = lease.get("port")
    if not host or not port:
        return _pbuy_json.dumps({"ok": False, "error": "unparseable_proxy", "message": f"У lease #{lease_id} не задан host/port."}, ensure_ascii=False)

    login = str(lease.get("login") or "").strip()
    password = str(lease.get("password") or "").strip()

    check_ok = False
    info: Dict[str, Any] = {}
    method = ""
    last_exc: Optional[Exception] = None
    for method_name, rdns in (("socks5h", True), ("socks5", False)):
        try:
            geo, ms = await _pbuy_call(_tpag_v4_proxy_geo_via_socks, host, int(port), login, password, rdns=rdns)
        except Exception as e:
            last_exc = e
            continue
        if geo.get("ip"):
            check_ok = True
            method = method_name
            info = {"protocol": method, "time": f"{ms} ms", "ip": geo.get("ip")}
            break

    if check_ok:
        status_text = f"ok, IP {info.get('ip')} ({method})"
    else:
        try:
            reason = _tpag_classify_error_reason(last_exc) if last_exc is not None else ""
        except Exception:
            reason = ""
        status_text = str(reason or "connection failed")[:200]

    try:
        await _ppool_update_check(lease_id, ok=check_ok, status_text=status_text, db_path=TPILOT_DB_PATH)
    except Exception:
        pass

    return _pbuy_json.dumps({
        "ok": True,
        "lease_id": lease_id,
        "check_ok": check_ok,
        "message": status_text,
        "protocol": info.get("protocol"),
        "time": info.get("time"),
        "ip": info.get("ip"),
    }, ensure_ascii=False)


async def _ppool_sync_entries_to_pool(entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Idempotent upsert of a provider list_proxies extraction into the
    local pool, keyed by provider_proxy_id via
    storage.proxy_lease_upsert_from_provider (P1) -- never overwrites
    manager_key/status, never deletes a local lease absent from `entries`.
    Extracted (R5c) from _handle_proxy_pool_sync_command so the periodic
    reconcile loop can refresh expires_at/host/port too (previously only a
    manual /proxy_pool_sync tap did this -- a lease that went unverified or
    otherwise fell out of sync stayed stale until an admin noticed). Pure
    storage; never calls the provider itself (caller already has `entries`)."""
    from storage import (
        proxy_lease_upsert_from_provider as _ppool_upsert,
        proxy_lease_get_by_provider_proxy_id as _ppool_get_by_pid,
    )

    created_count = 0
    updated_count = 0
    skipped_count = 0
    skipped_reasons: List[str] = []

    for entry in entries or []:
        provider_proxy_id = entry.get("provider_proxy_id")
        host = str(entry.get("host") or "").strip()
        port_raw = entry.get("port")
        if not provider_proxy_id or not host or port_raw in (None, ""):
            skipped_count += 1
            if len(skipped_reasons) < 10:
                skipped_reasons.append("пропущен: нет provider_proxy_id/host/port")
            continue
        try:
            port_i = int(str(port_raw).strip())
        except Exception:
            skipped_count += 1
            if len(skipped_reasons) < 10:
                skipped_reasons.append(f"пропущен provider_proxy_id={provider_proxy_id}: нечитаемый port")
            continue

        try:
            existing = await _ppool_get_by_pid("proxy_seller", str(provider_proxy_id), db_path=TPILOT_DB_PATH)
            await _ppool_upsert(
                provider_type="proxy_seller",
                provider_proxy_id=str(provider_proxy_id),
                host=host,
                port=port_i,
                login=entry.get("login"),
                password=entry.get("password"),
                scheme="socks5",
                proxy_type=_PBUY_PROXY_TYPE,
                provider_order_id=str(entry.get("order_id")) if entry.get("order_id") is not None else None,
                expires_at=str(entry.get("expires_at")) if entry.get("expires_at") else None,
                db_path=TPILOT_DB_PATH,
            )
            if existing:
                updated_count += 1
            else:
                created_count += 1
        except Exception as e:
            skipped_count += 1
            if len(skipped_reasons) < 10:
                skipped_reasons.append(_pbuy_safe_error(e))

    return {
        "total_seen": len(entries or []),
        "created_count": created_count,
        "updated_count": updated_count,
        "skipped_count": skipped_count,
        "skipped_reasons": skipped_reasons,
    }


async def _handle_proxy_pool_sync_command(args: str) -> str:
    """Sync the local pool from the provider's own proxy list
    (list_proxies with no order_id -> the FULL account list, read-only).
    Idempotent upsert keyed by provider_proxy_id via
    storage.proxy_lease_upsert_from_provider (P1) -- never overwrites
    manager_key/status, never deletes a local lease absent from this
    response. Never calls order/make or prolong/make; never passes
    allow_spend=True."""
    provider = _pbuy_provider()
    if provider is None:
        return _pbuy_json.dumps({"ok": False, "error": "not_configured", "message": "Proxy-Seller API key не настроен"}, ensure_ascii=False)

    try:
        lst = await _pbuy_call(provider.list_proxies, _PBUY_PROXY_TYPE)
    except Exception as e:
        return _pbuy_json.dumps({"ok": False, "error": "list_proxies_failed", "message": _pbuy_safe_error(e)}, ensure_ascii=False)

    from proxy_provider import extract_proxy_entries

    entries = extract_proxy_entries(lst)
    sync_summary = await _ppool_sync_entries_to_pool(entries)

    # PROXY LIFECYCLE SYNC 20260721: bidirectional desired-vs-observed
    # reconciliation, using the SAME provider read this sync already made
    # (no second call). Read-only against the provider; writes only local
    # lifecycle_status/desired/observed + audit + (on an actual state change)
    # an AdminBot notification. Never calls order/make or prolong/make.
    try:
        lifecycle_summary = await _plc_reconcile_all(entries)
    except Exception as e:
        lifecycle_summary = {"error": _pbuy_safe_error(e)}

    return _pbuy_json.dumps({
        "ok": True,
        "total_seen": sync_summary["total_seen"],
        "created_count": sync_summary["created_count"],
        "updated_count": sync_summary["updated_count"],
        "skipped_count": sync_summary["skipped_count"],
        "skipped_reasons": sync_summary["skipped_reasons"],
        "lifecycle": lifecycle_summary,
    }, ensure_ascii=False)
