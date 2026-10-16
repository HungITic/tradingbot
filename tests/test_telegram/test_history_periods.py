"""Calendar history must discard MT5's padded range before aggregating exits."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.broker.base import DealInfo
from app.core.timezone import VN_TZ
from app.telegram.history_periods import history_period
from app.telegram.personal_bot import PersonalTelegramBot
from app.trading.risk import RiskConfig
from app.trading.service import TradingService

NOW = datetime(2026, 10, 16, 12, tzinfo=VN_TZ)


@pytest.mark.parametrize('period,start,end,label', [
    ('today', datetime(2026, 10, 16, tzinfo=VN_TZ), NOW, 'Hôm nay'),
    ('yesterday', datetime(2026, 10, 15, tzinfo=VN_TZ), datetime(2026, 10, 16, tzinfo=VN_TZ), 'Hôm qua'),
    ('month', datetime(2026, 10, 1, tzinfo=VN_TZ), NOW, 'Tháng này'),
])
def test_calendar_boundaries(period, start, end, label):
    with patch('app.telegram.history_periods.now_vietnam', return_value=NOW):
        assert history_period(period) == (start, end, label)


def test_yesterday_and_month_at_new_year():
    now = datetime(2027, 1, 1, 1, tzinfo=VN_TZ)
    with patch('app.telegram.history_periods.now_vietnam', return_value=now):
        assert history_period('yesterday')[:2] == (datetime(2026, 12, 31, tzinfo=VN_TZ), datetime(2027, 1, 1, tzinfo=VN_TZ))
        assert history_period('month')[:2] == (datetime(2027, 1, 1, tzinfo=VN_TZ), now)


def deal(ticket, time, entry='OUT', position=42):
    return DealInfo(ticket=ticket, order=ticket, position_id=position, symbol='XAUUSD',
                    deal_type='BUY' if entry == 'IN' else 'SELL', entry=entry,
                    volume=D('.10') if entry == 'IN' else D('.01'), price=D('2650'),
                    profit=D('0') if entry == 'IN' else D('10'), swap=D('0'),
                    commission=D('-1'), time=time)


def make_service(deals):
    broker = AsyncMock()
    broker.get_history_deals.return_value = deals
    broker.get_positions.return_value = []
    broker.get_account_info.return_value = SimpleNamespace(currency='USD')
    return TradingService(broker=broker, risk_config=RiskConfig())


@pytest.mark.asyncio
async def test_padded_history_is_filtered_before_partial_exit_totals():
    midnight = NOW.replace(hour=0)
    service = make_service([
        deal(1, midnight - timedelta(days=1), 'IN'),
        deal(2, midnight - timedelta(seconds=1)),  # yesterday: exclude fees and volume
        deal(3, midnight.astimezone(timezone.utc)),  # exactly VN midnight: include
        deal(4, NOW - timedelta(seconds=1)),
        deal(5, NOW + timedelta(hours=1)),  # padded future: exclude
    ])
    with patch('app.telegram.history_periods.now_vietnam', return_value=NOW):
        bot = PersonalTelegramBot(service)
        text, keyboard = await bot._format_history_message('today')
    assert 'Hôm nay' in text and '+18.00 USD' in text and 'Đóng 0.02/0.1 lot' in text
    service.broker.get_history_deals.assert_awaited_once_with(start_time=midnight)
    positions = await service.get_closed_positions(start_time=midnight, end_time=NOW)
    assert len(positions) == 1
    pos = positions[0]
    assert pos.open_price == D('2650')  # keep opening context from before today
    assert pos.commission == D('-2') and pos.net_pnl == D('18')
    assert [d.ticket for d in pos.partial_deals] == [3, 4]
    assert pos.deals_count == 2 and pos.volume == D('.02')


@pytest.mark.asyncio
async def test_yesterday_excludes_today_and_two_days_ago():
    midnight = NOW.replace(hour=0)
    service = make_service([
        deal(1, midnight - timedelta(days=2), 'IN'),
        deal(2, midnight - timedelta(days=1, seconds=1)),
        deal(3, (midnight - timedelta(days=1)).astimezone(timezone.utc)),
        deal(4, midnight - timedelta(seconds=1)),
        deal(5, midnight),
    ])
    with patch('app.telegram.history_periods.now_vietnam', return_value=NOW):
        text, keyboard = await PersonalTelegramBot(service)._format_history_message('yesterday')
    assert 'Hôm qua' in text and '+18.00 USD' in text
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert 'history_detail_yesterday_0_42_0' in callbacks
    assert 'history_yesterday_0' in callbacks


@pytest.mark.asyncio
async def test_month_excludes_previous_month_exits():
    month_start = NOW.replace(day=1, hour=0)
    service = make_service([
        deal(1, month_start - timedelta(hours=2), 'IN'),
        deal(2, month_start - timedelta(seconds=1)),
        deal(3, month_start.astimezone(timezone.utc)),
    ])
    with patch('app.telegram.history_periods.now_vietnam', return_value=NOW):
        text, keyboard = await PersonalTelegramBot(service)._format_history_message('month')
    assert 'Tháng này' in text and '+9.00 USD' in text and 'Đóng 0.01/0.1 lot' in text


@pytest.mark.asyncio
async def test_calendar_callback_preserves_filter_for_detail_and_back():
    from app.telegram.keyboards import build_history_keyboard
    service = make_service([])
    bot = PersonalTelegramBot(service)
    context = SimpleNamespace(user_data={})
    query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    with patch('app.telegram.history_periods.now_vietnam', return_value=NOW):
        for data in ['history_today_0', 'history_yesterday_0', 'history_month_0', 'history_detail_month_2_42_0']:
            query.data = data
            await bot.handle_callback.__wrapped__(bot, update, context)
    back = query.edit_message_text.call_args.kwargs['reply_markup']
    assert back.inline_keyboard[0][0].callback_data == 'history_month_2'
    keyboard = build_history_keyboard('today')
    filters = {b.text: b.callback_data for row in keyboard.inline_keyboard for b in row}
    assert filters['Hôm nay'] == 'history_today_0'
    assert filters['Hôm qua'] == 'history_yesterday_0'
    assert filters['Tháng này'] == 'history_month_0'
