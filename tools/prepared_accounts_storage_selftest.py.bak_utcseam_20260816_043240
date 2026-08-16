# -*- coding: utf-8 -*-
"""Phase 2 selftest for storage.py's prepared_accounts side table and for
features.prepared_accounts.repository's INNER JOIN visibility layer.

Uses a throwaway temporary SQLite file only -- never touches the real
project DB (see _guard_temp_db). Pure/offline: no network, no Telegram, no
proxy-provider calls. A short subprocess is spawned once (fresh-interpreter
import-isolation check only) -- also fully local/offline, not a network call.

Run: python3.12 tools\\prepared_accounts_storage_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite  # noqa: E402
import manager_registry  # noqa: E402
import storage  # noqa: E402
from features.prepared_accounts import model, repository  # noqa: E402

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _guard_temp_db(db_path: str) -> None:
    prod_db_dir = os.path.abspath(os.path.join(str(BASE_DIR), "db"))
    target = os.path.abspath(str(db_path))
    assert target != prod_db_dir and not target.startswith(prod_db_dir + os.sep), \
        f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"


# --- deterministic fake clock (no real sleep needed for TTL-claim tests) ----
_BASE_DT = _dt.datetime(2026, 1, 1, 0, 0, 0)


class _FrozenDateTime(_dt.datetime):
    _offset_seconds = 0

    @classmethod
    def utcnow(cls):
        return _BASE_DT + _dt.timedelta(seconds=cls._offset_seconds)


def _set_fake_offset(seconds: int) -> None:
    _FrozenDateTime._offset_seconds = seconds


def _fake_now_iso(offset_seconds: int) -> str:
    return (_BASE_DT + _dt.timedelta(seconds=offset_seconds)).isoformat()


def _src_without_docstring(node) -> str:
    """Established project technique (see tools/proxy_pool_selftest.py):
    drop only a function/class's OWN leading docstring, keep every other
    string literal in its body untouched. Deliberately narrower than
    _code_without_string_literals below -- check 26b needs to tell apart a
    docstring's explanatory PROSE mention of "auth_source" (which must not
    trip the check) from a real `.get("auth_profile")`-style string literal
    used as actual code (which must NOT be blanked away, or the check could
    never observe the real key being read)."""
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(n) for n in body)


def _code_without_string_literals(source: str) -> str:
    """AST-aware reconstruction of `source` with every string literal's
    value blanked out (docstrings included), leaving imports/identifiers/
    attribute-access/calls intact -- the same technique
    prepared_accounts_pkg_selftest.py uses, duplicated here since each
    tools/*_selftest.py in this project is self-contained (no shared
    selftest-utils module exists)."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


async def _ensure_full_schema(db_path: str) -> None:
    """Real managers table (+ every other table storage.init_db() owns),
    PLUS the proxy_lease_id link column that _proxy_leases_table_ready adds
    to managers -- both needed so the view model's proxy_lease_id field is
    a real column, exactly as it is on the live project DB."""
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    await storage.init_db()
    async with aiosqlite.connect(db_path) as db:
        await storage._proxy_leases_table_ready(db)


async def _make_manager(db_path: str, manager_key: str, **overrides) -> None:
    fields = dict(
        status="new", is_enabled=1, manual_stopped=0, tg_user_id=None,
        phone="", telegram_username="", first_name="", last_name="",
        display_name=manager_key, session_path="",
        proxy_type="", proxy_host="", proxy_port=None, proxy_enabled=0, proxy_lease_id=None,
        auth_profile="project", owner_user_id=None,
    )
    fields.update(overrides)
    async with aiosqlite.connect(db_path) as db:
        cols = ["manager_key"] + list(fields.keys())
        vals = [manager_key] + list(fields.values())
        placeholders = ",".join("?" * len(cols))
        await db.execute(f"INSERT INTO managers({','.join(cols)}) VALUES ({placeholders})", vals)
        await db.commit()


# Baseline managers column set as created by storage.init_db() (base CREATE
# TABLE + its ALTER block) plus the proxy_lease_id link column added by
# _proxy_leases_table_ready -- captured from storage.py as it exists BEFORE
# this phase's edit (that edit only appends a new prepared_accounts block
# at the end of the file; it does not touch any managers CREATE/ALTER
# statement). A future accidental change to managers' schema breaks this
# set-equality check.
_EXPECTED_MANAGERS_COLUMNS = frozenset({
    "id", "manager_key", "display_name", "phone", "status", "session_path", "db_path",
    "workdir", "log_path", "is_enabled", "manual_stopped", "owner_user_id", "tg_user_id",
    "telegram_username", "first_name", "last_name", "last_login_at", "last_error",
    "created_at", "updated_at",
    "proxy_type", "proxy_host", "proxy_port", "proxy_username", "proxy_password",
    "proxy_enabled", "proxy_updated_at", "proxy_mode",
    "auth_guard_ok", "auth_guard_checked_at", "auth_proxy_ip", "auth_proxy_country",
    "auth_proxy_city", "auth_proxy_region", "auth_direct_ip", "auth_direct_country",
    "auth_direct_city", "auth_direct_region", "auth_guard_error", "auth_guard_source",
    "auth_guard_last_bad_at", "auth_guard_last_ok_at", "auth_guard_notified_bad",
    "proxy_required", "proxy_test_ok", "proxy_test_at", "proxy_last_error",
    "proxy_bypass_allowed", "proxy_bypass_by", "proxy_bypass_at", "proxy_bypass_reason",
    "display_name_source", "auth_profile", "proxy_lease_id",
})

DESTRUCTIVE_DDL_MARKERS = ("DROP TABLE", "DROP COLUMN", "ALTER TABLE managers RENAME", "REPLACE INTO")


def main() -> int:  # noqa: C901 (single-file selftest, straight-line checks)
    with tempfile.TemporaryDirectory(prefix="tpilot_prepacc_storage_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)

        asyncio.run(_ensure_full_schema(tmp_db))
        storage.datetime = _FrozenDateTime  # freeze the clock used by _now_iso and claim's cutoff math
        _set_fake_offset(0)

        # --- 1: CREATE TABLE idempotent ---------------------------------------
        storage.ensure_prepared_accounts_table(db_path=tmp_db)
        storage.ensure_prepared_accounts_table(db_path=tmp_db)
        con = storage._bsl_connect(tmp_db)
        try:
            exists = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prepared_accounts'"
            ).fetchone()
        finally:
            con.close()
        check("1. CREATE TABLE idempotent (table exists after two ensure calls)", exists is not None)

        # --- 2: repeat ensure does not change data -----------------------------
        asyncio.run(_make_manager(tmp_db, "acc_ensure", status="prepared"))
        storage.prepared_account_upsert("acc_ensure", auth_source="qr", db_path=tmp_db)
        before = storage.prepared_account_get("acc_ensure", db_path=tmp_db)
        storage.ensure_prepared_accounts_table(db_path=tmp_db)
        after = storage.prepared_account_get("acc_ensure", db_path=tmp_db)
        check("2. repeat ensure_prepared_accounts_table does not change existing rows", before == after, detail=f"{before} vs {after}")

        # --- 3/4: upsert create + repeat-upsert preserves prepared_at/created_at
        _set_fake_offset(0)
        asyncio.run(_make_manager(tmp_db, "acc_upsert", status="prepared"))
        row1 = storage.prepared_account_upsert("acc_upsert", auth_source="qr", prepared_by_user_id=111, db_path=tmp_db)
        check("3. upsert creates a side-row", row1 is not None and row1.get("manager_key") == "acc_upsert")
        check("3b. first upsert sets prepared_at/created_at/updated_at", row1.get("prepared_at") == _fake_now_iso(0) and row1.get("created_at") == _fake_now_iso(0) and row1.get("updated_at") == _fake_now_iso(0))
        check("3c. first upsert stores auth_source/prepared_by_user_id", row1.get("auth_source") == "qr" and row1.get("prepared_by_user_id") == 111)

        _set_fake_offset(100)
        row2 = storage.prepared_account_upsert("acc_upsert", auth_source="phone", db_path=tmp_db)
        check("4a. second upsert updates auth_source", row2.get("auth_source") == "phone")
        check("4b. second upsert updates updated_at", row2.get("updated_at") == _fake_now_iso(100))
        check("4c. second upsert preserves created_at", row2.get("created_at") == row1.get("created_at"))
        check("4d. second upsert preserves prepared_at", row2.get("prepared_at") == row1.get("prepared_at"))
        check("4e. second upsert (no prepared_by_user_id given) preserves it", row2.get("prepared_by_user_id") == 111)

        # --- 5: get of a missing key = None ------------------------------------
        check("5a. storage.prepared_account_get missing -> None", storage.prepared_account_get("acc_never_existed", db_path=tmp_db) is None)
        check("5b. repository.get_prepared missing -> None", repository.get_prepared("acc_never_existed", db_path=tmp_db) is None)

        # --- 6/7: delete removes only the side-row, managers row survives -----
        asyncio.run(_make_manager(tmp_db, "acc_delete", status="prepared"))
        storage.prepared_account_upsert("acc_delete", auth_source="qr", db_path=tmp_db)
        deleted = storage.prepared_account_delete("acc_delete", db_path=tmp_db)
        check("6. delete removes the side-row", deleted and storage.prepared_account_get("acc_delete", db_path=tmp_db) is None)
        check("7. managers row survives the side-row delete", manager_registry.get_manager_row_from_db_sync(tmp_db, "acc_delete") is not None)

        # --- 8/9: mark_verify OK / FAIL ------------------------------------------
        _set_fake_offset(200)
        asyncio.run(_make_manager(tmp_db, "acc_verify_ok", status="prepared", tg_user_id=555))
        storage.prepared_account_upsert("acc_verify_ok", auth_source="qr", db_path=tmp_db)
        storage.prepared_account_mark_verify("acc_verify_ok", True, db_path=tmp_db)
        v_ok = storage.prepared_account_get("acc_verify_ok", db_path=tmp_db)
        check("8. mark_verify OK sets last_verify_ok=1/empty error/timestamp", v_ok.get("last_verify_ok") == 1 and v_ok.get("last_verify_error") == "" and v_ok.get("last_verify_at") == _fake_now_iso(200))

        asyncio.run(_make_manager(tmp_db, "acc_verify_fail", status="prepared"))
        storage.prepared_account_upsert("acc_verify_fail", auth_source="session", db_path=tmp_db)
        storage.prepared_account_mark_verify("acc_verify_fail", False, "identity mismatch", db_path=tmp_db)
        v_fail = storage.prepared_account_get("acc_verify_fail", db_path=tmp_db)
        check("9. mark_verify FAIL sets last_verify_ok=0/error text", v_fail.get("last_verify_ok") == 0 and v_fail.get("last_verify_error") == "identity mismatch")

        # --- 10: mark_activated ---------------------------------------------------
        _set_fake_offset(300)
        asyncio.run(_make_manager(tmp_db, "acc_activated", status="prepared", tg_user_id=777))
        storage.prepared_account_upsert("acc_activated", auth_source="qr", db_path=tmp_db)
        storage.prepared_account_claim_activating("acc_activated", ttl_seconds=60, db_path=tmp_db)
        storage.prepared_account_mark_activated("acc_activated", db_path=tmp_db)
        activated = storage.prepared_account_get("acc_activated", db_path=tmp_db)
        check("10a. mark_activated sets activated_at", activated.get("activated_at") == _fake_now_iso(300))
        check("10b. mark_activated clears activating_at", activated.get("activating_at") == "")

        # --- 11/12/13: activation claim is atomic + stale-recoverable -----------
        _set_fake_offset(1000)
        asyncio.run(_make_manager(tmp_db, "acc_claim", status="prepared"))
        storage.prepared_account_upsert("acc_claim", auth_source="qr", db_path=tmp_db)
        claim1 = storage.prepared_account_claim_activating("acc_claim", ttl_seconds=60, db_path=tmp_db)
        check("11. first claim PASS", claim1 is True)
        claim2 = storage.prepared_account_claim_activating("acc_claim", ttl_seconds=60, db_path=tmp_db)
        check("12. second claim before TTL FAIL", claim2 is False)
        _set_fake_offset(1000 + 61)
        claim3 = storage.prepared_account_claim_activating("acc_claim", ttl_seconds=60, db_path=tmp_db)
        check("13. claim after stale TTL PASS", claim3 is True)

        # --- 14-18: repository INNER JOIN visibility ------------------------------
        _set_fake_offset(2000)
        # 14: side-row only, no managers row at all
        storage.prepared_account_upsert("acc_orphan_side", auth_source="qr", db_path=tmp_db)
        check("14. side-row without managers row -> NOT in list_prepared()", "acc_orphan_side" not in [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)])
        check("14b. side-row without managers row -> get_prepared() None", repository.get_prepared("acc_orphan_side", db_path=tmp_db) is None)

        # 15: managers row status='prepared', no side-row (transient import case)
        asyncio.run(_make_manager(tmp_db, "acc_transient", status="prepared"))
        check("15. managers prepared row without side-row -> NOT in list_prepared()", "acc_transient" not in [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)])
        check("15b. managers prepared row without side-row -> get_prepared() None", repository.get_prepared("acc_transient", db_path=tmp_db) is None)

        # 16: both present, status='prepared' -> visible
        asyncio.run(_make_manager(tmp_db, "acc_visible", status="prepared", tg_user_id=42, phone="+79990001122", proxy_enabled=1))
        storage.prepared_account_upsert("acc_visible", auth_source="qr", prepared_by_user_id=9, db_path=tmp_db)
        visible_keys = [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)]
        check("16. side-row + managers status='prepared' -> visible", "acc_visible" in visible_keys)
        view = repository.get_prepared("acc_visible", db_path=tmp_db)
        check("16b. get_prepared returns a populated view model", view is not None and view.get("status") == "prepared" and view.get("tg_user_id") == 42)
        check("16c. view model substate computed via model.derive_substate", view.get("substate") == model.PreparedSubstate.VERIFIED)

        # 17: side-row + managers status='active' -> NOT visible
        asyncio.run(_make_manager(tmp_db, "acc_became_active", status="active", is_enabled=1))
        storage.prepared_account_upsert("acc_became_active", auth_source="qr", db_path=tmp_db)
        check("17. side-row + managers status='active' -> NOT in list_prepared()", "acc_became_active" not in [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)])
        check("17b. side-row + managers status='active' -> get_prepared() None", repository.get_prepared("acc_became_active", db_path=tmp_db) is None)

        # 18: side-row + managers status='archived' -> NOT visible
        asyncio.run(_make_manager(tmp_db, "acc_archived", status="archived"))
        storage.prepared_account_upsert("acc_archived", auth_source="qr", db_path=tmp_db)
        check("18. side-row + managers status='archived' -> NOT in list_prepared()", "acc_archived" not in [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)])
        check("18b. side-row + managers status='archived' -> get_prepared() None", repository.get_prepared("acc_archived", db_path=tmp_db) is None)

        # --- 19: list is stable and deterministic ----------------------------------
        asyncio.run(_make_manager(tmp_db, "acc_zzz_last", status="prepared"))
        storage.prepared_account_upsert("acc_zzz_last", auth_source="tdata", db_path=tmp_db)
        asyncio.run(_make_manager(tmp_db, "acc_aaa_first", status="prepared"))
        storage.prepared_account_upsert("acc_aaa_first", auth_source="tdata", db_path=tmp_db)
        list_a = [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)]
        list_b = [v["manager_key"] for v in repository.list_prepared(db_path=tmp_db)]
        check("19a. list_prepared() is deterministic across repeat calls", list_a == list_b)
        check("19b. list_prepared() is sorted ascending by manager_key", list_a == sorted(list_a))
        check("19c. list_prepared() ordering places acc_aaa_first before acc_zzz_last", list_a.index("acc_aaa_first") < list_a.index("acc_zzz_last"))

        # --- 20/27: repository.py contains no SQL and no Telegram/proxy/network code
        repo_path = BASE_DIR / "features" / "prepared_accounts" / "repository.py"
        repo_src = repo_path.read_text(encoding="utf-8-sig")
        repo_cleaned = _code_without_string_literals(repo_src)
        sql_tokens = ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE TABLE", "sqlite3", "aiosqlite")
        sql_hits = [t for t in sql_tokens if t in repo_cleaned]
        check("20. repository.py contains no SQL tokens (code, not docstrings)", not sql_hits, detail=str(sql_hits))

        network_tokens = (
            "telethon", "TelegramClient", "connect(", "get_me", "is_user_authorized",
            "ProxySellerProvider", "allow_spend", "qr_login", "send_code_request", "opentele",
        )
        network_hits = [t for t in network_tokens if t in repo_cleaned]
        check("27. repository.py contains no Telegram/proxy/network code", not network_hits, detail=str(network_hits))

        # --- 21/22: managers schema unchanged, DDL additive-only -------------------
        con = storage._bsl_connect(tmp_db)
        try:
            actual_columns = frozenset(r[1] for r in con.execute("PRAGMA table_info(managers);").fetchall())
        finally:
            con.close()
        check(
            "21. managers table columns unchanged by this phase",
            actual_columns == _EXPECTED_MANAGERS_COLUMNS,
            detail=str(sorted(actual_columns ^ _EXPECTED_MANAGERS_COLUMNS)),
        )

        storage_src = (BASE_DIR / "storage.py").read_text(encoding="utf-8-sig")
        begin_marker = "# --- TPILOT PREPARED ACCOUNTS (feature:"
        end_marker = "# --- TPILOT PREPARED ACCOUNTS PHASE 2 END ---"
        begin_idx = storage_src.find(begin_marker)
        end_idx = storage_src.find(end_marker)
        check("22a. phase-2 block markers present in storage.py", begin_idx != -1 and end_idx != -1 and end_idx > begin_idx)
        new_block = storage_src[begin_idx:end_idx] if begin_idx != -1 and end_idx != -1 else ""
        destructive_hits = [m for m in DESTRUCTIVE_DDL_MARKERS if m in new_block]
        check("22b. no destructive DDL markers in the new phase-2 block", not destructive_hits, detail=str(destructive_hits))
        check(
            "22c. the only DELETE in the new block is narrowly scoped to manager_key=?",
            "DELETE FROM prepared_accounts WHERE manager_key=?" in new_block and new_block.count("DELETE FROM") == 1,
        )

        # --- 23: plain package import does not pull in storage/telethon/main/panel_bot
        probe_code = (
            "import sys\n"
            f"sys.path.insert(0, {str(BASE_DIR)!r})\n"
            "import features.prepared_accounts\n"
            "for name in ('storage', 'telethon', 'main', 'panel_bot'):\n"
            "    print(name + '=' + str(name in sys.modules))\n"
        )
        proc = subprocess.run([sys.executable, "-c", probe_code], capture_output=True, text=True, timeout=30)
        probe_result = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line)
        check(
            "23. fresh `import features.prepared_accounts` pulls in none of storage/telethon/main/panel_bot",
            proc.returncode == 0 and all(v == "False" for v in probe_result.values()),
            detail=f"returncode={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        storage_imported_by_base_package = probe_result.get("storage", "unknown")

        # --- 24: lazy get_repository works -----------------------------------------
        import features.prepared_accounts as prepared_accounts_pkg
        repo_module = prepared_accounts_pkg.get_repository()
        check("24. get_repository() returns the repository module with the expected API", repo_module is repository and callable(repo_module.list_prepared))

        # --- 25: substate classification is delegated to model, never duplicated ---
        check(
            "25a. repository.py calls model.derive_substate",
            "model.derive_substate" in repo_cleaned,
        )
        check(
            "25b. repository.py does not reference PreparedSubstate values directly (no duplicated branching)",
            "PreparedSubstate.DRAFT" not in repo_cleaned and "PreparedSubstate.VERIFIED" not in repo_cleaned and "PreparedSubstate.STORED" not in repo_cleaned,
        )

        # --- 26: auth_source is never used to pick the Telegram API profile --------
        model_path = BASE_DIR / "features" / "prepared_accounts" / "model.py"
        model_tree = ast.parse(model_path.read_text(encoding="utf-8-sig"), filename=str(model_path))
        client_source_fn = next(
            (n for n in ast.walk(model_tree) if isinstance(n, ast.FunctionDef) and n.name == "client_source_for"),
            None,
        )
        check("26a. model.client_source_for exists", client_source_fn is not None)
        if client_source_fn is not None:
            # Docstring stripped (it explains the contract using the word
            # "auth_source" in prose -- see _src_without_docstring's own
            # docstring); the real function BODY is what must actually read
            # auth_profile and never auth_source.
            fn_body_src = _src_without_docstring(client_source_fn)
            check(
                "26b. client_source_for reads managers.auth_profile, never prepared_accounts.auth_source",
                "auth_profile" in fn_body_src and "auth_source" not in fn_body_src,
                detail=fn_body_src,
            )

        print()
        print(f"STORAGE_IMPORTED_BY_BASE_PACKAGE={storage_imported_by_base_package}")
        if FAILURES:
            print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
            return 1
        print("SELFTEST OK: all checks passed.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
