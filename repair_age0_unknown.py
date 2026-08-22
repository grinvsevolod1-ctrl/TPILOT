"""
repair_age0_unknown.py  — TPILOT AGE0 FIX M2.6I

One-time repair: normalize age=0 (technical placeholder for "unknown age") to NULL
in the per-manager daily_leads table for manager_key='dariass'.

age=0 was never a real age. Restored historical rows (darias -> dariass reconnect)
often carry age=0 with no evidence, which downstream bucket logic used to count as
under18 ("-18"). Code-level bucket logic has been fixed separately (see M2.6I markers
in main.py, stats_engine.py, partner_stat_bot.py) to treat age=0 the same as missing
age going forward. This script fixes the DATA already stored in daily_leads so old
rows are consistent with the fixed logic without needing a re-parse.

WARNING: back up the DB before running with --apply:
    cp runtime/managers/dariass/dariass.db \\
       runtime/managers/dariass/dariass.db.bak_age0_$(date +%Y%m%d_%H%M%S)

Usage (from the deployment root, e.g. /opt/tpilot):
    ./venv/bin/python repair_age0_unknown.py          # dry-run (no writes)
    ./venv/bin/python repair_age0_unknown.py --apply  # actually update

The DB is always the one inside THIS tree (see DB_PATH below), so a dry-run from a
checkout can never read -- and --apply can never write -- another deployment's data.
"""

import os
import sys
import sqlite3

MANAGER_KEY = "dariass"

import tpilot_paths

# UBUNTU MIGRATION STAGE 2: this used to prefer a hardcoded C:\ALM_TPilot path and
# only fall back to the local tree. That ordering was a hazard even on Windows --
# running the script from a checkout would silently repair the SERVER database
# instead of the one next to it. The tree containing this file is now the single
# source of truth, so --apply can only ever touch the DB of the tree it lives in.
DB_PATH = str(tpilot_paths.manager_runtime_dir(MANAGER_KEY) / f"{MANAGER_KEY}.db")

DRY_RUN = "--apply" not in sys.argv

# Rows in scope: age=0 placeholder, no age evidence text, not confirmed 18+,
# and not a row an admin/manager already manually corrected (manual_status_override=1).
SCOPE_WHERE_SQL = """
    manager_key=?
    AND age=0
    AND COALESCE(age_evidence_text,'')=''
    AND COALESCE(age_confirmed_18_plus,0)=0
    AND COALESCE(manual_status_override,0)=0
"""

COUNT_SCOPE_SQL = f"SELECT COUNT(*) FROM daily_leads WHERE {SCOPE_WHERE_SQL}"
UPDATE_SQL = f"UPDATE daily_leads SET age=NULL WHERE {SCOPE_WHERE_SQL}"


def connect():
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] DB not found: {DB_PATH}")
        sys.exit(1)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def show_counts(con, label):
    print("=" * 60)
    print(f"{label}")
    print(f"DB: {DB_PATH}")
    print(f"manager_key: {MANAGER_KEY}")
    print("=" * 60)

    total = con.execute(
        "SELECT COUNT(*) FROM daily_leads WHERE manager_key=?", (MANAGER_KEY,)
    ).fetchone()[0]
    age0 = con.execute(
        "SELECT COUNT(*) FROM daily_leads WHERE manager_key=? AND age=0", (MANAGER_KEY,)
    ).fetchone()[0]
    in_scope = con.execute(COUNT_SCOPE_SQL, (MANAGER_KEY,)).fetchone()[0]
    skipped_manual = con.execute(
        "SELECT COUNT(*) FROM daily_leads WHERE manager_key=? AND age=0 "
        "AND COALESCE(age_evidence_text,'')='' AND COALESCE(age_confirmed_18_plus,0)=0 "
        "AND COALESCE(manual_status_override,0)=1",
        (MANAGER_KEY,),
    ).fetchone()[0]
    age_null = con.execute(
        "SELECT COUNT(*) FROM daily_leads WHERE manager_key=? AND age IS NULL", (MANAGER_KEY,)
    ).fetchone()[0]

    print(f"total daily_leads rows for {MANAGER_KEY}: {total}")
    print(f"rows with age=0:                          {age0}")
    print(f"  -> in scope for repair (age=NULL):       {in_scope}")
    print(f"  -> skipped (manual_status_override=1):   {skipped_manual}")
    print(f"rows with age IS NULL (already unknown):   {age_null}")
    print()


def do_repair(con):
    cur = con.execute(UPDATE_SQL, (MANAGER_KEY,))
    con.commit()
    print(f"UPDATED {cur.rowcount} rows: age 0 -> NULL")
    print("COMMITTED OK")


# ── Main ─────────────────────────────────────────────────────────────────────
if DRY_RUN:
    print("!!! Back up the DB before running with --apply:")
    print(f"    Copy-Item \"{DB_PATH}\" \"{DB_PATH}.bak_age0_<timestamp>\"")
    print()

con = connect()
try:
    show_counts(con, "BEFORE")
    if DRY_RUN:
        print("DRY-RUN mode — no changes written.")
        print("Run with --apply to execute the repair (back up the DB first).")
    else:
        print("APPLYING REPAIR ...")
        do_repair(con)
        print()
        show_counts(con, "AFTER")
finally:
    con.close()
