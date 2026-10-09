"""
Telethon Userbot Interactive Login & Verification Script.

Run this script to authenticate your Telegram user account and verify
access to the signal group:
    python scripts/login_telethon.py
"""

import asyncio
import os
import sys

from telethon import TelegramClient

from app.config.settings import settings


async def main() -> None:
    api_id = settings.telegram.api_id
    api_hash = settings.telegram.api_hash
    target_chat_id = settings.telegram.signal_chat_id

    print("=" * 60)
    print("🤖 Telethon Userbot Authentication & Verification")
    print("=" * 60)

    if not api_id or not api_hash:
        print("❌ Error: TELEGRAM_API_ID or TELEGRAM_API_HASH is missing in .env")
        sys.exit(1)

    print(f"API ID: {api_id}")
    print(f"API Hash: {api_hash[:6]}...{api_hash[-4:]}")
    print(f"Target Signal Chat ID: {target_chat_id}")
    print("-" * 60)

    session_name = "userbot"
    client = TelegramClient(session_name, api_id, api_hash)

    print("\nConnecting to Telegram servers...")
    print("💡 Lưu ý: Hãy nhập số điện thoại theo định dạng quốc tế kèm mã vùng (ví dụ: +84923286123, thay số 0 đầu bằng +84).")
    await client.start()

    me = await client.get_me()
    print("\n✅ Successfully authenticated as:")
    print(f"   Name:     {me.first_name} {me.last_name or ''}".strip())
    print(f"   Username: @{me.username}" if me.username else "   Username: (None)")
    print(f"   User ID:  {me.id}")
    print(f"   Phone:    +{me.phone}")
    print(f"   Session:  {session_name}.session saved.")

    print("\n🔍 Checking dialogs / groups to verify signal channel...")
    found_target = False
    print("-" * 60)
    print(f"{'Chat ID':<18} | {'Type':<12} | {'Title'}")
    print("-" * 60)

    async for dialog in client.iter_dialogs(limit=30):
        dialog_type = "Channel" if dialog.is_channel else ("Group" if dialog.is_group else "User")
        is_match = (dialog.id == target_chat_id)
        match_marker = " 🎯 [TARGET MATCH]" if is_match else ""
        if is_match:
            found_target = True
        print(f"{dialog.id:<18} | {dialog_type:<12} | {dialog.title}{match_marker}")

    print("-" * 60)
    if found_target:
        print(f"🎉 Target signal chat {target_chat_id} was FOUND and is accessible!")
    else:
        print(f"⚠️ Target signal chat {target_chat_id} was NOT found in the first 30 dialogs.")
        print("   Please make sure this account has joined the group/channel.")

    await client.disconnect()
    print("\nDone! You can now run the trading bot.")


if __name__ == "__main__":
    asyncio.run(main())
