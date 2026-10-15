"""Regression coverage for checks at actual execution boundaries."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.base import AccountInfo, DealInfo, OrderRequest, OrderResult, TickInfo
from app.core.timezone import now_vietnam
from app.signal.models import MessageType
from app.signal.parser import SignalParser
from app.trading.risk import RiskConfig
from app.trading.service import TradingService

SIGNAL = "BUY XAUUSD 2650\nSL: 2640\nTP1: 2670\nTP2: 2680"


@pytest.fixture
def service():
    broker = AsyncMock()
    broker.get_account_info.return_value = AccountInfo(1, D('900'), D('900'), D('0'), D('900'), 100, 'Demo', 'USD')
    broker.get_positions.return_value = []
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    broker.get_tick.return_value = TickInfo('XAUUSD', D('2650'), D('2650.5'), datetime.now(timezone.utc))
    broker.is_connected.return_value = True
    broker.open_order.return_value = OrderResult(True, ticket=1)
    broker.close_position.return_value = OrderResult(True, ticket=1)
    broker.cancel_order.return_value = OrderResult(True, ticket=2)
    svc = TradingService(broker=broker, risk_config=RiskConfig())
    svc.notify = AsyncMock()
    svc.set_signal_status = AsyncMock()
    svc._record_db_order = AsyncMock()
    svc.save_risk_config_to_db = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def database():
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = 1
    session.execute.return_value = result
    session.scalar.return_value = 1

    @asynccontextmanager
    async def db():
        yield session
    with patch('app.config.database.get_session', db):
        yield session


def deal(ticket, profit, when, kind='SELL', commission='0', swap='0'):
    return DealInfo(ticket, ticket, ticket, 'XAUUSD' if kind != 'BALANCE' else '', kind, 'OUT',
                    D('0.01'), D('2650'), D(profit), D(swap), D(commission), when)


@pytest.mark.asyncio
async def test_daily_loss_uses_real_deals_and_excludes_previous_day(service):
    midnight = now_vietnam().replace(hour=0, minute=0, second=0, microsecond=0)
    # Balance 900 = opening 1000 - 105 trading PnL + 5 deposit.
    service.broker.get_history_deals.return_value = [
        deal(1, '-100', midnight, commission='-3', swap='-2'),
        deal(2, '5', midnight, kind='BALANCE'),
        deal(3, '-999', midnight - timedelta(seconds=1)),
    ]
    context = await service.build_risk_context()
    assert context.today_realized_pnl == D('-105')
    assert context.today_starting_balance == D('1000')
    decision = service.risk_engine.evaluate(service.parser.parse(SIGNAL), context)
    assert not decision.approved
    assert 'daily_loss_limit' in decision.rejection_reason


@pytest.mark.asyncio
async def test_history_failure_blocks_and_cannot_be_overridden(service):
    service.broker.get_history_deals.side_effect = RuntimeError('offline')
    await service.process_raw_message(SIGNAL)
    service.broker.open_order.assert_not_awaited()
    assert not service._pending_overrides
    assert 'daily_history' in service.notify.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', [
    'BUY XAUUSD 2650\nSL: 2660\nTP: 2670',
    'BUY XAUUSD 2650 - 2652\nSL: 2640\nTP: 2651',
    'SELL XAUUSD 2650\nSL: 2660\nTP1: 2640\nTP2: 2645',
])
async def test_invalid_prices_never_reach_broker(service, raw):
    await service.process_raw_message(raw)
    service.broker.open_order.assert_not_awaited()
    assert 'Rejected' in service.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_expired_signal_and_stale_tick_are_blocked(service):
    await service.process_raw_message(SIGNAL, signal_time=datetime.now(timezone.utc) - timedelta(minutes=10))
    service.broker.open_order.assert_not_awaited()
    assert 'hết hạn' in service.notify.call_args.args[0]
    service.broker.get_tick.return_value.time -= timedelta(minutes=3)
    await service.process_raw_message(SIGNAL)
    service.broker.open_order.assert_not_awaited()
    assert 'cũ' in service.notify.call_args.args[0]


@pytest.mark.asyncio
async def test_missing_tick_never_falls_back_to_signal_price(service):
    service.broker.get_tick.side_effect = RuntimeError('no quote')
    await service.process_raw_message(SIGNAL)
    service.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_never_writes_or_mutates_broker(service, database):
    preview = await service.preview_signal(SIGNAL, signal_time=datetime.now(timezone.utc))
    assert preview['can_execute']
    service.broker.open_order.assert_not_awaited()
    service.broker.close_position.assert_not_awaited()
    service.broker.modify_position.assert_not_awaited()
    database.execute.assert_not_awaited()
    database.add.assert_not_called()
    service.notify.assert_not_awaited()


def pending(service, allowed=None, when=None):
    service._pending_overrides[1] = {
        'signal': service.parser.parse(SIGNAL), 'signal_time': when or datetime.now(timezone.utc),
        'db_id': 1, 'orders': {'min': OrderRequest('XAUUSD', 'BUY', D('.01'), D('2650'), D('2640'), D('2680'))},
        'allowed_rule': allowed,
    }


@pytest.mark.asyncio
async def test_approval_rechecks_pause_expiry_and_current_sl(service):
    pending(service)
    service.risk_config.enabled = False
    assert 'Không thể duyệt' in await service.execute_override_order(1, 'both')
    service.broker.open_order.assert_not_awaited()
    service.risk_config.enabled = True
    pending(service, when=datetime.now(timezone.utc) - timedelta(minutes=10))
    assert 'hết hạn' in await service.execute_override_order(1, 'both')
    assert 1 not in service._pending_overrides
    pending(service)
    service.broker.get_tick.return_value = TickInfo('XAUUSD', D('2630'), D('2630.5'), datetime.now(timezone.utc))
    assert 'Stop loss' in await service.execute_override_order(1, 'both')
    service.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_double_click_approval_executes_once(service):
    pending(service)
    await asyncio.gather(service.execute_override_order(1, 'both'), service.execute_override_order(1, 'both'))
    service.broker.open_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_history_cannot_bypass_daily_loss_override(service):
    pending(service, allowed='daily_loss_limit')
    service.broker.get_history_deals.side_effect = RuntimeError('unavailable')
    await service.execute_override_order(1, 'both')
    service.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_receipt_blocks_execution_and_db_failure_fails_closed(service, database):
    database.execute.return_value.scalar_one_or_none.return_value = None
    await service.process_raw_message(SIGNAL, chat_id=1, message_id=1)
    service.broker.open_order.assert_not_awaited()
    database.execute.side_effect = RuntimeError('DB unavailable')
    await service.process_raw_message(SIGNAL, chat_id=1, message_id=2)
    service.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_kill_reports_failures_and_cancels_pending_first(service):
    position, order = MagicMock(ticket=10, volume=D('.01')), MagicMock(ticket=20)
    service.broker.get_positions.return_value = [position]
    service.broker.get_pending_orders.return_value = [order]
    service.broker.close_position.return_value = OrderResult(False, ticket=10, comment='rejected')
    report = await service.emergency_stop()
    assert not service.risk_config.enabled
    assert report['remaining_positions'] == report['remaining_orders'] == 1
    assert not report['closes'][0].success
    assert report['cancellations'][0].success
    calls = [call[0] for call in service.broker.mock_calls]
    assert calls.index('cancel_order') < calls.index('close_position')


@pytest.mark.asyncio
async def test_config_is_atomic_and_rolls_back_after_save_failure(service):
    previous = service.risk_config.model_dump()
    result = await service.update_config_keys({'default_lot': '.10', 'max_lot': '.05'})
    assert not any(result.values())
    assert service.risk_config.model_dump() == previous
    service.save_risk_config_to_db.return_value = False
    result = await service.update_config_keys({'default_lot': '.02'})
    assert not any(result.values())
    assert service.risk_config.model_dump() == previous


@pytest.mark.parametrize('updates', [{'require_sl': 'maybe'}, {'max_open_positions': -1},
                                      {'signal_expire_seconds': 0}, {'default_lot': 'NaN'},
                                      {'max_daily_loss_percent': 101}, {'default_partial_close_percent': 100}])
def test_invalid_config_values_are_rejected(updates):
    with pytest.raises(ValueError):
        RiskConfig().with_updates(updates)


@pytest.mark.parametrize('text', ['Chưa chốt vàng nhé', 'Đừng đóng lệnh',
                                  'Nếu lên 2670 thì chốt hết vàng', 'Do not close gold'])
def test_negated_and_conditional_instructions_need_review(text):
    parsed = SignalParser().parse(text)
    assert parsed.message_type == MessageType.UNKNOWN
    assert parsed.metadata['reason']


@pytest.mark.asyncio
async def test_dual_entry_reserves_both_slots_including_pending_orders(service):
    service.risk_config.max_open_positions = 2
    service.broker.get_pending_orders.return_value = [MagicMock(ticket=10)]
    await service.process_raw_message('BUY XAUUSD 2650 - 2648\nSL: 2640\nTP: 2670')
    service.broker.open_order.assert_not_awaited()
    assert any(entry['allowed_rule'] == 'max_positions' for entry in service._pending_overrides.values())


@pytest.mark.asyncio
async def test_failed_first_entry_does_not_create_orphan_second_entry(service):
    service.broker.open_order.return_value = OrderResult(False, comment='rejected')
    await service.process_raw_message('BUY XAUUSD 2650 - 2648\nSL: 2640\nTP: 2670')
    service.broker.open_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_unresolved_reply_does_not_fall_back_to_all_positions_of_symbol(service):
    service.get_tickets_for_reply_message = AsyncMock(return_value=[])
    service.broker.get_positions.return_value = [MagicMock(symbol='XAUUSD', ticket=5)]
    await service.process_raw_message('Chốt hết vàng', reply_to_msg_id=999)
    service.broker.close_position.assert_not_awaited()
    service.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_survives_new_service_instance(service, database):
    # Simulate the unique row retained in PostgreSQL across process instances.
    claimed = set()
    def execute(stmt):
        key = (stmt.compile().params.get('chat_id'), stmt.compile().params.get('message_id'))
        result = MagicMock()
        result.scalar_one_or_none.return_value = None if key in claimed else key[1]
        claimed.add(key)
        return result
    database.execute.side_effect = execute
    service._process_raw_message = AsyncMock()
    await service.process_raw_message(SIGNAL, chat_id=10, message_id=20)
    service._process_raw_message.assert_awaited_once()
    fresh = TradingService(broker=service.broker, risk_config=RiskConfig())
    fresh._process_raw_message = AsyncMock()
    fresh.notify = AsyncMock()
    await fresh.process_raw_message(SIGNAL, chat_id=10, message_id=20)
    fresh._process_raw_message.assert_not_awaited()
    await fresh.process_raw_message(SIGNAL, chat_id=11, message_id=20)
    fresh._process_raw_message.assert_awaited_once()
