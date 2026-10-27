"""Resolve re-entry commands only through proven same-chat Telegram reply edges."""
from copy import deepcopy
import asyncio

import structlog
from sqlalchemy import select

from app.config import database
from app.models.signal import ParsedSignal, Signal, SignalSource
from app.signal.models import ManagementType, MessageType
from app.signal.validator import SignalValidator

logger = structlog.get_logger(__name__)
MAX_REPLY_HOPS = 100


class ReplyChainError(ValueError):
    pass


def reply_id(message):
    value = getattr(message, 'reply_to_msg_id', None)
    if not isinstance(value, int):
        value = getattr(getattr(message, 'reply_to', None), 'reply_to_msg_id', None)
    return value if isinstance(value, int) and value > 0 else None


class ReplyChainMixin:
    def is_reentry_command(self, text):
        return any(a.action_type == ManagementType.REENTRY
                   for a in self.service.parser.rule_engine.match_actions(text))

    def validate_root_signal(self, text, message_id):
        parsed = self.service.parser.parse(text)
        if parsed.message_type != MessageType.NEW_ORDER or parsed.new_order is None:
            return False
        order = parsed.new_order
        if ((self.service.risk_config.require_sl and order.stop_loss is None)
                or (self.service.risk_config.require_tp and order.take_profit is None and not order.raw_tp_text)):
            return False  # Status updates can mention BUY/entry without being a complete signal.
        valid, reason = SignalValidator(self.service.risk_config.require_sl,
                                         self.service.risk_config.require_tp).validate_signal(parsed)
        if not valid:
            raise ReplyChainError(f'Tin #{message_id} chứa tín hiệu mở lệnh không hợp lệ: {reason}')
        return True

    async def load_reentry_origin(self, chat_id, message_id):
        """Read persisted pipeline audit; never claim or execute ancestor messages."""
        try:
            async with database.get_session() as session:
                metadata = await session.scalar(select(ParsedSignal.extra_metadata)
                    .join(Signal, ParsedSignal.signal_id == Signal.id)
                    .join(SignalSource, Signal.source_id == SignalSource.id)
                    .where(SignalSource.chat_id == chat_id, Signal.telegram_message_id == message_id)
                    .order_by(Signal.id.desc()).limit(1))
            origin = metadata.get('reentry_origin') if isinstance(metadata, dict) else None
            return origin if isinstance(origin, dict) else None
        except Exception as exc:
            logger.error('reentry_origin_lookup_failed', error=str(exc))
            return None  # Telegram traversal can still prove the chain; execution requires DB later.

    async def cached_reentry_origin(self, message, origin):
        if not isinstance(origin, dict) or origin.get('chat_id') != message.chat_id:
            return None
        path = origin.get('message_path')
        if (not isinstance(path, list) or not 2 <= len(path) <= MAX_REPLY_HOPS + 1
                or any(type(i) is not int or i <= 0 for i in path)
                or len(set(path)) != len(path) or path[0] != message.id
                or (reply_id(message) is not None and path[1] != reply_id(message))
                or origin.get('root_message_id') != path[-1]
                or origin.get('command_text') != message.raw_text):
            return None
        # Re-read the source: a persisted link is not permission to trade a deleted
        # or edited source. Changed content falls back to normal traversal.
        try:
            root = await self.client.get_messages(await message.get_input_chat(), ids=path[-1])
        except Exception as exc:
            raise ReplyChainError(f'Không đọc được signal gốc #{path[-1]}; chưa thực thi vòng mới') from exc
        if not root or root.id != path[-1] or root.chat_id != message.chat_id:
            raise ReplyChainError(f'Không đọc được signal gốc #{path[-1]}; chưa thực thi vòng mới')
        if root.raw_text != origin.get('root_text'):
            return None
        if not self.validate_root_signal(root.raw_text, root.id):
            return None
        return deepcopy(origin)

    async def resolve_reentry(self, message):
        try:
            async with asyncio.timeout(30):
                return await self._resolve_reentry(message)
        except TimeoutError as exc:
            raise ReplyChainError('Quá thời gian đọc chuỗi reply; chưa thực thi, hãy thử lại khi Telegram ổn định') from exc

    async def _resolve_reentry(self, message):
        """Return effective text, re-entry flag, direct parent, and durable provenance."""
        command = message.raw_text or ''
        direct_parent = reply_id(message)
        if not self.is_reentry_command(command):
            if direct_parent is None and getattr(message, 'is_reply', False):
                parent = await message.get_reply_message()
                if parent:
                    if parent.chat_id != message.chat_id:
                        raise ReplyChainError('Tin reply không thuộc đúng chat; chưa thực thi')
                    direct_parent = parent.id
            return command, False, direct_parent, None
        # Keep negation/conditional rejection in the parser instead of manufacturing an order.
        parsed_command = self.service.parser.parse(command)
        if parsed_command.metadata.get('reason'):
            return command, False, direct_parent, None
        if not getattr(message, 'is_reply', False):
            raise ReplyChainError('Tin vòng mới cần reply vào kèo hoặc chuỗi reply của kèo; chưa thực thi')

        chat_id = message.chat_id
        current = message
        path = [message.id]
        visited = {message.id}
        for _ in range(MAX_REPLY_HOPS):
            if self.is_reentry_command(current.raw_text or ''):
                saved = await self.load_reentry_origin(chat_id, current.id)
                cached = await self.cached_reentry_origin(current, saved)
                if cached:
                    tail = cached['message_path'][1:]
                    if visited.intersection(tail):
                        raise ReplyChainError('Chuỗi reply bị lặp; chưa thực thi')
                    if len(path) + len(tail) > MAX_REPLY_HOPS + 1:
                        raise ReplyChainError('Chuỗi reply vượt giới hạn 100 bước; chưa thực thi')
                    path.extend(tail)
                    root_text, root_id = cached['root_text'], cached['root_message_id']
                    direct_parent = direct_parent or path[1]
                    break
            parent_id = reply_id(current)
            try:
                parent = await current.get_reply_message()
            except Exception as exc:
                raise ReplyChainError(f'Không đọc được tin reply của #{current.id}; chưa thực thi') from exc
            if not parent or not isinstance(parent.id, int):
                raise ReplyChainError(f'Chuỗi reply bị đứt tại #{current.id}; chưa thực thi')
            if parent.chat_id != chat_id or (parent_id is not None and parent.id != parent_id):
                raise ReplyChainError('Tin reply không thuộc đúng chat hoặc sai message ID; chưa thực thi')
            if parent.id in visited:
                raise ReplyChainError('Chuỗi reply bị lặp; chưa thực thi')
            if direct_parent is None:
                direct_parent = parent.id
            visited.add(parent.id)
            path.append(parent.id)
            root_text, root_id = parent.raw_text or '', parent.id
            if self.validate_root_signal(root_text, root_id):
                break
            if not getattr(parent, 'is_reply', False):
                raise ReplyChainError('Không tìm thấy signal mở lệnh trong chuỗi reply; chưa thực thi')
            current = parent
        else:
            raise ReplyChainError('Chuỗi reply vượt giới hạn 100 bước; chưa thực thi')
        origin = {'chat_id': chat_id, 'root_message_id': root_id, 'message_path': path,
                  'root_text': root_text, 'command_text': command}
        return f'{root_text}\n\n[REENTRY COMMAND]: {command}', True, direct_parent, origin
