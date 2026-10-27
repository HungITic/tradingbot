"""Management messages without replies must touch only the latest exact source signal."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal as D
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import PositionInfo, OrderResult, SymbolInfo
from app.signal.parser import SignalParser
from app.trading.service import TradingService
from app.trading.risk import RiskConfig

CHAT = -1001234567890


@pytest.fixture
def scoped(monkeypatch):
    session = AsyncMock()
    selected = MagicMock()
    selected.first.return_value = (20, 200, 'XAUUSD')
    session.execute.return_value = selected
    tickets = MagicMock()
    tickets.all.return_value = [22]
    session.scalars.return_value = tickets
    session.add = MagicMock()
    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    broker = AsyncMock()
    broker.get_pending_orders.return_value = []
    broker.get_symbol_info.return_value = SymbolInfo('XAUUSD', 2, D('.01'), D('.01'), D('100'), D('.01'), D('100'))
    broker.close_position.return_value = OrderResult(True)
    svc = TradingService(broker, risk_config=RiskConfig(auto_breakeven_on_partial=False))
    def pos(ticket):
        return PositionInfo(ticket, 'XAUUSD', D('.10'), D('2650'), D('2670'), D('2640'), D('2690'),
                            D('1'), D('0'), D('0'), 'BUY', datetime.now(timezone.utc))
    positions = [pos(11), pos(22)]
    svc.get_open_positions = AsyncMock(return_value=positions)
    svc.position_orders = AsyncMock(return_value={})
    svc.management_history = AsyncMock(return_value=(set(), set()))
    svc._insert_management_action = AsyncMock()
    svc.cancel_dual_entry_pending_orders_on_close = AsyncMock()
    svc.set_signal_status = AsyncMock()
    svc.notify = AsyncMock()
    return svc, session, selected, tickets, positions


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['chốt bớt', 'XAUUSD chốt bớt', 'đóng lệnh'])
async def test_no_reply_targets_only_latest_signal_even_with_multiple_same_symbol_positions(scoped, text):
    svc, session, _, _, _ = scoped
    await svc.process_management_signal(SignalParser().parse(text), 30, None, None, chat_id=CHAT, message_id=300)
    svc.broker.close_position.assert_awaited_once_with(22, D('.10') if text == 'đóng lệnh' else D('.05'))
    stmt = session.execute.call_args.args[0]
    sql, params = str(stmt), stmt.compile().params
    assert 'signal_sources.chat_id' in sql and CHAT in params.values()
    assert 'signals.telegram_message_id <' in sql and 300 in params.values()
    assert 'signals.telegram_message_id DESC' in sql
    assert session.add.call_args.args[0].details['signal_id'] == 20


@pytest.mark.asyncio
async def test_latest_signal_closed_does_not_fall_back_to_older_live_position(scoped):
    svc, _, _, _, positions = scoped
    svc.get_open_positions.return_value = positions[:1]
    await svc.process_management_signal(SignalParser().parse('chốt bớt'), 30, None, None, chat_id=CHAT, message_id=300)
    svc.broker.close_position.assert_not_awaited()
    assert svc.set_signal_status.call_args.args[1] == 'SKIPPED'


@pytest.mark.asyncio
async def test_symbol_mismatch_does_not_select_an_older_matching_setup(scoped):
    svc, _, selected, _, _ = scoped
    selected.first.return_value = (20, 200, 'EURUSD')
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt'), 30, None, None, chat_id=CHAT, message_id=300)
    svc.broker.close_position.assert_not_awaited()
    assert 'khác signal' in svc.notify.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('missing', ['signal', 'orders'])
async def test_missing_proven_scope_never_closes_arbitrary_positions(scoped, missing):
    svc, _, selected, tickets, _ = scoped
    if missing == 'signal':
        selected.first.return_value = None
    else:
        tickets.all.return_value = []
    await svc.process_management_signal(SignalParser().parse('chốt bớt'), 30, None, None, chat_id=CHAT, message_id=300)
    svc.broker.close_position.assert_not_awaited()
    assert svc.set_signal_status.call_args.args[1] == 'REJECTED'


@pytest.mark.asyncio
async def test_explicit_reply_wins_over_latest_and_is_scoped_to_chat(scoped):
    svc, session, _, _, _ = scoped
    svc.get_tickets_for_reply_message = AsyncMock(return_value=[11])
    await svc.process_management_signal(SignalParser().parse('chốt bớt'), 30, 100, None, chat_id=CHAT, message_id=300)
    svc.get_tickets_for_reply_message.assert_awaited_once_with(100, chat_id=CHAT)
    svc.broker.close_position.assert_awaited_once_with(11, D('.05'))
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_pins_scope_for_confirm_without_writing_or_trading(scoped):
    svc, session, _, _, _ = scoped
    preview = await svc.preview_signal('chốt bớt', chat_id=CHAT, message_id=300)
    assert preview['can_execute']
    assert preview['management_scope'] == {'signal_id': 20, 'message_id': 200, 'chat_id': CHAT, 'method': 'latest_signal'}
    assert 'signal mở lệnh gần nhất' in preview['text']
    session.add.assert_not_called()
    svc.broker.close_position.assert_not_awaited()
    await svc.process_management_signal(SignalParser().parse('chốt bớt'), 30, None, None,
        chat_id=CHAT, message_id=300, management_scope=preview['management_scope'])
    stmt = session.execute.call_args.args[0]
    assert 'signals.id =' in str(stmt) and 'signals.telegram_message_id =' in str(stmt)
    assert 20 in stmt.compile().params.values()
    svc.broker.close_position.assert_awaited_once_with(22, D('.05'))


@pytest.mark.asyncio
async def test_explicit_all_keeps_account_scope(scoped):
    svc, session, _, _, _ = scoped
    await svc.process_management_signal(SignalParser().parse('tất cả chốt bớt'), 30, None, None, chat_id=CHAT, message_id=300)
    assert svc.broker.close_position.await_count == 2
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_wrong_chat_pin_is_rejected(scoped):
    svc, _, _, _, _ = scoped
    preview_scope = {'signal_id': 20, 'message_id': 200, 'chat_id': CHAT - 1}
    await svc.process_management_signal(SignalParser().parse('chốt bớt'), 30, None, None,
        chat_id=CHAT, message_id=300, management_scope=preview_scope)
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_sl_action_targets_only_latest_setup(scoped):
    svc, _, _, _, positions = scoped
    svc.apply_stop = AsyncMock(return_value=OrderResult(True, price=D('2655')))
    await svc.process_management_signal(SignalParser().parse('dời SL về 2655'), 30, None, None, chat_id=CHAT, message_id=300)
    svc.apply_stop.assert_awaited_once_with(positions[1], D('2655'), protect=False, per_member_entry=False)
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_audit_failure_blocks_mutation(scoped):
    svc, _, _, _, _ = scoped
    svc.audit_management_scope = AsyncMock(side_effect=RuntimeError('database offline'))
    await svc.process_management_signal(SignalParser().parse('chốt bớt'), 30, None, None, chat_id=CHAT, message_id=300)
    svc.broker.close_position.assert_not_awaited()
    svc.broker.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_latest_pending_only_setup_can_be_previewed_and_cancelled(scoped):
    from app.broker.base import PendingOrderInfo
    svc, _, _, _, positions = scoped
    svc.get_open_positions.return_value = positions[:1]  # Older setup is still open.
    pending = PendingOrderInfo(22, 'XAUUSD', 'BUY_LIMIT', D('.1'), D('2645'), D('2640'), D('2670'),
                               'bot-max-entry', datetime.now(timezone.utc))
    unrelated = PendingOrderInfo(11, 'XAUUSD', 'BUY_LIMIT', D('.1'), D('2645'), D('2640'), D('2670'),
                                 'bot-max-entry', datetime.now(timezone.utc))
    svc.broker.get_pending_orders.return_value = [unrelated, pending]
    svc.broker.cancel_order.return_value = OrderResult(True)
    preview = await svc.preview_signal('đóng lệnh', chat_id=CHAT, message_id=300)
    assert preview['can_execute']
    await svc.process_management_signal(SignalParser().parse('đóng lệnh'), 30, None, None,
        chat_id=CHAT, message_id=300, management_scope=preview['management_scope'])
    svc.broker.close_position.assert_not_awaited()
    svc.broker.cancel_order.assert_awaited_once_with(22)


@pytest.mark.asyncio
async def test_replay_confirmation_payload_pins_latest_setup(scoped):
    from types import SimpleNamespace as NS
    from unittest.mock import patch
    from app.telegram.personal_bot import PersonalTelegramBot
    svc, _, _, _, _ = scoped
    bot = PersonalTelegramBot(svc)
    status = NS(edit_text=AsyncMock())
    update = NS(effective_message=NS(reply_text=AsyncMock(return_value=status)))
    fetched = ('chốt bớt', 300, CHAT, 'https://t.me/testsignals/300', False, None, datetime.now(timezone.utc))
    with patch('app.telegram.signal_listener.signal_listener.fetch_message_by_link', AsyncMock(return_value=fetched)):
        await bot._execute_replay_link(update, NS(user_data={}), fetched[3])
    payload = next(iter(bot._drafts.values()))['payload']
    assert payload['management_scope']['signal_id'] == 20
    assert payload['management_scope']['message_id'] == 200
    assert payload['skip_signal_expiry_check']
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_reply_cache_does_not_leak_between_chats(scoped):
    svc, session, _, tickets, _ = scoped
    svc._signal_msg_id_to_tickets[100] = [11]  # Legacy cache, unknown source chat.
    assert await svc.get_tickets_for_reply_message(100, chat_id=CHAT) == [22]
    stmt = session.scalars.call_args.args[0]
    assert 'signal_sources.chat_id' in str(stmt) and CHAT in stmt.compile().params.values()
    tickets.all.return_value = [33]
    assert await svc.get_tickets_for_reply_message(100, chat_id=CHAT - 1) == [33]
    assert await svc.get_tickets_for_reply_message(100, chat_id=CHAT) == [22]
