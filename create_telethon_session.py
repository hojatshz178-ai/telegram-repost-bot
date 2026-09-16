from __future__ import annotations

import asyncio
import os

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    api_id = int(os.getenv("TELEGRAM_API_ID") or input("Telegram API ID: ").strip())
    api_hash = os.getenv("TELEGRAM_API_HASH") or input("Telegram API HASH: ").strip()
    print("Telegram will send a login code to your account. 2FA password may be requested.")
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start()
    session = client.session.save()
    print("\nTELETHON_SESSION=\n" + session)
    print("\nCopy the value into Railway Variables. Do NOT commit it to GitHub.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
