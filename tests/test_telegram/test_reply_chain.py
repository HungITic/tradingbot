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
    listener.latest_reentry_signal = AsyncMock(return_value=None)
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
    listener.latest_reentry_signal.assert_not_awaited()


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
        command.reply_to_msg_id = None
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


async def latest_origin(listener, number=3):
    root, command = msg(1, ROOT), msg(number, 'vòng 2')
    root.date = NOW-timedelta(days=30)
    listener.latest_reentry_signal.return_value = (10, 1)
    listener.client.get_messages.return_value = root
    result = await listener.resolve_reentry(command)
    return root, command, result


@pytest.mark.asyncio
async def test_no_reply_round_uses_latest_recorded_signal_without_checking_open_positions(listener):
    root, command, result = await latest_origin(listener)
    text, reentry, parent, origin = result
    assert text == ROOT+'\n\n[REENTRY COMMAND]: vòng 2' and reentry and parent is None
    assert origin['method'] == 'latest_signal' and origin['message_path'] == [3, 1]
    assert origin['selected_signal_id'] == 10 and origin['selected_message_id'] == 1
    assert origin['root_text'] == root.raw_text
    listener.latest_reentry_signal.assert_awaited_once_with(CHAT, 3)
    command.get_reply_message.assert_not_awaited()
    listener.service.broker.get_positions.assert_not_awaited()
    listener.service.process_raw_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetch_no_reply_replay_keeps_command_date_instead_of_old_root_date(listener):
    root, command, _ = await latest_origin(listener)
    listener.client.get_messages.side_effect = lambda entity, ids: command if ids == command.id else root
    result = await listener.fetch_message_by_link('https://t.me/testsignals/3')
    assert result[4:7] == (True, None, NOW)
    assert result[7]['method'] == 'latest_signal' and result[7]['root_message_id'] == root.id


@pytest.mark.asyncio
async def test_reply_to_previous_no_reply_round_reuses_stored_origin(listener):
    root, prior, resolved = await latest_origin(listener)
    listener.load_reentry_origin.side_effect = lambda chat, mid: resolved[3] if mid == prior.id else None
    listener.client.get_messages.return_value = root
    current = msg(4, 'vào lại', prior)
    result = await listener.resolve_reentry(current)
    assert result[3]['root_message_id'] == 1 and result[3]['message_path'] == [4, 3, 1]
    assert result[2] == prior.id


@pytest.mark.asyncio
async def test_latest_lookup_is_scoped_by_chat_and_message_order_without_status_filter(listener):
    from app.telegram.reply_chain import ReplyChainMixin
    session = AsyncMock()
    result = MagicMock()
    result.first.return_value = (10, 1)
    session.execute.return_value = result
    @asynccontextmanager
    async def db():
        yield session
    with patch('app.config.database.get_session', db):
        assert await ReplyChainMixin.latest_reentry_signal(listener, CHAT, 3) == (10, 1)
    stmt = session.execute.call_args.args[0]
    sql, params = str(stmt), stmt.compile().params
    assert 'signal_sources.chat_id' in sql and CHAT in params.values()
    assert 'signals.telegram_message_id <' in sql and 3 in params.values()
    assert 'ORDER BY signals.telegram_message_id DESC, signals.id DESC' in sql
    assert 'orders' not in sql and 'status' not in sql


@pytest.mark.asyncio
async def test_latest_can_be_a_previous_no_reply_round_after_restart(listener):
    root, prior, resolved = await latest_origin(listener)
    prior_origin = resolved[3]
    listener.latest_reentry_signal.return_value = (20, prior.id)
    listener.load_reentry_origin.side_effect = lambda chat, mid: prior_origin if mid == prior.id else None
    listener.client.get_messages.side_effect = lambda entity, ids: prior if ids == prior.id else root
    result = await listener.resolve_reentry(msg(4, 'vào lại'))
    assert result[3]['message_path'] == [4, 3, 1]
    assert result[3]['selected_message_id'] == 3 and result[3]['root_message_id'] == 1
    assert result[2] is None


@pytest.mark.asyncio
async def test_latest_can_be_a_previous_reply_round_without_cached_origin(listener):
    root = msg(1, ROOT)
    prior = msg(3, 'vòng 2', root)
    listener.latest_reentry_signal.return_value = (20, 3)
    listener.client.get_messages.return_value = prior
    result = await listener.resolve_reentry(msg(4, 'vào lại'))
    assert result[3]['message_path'] == [4, 3, 1] and result[3]['root_text'] == ROOT


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['none', 'database', 'deleted', 'wrong_chat', 'wrong_id',
    'invalid', 'incomplete', 'unreadable', 'prior_without_origin', 'future', 'symbol'])
async def test_no_reply_invalid_latest_source_never_falls_through_to_older_setup(listener, failure):
    root = msg(1, ROOT)
    listener.latest_reentry_signal.return_value = (10, 1)
    listener.client.get_messages.return_value = root
    command = msg(3, 'vòng 2')
    if failure == 'none':
        listener.latest_reentry_signal.return_value = None
    elif failure == 'database':
        listener.latest_reentry_signal.side_effect = RuntimeError('DB offline')
    elif failure == 'deleted':
        listener.client.get_messages.return_value = None
    elif failure == 'wrong_chat':
        root.chat_id = CHAT-1
    elif failure == 'wrong_id':
        root.id = 2
    elif failure == 'invalid':
        root.raw_text = 'BUY XAUUSD 2650\nSL: 2660\nTP: 2670'
    elif failure == 'incomplete':
        root.raw_text = 'BUY XAUUSD 2650 đang chạy'
    elif failure == 'unreadable':
        listener.client.get_messages.side_effect = RuntimeError('Telegram offline')
    elif failure == 'prior_without_origin':
        root.raw_text = 'vòng 2'
    elif failure == 'future':
        listener.latest_reentry_signal.return_value = (10, 4)
    else:
        command.raw_text = 'EURUSD vào lại'
    with pytest.raises(ReplyChainError):
        await listener.resolve_reentry(command)
    assert listener.latest_reentry_signal.await_count == 1
    listener.service.process_raw_message.assert_not_awaited()
    listener.service.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_reply_header_takes_priority_even_if_is_reply_flag_is_missing(listener):
    command = msg(3, 'vòng 2', msg(1, ROOT))
    command.is_reply = False
    result = await listener.resolve_reentry(command)
    assert result[2] == 1 and result[3]['root_message_id'] == 1
    listener.latest_reentry_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_negated_no_reply_round_does_not_lookup_or_manufacture_order(listener):
    result = await listener.resolve_reentry(msg(3, 'đừng vào lại'))
    assert not result[1] and result[3] is None
    listener.latest_reentry_signal.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_confirmation_revalidates_pinned_source_without_choosing_a_newer_signal(listener):
    root, command, result = await latest_origin(listener)
    payload = {'raw_text': result[0], 'chat_id': CHAT, 'message_id': 3, 'is_reentry': True, 'reentry_origin': result[3]}
    listener.latest_reentry_signal.reset_mock()
    listener.latest_reentry_signal.return_value = (20, 2)
    listener.client.get_messages.return_value = [command, root]
    await listener.validate_latest_reentry_payload(payload)
    listener.latest_reentry_signal.assert_not_awaited()
    assert payload['reentry_origin']['root_message_id'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['root_edit', 'root_deleted', 'command_edit', 'other_chat'])
async def test_replay_confirmation_rejects_changed_or_deleted_pinned_messages(listener, failure):
    root, command, result = await latest_origin(listener)
    payload = {'raw_text': result[0], 'chat_id': CHAT, 'message_id': 3, 'is_reentry': True, 'reentry_origin': result[3]}
    if failure == 'root_edit':
        root.raw_text = ROOT.replace('2650', '2651')
    elif failure == 'root_deleted':
        root = None
    elif failure == 'command_edit':
        command.raw_text = 'đừng vào lại'
    else:
        root.chat_id = CHAT-1
    listener.client.get_messages.return_value = [command, root]
    with pytest.raises(ReplyChainError):
        await listener.validate_latest_reentry_payload(payload)
    listener.service.process_raw_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [False, True])
async def test_no_reply_replay_draft_confirms_only_original_selection(listener, changed):
    from app.telegram.personal_bot import PersonalTelegramBot
    root, command, result = await latest_origin(listener)
    service = listener.service
    service.preview_signal = AsyncMock(return_value={'text': 'Xem trước', 'can_execute': True})
    bot = PersonalTelegramBot(service)
    fetched = (result[0], 3, CHAT, 'https://t.me/testsignals/3', True, None, NOW, result[3])
    status = NS(edit_text=AsyncMock())
    update = NS(effective_message=NS(reply_text=AsyncMock(return_value=status)))
    with patch('app.telegram.signal_listener.signal_listener.fetch_message_by_link', AsyncMock(return_value=fetched)):
        await bot._execute_replay_link(update, NS(user_data={}), 'https://t.me/testsignals/3')
    token = next(iter(bot._drafts))
    payload = bot._drafts[token]['payload']
    assert payload['signal_time'] == NOW and payload['reentry_origin']['selected_message_id'] == 1
    if changed:
        root.raw_text = ROOT.replace('2650', '2651')
    listener.client.get_messages.return_value = [command, root]
    callback = NS(callback_query=NS(edit_message_text=AsyncMock()))
    with patch('app.telegram.signal_listener.signal_listener.validate_latest_reentry_payload', listener.validate_latest_reentry_payload):
        await bot.handle_console_callback(callback, NS(user_data={}), f'draft_ok_{token}')
    if changed:
        service.process_raw_message.assert_not_awaited()
    else:
        service.process_raw_message.assert_awaited_once_with(**payload)


@pytest.mark.asyncio
async def test_live_ingress_records_previous_signal_before_latest_fallback_lookup(listener):
    import asyncio
    handlers, recorded = [], []
    def on(event_filter):
        def register(fn):
            handlers.append(fn)
            return fn
        return register
    listener.client.on.side_effect = on
    listener.client.connect = AsyncMock()
    listener.client.get_me = AsyncMock(return_value=NS(id=1, username='test'))
    await listener.start()
    root, command = msg(1, ROOT), msg(3, 'vòng 2')
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow_chat():
        entered.set()
        await release.wait()
        return NS(username='testsignals')
    root.get_chat.side_effect = slow_chat
    async def process(text, **kwargs):
        recorded.append(kwargs['message_id'])
    listener.service.process_raw_message.side_effect = process
    async def latest(chat, mid):
        assert recorded == [1]
        return (10, 1)
    listener.latest_reentry_signal.side_effect = latest
    listener.client.get_messages.return_value = root
    def event(message):
        return NS(message=message, raw_text=message.raw_text, id=message.id, chat_id=CHAT, get_chat=message.get_chat)
    first = asyncio.create_task(handlers[0](event(root)))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(handlers[0](event(command)))
    await asyncio.sleep(0)
    listener.latest_reentry_signal.assert_not_awaited()
    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 1)
    assert recorded == [1, 3]
    assert listener.service.process_raw_message.await_args.kwargs['reentry_origin']['method'] == 'latest_signal'
