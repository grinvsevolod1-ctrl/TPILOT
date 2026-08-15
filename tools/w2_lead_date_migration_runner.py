# -*- coding: utf-8 -*-
"""tools/w2_lead_date_migration_runner.py -- W2 REVISION (Blocker 5): the
ONLY controlled, explicit way to run the manager_lead_cards lead_date
identity migration (D-06/B1/I-01/I-02). NOT reachable from ManagerBot's
ordinary startup (manager_bot.py's init_schema() only performs a read-only
compatibility check -- storage.w2_assert_lead_cards_schema_compatible --
and fails closed on a legacy schema instead of silently rebuilding it).

This tool:
  1. Takes an explicit db_path argument (never guesses, never defaults to
     the production path silently).
  2. Reports the CURRENT schema status (not_created / current /
     legacy_narrow_key / unknown_columns) before doing anything.
  3. If already current or not yet created, exits 0 with no write.
  4. If legacy_narrow_key: makes an explicit file-level BACKUP COPY of the
     database next to it (never overwrites, timestamped) BEFORE touching
     anything, then runs the real migration
     (storage.w2_manager_lead_cards_add_date_identity), then INDEPENDENTLY
     re-verifies the post-migration schema status and row count against
     the pre-migration count captured in step 2.
  5. Any failure at any step -- backup, migration, or post-verification --
     is a HARD failure (non-zero exit, full traceback), never swallowed.
  6. --dry-run reports what would happen without writing anything.

Usage:
    python tools\\w2_lead_date_migration_runner.py <db_path> [--dry-run]

This project's absolute safety rules apply: this tool is NEVER invoked
against the real production database from this session. It is exercised
here only against synthetic temp SQLite files (see
tools/w2_migration_safety_selftest.py).
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402


def _timestamp() -> str:
    return datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).strftime("%Y%m%d_%H%M%S")


def run(db_path: str, dry_run: bool) -> int:
    print(f"[w2-migration-runner] target db: {db_path}")
    status = storage.w2_manager_lead_cards_schema_status(db_path=db_path)
    print(f"[w2-migration-runner] pre-check status: {status}")

    if status["status"] == "not_created":
        print("[w2-migration-runner] table does not exist yet -- nothing to migrate; "
              "the next process startup will create it directly in the CURRENT (wide-key) form.")
        return 0
    if status["status"] == "current":
        print("[w2-migration-runner] already on the CURRENT (wide-key) schema -- nothing to do.")
        return 0
    if status["status"] == "unknown_columns":
        raise RuntimeError(
            f"unrecognized columns {status.get('unknown_columns')} on manager_lead_cards -- "
            f"refusing to guess a migration path. No changes made."
        )

    assert status["status"] == "legacy_narrow_key"
    pre_row_count = status["row_count"]
    print(f"[w2-migration-runner] legacy narrow-key schema detected, row_count={pre_row_count}")

    if dry_run:
        print("[w2-migration-runner] --dry-run: would back up the database file, then run the "
              "rebuild migration, then verify. No changes made.")
        return 0

    backup_path = f"{db_path}.pre_w2_lead_date_migration_{_timestamp()}.bak"
    print(f"[w2-migration-runner] creating explicit backup copy: {backup_path}")
    shutil.copy2(db_path, backup_path)
    con = sqlite3.connect(backup_path)
    try:
        backup_row_count = con.execute("SELECT COUNT(*) FROM manager_lead_cards").fetchone()[0]
    finally:
        con.close()
    if backup_row_count != pre_row_count:
        raise RuntimeError(
            f"backup integrity check failed: backup has {backup_row_count} rows, "
            f"source had {pre_row_count} -- refusing to proceed."
        )
    print(f"[w2-migration-runner] backup verified ({backup_row_count} rows). Running migration...")

    result = storage.w2_manager_lead_cards_add_date_identity(db_path=db_path)
    print(f"[w2-migration-runner] migration result: {result}")
    if result.get("status") != "migrated":
        raise RuntimeError(f"migration did not report status='migrated' (got {result!r}) -- "
                            f"the pre-migration backup at {backup_path} is untouched.")

    post_status = storage.w2_manager_lead_cards_schema_status(db_path=db_path)
    print(f"[w2-migration-runner] post-check status: {post_status}")
    if post_status["status"] != "current":
        raise RuntimeError(f"post-migration verification FAILED: schema status is "
                            f"{post_status['status']!r}, expected 'current'. Restore from "
                            f"{backup_path} and investigate before retrying.")

    con = sqlite3.connect(db_path)
    try:
        post_row_count = con.execute("SELECT COUNT(*) FROM manager_lead_cards").fetchone()[0]
    finally:
        con.close()
    if post_row_count != pre_row_count:
        raise RuntimeError(f"post-migration row count mismatch: before={pre_row_count} "
                            f"after={post_row_count}. Restore from {backup_path} immediately.")

    print(f"[w2-migration-runner] SUCCESS: {post_row_count} rows preserved, schema is now 'current'. "
          f"Backup retained at {backup_path} (not deleted automatically).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db_path", help="Explicit path to the SQLite database to check/migrate.")
    ap.add_argument("--dry-run", action="store_true", help="Report status only, write nothing.")
    args = ap.parse_args()
    try:
        return run(args.db_path, args.dry_run)
    except Exception as exc:
        print(f"[w2-migration-runner] HARD FAILURE: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main())
