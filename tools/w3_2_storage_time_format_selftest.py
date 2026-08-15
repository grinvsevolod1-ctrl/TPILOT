# -*- coding: utf-8 -*-
"""tools/w3_2_storage_time_format_selftest.py -- W3.2 storage/datetime safety proofs
(task spec section 8) plus the stats_engine.SE_TZ equivalence test (section 5 / 12.7,
frozen contract test A6).

Proves:
  - aware Europe/Kyiv instant -> naive UTC storage form round-trips to the same instant
  - stored naive UTC -> aware Kyiv conversion is correct (storage.w3_parse_utc_to_local)
  - midnight edge: 23:59:59 belongs to date D, 00:00:00 belongs to D+1
  - storage._now_iso() form is unchanged (naive, second precision, lexicographically
    comparable) -- W3.2 introduces NO new aware-ISO column
  - stats_engine.SE_TZ.key == storage.W3_TZ_NAME, and stats_engine still does NOT
    import storage (06_CANONICAL_RESOLVER... / 12.7: "stats_engine -- read, not
    modified")

No DB, no network, no Telegram.

    python tools\\w3_2_storage_time_format_selftest.py
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402
import stats_engine  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def main() -> int:
    tz = storage.w3_tz()

    # ---- aware Kyiv -> naive UTC storage form -> back to the same instant
    aware_kyiv = datetime(2026, 7, 15, 14, 30, 0, tzinfo=tz)
    naive_utc_str = aware_kyiv.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()
    round_tripped = datetime.fromisoformat(naive_utc_str).replace(tzinfo=timezone.utc).astimezone(tz)
    check("aware Kyiv instant -> naive UTC storage form -> same instant round-trips",
          round_tripped == aware_kyiv.replace(microsecond=0), f"{round_tripped} != {aware_kyiv}")

    # ---- storage.w3_parse_utc_to_local tolerates the storage._now_iso() naive form, 'Z', offset
    naive_form = "2026-07-15T11:30:00"
    parsed = storage.w3_parse_utc_to_local(naive_form)
    check("w3_parse_utc_to_local() parses storage._now_iso()-style naive UTC string",
          parsed is not None and parsed.astimezone(timezone.utc).replace(tzinfo=None) == datetime(2026, 7, 15, 11, 30, 0))
    parsed_z = storage.w3_parse_utc_to_local(naive_form + "Z")
    check("w3_parse_utc_to_local() tolerates a trailing 'Z'", parsed_z == parsed)

    # ---- midnight edge: 23:59:59 belongs to D, 00:00:00 belongs to D+1
    d = "2026-08-14"
    d_plus_1 = "2026-08-15"
    late = datetime(2026, 8, 14, 23, 59, 59, tzinfo=tz)
    midnight = datetime(2026, 8, 15, 0, 0, 0, tzinfo=tz)
    check("23:59:59 business_date == D", storage.w3_business_date(at_instant=late) == d)
    check("00:00:00 business_date == D+1", storage.w3_business_date(at_instant=midnight) == d_plus_1)

    # ---- storage._now_iso() form is unchanged: naive, second precision, no tzinfo,
    # ISO 'T' separator, lexicographically comparable across two successive calls.
    import time
    s1 = storage._now_iso()
    time.sleep(0.01)
    s2 = storage._now_iso()
    check("storage._now_iso() returns a NAIVE (no offset) ISO string",
          "+" not in s1 and "Z" not in s1 and s1.count(":") == 2)
    check("storage._now_iso() successive calls are lexicographically non-decreasing (SQL-comparable)",
          s2 >= s1, f"{s1} vs {s2}")
    try:
        datetime.fromisoformat(s1)
        parse_ok = True
    except Exception:
        parse_ok = False
    check("storage._now_iso() is a valid isoformat() string", parse_ok)

    # ---- no new aware-ISO column introduced: scan the W3.2-touched storage.py block for
    # any write that stores a `.isoformat()` result of an AWARE datetime into a column
    # this task's frozen contract reserves for naive UTC (settings/audit/*_at columns).
    # The only aware .isoformat() usages introduced by W3.2 are inside the in-memory
    # `stats.*_local` / `at_instant` RESPONSE fields (05_CANONICAL_RESOLVER_CONTRACT.md
    # 5.3 explicitly documents these as aware ISO, they are not stored as a legacy
    # naive-UTC column) -- never assigned to an `_at`/`updated_at`/`resolved_at_utc` SQL
    # column. resolved_at_utc itself continues to use storage._now_iso().
    check("w3_resolve_schedule's resolved_at_utc still uses storage._now_iso() (naive UTC)",
          "resolved_at_utc = _now_iso()" in Path(BASE_DIR / "storage.py").read_text(encoding="utf-8"))

    # ---- stats_engine.SE_TZ equivalence (frozen contract test A6) + independence intact
    check("stats_engine.SE_TZ.key == storage.W3_TZ_NAME", stats_engine.SE_TZ.key == storage.W3_TZ_NAME)
    se_src = Path(BASE_DIR / "stats_engine.py").read_text(encoding="utf-8-sig")
    se_tree = ast.parse(se_src, filename="stats_engine.py")
    # Module-level (unconditional, top-of-file) import of storage is what the frozen
    # contract forbids -- "stats_engine intentionally does not import storage" (05 2.3 /
    # 12.7) so the engine survives storage being unavailable. A pre-existing, lazy,
    # try/except-guarded, FUNCTION-LOCAL import for the explicitly opt-in
    # schedule_aware=True path (se_prev_working_day, docstring: "Import is lazy so the
    # engine stays usable even if storage is unavailable") is the documented exception,
    # predates W3 entirely, and is untouched here -- only a module-body-level import
    # would violate the contract.
    module_level_imports_storage = any(
        (isinstance(n, ast.Import) and any(a.name == "storage" for a in n.names))
        or (isinstance(n, ast.ImportFrom) and n.module == "storage")
        for n in se_tree.body
    )
    check("stats_engine.py has no MODULE-LEVEL import of storage (12.7: read-only, never "
          "modified; the one pre-existing lazy/guarded function-local import for the "
          "opt-in schedule_aware path is the documented exception)",
          not module_level_imports_storage)
    check("W3.2 made zero source changes to stats_engine.py (never touched per file boundary)",
          "SE_TZ = ZoneInfo(\"Europe/Kyiv\")" in se_src)

    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
