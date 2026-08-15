# -*- coding: utf-8 -*-
"""tools/proxy_parser_selftest.py -- offline self-test for proxy_parser.py.

Pure, no network calls, no DB, no Telegram. Safe to run any time:

    python3.12 tools\\proxy_parser_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import proxy_parser as pp

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def main() -> int:
    # ------------------------------------------------------------------
    # 1. login:password@host:port
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("login:password@77.90.178.11:50101")
    check(
        "login:password@host:port",
        bool(r) and r["host"] == "77.90.178.11" and r["port"] == 50101
        and r["login"] == "login" and r["password"] == "password"
        and r["scheme"] == "socks5",
        repr(r),
    )

    # ------------------------------------------------------------------
    # 2. socks5://login:password@host:port
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("socks5://login:password@77.90.178.11:50101")
    check(
        "socks5://login:password@host:port",
        bool(r) and r["scheme"] == "socks5" and r["host"] == "77.90.178.11"
        and r["port"] == 50101 and r["login"] == "login" and r["password"] == "password",
        repr(r),
    )

    # ------------------------------------------------------------------
    # 3. host:port:login:password
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("77.90.178.11:50101:login:password")
    check(
        "host:port:login:password",
        bool(r) and r["host"] == "77.90.178.11" and r["port"] == 50101
        and r["login"] == "login" and r["password"] == "password",
        repr(r),
    )

    # ------------------------------------------------------------------
    # 4. host:port@login:password
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("77.90.178.11:50101@login:password")
    check(
        "host:port@login:password",
        bool(r) and r["host"] == "77.90.178.11" and r["port"] == 50101
        and r["login"] == "login" and r["password"] == "password",
        repr(r),
    )

    # ------------------------------------------------------------------
    # 5. host:port (no credentials)
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("77.90.178.11:50101")
    check(
        "host:port (no credentials)",
        bool(r) and r["host"] == "77.90.178.11" and r["port"] == 50101
        and r["login"] is None and r["password"] is None and r["scheme"] == "socks5",
        repr(r),
    )

    # ------------------------------------------------------------------
    # 6. socks5://host:port
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("socks5://77.90.178.11:50101")
    check(
        "socks5://host:port",
        bool(r) and r["scheme"] == "socks5" and r["host"] == "77.90.178.11" and r["port"] == 50101,
        repr(r),
    )

    # ------------------------------------------------------------------
    # extra: http:// and https:// schemes (explicitly required formats)
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("http://login:password@77.90.178.11:50101")
    check("http://login:password@host:port", bool(r) and r["scheme"] == "http", repr(r))

    r = pp.parse_proxy_line("https://login:password@77.90.178.11:50101")
    check("https://login:password@host:port", bool(r) and r["scheme"] == "https", repr(r))

    # ------------------------------------------------------------------
    # 7. multiple lines (bulk)
    # ------------------------------------------------------------------
    bulk_text = "\n".join([
        "login:password@77.90.178.11:50101",
        "",  # empty line must be ignored
        "  socks5://5.6.7.8:1081  ",  # spaces around line
        "9.9.9.9:1082:u:p",
    ])
    bulk = pp.parse_proxy_bulk(bulk_text)
    check(
        "multiple lines: parses 3 valid, ignores empty line",
        len(bulk) == 3
        and bulk[0]["host"] == "77.90.178.11"
        and bulk[1]["host"] == "5.6.7.8" and bulk[1]["port"] == 1081
        and bulk[2]["login"] == "u" and bulk[2]["password"] == "p",
        repr(bulk),
    )

    # ------------------------------------------------------------------
    # 8. invalid port
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line("77.90.178.11:999999")
    check("invalid port (out of range) -> None", r is None, repr(r))

    r = pp.parse_proxy_line("77.90.178.11:notaport")
    check("invalid port (non-numeric) -> None", r is None, repr(r))

    ok, err = pp.validate_proxy_fields("77.90.178.11", 999999, "socks5")
    check("validate_proxy_fields rejects out-of-range port", ok is False and bool(err), repr((ok, err)))

    # ------------------------------------------------------------------
    # 9. empty host
    # ------------------------------------------------------------------
    r = pp.parse_proxy_line(":50101")
    check("empty host -> None", r is None, repr(r))

    ok, err = pp.validate_proxy_fields("", 50101, "socks5")
    check("validate_proxy_fields rejects empty host", ok is False and bool(err), repr((ok, err)))

    # empty/whitespace-only input
    check("empty string -> None", pp.parse_proxy_line("") is None)
    check("whitespace-only string -> None", pp.parse_proxy_line("   ") is None)

    # ------------------------------------------------------------------
    # 10. masking does not expose password
    # ------------------------------------------------------------------
    masked = pp.mask_proxy("login:supersecret@77.90.178.11:50101")
    check(
        "mask_proxy hides password (userinfo@host:port)",
        masked == "login:***@77.90.178.11:50101" and "supersecret" not in masked,
        masked,
    )

    masked2 = pp.mask_proxy("socks5://login:supersecret@77.90.178.11:50101")
    check(
        "mask_proxy hides password (scheme + userinfo@host:port)",
        masked2 == "socks5://login:***@77.90.178.11:50101" and "supersecret" not in masked2,
        masked2,
    )

    masked3 = pp.mask_proxy("77.90.178.11:50101:login:supersecret")
    check(
        "mask_proxy hides password (host:port:login:password)",
        masked3 == "77.90.178.11:50101:login:***" and "supersecret" not in masked3,
        masked3,
    )

    masked4 = pp.mask_proxy("77.90.178.11:50101@login:supersecret")
    check(
        "mask_proxy hides password (host:port@login:password)",
        masked4 == "77.90.178.11:50101@login:***" and "supersecret" not in masked4,
        masked4,
    )

    rec = pp.parse_proxy_line("login:supersecret@77.90.178.11:50101")
    check(
        "parsed record's raw_masked field hides password",
        bool(rec) and "supersecret" not in rec["raw_masked"] and "***" in rec["raw_masked"],
        repr(rec.get("raw_masked") if rec else None),
    )

    # mask_proxy on a parsed dict (not just raw text)
    masked5 = pp.mask_proxy(rec)
    check(
        "mask_proxy(dict) hides password",
        "supersecret" not in masked5 and "***" in masked5,
        masked5,
    )

    # ------------------------------------------------------------------
    # extra: build_telethon_proxy shape
    # ------------------------------------------------------------------
    tp = pp.build_telethon_proxy(pp.parse_proxy_line("login:password@1.2.3.4:1080"))
    check(
        "build_telethon_proxy returns expected Telethon proxy dict shape",
        tp == {
            "proxy_type": "socks5",
            "addr": "1.2.3.4",
            "port": 1080,
            "username": "login",
            "password": "password",
            "rdns": True,
        },
        repr(tp),
    )

    tp_none = pp.build_telethon_proxy(None)
    check("build_telethon_proxy(None) -> None", tp_none is None)

    # ------------------------------------------------------------------
    # extra: validate_proxy_fields scheme allowlist
    # ------------------------------------------------------------------
    ok, _ = pp.validate_proxy_fields("1.2.3.4", 1080, "socks5")
    check("validate_proxy_fields accepts socks5", ok is True)
    ok, _ = pp.validate_proxy_fields("1.2.3.4", 1080, "http")
    check("validate_proxy_fields accepts http", ok is True)
    ok, _ = pp.validate_proxy_fields("1.2.3.4", 1080, "https")
    check("validate_proxy_fields accepts https", ok is True)
    ok, err = pp.validate_proxy_fields("1.2.3.4", 1080, "ftp")
    check("validate_proxy_fields rejects unknown scheme", ok is False and bool(err), repr((ok, err)))

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
