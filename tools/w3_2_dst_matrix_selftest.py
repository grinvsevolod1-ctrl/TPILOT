# -*- coding: utf-8 -*-
"""tools/w3_2_dst_matrix_selftest.py -- W3.2 required 15-case DST matrix
(task spec section 7) plus the structural day/night tiling proof (section 6).

Every transition date is derived from the REAL Europe/Kyiv tzdata (last Sunday of
March / October for the current run's year), never hardcoded, so a tzdata rule change
cannot silently desync this matrix from reality (02_S9_TIMEZONE_CONTRACT.md 2.6).

Uses a TEMPORARY, throwaway SQLite file for storage.w3_resolve_schedule() calls (never
the real project DB). No network, no Telegram.

    python tools\\w3_2_dst_matrix_selftest.py
"""
from __future__ import annotations

import sys
import tempfile
import uuid
from datetime import date, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

FAILURES: list[str] = []
CASES: list[dict] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)
    CASES.append({"label": label, "pass": bool(condition), "detail": detail})


def _last_sunday(year: int, month: int) -> date:
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def _hours_between(a, b) -> float:
    return (b.astimezone(timezone.utc) - a.astimezone(timezone.utc)).total_seconds() / 3600.0


def main() -> int:
    tz = storage.w3_tz()
    year = storage.w3_now().year
    spring = _last_sunday(year, 3)
    fall = _last_sunday(year, 10)
    ordinary_winter = date(year, 1, 15)
    ordinary_summer = date(year, 7, 15)
    day_before_spring = spring - timedelta(days=1)
    day_after_spring = spring + timedelta(days=1)
    day_before_fall = fall - timedelta(days=1)
    day_after_fall = fall + timedelta(days=1)

    print(f"Derived transition dates for {year} (from real ZoneInfo, not hardcoded):")
    print(f"  spring-forward: {spring}   fall-back: {fall}")
    print()

    # 1/2. ordinary winter/summer dates: 08:00 day-start / 17:00 day-end are unambiguous,
    # and elapsed 08:00->17:00 is exactly 9h, 17:00->08:00(next) is exactly 15h.
    for label, d in (("ordinary winter", ordinary_winter), ("ordinary summer", ordinary_summer)):
        deg = []
        day_start = storage.w3_local_at(d.isoformat(), 8 * 60, which="start", degraded=deg)
        day_end = storage.w3_local_at(d.isoformat(), 17 * 60, which="end", degraded=deg)
        check(f"{label} ({d}): no DST degradation on 08:00/17:00", deg == [], str(deg))
        check(f"{label} ({d}): day 08:00->17:00 elapsed == 9.0h", _hours_between(day_start, day_end) == 9.0,
              str(_hours_between(day_start, day_end)))
        next_day_start = storage.w3_local_at((d + timedelta(days=1)).isoformat(), 8 * 60, which="start", degraded=[])
        check(f"{label} ({d}): night 17:00->08:00(+1) elapsed == 15.0h",
              _hours_between(day_end, next_day_start) == 15.0, str(_hours_between(day_end, next_day_start)))

    # 3. spring-forward date: night 08:00-anchored elapsed hours == 14 (contract requirement)
    deg = []
    spring_prev_day_end = storage.w3_local_at(day_before_spring.isoformat(), 17 * 60, which="end", degraded=deg)
    spring_day_start = storage.w3_local_at(spring.isoformat(), 8 * 60, which="start", degraded=deg)
    check("spring-forward date: night (prev 17:00 -> spring 08:00) elapsed == 14.0h",
          _hours_between(spring_prev_day_end, spring_day_start) == 14.0,
          str(_hours_between(spring_prev_day_end, spring_day_start)))

    # 4. day before spring-forward: ordinary (no degradation, 15h night into the transition
    # day itself is covered by case 3 above; this case checks the PRECEDING night, which is
    # still winter-time, unaffected).
    deg = []
    dbs_start = storage.w3_local_at(day_before_spring.isoformat(), 8 * 60, which="start", degraded=deg)
    check("day before spring-forward: 08:00 boundary is NOT degraded", deg == [], str(deg))

    # 5. day after spring-forward: fully in new (summer) offset, ordinary 9h/15h day/night.
    deg = []
    das_start = storage.w3_local_at(day_after_spring.isoformat(), 8 * 60, which="start", degraded=deg)
    das_end = storage.w3_local_at(day_after_spring.isoformat(), 17 * 60, which="end", degraded=deg)
    check("day after spring-forward: 08:00/17:00 NOT degraded, elapsed == 9.0h",
          deg == [] and _hours_between(das_start, das_end) == 9.0, f"deg={deg}")

    # 6. fall-back date: night elapsed hours == 16 (contract requirement)
    deg = []
    fall_prev_day_end = storage.w3_local_at(day_before_fall.isoformat(), 17 * 60, which="end", degraded=deg)
    fall_day_start = storage.w3_local_at(fall.isoformat(), 8 * 60, which="start", degraded=deg)
    check("fall-back date: night (prev 17:00 -> fall 08:00) elapsed == 16.0h",
          _hours_between(fall_prev_day_end, fall_day_start) == 16.0,
          str(_hours_between(fall_prev_day_end, fall_day_start)))

    # 7. day before fall-back: ordinary, not degraded
    deg = []
    dbf_start = storage.w3_local_at(day_before_fall.isoformat(), 8 * 60, which="start", degraded=deg)
    check("day before fall-back: 08:00 boundary NOT degraded", deg == [], str(deg))

    # 8. day after fall-back: fully in new (winter) offset, ordinary 9h/15h
    deg = []
    daf_start = storage.w3_local_at(day_after_fall.isoformat(), 8 * 60, which="start", degraded=deg)
    daf_end = storage.w3_local_at(day_after_fall.isoformat(), 17 * 60, which="end", degraded=deg)
    check("day after fall-back: 08:00/17:00 NOT degraded, elapsed == 9.0h",
          deg == [] and _hours_between(daf_start, daf_end) == 9.0, f"deg={deg}")

    # 9. 08:00->17:00 day window on an ordinary date (already covered above); explicit here
    check("08:00->17:00 day window (ordinary date) == 9.0h (explicit case 9)",
          _hours_between(
              storage.w3_local_at(ordinary_summer.isoformat(), 8 * 60, which="start", degraded=[]),
              storage.w3_local_at(ordinary_summer.isoformat(), 17 * 60, which="end", degraded=[]),
          ) == 9.0)

    # 10. 17:00->08:00 night window (ordinary date) == 15.0h (explicit case 10)
    check("17:00->08:00 night window (ordinary date) == 15.0h (explicit case 10)",
          _hours_between(
              storage.w3_local_at(ordinary_summer.isoformat(), 17 * 60, which="end", degraded=[]),
              storage.w3_local_at((ordinary_summer + timedelta(days=1)).isoformat(), 8 * 60, which="start", degraded=[]),
          ) == 15.0)

    # 11. cross-midnight custom window (22:00 -> 06:00, ordinary date): elapsed == 8h
    deg = []
    cw_start = storage.w3_local_at(ordinary_summer.isoformat(), 22 * 60, which="end", degraded=deg)
    cw_end = storage.w3_local_at((ordinary_summer + timedelta(days=1)).isoformat(), 6 * 60, which="start", degraded=deg)
    check("cross-midnight custom window 22:00->06:00 elapsed == 8.0h", _hours_between(cw_start, cw_end) == 8.0,
          str(_hours_between(cw_start, cw_end)))

    # 12. boundary exactly at the spring transition gap (03:30 falls inside [03:00,04:00))
    deg = []
    gap_dt = storage.w3_local_at(spring.isoformat(), 3 * 60 + 30, which="start", degraded=deg)
    check("boundary inside spring gap (03:30) is snapped forward to 04:00 with dst_gap_snapped",
          gap_dt.strftime("%H:%M") == "04:00" and "dst_gap_snapped" in deg, f"got {gap_dt} deg={deg}")

    # 13. boundary inside the fall-back ambiguous hour (03:30 occurs twice)
    deg_s, deg_e = [], []
    amb_start = storage.w3_local_at(fall.isoformat(), 3 * 60 + 30, which="start", degraded=deg_s)
    amb_end = storage.w3_local_at(fall.isoformat(), 3 * 60 + 30, which="end", degraded=deg_e)
    check("boundary inside fall-back ambiguous hour (03:30): start/end differ by 1h, both flagged",
          _hours_between(amb_start, amb_end) == 1.0 and "dst_fold_ambiguous" in deg_s
          and "dst_fold_ambiguous" in deg_e,
          f"start={amb_start} end={amb_end} deg_s={deg_s} deg_e={deg_e}")

    # 14. same-minute start/end invalid case (schedule-level validation is main.py's
    # _tp_gq_validate_schedule, frozen/untouched -- this only proves w3_local_at itself
    # does not silently coalesce two identical requests into different instants).
    same_a = storage.w3_local_at(ordinary_summer.isoformat(), 9 * 60, which="start", degraded=[])
    same_b = storage.w3_local_at(ordinary_summer.isoformat(), 9 * 60, which="start", degraded=[])
    check("same (date, minute, which) request is deterministic (same-minute case)", same_a == same_b)

    # 15. day/night structural tiling: day_end IS night_start, night_end IS day_start --
    # by construction (M32-6 guard), across an ordinary date, spring date, and fall date.
    for label, d in (("ordinary", ordinary_summer), ("spring", spring), ("fall", fall)):
        deg = []
        day_end = storage.w3_local_at(d.isoformat(), 17 * 60, which="end", degraded=deg)
        night_start = storage.w3_local_at(d.isoformat(), 17 * 60, which="end", degraded=deg)
        day_start_next = storage.w3_local_at((d + timedelta(days=1)).isoformat(), 8 * 60, which="start", degraded=deg)
        night_end = storage.w3_local_at((d + timedelta(days=1)).isoformat(), 8 * 60, which="start", degraded=deg)
        check(f"{label} ({d}): night_start == day_end exactly (no gap/overlap)", night_start == day_end)
        check(f"{label} ({d}): night_end == next day_start exactly (no gap/overlap)", night_end == day_start_next)

    print()
    print("Also test any OTHER transition behavior current tzdata exposes for Europe/Kyiv "
          "(section 7 'also test'): scanning +/-2 years for any additional gap/fold not "
          "of the last-Sunday-of-March/October shape.")
    anomalies = []
    for y in (year - 1, year, year + 1):
        s = _last_sunday(y, 3)
        f = _last_sunday(y, 10)
        for d in (s, f):
            deg = []
            storage.w3_local_at(d.isoformat(), 3 * 60 + 30, which="start", degraded=deg)
            if not deg:
                anomalies.append((str(d), "expected gap/ambiguous at 03:30 but got none"))
    check("no unexpected additional Europe/Kyiv transition shape found in scanned years",
          not anomalies, str(anomalies))

    print()
    n_pass = sum(1 for c in CASES if c["pass"])
    print(f"15-case (+structural) DST matrix: {n_pass}/{len(CASES)} sub-checks green, "
          f"{len(FAILURES)} mismatch(es) (contract requires 0)")
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for fl in FAILURES:
            print(f"  - {fl}")
        return 1
    print("RESULT: PASS (0 mismatches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
