import sqlite3
import glob
import os

tpilot_db = r"C:\ALM_TPilot\db\data_tpilot.db"

con = sqlite3.connect(tpilot_db)
try:
    print("=== TPILOT DB ===")
    print("known_contacts:", con.execute("SELECT COUNT(*) FROM known_contacts").fetchone()[0])
    print("contact_identifiers:", con.execute("SELECT COUNT(*) FROM contact_identifiers").fetchone()[0])
    print("partner_lead_events columns:")
    print([r[1] for r in con.execute("PRAGMA table_info(partner_lead_events)").fetchall()])
finally:
    con.close()

print()
print("=== MANAGER DBS ===")

for p in glob.glob(r"C:\ALM_TPilot\runtime\managers\*\*.db"):
    key = os.path.splitext(os.path.basename(p))[0]
    con = sqlite3.connect(p)
    try:
        daily_cols = [r[1] for r in con.execute("PRAGMA table_info(daily_leads)").fetchall()]
        inbound_exists = bool(
            con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='inbound_events'"
            ).fetchone()
        )
        inbound_cols = [r[1] for r in con.execute("PRAGMA table_info(inbound_events)").fetchall()] if inbound_exists else []

        print()
        print("MANAGER:", key)
        print("lead_countable:", "lead_countable" in daily_cols)
        print("contact_kind:", "contact_kind" in daily_cols)
        print("known_contact_id:", "known_contact_id" in daily_cols)
        print("event_key:", "event_key" in daily_cols)
        print("dedupe_reason:", "dedupe_reason" in daily_cols)
        print("baseline_old:", "baseline_old" in daily_cols)
        print("inbound_events:", inbound_exists)
        print("inbound_events columns:", inbound_cols)
    finally:
        con.close()
