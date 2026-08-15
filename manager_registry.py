# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import sqlite3
import sys
from typing import Dict, List, Optional

from dotenv import dotenv_values

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def normalize_manager_key(raw: str) -> str:
    return re.sub(r"\s+", "", str(raw or "")).casefold().replace("ё", "е")


def validate_manager_key(raw: str):
    key = normalize_manager_key(raw)
    if not key:
        return False, "Ключ пустой.", ""
    if not re.fullmatch(r"[a-z0-9_-]+", key):
        return False, "Ключ должен содержать только латиницу, цифры, _ или -.", key
    return True, "", key


def mask_phone(phone: str) -> str:
    s = str(phone or "").strip()
    if not s:
        return "_"
    digits = re.sub(r"\D+", "", s)
    if len(digits) < 6:
        return s
    return "+" + digits[:2] + "*" * max(0, len(digits) - 5) + digits[-3:]


def build_manager_paths(base_dir: str, key: str) -> Dict[str, str]:
    k = normalize_manager_key(key)
    root = os.path.join(base_dir, "runtime", "managers", k)
    return {
        "root": root,
        "session_path": os.path.join(root, f"{k}.session"),
        "db_path": os.path.join(root, f"{k}.db"),
        "log_path": os.path.join(root, f"{k}.log"),
    }


def ensure_manager_dirs(base_dir: str, key: str) -> Dict[str, str]:
    paths = build_manager_paths(base_dir, key)
    os.makedirs(paths["root"], exist_ok=True)
    return paths


def _resolve_from_env_dir(env_path: str, raw_path: str) -> str:
    p = str(raw_path or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    env_dir = os.path.dirname(os.path.abspath(env_path))
    return os.path.abspath(os.path.join(env_dir, p))


def resolve_tpilot_db_path(env_path: str) -> str:
    vals = dotenv_values(env_path)
    raw_db = (vals.get("DB_PATH") or "db/data_tpilot.db").strip()
    return _resolve_from_env_dir(env_path, raw_db)


def _fetch_rows(db_path: str, where_sql: str = "", params: tuple = ()) -> List[dict]:
    if not db_path or not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        sql = "SELECT * FROM managers"
        if where_sql:
            sql += " WHERE " + where_sql
        sql += " ORDER BY id ASC"
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def list_manager_rows_from_db_sync(db_path: str, *, only_enabled: Optional[bool] = None, only_active: Optional[bool] = None, include_removed: bool = False) -> List[dict]:
    parts = []
    if not include_removed:
        parts.append("COALESCE(status,'') != 'archived'")
    if only_enabled is True:
        parts.append("COALESCE(is_enabled,0)=1")
    elif only_enabled is False:
        parts.append("COALESCE(is_enabled,0)=0")
    if only_active is True:
        parts.append("COALESCE(status,'')='active'")
    elif only_active is False:
        parts.append("COALESCE(status,'')!='active'")
    where_sql = " AND ".join(parts)
    rows = _fetch_rows(db_path, where_sql, ())
    if only_active is True:
        rows = [r for r in rows if int(r.get('manual_stopped') or 0) == 0]
    return rows


def get_manager_row_from_db_sync(db_path: str, key: str) -> Optional[dict]:
    rows = _fetch_rows(db_path, "manager_key=?", (normalize_manager_key(key),))
    return rows[0] if rows else None


# TPILOT RESTART ISOLATION 20260809: taxonomy of start_status.json reason_class
# values a launcher can safely treat as "this one manager failed, the rest are fine".
# Mirrors (does not duplicate) the reason_class values _manager_recovery_classify
# (main.py:29274) already recognizes -- both its non_recoverable bucket
# (main.py:29303: auth_session, session_unauthorized, telegram_identity_mismatch) and
# its recoverable bucket (main.py:29305: connection_error, proxy_timeout,
# process_exited) name reason_class values that are, by construction, specific to one
# manager's runtime -- not to the shared codebase. "unknown" is intentionally excluded:
# it is the classifier's own fallback for anything it doesn't recognize, so it must
# never be pre-approved here. See tools/startup_isolation_selftest.py for the AST
# parity check against main.py's classifier.
MANAGER_SCOPED_START_FAILURES = frozenset({
    "session_unauthorized",
    "telegram_identity_mismatch",
    "auth_session",
    "proxy_timeout",
    "connection_error",
    "process_exited",
})


def classify_start_failure(reason_class: str) -> str:
    """Classify a start_status.json reason_class for launcher isolation purposes.

    Returns 'manager_scoped' when the reason_class is a known value that only ever
    describes one manager's own runtime; 'unknown' otherwise (empty/unrecognized).
    This function does NOT decide the launcher's exit code by itself -- the caller
    (start_manager.ps1) additionally requires the start_status.json file to exist
    before treating ANY failure (known or unknown reason_class) as isolatable, because
    that file is only ever written from inside main() after the shared runtime has
    already loaded successfully (main.py:41296, before asyncio.run(main())). A missing
    status file always means 'global' regardless of what this function would return."""
    rc = str(reason_class or "").strip().lower()
    if rc and rc in MANAGER_SCOPED_START_FAILURES:
        return "manager_scoped"
    return "unknown"


def _cli_list_keys(env_path: str, only_active: bool | None, only_enabled: bool | None) -> int:
    db_path = resolve_tpilot_db_path(env_path)
    rows = list_manager_rows_from_db_sync(db_path, only_enabled=only_enabled, only_active=only_active)
    for row in rows:
        key = normalize_manager_key(row.get("manager_key") or "")
        if key:
            print(key)
    return 0


def main() -> int:
    argv = sys.argv[1:]
    env_path = ".env.TPilot"
    if "--env" in argv:
        i = argv.index("--env")
        if i + 1 < len(argv):
            env_path = argv[i + 1]
            del argv[i:i+2]
    if not os.path.isabs(env_path):
        env_path = os.path.join(BASE_DIR, env_path)
    if not argv:
        print("Usage: manager_registry.py [--env .env.TPilot] list-active-keys|list-all-keys|classify-start-failure <reason_class>", file=sys.stderr)
        return 1
    cmd = str(argv[0] or "").strip().lower()
    if cmd == "list-active-keys":
        return _cli_list_keys(env_path, True, True)
    if cmd == "list-all-keys":
        return _cli_list_keys(env_path, None, None)
    if cmd == "classify-start-failure":
        # TPILOT RESTART ISOLATION 20260809: no DB/env access needed -- pure
        # taxonomy lookup so start_manager.ps1 can call this without a DB round-trip.
        reason = argv[1] if len(argv) > 1 else ""
        print(classify_start_failure(reason))
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
