# -*- coding: utf-8 -*-
"""UTCNOW MIGRATION 20260815 -- one-shot mechanical migration away from the
datetime.utcnow() API deprecated in Python 3.12.

Semantics are preserved EXACTLY: every replacement produces the same naive
(tzinfo=None) UTC datetime the old call returned:

    X.utcnow()  ->  X.now(<tz>.utc).replace(tzinfo=None)

Two call shapes exist in this codebase:

1. module-alias calls  (M.datetime.utcnow(), where M is `import datetime as M`)
   -> M.datetime.now(M.timezone.utc).replace(tzinfo=None)
   No new names needed: the module alias already carries `timezone`.

2. class calls  (D.utcnow(), where D is `from datetime import datetime [as D]`)
   -> D.now(__import__("datetime").timezone.utc).replace(tzinfo=None)
   SELF-CONTAINED ON PURPOSE: dozens of selftests in tools/ extract single
   product defs via AST and exec them in restricted namespaces.  Any new
   module-global name (a `from datetime import timezone as ...` alias) would
   NameError inside those extracted bodies -- this was tried first and broke
   13+ selftests.  __import__ goes through builtins, which every exec
   namespace has, so the expression works wherever D itself works.

Only real call sites are rewritten (regex requires an identifier before the
dot); `def utcnow()` fakes in selftests and docstring mentions are untouched.

Usage: python tools/utcnow_migration.py file1.py [file2.py ...]
Prints a per-file replacement count.  Idempotent: a second run replaces 0.
"""
from __future__ import annotations

import io
import re
import sys

MODULE_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.datetime\.utcnow\(\)")
CLASS_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.utcnow\(\)")
SELF_CONTAINED_TZ = '__import__("datetime").timezone.utc'


def migrate(path: str) -> int:
    with io.open(path, "r", encoding="utf-8-sig") as fh:
        src = fh.read()
    count = 0

    def module_sub(m: re.Match) -> str:
        nonlocal count
        count += 1
        mod = m.group(1)
        return f"{mod}.datetime.now({mod}.timezone.utc).replace(tzinfo=None)"

    src = MODULE_CALL.sub(module_sub, src)

    def class_sub(m: re.Match) -> str:
        nonlocal count
        cls = m.group(1)
        if cls.endswith("datetime") or cls == "datetime":
            count += 1
            return f"{cls}.now({SELF_CONTAINED_TZ}).replace(tzinfo=None)"
        return m.group(0)  # unknown receiver -- leave untouched

    src = CLASS_CALL.sub(class_sub, src)

    if count:
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(src)
    return count


def main() -> None:
    total = 0
    for path in sys.argv[1:]:
        n = migrate(path)
        total += n
        print(f"{path}: {n} replaced")
    print(f"TOTAL: {total}")


if __name__ == "__main__":
    main()
