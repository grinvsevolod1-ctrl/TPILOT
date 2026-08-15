# -*- coding: utf-8 -*-
"""Offline selftest for login_code_parser.parse_login_code.

Pure/offline: no network, no Telegram, no DB. Proves the strict 5-digit
login-code parser accepts genuine RU/EN service messages (leading zeros
preserved) and rejects phone numbers, IPs, dates, times, URLs, order/device
numbers, over-long text, and ambiguous multi-code messages.

Run:  python tools\\devlogin_parser_selftest.py
"""
from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from login_code_parser import MAX_TEXT_LEN, parse_login_code  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


class FakeEntity:
    def __init__(self, name, offset, length):
        self.__class__.__name__ = name  # not used; see subclasses below
        self.offset = offset
        self.length = length


class MessageEntityCode:
    def __init__(self, offset, length):
        self.offset = offset
        self.length = length


class MessageEntityPre:
    def __init__(self, offset, length):
        self.offset = offset
        self.length = length


POSITIVE = [
    ("RU canonical", "Код для входа: 12345. Никому не сообщайте этот код.", "12345"),
    ("RU short", "Ваш код: 05123", "05123"),
    ("RU leading zeros", "Код подтверждения: 00042", "00042"),
    ("EN canonical", "Login code: 54321. Do not give this code to anyone.", "54321"),
    ("EN long warning", "Login code: 05001. Do not give this code to anyone, even if they claim to be from Telegram!", "05001"),
    ("EN generic", "Your Telegram login code is 90007", "90007"),
    ("RU code end-of-string", "Никому не сообщайте код 24680", "24680"),
]

NEGATIVE = [
    ("phone", "+79991234567"),
    ("phone with code word nearby", "Ваш код придёт на +79991234567 сейчас"),
    ("ipv4", "Your server code path is 192.168.0.1 now (code)"),
    ("time", "Login at 13:45 today, code coming"),
    ("date", "Дата код-релиза 01.02.2026 важна"),
    ("url", "Открой ссылку для входа https://t.me/joinchat/12345"),
    ("order number no context", "Заказ 12345 на 5 позиций готов"),
    ("empty", ""),
    ("ambiguous two codes", "код 12345 и код 67890 — два разных"),
    ("no context word", "Просто число 12345 без смысла"),
    ("four digits", "Код: 1234"),
    ("six digits glued", "Код: 123456"),
]


def main():
    for label, text, want in POSITIVE:
        got = parse_login_code(text)
        check(f"POS {label}: -> {want}", got == want, detail=f"got={got!r}")

    for label, text in NEGATIVE:
        got = parse_login_code(text)
        check(f"NEG {label}: -> None", got is None, detail=f"got={got!r}")

    # leading zero preserved as a STRING (not int-coerced)
    got = parse_login_code("Ваш код: 00042")
    check("leading zero is a 5-char string", got == "00042" and isinstance(got, str))

    # over-long text rejected even with context + a clean code
    big = ("код " * 200) + "12345"
    check("over-MAX_TEXT_LEN rejected", len(big) > MAX_TEXT_LEN and parse_login_code(big) is None)

    # entity-guided: a MessageEntityCode span pointing at the 5 digits is honored
    txt = "Login code below\n54321\nkeep it secret"
    off = txt.index("54321")
    got = parse_login_code(txt, entities=[MessageEntityCode(off, 5)])
    check("entity MessageEntityCode span honored", got == "54321", detail=f"got={got!r}")

    # entity pointing at a non-5-digit / masked span is ignored, falls back to text scan
    txt2 = "Your login code is 77777 now"
    off2 = txt2.index("77777")
    got2 = parse_login_code(txt2, entities=[MessageEntityPre(off2, 5)])
    check("entity pre span honored when 5 digits", got2 == "77777")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN PARSER SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
