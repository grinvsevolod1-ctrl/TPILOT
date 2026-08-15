# -*- coding: utf-8 -*-
"""
Offline selftest for admin_ai.index_builder — AST extraction of AdminBot's
active capability surface (menu keys, callback data, /commands) from
panel_bot.py + main.py.

No network, no Telegram, no DB, no production files modified. Two kinds of
checks:
  1. Sanity checks against the REAL current panel_bot.py/main.py (proves
     the extractor finds real, known-good facts in the actual codebase).
  2. Mechanics checks against small SYNTHETIC source files written to a
     temp dir (proves last-def-wins for ordinary names, full-union for the
     two cooperative router names, and that changing a source file changes
     the semantic/source hashes) -- this isolates the *mechanism* from the
     ever-changing real files, per the project's established AST-extraction
     selftest idiom (see tools\\devlogin_callback_index_selftest.py).

Run:  python tools\\admin_ai_index_selftest.py
"""
from __future__ import annotations

import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from admin_ai.index_builder import build_index  # noqa: E402

PANEL_BOT_PY = os.path.join(BASE_DIR, "panel_bot.py")
MAIN_PY = os.path.join(BASE_DIR, "main.py")

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


# =========================================================================
# Part 1 -- real-file sanity checks (known-good facts as of this session)
# =========================================================================
def run_real_file_checks():
    index = build_index(PANEL_BOT_PY, MAIN_PY)

    menu_exact = set(index["menu_keys"]["exact"])
    menu_prefix = set(index["menu_keys"]["prefix"])
    cb_exact = set(index["callback_prefixes"]["exact"])
    cb_prefix = set(index["callback_prefixes"]["prefix"])
    commands = set(index["commands"]["exact"])

    check("real: 'main' is a known menu key", "main" in menu_exact)
    check("real: 'manager_admin' is a known menu key", "manager_admin" in menu_exact)
    check("real: 'ppool' is a known menu key", "ppool" in menu_exact)
    check("real: 'manager_admin:' is a known menu-key prefix (manager card)", "manager_admin:" in menu_prefix)

    check("real: 'noop' is a known exact callback", "noop" in cb_exact)
    check("real: 'bulk:' is a known callback prefix (from CallbackQuery pattern)", "bulk:" in cb_prefix)
    check("real: 'wiz:add_manager:proxy:' is a known callback prefix", "wiz:add_manager:proxy:" in cb_prefix)

    check("real: '/manager_list' is a known command", "/manager_list" in commands)
    check("real: '/manager_add' is a known command", "/manager_add" in commands)
    check("real: '/proxy_pool_list' is a known command", "/proxy_pool_list" in commands)

    diag = index["diagnostics"]
    coop_menu = diag["panel_bot.py"]["cooperative"].get("_title_for_menu", {})
    coop_cmd = diag["main.py"]["cooperative"].get("_panel_execute_command_text", {})
    check("real: _title_for_menu cooperative chain has >= 20 defs (all unioned)",
          coop_menu.get("defs", 0) >= 20, detail=str(coop_menu.get("defs")))
    check("real: _panel_execute_command_text cooperative chain has >= 20 defs (all unioned)",
          coop_cmd.get("defs", 0) >= 20, detail=str(coop_cmd.get("defs")))
    check("real: panel_bot.py reports at least one shadowed ordinary function",
          diag["panel_bot.py"]["functions_shadowed"] > 0)
    check("real: main.py reports at least one shadowed ordinary function",
          diag["main.py"]["functions_shadowed"] > 0)

    check("real: button_callbacks extracted a non-trivial number of entries",
          len(index["button_callbacks"]) > 100, detail=str(len(index["button_callbacks"])))
    check("real: semantic_hash is a 64-char hex sha256", len(index["semantic_hash"]) == 64)


# =========================================================================
# Part 2 -- synthetic-source mechanics checks
# =========================================================================
SYNTH_PANEL_V1 = '''\
from telethon import Button, events


def _main_menu():
    return [[Button.inline("Old Btn", b"menu:old_shadowed")]]


def _main_menu():  # last def -- the only one that should be active
    return [[Button.inline("New Btn", b"menu:new_active")]]


def _title_for_menu(menu):
    raw = str(menu or "main")
    if raw == "layer_one":
        return "one", []
    return "fallback", []


def _title_for_menu(menu):
    raw_menu = str(menu or "main")
    if raw_menu == "layer_two":
        return "two", []
    return "fallback", []


@client.on(events.CallbackQuery(pattern=b"^bulk:"))
async def on_callback(event):
    data = (event.data or b"").decode("utf-8", errors="ignore")
    if data == "noop":
        pass
    if data.startswith("wiz:add_manager:"):
        pass
'''

SYNTH_PANEL_V2_TAMPERED = SYNTH_PANEL_V1.replace(
    'Button.inline("New Btn", b"menu:new_active")',
    'Button.inline("New Btn V2", b"menu:new_active_v2")',
)

# A tamper that touches a CALLBACK-PREFIX literal (not just a button's data
# argument) -- per the index's design (see build_index docstring / plan
# §13), semantic_hash is scoped to (menu_keys, callback_prefixes,
# commands) specifically, so only THIS kind of edit is expected to move
# it. A button-data-only edit is still caught (via source_hashes, which
# changes on ANY byte-level edit) but deliberately does not move
# semantic_hash by itself.
SYNTH_PANEL_V3_PREFIX_TAMPERED = SYNTH_PANEL_V1.replace(
    'data.startswith("wiz:add_manager:")',
    'data.startswith("wiz:add_manager_v2:")',
)

SYNTH_MAIN_V1 = '''\
async def _panel_execute_command_text(command_text):
    cmd, _, args = str(command_text or "").strip().partition(" ")
    if cmd == "/layer_one_cmd":
        return {}
    return {}


async def _panel_execute_command_text(command_text):
    cmd, _, args = str(command_text or "").strip().partition(" ")
    if cmd == "/layer_two_cmd":
        return {}
    return {}


_TEST_DISPATCH = {
    "/dispatch_cmd": None,
}
'''


def _write(tmpdir, name, content):
    path = os.path.join(tmpdir, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path


def run_synthetic_checks():
    tmpdir = tempfile.mkdtemp(prefix="admin_ai_index_selftest_")

    panel_v1 = _write(tmpdir, "syn_panel_bot.py", SYNTH_PANEL_V1)
    main_v1 = _write(tmpdir, "syn_main.py", SYNTH_MAIN_V1)
    index_v1 = build_index(panel_v1, main_v1)

    menu_exact = set(index_v1["menu_keys"]["exact"])
    check("synthetic: cooperative _title_for_menu union includes BOTH layers' keys",
          {"layer_one", "layer_two"}.issubset(menu_exact), detail=str(menu_exact))

    commands = set(index_v1["commands"]["exact"])
    check("synthetic: cooperative _panel_execute_command_text union includes BOTH layers' commands",
          {"/layer_one_cmd", "/layer_two_cmd"}.issubset(commands), detail=str(commands))
    check("synthetic: module-level dispatch dict command is captured",
          "/dispatch_cmd" in commands, detail=str(commands))

    button_data = {b["data"] for b in index_v1["button_callbacks"]}
    check("synthetic: last-def-wins -- the ACTIVE _main_menu's button is present",
          "menu:new_active" in button_data, detail=str(button_data))
    check("synthetic: last-def-wins -- the SHADOWED _main_menu's button is EXCLUDED",
          "menu:old_shadowed" not in button_data, detail=str(button_data))

    cb_exact = set(index_v1["callback_prefixes"]["exact"])
    cb_prefix = set(index_v1["callback_prefixes"]["prefix"])
    check("synthetic: exact callback 'noop' extracted", "noop" in cb_exact)
    check("synthetic: prefix callback 'wiz:add_manager:' extracted", "wiz:add_manager:" in cb_prefix)
    check("synthetic: CallbackQuery(pattern=b'^bulk:') extracted as 'bulk:' prefix", "bulk:" in cb_prefix)

    # --- tamper test: change ONLY the button's text/data, rebuild, expect
    # both source_hashes and semantic_hash to change (button data feeds the
    # semantic hash; unrelated whitespace-only edits would not, by design).
    panel_v1_hash = next(iter(index_v1["source_hashes"].values()))

    panel_v2 = _write(tmpdir, "syn_panel_bot_v2.py", SYNTH_PANEL_V2_TAMPERED)
    index_v2 = build_index(panel_v2, main_v1)
    panel_v2_hash = next(iter(index_v2["source_hashes"].values()))

    check("tamper: source_hash for the tampered file changed (catches ANY byte-level edit, "
          "including button-only changes -- this is the PRIMARY change-detection signal)",
          panel_v1_hash != panel_v2_hash)
    check("tamper: semantic_hash is UNCHANGED by a button-data-only edit (by design -- it is "
          "scoped to menu_keys/callback_prefixes/commands, not raw button payloads; "
          "source_hashes above is what actually catches this edit)",
          index_v1["semantic_hash"] == index_v2["semantic_hash"])

    button_data_v2 = {b["data"] for b in index_v2["button_callbacks"]}
    check("tamper: new button data 'menu:new_active_v2' present after rebuild",
          "menu:new_active_v2" in button_data_v2)
    check("tamper: stale button data 'menu:new_active' gone after rebuild",
          "menu:new_active" not in button_data_v2)

    # --- a tamper that DOES touch a callback-prefix literal must move BOTH
    # source_hashes and semantic_hash (this is test #25 from the matrix:
    # "новая кнопка/изменение в исходнике меняет hash индекса").
    panel_v3 = _write(tmpdir, "syn_panel_bot_v3.py", SYNTH_PANEL_V3_PREFIX_TAMPERED)
    index_v3 = build_index(panel_v3, main_v1)
    panel_v3_hash = next(iter(index_v3["source_hashes"].values()))
    check("tamper: a callback-PREFIX literal edit changes source_hash",
          panel_v1_hash != panel_v3_hash)
    check("tamper: a callback-PREFIX literal edit changes semantic_hash too "
          "(it IS part of the menu/callback/command surface)",
          index_v1["semantic_hash"] != index_v3["semantic_hash"])
    cb_prefix_v3 = set(index_v3["callback_prefixes"]["prefix"])
    check("tamper: new callback prefix 'wiz:add_manager_v2:' present after rebuild",
          "wiz:add_manager_v2:" in cb_prefix_v3)
    check("tamper: stale callback prefix 'wiz:add_manager:' gone after rebuild",
          "wiz:add_manager:" not in cb_prefix_v3)

    # --- determinism: rebuilding from IDENTICAL sources yields identical hash
    index_v1_again = build_index(panel_v1, main_v1)
    check("determinism: rebuilding from unchanged sources yields the same semantic_hash",
          index_v1["semantic_hash"] == index_v1_again["semantic_hash"])


def main():
    print("=== Part 1: real panel_bot.py / main.py sanity checks ===")
    run_real_file_checks()
    print()
    print("=== Part 2: synthetic-source mechanics checks ===")
    run_synthetic_checks()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ADMIN_AI INDEX SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
