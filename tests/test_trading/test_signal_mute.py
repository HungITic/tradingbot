"""Per-signal opt-out from channel management actions."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import OrderResult, PendingOrderInfo, PositionInfo, SymbolInfo, TickInfo
from app.models.risk import RiskEvent
from app.signal.parser import SignalParser
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


def position(ticket, side='BUY'):
    return PositionInfo(ticket, 'XAUUSD', D('.10'), D('2650'), D('2670'), None, D('2690'),
                        D('5'), D('0'), D('0'), side, datetime.now(timezone.utc))


def pending(ticket):
    return PendingOrderInfo(ticket, 'XAUUSD', 'BUY_LIMIT', D('.1'), D('2640'),
                            D('2630'), D('2690'), 'bot-max-entry', datetime.now(timezone.utc))


@pytest.fixture
def svc():
    broker = AsyncMock()
    broker.get_positions.return_value = [position(1), position(2)]
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    broker.get_symbol_info.return_value = SymbolInfo('XAUUSD', 3, D('.001'), D('.01'), D('100'), D('.01'), D('100'))
    broker.get_tick.side_effect = lambda s: TickInfo(s, D('2670'), D('2670.1'), datetime.now(timezone.utc))
    broker.close_position.side_effect = lambda t, v: OrderResult(True, ticket=t, volume=v, comment='Closed')
    broker.cancel_order.side_effect = lambda t: OrderResult(True, ticket=t)
    service = TradingService(broker=broker, risk_config=RiskConfig())
    service.position_orders = AsyncMock(side_effect=lambda items: {
        p.ticket: SimpleNamespace(mt5_ticket=p.ticket, signal_id=10 if p.ticket == 1 else 20,
                                  comment=None, volume=p.volume, tp1=None, tp2=None)
        for p in items})
    service.management_history = AsyncMock(return_value=(set(), set()))
    service._insert_management_action = AsyncMock()
    service.set_signal_status = AsyncMock()
    service.notify = AsyncMock()
    service._update_db_order_status = AsyncMock(return_value=True)
    return service


@pytest.mark.asyncio
async def test_symbol_action_skips_only_muted_signal(svc):
    svc._muted_signal_ids = {10}
    await svc.process_management_signal(SignalParser().parse('XAUUSD đóng buy'), 99, None, None)
    svc.broker.close_position.assert_awaited_once_with(2, D('.10'))
    assert any('#10' in c.args[0] for c in svc.notify.call_args_list)


@pytest.mark.asyncio
async def test_action_targeting_only_muted_signal_is_skipped(svc):
    svc._muted_signal_ids = {10, 20}
    await svc.process_management_signal(SignalParser().parse('XAUUSD đóng buy'), 99, None, None)
    svc.broker.close_position.assert_not_awaited()
    svc.set_signal_status.assert_awaited_with(99, 'SKIPPED', 'Kèo #10, #20 đang ngừng nhận action')


@pytest.mark.asyncio
async def test_reply_close_keeps_muted_pending_orders(svc, monkeypatch):
    svc._muted_signal_ids = {10}
    svc.broker.get_positions.return_value = [position(1)]
    svc.broker.get_pending_orders.return_value = [pending(5)]
    svc.get_tickets_for_reply_message = AsyncMock(return_value=[1, 5])
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = [(5, 10)]
    session.execute.return_value = result

    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    await svc.process_management_signal(SignalParser().parse('đóng lệnh'), 99, 1000, None)
    svc.broker.cancel_order.assert_not_awaited()
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_unmuted_service_does_not_touch_database_for_filtering(svc):
    positions, pending_orders, skipped = await svc.exclude_muted_targets([position(1)], [pending(5)])
    assert [p.ticket for p in positions] == [1] and [o.ticket for o in pending_orders] == [5]
    assert not skipped
    svc.position_orders.assert_not_awaited()


@pytest.mark.asyncio
async def test_mute_state_is_durable_and_latest_event_wins(monkeypatch):
    added = []
    session = AsyncMock()
    session.add = MagicMock(side_effect=added.append)
    session.scalar.return_value = 10
    result = MagicMock()
    session.execute.return_value = result

    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    svc = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    await svc.set_signal_actions_muted(10, True)
    assert svc.is_signal_muted(10)
    assert isinstance(added[0], RiskEvent) and added[0].event_type == 'SIGNAL_ACTIONS_MUTED'
    await svc.set_signal_actions_muted(10, False)
    assert not svc.is_signal_muted(10)

    result.all.return_value = [(10, 'SIGNAL_ACTIONS_MUTED'), (20, 'SIGNAL_ACTIONS_MUTED'),
                               (10, 'SIGNAL_ACTIONS_UNMUTED')]
    restarted = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    assert await restarted.load_signal_mutes_from_db() == {20}


@pytest.mark.asyncio
async def test_slippage_limits_of_muted_signal_are_kept(svc):
    svc._muted_signal_ids = {10}
    svc.slippage_invalidation_targets = AsyncMock(return_value=[10])
    svc.cancel_invalidated_slippage_limits = AsyncMock()
    assert await svc.invalidate_slippage_limits(SignalParser().parse('XAUUSD đóng'), 1, 2) == []
    svc.cancel_invalidated_slippage_limits.assert_not_awaited()

