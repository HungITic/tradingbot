"""Resolve re-entry by explicit reply ancestry or a recorded same-chat latest signal."""
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
    async def related_action_path(self, message, origin=None):
        if origin:
            return origin['message_path']
        parsed = self.service.parser.parse(message.raw_text or '')
        if parsed.message_type != MessageType.POSITION_MANAGEMENT or not parsed.management_actions or not message.is_reply:
            return None
        path, current = [message.id], message
        async with asyncio.timeout(30):
            for _ in range(MAX_REPLY_HOPS):
                parent_id = reply_id(current)
                parent = await current.get_reply_message()
                if not parent:
                    if parent_id and parent_id not in path:
                        path.append(parent_id)  # An exact stored source can still prove this direct edge.
                    return path
                if parent.chat_id != message.chat_id or (parent_id and parent.id != parent_id) or parent.id in path:
                    raise ReplyChainError('Chuỗi reply của action không hợp lệ; chưa thay đổi lệnh')
                path.append(parent.id)
                if self.validate_root_signal(parent.raw_text or '', parent.id) or not parent.is_reply:
                    return path
                current = parent
        raise ReplyChainError('Chuỗi reply của action vượt giới hạn 100 bước')

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
            from app.signal.recovery import recoverable_source
            if not recoverable_source(order, self.service.risk_config):
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
                or any(a <= b for a, b in zip(path, path[1:]))
                or (reply_id(message) is not None and path[1] != reply_id(message))
                or origin.get('root_message_id') != path[-1]
                or origin.get('command_text') != message.raw_text):
            return None
        if origin.get('method') == 'latest_signal':
            if (getattr(message, 'is_reply', False) or reply_id(message) is not None
                    or origin.get('selected_message_id') != path[1]
                    or type(origin.get('selected_signal_id')) is not int):
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

    async def latest_reentry_signal(self, chat_id, message_id):
        """Select one source before the command, regardless of its position/order status."""
        async with database.get_session() as session:
            row = (await session.execute(select(Signal.id, Signal.telegram_message_id)
                .join(SignalSource, Signal.source_id == SignalSource.id)
                .where(SignalSource.chat_id == chat_id, Signal.message_type == 'NEW_ORDER',
                       Signal.telegram_message_id < message_id)
                .order_by(Signal.telegram_message_id.desc(), Signal.id.desc()).limit(1))).first()
        return tuple(row) if row is not None else None

    async def resolve_latest_reentry(self, message):
        if type(message.id) is not int or message.id <= 0 or type(message.chat_id) is not int:
            raise ReplyChainError('Thiếu định danh tin vòng mới; chưa chọn signal gần nhất')
        saved = await self.load_reentry_origin(message.chat_id, message.id)
        if saved and saved.get('method') == 'latest_signal':
            origin = await self.cached_reentry_origin(message, saved)
            if origin:
                return self.reentry_text(origin), True, None, origin
            raise ReplyChainError('Nguồn vòng mới đã thay đổi; chưa tự chuyển sang kèo khác')
        try:
            candidate = await self.latest_reentry_signal(message.chat_id, message.id)
        except Exception as exc:
            raise ReplyChainError('Không đọc được signal gần nhất từ database; chưa thực thi vòng mới') from exc
        if candidate is None:
            raise ReplyChainError('Không có signal mở lệnh trước tin vòng mới trong cùng nhóm; chưa thực thi')
        signal_id, selected_id = candidate
        if type(selected_id) is not int or not 0 < selected_id < message.id:
            raise ReplyChainError('Signal gần nhất không hợp lệ; chưa thực thi')
        try:
            selected = await self.client.get_messages(await message.get_input_chat(), ids=selected_id)
        except Exception as exc:
            raise ReplyChainError('Không đọc được signal gần nhất trên Telegram; chưa thực thi') from exc
        if not selected or selected.id != selected_id or selected.chat_id != message.chat_id:
            raise ReplyChainError('Signal gần nhất bị xóa hoặc sai nhóm; chưa chuyển sang kèo cũ hơn')
        root_text = selected.raw_text or ''
        if self.validate_root_signal(root_text, selected_id):
            root_id, path = selected_id, [message.id, selected_id]
        elif self.is_reentry_command(root_text):
            prior = await self.cached_reentry_origin(selected,
                await self.load_reentry_origin(message.chat_id, selected_id))
            if prior is None and (selected.is_reply or reply_id(selected) is not None):
                _, _, _, prior = await self._resolve_reentry(selected)
            if prior is None:
                raise ReplyChainError('Tin vào lại gần nhất thiếu nguồn đã lưu; chưa chuyển sang kèo khác')
            root_id, root_text = prior['root_message_id'], prior['root_text']
            path = [message.id, *prior['message_path']]
        else:
            raise ReplyChainError('Signal gần nhất thiếu dữ liệu vào lệnh; chưa chọn kèo cũ hơn')
        if len(path) > MAX_REPLY_HOPS + 1 or any(a <= b for a, b in zip(path, path[1:])):
            raise ReplyChainError('Nguồn vào lại vượt giới hạn hoặc bị lặp; chưa thực thi')
        command_symbol = self.service.parser.parse(message.raw_text or '').symbol
        if command_symbol and command_symbol != self.service.parser.parse(root_text).symbol:
            raise ReplyChainError('Mã trong tin vòng mới khác signal gần nhất; hãy reply đúng kèo')
        # This path records selection provenance, not a fabricated Telegram reply edge.
        origin = {'chat_id': message.chat_id, 'root_message_id': root_id, 'message_path': path,
                  'root_text': root_text, 'command_text': message.raw_text,
                  'method': 'latest_signal', 'selected_signal_id': signal_id,
                  'selected_message_id': selected_id, 'selected_text': selected.raw_text}
        return self.reentry_text(origin), True, None, origin

    @staticmethod
    def reentry_text(origin):
        return f"{origin['root_text']}\n\n[REENTRY COMMAND]: {origin['command_text']}"

    async def validate_latest_reentry_payload(self, payload):
        """Recheck the pinned replay selection; never choose a newer setup on confirmation."""
        origin = payload.get('reentry_origin') or {}
        if origin.get('method') != 'latest_signal':
            return
        chat_id, command_id = payload.get('chat_id'), payload.get('message_id')
        path = origin.get('message_path')
        if (not payload.get('is_reentry') or origin.get('chat_id') != chat_id
                or not isinstance(path, list) or not 2 <= len(path) <= MAX_REPLY_HOPS + 1
                or any(type(i) is not int or i <= 0 for i in path) or path[0] != command_id
                or any(a <= b for a, b in zip(path, path[1:]))
                or any(not isinstance(origin.get(key), str) for key in ('root_text', 'selected_text', 'command_text'))
                or origin.get('selected_message_id') != path[1] or origin.get('root_message_id') != path[-1]
                or payload.get('raw_text') != self.reentry_text(origin)):
            raise ReplyChainError('Nguồn vòng mới đã xem trước không hợp lệ; hãy nạp lại link')
        if not self.client:
            raise ReplyChainError('Telegram chưa sẵn sàng để kiểm tra lại signal đã chọn')
        ids = list(dict.fromkeys([command_id, origin['selected_message_id'], origin['root_message_id']]))
        try:
            async with asyncio.timeout(30):
                messages = await self.client.get_messages(chat_id, ids=ids)
        except Exception as exc:
            raise ReplyChainError('Không kiểm tra lại được signal đã chọn; hãy nạp lại link') from exc
        by_id = {m.id: m for m in messages if m and m.chat_id == chat_id}
        expected = [(command_id, origin['command_text']),
                    (origin['selected_message_id'], origin['selected_text']),
                    (origin['root_message_id'], origin['root_text'])]
        if (any(mid not in by_id or by_id[mid].raw_text != text for mid, text in expected)
                or by_id[command_id].is_reply or reply_id(by_id[command_id]) is not None
                or not self.validate_root_signal(origin['root_text'], origin['root_message_id'])):
            raise ReplyChainError('Tin vòng mới hoặc signal đã chọn bị sửa/xóa; chưa thực thi, hãy nạp lại link')

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
        if direct_parent is None and not getattr(message, 'is_reply', False):
            return await self.resolve_latest_reentry(message)

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
            if (not getattr(parent, 'is_reply', False) and reply_id(parent) is None
                    and not self.is_reentry_command(root_text)):
                raise ReplyChainError('Không tìm thấy signal mở lệnh trong chuỗi reply; chưa thực thi')
            current = parent
        else:
            raise ReplyChainError('Chuỗi reply vượt giới hạn 100 bước; chưa thực thi')
        origin = {'chat_id': chat_id, 'root_message_id': root_id, 'message_path': path,
                  'root_text': root_text, 'command_text': command}
        return f'{root_text}\n\n[REENTRY COMMAND]: {command}', True, direct_parent, origin
