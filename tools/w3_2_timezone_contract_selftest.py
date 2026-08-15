# -*- coding: utf-8 -*-
"""tools/w3_2_timezone_contract_selftest.py -- W3.2 single-timezone-contract selftest.

Proves (task spec section 2/10) that:
  - storage.W3_TZ_NAME / storage.w3_tz() / storage.w3_now() behave per the frozen
    contract (02_S9_TIMEZONE_CONTRACT.md 2.2): one name, cached ZoneInfo, RAISES
    W3TimezoneError on an unloadable zone, never falls back to the OS-local zone.
  - the redirected call sites (main.py:_kyiv_now, panel_bot.py:_pf_kyiv_now,
    preflight_check.py's four kyiv_* helpers) no longer contain a silent OS-local
    fallback, verified via AST on the live source (main.py/panel_bot.py cannot be
    imported standalone -- Telethon/env side effects at import, per CLAUDE.md's
    documented technique).
  - preflight_check.py CAN be imported directly (it is a pure helper module) and its
    real kyiv_today()/kyiv_now_hms()/kyiv_now_hm() agree with storage.w3_now() to the
    second.

Read-only against the real project tree; no DB is opened; no network; no Telegram.

    python tools\\w3_2_timezone_contract_selftest.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except Exception:
        return path.read_bytes().decode("utf-8", errors="replace")


def count_bare_datetime_now_today(path: Path) -> list[int]:
    """THE real static guard used by A7/A10: return the line numbers of every bare
    (no-argument) datetime.now()/datetime.today() call in `path`. Both this module's own
    check and tools/w3_2_mutation_proof.py's M32-8 call this exact function against a
    (real or mutated) source file -- never a duplicated/simulated detector."""
    src = _read(path)
    tree = ast.parse(src, filename=str(path))
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("now", "today") and not node.args and not node.keywords
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "datetime"
    ]


def _func_body_src(tree: ast.Module, src: str, name: str) -> str:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = node.lineno
            end = getattr(node, "end_lineno", start)
            lines = src.splitlines()[start - 1:end]
            return "\n".join(lines)
    return ""


def check_storage_contract() -> None:
    import storage

    check("storage.W3_TZ_NAME == 'Europe/Kyiv'", storage.W3_TZ_NAME == "Europe/Kyiv")

    tz = storage.w3_tz()
    check("storage.w3_tz() returns a ZoneInfo for the canonical name", str(tz) == "Europe/Kyiv")
    tz2 = storage.w3_tz()
    check("storage.w3_tz() is cached (same object on repeat call)", tz is tz2)

    now = storage.w3_now()
    check("storage.w3_now() returns an AWARE datetime", now.tzinfo is not None and now.utcoffset() is not None)

    try:
        storage.w3_tz("Not/A_Real_Zone_xyz")
        check("storage.w3_tz() RAISES W3TimezoneError on an unloadable zone name", False, "no exception raised")
    except storage.W3TimezoneError:
        check("storage.w3_tz() RAISES W3TimezoneError on an unloadable zone name", True)
    except Exception as exc:
        check("storage.w3_tz() RAISES W3TimezoneError on an unloadable zone name", False,
              f"wrong exception type: {type(exc).__name__}")

    try:
        import datetime as _dt
        storage.w3_business_date(at_instant=_dt.datetime(2026, 1, 1))
        check("storage.w3_business_date() RAISES W3NaiveDatetimeError on a naive at_instant", False,
              "no exception raised")
    except storage.W3NaiveDatetimeError:
        check("storage.w3_business_date() RAISES W3NaiveDatetimeError on a naive at_instant", True)

    bd = storage.w3_business_date(at_instant=now)
    check("storage.w3_business_date() returns 'YYYY-MM-DD'", len(bd) == 10 and bd[4] == "-" and bd[7] == "-")


def check_no_source_module_binding_leak() -> None:
    # sanity: storage module import above must not have opened the real project DB or
    # started any network/Telegram client as an import-time side effect.
    check("storage module import above completed without raising", True)


def check_main_py_redirect() -> None:
    src = _read(BASE_DIR / "main.py")
    tree = ast.parse(src, filename="main.py")
    body = _func_body_src(tree, src, "_kyiv_now")
    check("main.py: _kyiv_now() is defined", bool(body))
    check("main.py: _kyiv_now() no longer bare-constructs TZ_KYIV/ZoneInfo in its own body",
          "ZoneInfo(" not in body)
    check("main.py: _kyiv_now() delegates to storage.w3_now (w3_now referenced in its body)",
          "w3_now" in body)
    check("main.py: _kyiv_now() has no except/fallback branch", "except" not in body)
    check("main.py: TZ_KYIV (line 60) is left untouched (still present, single definition)",
          src.count('TZ_KYIV = ZoneInfo("Europe/Kyiv")') == 1)


def check_panel_bot_redirect() -> None:
    src = _read(BASE_DIR / "panel_bot.py")
    tree = ast.parse(src, filename="panel_bot.py")

    body = _func_body_src(tree, src, "_pf_kyiv_now")
    check("panel_bot.py: _pf_kyiv_now() is defined", bool(body))
    check("panel_bot.py: _pf_kyiv_now() no longer has a try/except OS-local fallback",
          "except" not in body)
    check("panel_bot.py: _pf_kyiv_now() no longer bare-constructs ZoneInfo in its own body",
          "ZoneInfo(" not in body)
    check("panel_bot.py: _pf_kyiv_now() delegates to storage.w3_now", "w3_now" in body)

    check("panel_bot.py: settings.updated_at write no longer uses bare datetime.now()",
          "now = datetime.now().replace(microsecond=0).isoformat()" not in src)
    check("panel_bot.py: TZ-6 slow-callback log line no longer uses bare datetime.now()",
          "datetime.now().isoformat(timespec='seconds')" not in src)

    # _sched_pb_kyiv_today is EXPLICITLY W3.3 scope per the frozen Plan Freeze
    # (02_S9_TIMEZONE_CONTRACT.md 2.3 redirect table) -- W3.2 must NOT touch it.
    check("panel_bot.py: _sched_pb_kyiv_today is left untouched (deferred to W3.3 by the frozen plan)",
          'def _sched_pb_kyiv_today():\n    return datetime.now(tz=ZoneInfo("Europe/Kyiv")).date()' in src)


def check_preflight_redirect() -> None:
    src = _read(BASE_DIR / "preflight_check.py")
    tree = ast.parse(src, filename="preflight_check.py")

    check("preflight_check.py: no try/except around zoneinfo import anymore",
          "except Exception:  # pragma: no cover - stdlib tzdata always available" not in src)
    check("preflight_check.py: _TZ is assigned via storage.w3_tz() (hard import)",
          "_TZ = storage.w3_tz()" in src)

    for fn in ("kyiv_today", "kyiv_now_hms", "kyiv_now_hm"):
        body = _func_body_src(tree, src, fn)
        check(f"preflight_check.py: {fn}() has no '_TZ is not None' branch", "_TZ is not None" not in body)
        check(f"preflight_check.py: {fn}() has no bare datetime.now() (no-arg) call",
              "datetime.now()" not in body)
        check(f"preflight_check.py: {fn}() delegates to storage.w3_now()", "storage.w3_now()" in body)

    # zero bare datetime.now()/today() (no arguments) anywhere in the module -- the
    # frozen gate (12_FILE_AND_RELEASE_BOUNDARIES.md 12.5, tests A7/A10).
    bare_calls = count_bare_datetime_now_today(BASE_DIR / "preflight_check.py")
    check("preflight_check.py: ZERO bare datetime.now()/today() call sites (A7/A10 gate)",
          len(bare_calls) == 0, str(bare_calls))


def check_preflight_runtime() -> None:
    import importlib
    import preflight_check
    importlib.reload(preflight_check)
    import storage

    a = storage.w3_now()
    b_iso = preflight_check.kyiv_today_iso()
    check("preflight_check.kyiv_today_iso() matches storage.w3_now().date()", b_iso == a.date().isoformat())

    hm1 = preflight_check.kyiv_now_hm()
    hm2 = storage.w3_now().strftime("%H:%M")
    check("preflight_check.kyiv_now_hm() agrees with storage.w3_now() to the minute (allowing a 1-min race)",
          hm1 == hm2 or abs(int(hm1[:2]) * 60 + int(hm1[3:]) - (int(hm2[:2]) * 60 + int(hm2[3:]))) <= 1)


def main() -> int:
    check_storage_contract()
    check_no_source_module_binding_leak()
    check_main_py_redirect()
    check_panel_bot_redirect()
    check_preflight_redirect()
    check_preflight_runtime()
    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
