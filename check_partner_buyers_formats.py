import sqlite3

db = r"C:\ALM_TPilot\db\data_tpilot.db"
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

print("=== TABLES ===")
tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
for t in tables:
    if "buyer" in t.lower() or "partner" in t.lower() or "source" in t.lower():
        print(t)

for t in ["partner_buyers", "buyers", "buyer_access", "sources"]:
    print()
    print("=== " + t + " ===")
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(" + t + ")").fetchall()]
        print("COLUMNS:", cols)

        if not cols:
            continue

        rows = con.execute("SELECT * FROM " + t + " LIMIT 50").fetchall()
        if not rows:
            print("no rows")
            continue

        for row in rows:
            print(dict(row))
    except Exception as e:
        print("ERR:", repr(e))

con.close()
