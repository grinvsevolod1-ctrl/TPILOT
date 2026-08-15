# -*- coding: utf-8 -*-
"""tools/partner_excel_dup_filter_selftest.py -- offline self-test for the
PartnerBot Excel duplicate-consistency fix (Option C+, 2026-07-13).

Context: a read-only audit found that _tp_pdf_export_xlsx (day/flight Excel
exports) exported ALL countable rows regardless of a buyer's
show_duplicates setting, so a buyer with duplicates hidden could still see
duplicate-like rows in the Excel file (duplicate=1 + lead_countable=1,
contact_kind in duplicate/returning/old_baseline, same-source identity
dupes, "other_manager" dedupe_reason) even though the SAME rows are already
excluded from that buyer's TEXT stats totals
(drop_duplicates=not show_duplicates -> se_is_buyer_duplicate). This patch
adds one small helper (_tp_pdf_leads_for_export) reusing the EXISTING
predicates (_buyer_show_duplicates, _psf3_is_buyer_duplicate) -- no new
duplicate-detection logic -- and wires it into _tp_pdf_export_xlsx right
after leads are collected, before any row is written to the workbook.

Scope: PartnerBot Excel export only. Text stats, the duplicate section,
live push, and AdminBot/ManagerBot/internal stats are untouched.
stats_engine.py/main.py/manager_bot.py/panel_bot.py are not edited.

Techniques: _buyer_show_duplicates/_psf3_is_buyer_duplicate/
_tp_partner_dup_is_identity_duplicate/_tp_pdf_leads_for_export are all pure
(no DB/Telethon/network access) -- extracted via ast.parse + ast.unparse +
exec() (same technique used by tools/preflight_message_update_selftest.py)
and executed FOR REAL, not faked, so this test locks in the exact
predicate behavior rather than a re-implementation of it. No openpyxl
Workbook is written -- the helper operates on plain lead dicts, which is
everything worth testing here (the Workbook-writing code around it is
unchanged plumbing).

    python3.12 tools\\partner_excel_dup_filter_selftest.py
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


# ======================================================================
# AST extraction (partner_stat_bot.py cannot be imported standalone --
# Telethon/env side effects at import time). Same technique as
# tools/preflight_message_update_selftest.py / tools/proxy_pool_selftest.py.
# ======================================================================

def _extract_and_exec(path: str, names: set, extra_ns: dict) -> dict:
    src = open(path, encoding="utf-8-sig").read()
    tree = ast.parse(src)
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm and nm in names:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names:
            nodes.append(n)
            seen.add(n.targets[0].id)
            continue
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
            nodes.append(n)
            seen.add(n.target.id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing} in {path}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path}>", "exec"), ns)
    return ns


PARTNER_PATH = str(BASE_DIR / "partner_stat_bot.py")

# All four are pure, real functions -- no DB/Telethon/network access --
# extracted and executed FOR REAL (not faked) so this test exercises the
# exact production predicate logic.
FILTER_NAMES = {
    "_tp_pdf_leads_for_export",
    "_buyer_show_duplicates",
    "_psf3_is_buyer_duplicate",
    "_tp_partner_dup_is_identity_duplicate",
}


def _new_block_text() -> str:
    src = open(PARTNER_PATH, encoding="utf-8-sig").read()
    start_marker = "# --- TPILOT PARTNER EXCEL DUPLICATE FILTER (Option C+) 20260713 START"
    end_marker = "# --- TPILOT PARTNER EXCEL DUPLICATE FILTER (Option C+) 20260713 END"
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def _export_fn_text() -> str:
    src = open(PARTNER_PATH, encoding="utf-8-sig").read()
    i = src.index("def _tp_pdf_export_xlsx(")
    j = src.index("\ndef _export_xlsx_for_buyer(", i)
    return src[i:j]


# ======================================================================
# Fixture leads
# ======================================================================

BUYER_HIDE = {"user_id": 1, "source_key": "rassylka", "show_duplicates": 0}
BUYER_SHOW = {"user_id": 2, "source_key": "rassylka", "show_duplicates": 1}
BUYER_DEFAULT = {"user_id": 3, "source_key": "rassylka"}  # show_duplicates absent -> show

LEGACY_FALSE_POSITIVE = {
    "id": 1, "chat_id": 111, "duplicate": 1, "lead_countable": 1,
    "contact_kind": "new", "dedupe_reason": "new_contact",
}
KIND_DUPLICATE = {"id": 2, "chat_id": 222, "duplicate": 0, "lead_countable": 0, "contact_kind": "duplicate", "dedupe_reason": ""}
KIND_RETURNING = {"id": 3, "chat_id": 333, "duplicate": 0, "lead_countable": 0, "contact_kind": "returning", "dedupe_reason": ""}
KIND_OLD_BASELINE = {"id": 4, "chat_id": 444, "duplicate": 0, "lead_countable": 0, "contact_kind": "old_baseline", "dedupe_reason": ""}
OTHER_MANAGER_REASON = {"id": 5, "chat_id": 555, "duplicate": 0, "lead_countable": 1, "contact_kind": "new", "dedupe_reason": "other_manager_active"}
NORMAL_ROW = {"id": 6, "chat_id": 666, "duplicate": 0, "lead_countable": 1, "contact_kind": "new", "dedupe_reason": "new_contact"}

ALL_ROWS = [LEGACY_FALSE_POSITIVE, KIND_DUPLICATE, KIND_RETURNING, KIND_OLD_BASELINE, OTHER_MANAGER_REASON, NORMAL_ROW]


def main() -> int:
    ns = _extract_and_exec(PARTNER_PATH, FILTER_NAMES, {})
    leads_for_export = ns["_tp_pdf_leads_for_export"]

    check("1. _tp_pdf_leads_for_export exists and is callable", callable(leads_for_export))

    hidden = leads_for_export(BUYER_HIDE, list(ALL_ROWS))
    hidden_ids = {r["id"] for r in hidden}

    check("2. show_duplicates=0 excludes legacy false-positive (duplicate=1, lead_countable=1, contact_kind=new, dedupe_reason=new_contact)", LEGACY_FALSE_POSITIVE["id"] not in hidden_ids, hidden_ids)
    check("3. show_duplicates=0 excludes contact_kind='duplicate'", KIND_DUPLICATE["id"] not in hidden_ids, hidden_ids)
    check("4. show_duplicates=0 excludes contact_kind='returning'", KIND_RETURNING["id"] not in hidden_ids, hidden_ids)
    check("5. show_duplicates=0 excludes contact_kind='old_baseline'", KIND_OLD_BASELINE["id"] not in hidden_ids, hidden_ids)
    check("6. show_duplicates=0 excludes dedupe_reason containing 'other_manager'", OTHER_MANAGER_REASON["id"] not in hidden_ids, hidden_ids)
    check("7. show_duplicates=0 keeps normal new/countable rows", NORMAL_ROW["id"] in hidden_ids, hidden_ids)
    check("7b. show_duplicates=0 result contains ONLY the normal row", hidden_ids == {NORMAL_ROW["id"]}, hidden_ids)

    shown = leads_for_export(BUYER_SHOW, list(ALL_ROWS))
    shown_ids = {r["id"] for r in shown}
    check("8. show_duplicates=1 keeps ALL rows unchanged (no filtering)", shown_ids == {r["id"] for r in ALL_ROWS}, shown_ids)
    check("8b. show_duplicates=1 returns the same number of rows as the input", len(shown) == len(ALL_ROWS), (len(shown), len(ALL_ROWS)))

    default_shown = leads_for_export(BUYER_DEFAULT, list(ALL_ROWS))
    check("(extra) buyer with show_duplicates absent (None) defaults to show -- unchanged", {r["id"] for r in default_shown} == {r["id"] for r in ALL_ROWS})

    # empty/None-safety
    check("(extra) empty leads list -> empty result, no crash", leads_for_export(BUYER_HIDE, []) == [])
    check("(extra) None leads -> empty result, no crash", leads_for_export(BUYER_HIDE, None) == [])
    check("(extra) None buyer treated as show=1 (no filtering) -- matches _buyer_show_duplicates(None) semantics", len(leads_for_export(None, list(ALL_ROWS))) == len(ALL_ROWS))

    block = _new_block_text()
    check("9. helper uses _buyer_show_duplicates", "_buyer_show_duplicates(buyer)" in block, block)
    check("10. helper uses _psf3_is_buyer_duplicate", "_psf3_is_buyer_duplicate(l)" in block, block)

    export_fn = _export_fn_text()
    call_idx = export_fn.find("_tp_pdf_leads_for_export(buyer, leads)")
    write_idx = export_fn.find("ws.append(headers)")
    check("11. _tp_pdf_export_xlsx calls the helper", call_idx != -1, export_fn)
    check("11b. the helper call happens BEFORE any workbook row is written", call_idx != -1 and write_idx != -1 and call_idx < write_idx, (call_idx, write_idx))

    other_files = ["stats_engine.py", "main.py", "manager_bot.py", "panel_bot.py"]
    leaked = []
    for f in other_files:
        try:
            text = open(BASE_DIR / f, encoding="utf-8-sig").read()
        except Exception as e:
            check(f"12. {f} is readable for the scope check", False, repr(e))
            continue
        if "_tp_pdf_leads_for_export" in text or "TPILOT PARTNER EXCEL DUPLICATE FILTER (Option C+)" in text:
            leaked.append(f)
    check("12. no changes leaked into stats_engine.py/main.py/manager_bot.py/panel_bot.py", leaked == [], leaked)

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
