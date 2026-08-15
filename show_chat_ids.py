import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env.TPilot")

load_dotenv(ENV_PATH, override=True)

API_ID = int(os.getenv("API_ID") or "0")
API_HASH = (os.getenv("API_HASH") or "").strip()
SESSION_FILE = (os.getenv("SESSION_FILE") or "session_main").strip()

if not os.path.isabs(SESSION_FILE):
    SESSION_FILE = os.path.join(BASE_DIR, SESSION_FILE)

async def main():
    if API_ID <= 0 or not API_HASH:
        raise RuntimeError("API_ID/API_HASH пустые. роверь .env.TPilot")

    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    await client.start()

    print("GROUPS / CHANNELS:")
    async for d in client.iter_dialogs():
        if d.is_group or d.is_channel:
            print(f"{d.id} | {d.title}")

    await client.disconnect()

asyncio.run(main())
