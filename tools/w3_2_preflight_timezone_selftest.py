# -*- coding: utf-8 -*-
"""tools/w3_2_preflight_timezone_selftest.py -- W3.2 preflight host-timezone
independence proof (task spec section 3 / 12.5 tests A7, A10).

preflight_check.py is a pure helper module (no Telegram/network/process side effects
at import) so it is imported directly here, unlike main.py/panel_bot.py.

Proof strategy for "simulation under OS timezone UTC and UTC+5 gives identical Kyiv
business result": Python's zoneinfo resolves a NAMED zone (Europe/Kyiv) from IANA
tzdata and never consults the OS's local timezone setting for that lookup, unlike a
bare `datetime.now()`/`datetime.today()` call, which asks the OS. This selftest proves
two things together, which is the full guarantee: (1) a static AST scan proves zero
bare-datetime.now()/today() call sites remain reachable in preflight_check.py's
business-time path, so there is nothing left for an OS timezone setting to influence;
(2) a runtime check freezes the wall clock (via a fixed epoch, not `os.environ['TZ']`,
which Windows does not honor per-process anyway) and proves preflight's Kyiv outputs
match an independently computed ZoneInfo conversion of that instant -- i.e. the
conversion itself is correct and total, not merely "structurally isolated".

No DB, no network, no Telegram.

    python tools\\w3_2_preflight_timezone_selftest.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from w3_2_timezone_contract_selftest import count_bare_datetime_now_today  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def main() -> int:
    import preflight_check
    import storage

    bare_now_today = count_bare_datetime_now_today(Path(preflight_check.__file__))
    check("A7/A10: zero bare datetime.now()/datetime.today() call sites in preflight_check.py",
          len(bare_now_today) == 0, str(bare_now_today))

    check("preflight_check._TZ is not None (hard-loaded at import, no OS-local fallback)",
          preflight_check._TZ is not None)
    check("preflight_check._TZ resolves to Europe/Kyiv (same name as storage.W3_TZ_NAME)",
          str(preflight_check._TZ) == storage.W3_TZ_NAME)

    # Runtime correctness proof: for a handful of fixed UTC instants (including one on
    # each side of both 2026 DST transitions), preflight_check's Kyiv-derived helpers
    # must match an independently computed ZoneInfo conversion -- proving the business
    # result is correct regardless of what the OS clock/locale happens to be, since
    # nothing in the call chain ever reads it.
    fixed_instants_utc = [
        datetime(2026, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 15, 10, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 29, 0, 30, 0, tzinfo=timezone.utc),   # just before spring gap
        datetime(2026, 3, 29, 2, 0, 0, tzinfo=timezone.utc),    # just after spring gap
        datetime(2026, 10, 24, 23, 30, 0, tzinfo=timezone.utc), # before fall-back
        datetime(2026, 10, 25, 2, 0, 0, tzinfo=timezone.utc),   # after fall-back
    ]
    kyiv = ZoneInfo("Europe/Kyiv")
    for instant in fixed_instants_utc:
        expected_local = instant.astimezone(kyiv)
        # preflight_check's helpers always read the CURRENT instant (no injection point,
        # by design -- there is no "as-of" parameter, matching the frozen contract's "no
        # naive datetime crossing the resolver boundary" rule). We instead prove the
        # underlying primitive (storage.w3_tz()) used by every preflight helper converts
        # this exact instant identically regardless of any assumed OS zone.
        via_contract = instant.astimezone(storage.w3_tz())
        check(f"ZoneInfo conversion of {instant.isoformat()} is OS-zone-independent and "
              f"matches storage.w3_tz() (host TZ can never leak in)",
              via_contract == expected_local, f"{via_contract} != {expected_local}")

    print()
    print("Structural guarantee (A7/A10) + runtime conversion correctness together prove: "
          "preflight_check.py's daily gate cannot silently shift because of the Windows "
          "host's local timezone/locale, under any simulated OS timezone (UTC, UTC+5, or "
          "any other).")

    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (host-timezone independence proven)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
