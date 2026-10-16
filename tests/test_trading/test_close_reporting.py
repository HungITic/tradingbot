"""Regression tests for broker reasons and exact bot-execution attribution."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.base import DealInfo, OrderResult
from app.broker.mt5.mapper import map_order_result
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


@pytest.fixture
def db():
    session = AsyncMock()
    session.scalars.return_value = MagicMock()
    session.scalars.return_value.all.return_value = []
    @asynccontextmanager
    async def scope():
        yield session
    with patch('app.config.database.get_session', scope):
        yield session


@pytest.fixture
def service(db):
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    service._insert_management_action = AsyncMock()
    return service


def deal(reason=3, *, ticket=100, order=200, position_id=1, profit='10', swap='0', commission='0', entry='OUT'):
    return DealInfo(ticket, order, position_id, 'XAUUSD', 'SELL', entry, D('.05'), D('2670'),
                    D(profit), D(swap), D(commission), datetime.now(timezone.utc), reason=reason)


def execution(success=True):
    return OrderResult(success, ticket=200, volume=D('.05'), raw={'order': 200, 'deal': 100})


@pytest.mark.asyncio
@pytest.mark.parametrize('code,label', [(0, 'máy tính'), (1, 'di động'), (2, 'web'),
                                        (3, 'chương trình'), (4, 'Stop Loss'), (5, 'Take Profit'), (6, 'Stop Out')])
async def test_reason_codes_match_mt5_instead_of_shifted_mapping(service, code, label):
    reason, _ = await service.exit_reason(deal(code))
    assert label in reason


@pytest.mark.asyncio
async def test_profitable_sl_stays_sl_and_losing_tp_stays_tp(service):
    assert 'Stop Loss' in (await service.exit_reason(deal(4, profit='100')))[0]
    assert 'Take Profit' in (await service.exit_reason(deal(5, profit='-10')))[0]


@pytest.mark.asyncio
async def test_sl_tp_and_stopout_take_precedence_over_old_bot_context(service):
    service.close_execution_metadata(1, execution(), 'TP1_PARTIAL', partial=True)
    for code, label in [(4, 'Stop Loss'), (5, 'Take Profit'), (6, 'Stop Out')]:
        reason, context = await service.exit_reason(deal(code))
        assert label in reason and context is None


@pytest.mark.asyncio
@pytest.mark.parametrize('cause,label', [('TP1_PARTIAL', 'TP1'), ('MIN_ENTRY_PARTIAL', 'entry xấu'),
    ('MANUAL_PARTIAL', 'menu / command'), ('SIGNAL_PARTIAL', 'tín hiệu'), ('SIGNAL_CLOSE', 'tín hiệu'),
    ('KILL_SWITCH', 'khẩn cấp'), ('CLOSE_ALL', 'toàn bộ'), ('FINAL_TP', 'TP cuối')])
async def test_exact_deal_execution_identifies_bot_action(service, cause, label):
    service.close_execution_metadata(1, execution(), cause, partial=True)
    reason, context = await service.exit_reason(deal())
    assert label in reason and context['partial_close']


@pytest.mark.asyncio
async def test_prior_tp1_does_not_label_unrelated_later_exit(service):
    service.close_execution_metadata(1, execution(), 'TP1_PARTIAL', partial=True)
    reason, context = await service.exit_reason(deal(ticket=101, order=201))
    assert 'chương trình' in reason and context is None
    reason, context = await service.exit_reason(deal(position_id=2))
    assert context is None


@pytest.mark.asyncio
async def test_failed_close_is_not_used_as_successful_exit_cause(service):
    service.close_execution_metadata(1, execution(False), 'TP1_PARTIAL', partial=True)
    reason, context = await service.exit_reason(deal())
    assert 'chương trình' in reason and context is None


@pytest.mark.asyncio
async def test_order_id_matches_multiple_deals_for_same_execution(service):
    service.close_execution_metadata(1, execution(), 'MANUAL_PARTIAL', partial=True)
    reason, context = await service.exit_reason(deal(ticket=102, order=200))
    assert 'menu / command' in reason and context['partial_close']


@pytest.mark.asyncio
async def test_exact_close_context_survives_restart(service, db):
    metadata = service.close_execution_metadata(1, execution(), 'MIN_ENTRY_PARTIAL', partial=True)
    db.scalars.return_value.all.return_value = [SimpleNamespace(result=metadata)]
    fresh = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    reason, context = await fresh.exit_reason(deal())
    assert 'entry xấu' in reason and context['partial_close']
    assert not fresh._close_executions


@pytest.mark.asyncio
async def test_missing_execution_ids_do_not_guess_from_position_ticket(service):
    result = OrderResult(True, ticket=1)
    service.close_execution_metadata(1, result, 'TP1_PARTIAL', partial=True)
    assert not service._close_executions
    reason, context = await service.exit_reason(deal())
    assert context is None and 'chương trình' in reason


@pytest.mark.asyncio
async def test_audit_preserves_ids_and_bad_entry_cause(service):
    pos = SimpleNamespace(ticket=1, volume=D('.10'))
    await service.record_management(10, pos, 'PARTIAL_CLOSE', execution(), target='MIN_ENTRY')
    audit = service._insert_management_action.call_args.args
    assert audit[4] == 'EXECUTED'
    assert audit[5]['broker_deal_id'] == 100
    assert audit[5]['broker_order_id'] == 200
    assert audit[5]['close_cause'] == 'MIN_ENTRY_PARTIAL'
    assert 'entry xấu' in (await service.exit_reason(deal()))[0]


@pytest.mark.asyncio
async def test_exit_report_is_per_deal_with_net_components_and_correct_link(service):
    service.close_execution_metadata(1, execution(), 'TP1_PARTIAL', partial=True)
    service._order_message_links[1] = 'https://t.me/example/1'
    text, reason = await service.format_exit_notification(deal(profit='10', swap='-1', commission='-2'), 'USC')
    assert 'Đã chốt một phần' in text
    assert '+7.00 USC' in text
    assert 'Swap' in text and 'Commission' in text
    assert 'Kết thúc PnL' not in text
    assert 'https://t.me/example/1' in text
    assert 'TP1' in reason
    assert 'Mã lệnh' in text and '<code>#1</code>' in text
    assert '<code>#100</code>' not in text


@pytest.mark.asyncio
async def test_exit_without_position_id_does_not_show_deal_as_order_id(service):
    text, _ = await service.format_exit_notification(deal(4, position_id=0))
    assert 'Chưa xác định' not in text
    assert '<code>#100</code>' not in text and '<code>#200</code>' not in text


@pytest.mark.asyncio
async def test_reversal_does_not_mislabel_deal_volume_as_closed_volume(service):
    text, _ = await service.format_exit_notification(deal(entry='INOUT'))
    assert 'đảo chiều' in text
    assert 'Khối lượng deal (gồm mở lại)' in text
    assert 'Đã đóng:' not in text


@pytest.mark.asyncio
async def test_unknown_reason_is_explicit_instead_of_invented_tp_or_sl(service):
    text, reason = await service.format_exit_notification(deal(99))
    assert 'Chưa xác định' in reason and '99' in reason
    assert 'đóng toàn bộ' not in text.lower()


def test_bridge_result_object_keeps_execution_ids_for_audit():
    result = map_order_result(SimpleNamespace(retcode=10009, order=200, deal=100, price=2670, volume=.05, comment='Done'))
    assert result.success and result.raw['order'] == 200 and result.raw['deal'] == 100


@pytest.mark.asyncio
async def test_sync_sends_correct_sl_reason_through_real_report_formatter(service):
    import asyncio
    service.broker.is_connected.return_value = True
    service.broker.get_history_deals.side_effect = [[], [deal(4, profit='10')]]
    service.broker.get_account_info.return_value = SimpleNamespace(currency='USD')
    service._insert_trade_record = AsyncMock(return_value=True)
    service._check_tp_milestones = AsyncMock()
    service._reconcile_dual_entry_pending_orders = AsyncMock()
    service.cancel_dual_entry_pending_orders_on_close = AsyncMock()
    service.notify = AsyncMock()
    with patch('asyncio.sleep', AsyncMock(side_effect=[None, asyncio.CancelledError()])):
        with pytest.raises(asyncio.CancelledError):
            await service._sync_loop()
    service.notify.assert_awaited_once()
    text = service.notify.call_args.args[0]
    assert 'Stop Loss (SL)' in text
    assert 'Take Profit' not in text
    assert '+10.00 USD' in text
    assert 'Kết thúc PnL' not in text
