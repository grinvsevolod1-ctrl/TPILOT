# -*- coding: utf-8 -*-
"""proxy_provider.py -- TPilot proxy provider abstraction (Stage 2/3
foundation).

Defines a provider-agnostic interface (ProxyProvider) and a Proxy-Seller
implementation (ProxySellerProvider) on top of it. Nothing in this module
is wired into any UI/command flow yet -- purely a foundation layer.

No real network call happens unless a caller BOTH constructs a provider
with a real HTTP-capable session (or has the `requests` package installed)
AND, for the two spend-capable methods, explicitly opts in via
allow_spend=True. The default is always no-spend / dry-run.

Safety model:
  - Every method is a plain synchronous function (blocking `requests` calls
    when a real session is used). A future async caller is expected to run
    these through asyncio's executor, e.g.
        await loop.run_in_executor(None, provider.balance)
    This module deliberately does not fight that by being async itself.
  - HTTP 200 with a non-empty `errors` list is ALWAYS treated as a failure
    (raises ProxyProviderError), never as success, even if `data` also
    happens to be populated.
  - make_ipv4() and prolong_make() -- the only two methods that spend
    balance -- refuse to run (and make NO network call at all) unless
    allow_spend=True was set EITHER on the provider (constructor) OR on
    the individual call.
  - The API key lives only inside the request URL, built locally right
    before each call. It is never placed into any exception message, log
    line, or return value; as defense in depth, any text that DOES happen
    to contain the literal key (e.g. an underlying requests exception that
    embeds the URL) is scrubbed before being raised.
  - Provider responses can contain plaintext proxy login/password (e.g.
    proxy/download/ipv4, proxy/list/ipv4). Callers MUST use
    safe_log_repr()/proxy_parser.mask_proxy() before printing/logging any
    provider response -- this module never prints/logs anything itself.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional, Sequence

import proxy_parser

try:
    import requests  # type: ignore
except Exception:  # pragma: no cover - not installed in this dev copy yet
    requests = None  # type: ignore


PROXY_SELLER_BASE_TEMPLATE = "https://proxy-seller.com/personal/api/v1/{api_key}/"

_SENSITIVE_KEYS = {"password", "pass", "secret", "token", "api_key", "apikey", "authorization"}


# --- PROXY RENEWAL RELIABILITY R4 (error classification) 20260808 START ---
# Coarse, structural classification of WHERE a failure happened -- kept
# separate from the human-facing RU category mapping (main.py
# _prenew_classify_provider_error), which additionally sniffs the (already
# scrubbed) message text for finer categories like "insufficient balance"
# or "IP not found". This module only ever sets ONE of these five values;
# it never guesses a business-level reason.
PROXY_ERROR_KIND_TRANSPORT = "transport"          # network/timeout/no requests lib
PROXY_ERROR_KIND_PROVIDER_ERRORS = "provider_errors"  # envelope errors[] non-empty, or status=error/fail
PROXY_ERROR_KIND_HTTP = "http"                    # non-2xx HTTP with no errors[]/status signal
PROXY_ERROR_KIND_RESPONSE_SHAPE = "response_shape"  # non-JSON / not a dict
PROXY_ERROR_KIND_AUTH = "auth"                    # 401/403 -- API key rejected
PROXY_ERROR_KIND_SPEND_GUARD = "spend_guard"      # allow_spend was not set (no network call made)


class ProxyProviderError(Exception):
    """Business/HTTP/network failure. Message text is always safe to log --
    never contains the API key or a raw proxy password.

    `kind` (R4, 20260808) is a coarse, structural classification of WHERE
    the failure happened (see the PROXY_ERROR_KIND_* constants above).
    Optional and defaults to None for any caller that doesn't set it, so
    this stays backward compatible with existing `raise ProxyProviderError(msg)`
    call sites and with anything that catches this by type alone."""

    def __init__(self, message: str = "", *, kind: Optional[str] = None) -> None:
        super().__init__(message)
        self.kind = kind


class SpendGuardError(ProxyProviderError):
    """Raised when a spend-capable method (make_ipv4/prolong_make) is
    called without explicit permission. No network call is made when this
    is raised."""

    def __init__(self, message: str = "") -> None:
        super().__init__(message, kind=PROXY_ERROR_KIND_SPEND_GUARD)
# --- PROXY RENEWAL RELIABILITY R4 (error classification) 20260808 END ---


def _scrub_value(value: Any) -> Any:
    """Recursively scrub known-sensitive dict keys and mask any embedded
    login:password@host:port-shaped strings. Display-only -- never used to
    decide correctness."""
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for k, v in value.items():
            if str(k).strip().lower() in _SENSITIVE_KEYS:
                out[k] = "***"
            else:
                out[k] = _scrub_value(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub_value(v) for v in value]
    if isinstance(value, str):
        if "@" in value and ":" in value:
            try:
                return proxy_parser.mask_proxy(value)
            except Exception:
                return value
        return value
    return value


def safe_log_repr(data: Any) -> str:
    """Produce a string representation of ANY provider payload/response
    that is safe to print/log. Always use this instead of str(raw)/repr(raw)
    when a provider response needs to appear in a log line or admin
    message."""
    try:
        return repr(_scrub_value(data))
    except Exception:
        return "<unrepresentable response>"


# ---------------------------------------------------------------------------
# Provider-agnostic interface
# ---------------------------------------------------------------------------

class ProxyProvider(abc.ABC):
    """Provider-agnostic proxy purchasing/management interface. All methods
    are synchronous -- see module docstring for the executor-offload
    expectation."""

    @abc.abstractmethod
    def ping(self) -> Dict[str, Any]:
        """Cheap read-only call that also validates credentials/reachability."""

    @abc.abstractmethod
    def balance(self) -> Dict[str, Any]:
        """Returns a dict that includes at least {"summ": <balance>, ...}."""

    @abc.abstractmethod
    def reference_list(self, proxy_type: str = "ipv4") -> Dict[str, Any]:
        """Country IDs, period IDs, etc. for the given proxy type."""

    @abc.abstractmethod
    def calc_ipv4(
        self,
        country_id: Any,
        period_id: Any,
        quantity: int = 1,
        authorization: str = "",
        coupon: str = "",
        custom_target_name: str = "",
        payment_id: int = 1,
    ) -> Dict[str, Any]:
        """Price calculation only -- never charges balance."""

    @abc.abstractmethod
    def make_ipv4(
        self,
        country_id: Any,
        period_id: Any,
        quantity: int = 1,
        authorization: str = "",
        coupon: str = "",
        custom_target_name: str = "",
        payment_id: int = 1,
        *,
        allow_spend: bool = False,
    ) -> Dict[str, Any]:
        """Charges balance and creates an order. MUST refuse unless
        allow_spend=True (call or provider level)."""

    @abc.abstractmethod
    def list_proxies(
        self,
        proxy_type: str = "ipv4",
        order_id: Optional[str] = None,
        latest: Optional[str] = None,
        country: Optional[str] = None,
        ends: Optional[str] = None,
    ) -> Dict[str, Any]:
        ...

    @abc.abstractmethod
    def download_proxies(
        self,
        proxy_type: str = "ipv4",
        proto: str = "socks5",
        ext: str = "txt",
        country: Optional[str] = None,
        ends: Optional[str] = None,
    ) -> Dict[str, Any]:
        """May return plaintext login:password@ip:port strings -- caller
        must mask before logging."""

    @abc.abstractmethod
    def check_proxy(self, proxy_string: str) -> Dict[str, Any]:
        """proxy_string may contain a plaintext login/password -- never let
        it reach a log/exception unmasked."""

    @abc.abstractmethod
    def prolong_calc(
        self,
        proxy_type: str,
        ids: Sequence[Any],
        period_id: Any,
        payment_id: int = 1,
        coupon: str = "",
    ) -> Dict[str, Any]:
        """Renewal price calculation only -- never charges balance."""

    @abc.abstractmethod
    def prolong_make(
        self,
        proxy_type: str,
        ids: Sequence[Any],
        period_id: Any,
        payment_id: int = 1,
        coupon: str = "",
        *,
        allow_spend: bool = False,
    ) -> Dict[str, Any]:
        """Charges balance and renews proxies. MUST refuse unless
        allow_spend=True (call or provider level)."""

    @abc.abstractmethod
    def comment_set(self, ids: Sequence[Any], comment: str) -> Dict[str, Any]:
        ...


# ---------------------------------------------------------------------------
# Proxy-Seller implementation
# ---------------------------------------------------------------------------

class ProxySellerProvider(ProxyProvider):
    """Proxy-Seller.com REST API (v1) client.

    Not wired into any command/UI flow in this stage. `session` is
    injectable (any object exposing `.request(method, url, params=,
    json=, timeout=)` returning something with `.status_code` and
    `.json()`) so tests never need the real `requests` package or network
    access. If `session` is not given, the real `requests` module is used
    lazily (only at call time, not at import time) -- this module remains
    importable even when `requests` is not installed.
    """

    def __init__(
        self,
        api_key: str,
        *,
        allow_spend: bool = False,
        timeout: float = 20.0,
        session: Any = None,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise ValueError("Proxy-Seller API key is required")
        self.allow_spend = bool(allow_spend)
        self.timeout = float(timeout)
        self._session = session

    # -- internal ----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"https://proxy-seller.com/personal/api/v1/{self._api_key}/{str(path).lstrip('/')}"

    def _scrub_text(self, text: str) -> str:
        text = str(text or "")
        if self._api_key and self._api_key in text:
            text = text.replace(self._api_key, "***API_KEY***")
        return text

    def _http(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Any:
        if self._session is not None:
            caller = self._session
        elif requests is not None:
            caller = requests
        else:
            raise ProxyProviderError(
                "the 'requests' package is not installed -- cannot make a real Proxy-Seller call",
                kind=PROXY_ERROR_KIND_TRANSPORT,
            )
        url = self._url(path)
        try:
            return caller.request(method, url, params=params, json=json_body, timeout=self.timeout)
        except Exception as e:
            # A raw requests exception can embed the full URL (incl. API
            # key) in its own message -- scrub before it ever leaves here.
            raise ProxyProviderError(
                self._scrub_text(f"network error calling {path}: {type(e).__name__}: {e}"),
                kind=PROXY_ERROR_KIND_TRANSPORT,
            ) from None

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        resp = self._http(method, path, params=params, json_body=json_body)
        status_code = getattr(resp, "status_code", None)
        try:
            data = resp.json()
        except Exception:
            raise ProxyProviderError(f"non-JSON response from {path} (status {status_code})", kind=PROXY_ERROR_KIND_RESPONSE_SHAPE) from None
        if not isinstance(data, dict):
            raise ProxyProviderError(f"unexpected response shape from {path} (status {status_code})", kind=PROXY_ERROR_KIND_RESPONSE_SHAPE)
        # Business errors can arrive with HTTP 200 -- errors[] is always
        # authoritative, checked before the HTTP status code.
        errors = data.get("errors") or []
        if errors:
            # safe_log_repr() only scrubs known-sensitive dict KEYS and
            # proxy-shaped strings -- it would miss the API key if a
            # provider ever echoes it back as free text inside an errors[]
            # string (e.g. "invalid key: <key>"). Scrub by exact submitted
            # API key value too, on every _request() caller, not just
            # check_proxy()'s own dedicated scrub.
            raise ProxyProviderError(
                self._scrub_text(f"provider returned errors[] on {path}: {safe_log_repr(errors)}"),
                kind=PROXY_ERROR_KIND_PROVIDER_ERRORS,
            )
        # R4 (20260808): HTTP 200 does not by itself mean business success --
        # the envelope's own status field can say otherwise even with an
        # empty errors[]. Only an EXPLICIT failure value is authoritative
        # here (missing/unrecognized/"success"/"ok" is never treated as a
        # failure -- many real and test responses omit this field entirely).
        status_field = data.get("status")
        if isinstance(status_field, str) and status_field.strip().lower() in ("error", "fail", "failed"):
            raise ProxyProviderError(
                self._scrub_text(f"provider envelope status={status_field!r} from {path}"),
                kind=PROXY_ERROR_KIND_PROVIDER_ERRORS,
            )
        if status_code is not None and int(status_code) in (401, 403):
            raise ProxyProviderError(f"HTTP {status_code} from {path}", kind=PROXY_ERROR_KIND_AUTH)
        if status_code is not None and int(status_code) >= 400:
            raise ProxyProviderError(f"HTTP {status_code} from {path}", kind=PROXY_ERROR_KIND_HTTP)
        return data

    # -- interface -----------------------------------------------------------

    def ping(self) -> Dict[str, Any]:
        # No dedicated ping endpoint is documented; balance/get is the
        # cheapest safe read-only call and also validates the API key.
        return self.balance()

    def balance(self) -> Dict[str, Any]:
        data = self._request("GET", "balance/get")
        out = dict(data)
        try:
            out["summ"] = (data.get("data") or {}).get("summ")
        except Exception:
            out["summ"] = None
        return out

    def reference_list(self, proxy_type: str = "ipv4") -> Dict[str, Any]:
        proxy_type = str(proxy_type or "ipv4")
        return self._request("GET", f"reference/list/{proxy_type}")

    def calc_ipv4(
        self,
        country_id: Any,
        period_id: Any,
        quantity: int = 1,
        authorization: str = "",
        coupon: str = "",
        custom_target_name: str = "",
        payment_id: int = 1,
    ) -> Dict[str, Any]:
        body = {
            "countryId": country_id,
            "periodId": period_id,
            "quantity": int(quantity or 1),
            # Per project decision: generateAuth=N / authorization="" so the
            # regular login/password from proxy/list/download can be used.
            "generateAuth": "N",
            "authorization": authorization or "",
            "coupon": coupon or "",
            "customTargetName": custom_target_name or "",
            "paymentId": int(payment_id or 1),
        }
        return self._request("POST", "order/calc", json_body=body)

    def make_ipv4(
        self,
        country_id: Any,
        period_id: Any,
        quantity: int = 1,
        authorization: str = "",
        coupon: str = "",
        custom_target_name: str = "",
        payment_id: int = 1,
        *,
        allow_spend: bool = False,
    ) -> Dict[str, Any]:
        if not (self.allow_spend or allow_spend):
            raise SpendGuardError(
                "make_ipv4 blocked: this call spends real balance and creates "
                "a real order. Refusing by default -- pass allow_spend=True on "
                "the call, or set allow_spend=True on the provider, only AFTER "
                "explicit admin confirmation. No network call was made."
            )
        body = {
            "countryId": country_id,
            "periodId": period_id,
            "quantity": int(quantity or 1),
            "generateAuth": "N",
            "authorization": authorization or "",
            "coupon": coupon or "",
            "customTargetName": custom_target_name or "",
            "paymentId": int(payment_id or 1),
        }
        return self._request("POST", "order/make", json_body=body)

    def list_proxies(
        self,
        proxy_type: str = "ipv4",
        order_id: Optional[str] = None,
        latest: Optional[str] = None,
        country: Optional[str] = None,
        ends: Optional[str] = None,
    ) -> Dict[str, Any]:
        proxy_type = str(proxy_type or "ipv4")
        params: Dict[str, Any] = {}
        if order_id is not None:
            params["orderId"] = order_id
        if latest is not None:
            params["latest"] = latest
        if country is not None:
            params["country"] = country
        if ends is not None:
            params["ends"] = ends
        return self._request("GET", f"proxy/list/{proxy_type}", params=params)

    def download_proxies(
        self,
        proxy_type: str = "ipv4",
        proto: str = "socks5",
        ext: str = "txt",
        country: Optional[str] = None,
        ends: Optional[str] = None,
    ) -> Dict[str, Any]:
        proxy_type = str(proxy_type or "ipv4")
        params: Dict[str, Any] = {"proto": proto, "ext": ext}
        if country is not None:
            params["country"] = country
        if ends is not None:
            params["ends"] = ends
        return self._request("GET", f"proxy/download/{proxy_type}", params=params)

    def check_proxy(self, proxy_string: str) -> Dict[str, Any]:
        # proxy_string may contain a plaintext login/password. The generic
        # shape-based masking in safe_log_repr()/_scrub_value() only
        # recognizes strings that still look like a full
        # login:password@host:port proxy line -- if the provider echoes the
        # submitted credentials back inside errors[] as free text WITHOUT an
        # accompanying "@host:port" (e.g. "authentication rejected for
        # credentials myuser:TOPSECRETPW"), that generic pass would miss it
        # and the password would leak into the exception message. So here we
        # scrub by VALUE: parse exactly what we submitted and strip those
        # exact login/password strings out of the error text, regardless of
        # what shape they appear in.
        parsed = None
        try:
            parsed = proxy_parser.parse_proxy_line(proxy_string)
        except Exception:
            parsed = None

        def _scrub_submitted(text: str) -> str:
            text = self._scrub_text(text)
            if parsed:
                password = parsed.get("password")
                login = parsed.get("login")
                if password:
                    text = text.replace(str(password), "***")
                if login:
                    text = text.replace(str(login), "***")
            return text

        # Never put the raw proxy_string itself into an exception message --
        # only ever the provider's own (now-scrubbed) response text.
        # TRANSPORT FIX 20260711: tools/proxy/check is a GET endpoint that
        # reads `proxy` as a query parameter -- it does not read a JSON
        # body at all. Posting {"proxy": ...} as json_body left the
        # endpoint unable to find the parameter, so the provider always
        # replied errors=[{"message": "Could not find value for parameter
        # {proxy}", ...}]. The proxy_string FORMAT (login:password@host:
        # port) was already correct; only the transport was wrong.
        params = {"proxy": str(proxy_string or "")}
        try:
            return self._request("GET", "tools/proxy/check", params=params)
        except ProxyProviderError as e:
            raise ProxyProviderError(_scrub_submitted(str(e)), kind=e.kind) from None

    def prolong_calc(
        self,
        proxy_type: str,
        ids: Sequence[Any],
        period_id: Any,
        payment_id: int = 1,
        coupon: str = "",
    ) -> Dict[str, Any]:
        proxy_type = str(proxy_type or "ipv4")
        body = {
            "ids": list(ids or []),
            "periodId": period_id,
            "paymentId": int(payment_id or 1),
            "coupon": coupon or "",
        }
        return self._request("POST", f"prolong/calc/{proxy_type}", json_body=body)

    def prolong_make(
        self,
        proxy_type: str,
        ids: Sequence[Any],
        period_id: Any,
        payment_id: int = 1,
        coupon: str = "",
        *,
        allow_spend: bool = False,
    ) -> Dict[str, Any]:
        if not (self.allow_spend or allow_spend):
            raise SpendGuardError(
                "prolong_make blocked: this call spends real balance and "
                "renews real proxies. Refusing by default -- pass "
                "allow_spend=True on the call, or set allow_spend=True on "
                "the provider, only AFTER explicit admin confirmation. No "
                "network call was made."
            )
        proxy_type = str(proxy_type or "ipv4")
        body = {
            "ids": list(ids or []),
            "periodId": period_id,
            "paymentId": int(payment_id or 1),
            "coupon": coupon or "",
        }
        return self._request("POST", f"prolong/make/{proxy_type}", json_body=body)

    def comment_set(self, ids: Sequence[Any], comment: str) -> Dict[str, Any]:
        body = {"ids": list(ids or []), "comment": str(comment or "")}
        return self._request("POST", "proxy/comment/set", json_body=body)


# ---------------------------------------------------------------------------
# Stage 4 buy-flow helpers: best-effort response parsing.
#
# This project has not verified these endpoints' exact JSON shapes against
# live Proxy-Seller documentation/traffic -- these helpers are deliberately
# defensive, searching several plausible container/field-name variants
# instead of assuming one fixed shape, and NEVER raise (return None/[] on
# anything unrecognized). The caller (main.py) MUST treat a None/[] result
# as "could not determine this from the response" and show the admin a
# clear error rather than guessing. These must be validated against a real
# response before any real purchase is trusted end-to-end.
# ---------------------------------------------------------------------------

def _get_any_ci(d: Dict[str, Any], keys: Sequence[str]) -> Any:
    """Case-insensitive dict lookup across several candidate key names."""
    wanted = {str(k).lower() for k in keys}
    for real_key, value in d.items():
        if str(real_key).lower() in wanted:
            return value
    return None


def find_country_id(reference_data: Any, *, alpha3: str = "DEU", name: str = "Germany") -> Optional[Any]:
    """Search a reference_list("ipv4") response for a country entry
    matching the given ISO alpha3 code (preferred) or display name
    (fallback). Returns the country's id value, or None if not found."""
    alpha3_n = str(alpha3 or "").strip().upper()
    name_n = str(name or "").strip().lower()
    candidates: List[Dict[str, Any]] = []

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            keys_lower = {str(k).lower() for k in node.keys()}
            has_id = bool(keys_lower & {"id", "countryid", "country_id"})
            has_marker = bool(keys_lower & {"alpha3", "iso3", "code3", "name", "countryname", "title"})
            if has_id and has_marker:
                candidates.append(node)
                return  # a country entry is a leaf -- don't recurse into it
            for v in node.values():
                _collect(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _collect(item)

    try:
        _collect(reference_data)
    except Exception:
        return None

    if alpha3_n:
        for c in candidates:
            v = _get_any_ci(c, ("alpha3", "iso3", "code3"))
            if v is not None and str(v).strip().upper() == alpha3_n:
                cid = _get_any_ci(c, ("id", "countryid", "country_id"))
                if cid is not None:
                    return cid

    if name_n:
        for c in candidates:
            v = _get_any_ci(c, ("name", "countryname", "title"))
            if v is not None and str(v).strip().lower() == name_n:
                cid = _get_any_ci(c, ("id", "countryid", "country_id"))
                if cid is not None:
                    return cid

    return None


_PERIOD_NAME_ALIASES_1M = {"1 month", "month", "30 days", "30d", "1month", "30days"}


def _normalize_period_text(s: Any) -> str:
    return " ".join(str(s or "").strip().lower().split())


def find_period_id(reference_data: Any, preferred_id: str = "1m", preferred_name: str = "1 month") -> Optional[Any]:
    """Search a reference_list("ipv4") response for a period entry. The
    documented shape nests periods inside each country entry
    (data.items[].period[]), so this recurses THROUGH country-like dicts
    (unlike find_country_id, which treats them as leaves) to reach their
    nested period[] list. Also tolerates a flat/top-level period list or an
    already-unwrapped shape, since the search is fully generic. Prefers an
    exact period id match (case-insensitive), falls back to a normalized
    name match (with a small alias set for the "1 month" family: month/30
    days/30d, only applied when preferred_name is itself that family).
    Returns the period's own id value, or None if not found. Never raises."""
    preferred_id_n = str(preferred_id or "").strip().lower()
    preferred_name_n = _normalize_period_text(preferred_name)
    name_alias_set = {preferred_name_n} if preferred_name_n else set()
    if preferred_name_n in _PERIOD_NAME_ALIASES_1M or preferred_name_n == "1 month":
        name_alias_set |= _PERIOD_NAME_ALIASES_1M

    candidates: List[Dict[str, Any]] = []

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            keys_lower = {str(k).lower() for k in node.keys()}
            has_id = bool(keys_lower & {"id", "periodid", "period_id"})
            has_name = bool(keys_lower & {"name", "periodname", "title"})
            is_country_like = bool(keys_lower & {"alpha3", "iso3", "code3"})
            if has_id and has_name and not is_country_like:
                candidates.append(node)
                return  # a period entry is a leaf -- don't recurse into it
            for v in node.values():
                _collect(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _collect(item)

    try:
        _collect(reference_data)
    except Exception:
        return None

    if preferred_id_n:
        for c in candidates:
            v = _get_any_ci(c, ("id", "periodid", "period_id"))
            if v is not None and str(v).strip().lower() == preferred_id_n:
                return v

    if name_alias_set:
        for c in candidates:
            v = _get_any_ci(c, ("name", "periodname", "title"))
            if v is not None and _normalize_period_text(v) in name_alias_set:
                cid = _get_any_ci(c, ("id", "periodid", "period_id"))
                if cid is not None:
                    return cid

    return None


def extract_price_info(calc_data: Any) -> Dict[str, Any]:
    """Best-effort extraction of price/total/currency from an order/calc
    response. Values are returned exactly as the provider sent them (no
    conversion). Never raises."""
    out: Dict[str, Any] = {"price": None, "total": None, "currency": None}

    def _dig(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if kl in ("price", "priceperitem", "unitprice") and out["price"] is None and not isinstance(v, (dict, list)):
                    out["price"] = v
                elif kl in ("total", "totalprice", "sum", "summ", "amount") and out["total"] is None and not isinstance(v, (dict, list)):
                    out["total"] = v
                elif kl in ("currency", "curr") and out["currency"] is None and not isinstance(v, (dict, list)):
                    out["currency"] = v
                else:
                    _dig(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _dig(item)

    try:
        _dig(calc_data)
    except Exception:
        pass
    return out


def extract_order_info(make_data: Any) -> Dict[str, Any]:
    """Best-effort extraction of orderId/order number from an order/make
    response. Never raises."""
    out: Dict[str, Any] = {"order_id": None, "order_number": None}

    def _dig(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if kl in ("orderid", "order_id") and out["order_id"] is None and not isinstance(v, (dict, list)):
                    out["order_id"] = v
                elif kl in ("ordernumber", "order_number") and out["order_number"] is None and not isinstance(v, (dict, list)):
                    out["order_number"] = v
                elif kl == "listbaseordernumbers" and isinstance(v, list) and v and out["order_number"] is None:
                    out["order_number"] = v[0]
                else:
                    _dig(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _dig(item)

    try:
        _dig(make_data)
    except Exception:
        pass
    return out


def normalize_auto_renew(value: Any) -> str:
    """PROXY LIFECYCLE SYNC 20260721: normalize the provider's auto-renew flag
    to canonical 'Y'/'N'/'' (unknown). Proxy-Seller returns it as "Y"/"N" on
    proxy/list rows, but tolerate booleans and 1/0/on/off/true/false variants
    since the exact live shape is only defensively assumed here. Never raises.
    '' means the field was absent/unrecognized -- callers MUST treat '' as
    'unknown', never as 'N', so a parse gap can never be mistaken for a
    disabled proxy."""
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


def extract_proxy_entries(list_data: Any) -> List[Dict[str, Any]]:
    """Best-effort extraction of individual proxy records from a
    proxy/list/ipv4 response. Each dict has (when found): host, port,
    login, password, provider_proxy_id, expires_at, order_id, plus the
    PROXY LIFECYCLE SYNC 20260721 read-only reconciliation fields
    auto_renew ('Y'/'N'/'' via normalize_auto_renew), can_prolong (raw),
    auto_renew_period (raw). Never raises; returns [] if nothing
    recognizable is found. Callers must treat login/password as secret and
    never log this return value directly -- use safe_log_repr()."""
    entries: List[Dict[str, Any]] = []

    def _looks_like_proxy_row(d: Dict[str, Any]) -> bool:
        keys_lower = {str(k).lower() for k in d.keys()}
        has_host = bool(keys_lower & {"ip", "ip_only", "host"})
        has_port = bool(keys_lower & {"port_socks", "port", "socks_port", "portsocks"})
        return has_host and has_port

    def _collect(node: Any) -> None:
        if isinstance(node, dict):
            if _looks_like_proxy_row(node):
                entries.append({
                    "host": _get_any_ci(node, ("ip", "ip_only", "host")),
                    "port": _get_any_ci(node, ("port_socks", "port", "socks_port", "portsocks")),
                    "login": _get_any_ci(node, ("login", "user", "username")),
                    "password": _get_any_ci(node, ("password", "pass")),
                    "provider_proxy_id": _get_any_ci(node, ("id", "proxyid", "proxy_id")),
                    "expires_at": _get_any_ci(node, ("date_end", "dateend", "expire", "expiresat", "expires_at")),
                    "order_id": _get_any_ci(node, ("orderid", "order_id")),
                    # Read-only provider auto-renew state (never written back --
                    # Proxy-Seller has no API to set it; this is for detecting a
                    # desired-vs-observed mismatch only).
                    "auto_renew": normalize_auto_renew(
                        _get_any_ci(node, ("auto_renew", "autorenew", "auto_prolong", "autoprolong", "is_prolong", "isprolong"))
                    ),
                    "can_prolong": _get_any_ci(node, ("can_prolong", "canprolong")),
                    "auto_renew_period": _get_any_ci(node, ("auto_renew_period", "autorenewperiod", "auto_prolong_period")),
                })
                return  # a proxy row is a leaf -- don't also recurse into it
            for v in node.values():
                _collect(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _collect(item)

    try:
        _collect(list_data)
    except Exception:
        pass
    return entries


def extract_download_lines(download_data: Any) -> List[str]:
    """Best-effort extraction of raw proxy-line strings (e.g.
    "login:password@ip:port") from a proxy/download/ipv4 response. Never
    raises; returns []. Callers must never log the returned lines
    directly -- use proxy_parser.mask_proxy() first."""
    lines: List[str] = []

    def _collect(node: Any) -> None:
        if isinstance(node, str):
            for ln in node.splitlines():
                ln = ln.strip()
                if ln and "@" in ln and ":" in ln:
                    lines.append(ln)
        elif isinstance(node, dict):
            for v in node.values():
                _collect(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _collect(item)

    try:
        _collect(download_data)
    except Exception:
        pass
    return lines


def extract_renewal_info(data: Any) -> Dict[str, Any]:
    """Best-effort extraction of price/total/currency (from a prolong/calc
    response) and the new expiry date (from a prolong/make response) --
    combined into one helper since Stage 5 uses the same shape-search for
    both. Never raises. Any key not found stays None."""
    out: Dict[str, Any] = {"price": None, "total": None, "currency": None, "new_expires_at": None}

    def _dig(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                kl = str(k).lower()
                if kl in ("price", "priceperitem", "unitprice") and out["price"] is None and not isinstance(v, (dict, list)):
                    out["price"] = v
                elif kl in ("total", "totalprice", "sum", "summ", "amount") and out["total"] is None and not isinstance(v, (dict, list)):
                    out["total"] = v
                elif kl in ("currency", "curr") and out["currency"] is None and not isinstance(v, (dict, list)):
                    out["currency"] = v
                elif kl in ("date_end", "dateend", "expire", "expiresat", "expires_at") and out["new_expires_at"] is None and not isinstance(v, (dict, list)):
                    out["new_expires_at"] = v
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
