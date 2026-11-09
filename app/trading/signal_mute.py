"""Per-signal opt-out from source-channel management actions.

State is an append-only RiskEvent log (latest event per signal wins), so it is
durable across restarts without a schema migration. The hot path reads an
in-memory copy loaded strictly at startup and updated only after DB commit.
"""
from sqlalchemy import select

from app.config import database
from app.models.risk import RiskEvent
from app.models.signal import Signal
from app.models.trade import Order

MUTED, UNMUTED = 'SIGNAL_ACTIONS_MUTED', 'SIGNAL_ACTIONS_UNMUTED'


class SignalMuteMixin:
    def is_signal_muted(self, signal_id) -> bool:
        return isinstance(signal_id, int) and signal_id in self._muted_signal_ids

    async def load_signal_mutes_from_db(self) -> set[int]:
        async with database.get_session() as session:
            rows = (await session.execute(select(RiskEvent.signal_id, RiskEvent.event_type)
                .where(RiskEvent.event_type.in_([MUTED, UNMUTED]), RiskEvent.signal_id.isnot(None))
                .order_by(RiskEvent.id))).all()
        latest = {}
        for signal_id, event_type in rows:
            latest[signal_id] = event_type
        self._muted_signal_ids = {sid for sid, event in latest.items() if event == MUTED}
        return set(self._muted_signal_ids)

    async def set_signal_actions_muted(self, signal_id, muted: bool, updated_by: str = 'TELEGRAM') -> None:
        if type(signal_id) is not int or signal_id <= 0:
            raise ValueError('Signal không hợp lệ')
        async with database.get_session() as session:
            if await session.scalar(select(Signal.id).where(Signal.id == signal_id)) is None:
                raise ValueError(f'Không tìm thấy kèo #{signal_id}')
            session.add(RiskEvent(signal_id=signal_id, event_type=MUTED if muted else UNMUTED,
                                  rule_name='signal_actions_mute', details={'updated_by': updated_by}))
        # Memory follows the committed DB state only.
        if muted:
            self._muted_signal_ids.add(signal_id)
        else:
            self._muted_signal_ids.discard(signal_id)

    async def exclude_muted_targets(self, positions, pending):
        """Drop positions/pending orders whose stored signal is muted.

        Native TP groups are dropped if any child belongs to a muted signal.
        Items without a stored signal link are kept (never inferred by symbol).
        """
        if not self._muted_signal_ids or (not positions and not pending):
            return positions, pending, set()
        children = [child for p in positions for child in (p.members or (p,))]
        orders = await self.position_orders(children) if children else {}
        pending_signals = {}
        if pending:
            async with database.get_session() as session:
                rows = (await session.execute(select(Order.mt5_ticket, Order.signal_id)
                    .where(Order.mt5_ticket.in_([o.ticket for o in pending])))).all()
            pending_signals = {ticket: signal_id for ticket, signal_id in rows}
        skipped = set()
        kept_positions = []
        for pos in positions:
            muted = {orders[c.ticket].signal_id for c in (pos.members or (pos,))
                     if c.ticket in orders and self.is_signal_muted(orders[c.ticket].signal_id)}
            if muted:
                skipped |= muted
            else:
                kept_positions.append(pos)
        kept_pending = []
        for order in pending:
            signal_id = pending_signals.get(order.ticket)
            if self.is_signal_muted(signal_id):
                skipped.add(signal_id)
            else:
                kept_pending.append(order)
        return kept_positions, kept_pending, skipped

