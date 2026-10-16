"""Calendar-month turnover counts executed entry and exit lots, not pending requests."""
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock, patch

import pytest

from app.broker.base import DealInfo
from app.core.timezone import VN_TZ
from app.trading.risk import RiskConfig
from app.trading.service import TradingService
from app.trading.volume import monthly_volume

NOW = datetime(2026, 10, 16, 12, tzinfo=VN_TZ)


def deal(ticket, entry, volume, when=None, side='BUY', position_id=10, symbol='XAUUSD'):
    return DealInfo(ticket, ticket, position_id, symbol, side, entry, D(volume), D('4000'),
                    D('0'), D('0'), D('0'), when or NOW)


def test_counts_both_sides_partial_closes_and_filters_non_trading_deals():
    rows = [deal(1, 'IN', '1'), deal(2, 'OUT', '.3', side='SELL'),
            deal(3, 'OUT', '.7', side='SELL'), deal(4, 'IN', '.2', symbol='EURUSD'),
            deal(5, 'OUT_BY', '.2', side='SELL', symbol='EURUSD'),
            deal(6, 'IN', '1000', side='BALANCE', symbol='')]
    stats = monthly_volume(rows, NOW)
    assert stats['opened'] == stats['closed'] == D('1.2')
    assert stats['total'] == D('2.4')
    assert stats['unallocated'] == 0


def test_calendar_month_uses_vietnam_midnight_and_excludes_future_and_duplicates():
    # 17:00 UTC Sep 30 is exactly midnight Oct 1 in Vietnam.
    first = deal(1, 'IN', '.5', datetime(2026, 9, 30, 17, tzinfo=timezone.utc))
    old = deal(2, 'IN', '9', datetime(2026, 9, 30, 16, 59, 59, tzinfo=timezone.utc))
    future = deal(3, 'IN', '8', datetime(2026, 11, 1, tzinfo=VN_TZ))
    result = monthly_volume([first, first, old, future], NOW)
    assert result['month'] == '10/2026'
    assert result['opened'] == result['total'] == D('.5')


def test_old_position_closed_this_month_only_counts_its_exit_this_month():
    result = monthly_volume([deal(1, 'IN', '1', datetime(2026, 9, 15, tzinfo=VN_TZ)),
                             deal(2, 'OUT', '1', side='SELL')], NOW)
    assert result['opened'] == 0 and result['closed'] == result['total'] == 1


def test_reversal_splits_closing_and_newly_opened_lots_without_double_counting():
    original = deal(1, 'IN', '1', datetime(2026, 9, 15, tzinfo=VN_TZ))
    reverse = deal(2, 'INOUT', '1.4', side='SELL')
    result = monthly_volume([reverse], NOW, history=[original, reverse])
    assert result['closed'] == D('1')
    assert result['opened'] == D('.4')
    assert result['total'] == D('1.4')
    assert result['unallocated'] == 0


def test_reversal_without_old_history_reports_unsplit_volume_instead_of_guessing():
    result = monthly_volume([deal(2, 'INOUT', '1.4', side='SELL')], NOW)
    assert result['opened'] == result['closed'] == 0
    assert result['unallocated'] == result['total'] == D('1.4')


def test_empty_month_is_available_with_zero_volume():
    result = monthly_volume([], NOW)
    assert result['available'] and result['total'] == 0


@pytest.mark.asyncio
async def test_service_queries_from_calendar_month_start_and_reports_connection_failure():
    broker = AsyncMock()
    broker.get_history_deals.return_value = [deal(1, 'IN', '.25')]
    service = TradingService(broker=broker, risk_config=RiskConfig())
    with patch('app.trading.service.now_vietnam', return_value=NOW):
        result = await service.get_monthly_volume()
    broker.get_history_deals.assert_awaited_once_with(start_time=datetime(2026, 10, 1, tzinfo=VN_TZ))
    assert result['opened'] == D('.25')
    broker.get_history_deals.side_effect = RuntimeError('disconnected')
    with patch('app.trading.service.now_vietnam', return_value=NOW):
        result = await service.get_monthly_volume()
    assert not result['available']
    assert 'total' not in result


@pytest.mark.asyncio
async def test_service_fetches_prior_history_only_when_reversal_needs_it():
    broker = AsyncMock()
    original = deal(1, 'IN', '1', datetime(2026, 9, 15, tzinfo=VN_TZ))
    reverse = deal(2, 'INOUT', '1.4', side='SELL')
    broker.get_history_deals.side_effect = [[reverse], [original, reverse]]
    service = TradingService(broker=broker, risk_config=RiskConfig())
    with patch('app.trading.service.now_vietnam', return_value=NOW):
        result = await service.get_monthly_volume()
    assert broker.get_history_deals.await_count == 2
    assert result['opened'] == D('.4') and result['closed'] == 1


def test_missing_history_does_not_invent_balance_for_subsequent_reversals():
    rows = [deal(2, 'INOUT', '1.4', side='BUY'), deal(3, 'INOUT', '1.6', side='SELL')]
    result = monthly_volume(rows, NOW)
    assert result['unallocated'] == result['total'] == D('3.0')
    assert result['opened'] == result['closed'] == 0
