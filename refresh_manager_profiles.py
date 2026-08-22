import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from dotenv import dotenv_values
from telethon import TelegramClient

import tpilot_paths

# UBUNTU MIGRATION STAGE 2: was Path(r"C:\ALM_TPilot"), which made this script
# unusable anywhere but the old Windows server. Derived from the repo location
# instead, with a TPILOT_ROOT override for inspecting a relocated tree.
root = tpilot_paths.ROOT
env = dotenv_values(root / ".env.TPilot")

api_id = int(env.get("API_ID") or 0)
api_hash = (env.get("API_HASH") or "").strip()
db_path = root / "db" / "data_tpilot.db"

def now_iso():
    # utcnow refactor: naive-UTC seam, byte-identical to the old
    # datetime.utcnow() output and deprecation-free on Python 3.12+.
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()

async def refresh_one(key):
    session_path = root / "runtime" / "managers" / key / f"{key}.session"
    print(f"CHECK {key}: {session_path}")

    if not session_path.exists():
        print(f"SKIP {key}: session not found")
        return

    client = TelegramClient(str(session_path), api_id, api_hash)
    await client.connect()

    try:
        auth = await client.is_user_authorized()
        print(f"AUTHORIZED {key}: {auth}")

        if not auth:
            return

        me = await client.get_me()

        username = str(getattr(me, "username", "") or "")
        first_name = str(getattr(me, "first_name", "") or "")
        last_name = str(getattr(me, "last_name", "") or "")
        phone = str(getattr(me, "phone", "") or "")
        tg_user_id = int(getattr(me, "id", 0) or 0)

        display_name = first_name or username or key

        con = sqlite3.connect(str(db_path))
        try:
            con.execute("""
                UPDATE managers
                SET display_name=?,
                    phone=?,
                    tg_user_id=?,
                    telegram_username=?,
                    first_name=?,
                    last_name=?,
                    last_login_at=?,
                    updated_at=?,
                    last_error=''
                WHERE manager_key=?
            """, (
                display_name,
                phone,
                tg_user_id,
                username,
                first_name,
                last_name,
                now_iso(),
                now_iso(),
                key
            ))
            con.commit()
        finally:
            con.close()

        print(f"OK {key}: @{username if username else '_'} | {first_name} {last_name} | {phone}")

    finally:
        await client.disconnect()

async def main():
    for key in ["jora", "nik"]:
        await refresh_one(key)

asyncio.run(main())
