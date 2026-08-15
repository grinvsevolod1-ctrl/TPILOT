# -*- coding: utf-8 -*-
"""Proxy-first enforcement gate.

Owner decision #5: import is FORBIDDEN for direct-mode managers; a verified
SOCKS5 proxy is mandatory, with no fallback. This module is the single place
that enforces that rule for the tdata-import flow, mirroring (but not
importing, since main.py cannot be imported standalone) the same
proxy-mandatory contract already enforced project-wide by
`_resolve_manager_telethon_proxy` (main.py:298-312) and verified for real by
`_tpag_v4_proxy_geo_via_socks` (main.py:13115) / the pool-check command
`_handle_proxy_pool_check_command` (main.py:31632, socks5h->socks5 fallback
pattern reused below for the TCP/tunnel probe itself, NOT for skipping the
proxy).

The actual SOCKS5 tunnel probe is injectable (`prober=`) so this module is
fully offline-testable; the default prober performs a REAL SOCKS5 CONNECT +
external-IP-over-HTTP check through the proxy (via PySocks, already an
optional project dependency guarded exactly like main.py:54-57 guards it).
"""
from __future__ import annotations

import http.client
import json
import socket
from dataclasses import dataclass
from typing import Callable, Optional

from .errors import DirectConnectionBlocked, ProxyMissing, ProxyUnavailable

try:
    import socks  # PySocks -- optional, guarded exactly like main.py's `import socks`
except ImportError:  # pragma: no cover
    socks = None

# Public, well-known geo-IP endpoint used purely to prove outbound HTTP works
# through the tunnel and to surface the externally-visible IP for the admin
# confirmation screen -- no credentials, no Telegram traffic.
_PROBE_HOST = "ip-api.com"
_PROBE_PATH = "/json/?fields=status,message,query"
_PROBE_TIMEOUT_SEC = 10


@dataclass(frozen=True)
class ProxyVerification:
    ip: str
    method: str  # "socks5h" | "socks5" (which DNS mode succeeded)


ProberFn = Callable[[str, int, str, str, bool], ProxyVerification]


def _real_prober(host: str, port: int, username: str, password: str, rdns: bool) -> ProxyVerification:
    """Real SOCKS5 CONNECT + external-IP-over-HTTP probe. Mirrors
    `_tpag_v4_proxy_geo_via_socks` (main.py:13115): open a PySocks socket
    through the proxy, connect to the probe host over HTTP, parse the JSON
    body for the caller's externally-visible IP. Raises ProxyUnavailable on
    any failure -- never returns a partial/unverified result."""
    if socks is None:
        raise ProxyUnavailable("PySocks not available in this environment")
    sock = socks.socksocket()
    try:
        sock.set_proxy(
            socks.SOCKS5, host, int(port), rdns,
            username or None, password or None,
        )
        sock.settimeout(_PROBE_TIMEOUT_SEC)
        sock.connect((_PROBE_HOST, 80))
        conn = http.client.HTTPConnection(_PROBE_HOST, timeout=_PROBE_TIMEOUT_SEC)
        conn.sock = sock
        conn.request("GET", _PROBE_PATH, headers={"Host": _PROBE_HOST, "Connection": "close"})
        resp = conn.getresponse()
        body = resp.read(4096)
        data = json.loads(body.decode("utf-8", errors="replace"))
        if str(data.get("status") or "").lower() != "success" or not data.get("query"):
            raise ProxyUnavailable("proxy external IP not detected")
        return ProxyVerification(ip=str(data["query"]), method="socks5h" if rdns else "socks5")
    except (OSError, socket.error, json.JSONDecodeError, ValueError) as exc:
        raise ProxyUnavailable(f"proxy probe failed: {type(exc).__name__}")
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


def assert_proxy_mode_allowed(proxy_mode: str) -> None:
    """Hard gate: the tdata-import flow accepts ONLY proxy_mode=='proxy'.
    Unlike phone/QR onboarding (which allows an explicit, password-gated
    'direct' bypass -- `wiz:add_manager:bypass:`), THIS flow has no bypass at
    all (owner decision #5). Anything other than 'proxy' is rejected."""
    if str(proxy_mode or "").strip() != "proxy":
        raise DirectConnectionBlocked(
            "tdata/session import requires a proxy-mode manager; direct connections are forbidden for this flow"
        )


def verify_proxy(host: str, port: int, username: str = "", password: str = "",
                 *, prober: Optional[ProberFn] = None) -> ProxyVerification:
    """Verify SOCKS5 connectivity + fetch the externally-visible IP through
    the tunnel. Tries socks5h (DNS via proxy) first, then socks5 (local DNS)
    as a fallback DNS MODE ONLY -- mirroring `_handle_proxy_pool_check_command`
    (main.py:31673-31683). This is never a fallback to a DIRECT connection;
    both attempts go through the same proxy host/port.

    Raises ProxyMissing if host/port aren't set, ProxyUnavailable if neither
    DNS mode succeeds."""
    h = str(host or "").strip()
    p = int(port or 0)
    if not h or p <= 0:
        raise ProxyMissing("proxy host/port not configured")

    fn = prober or _real_prober
    last_error: Optional[Exception] = None
    for rdns in (True, False):
        try:
            return fn(h, p, username or "", password or "", rdns)
        except ProxyUnavailable as exc:
            last_error = exc
            continue
    raise last_error or ProxyUnavailable("proxy verification failed")
