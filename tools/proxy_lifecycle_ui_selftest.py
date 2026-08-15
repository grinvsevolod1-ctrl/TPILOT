# -*- coding: utf-8 -*-
"""tools/proxy_lifecycle_ui_selftest.py -- offline selftest for the
PROXY LIFECYCLE SYNC 20260721 panel_bot.py UI (Phase 5).

panel_bot.py cannot be imported standalone (Telethon/env side effects) --
uses this project's established AST-extraction idiom. Real execution of the
extracted functions; no network, no Telegram, no provider.

Covers:
  - _plc_lifecycle_line: renders desired-vs-observed for all four action
    states + the not-yet-synced fallback, credential-free;
  - _plc_action_notification_buttons: safe fallback buttons for the four
    grouped-checklist notification kinds;
  - _plc_terminal_review_buttons: parses manager_key out of a real
    notification body, resolves to the manager's SHORT NUMERIC id (never the
    raw key) for the confirm button, falls back safely on any parse/lookup
    failure;
  - the `plcterm:` callback wiring: resolves id->key and rebuilds the exact
    "/proxy_lifecycle_terminal_confirm <key>" command through the SAME
    generic cmd: pipeline as srcpick: (no duplicated confirm logic);
  - notification kind routing: all four proxy_lifecycle_* action kinds and
    proxy_lifecycle_terminal_review route to the correct button builder in
    _panel_notification_loop;
  - _ppool_card_text includes the lifecycle line;
  - _handle_proxy_pool_card_command (main.py) surfaces the 4 new lifecycle
    fields;
  - callback_data length: plcterm:<id> stays <=64 bytes even for an
    adversarial int64-max manager id.

Run:  python tools\\proxy_lifecycle_ui_selftest.py
"""
from __future__ import annotations

import ast
import re as _re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from manager_registry import normalize_manager_key

PANEL_PY = BASE_DIR / "panel_bot.py"
MAIN_PY = BASE_DIR / "main.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def last_def(tree, name):
    defs = find_defs(tree, name)
    if not defs:
        raise AssertionError(f"no top-level def named {name!r} found")
    return defs[-1]


def src_of(tree, name):
    return ast.unparse(last_def(tree, name))


def extract_and_exec(tree, names, extra_ns):
    nodes = [last_def(tree, n) for n in names]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<extract {names}>", "exec"), ns)
    return ns


def main() -> int:
    panel_src = PANEL_PY.read_text(encoding="utf-8-sig")
    panel_tree = ast.parse(panel_src)
    main_src = MAIN_PY.read_text(encoding="utf-8-sig")
    main_tree = ast.parse(main_src)

    plc_labels = {
        "enable_required": "ENABLE_LABEL_SENTINEL",
        "disable_required": "DISABLE_LABEL_SENTINEL",
        "active_confirmed_on": "ON_LABEL_SENTINEL",
        "released_off_confirmed": "OFF_LABEL_SENTINEL",
        "expired": "EXPIRED_LABEL_SENTINEL",
    }

    # ======================================================================
    # _plc_lifecycle_line
    # ======================================================================
    ns = extract_and_exec(panel_tree, ["_plc_lifecycle_line"], {"_PLC_LIFECYCLE_LABELS": plc_labels})
    line_fn = ns["_plc_lifecycle_line"]

    l1 = line_fn({"lifecycle_status": "enable_required", "desired_provider_auto_renew": "Y", "observed_provider_auto_renew": "N"})
    check("enable_required shows desired=Y / observed=N + the enable label", "нужно=Y" in l1 and "сейчас=N" in l1 and "ENABLE_LABEL_SENTINEL" in l1, l1)

    l2 = line_fn({"lifecycle_status": "disable_required", "desired_provider_auto_renew": "N", "observed_provider_auto_renew": "Y"})
    check("disable_required shows desired=N / observed=Y + the disable label", "нужно=N" in l2 and "сейчас=Y" in l2 and "DISABLE_LABEL_SENTINEL" in l2, l2)

    l3 = line_fn({"lifecycle_status": "active_confirmed_on", "desired_provider_auto_renew": "Y", "observed_provider_auto_renew": "Y", "provider_sync_at": "2026-07-21T04:00:00"})
    check("active_confirmed_on shows the ON label", "ON_LABEL_SENTINEL" in l3)
    check("a present provider_sync_at is shown", "2026-07-21T04:00:00" in l3, l3)

    l4 = line_fn({"lifecycle_status": "released_off_confirmed", "desired_provider_auto_renew": "N", "observed_provider_auto_renew": "N"})
    check("released_off_confirmed shows the OFF label", "OFF_LABEL_SENTINEL" in l4)

    l5 = line_fn({})
    check("an empty/never-synced lease falls back to a safe 'not yet synced' label, not a KeyError",
          "не синхронизировано" in l5, l5)
    check("SAFETY: _plc_lifecycle_line output never contains the substring 'password'", "password" not in l1.lower() and "password" not in l4.lower())

    # ======================================================================
    # _plc_action_notification_buttons / _plc_terminal_review_buttons
    # ======================================================================
    def _fake_manager_row_by_key(key):
        return {"newmgr": {"id": 42, "manager_key": "newmgr"}}.get(str(key or "").strip().lower(), {})

    class _FakeBtn:
        def __init__(self, text, data):
            self.text = text
            self.data = data

        @staticmethod
        def inline(text, data):
            return _FakeBtn(text, data)

    ns2 = extract_and_exec(
        panel_tree,
        ["_plc_action_notification_buttons", "_plc_terminal_review_buttons", "_back_to_panel_buttons"],
        {
            "Button": _FakeBtn, "re": _re,
            "normalize_manager_key": normalize_manager_key,
            "_manager_row_by_key": _fake_manager_row_by_key,
        },
    )
    action_btns_fn = ns2["_plc_action_notification_buttons"]
    review_btns_fn = ns2["_plc_terminal_review_buttons"]

    action_rows = action_btns_fn("⚠️ Требуется включить автопродление\n\n• #P1 1.2.3.4:50101")
    action_flat = [b for row in action_rows for b in row]
    check("action-notification buttons include a link to the proxy pool", any(b.data == b"ppool:list:all" for b in action_flat), [b.data for b in action_flat])

    review_body_good = (
        "⛔ newmgr: аккаунт похож на окончательно недоступный\n\n"
        "Менеджер: newmgr\nСтатус: заблокирован (durable), без восстановления дольше 30 мин.\n\n"
        "Подтвердить: /proxy_lifecycle_terminal_confirm newmgr"
    )
    review_rows = review_btns_fn(review_body_good)
    review_flat = [b for row in review_rows for b in row]
    check("terminal-review buttons parse the manager_key and embed its SHORT NUMERIC id (never the raw key)",
          any(b.data == b"plcterm:42" for b in review_flat), [b.data for b in review_flat])
    check("terminal-review buttons never embed the raw manager_key text in callback_data",
          not any(b"newmgr" in (b.data or b"") for b in review_flat), [b.data for b in review_flat])

    review_rows_unknown = review_btns_fn("Подтвердить: /proxy_lifecycle_terminal_confirm ghostmgr")
    review_flat_unknown = [b for row in review_rows_unknown for b in row]
    check("terminal-review buttons fall back safely for an unresolvable manager (no plcterm: button)",
          not any((b.data or b"").startswith(b"plcterm:") for b in review_flat_unknown), [b.data for b in review_flat_unknown])

    review_rows_bad = review_btns_fn("no confirm command line here at all")
    check("terminal-review buttons fall back to EXACTLY back-to-panel on unparseable body (never crashes)",
          button_callbacks(review_rows_bad) == button_callbacks(ns2["_back_to_panel_buttons"]()),
          repr(button_callbacks(review_rows_bad)))

    # ======================================================================
    # plcterm: callback wiring (source-scan, on_callback is a giant monolith)
    # ======================================================================
    on_callback_src = src_of(panel_tree, "on_callback")
    check("on_callback resolves plcterm: BEFORE the generic cmd: branch",
          on_callback_src.index("plcterm:") < on_callback_src.index("'cmd:'"))
    check("plcterm: handler rebuilds the UNCHANGED '/proxy_lifecycle_terminal_confirm <key>' command "
          "(no duplicated confirm logic)",
          "/proxy_lifecycle_terminal_confirm {manager_key}" in on_callback_src)
    check("plcterm: handler resolves via _manager_row_by_id (exact id match, never fuzzy)",
          "_manager_row_by_id(parts[1]" in on_callback_src)

    # ======================================================================
    # Notification kind routing in _panel_notification_loop
    # ======================================================================
    loop_src = src_of(panel_tree, "_panel_notification_loop")
    for kind in ("proxy_lifecycle_enable_required", "proxy_lifecycle_disable_required",
                 "proxy_lifecycle_confirmed_on", "proxy_lifecycle_confirmed_off"):
        check(f"_panel_notification_loop routes kind={kind!r} to _plc_action_notification_buttons",
              kind in loop_src and "_plc_action_notification_buttons(body)" in loop_src)
    check("_panel_notification_loop routes kind='proxy_lifecycle_terminal_review' to _plc_terminal_review_buttons",
          "proxy_lifecycle_terminal_review" in loop_src and "_plc_terminal_review_buttons(body)" in loop_src)

    # ======================================================================
    # _ppool_card_text includes the lifecycle line
    # ======================================================================
    card_src = src_of(panel_tree, "_ppool_card_text")
    check("_ppool_card_text calls _plc_lifecycle_line", "_plc_lifecycle_line(data)" in card_src)

    # ======================================================================
    # main.py: _handle_proxy_pool_card_command surfaces the 4 new fields
    # ======================================================================
    card_cmd_src = src_of(main_tree, "_handle_proxy_pool_card_command")
    for field in ("lifecycle_status", "desired_provider_auto_renew", "observed_provider_auto_renew", "provider_sync_at"):
        check(f"_handle_proxy_pool_card_command's JSON includes {field!r}", f"'{field}'" in card_cmd_src, card_cmd_src[-400:])
    check("_handle_proxy_pool_card_command still never emits a raw 'password' JSON key",
          "'password':" not in card_cmd_src)

    # ======================================================================
    # Callback size: plcterm:<id> stays <=64 bytes even for adversarial ids
    # ======================================================================
    max_int64 = 9223372036854775807
    worst = f"plcterm:{max_int64}".encode("utf-8")
    check("plcterm: callback stays <=64 bytes even for an adversarial near-int64-max manager id",
          len(worst) <= 64, str(len(worst)))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PROXY LIFECYCLE UI SELFTESTS PASSED")
    return 0


def button_callbacks(rows):
    return [b.data for row in rows for b in row]


if __name__ == "__main__":
    raise SystemExit(main())
