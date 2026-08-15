"""
repair_darias_metadata.py  — TPILOT HISTSTATS M2.6H

One-time repair: copy metadata rows from old key 'darias' -> new key 'dariass'.
Runs against the PRODUCTION central DB on the server.

Usage (on server, from C:\ALM_TPilot):
    .\venv\Scripts\python.exe repair_darias_metadata.py          # dry-run (no writes)
    .\venv\Scripts\python.exe repair_darias_metadata.py --apply  # actually migrate

Local dry-run (adjust DB_PATH below):
    python3.12 repair_darias_metadata.py
"""

import os
import sys
import sqlite3

# ── Config ───────────────────────────────────────────────────────────────────
OLD_KEY = "darias"
NEW_KEY = "dariass"

# Server path — adjust if needed
SERVER_DB = r"C:\ALM_TPilot\db\data_tpilot.db"
LOCAL_DB = os.path.join(os.path.dirname(__file__), "db", "data_tpilot.db")
DB_PATH = SERVER_DB if os.path.exists(SERVER_DB) else LOCAL_DB

DRY_RUN = "--apply" not in sys.argv

# ── Helpers ──────────────────────────────────────────────────────────────────

def connect():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}")
        sys.exit(1)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def show_existing(con):
    print("=" * 60)
    print(f"DB: {DB_PATH}")
    print(f"OLD_KEY: {OLD_KEY}   NEW_KEY: {NEW_KEY}")
    print("=" * 60)

    # managers table status
    for key in (OLD_KEY, NEW_KEY):
        row = con.execute(
            "SELECT manager_key, status, is_enabled, display_name, tg_user_id FROM managers WHERE manager_key=?",
            (key,),
        ).fetchone()
        if row:
            print(f"managers[{key}]: status={row['status']} is_enabled={row['is_enabled']} "
                  f"display_name={row['display_name']} tg_user_id={row['tg_user_id']}")
        else:
            print(f"managers[{key}]: NOT FOUND")

    print()

    # manager_work_schedule_days
    old_sched = con.execute(
        "SELECT work_date FROM manager_work_schedule_days WHERE manager_key=? ORDER BY work_date",
        (OLD_KEY,),
    ).fetchall()
    new_sched = con.execute(
        "SELECT work_date FROM manager_work_schedule_days WHERE manager_key=? ORDER BY work_date",
        (NEW_KEY,),
    ).fetchall()
    old_dates = {r["work_date"] for r in old_sched}
    new_dates = {r["work_date"] for r in new_sched}
    to_copy_sched = old_dates - new_dates
    print(f"manager_work_schedule_days: old={len(old_dates)} new={len(new_dates)} to_copy={len(to_copy_sched)}")
    if to_copy_sched:
        print(f"  will copy: {sorted(to_copy_sched)}")

    # manager_work_days
    old_wd = con.execute(
        "SELECT work_date FROM manager_work_days WHERE manager_key=? ORDER BY work_date",
        (OLD_KEY,),
    ).fetchall()
    new_wd = con.execute(
        "SELECT work_date FROM manager_work_days WHERE manager_key=? ORDER BY work_date",
        (NEW_KEY,),
    ).fetchall()
    old_wd_dates = {r["work_date"] for r in old_wd}
    new_wd_dates = {r["work_date"] for r in new_wd}
    to_copy_wd = old_wd_dates - new_wd_dates
    print(f"manager_work_days: old={len(old_wd_dates)} new={len(new_wd_dates)} to_copy={len(to_copy_wd)}")
    if to_copy_wd:
        print(f"  will copy: {sorted(to_copy_wd)}")

    # manager_source_links
    old_sl = con.execute(
        "SELECT source_key FROM manager_source_links WHERE manager_key=?",
        (OLD_KEY,),
    ).fetchone()
    new_sl = con.execute(
        "SELECT source_key FROM manager_source_links WHERE manager_key=?",
        (NEW_KEY,),
    ).fetchone()
    print(f"manager_source_links: old={old_sl['source_key'] if old_sl else 'NONE'} "
          f"new={new_sl['source_key'] if new_sl else 'NONE'}")
    if old_sl and not new_sl:
        print(f"  will copy: source_key={old_sl['source_key']}")
    elif old_sl and new_sl:
        print("  new key already has source_link — INSERT OR IGNORE will skip")

    print()


def do_migrate(con):
    # manager_work_schedule_days
    cur = con.execute(
        """INSERT OR IGNORE INTO manager_work_schedule_days
           (manager_key,work_date,is_working,source,updated_by_user_id,updated_by_role,updated_at)
           SELECT ?,work_date,is_working,source,updated_by_user_id,updated_by_role,updated_at
           FROM manager_work_schedule_days WHERE manager_key=?""",
        (NEW_KEY, OLD_KEY),
    )
    print(f"manager_work_schedule_days: inserted {cur.rowcount} rows")

    # manager_work_days
    cur = con.execute(
        """INSERT OR IGNORE INTO manager_work_days
           (manager_key,work_date,is_working,first_manual_out_utc,last_manual_out_utc,updated_at)
           SELECT ?,work_date,is_working,first_manual_out_utc,last_manual_out_utc,updated_at
           FROM manager_work_days WHERE manager_key=?""",
        (NEW_KEY, OLD_KEY),
    )
    print(f"manager_work_days: inserted {cur.rowcount} rows")

    # manager_source_links
    cur = con.execute(
        """INSERT OR IGNORE INTO manager_source_links
           (manager_key,source_key,created_at,updated_at)
           SELECT ?,source_key,created_at,updated_at
           FROM manager_source_links WHERE manager_key=?""",
        (NEW_KEY, OLD_KEY),
    )
    print(f"manager_source_links: inserted {cur.rowcount} rows")

    con.commit()
    print("COMMITTED OK")


# ── Main ─────────────────────────────────────────────────────────────────────
con = connect()
try:
    show_existing(con)
    if DRY_RUN:
        print("DRY-RUN mode — no changes written.")
        print("Run with --apply to execute the migration.")
    else:
        print("APPLYING MIGRATION ...")
        do_migrate(con)
        print()
        print("Post-migration state:")
        show_existing(con)
finally:
    con.close()
