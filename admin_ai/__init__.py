# -*- coding: utf-8 -*-
"""
admin_ai — AdminBot AI assistant package (TPilot).

Standalone-importable: this package never imports panel_bot, main,
storage, or telethon. All Telegram/DB access is done by panel_bot and
injected into this package as plain data or callables. See the audit +
implementation plan (owner-approved 2026-07-21) for the full design.

Implemented so far (local milestones P0+P1 -- see the plan's §21):
  - index_builder: AST extraction of the live capability surface from
    panel_bot.py + main.py (menu keys, callback data, /commands)
  - registry:      curated capabilities.json validated against the index
  - schema:        strict JSON response contract + validator
  - entities:      deterministic manager/source/date resolution
  - context:       sanitized live-state snapshot builder

NOT implemented yet (later local milestones -- provider.py, prompt.py,
pipeline.py, and the panel_bot.py glue block are explicitly out of scope
for P0+P1 and must not be created here).
"""
from __future__ import annotations

__version__ = "0.1.0-p0p1"

__all__ = ["__version__"]
