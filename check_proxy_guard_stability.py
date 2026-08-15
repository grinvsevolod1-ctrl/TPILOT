import sqlite3

db = r"C:\ALM_TPilot\db\data_tpilot.db"
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row

cols = [r[1] for r in con.execute("PRAGMA table_info(managers)").fetchall()]
print("NEW COLUMNS:")
for c in [
    "auth_guard_state",
    "auth_guard_fail_count",
    "auth_guard_soft_fail_count",
    "auth_guard_last_soft_fail_at",
    "auth_guard_last_notify_at",
    "auth_guard_last_fail_reason",
    "auth_guard_last_recovery_at",
]:
    print(c + ":", c in cols)

print()
print("PROXY MANAGERS:")
rows = con.execute("""
    SELECT
        manager_key,
        proxy_mode,
        proxy_enabled,
        auth_guard_ok,
        auth_guard_state,
        auth_guard_fail_count,
        auth_guard_soft_fail_count,
        auth_guard_last_ok_at,
        auth_guard_checked_at,
        auth_guard_error
    FROM managers
    WHERE COALESCE(proxy_mode,'')='proxy'
       OR COALESCE(proxy_enabled,0)=1
    ORDER BY manager_key
""").fetchall()

for r in rows:
    print(dict(r))

con.close()
