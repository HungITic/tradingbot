"""History and pending cancellation share broker position/opening-order identity."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.base import PendingOrderInfo, PositionInfo, OrderResult
from app.broker.mt5.executor import MT5Executor
from app.broker.mt5.history import read_history_deals
from app.broker.mt5.mapper import map_deal_info
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


@pytest.fixture
def database():
    session = AsyncMock()
    session.scalar.return_value = None
    session.scalars.return_value = MagicMock()
    session.scalars.return_value.all.return_value = []
    @asynccontextmanager
    async def scope():
        yield session
    with patch('app.config.database.get_session', scope):
        yield session


@pytest.fixture
def records():
    base = dict(position_id=7001, symbol='XAUUSD.sc', swap=0, commission=0, magic=234000)
    stamp = int(datetime(2026, 10, 19, 6, tzinfo=timezone.utc).timestamp())
    return [
        dict(base, ticket=500, order=345557460, entry=0, type=1, volume=1.5,
             price=4182.67, profit=0, time=stamp),
        dict(base, ticket=501, order=345580000, entry=1, type=0, volume=.75,
             price=4177.33, profit=400.10, time=stamp + 23 * 60, reason=3),
        dict(base, ticket=502, order=345596165, entry=1, type=0, volume=.75,
             price=4183.08, profit=-30.78, time=stamp + 57 * 60, reason=4),
    ]


@pytest.fixture
def service(database):
    broker = AsyncMock()
    broker.get_positions.return_value = []
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    broker.get_account_info.return_value = SimpleNamespace(currency='USC')
    broker.cancel_order.return_value = OrderResult(True)
    broker.close_position.return_value = OrderResult(True)
    service = TradingService(broker, RiskConfig())
    service.notify = AsyncMock()
    service._update_db_order_status = AsyncMock(return_value=True)
    return service


def pending(ticket=9002, comment='bot-max-entry'):
    return PendingOrderInfo(ticket, 'XAUUSD.sc', 'SELL_LIMIT', D('1.5'), D('4190'),
                            D('4200'), D('4100'), comment, datetime.now(timezone.utc))


def active():
    # Position ticket may change; the broker identifier stays fixed.
    return PositionInfo(8001, 'XAUUSD.sc', D('.75'), D('4182.67'), D('4177.33'), None,
                        None, D('0'), D('0'), D('0'), 'SELL', datetime.now(timezone.utc), identifier=7001)


@pytest.mark.asyncio
async def test_mac_bridge_to_history_groups_two_exits_and_retains_signal(service, records):
    backend = SimpleNamespace(_send=MagicMock(return_value={'deals': records}))
    api = SimpleNamespace(__name__='mt5_mac', _backend=backend)
    executor = MT5Executor()
    executor._client = SimpleNamespace(is_connected=True,
        run_in_thread=AsyncMock(side_effect=lambda fn, *args, **kwargs: fn(*args, **kwargs)))
    # Exercise the actual adapter instead of injecting pre-corrected DealInfo.
    with patch('app.broker.mt5.executor.mt5', api):
        deals = await executor.get_history_deals()
    assert [d.position_id for d in deals] == [7001, 7001, 7001]
    assert [d.order for d in deals] == [345557460, 345580000, 345596165]
    service.broker.get_history_deals.return_value = deals
    service._order_message_links[345557460] = 'https://t.me/signals/123'
    positions = await service.get_closed_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.ticket == 7001 and pos.open_price == D('4182.67')
    assert pos.volume == pos.total_volume == D('1.5')
    assert pos.net_pnl == D('369.32') and pos.close_price == D('4180.20500')
    assert pos.deals_count == 2 and pos.is_fully_closed is True
    assert [d.profit for d in pos.partial_deals] == [D('400.10'), D('-30.78')]
    assert await service.history_signal_link(pos) == 'https://t.me/signals/123'
    from app.telegram.personal_bot import PersonalTelegramBot
    period = (datetime(2026, 10, 19, tzinfo=timezone.utc),
              datetime(2026, 10, 20, tzinfo=timezone.utc), '19/10/2026')
    with patch('app.telegram.personal_bot.history_period', return_value=period):
        text, _ = await PersonalTelegramBot(service)._format_history_detail(7, 0, 7001)
    assert '+369.32 USC' in text and '2 lần chốt' in text
    assert '+400.10 USC' in text and '-30.78 USC' in text
    assert 'https://t.me/signals/123' in text


def test_native_history_uses_official_position_filter():
    api = SimpleNamespace(__name__='MetaTrader5', history_deals_get=MagicMock(return_value=[]))
    read_history_deals(api, 0, 100, position_id=7001)
    api.history_deals_get.assert_called_once_with(position=7001)


@pytest.mark.asyncio
async def test_partial_history_uses_stable_active_identifier(service, records):
    service.broker.get_history_deals.return_value = [map_deal_info(d) for d in records[:2]]
    service.broker.get_positions.return_value = [active()]
    pos, = await service.get_closed_positions()
    assert pos.is_fully_closed is False
    assert pos.volume == D('.75') and pos.total_volume == D('1.5')


@pytest.mark.asyncio
async def test_missing_entry_is_recovered_by_exact_position_and_cached(service, records):
    original, *exits = [map_deal_info(d) for d in records]
    async def history(**kwargs):
        return [original, *exits] if kwargs.get('position_id') == 7001 else exits
    service.broker.get_history_deals.side_effect = history
    pos, = await service.get_closed_positions()
    assert pos.open_price == D('4182.67') and pos.deals_count == 2
    assert pos.volume == pos.total_volume == D('1.5')
    assert any(call.kwargs == {'position_id': 7001} for call in service.broker.get_history_deals.call_args_list)
    service.broker.get_history_deals.reset_mock()
    await service.get_closed_positions()
    assert service.broker.get_history_deals.await_count == 1


@pytest.mark.asyncio
async def test_failed_live_snapshot_does_not_claim_fully_closed(service, records):
    service.broker.get_history_deals.return_value = [map_deal_info(d) for d in records]
    service.broker.get_positions.side_effect = RuntimeError('disconnected')
    pos, = await service.get_closed_positions()
    assert pos.is_fully_closed is None


@pytest.mark.asyncio
async def test_reconcile_after_restart_cancels_max_on_partial_exit_from_same_signal(service, database, records):
    service.broker.get_history_deals.return_value = [map_deal_info(d) for d in records[:2]]
    service.broker.get_positions.return_value = [active()]
    service.broker.get_pending_orders.return_value = [pending(), pending(9003)]
    # Restore the exact signal/order association from DB, without an in-memory group.
    database.scalar.side_effect = lambda statement: 10
    def siblings(statement):
        # Both pending signals are inspected, but only 9002 belongs to this entry.
        sql = str(statement.compile(compile_kwargs={'literal_binds': True}))
        result = MagicMock()
        result.all.return_value = [345557460, 9002] if '!= 9003' not in sql else [9004]
        return result
    database.scalars.side_effect = siblings
    assert await service._reconcile_dual_entry_pending_orders() == [9002]
    service.broker.cancel_order.assert_awaited_once_with(9002)


@pytest.mark.asyncio
async def test_successful_partial_cancels_max_only_and_failure_is_retryable(service, records):
    pos = replace(active(), ticket=345557460, volume=D('1.5'))
    service.broker.get_symbol_info.return_value = SimpleNamespace(volume_min=D('.01'), volume_step=D('.01'))
    service._signal_db_id_to_tickets = {10: [345557460, 9002], 11: [9003, 9004]}
    service.broker.get_pending_orders.return_value = [pending(), pending(9003)]
    service.broker.cancel_order.side_effect = [OrderResult(False), OrderResult(True)]
    result, volume = await service.partial_one(pos, D('50'))
    assert result.success and volume == D('.75')
    assert 9002 not in service._cancelled_dual_entry_tickets
    assert await service.cancel_dual_entry_pending_orders_on_close(pos.ticket) == [9002]
    assert all(call.args == (9002,) for call in service.broker.cancel_order.call_args_list)
    assert await service.cancel_dual_entry_pending_orders_on_close(pos.ticket) == []


@pytest.mark.asyncio
async def test_failed_partial_and_wrong_pending_branch_are_not_cancelled(service):
    service._signal_db_id_to_tickets = {10: [345557460, 9002]}
    service.broker.get_pending_orders.return_value = [pending()]
    service.broker.get_symbol_info.return_value = SimpleNamespace(volume_min=D('.01'), volume_step=D('.01'))
    service.broker.close_position.return_value = OrderResult(False)
    pos = replace(active(), ticket=345557460)
    result, _ = await service.partial_one(pos, D('50'))
    assert not result.success
    service.broker.cancel_order.assert_not_awaited()
    service.broker.get_pending_orders.return_value = [pending(comment='bot-min-entry')]
    assert await service.cancel_dual_entry_pending_orders_on_close(pos.ticket) == []
    service.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_number_collision_does_not_cancel_another_signal(service):
    service._ticket_to_signal_db_id = {345557460: 10, 9002: 10, 9003: 11}
    service._signal_db_id_to_tickets = {10: [345557460, 9002], 11: [9003]}
    # Telegram message IDs repeat across channels. This cache is not sufficient evidence.
    service._ticket_to_msg_id = {345557460: 123, 9003: 123}
    service._signal_msg_id_to_tickets = {123: [345557460, 9002, 9003]}
    service.broker.get_pending_orders.return_value = [pending(), pending(9003)]
    assert await service.cancel_dual_entry_pending_orders_on_close(345557460) == [9002]
    service.broker.cancel_order.assert_awaited_once_with(9002)
