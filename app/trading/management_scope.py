"""Exact, durable scope for source-channel management messages missing a reply."""
import re

from sqlalchemy import select

from app.config import database
from app.models.signal import Signal, SignalSource, ParsedSignal
from app.models.trade import Order
from app.models.risk import RiskEvent
from app.signal.models import ManagementType


class ManagementScopeMixin:
    async def resolve_management_scope(self, signal, reply_id, chat_id, message_id, expected=None):
        if reply_id is not None or chat_id is None:
            return None
        # Explicit account-wide instructions and re-entry commands retain their own paths.
        if re.search(r'(?i)tất cả|toàn bộ|\ball\b', signal.raw_message):
            return None
        if not signal.management_actions or any(a.action_type == ManagementType.REENTRY for a in signal.management_actions):
            return None
        if type(message_id) is not int or message_id <= 0:
            raise ValueError('Thiếu message ID để xác định signal gần nhất; chưa thay đổi vị thế')
        stmt = (select(Signal.id, Signal.telegram_message_id, ParsedSignal.symbol)
            .join(SignalSource, Signal.source_id == SignalSource.id)
            .outerjoin(ParsedSignal, ParsedSignal.signal_id == Signal.id)
            .where(SignalSource.chat_id == chat_id, Signal.message_type == 'NEW_ORDER',
                   Signal.telegram_message_id < message_id))
        if expected is not None:
            if (not isinstance(expected, dict) or expected.get('chat_id') != chat_id
                    or type(expected.get('signal_id')) is not int or type(expected.get('message_id')) is not int):
                raise ValueError('Phạm vi kèo đã xem trước không hợp lệ; hãy xem trước lại')
            stmt = stmt.where(Signal.id == expected['signal_id'], Signal.telegram_message_id == expected['message_id'])
        async with database.get_session() as session:
            row = (await session.execute(stmt.order_by(Signal.telegram_message_id.desc(), Signal.id.desc()).limit(1))).first()
            if row is None:
                raise ValueError('Không tìm thấy signal mở lệnh trước tin này trong cùng chat; chưa thay đổi vị thế')
            signal_id, root_message_id, symbol = row
            if signal.symbol and (not symbol or signal.symbol.upper() != symbol.upper()):
                raise ValueError('Mã trong action khác signal mở lệnh gần nhất; hãy reply đúng kèo')
            tickets = list((await session.scalars(select(Order.mt5_ticket).where(
                Order.signal_id == signal_id, Order.mt5_ticket.isnot(None)))).all())
        if not tickets:
            raise ValueError('Signal mở lệnh gần nhất chưa có lệnh được ghi nhận; không chuyển sang kèo cũ hơn')
        return {'signal_id': signal_id, 'message_id': root_message_id, 'chat_id': chat_id,
                'tickets': tickets, 'method': 'latest_signal'}

    async def audit_management_scope(self, signal_id, scope):
        async with database.get_session() as session:
            session.add(RiskEvent(signal_id=signal_id, event_type='MANAGEMENT_SCOPE',
                                  rule_name='latest_signal', details=scope))
