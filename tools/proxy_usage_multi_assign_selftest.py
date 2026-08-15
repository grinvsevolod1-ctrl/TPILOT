# -*- coding: utf-8 -*-
"""tools/proxy_usage_multi_assign_selftest.py -- offline self-test for the
"Proxy Usage Detection + Multi-Assign Confirmation + Clean Proxy UI" patch
(main.py, panel_bot.py).

Business rules under test:
* A proxy is "occupied" if its host+port is actively used (proxy_enabled=1)
  by any LIVE manager's own proxy_* fields -- detected by reading real
  manager configuration (manager_list_rows), not just proxy_leases.
  proxy_enabled=0 rows are never counted (owner decision).
* Occupied proxies are excluded from "free"/"available" pool listings.
* A host+port used by a live manager but with NO matching proxy_leases row
  is "external" (сторонний) -- surfaced read-only, masked, with used_by.
* /proxy_pool_assign refuses a real usage conflict (error=proxy_in_use,
  used_by list, never a password) unless the caller passes the trailing
  'multi' token, in which case the SAME proxy is applied to the new manager
  too -- existing user(s) are never detached/cleared, and the lease's own
  manager_key/proxy_lease_id link is NOT reassigned to the new manager.
* Full host:port:login:password only ever appears via a safe local read
  (mirroring the existing PIN reveal) or already-in-hand manual input --
  never via panel_commands result_text. Normal lists/cards stay masked
  (host:port:login:****) or password-free entirely.

Techniques (matching the project's own tools/*_selftest.py conventions):
* storage.py IS importable -> real proxy_leases helpers exercised against a
  throwaway temporary SQLite file (never the real project DB);
* main.py / panel_bot.py are NOT importable (Telethon/env side effects at
  import time) -> the relevant functions are AST-extracted (last top-level
  def wins, per this project's override-stack convention) and exec'd into a
  namespace seeded with fakes for their Telethon/env/log boundaries.
  manager_list_rows / _tpag_registry_get / _tpag_registry_set_fields /
  _tpag_run_guard are faked against a single shared in-memory STATE dict
  (simulating the `managers` table) so the usage map and the assign
  command's storage writes agree with each other, exactly like the real
  single-central-DB system does.

Pure/offline: no network, no Telegram, no production DB, no external APIs.

    python3.12 tools\\proxy_usage_multi_assign_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage
from manager_registry import normalize_manager_key

MAIN_PY = BASE_DIR / "main.py"
PANEL_BOT_PY = BASE_DIR / "panel_bot.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def find_defs(path: Path, name: str) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return [n for n in tree.body if getattr(n, "name", None) == name]


def extract_and_exec(path: Path, names: set, extra_ns: dict) -> dict:
    """AST-extract the LAST (active) def of each requested name and exec into a
    seeded namespace -- same technique as the other tools/*_selftest.py files."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    picked: dict = {}
    for node in tree.body:
        if getattr(node, "name", None) in names:
            picked[node.name] = node  # later defs overwrite earlier -> last wins
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"could not find {missing} as top-level defs in {path}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in sorted(picked))
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path.name}>", "exec"), ns)
    return ns


class FakeButton:
    def __init__(self, text, data):
        self.text = str(text)
        self.data = data if isinstance(data, bytes) else str(data).encode()

    @classmethod
    def inline(cls, text, data=b""):
        return cls(text, data)


_COMMON_TYPE_NS = {"Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple}


# ======================================================================
# Shared fake "managers table" -- a single in-memory dict used by
# manager_list_rows / _tpag_registry_get / _tpag_registry_set_fields, so
# the usage map (reads) and the assign command (writes) always agree,
# exactly like the real single-central-DB system does.
# ======================================================================

def _make_manager_state_fakes(state: Dict[str, Dict[str, Any]]):
    async def _fake_manager_list_rows(include_removed: bool = False):
        return [dict(v) for v in state.values()]

    async def _fake_tpag_registry_get(key):
        k = normalize_manager_key(key or "")
        return dict(state[k]) if k in state else None

    async def _fake_tpag_registry_set_fields(key, **fields):
        k = normalize_manager_key(key or "")
        state.setdefault(k, {"manager_key": k})
        state[k].update(fields)

    async def _fake_tpag_run_guard(key, *, source: str = "", force: bool = False):
        return True, "ok"

    def _fake_manager_proxy_info_text(row):
        return f"proxy_info:{(row or {}).get('manager_key', '')}"

    def _fake_now_utc_iso():
        return "2026-07-11T00:00:00"

    return {
        "manager_list_rows": _fake_manager_list_rows,
        "_tpag_registry_get": _fake_tpag_registry_get,
        "_tpag_registry_set_fields": _fake_tpag_registry_set_fields,
        "_tpag_run_guard": _fake_tpag_run_guard,
        "_manager_proxy_info_text": _fake_manager_proxy_info_text,
        "_now_utc_iso": _fake_now_utc_iso,
    }


def _seed_manager(state: Dict[str, Dict[str, Any]], key: str, *, host="", port=None, login="", enabled=0,
                   display_name="", telegram_username="", status="active", proxy_lease_id=None) -> None:
    k = normalize_manager_key(key)
    state[k] = {
        "manager_key": k,
        "display_name": display_name or k,
        "telegram_username": telegram_username,
        "proxy_host": host, "proxy_port": port, "proxy_username": login,
        "proxy_enabled": enabled, "status": status, "proxy_lease_id": proxy_lease_id,
    }


# ======================================================================
# GROUP A -- _ppool_manager_usage_map (REAL execution via AST)
# ======================================================================

def run_usage_map_checks() -> None:
    print("\n-- main.py: _ppool_manager_usage_map (REAL execution via AST) --")

    state: Dict[str, Dict[str, Any]] = {}
    _seed_manager(state, "dariass", host="1.2.3.4", port=1080, login="u1", enabled=1, display_name="Дарья", telegram_username="dariass_tg")
    _seed_manager(state, "offmgr", host="1.2.3.4", port=1080, login="u2", enabled=0, display_name="Off Mgr")  # disabled -- must NOT count
    _seed_manager(state, "noproxy", host="", port=None, enabled=0, display_name="No Proxy")

    ns = extract_and_exec(
        MAIN_PY, {"_ppool_manager_usage_map", "_ppool_usage_key", "_ppool_usage_public"},
        {**_COMMON_TYPE_NS, "registry_normalize_manager_key": normalize_manager_key,
         **_make_manager_state_fakes(state)},
    )
    usage_map = asyncio.run(ns["_ppool_manager_usage_map"]())

    check("1. usage map matches the enabled manager by normalized host+port",
          ("1.2.3.4", 1080) in usage_map and any(u["manager_key"] == "dariass" for u in usage_map[("1.2.3.4", 1080)]),
          repr(usage_map))
    check("2. a proxy_enabled=0 manager is NEVER counted as occupying its proxy",
          not any(u["manager_key"] == "offmgr" for u in usage_map.get(("1.2.3.4", 1080), [])),
          repr(usage_map.get(("1.2.3.4", 1080))))
    check("login is carried as extra info on the usage entry",
          any(u.get("proxy_login") == "u1" for u in usage_map.get(("1.2.3.4", 1080), [])))
    check("a manager with no host/port never contributes a usage-map entry",
          all("noproxy" not in [u["manager_key"] for u in entries] for entries in usage_map.values()))

    # host normalization: mixed case + surrounding whitespace collapse to
    # the same key as the canonical lower/stripped form.
    key_norm = ns["_ppool_usage_key"](" 1.2.3.4 ".upper(), "1080")
    check("_ppool_usage_key normalizes host (case/whitespace) and coerces port to int",
          key_norm == ("1.2.3.4", 1080), repr(key_norm))
    check("_ppool_usage_key returns None for a missing/invalid port",
          ns["_ppool_usage_key"]("1.2.3.4", "not-a-port") is None)

    public = ns["_ppool_usage_public"](usage_map[("1.2.3.4", 1080)])
    check("_ppool_usage_public strips down to identity-only fields (no login/password key at all)",
          all(set(u.keys()) == {"manager_key", "display_name", "telegram_username"} for u in public), repr(public))


# ======================================================================
# GROUP B -- _ppool_derive_status / _ppool_lease_summary occupied
# classification (REAL execution via AST).
# ======================================================================

def run_derive_status_checks(tmp_db: str) -> None:
    print("\n-- main.py: _ppool_derive_status / _ppool_lease_summary occupied (REAL execution via AST) --")

    state: Dict[str, Dict[str, Any]] = {}
    _seed_manager(state, "external_user", host="9.9.9.9", port=2000, login="ext", enabled=1, display_name="External User")
    _seed_manager(state, "assigned_owner", host="5.5.5.5", port=3000, login="own", enabled=1, display_name="Assigned Owner", status="active")

    ns = extract_and_exec(
        MAIN_PY,
        {"_ppool_derive_status", "_ppool_lease_summary", "_ppool_manager_usage_map", "_ppool_usage_key", "_ppool_usage_public",
         "_ppool_parse_expires_at"},
        {**_COMMON_TYPE_NS, "registry_normalize_manager_key": normalize_manager_key,
         "datetime": __import__("datetime").datetime,
         **_make_manager_state_fakes(state)},
    )
    derive = ns["_ppool_derive_status"]
    summary = ns["_ppool_lease_summary"]
    usage_map = asyncio.run(ns["_ppool_manager_usage_map"]())

    # 5. lease with no manager_key, host+port matches usage_map -> occupied
    lease_occupied = {"id": 1, "host": "9.9.9.9", "port": 2000, "manager_key": "", "status": "active", "expires_at": ""}
    status, mgr = asyncio.run(derive(lease_occupied, usage_map=usage_map))
    check("5. a free lease whose host+port is actively used elsewhere is classified 'occupied', never 'free'",
          status == "occupied", status)

    # 6. lease with no manager_key, host+port not in usage map -> free
    lease_free = {"id": 2, "host": "8.8.8.8", "port": 4000, "manager_key": "", "status": "active", "expires_at": ""}
    status2, _ = asyncio.run(derive(lease_free, usage_map=usage_map))
    check("6. a lease whose host+port matches nobody stays 'free'", status2 == "free", status2)

    # 7. assigned (lease.manager_key set) takes priority over occupied even
    # though the assigned manager also appears in the usage map themselves.
    lease_assigned = {"id": 3, "host": "5.5.5.5", "port": 3000, "manager_key": "assigned_owner", "status": "active", "expires_at": ""}
    status3, mgr3 = asyncio.run(derive(lease_assigned, usage_map=usage_map))
    check("7. a lease.manager_key-owned lease reports 'assigned', not 'occupied' (priority order)",
          status3 == "assigned", status3)

    # Backward compatibility: omitting usage_map entirely must not change
    # the pre-existing free/assigned/... behavior at all.
    status_nomap, _ = asyncio.run(derive(lease_occupied))
    check("omitting usage_map (existing callers) preserves the original free/assigned/... behavior byte-for-byte",
          status_nomap == "free", status_nomap)

    # 9. used_by is populated in the summary for an occupied lease.
    s = asyncio.run(summary(lease_occupied, usage_map=usage_map))
    check("9. lease summary's used_by lists the real occupant for an 'occupied' lease",
          s["status"] == "occupied" and any(u["manager_key"] == "external_user" for u in s["used_by"]), repr(s))
    check("used_by never carries a password key", all("password" not in u and "proxy_login" not in u for u in s["used_by"]))


# ======================================================================
# GROUP C -- _handle_proxy_pool_list_command: available/occupied/external
# (REAL execution via AST + real proxy_leases in a temp SQLite file).
# ======================================================================

def run_list_command_checks(tmp_db: str) -> None:
    print("\n-- main.py: _handle_proxy_pool_list_command available/occupied/external (REAL execution via AST) --")

    state: Dict[str, Dict[str, Any]] = {}
    _seed_manager(state, "occupier", host="7.7.7.7", port=5000, login="occ", enabled=1, display_name="Occupier")
    _seed_manager(state, "ext_user", host="6.6.6.6", port=6000, login="extlogin", enabled=1, display_name="Ext User", telegram_username="extuser_tg")

    con = sqlite3.connect(tmp_db)
    con.close()  # just ensure the file exists; storage.py creates the table lazily

    lease_id_free = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="1.1.1.1", port=1111, db_path=tmp_db,
    ))
    lease_id_occupied = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="7.7.7.7", port=5000, db_path=tmp_db,
    ))

    ns = extract_and_exec(
        MAIN_PY,
        {"_handle_proxy_pool_list_command", "_ppool_derive_status", "_ppool_lease_summary",
         "_ppool_manager_usage_map", "_ppool_usage_key", "_ppool_usage_public", "_ppool_parse_expires_at"},
        {**_COMMON_TYPE_NS, "registry_normalize_manager_key": normalize_manager_key,
         "TPILOT_DB_PATH": tmp_db, "_pbuy_json": json,
         "datetime": __import__("datetime").datetime,
         # Module-level constant tuples aren't FunctionDefs -- extract_and_exec
         # only picks up defs by name, so these are mirrored here verbatim
         # from main.py (see run_safety_checks-adjacent literal-sync note
         # below; kept byte-identical intentionally, not re-derived).
         "_PPOOL_VALID_FILTERS": ("all", "free", "occupied", "assigned", "orphaned", "expired", "disabled", "external", "available"),
         "_PPOOL_AVAILABLE_STATUSES": ("free", "orphaned"),
         **_make_manager_state_fakes(state)},
    )
    fn = ns["_handle_proxy_pool_list_command"]

    result_available = json.loads(asyncio.run(fn("available")))
    check("10. filt=available EXCLUDES an occupied proxy",
          not any(it["lease_id"] == lease_id_occupied for it in result_available["items"]), repr(result_available))
    check("filt=available still includes a genuinely free lease",
          any(it["lease_id"] == lease_id_free for it in result_available["items"]), repr(result_available))

    result_occupied = json.loads(asyncio.run(fn("occupied")))
    check("11. filt=occupied returns exactly the occupied lease, with used_by populated",
          len(result_occupied["items"]) == 1 and result_occupied["items"][0]["lease_id"] == lease_id_occupied
          and any(u["manager_key"] == "occupier" for u in result_occupied["items"][0]["used_by"]),
          repr(result_occupied))

    result_external = json.loads(asyncio.run(fn("external")))
    ext_hosts = [(it["host"], it["port"]) for it in result_external["items"]]
    check("12. filt=external finds a manager-used host+port with NO matching proxy_leases row",
          ("6.6.6.6", 6000) in ext_hosts, repr(result_external))
    check("filt=external does NOT include a host+port that DOES have a proxy_leases row (that's 'occupied', not 'external')",
          ("7.7.7.7", 5000) not in ext_hosts, repr(result_external))
    raw_json = json.dumps(result_external, ensure_ascii=False)
    check("external items never carry a password anywhere in the JSON",
          "password" not in raw_json, raw_json)


# ======================================================================
# GROUP D -- _handle_proxy_pool_assign_command: conflict + multi-assign
# (REAL execution via AST + real proxy_leases).
# ======================================================================

def run_assign_conflict_checks(tmp_db: str) -> None:
    print("\n-- main.py: _handle_proxy_pool_assign_command conflict/multi (REAL execution via AST) --")

    state: Dict[str, Dict[str, Any]] = {}
    # Scenario A: a FREE lease (no tracked owner), but an INDEPENDENT
    # manager (external_dup) already has proxy_enabled=1 at the exact same
    # host+port via their OWN config (never through this lease).
    _seed_manager(state, "external_dup", host="3.3.3.3", port=7000, login="extlogin", enabled=1, display_name="External Dup", telegram_username="extdup_tg")
    _seed_manager(state, "newbie", host="", port=None, enabled=0, display_name="Newbie", status="active")
    # Scenario B: a normally-owned lease with NO independent conflicts --
    # plain reassign must keep working exactly as before (regression guard).
    _seed_manager(state, "owner_b", host="4.4.4.4", port=8000, login="blogin", enabled=1, display_name="Owner B")
    _seed_manager(state, "target_c", host="", port=None, enabled=0, display_name="Target C", status="active")
    # Scenario C: a normally-owned lease PLUS an independent third manager
    # also using the same host+port (unrelated to the lease's own tracking).
    _seed_manager(state, "owner_d", host="5.5.5.5", port=9000, login="dlogin", enabled=1, display_name="Owner D")
    _seed_manager(state, "intruder", host="5.5.5.5", port=9000, login="ilogin", enabled=1, display_name="Intruder", telegram_username="intruder_tg")
    _seed_manager(state, "target_e", host="", port=None, enabled=0, display_name="Target E", status="active")

    lease_a = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="3.3.3.3", port=7000, db_path=tmp_db,
    ))
    lease_b = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="4.4.4.4", port=8000,
        manager_key="owner_b", login="blogin", password="secretpw_b", db_path=tmp_db,
    ))
    asyncio.run(storage.proxy_lease_assign_to_manager(lease_b, "owner_b", db_path=tmp_db))
    state["owner_b"]["proxy_lease_id"] = lease_b
    lease_d = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="5.5.5.5", port=9000,
        manager_key="owner_d", login="dlogin", password="secretpw_d", db_path=tmp_db,
    ))
    asyncio.run(storage.proxy_lease_assign_to_manager(lease_d, "owner_d", db_path=tmp_db))
    state["owner_d"]["proxy_lease_id"] = lease_d

    ns = extract_and_exec(
        MAIN_PY,
        {"_handle_proxy_pool_assign_command", "_pbuy_apply_lease_to_manager", "_ppool_disable_manager_proxy_fields",
         "_ppool_derive_status", "_ppool_manager_usage_map", "_ppool_usage_key", "_ppool_usage_public",
         "_ppool_parse_expires_at"},
        {**_COMMON_TYPE_NS, "registry_normalize_manager_key": normalize_manager_key,
         "TPILOT_DB_PATH": tmp_db, "_pbuy_json": json,
         "datetime": __import__("datetime").datetime,
         "_pbuy_safe_error": lambda e: repr(e),
         **_make_manager_state_fakes(state)},
    )
    fn = ns["_handle_proxy_pool_assign_command"]

    # --- Scenario A: free lease, independent occupant -------------------
    result_a_conflict = json.loads(asyncio.run(fn(f"{lease_a} newbie")))
    check("13. assign without 'multi' to a free lease whose host+port is used by an "
          "INDEPENDENT manager returns error=proxy_in_use",
          result_a_conflict.get("ok") is False and result_a_conflict.get("error") == "proxy_in_use", repr(result_a_conflict))
    check("13b. proxy_in_use response lists the real (independent) occupant in used_by",
          any(u.get("manager_key") == "external_dup" for u in result_a_conflict.get("used_by") or []), repr(result_a_conflict))
    raw_conflict = json.dumps(result_a_conflict, ensure_ascii=False)
    check("13c. proxy_in_use response never contains a password anywhere in the JSON",
          "password" not in raw_conflict, raw_conflict)
    check("newbie was NOT modified by the refused assign",
          int(state["newbie"].get("proxy_enabled") or 0) == 0)

    result_a_multi = json.loads(asyncio.run(fn(f"{lease_a} newbie multi")))
    check("14. assign with 'multi' succeeds and applies the proxy to the new manager",
          result_a_multi.get("ok") is True and int(state["newbie"].get("proxy_enabled") or 0) == 1
          and str(state["newbie"].get("proxy_host")) == "3.3.3.3", repr((result_a_multi, state["newbie"])))
    check("14b. multi_assigned flag is reported true", result_a_multi.get("multi_assigned") is True, repr(result_a_multi))
    check("15. the independent occupant (external_dup) is left completely untouched -- still enabled, same host/port",
          int(state["external_dup"].get("proxy_enabled") or 0) == 1 and str(state["external_dup"].get("proxy_host")) == "3.3.3.3",
          repr(state["external_dup"]))
    lease_a_after = asyncio.run(storage.proxy_lease_get(lease_a, db_path=tmp_db))
    check("a previously-FREE lease IS linked to the new manager under multi (nothing to protect)",
          str((lease_a_after or {}).get("manager_key") or "") == "newbie", repr(lease_a_after))

    # --- Scenario B: owned lease, plain reassign, NO conflict (regression) ---
    result_b = json.loads(asyncio.run(fn(f"{lease_b} target_c")))
    check("plain reassign FROM the lease's own current owner (no independent conflict) "
          "still succeeds exactly as before -- NOT blocked as proxy_in_use",
          result_b.get("ok") is True, repr(result_b))
    check("plain reassign detaches the old owner (owner_b) as before",
          int(state["owner_b"].get("proxy_enabled") or 0) == 0, repr(state["owner_b"]))
    check("plain reassign links the new manager (target_c) as before",
          int(state["target_c"].get("proxy_enabled") or 0) == 1, repr(state["target_c"]))
    check("plain reassign reports old_manager_key (unchanged existing behavior)",
          result_b.get("old_manager_key") == "owner_b", repr(result_b))

    # --- Scenario C: owned lease + an INDEPENDENT third-party conflict ---
    result_c_conflict = json.loads(asyncio.run(fn(f"{lease_d} target_e")))
    check("assign of an OWNED lease still refuses when an INDEPENDENT third manager "
          "(not the target, not the lease's own owner) also uses that host+port",
          result_c_conflict.get("ok") is False and result_c_conflict.get("error") == "proxy_in_use", repr(result_c_conflict))
    check("the conflict list names the independent intruder, NOT the lease's own current owner",
          any(u.get("manager_key") == "intruder" for u in result_c_conflict.get("used_by") or [])
          and not any(u.get("manager_key") == "owner_d" for u in result_c_conflict.get("used_by") or []),
          repr(result_c_conflict))

    result_c_multi = json.loads(asyncio.run(fn(f"{lease_d} target_e multi")))
    check("16. assign with 'multi' on an OWNED lease succeeds and applies the proxy to the new manager",
          result_c_multi.get("ok") is True and int(state["target_e"].get("proxy_enabled") or 0) == 1, repr(result_c_multi))
    check("the lease's ORIGINAL owner (owner_d) is left completely untouched",
          int(state["owner_d"].get("proxy_enabled") or 0) == 1, repr(state["owner_d"]))
    check("the unrelated intruder is also left completely untouched",
          int(state["intruder"].get("proxy_enabled") or 0) == 1, repr(state["intruder"]))
    check("old_manager_key is reported None under multi (nobody was detached)",
          result_c_multi.get("old_manager_key") is None, repr(result_c_multi))
    lease_d_after = asyncio.run(storage.proxy_lease_get(lease_d, db_path=tmp_db))
    check("the lease keeps tracking its ORIGINAL owner (owner_d), NOT reassigned to target_e under multi",
          str((lease_d_after or {}).get("manager_key") or "") == "owner_d", repr(lease_d_after))

    # Idempotent re-assign to the lease's OWN sole current occupant is never blocked.
    result_idempotent = json.loads(asyncio.run(fn(f"{lease_b} target_c")))
    check("re-assigning a lease to its OWN sole current occupant is never blocked as a conflict",
          result_idempotent.get("ok") is True, repr(result_idempotent))


# ======================================================================
# GROUP E -- panel_bot.py UI helpers (masked line, success card, used_by)
# (REAL execution via AST + FakeButton).
# ======================================================================

def run_panel_ui_checks() -> None:
    print("\n-- panel_bot.py: masked line / success card / used_by rendering (REAL execution via AST) --")

    ns = extract_and_exec(
        PANEL_BOT_PY,
        {"_ppool_masked_line", "_ppool_used_by_bullets", "_ppool_used_by_lines",
         "_ppool_multi_assign_warning_text", "_proxy_success_card_text"},
        {**_COMMON_TYPE_NS, "Button": FakeButton},
    )

    masked = ns["_ppool_masked_line"]("1.2.3.4", 1080, "user1")
    check("17. masked list format is exactly host:port:login:****",
          masked == "1.2.3.4:1080:user1:****", masked)
    check("masked line never contains a real password fragment",
          "****" in masked and masked.count(":") == 3, masked)

    used_by = [
        {"manager_key": "ivan", "display_name": "Иван", "telegram_username": "ivan123"},
        {"manager_key": "petr", "display_name": "Пётр", "telegram_username": ""},
    ]
    bullets = ns["_ppool_used_by_bullets"](used_by)
    check("used_by bullets include display name and @username when available",
          any("Иван" in b and "@ivan123" in b for b in bullets), repr(bullets))
    check("used_by bullets omit the @username segment when not available",
          any("Пётр" in b and "@" not in b for b in bullets), repr(bullets))

    warning_text = ns["_ppool_multi_assign_warning_text"](used_by)
    check("multi-assign warning text includes the required prompt line",
          "Установить этот прокси ещё одному аккаунту?" in warning_text, warning_text)
    check("multi-assign warning text never contains a password",
          "password" not in warning_text.lower() and "****" not in warning_text, warning_text)

    # 18. success card builds the full host:port:login:password row only
    # from explicit values passed in by the caller -- never touches
    # panel_commands result_text (this function has no such parameter at
    # all, which is itself the safety property).
    card = ns["_proxy_success_card_text"](
        manager_key="ivan123mgr", display_name="Иван", telegram_username="ivan123",
        host="89.248.68.227", port=62913, login="VADQiD83", password="4fk5w3Ns",
    )
    check("18. success card includes the exact required host:port:login:password row",
          "`89.248.68.227:62913:VADQiD83:4fk5w3Ns`" in card, card)
    check("success card includes account identity lines",
          "• Ключ: ivan123mgr" in card and "• Имя: Иван" in card and "• Username: @ivan123" in card, card)
    check("success card includes the delete-after-copy reminder",
          "Удалите сообщение" in card, card)
    check("success card is the only tested surface containing the raw password 4fk5w3Ns -- "
          "confirms it is a deliberate, explicit render, not an accidental leak",
          "4fk5w3Ns" in card)


# ======================================================================
# GROUP E2 -- panel_bot.py _ppool_run_assign_confirm_screen conflict
# filtering (REAL execution via AST). BLOCKER REGRESSION GUARD 20260711:
# a prior version excluded only the target manager from the panel-side
# conflict check, so a plain reassign FROM the lease's own current owner
# (who naturally appears in used_by, since applying a lease sets their own
# proxy_* fields) was misrouted into the multi-assign warning -- confirming
# it then silently turned a normal single-owner transfer into a
# multi-assign (both old and new manager left on the same proxy), breaking
# the "1 manager = 1 proxy by default" rule. Fixed to exclude BOTH the
# target and the lease's own current owner, mirroring
# _handle_proxy_pool_assign_command's own filter exactly.
# ======================================================================

class _FakePanelClient:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons})


def _run_confirm_screen(card_json: dict, manager_key: str):
    """Real execution of _ppool_run_assign_confirm_screen via AST extraction,
    with _submit_and_wait faked to return the given /proxy_pool_card JSON
    payload (as if the controller already answered), client.send_message
    and _wizard_set faked to record what was sent/stored. Returns
    (fake_client, wizard_calls)."""
    client_fake = _FakePanelClient()
    wizard_calls: list = []

    def _fake_wizard_set(chat_id, user_id, wizard, step, payload=None):
        wizard_calls.append({"wizard": wizard, "step": step, "payload": payload})

    async def _fake_submit_and_wait(command_text, user_id, source_chat_id, response_chat_id):
        return {"status": "done", "result_text": json.dumps(card_json, ensure_ascii=False)}

    ns = extract_and_exec(
        PANEL_BOT_PY,
        {"_ppool_run_assign_confirm_screen", "_ppool_parse_result_json", "_ppool_multi_assign_warning_text",
         "_ppool_multi_assign_buttons", "_ppool_assign_confirm_text", "_ppool_assign_confirm_buttons",
         "_ppool_used_by_bullets"},
        {
            **_COMMON_TYPE_NS, "Button": FakeButton, "normalize_manager_key": normalize_manager_key,
            "json": json,
            "_safe_text": lambda s: str(s or ""),
            "client": client_fake,
            "_wizard_set": _fake_wizard_set,
            "_submit_and_wait": _fake_submit_and_wait,
            "_ppool_root_buttons": lambda: [[FakeButton.inline("Назад", b"panel:back")]],
        },
    )
    fn = ns["_ppool_run_assign_confirm_screen"]
    asyncio.run(fn(111, 222, card_json.get("lease_id", 1), manager_key))
    return client_fake, wizard_calls


def run_panel_confirm_screen_conflict_checks() -> None:
    print("\n-- panel_bot.py: _ppool_run_assign_confirm_screen conflict filtering (REAL execution via AST) --")

    # Scenario 1: lease is currently assigned to owner A (used_by contains
    # ONLY A, since applying a lease sets A's own proxy_* fields). Target is
    # B. This is a PLAIN REASSIGN -- must NOT show the multi-assign warning
    # just because A (the very owner being replaced) is in used_by.
    card_plain = {
        "ok": True, "lease_id": 501, "manager_key": "owner_a",
        "used_by": [{"manager_key": "owner_a", "display_name": "Owner A", "telegram_username": "ownera_tg"}],
    }
    client1, wizard1 = _run_confirm_screen(card_plain, "target_b")
    check("BLOCKER FIX: reassigning FROM the lease's own current owner (A) to a new "
          "manager (B) does NOT trigger the multi-assign warning merely because A is in used_by",
          bool(wizard1) and wizard1[-1]["step"] == "assign_confirm", repr(wizard1))
    check("normal reassign confirm screen is shown (not the multi-assign warning)",
          bool(client1.sent) and "уже используется" not in client1.sent[-1]["text"], repr(client1.sent))
    check("normal reassign confirm text asks about the plain reconnect, per _ppool_assign_confirm_text",
          bool(client1.sent) and "Подключить proxy #501 к менеджеру target_b?" in client1.sent[-1]["text"],
          repr(client1.sent))

    # Scenario 2: same lease/owner A, but used_by ALSO contains an
    # INDEPENDENT manager C (neither the target B nor the current owner A).
    # This IS a genuine conflict -- the multi-assign warning MUST appear.
    card_conflict = {
        "ok": True, "lease_id": 502, "manager_key": "owner_a2",
        "used_by": [
            {"manager_key": "owner_a2", "display_name": "Owner A2", "telegram_username": ""},
            {"manager_key": "indep_c", "display_name": "Indep C", "telegram_username": "indepc_tg"},
        ],
    }
    client2, wizard2 = _run_confirm_screen(card_conflict, "target_b2")
    check("an INDEPENDENT manager (C, neither target nor current owner) in used_by DOES "
          "trigger the multi-assign warning",
          bool(wizard2) and wizard2[-1]["step"] == "assign_multi_confirm", repr(wizard2))
    check("multi-assign warning text is shown with the required confirmation prompt",
          bool(client2.sent) and "Установить этот прокси ещё одному аккаунту?" in client2.sent[-1]["text"],
          repr(client2.sent))
    check("multi-assign warning names the independent conflict (C), not the excluded current owner (A2)",
          bool(client2.sent) and "Indep C" in client2.sent[-1]["text"] and "Owner A2" not in client2.sent[-1]["text"],
          repr(client2.sent))

    # Sanity: a genuinely free lease (empty used_by) also takes the plain
    # confirm path, never the warning.
    card_free = {"ok": True, "lease_id": 503, "manager_key": None, "used_by": []}
    client3, wizard3 = _run_confirm_screen(card_free, "target_b3")
    check("a free lease (no used_by at all) also takes the plain confirm path",
          bool(wizard3) and wizard3[-1]["step"] == "assign_confirm", repr(wizard3))


# ======================================================================
# GROUP F -- safety / scope (structural + regression-style)
# ======================================================================

def run_safety_checks(tmp_db: str) -> None:
    print("\n-- Safety / scope --")
    main_src = MAIN_PY.read_text(encoding="utf-8-sig")
    panel_src = PANEL_BOT_PY.read_text(encoding="utf-8-sig")
    storage_src = (BASE_DIR / "storage.py").read_text(encoding="utf-8-sig")

    def _allow_spend_sites(src: str) -> list:
        tree = ast.parse(src)
        return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                for kw in (n.keywords or [])
                if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]

    check("20. allow_spend=True is still exactly 2 real call-keyword sites, both in main.py "
          "(this patch adds none)",
          len(_allow_spend_sites(main_src)) == 2 and len(_allow_spend_sites(panel_src)) == 0
          and len(_allow_spend_sites(storage_src)) == 0)

    # No spend/provider markers anywhere near the new usage-detection/assign code.
    for fname in ("_ppool_manager_usage_map", "_ppool_derive_status", "_handle_proxy_pool_assign_command",
                  "_pbuy_apply_lease_to_manager"):
        found = find_defs(MAIN_PY, fname)
        check(f"{fname} has at least one top-level def", len(found) >= 1, fname)
        if found:
            body_src = ast.unparse(found[-1])
            for marker in ("allow_spend", "make_ipv4", "prolong_make", "reference_list"):
                check(f"{fname} never references spend/provider marker {marker!r}", marker not in body_src)

    for name, src in (("main.py", main_src), ("panel_bot.py", panel_src)):
        moji = sum(src.count(c) for c in ("Ð", "Ñ")) + src.count("â€")
        check(f"mojibake clean: {name}", moji == 0, str(moji))

    real_path = os.path.realpath(tmp_db)
    check("22. selftest used ONLY temp SQLite files (never db/data_tpilot.db or db/data.db)",
          os.path.realpath(tempfile.gettempdir()) in real_path
          and "data_tpilot.db" not in real_path and (os.sep + "db" + os.sep) not in real_path, real_path)

    self_src = Path(__file__).read_text(encoding="utf-8-sig")
    check("23. selftest makes no network/Telethon/API calls (no requests/telethon/urllib/anthropic imports)",
          not re.search(r"^\s*(import|from)\s+(requests|telethon|urllib|http\.client|anthropic)\b", self_src, re.MULTILINE))

    # Proxy buy/renewal logic itself must be untouched by this patch.
    for fname in ("_handle_manager_proxy_buy_confirm_command", "_prenew_execute_renewal"):
        found = find_defs(MAIN_PY, fname)
        check(f"{fname} is still defined exactly once (proxy buy/renewal untouched)", len(found) == 1, f"count={len(found)}")
        if found:
            body_src = ast.unparse(found[-1])
            check(f"{fname} contains no proxy-usage-detection/multi-assign code (scope stayed narrow)",
                  "_ppool_manager_usage_map" not in body_src and "proxy_in_use" not in body_src)


def main() -> int:
    tmp_db = tempfile.mktemp(suffix="_proxy_usage_multi_assign_selftest.db")
    try:
        run_usage_map_checks()
        run_derive_status_checks(tmp_db)
        run_list_command_checks(tmp_db)
        run_assign_conflict_checks(tmp_db)
        run_panel_ui_checks()
        run_panel_confirm_screen_conflict_checks()
        run_safety_checks(tmp_db)
    finally:
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except Exception:
            pass

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
