# -*- coding: utf-8 -*-
"""Vendored (from-scratch reimplementation, see NOTICE) Telegram Desktop
`tdata` reader: extracts (dc_id, user_id, auth_key) from a tdata directory
without any Telegram network connection.

See NOTICE in this directory for the exact pinned upstream source, the
algorithm-by-algorithm attribution, and what was deliberately NOT ported.

Public surface: `read_tdata_accounts()` in reader.py. Nothing here imports
telethon.TelegramClient, main.py, or opentele.
"""
from .reader import read_tdata_accounts, TdataAccount  # noqa: F401

__all__ = ["read_tdata_accounts", "TdataAccount"]
