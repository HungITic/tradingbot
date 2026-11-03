"""Telegram latency, retries and lifecycle never enter the MT5 execution lane."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter

from app.models.notification import TelegramNotification
from app.telegram.notifications import NotificationClaim, NotificationStore, NotificationWorker
from app.telegram.personal_bot import PersonalTelegramBot
from app.trading.service import TradingService


def claim(number=1, **kwargs):
    values = dict(id=number, chat_id=42, text=f'notice {number}', reply_markup=None,
                  token=f'token{number}', attempts=1)
    values.update(kwargs)
    return NotificationClaim(**values)


def store_with(*claims):
    pending = list(claims)
    return NS(enqueue=AsyncMock(), cleanup=AsyncMock(), finish=AsyncMock(),
              claim=AsyncMock(side_effect=lambda: pending.pop(0) if pending else None))


@pytest.mark.asyncio
async def test_enqueue_commits_only_and_never_calls_sender():
    store, sender = store_with(), AsyncMock()
    worker = NotificationWorker(store, sender)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton('Approve', callback_data='approve_1')]])
    await worker.enqueue('hello', markup)
    store.enqueue.assert_awaited_once_with('hello', markup)
    sender.assert_not_awaited()
    store.enqueue.side_effect = RuntimeError('DB unavailable')
    with pytest.raises(RuntimeError, match='DB unavailable'):
        await worker.enqueue('not durable')
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_sender_runs_fifo_and_finishes_before_next_send():
    store, events = store_with(claim(1), claim(2)), []
    async def send(item):
        events.append(item.id)
        if item.id == 2:
            assert store.finish.await_count == 1
        return item.id + 100
    worker = NotificationWorker(store, send)
    worker.start()
    await worker.stop()
    assert events == [1, 2]
    assert store.finish.await_args_list[1].kwargs == {'message_id': 102}


@pytest.mark.asyncio
async def test_slow_telegram_does_not_hold_execution_lock_or_delay_next_mt5_step():
    entered, release = asyncio.Event(), asyncio.Event()
    store = store_with(claim())
    async def blocked_send(item):
        entered.set()
        await release.wait()
        return 100
    worker = NotificationWorker(store, blocked_send)
    bot = PersonalTelegramBot(TradingService(broker=AsyncMock()))
    bot._notification_worker = worker
    bot.service.set_notification_callback(bot.send_notification)
    worker.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async def execute():
            async with bot.service._execution_lock:
                await bot.service.broker.close_position(1, '.05')
                await bot.service.notify('Position closed')
                await bot.service.broker.modify_position(1, sl=2650)
        await asyncio.wait_for(execute(), 1)
        assert not release.is_set() and not bot.service._execution_lock.locked()
        bot.service.broker.modify_position.assert_awaited_once_with(1, sl=2650)
        store.enqueue.assert_awaited_once_with('Position closed', None)
    finally:
        release.set()
        await worker.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('error,delay', [(NetworkError('offline'), 2), (TimeoutError(), 2),
    (RetryAfter(7), 7), (RetryAfter(timedelta(seconds=8)), 8),
    (BadRequest('malformed HTML'), None), (Forbidden('blocked'), None)])
async def test_delivery_failure_persists_retry_or_terminal_error(error, delay):
    store = store_with()
    worker = NotificationWorker(store, AsyncMock(side_effect=error))
    item = claim()
    await worker._deliver(item)
    assert store.finish.await_args.args == (item,)
    assert store.finish.await_args.kwargs['retry_delay'] == delay
    assert type(error).__name__ in store.finish.await_args.kwargs['error']


@pytest.mark.asyncio
async def test_send_timeout_is_bounded_and_saved_for_retry():
    store = store_with()
    async def hung_send(item):
        await asyncio.Event().wait()
    worker = NotificationWorker(store, hung_send, send_timeout=.01)
    await asyncio.wait_for(worker._deliver(claim()), 1)
    assert store.finish.await_args.kwargs['retry_delay'] == 2


@pytest.mark.asyncio
async def test_shutdown_cancels_slow_sender_without_acknowledging_inflight_row():
    entered, cancelled = asyncio.Event(), asyncio.Event()
    store = store_with(claim())
    async def blocked_send(item):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    worker = NotificationWorker(store, blocked_send)
    worker.start()
    await asyncio.wait_for(entered.wait(), 1)
    task = worker._task
    await worker.stop(grace_seconds=.01)
    assert task.done() and worker._task is None and cancelled.is_set()
    store.finish.assert_not_awaited()


@pytest.mark.asyncio
async def test_finish_commit_failure_does_not_mark_delivery_success_or_send_again_immediately():
    store = store_with(claim())
    store.finish.side_effect = RuntimeError('commit failed')
    sender = AsyncMock(return_value=100)
    worker = NotificationWorker(store, sender)
    worker.start()
    await worker.stop()
    sender.assert_awaited_once()
    # The lease remains SENDING in DB; store.claim controls when it can be retried.
    assert store.finish.await_count == 1


@pytest.mark.asyncio
async def test_worker_recovers_after_database_failure():
    store = store_with()
    error_seen = asyncio.Event()
    attempts = 0
    async def fetch():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            error_seen.set()
            raise RuntimeError('DB offline')
        return claim() if attempts == 2 else None
    store.claim.side_effect = fetch
    sender = AsyncMock(return_value=100)
    worker = NotificationWorker(store, sender, poll_interval=.01)
    worker.start()
    await asyncio.wait_for(error_seen.wait(), 1)
    worker._wake.set()
    await worker.stop()
    sender.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbox_serializes_buttons_and_scopes_to_bot_without_persisting_token(monkeypatch):
    session = AsyncMock()
    session.add = MagicMock()
    committed = []
    @asynccontextmanager
    async def db():
        yield session
        committed.append(True)
    monkeypatch.setattr('app.config.database.get_session', db)
    store = NotificationStore('private-bot-token', 42)
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton('Approve', callback_data='approve_1')]])
    await store.enqueue('Hello <b>world</b>', buttons)
    row = session.add.call_args.args[0]
    assert isinstance(row, TelegramNotification) and committed
    assert row.reply_markup == buttons.to_dict() and row.chat_id == 42
    assert 'private-bot-token' not in row.bot_key
    assert store.bot_key != NotificationStore('another-bot', 42).bot_key


@pytest.mark.asyncio
async def test_delivery_preserves_html_buttons_and_message_id():
    bot = PersonalTelegramBot(TradingService(broker=AsyncMock()))
    bot.app = NS(bot=NS(send_message=AsyncMock(return_value=NS(message_id=123))))
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton('Approve', callback_data='approve_1')]])
    result = await bot._deliver_notification(claim(text='Hello <b>world</b>', reply_markup=buttons.to_dict()))
    assert result == 123
    arguments = bot.app.bot.send_message.await_args.kwargs
    assert arguments['chat_id'] == 42 and arguments['text'] == 'Hello <b>world</b>'
    assert arguments['reply_markup'] == buttons


@pytest.mark.asyncio
async def test_lifecycle_drains_worker_before_shutting_down_telegram_transport():
    events = []
    bot = PersonalTelegramBot(TradingService(broker=AsyncMock()))
    async def app_stop():
        events.append('app-stop')
    async def worker_stop():
        events.append('worker-stop')
    async def shutdown():
        events.append('transport-shutdown')
    bot.app = NS(running=True, updater=NS(running=True, stop=AsyncMock(), start_polling=AsyncMock()),
        start=AsyncMock(), stop=AsyncMock(side_effect=app_stop), shutdown=AsyncMock(side_effect=shutdown))
    bot._notification_worker = NS(start=MagicMock(), stop=AsyncMock(side_effect=worker_stop))
    await bot.start()
    bot._notification_worker.start.assert_called_once()
    await bot.stop()
    assert events == ['app-stop', 'worker-stop', 'transport-shutdown']
