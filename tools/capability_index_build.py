# -*- coding: utf-8 -*-
"""
CLI: builds admin_ai\\capability_index.json from the current panel_bot.py +
main.py via admin_ai.index_builder (pure AST extraction, read-only, no
network, no DB, no Telegram). Local build-time tool only -- not imported by
panel_bot.py or main.py at runtime.

Run (locally, from the project root):
    python tools\\capability_index_build.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from admin_ai.index_builder import build_index  # noqa: E402

PANEL_BOT_PY = os.path.join(BASE_DIR, "panel_bot.py")
MAIN_PY = os.path.join(BASE_DIR, "main.py")
OUT_PATH = os.path.join(BASE_DIR, "admin_ai", "capability_index.json")


def main() -> int:
    if not os.path.isfile(PANEL_BOT_PY):
        print(f"[FAIL] panel_bot.py not found at {PANEL_BOT_PY}")
        return 1
    if not os.path.isfile(MAIN_PY):
        print(f"[FAIL] main.py not found at {MAIN_PY}")
        return 1

    index = build_index(PANEL_BOT_PY, MAIN_PY)
    index["built_at"] = datetime.now(timezone.utc).isoformat()

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    diag = index["diagnostics"]
    print(f"[OK] wrote {OUT_PATH}")
    print(f"  tool_version   = {index['tool_version']}")
    print(f"  built_at       = {index['built_at']}")
    print(f"  semantic_hash  = {index['semantic_hash'][:16]}...")
    print(f"  menu_keys      exact={len(index['menu_keys']['exact'])} prefix={len(index['menu_keys']['prefix'])}")
    print(f"  callback_pfx   exact={len(index['callback_prefixes']['exact'])} prefix={len(index['callback_prefixes']['prefix'])}")
    print(f"  commands       exact={len(index['commands']['exact'])} prefix={len(index['commands']['prefix'])} "
          f"dispatch_dict_entries={len(index['commands']['dispatch_dict'])}")
    print(f"  button_callbacks = {len(index['button_callbacks'])} (skipped dynamic: {diag['skipped_dynamic_buttons']})")
    print(f"  panel_bot.py   functions_active={diag['panel_bot.py']['functions_active']} "
          f"shadowed={diag['panel_bot.py']['functions_shadowed']} "
          f"cooperative={diag['panel_bot.py']['cooperative']}")
    print(f"  main.py        functions_active={diag['main.py']['functions_active']} "
          f"shadowed={diag['main.py']['functions_shadowed']} "
          f"cooperative={diag['main.py']['cooperative']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
