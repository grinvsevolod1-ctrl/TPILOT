import partner_stat_bot as p

uid = 8254783932

buyer = p._buyer(uid)
print("BUYER:")
print(buyer)
print()
print("MODE:")
print(p._psf3_mode(uid) if hasattr(p, "_psf3_mode") else "NO _psf3_mode")
print()
print("STATS:")
print(p._format_stats_for_buyer(uid, "today"))
