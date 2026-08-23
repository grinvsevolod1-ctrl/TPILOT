#!/usr/bin/env python3
"""tools/ci_check.py -- the single CI gate for TPilot (roadmap stage R5).

Runs, in order (fail-fast unless --keep-going):

  1. py_compile        -- every tracked *.py must byte-compile
  2. mojibake scan     -- no UTF-8-misdecoded Cyrillic / U+FFFD in tracked *.py
  3. allow_spend audit -- allow_spend=True appears as a real call argument in
                          EXACTLY 2 places in PRODUCTION code (selftest fakes
                          under tools/ are exempt); _pbuy_provider() must pass
                          allow_spend=False
  4. password-leak grep-- no raw proxy password fields in panel_commands
                          payload builders (heuristic; see AGENTS.md section 4)
  5. override baseline -- tools/override_chain_selftest.py
  6. selftest suite    -- all tools/*_selftest.py (skippable via --no-selftests
                          or SLOW list trimming with --fast)

Usage:
  python tools/ci_check.py               # full run
  python tools/ci_check.py --fast        # skip the slow selftests
  python tools/ci_check.py --no-selftests

Exit code 0 = all gates green.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import os
import py_compile
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Selftests known to legitimately run longer than the default per-test budget.
SLOW_SELFTESTS = {
    "bizlink_readiness_integration_selftest.py",
    "tg_health_recovery_selftest.py",
}
DEFAULT_TIMEOUT = 180
SLOW_TIMEOUT = 600

# Real mojibake is a LEAD byte followed by a misdecoded continuation byte:
#   "\u00d0"/"\u00d1" + char in U+0080..U+00FF  (UTF-8 Cyrillic read as Latin-1)
#   "\u00e2\u20ac" + one more char              (UTF-8 punctuation read as cp1252)
# Bare "\u00d0" / "\u00e2\u20ac" immediately before a quote are legitimate:
# several selftests embed them as literals of their OWN mojibake detectors.
import re as _re

MOJIBAKE_RE = _re.compile(
    "[\u00d0\u00d1][\u0080-\u00ff]|\u00e2\u20ac[^\"'\\s)|]|\ufffd"
)

# The two blessed allow_spend=True call sites live in production code.
# After R2 extraction they may move to a proxy module -- the COUNT stays 2.
ALLOW_SPEND_EXPECTED = 2


def tracked_py_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=ROOT, capture_output=True, text=True
    )
    return [ROOT / line for line in out.stdout.splitlines() if line.strip()]


def gate_py_compile(files: list[Path]) -> list[str]:
    errors = []
    for f in files:
        try:
            py_compile.compile(str(f), doraise=True)
        except Exception as exc:  # noqa: BLE001 - report every failure kind
            errors.append(f"{f.relative_to(ROOT)}: {exc}")
    return errors


def gate_mojibake(files: list[Path]) -> list[str]:
    errors = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            errors.append(f"{f.relative_to(ROOT)}: not valid UTF-8 ({exc})")
            continue
        m = MOJIBAKE_RE.search(text)
        if m:
            line_no = text.count("\n", 0, m.start()) + 1
            errors.append(
                f"{f.relative_to(ROOT)}:{line_no}: mojibake {m.group(0)!r}"
            )
    return errors


def gate_allow_spend(files: list[Path]) -> list[str]:
    errors = []
    prod_sites: list[str] = []
    for f in files:
        rel = f.relative_to(ROOT)
        in_tools = rel.parts[0] == "tools"
        try:
            tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        except SyntaxError:
            continue  # py_compile gate reports this
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "allow_spend"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    if not in_tools:
                        prod_sites.append(f"{rel}:{node.lineno}")
    if len(prod_sites) != ALLOW_SPEND_EXPECTED:
        errors.append(
            f"allow_spend=True production call sites: {len(prod_sites)} "
            f"(expected {ALLOW_SPEND_EXPECTED}): {prod_sites}"
        )
    # _pbuy_provider must construct with allow_spend=False
    main_py = ROOT / "main.py"
    if main_py.exists():
        src = main_py.read_text(encoding="utf-8-sig")
        tree = ast.parse(src)
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_pbuy_provider"
            ):
                ok = False
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "allow_spend"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                ok = True
                if not ok:
                    errors.append(
                        "_pbuy_provider() does not pass allow_spend=False"
                    )
    return errors


def gate_password_leak(files: list[Path]) -> list[str]:
    """Heuristic: panel_commands result payloads must never carry a raw proxy
    password -- only has_password booleans. Flag suspicious json/dict keys."""
    errors = []
    suspicious = ('"password":', "'password':")
    allowed_markers = ("has_password", "compare_digest", "PASSWORD_PIN")
    for f in files:
        rel = f.relative_to(ROOT)
        if rel.parts[0] == "tools":
            continue
        try:
            text = f.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if "panel_command" not in line and "result_text" not in line:
                continue
            low = line.lower()
            if any(s in low for s in suspicious) and not any(
                a.lower() in low for a in allowed_markers
            ):
                errors.append(f"{rel}:{i}: possible raw password in panel payload")
    return errors


def gate_override_baseline(python: str) -> list[str]:
    proc = subprocess.run(
        [python, str(ROOT / "tools" / "override_chain_selftest.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=DEFAULT_TIMEOUT,
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-5:])
        return [f"override_chain_selftest failed:\n{tail}"]
    return []


def run_one_selftest(python: str, path: Path) -> tuple[str, bool, float]:
    timeout = SLOW_TIMEOUT if path.name in SLOW_SELFTESTS else DEFAULT_TIMEOUT
    start = time.monotonic()
    try:
        proc = subprocess.run(
            [python, str(path)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ok = proc.returncode == 0
    except subprocess.TimeoutExpired:
        ok = False
    return path.name, ok, time.monotonic() - start


def gate_selftests(python: str, fast: bool, jobs: int) -> list[str]:
    tests = sorted((ROOT / "tools").glob("*_selftest.py"))
    if fast:
        tests = [t for t in tests if t.name not in SLOW_SELFTESTS]
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_one_selftest, python, t) for t in tests]
        done = 0
        for fut in concurrent.futures.as_completed(futures):
            name, ok, took = fut.result()
            done += 1
            status = "PASS" if ok else "FAIL"
            print(f"  [{done}/{len(tests)}] {status} {name} ({took:.0f}s)", flush=True)
            if not ok:
                failures.append(name)
    return [f"selftest failed: {n}" for n in sorted(failures)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fast", action="store_true", help="skip slow selftests")
    parser.add_argument("--no-selftests", action="store_true")
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 2))
    args = parser.parse_args()

    python = sys.executable
    files = tracked_py_files()
    print(f"ci_check: {len(files)} tracked python files, python={python}")

    gates: list[tuple[str, list[str]]] = []

    def run_gate(name, fn):
        print(f"== {name} ==", flush=True)
        errs = fn()
        gates.append((name, errs))
        for e in errs:
            print(f"  ERROR: {e}")
        if errs and not args.keep_going:
            return False
        return True

    ok = (
        run_gate("py_compile", lambda: gate_py_compile(files))
        and run_gate("mojibake scan", lambda: gate_mojibake(files))
        and run_gate("allow_spend audit", lambda: gate_allow_spend(files))
        and run_gate("password-leak heuristic", lambda: gate_password_leak(files))
        and run_gate("override baseline", lambda: gate_override_baseline(python))
    )
    if ok and not args.no_selftests:
        ok = run_gate(
            "selftest suite",
            lambda: gate_selftests(python, args.fast, args.jobs),
        )

    total_errors = sum(len(errs) for _, errs in gates)
    print("== summary ==")
    for name, errs in gates:
        print(f"  {'PASS' if not errs else 'FAIL'} {name}")
    print(f"ci_check: {'GREEN' if total_errors == 0 else f'{total_errors} error(s)'}")
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
