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
                COALESCE(quality_bucket, '') AS bucket,
                COALESCE(quality_reason, '') AS reason,
                COUNT(*) AS cnt
            FROM daily_leads
            WHERE lead_date=?
              AND COALESCE(lead_countable, 1)=1
            GROUP BY COALESCE(quality_bucket, ''), COALESCE(quality_reason, '')
            ORDER BY bucket ASC, cnt DESC
        """, (today,)).fetchall()

        print("MANAGER:", key)
        if not rows:
            print("  no countable leads today")
        else:
            for bucket, reason, cnt in rows:
                print(" ", bucket or "_empty_", "|", reason or "_", "|", cnt)
        print()
    finally:
        con.close()
