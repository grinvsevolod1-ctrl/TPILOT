import sqlite3
import os

import tpilot_paths

for p in tpilot_paths.manager_db_paths():
    key = os.path.splitext(os.path.basename(p))[0]
    con = sqlite3.connect(p)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(daily_leads)").fetchall()]
        print()
        print("MANAGER:", key)
        for c in [
            "quality_status",
            "quality_bucket",
            "quality_reason",
            "quality_confidence",
            "quality_source",
            "quality_checked_at",
            "quality_version",
            "profile_raw_text",
            "manual_status_override",
            "trash",
            "trash_reason",
        ]:
            print(c + ":", c in cols)

        audit = bool(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='lead_status_audit'").fetchone())
        overrides = bool(con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='lead_status_overrides'").fetchone())
        print("lead_status_audit:", audit)
        print("lead_status_overrides:", overrides)
    finally:
        con.close()
