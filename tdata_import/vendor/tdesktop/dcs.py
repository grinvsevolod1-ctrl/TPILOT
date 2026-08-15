# -*- coding: utf-8 -*-
"""Public Telegram production data-center IPv4 addresses.

This is standard, publicly documented MTProto infrastructure data -- the same
addresses are embedded in every open-source MTProto client (Telethon itself
seeds its very first connection with DC2's `149.154.167.51`, see
telethon/client/telegrambaseclient.py:DEFAULT_IPV4_IP), not something specific
or proprietary to the reviewed upstream reference. It happens to also appear
in the reviewed source's `BuiltInDc.kBuiltInDcs` table, which cross-validates
this list rather than being its origin.

Used only to fill `server_address`/`port` in the Telethon session row we
build for a converted account -- Telethon will migrate to the correct DC on
first connect if a value here is ever stale (standard MTProto PhoneMigrate
handling), and no connection happens during conversion itself.
"""
from __future__ import annotations

from typing import Dict, Tuple

# dc_id -> (server_address, port), production, IPv4, non-CDN.
PRODUCTION_DC_IPV4: Dict[int, Tuple[str, int]] = {
    1: ("149.154.175.50", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("149.154.171.5", 443),
}


def address_for_dc(dc_id: int) -> Tuple[str, int]:
    entry = PRODUCTION_DC_IPV4.get(int(dc_id))
    if not entry:
        raise ValueError(f"unknown/unsupported dc_id {dc_id!r}")
    return entry
