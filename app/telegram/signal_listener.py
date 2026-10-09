"""
Telegram Signal Listener (Telethon Userbot).

Listens for incoming trading signal messages from configured signal channels/groups
and routes them through the TradingService pipeline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from telethon import TelegramClient, events

from app.config.settings import settings
from app.trading.service import TradingService, trading_service

logger = structlog.get_logger(__name__)


class TelegramSignalListener:
    """Telethon userbot that listens to signal channels."""

    def __init__(self, service: TradingService | None = None) -> None:
        self.service = service or trading_service
        self.client: TelegramClient | None = None
        self._running = False

    async def initialize(self) -> None:
        """Create the Telethon client instance."""
        if not settings.telegram.api_id or not settings.telegram.api_hash:
            logger.warning("telethon_credentials_missing", msg="TELEGRAM_API_ID or TELEGRAM_API_HASH not set.")
            return

        session_name = "userbot"
        self.client = TelegramClient(
            session_name,
            settings.telegram.api_id,
            settings.telegram.api_hash,
            connection_retries=None,
        )

    async def start(self) -> None:
        """Connect client, verify authorization, and register message handlers."""
        if not self.client:
            await self.initialize()

        if not self.client:
            logger.error("signal_listener_init_failed")
            return

        logger.info("connecting_signal_listener")
        await self.client.connect()

        if not await self.client.is_user_authorized():
            logger.error(
                "signal_listener_not_authorized",
                msg="Userbot session not authorized! Please run 'python scripts/login_telethon.py' to login.",
            )
            return

        me = await self.client.get_me()
        logger.info("signal_listener_authorized", user_id=me.id, username=me.username)

        # Set up chat target filter
        target_chat = settings.telegram.signal_chat_id
        chats_filter = [target_chat] if target_chat else None

        @self.client.on(events.NewMessage(chats=chats_filter))
        async def on_new_signal_message(event: events.NewMessage.Event) -> None:
            raw_text = event.raw_text
            chat_id = event.chat_id
            msg_id = event.id
            is_reentry = False

            try:
                if event.is_reply:
                    from app.signal.models import ManagementType
                    actions = self.service.parser.rule_engine.match_actions(raw_text)
                    if any(a.action_type == ManagementType.REENTRY for a in actions):
                        reply_msg = await event.get_reply_message()
                        if reply_msg and reply_msg.raw_text:
                            logger.info("reentry_signal_detected", reply_to_id=reply_msg.id)
                            raw_text = f"{reply_msg.raw_text}\n\n[REENTRY COMMAND]: {raw_text}"
                            is_reentry = True

                logger.info(
                    "signal_message_received",
                    chat_id=chat_id,
                    message_id=msg_id,
                    text_preview=raw_text[:60].replace("\n", " "),
                    is_reentry=is_reentry,
                )

                await self.service.process_raw_message(
                    raw_text, 
                    is_reentry=is_reentry, 
                    message_id=msg_id, 
                    chat_id=chat_id
                )
            except Exception as e:
                logger.exception("signal_processing_error", error=str(e), message_id=msg_id)

        self._running = True
        logger.info(
            "signal_listener_listening",
            target_chat=target_chat,
            status="ACTIVE",
        )

    async def stop(self) -> None:
        """Gracefully disconnect the Telethon client."""
        if not self._running or not self.client:
            return
        self._running = False
        logger.info("signal_listener_stopping")
        try:
            await self.client.disconnect()
        except Exception as e:
            logger.warning("signal_listener_disconnect_error", error=str(e))
        logger.info("signal_listener_stopped")


# Singleton instance
signal_listener = TelegramSignalListener()
