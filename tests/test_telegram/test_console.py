"""Behavioral tests for preview/confirmation and paginated management."""
import time
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import PositionInfo, SymbolInfo, OrderResult
from app.telegram.personal_bot import PersonalTelegramBot
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


@pytest.fixture
def bot():
    broker = AsyncMock()
    service = TradingService(broker=broker, risk_config=RiskConfig())
    service.save_risk_config_to_db = AsyncMock(return_value=True)
    service.process_raw_message = AsyncMock()
    service.position_orders = AsyncMock(return_value={})
    service.management_history = AsyncMock(return_value=(set(), set()))
    service._insert_management_action = AsyncMock()
    service.broker.get_positions.return_value = []
    service.broker.get_symbol_info.return_value = SymbolInfo('XAUUSD', 2, D('.01'), D('.01'), D('100'), D('.01'), D('100'))
    return PersonalTelegramBot(service)


def callback():
    query = SimpleNamespace(edit_message_text=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
    return SimpleNamespace(callback_query=query), SimpleNamespace(user_data={})


def position(ticket, volume='.02'):
    return PositionInfo(ticket, 'XAUUSD', D(volume), D('2650'), D('2655'), D('2640'), D('2670'), D('5'), D('0'), D('0'), 'BUY', datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_config_needs_confirmation_and_is_single_use(bot):
    text, keyboard = bot.prepare_config({'default_lot': '.02'})
    assert '0.01' in text and '0.02' in text
    assert bot.service.risk_config.default_lot == D('.01')
    bot.service.save_risk_config_to_db.assert_not_awaited()
    update, context = callback()
    data = keyboard.inline_keyboard[0][0].callback_data
    await bot.handle_console_callback(update, context, data)
    assert bot.service.risk_config.default_lot == D('.02')
    await bot.handle_console_callback(update, context, data)
    bot.service.save_risk_config_to_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_config_preview_does_not_overwrite_newer_setting(bot):
    _, keyboard = bot.prepare_config({'default_lot': '.02'})
    bot.service.risk_config.max_open_positions = 2
    update, context = callback()
    await bot.handle_console_callback(update, context, keyboard.inline_keyboard[0][0].callback_data)
    assert bot.service.risk_config.default_lot == D('.01')
    bot.service.save_risk_config_to_db.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_or_cancelled_signal_preview_never_executes(bot):
    update, context = callback()
    token = bot.new_draft('signal', {'raw_text': 'BUY GOLD'})
    bot._drafts[token]['expires'] = time.monotonic() - 1
    await bot.handle_console_callback(update, context, f'draft_ok_{token}')
    token = bot.new_draft('signal', {'raw_text': 'BUY GOLD'})
    await bot.handle_console_callback(update, context, f'draft_no_{token}')
    await bot.handle_console_callback(update, context, f'draft_ok_{token}')
    bot.service.process_raw_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('replay', [False, True])
async def test_confirm_signal_routes_through_checks_once(bot, replay):
    update, context = callback()
    payload = {'raw_text': 'BUY GOLD', 'signal_time': datetime.now(timezone.utc)}
    if replay:
        payload['skip_signal_expiry_check'] = True
    token = bot.new_draft('signal', payload)
    await bot.handle_console_callback(update, context, f'draft_ok_{token}')
    await bot.handle_console_callback(update, context, f'draft_ok_{token}')
    bot.service.process_raw_message.assert_awaited_once_with(**payload)


@pytest.mark.asyncio
async def test_close_all_button_only_stages_exact_ticket_list(bot):
    update, context = callback()
    bot.service.broker.get_positions.return_value = [position(1), position(2)]
    await bot.handle_console_callback(update, context, 'close_all_confirm')
    bot.service.broker.close_position.assert_not_awaited()
    assert list(bot._drafts.values())[0]['payload']['tickets'] == [1, 2]
    assert '2 ticket' in update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_partial_close_never_turns_into_full_close_at_minimum_volume(bot):
    bot.service.broker.get_positions.return_value = [position(1, '.01')]
    results = await bot.service.partial_close_positions(ticket=1, percentage=D('50'))
    assert not results[0][1].success
    bot.service.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_close_honors_broker_step_and_exact_ticket(bot):
    bot.service.broker.get_positions.return_value = [position(1, '.05'), position(2, '.05')]
    bot.service.broker.close_position.return_value = OrderResult(True, ticket=1)
    await bot.service.partial_close_positions(ticket=1, percentage=D('50'))
    bot.service.broker.close_position.assert_awaited_once_with(1, D('.02'))


@pytest.mark.asyncio
async def test_every_position_is_accessible_with_matching_page_buttons(bot):
    bot.service.broker.get_positions.return_value = [position(i) for i in range(1, 13)]
    text, keyboard = await bot.positions_view(2)
    assert '#11' in text and '#12' in text and '#1\n' not in text
    data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert 'close_11' in data and 'close_12' in data
    assert 'close_1' not in data
    assert 'positions_page_1' in data


@pytest.mark.asyncio
async def test_failed_config_save_reports_failure_and_keeps_old_values(bot):
    bot.service.save_risk_config_to_db.return_value = False
    _, keyboard = bot.prepare_config({'default_lot': '.02'})
    update, context = callback()
    await bot.handle_console_callback(update, context, keyboard.inline_keyboard[0][0].callback_data)
    assert bot.service.risk_config.default_lot == D('.01')
    assert 'Không lưu được' in update.callback_query.edit_message_text.call_args.args[0]


@pytest.mark.asyncio
async def test_health_never_labels_disconnected_listener_ready(bot):
    from unittest.mock import patch
    from contextlib import asynccontextmanager
    from app.telegram.signal_listener import TelegramSignalListener
    listener = TelegramSignalListener(service=bot.service)
    listener._running = True
    listener.client = MagicMock()
    listener.client.is_connected.return_value = False
    bot.service.broker.is_connected.return_value = False
    @asynccontextmanager
    async def unavailable():
        raise RuntimeError('DB offline')
        yield
    with patch('app.telegram.signal_listener.signal_listener', listener), patch('app.config.database.get_session', unavailable):
        text = await bot.health_view()
    assert 'MT5: ❌' in text
    assert 'Telegram nhận tín hiệu: ❌' in text
    assert 'Database: ❌' in text
    assert 'Đồng bộ lệnh: ❌' in text


@pytest.mark.asyncio
async def test_policy_choice_is_previewed_before_saving(bot):
    update, context = callback()
    await bot.handle_console_callback(update, context, 'cfg_tp1_min_volume_action')
    assert bot.service.risk_config.tp1_min_volume_action == 'breakeven'
    await bot.handle_console_callback(update, context, 'policy_:tp1_min_volume_action:skip')
    bot.service.save_risk_config_to_db.assert_not_awaited()
    assert bot.service.risk_config.tp1_min_volume_action == 'breakeven'
    token = next(iter(bot._drafts))
    await bot.handle_console_callback(update, context, f'draft_ok_{token}')
    assert bot.service.risk_config.tp1_min_volume_action == 'skip'


@pytest.mark.asyncio
async def test_be_button_previews_and_confirms_exact_proposal(bot):
    update, context = callback()
    bot.service.breakeven_preview = AsyncMock(return_value={'text': 'SL dự kiến: 2650', 'price': D('2650')})
    bot.service.move_to_breakeven = AsyncMock(return_value=OrderResult(True, price=D('2650')))
    await bot.handle_console_callback(update, context, 'be_1')
    bot.service.move_to_breakeven.assert_not_awaited()
    token = next(iter(bot._drafts))
    await bot.handle_console_callback(update, context, f'draft_ok_{token}')
    bot.service.move_to_breakeven.assert_awaited_once_with(1, expected_price=D('2650'))


def test_settings_menu_exposes_every_editable_runtime_field_once():
    from collections import Counter
    from app.telegram.configuration import CONFIG_FIELDS, CONFIG_SECTIONS
    from app.telegram.keyboards import build_risk_config_keyboard

    # breakeven_mode is fixed policy, deliberately not an editable setting.
    expected = set(RiskConfig.model_fields) - {'breakeven_mode'}
    callbacks = [button.callback_data[4:]
                 for group in CONFIG_SECTIONS
                 for row in build_risk_config_keyboard(RiskConfig(), group).inline_keyboard
                 for button in row if button.callback_data.startswith('cfg_')]
    assert set(CONFIG_FIELDS) == expected
    assert Counter(callbacks) == Counter({key: 1 for key in expected})


@pytest.mark.asyncio
@pytest.mark.parametrize('key,value,expected', [
    ('min_lot', '0.005', D('.005')),
    ('max_lot', '0.1', D('.1')),
    ('allowed_symbols', 'XAUUSD.sc, EURUSD', ['XAUUSD.SC', 'EURUSD']),
    ('duplicate_timeout_seconds', '20', 20),
    ('broker_utc_offset_hours', '2', 2),
    ('bad_entry_partial_close_percent', '35', D('35')),
    ('bad_entry_partial_close_percent', '100', D('100')),
    ('default_partial_close_percent', '0', D('0')),
    ('repeat_partial_close_keywords', 'chốt bớt entry xấu, chốt bớt 1 phần', ['chốt bớt entry xấu', 'chốt bớt 1 phần']),
    ('repeat_partial_close_keywords', '[]', []),
])
async def test_added_settings_input_preview_and_confirm(bot, key, value, expected):
    from unittest.mock import patch
    update, context = callback()
    await bot.handle_console_callback(update, context, f'cfg_{key}')
    assert context.user_data['config_field']['key'] == key
    assert 'Hiện tại' in update.callback_query.message.reply_text.call_args.args[0]
    before = getattr(bot.service.risk_config, key)

    message = SimpleNamespace(text=value, reply_text=AsyncMock())
    typed = SimpleNamespace(effective_message=message)
    await bot.handle_text_message.__wrapped__(bot, typed, context)
    assert getattr(bot.service.risk_config, key) == before
    bot.service.save_risk_config_to_db.assert_not_awaited()
    keyboard = message.reply_text.call_args.kwargs['reply_markup']
    with patch('app.trading.service.set_broker_utc_offset_hours'):
        await bot.handle_console_callback(update, context, keyboard.inline_keyboard[0][0].callback_data)
    assert getattr(bot.service.risk_config, key) == expected
    bot.service.save_risk_config_to_db.assert_awaited_once()


@pytest.mark.asyncio
async def test_require_tp_toggle_is_only_applied_after_confirmation(bot):
    update, context = callback()
    await bot.handle_console_callback(update, context, 'cfg_require_tp')
    assert not bot.service.risk_config.require_tp
    keyboard = update.callback_query.edit_message_text.call_args.kwargs['reply_markup']
    await bot.handle_console_callback(update, context, keyboard.inline_keyboard[0][0].callback_data)
    assert bot.service.risk_config.require_tp


@pytest.mark.asyncio
async def test_invalid_min_lot_keeps_existing_config_and_allows_retry(bot):
    update, context = callback()
    await bot.handle_console_callback(update, context, 'cfg_min_lot')
    message = SimpleNamespace(text='0.02', reply_text=AsyncMock())
    await bot.handle_text_message.__wrapped__(bot, SimpleNamespace(effective_message=message), context)
    assert 'chưa hợp lệ' in message.reply_text.call_args.args[0]
    assert bot.service.risk_config.min_lot == D('.01')
    bot.service.save_risk_config_to_db.assert_not_awaited()
    assert context.user_data['config_field']['key'] == 'min_lot'
    # Returning to the menu cancels the old input so unrelated text cannot change it.
    await bot.handle_console_callback(update, context, 'cfg_group_entry')
    assert 'config_field' not in context.user_data


def closed_position(ticket, deals=0):
    from app.broker.base import ClosedPositionInfo, PartialCloseDealInfo
    from datetime import timedelta
    timestamp = datetime(2026, 10, 16, 10, tzinfo=timezone.utc)
    return ClosedPositionInfo(
        ticket=ticket, symbol='XAUUSD', position_type='BUY', volume=D('.02'),
        open_price=D('2650'), close_price=D('2660'), profit=D('20'), swap=D('-1'),
        commission=D('-1'), net_pnl=D('18'), open_time=timestamp,
        close_time=timestamp + timedelta(minutes=ticket), deals_count=max(1, deals),
        partial_deals=[PartialCloseDealInfo(i, D('.01'), D('2660'), D('10'), timestamp + timedelta(minutes=i))
                       for i in range(deals)])


@pytest.mark.asyncio
async def test_history_pages_keep_latest_near_controls_and_reach_all_records(bot):
    bot.service.get_closed_positions = AsyncMock(return_value=[closed_position(i) for i in range(20, 0, -1)])
    first, keyboard = await bot._format_history_message(30)
    assert 'Trang 1/7' in first
    assert first.index('#20') < first.index('#19') < first.index('#18')
    assert '#17' not in first and len(first.splitlines()) <= 18
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert 'history_30_1' in callbacks and 'history_30_-1' not in callbacks
    assert 'history_detail_30_0_20_0' in callbacks
    args = bot.service.get_closed_positions.call_args.kwargs
    assert args['limit'] is None
    assert (args['end_time'] - args['start_time']).days == 30
    last, keyboard = await bot._format_history_message(30, 999)
    assert 'Trang 7/7' in last and '#2' in last and '#1' in last
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert 'history_30_0' in callbacks and 'history_30_5' in callbacks
    assert 'history_30_7' not in callbacks


@pytest.mark.asyncio
async def test_history_empty_and_stale_detail_offer_way_back(bot):
    bot.service.get_closed_positions = AsyncMock(return_value=[])
    text, keyboard = await bot._format_history_message(7, -2)
    assert 'Trang 1/1' in text and 'Chưa có lệnh chốt' in text
    assert not any(b.callback_data.startswith('history_detail_') for row in keyboard.inline_keyboard for b in row)
    text, keyboard = await bot._format_history_detail(7, 2, 404)
    assert 'không còn' in text and keyboard.inline_keyboard[0][0].callback_data == 'history_7_2'


@pytest.mark.asyncio
async def test_history_detail_bounds_partial_deals_and_preserves_list_page(bot):
    bot.service.get_closed_positions = AsyncMock(return_value=[closed_position(42, deals=17)])
    text, keyboard = await bot._format_history_detail(30, 3, 42)
    assert 'Các lần chốt · 1/4' in text
    assert text.count('0.01 lot @') == 5
    assert '<code>#16</code>' not in text
    assert len(text) < 4096
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert 'history_detail_30_3_42_1' in callbacks and 'history_30_3' in callbacks
    last, keyboard = await bot._format_history_detail(30, 3, 42, 999)
    assert 'Các lần chốt · 4/4' in last and last.count('0.01 lot @') == 2
    assert 'history_detail_30_3_42_4' not in [b.callback_data for row in keyboard.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_history_detail_links_to_original_signal_through_opening_order(bot):
    from dataclasses import replace
    pos = replace(closed_position(42, deals=2), opening_order_tickets=(100,))
    bot.service.get_closed_positions = AsyncMock(return_value=[pos])
    bot.service._order_message_links[100] = 'https://t.me/signals/12'
    text, _ = await bot._format_history_detail(30, 0, 42)
    assert '<a href="https://t.me/signals/12">Xem tin nhắn signal gốc</a>' in text
    assert '<code>#0</code>' not in text and '<code>#1</code>' not in text


@pytest.mark.asyncio
async def test_history_signal_link_survives_restart_and_reconstructs_missing_url(bot):
    from contextlib import asynccontextmanager
    from unittest.mock import patch
    session = AsyncMock()
    session.execute.return_value = MagicMock()
    session.execute.return_value.all.return_value = [(None, -1001234567890, 12)]

    @asynccontextmanager
    async def scope():
        yield session

    with patch('app.config.database.get_session', scope):
        assert await bot.service.history_signal_link(closed_position(42)) == 'https://t.me/c/1234567890/12'
        session.execute.return_value.all.return_value = [
            ({'message_link': 'https://t.me/signals/12'}, None, None)]
        assert await bot.service.history_signal_link(closed_position(42)) == 'https://t.me/signals/12'
        session.execute.return_value.all.return_value.append(
            ({'message_link': 'https://t.me/signals/13'}, None, None))
        assert await bot.service.history_signal_link(closed_position(42)) is None


@pytest.mark.asyncio
async def test_history_callbacks_support_new_pages_and_old_filter_buttons(bot):
    bot.service.get_closed_positions = AsyncMock(return_value=[closed_position(42)])
    update, context = callback()
    update.callback_query.answer = AsyncMock()
    for data, expected in [('history_30_2', 'Lịch sử lệnh'),
                           ('history_7', 'Lịch sử lệnh'),
                           ('history_detail_30_2_42_0', 'Chi tiết lệnh')]:
        update.callback_query.data = data
        await bot.handle_callback.__wrapped__(bot, update, context)
        assert expected in update.callback_query.edit_message_text.call_args.args[0]


def test_compact_cards_escape_broker_text_and_keep_precise_prices():
    from app.telegram.presentation import position_card, join_cards
    pos = position(1)
    pos.symbol = 'XAU<&>'
    pos.open_price = D('1.12345')
    card = position_card(pos, 'USD')
    assert 'XAU&lt;&amp;&gt;' in card and '1.12345' in card
    assert len(card.splitlines()) == 4 and '\n\n' not in card
    assert join_cards(['A', '', 'B']) == 'A\n\nB'
