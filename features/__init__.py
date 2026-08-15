# -*- coding: utf-8 -*-
"""TPilot feature packages (new modular architecture).

Namespace package only -- no business logic, no imports of main.py,
panel_bot.py, storage.py, manager_bot.py, or telethon at this level or
inside any subpackage's module-load path. Each feature subpackage documents
and enforces its own isolation contract (see prepared_accounts/__init__.py
for the current example).
"""
from __future__ import annotations

__all__: list = []
