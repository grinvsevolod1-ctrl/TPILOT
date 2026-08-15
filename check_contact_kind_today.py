import sqlite3
import glob
import os
from datetime import datetime
from zoneinfo import ZoneInfo

today = datetime.now(ZoneInfo("Europe/Kyiv")).date().isoformat()

print("DATE:", today)
print()

for p in glob.glob(r"C:\ALM_TPilot\runtime\managers\*\*.db"):
    key = os.path.splitext(os.path.basename(p))[0]
    con = sqlite3.connect(p)
    try:
        rows = con.execute("""
            SELECT
                COALESCE(contact_kind, '') AS kind,
                COALESCE(lead_countable, 0) AS countable,
                COUNT(*) AS cnt
            FROM daily_leads
            WHERE lead_date=?
            GROUP BY COALESCE(contact_kind, ''), COALESCE(lead_countable, 0)
            ORDER BY countable DESC, kind ASC
        """, (today,)).fetchall()

        print("MANAGER:", key)
        if not rows:
            print("  no leads today")
        else:
            for kind, countable, cnt in rows:
                print(" ", kind or "_empty_", "lead_countable=" + str(countable), "count=" + str(cnt))
        print()
    finally:
        con.close()
