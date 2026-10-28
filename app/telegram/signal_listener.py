"""
Telegram Signal Listener (Telethon Userbot).

Listens for incoming trading signal messages from configured signal channels/groups
and routes them through the TradingService pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from html import escape
from typing import Any

import structlog
from telethon import TelegramClient, events

from app.config.settings import settings
from app.trading.service import TradingService, trading_service
from app.telegram.reply_chain import ReplyChainMixin, ReplyChainError

logger = structlog.get_logger(__name__)


class TelegramSignalListener(ReplyChainMixin):
    """Telethon userbot that listens to signal channels."""

    def __init__(self, service: TradingService | None = None) -> None:
        self.service = service or trading_service
        self.client: TelegramClient | None = None
        self._running = False
        self.last_message_at: datetime | None = None

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
            self.last_message_at = event.message.date
            raw_text = event.raw_text
            chat_id = event.chat_id
            msg_id = event.id
            is_reentry = False
            reply_to_msg_id = getattr(event, "reply_to_msg_id", None)
            if reply_to_msg_id is None and getattr(event, "reply_to", None):
                reply_to_msg_id = getattr(event.reply_to, "reply_to_msg_id", None)

            try:
                raw_text, is_reentry, reply_to_msg_id, origin = await self.resolve_reentry(event.message)
                related_path = await self.related_action_path(event.message, origin)

                chat = await event.get_chat()
                username = getattr(chat, "username", None)
                from app.telegram.utils import construct_telegram_message_link
                message_link = construct_telegram_message_link(chat_id, msg_id, username=username)

                logger.info(
                    "signal_message_received",
                    chat_id=chat_id,
                    message_id=msg_id,
                    reply_to_msg_id=reply_to_msg_id,
                    message_link=message_link,
                    text_preview=raw_text[:60].replace("\n", " "),
                    is_reentry=is_reentry,
                )

                await self.service.process_raw_message(
                    raw_text, 
                    is_reentry=is_reentry, 
                    message_id=msg_id, 
                    chat_id=chat_id,
                    message_link=message_link,
                    reply_to_msg_id=reply_to_msg_id,
                    signal_time=event.message.date,
                    **({'reentry_origin': origin} if origin else {}),
                    **({'related_message_path': related_path} if related_path else {}),
                )
            except ReplyChainError as exc:
                logger.error('reentry_chain_rejected', chat_id=chat_id, message_id=msg_id, error=str(exc))
                from app.telegram.utils import construct_telegram_message_link
                link = construct_telegram_message_link(chat_id, msg_id)
                await self.service.notify(self.service.format_notification(f"⛔ {escape(str(exc))}", link))
            except Exception as e:
                logger.exception("signal_processing_error", error=str(e), message_id=msg_id)

        @self.client.on(events.MessageEdited(chats=chats_filter))
        async def on_edited_signal(event):
            # An edit must never create a second order or silently alter an existing trade.
            self.last_message_at = event.message.edit_date or event.message.date
            from app.telegram.utils import construct_telegram_message_link
            chat = await event.get_chat()
            link = construct_telegram_message_link(event.chat_id, event.id, username=getattr(chat, "username", None))
            await self.service.notify(self.service.format_notification(
                "✏️ <b>Tín hiệu đã được sửa — cần xem lại</b>\n"
                + escape(event.raw_text[:600])
                + "\nMở link trong bot để xem trước. Lệnh đang chạy chưa được thay đổi.", link))

        self._running = True
        logger.info(
            "signal_listener_listening",
            target_chat=target_chat,
            status="ACTIVE",
        )

    async def fetch_message_by_link(
        self, link: str
    ) -> tuple:
        """
        Fetch message content and metadata from a Telegram link.
        Returns: (raw_text, message_id, chat_id, canonical_link, is_reentry, reply_to_msg_id, sent_at)
        plus an eighth provenance dict for re-entry commands.
        """
        from app.telegram.utils import construct_telegram_message_link, parse_telegram_message_link

        entity, msg_id = parse_telegram_message_link(link)
        if entity is None or msg_id is None:
            raise ValueError(f"Định dạng link tin nhắn Telegram không hợp lệ: {link}")

        if not self.client:
            await self.initialize()

        if not self.client:
            raise RuntimeError("Telethon client chưa được khởi tạo. Vui lòng kiểm tra TELEGRAM_API_ID / TELEGRAM_API_HASH.")

        if not self.client.is_connected():
            await self.client.connect()

        if not await self.client.is_user_authorized():
            raise RuntimeError("Telethon userbot chưa đăng nhập. Vui lòng kiểm tra file session.")

        try:
            target_entity = entity
            if isinstance(entity, int):
                try:
                    target_entity = await self.client.get_entity(entity)
                except Exception:
                    target_entity = entity
            msg = await self.client.get_messages(target_entity, ids=msg_id)
        except Exception as e:
            logger.error("telethon_fetch_message_error", entity=entity, message_id=msg_id, error=str(e))
            raise RuntimeError(f"Không thể lấy tin nhắn từ Telegram: {e}")

        if not msg:
            raise ValueError(f"Không tìm thấy tin nhắn với ID {msg_id} trong {entity}.")

        raw_text = msg.raw_text or ""
        if not raw_text.strip():
            raise ValueError(f"Tin nhắn #{msg_id} không có nội dung văn bản.")

        chat_id = msg.chat_id
        raw_text, is_reentry, reply_to_msg_id, origin = await self.resolve_reentry(msg)
        related_path = await self.related_action_path(msg, origin)

        chat = await msg.get_chat()
        username = getattr(chat, "username", None) if chat else (entity if isinstance(entity, str) else None)
        canonical_link = construct_telegram_message_link(chat_id, msg_id, username=username) or link

        if not isinstance(msg.date, datetime) or msg.date.tzinfo is None:
            raise ValueError("Không xác định được thời gian gốc của tín hiệu")
        result = (raw_text, msg_id, chat_id, canonical_link, is_reentry, reply_to_msg_id, msg.date)
        return (*result, origin) if origin else (*result, None, related_path) if related_path else result

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
