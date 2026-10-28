"""Adverse-slippage limits expire on related management actions, durably across restarts."""
import re

from sqlalchemy import select, update, cast, func
from sqlalchemy.dialects.postgresql import JSONB
import structlog

from app.config import database
from app.models.signal import Signal, SignalSource, ParsedSignal
from app.models.trade import Order
from app.models.risk import RiskEvent
from app.signal.models import MessageType

logger = structlog.get_logger(__name__)


class SlippageLimitMixin:
    async def mark_slippage_limit(self, signal_id):
        if signal_id is None:
            raise ValueError('Thiếu signal ID để ghi nhận limit do trượt giá; chưa gửi MT5')
        async with database.get_session() as session:
            result = await session.execute(update(ParsedSignal).where(ParsedSignal.signal_id == signal_id).values(
                extra_metadata=func.coalesce(ParsedSignal.extra_metadata, cast({}, JSONB)).op('||')(
                    cast({'entry_mode': 'slippage_limit'}, JSONB))))
            if result.rowcount == 0:
                raise ValueError('Không lưu được chính sách limit của signal; chưa gửi MT5')

    async def slippage_invalidation_targets(self, signal, chat_id, message_id, reply_id=None,
                                            message_path=None, expected_scope=None, is_reentry=False):
        if chat_id is None or message_id is None:
            return []
        if not is_reentry and (signal.message_type != MessageType.POSITION_MANAGEMENT or not signal.management_actions):
            return []
        stmt = (select(Signal.id, Signal.telegram_message_id, ParsedSignal.extra_metadata, ParsedSignal.symbol)
            .join(SignalSource, Signal.source_id == SignalSource.id)
            .join(ParsedSignal, ParsedSignal.signal_id == Signal.id)
            .where(SignalSource.chat_id == chat_id, Signal.message_type == 'NEW_ORDER',
                   Signal.telegram_message_id < message_id))
        path = [i for i in (message_path or []) if type(i) is int and i != message_id]
        explicit_all = bool(re.search(r'(?i)tất cả|toàn bộ|\ball\b', signal.raw_message))
        if reply_id is not None or path:
            ancestors = path or [reply_id]
            stmt = stmt.where(Signal.telegram_message_id.in_(ancestors))
        elif expected_scope:
            if expected_scope.get('chat_id') != chat_id:
                raise ValueError('Phạm vi action không cùng chat; chưa hủy limit')
            stmt = stmt.where(Signal.id == expected_scope['signal_id'])
        async with database.get_session() as session:
            rows = list((await session.execute(stmt.order_by(Signal.telegram_message_id.desc(), Signal.id.desc()))).all())
            if reply_id is not None or path:
                rank = {mid: idx for idx, mid in enumerate(ancestors)}
                rows.sort(key=lambda r: rank[r[1]])
            if not explicit_all or reply_id is not None or path or expected_scope:
                rows = rows[:1]  # Never fall through to an older setup merely because it still has pending orders.
            targets = [sid for sid, _, meta, symbol in rows
                if isinstance(meta, dict) and meta.get('entry_mode') == 'slippage_limit'
                and (reply_id is not None or path or not signal.symbol or symbol == signal.symbol)]
        return targets

    async def preview_slippage_cancellations(self, signal, chat_id, message_id, reply_id=None,
                                             message_path=None, expected_scope=None, is_reentry=False):
        targets = await self.slippage_invalidation_targets(signal, chat_id, message_id, reply_id,
                                                          message_path, expected_scope, is_reentry)
        if not targets:
            return []
        async with database.get_session() as session:
            tickets = set((await session.scalars(select(Order.mt5_ticket).where(
                Order.signal_id.in_(targets), Order.status.in_(['PLACED', 'PENDING'])))).all())
        return [p.ticket for p in await self.broker.get_pending_orders()
                if p.ticket in tickets and p.order_type.endswith('_LIMIT')]

    async def invalidate_slippage_limits(self, signal, chat_id, message_id, reply_id=None,
                                         message_path=None, expected_scope=None, is_reentry=False):
        targets = await self.slippage_invalidation_targets(signal, chat_id, message_id, reply_id,
                                                          message_path, expected_scope, is_reentry)
        if not targets:
            return []
        async with database.get_session() as session:
            for target in targets:
                session.add(RiskEvent(signal_id=target, event_type='LIMIT_INVALIDATED',
                    rule_name='slippage_limit_update', details={'trigger_chat_id': chat_id,
                        'trigger_message_id': message_id, 'reply_to_msg_id': reply_id,
                        'message_path': message_path or []}))
        # Intent is committed before contacting MT5. A failed cancel is retried by sync.
        return await self.cancel_invalidated_slippage_limits(targets)

    async def cancel_invalidated_slippage_limits(self, signal_ids=None):
        invalidated = select(RiskEvent.signal_id).where(RiskEvent.event_type == 'LIMIT_INVALIDATED',
                                                        RiskEvent.rule_name == 'slippage_limit_update')
        if signal_ids is not None:
            invalidated = invalidated.where(RiskEvent.signal_id.in_(signal_ids))
        async with database.get_session() as session:
            tickets = set((await session.scalars(select(Order.mt5_ticket).where(
                Order.signal_id.in_(invalidated), Order.mt5_ticket.isnot(None),
                Order.status.in_(['PLACED', 'PENDING'])))).all())
        if not tickets:
            return []
        try:
            pending = await self.broker.get_pending_orders()
        except Exception as exc:
            logger.error('invalidated_limits_snapshot_failed', error=str(exc))
            return []
        cancelled = []
        for order in pending:
            if order.ticket not in tickets or not order.order_type.endswith('_LIMIT'):
                continue
            result = await self.broker.cancel_order(order.ticket)
            if result.raw.get('uncertain'):
                self.risk_config.enabled = False
                raise RuntimeError('Chưa rõ kết quả hủy limit; đã dừng mở mới để đối soát')
            if result.success:
                await self._update_db_order_status(order.ticket, 'CANCELLED')
                cancelled.append(order.ticket)
            else:
                logger.error('invalidated_limit_cancel_failed', ticket=order.ticket, error=result.comment)
        if cancelled:
            await self.notify(f'🗑 Đã hủy {len(cancelled)} limit chưa khớp do signal có action cập nhật.')
        return cancelled
