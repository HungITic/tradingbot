"""Exact chat-scoped re-entry ancestry; no ancestor is submitted for execution."""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.signal.models import MessageType
from app.telegram.signal_listener import TelegramSignalListener
from app.telegram.reply_chain import MAX_REPLY_HOPS, ReplyChainError
from app.trading.risk import RiskConfig
from app.trading.service import TradingService

CHAT = -1001234567890
ROOT = 'BUY XAUUSD 2650\nSL: 2640\nTP: 2670'
NOW = datetime.now(timezone.utc)


def msg(number, text, parent=None, chat=CHAT):
    return NS(id=number, raw_text=text, chat_id=chat, date=NOW,
              reply_to_msg_id=parent.id if parent else None, reply_to=None, is_reply=parent is not None,
              get_reply_message=AsyncMock(return_value=parent), get_input_chat=AsyncMock(return_value=chat),
              get_chat=AsyncMock(return_value=NS(username='testsignals')))


@pytest.fixture
def listener():
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    service.process_raw_message = AsyncMock()
    service.notify = AsyncMock()
    listener = TelegramSignalListener(service)
    listener.load_reentry_origin = AsyncMock(return_value=None)
    listener.client = MagicMock()
    listener.client.get_messages = AsyncMock()
    listener.client.is_connected.return_value = True
    listener.client.is_user_authorized = AsyncMock(return_value=True)
    return listener


@pytest.mark.asyncio
@pytest.mark.parametrize('middle_count', [0, 1, 12, 98])
async def test_walks_direct_or_many_replies_and_retains_current_command(listener, middle_count):
    root = msg(1, ROOT)
    root.date = NOW - timedelta(days=30)
    current = root
    for number in range(2, middle_count + 2):
        current = msg(number, 'Kèo đang chạy', current)
    command = msg(middle_count + 2, 'vòng 2', current)
    text, reentry, parent, origin = await listener.resolve_reentry(command)
    assert text == ROOT + '\n\n[REENTRY COMMAND]: vòng 2'
    assert reentry and parent == current.id
    assert origin['root_message_id'] == 1 and origin['chat_id'] == CHAT
    assert origin['message_path'] == list(range(command.id, 0, -1))
    assert listener.service.parser.parse(text).message_type == MessageType.NEW_ORDER
    listener.service.process_raw_message.assert_not_awaited()
    listener.service.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_reentry_management_message_does_not_walk_reply_chain(listener):
    current = msg(3, 'chốt bớt entry xấu', msg(2, 'cập nhật', msg(1, ROOT)))
    assert await listener.resolve_reentry(current) == ('chốt bớt entry xấu', False, 2, None)
    current.get_reply_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_negated_reentry_is_not_turned_into_opening_order(listener):
    command = msg(2, 'đừng vào lại', msg(1, ROOT))
    text, reentry, _, origin = await listener.resolve_reentry(command)
    assert text == command.raw_text and not reentry and origin is None
    command.get_reply_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['deleted', 'other_chat', 'wrong_id', 'no_signal', 'cycle', 'no_reply', 'unreadable'])
async def test_broken_or_ambiguous_chain_never_executes(listener, failure):
    root = msg(1, ROOT)
    command = msg(3, 'vòng 2', msg(2, 'cập nhật', root))
    if failure == 'deleted':
        command.get_reply_message.return_value = None
    elif failure == 'other_chat':
        command.get_reply_message.return_value.chat_id = CHAT - 1
    elif failure == 'wrong_id':
        command.reply_to_msg_id = 99
    elif failure == 'no_signal':
        command.get_reply_message.return_value = msg(2, 'Kèo đã đóng')
    elif failure == 'cycle':
        command.get_reply_message.return_value.get_reply_message.return_value = command
        command.get_reply_message.return_value.reply_to_msg_id = command.id
    elif failure == 'no_reply':
        command.is_reply = False
    else:
        command.get_reply_message.side_effect = RuntimeError('offline')
    with pytest.raises(ReplyChainError):
        await listener.resolve_reentry(command)
    listener.service.process_raw_message.assert_not_awaited()
    listener.service.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_depth_limit(listener):
    current = msg(1, ROOT)
    for number in range(2, MAX_REPLY_HOPS + 2):
        current = msg(number, 'cập nhật', current)
    with pytest.raises(ReplyChainError, match='100 bước'):
        await listener.resolve_reentry(msg(MAX_REPLY_HOPS + 2, 'vòng 2', current))


@pytest.mark.asyncio
async def test_invalid_opening_signal_stops_instead_of_selecting_older_signal(listener):
    invalid = msg(2, 'BUY XAUUSD 2650\nSL: 2660\nTP: 2670', msg(1, ROOT))
    with pytest.raises(ReplyChainError, match='không hợp lệ'):
        await listener.resolve_reentry(msg(3, 'vòng 2', invalid))


@pytest.mark.asyncio
async def test_fetch_replay_uses_same_chain_and_current_timestamp(listener):
    root = msg(1, ROOT)
    root.date = NOW - timedelta(days=30)
    current = msg(3, 'vòng 2', msg(2, 'cập nhật', root))
    listener.client.get_messages.return_value = current
    result = await listener.fetch_message_by_link('https://t.me/testsignals/3')
    assert result[:3] == (ROOT + '\n\n[REENTRY COMMAND]: vòng 2', 3, CHAT)
    assert result[4:7] == (True, 2, NOW)
    assert result[7]['message_path'] == [3, 2, 1]
    listener.service.process_raw_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_origin_after_restart_is_scoped_and_revalidates_root(listener):
    root = msg(1, ROOT)
    prior = msg(3, 'vòng 2', msg(2, 'cập nhật', root))
    origin = (await listener.resolve_reentry(prior))[3]
    # A new listener has no in-memory state and must use the DB audit.
    listener = TelegramSignalListener(listener.service)
    listener.client = MagicMock()
    listener.client.get_messages = AsyncMock(return_value=root)
    session = AsyncMock()
    session.scalar.return_value = {'reentry_origin': origin}
    statements = []
    async def read(stmt):
        statements.append(str(stmt))
        return {'reentry_origin': origin} if len(statements) == 2 else None
    session.scalar.side_effect = read
    @asynccontextmanager
    async def db():
        yield session
    latest = msg(4, 'vào lại', prior)
    with patch('app.config.database.get_session', db):
        resolved = await listener.resolve_reentry(latest)
    assert resolved[3]['message_path'] == [4, 3, 2, 1]
    assert all('signal_sources.chat_id' in s and 'signals.telegram_message_id' in s for s in statements)
    prior.get_reply_message.assert_awaited_once()  # Only the earlier resolution, no full traversal after restart.
    listener.client.get_messages.assert_awaited_once_with(CHAT, ids=1)


@pytest.mark.asyncio
async def test_live_listener_routes_only_new_command(listener):
    root = msg(1, ROOT)
    root.date = NOW - timedelta(days=30)
    current = msg(3, 'vòng 2', msg(2, 'cập nhật', root))
    handlers = []
    def on(event_filter):
        def register(fn):
            handlers.append(fn)
            return fn
        return register
    listener.client.on.side_effect = on
    listener.client.connect = AsyncMock()
    listener.client.get_me = AsyncMock(return_value=NS(id=1, username='test'))
    await listener.start()
    event = NS(message=current, raw_text=current.raw_text, id=3, chat_id=CHAT,
               get_chat=current.get_chat)
    await handlers[0](event)
    listener.service.process_raw_message.assert_awaited_once()
    args, kwargs = listener.service.process_raw_message.call_args
    assert args[0] == ROOT + '\n\n[REENTRY COMMAND]: vòng 2'
    assert kwargs['message_id'] == 3 and kwargs['signal_time'] == NOW
    assert kwargs['reply_to_msg_id'] == 2 and kwargs['reentry_origin']['root_message_id'] == 1


@pytest.mark.asyncio
async def test_replay_provenance_stays_in_preview_draft_until_confirmation(listener):
    from app.telegram.personal_bot import PersonalTelegramBot
    service = MagicMock(spec=TradingService)
    service.preview_signal = AsyncMock(return_value={'text': 'Xem trước', 'can_execute': True})
    service.process_raw_message = AsyncMock()
    bot = PersonalTelegramBot(service)
    root = msg(1, ROOT)
    current = msg(3, 'vòng 2', msg(2, 'cập nhật', root))
    resolved = await listener.resolve_reentry(current)
    fetched = (resolved[0], 3, CHAT, 'https://t.me/testsignals/3', True, 2, NOW, resolved[3])
    status = NS(edit_text=AsyncMock())
    update = NS(effective_message=NS(reply_text=AsyncMock(return_value=status)))
    with patch('app.telegram.signal_listener.signal_listener.fetch_message_by_link', AsyncMock(return_value=fetched)):
        await bot._execute_replay_link(update, NS(user_data={}), 'https://t.me/testsignals/3')
    payload = next(iter(bot._drafts.values()))['payload']
    assert payload['reentry_origin']['message_path'] == [3, 2, 1]
    assert payload['signal_time'] == NOW and payload['skip_signal_expiry_check']
    service.preview_signal.assert_awaited_once_with(**payload)
    service.process_raw_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_buy_status_is_not_mistaken_for_new_signal(listener):
    status = msg(2, 'BUY XAUUSD 2650 đang chạy', msg(1, ROOT))
    result = await listener.resolve_reentry(msg(3, 'vòng 2', status))
    assert result[3]['root_message_id'] == 1
    assert result[3]['message_path'] == [3, 2, 1]


@pytest.mark.asyncio
async def test_cached_origin_does_not_allow_deleted_root(listener):
    root = msg(1, ROOT)
    command = msg(3, 'vòng 2', msg(2, 'cập nhật', root))
    origin = (await listener.resolve_reentry(command))[3]
    listener.load_reentry_origin.return_value = origin
    listener.client.get_messages.return_value = None
    with pytest.raises(ReplyChainError, match='signal gốc'):
        await listener.resolve_reentry(command)
    listener.service.process_raw_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cached_origin_from_other_chat_is_ignored(listener):
    root = msg(1, ROOT)
    command = msg(3, 'vòng 2', msg(2, 'cập nhật', root))
    origin = (await listener.resolve_reentry(command))[3]
    origin['chat_id'] = CHAT - 1
    listener.load_reentry_origin.return_value = origin
    result = await listener.resolve_reentry(command)
    assert result[3]['chat_id'] == CHAT and result[3]['message_path'] == [3, 2, 1]
    listener.client.get_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_reentry_missing_header_keeps_direct_reply_fallback(listener):
    current = msg(2, 'chốt bớt', msg(1, ROOT))
    current.reply_to_msg_id = None
    result = await listener.resolve_reentry(current)
    assert result == ('chốt bớt', False, 1, None)
    current.get_reply_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_management_action_resolves_chain_for_limit_invalidation(listener):
    current = msg(3, 'chốt bớt entry xấu', msg(2, 'cập nhật', msg(1, ROOT)))
    assert await listener.related_action_path(current) == [3, 2, 1]
    listener.service.broker.cancel_order.assert_not_awaited()
