# -*- coding: utf-8 -*-
"""Delete dead override-stack defs by line spans (refactor stage 2, 2026-08-15).

Consumes /tmp/override_dead_spans_<file>.json produced by
override_reachability_report.py and rewrites the target file WITHOUT the
dead spans. Line-based, byte-preserving for every kept line: the file is
read and written as UTF-8 text with newline='' so no encoding or newline
normalization can occur (this project was previously corrupted by an
editing tool -- hence a script, not manual edits).

Safety gates BEFORE writing:
  * every span's def_line must currently hold a `def name(` / `async def name(`
  * a LATER def of the same name must still exist after the span
  * spans must be sorted and non-overlapping

Safety gates AFTER writing (caller runs): py_compile, pyflakes, mojibake
scan, allow_spend AST gate, full selftest suite.

A leading blank line immediately before each removed span is collapsed so
the file does not accumulate double blank gaps.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    fname = sys.argv[1]
    base = Path(__file__).resolve().parent.parent
    target = base / fname
    spans = json.loads(Path(f"/tmp/override_dead_spans_{fname}.json").read_text())
    spans.sort(key=lambda s: s["start"])

    with open(target, encoding="utf-8", newline="") as fh:
        lines = fh.readlines()  # keeps original line endings per line

    # --- pre-write gates -------------------------------------------------
    for a, b in zip(spans, spans[1:]):
        assert a["end"] < b["start"], f"overlap: {a} vs {b}"
    for s in spans:
        def_src = lines[s["def_line"] - 1]
        assert def_src.startswith((f"def {s['name']}(", f"async def {s['name']}(")), (
            f"def_line mismatch for {s['name']} at {s['def_line']}: {def_src[:80]!r}"
        )
        later = any(
            l.startswith((f"def {s['name']}(", f"async def {s['name']}("))
            for l in lines[s["end"]:]
        )
        assert later, f"no later def of {s['name']} after line {s['end']}"

    # --- delete (bottom-up so line numbers stay valid) --------------------
    removed = 0
    for s in sorted(spans, key=lambda x: -x["start"]):
        start_idx = s["start"] - 1  # 0-based inclusive
        end_idx = s["end"]          # 0-based exclusive
        # collapse ONE leading blank line if both neighbours are blank/EOF
        if (
            start_idx > 0
            and lines[start_idx - 1].strip() == ""
            and (end_idx >= len(lines) or lines[end_idx].strip() == "")
        ):
            start_idx -= 1
        removed += end_idx - start_idx
        del lines[start_idx:end_idx]

    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(lines)

    print(f"{fname}: removed {removed} lines across {len(spans)} dead defs")


if __name__ == "__main__":
    main()
