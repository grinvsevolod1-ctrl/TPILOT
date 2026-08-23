#!/usr/bin/env python3
"""Единый CI-гейт TPilot (этап R5 из AGENTS.md).

Прогоняет по порядку:
  1. py_compile всех отслеживаемых .py файлов
  2. Аудит инвариантов:
     - allow_spend=True как реальный аргумент вызова встречается РОВНО 2 раза
       по всему проекту (buy-confirm make_ipv4 + prolong_make в renewal executor)
     - _pbuy_provider() всегда конструирует провайдера с allow_spend=False
  3. Моджибейк-скан: маркеры двойной перекодировки (U+00D0, U+00D1,
     U+00E2+U+20AC) перед не-ASCII символом и U+FFFD в .py файлах
  4. pyflakes/ruff, если установлены (не блокирует, если их нет)
  5. Полный прогон tools/*_selftest.py (пропускается с --no-selftests)

Выход: 0 если все гейты зелёные, 1 иначе.

Использование:
  python tools/ci_check.py                # всё
  python tools/ci_check.py --no-selftests # только статические гейты
  python tools/ci_check.py --selftest-timeout 300
"""
from __future__ import annotations

import argparse
import ast
import os
import py_compile
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Моджибейк — результат чтения UTF-8 как cp1252/latin-1: U+00D0/U+00D1
# (первые байты кириллицы), "â€" (типографика) и U+FFFD. Настоящая порча
# всегда идёт последовательностями не-ASCII символов, поэтому маркер
# считается порчей, только если сразу за ним стоит не-ASCII символ. Это
# отсекает детекторные строки в самих селфтестах вида ("Ð", "Ñ", "â€"),
# где за маркером следует ASCII-кавычка. U+FFFD — порча всегда.
import re as _re

MOJIBAKE_RE = _re.compile(
    "\ufffd|[\u00d0\u00d1](?=[^\x00-\x7f])|\u00e2\u20ac(?=[^\x00-\x7f])"
)

ALLOW_SPEND_EXPECTED = 2


def _tracked_py_files() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.py"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout
        files = [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        files = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in {"venv", ".git", "__pycache__", "runtime", "node_modules"}]
            for fn in filenames:
                if fn.endswith(".py"):
                    files.append(os.path.relpath(os.path.join(dirpath, fn), ROOT))
    return sorted(files)


def gate_py_compile(files: list[str]) -> list[str]:
    errors = []
    for rel in files:
        path = os.path.join(ROOT, rel)
        try:
            py_compile.compile(path, doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{rel}: {exc.msg.splitlines()[0] if exc.msg else exc}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{rel}: {exc}")
    return errors


class _AllowSpendVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.true_calls: list[int] = []
        self.pbuy_provider_bad: list[int] = []
        self._in_pbuy_provider = 0

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg == "allow_spend":
                if isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    self.true_calls.append(node.lineno)
                elif self._in_pbuy_provider and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False
                ):
                    self.pbuy_provider_bad.append(node.lineno)
        self.generic_visit(node)

    def _visit_func(self, node) -> None:
        is_pbuy = node.name == "_pbuy_provider"
        if is_pbuy:
            self._in_pbuy_provider += 1
        self.generic_visit(node)
        if is_pbuy:
            self._in_pbuy_provider -= 1

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func


def gate_invariants(files: list[str]) -> list[str]:
    """Селфтесты исключаются из подсчёта allow_spend=True: они легитимно
    передают его фейковым провайдерам. Инвариант «ровно 2» — про боевой код;
    сами селфтесты (check 30) дополнительно контролируют это изнутри."""
    errors: list[str] = []
    locations: list[str] = []
    for rel in files:
        if os.path.basename(rel).endswith("_selftest.py"):
            continue
        path = os.path.join(ROOT, rel)
        try:
            tree = ast.parse(open(path, encoding="utf-8-sig").read())
        except SyntaxError as exc:
            errors.append(f"{rel}: SyntaxError при аудите: {exc}")
            continue
        visitor = _AllowSpendVisitor()
        visitor.visit(tree)
        locations.extend(f"{rel}:{ln}" for ln in visitor.true_calls)
        errors.extend(
            f"{rel}:{ln}: _pbuy_provider содержит allow_spend != False"
            for ln in visitor.pbuy_provider_bad
        )
    if len(locations) != ALLOW_SPEND_EXPECTED:
        errors.append(
            f"allow_spend=True встречается {len(locations)} раз(а) вместо "
            f"{ALLOW_SPEND_EXPECTED}: {locations}"
        )
    return errors


def gate_mojibake(files: list[str]) -> list[str]:
    errors = []
    for rel in files:
        path = os.path.join(ROOT, rel)
        try:
            text = open(path, encoding="utf-8-sig").read()
        except UnicodeDecodeError as exc:
            errors.append(f"{rel}: не UTF-8: {exc}")
            continue
        m = MOJIBAKE_RE.search(text)
        if m:
            line_no = text[: m.start()].count("\n") + 1
            errors.append(f"{rel}:{line_no}: маркер моджибейка {m.group()!r}")
    return errors


def gate_lint(files: list[str]) -> list[str]:
    """ruff или pyflakes, если доступны; отсутствие линтера — не ошибка."""
    for linter, args in (("ruff", ["check", "--select", "F821,F811,E999"]), ("pyflakes", [])):
        try:
            proc = subprocess.run(
                [sys.executable, "-m", linter, *args, *files],
                cwd=ROOT, capture_output=True, text=True, timeout=600,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if proc.returncode in (0,):
            return []
        if "No module named" in (proc.stderr or ""):
            continue
        out = (proc.stdout or proc.stderr or "").strip()
        return [f"{linter}: {line}" for line in out.splitlines()[:50]]
    print("  (линтер не установлен — пропущено)")
    return []


def gate_selftests(timeout: int) -> list[str]:
    tools_dir = os.path.join(ROOT, "tools")
    tests = sorted(
        fn for fn in os.listdir(tools_dir) if fn.endswith("_selftest.py")
    )
    errors = []
    for fn in tests:
        start = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(tools_dir, fn)],
                cwd=ROOT, capture_output=True, text=True, timeout=timeout,
            )
            status = "PASS" if proc.returncode == 0 else f"FAIL({proc.returncode})"
        except subprocess.TimeoutExpired:
            status = "TIMEOUT"
            proc = None
        dur = time.time() - start
        print(f"  {fn}: {status} ({dur:.0f}s)", flush=True)
        if status != "PASS":
            tail = ""
            if proc is not None:
                tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-500:]
            errors.append(f"{fn}: {status}\n{tail}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-selftests", action="store_true")
    parser.add_argument("--selftest-timeout", type=int, default=300)
    args = parser.parse_args()

    files = _tracked_py_files()
    print(f"ci_check: {len(files)} .py файлов")
    failed = False

    for name, fn in (
        ("py_compile", lambda: gate_py_compile(files)),
        ("invariants(allow_spend)", lambda: gate_invariants(files)),
        ("mojibake", lambda: gate_mojibake(files)),
        ("lint", lambda: gate_lint(files)),
    ):
        print(f"[{name}]", flush=True)
        errors = fn()
        if errors:
            failed = True
            for e in errors:
                print(f"  FAIL: {e}")
        else:
            print("  OK")

    if not args.no_selftests:
        print("[selftests]", flush=True)
        errors = gate_selftests(args.selftest_timeout)
        if errors:
            failed = True
            print(f"  {len(errors)} селфтест(ов) упало")
        else:
            print("  OK")

    print("ci_check:", "FAIL" if failed else "OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
