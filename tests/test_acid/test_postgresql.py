"""Opt-in tests against a disposable PostgreSQL database; never use the trading DB.

Run with ACID_TEST_DB_NAME=codex_acid_... and ACID_TEST_DB_HOST=... .
Credentials come from the existing DB settings and are never printed.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal as D
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.broker.base import DealInfo, OrderRequest
from app.broker.mt5.journal import send_journaled, UnresolvedBrokerCommand
from app.config import database
from app.config.settings import settings
from app.models import Base, BrokerCommand, Order, SignalReceipt, SignalSource, Trade
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


@pytest_asyncio.fixture
async def pg(monkeypatch):
    name = os.environ.get('ACID_TEST_DB_NAME', '')
    if not name:
        pytest.skip('Disposable PostgreSQL DB not configured')
    if not name.startswith('codex_acid_') or name == 'trading_bot':
        pytest.fail('Refusing to test against a non-test database')
    url = make_url(settings.db.url).set(database=name, host=os.environ.get('ACID_TEST_DB_HOST', 'localhost'))
    engine = create_async_engine(url, pool_size=5, max_overflow=5)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(database, 'async_session_factory', factory)
    monkeypatch.setattr(database, 'application_lock_pid', None)
    async with engine.begin() as conn:
        assert await conn.scalar(text('SELECT current_database()')) == name
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


def broker_client(response=None):
    api = SimpleNamespace(order_send=MagicMock(return_value=response or {
        'retcode': 10009, 'order': 123, 'deal': 456, 'price': 4182.67, 'volume': .01, 'comment': 'Done'}))
    client = SimpleNamespace(run_in_thread=AsyncMock(side_effect=lambda fn, *a: fn(*a)))
    return client, api


def request():
    return {'action': 1, 'symbol': 'XAUUSD.sc', 'type': 1, 'volume': .01, 'price': 4182.67}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [RuntimeError, asyncio.CancelledError])
async def test_transaction_rolls_back_even_on_task_cancellation(pg, failure):
    with pytest.raises(failure):
        async with database.get_session() as session:
            session.add(SignalSource(name='rollback', chat_id=42))
            await session.flush()
            raise failure()
    async with database.get_session() as session:
        assert await session.scalar(select(func.count()).select_from(SignalSource)) == 0


@pytest.mark.asyncio
async def test_concurrent_deal_import_inserts_exactly_once(pg):
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    deal = DealInfo(456, 123, 7001, 'XAUUSD.sc', 'BUY', 'OUT', D('.01'), D('4183'),
                    D('10'), D('0'), D('0'), datetime.now(timezone.utc))
    outcomes = await asyncio.gather(*(service._insert_trade_record(deal) for _ in range(15)))
    assert outcomes.count(True) == 1
    async with database.get_session() as session:
        assert await session.scalar(select(func.count()).select_from(Trade)) == 1
        trade = await session.scalar(select(Trade))
        assert trade.mt5_position_id == 7001 and trade.mt5_order_ticket == 123


@pytest.mark.asyncio
async def test_broker_ack_and_order_projection_commit_together(pg):
    client, api = broker_client()
    opening = OrderRequest('XAUUSD.sc', 'SELL', D('.01'), D('4182.67'))
    await send_journaled(client, api, request(), opening=opening)
    async with database.get_session() as session:
        command = await session.scalar(select(BrokerCommand))
        order = await session.scalar(select(Order))
        assert command.status == 'CONFIRMED' and order.mt5_ticket == 123
        assert order.volume == D('.01') and order.price == D('4182.67')


@pytest.mark.asyncio
async def test_intent_commit_failure_never_sends_to_mt5(pg):
    client, api = broker_client()
    opening = OrderRequest('XAUUSD.sc', 'SELL', D('.01'), signal_id=999999)  # FK rejects before send.
    with pytest.raises(Exception):
        await send_journaled(client, api, request(), opening=opening)
    client.run_in_thread.assert_not_awaited()
    async with database.get_session() as session:
        assert await session.scalar(select(func.count()).select_from(BrokerCommand)) == 0


@pytest.mark.asyncio
async def test_timeout_leaves_durable_unknown_and_blocks_retry_after_restart(pg):
    client, api = broker_client()
    client.run_in_thread.side_effect = TimeoutError('ack lost')
    with pytest.raises(TimeoutError):
        await send_journaled(client, api, request())
    # Dispose connections to prove the next attempt relies on the DB, not an in-memory set.
    await pg.dispose()
    with pytest.raises(UnresolvedBrokerCommand):
        await send_journaled(client, api, request())
    assert client.run_in_thread.await_count == 1
    async with database.get_session() as session:
        assert (await session.scalar(select(BrokerCommand))).status == 'UNKNOWN'


@pytest.mark.asyncio
async def test_post_send_commit_failure_does_not_lose_intent_or_allow_resend(pg):
    client, api = broker_client()
    original = database.get_session
    calls = 0
    @asynccontextmanager
    async def fail_confirmation():
        nonlocal calls
        calls += 1
        async with original() as session:
            yield session
            if calls == 2:
                raise RuntimeError('DB failure after broker acknowledgement')
    with patch.object(database, 'get_session', fail_confirmation):
        with pytest.raises(RuntimeError):
            await send_journaled(client, api, request(), opening=OrderRequest('XAUUSD.sc', 'SELL', D('.01')))
    async with original() as session:
        assert (await session.scalar(select(BrokerCommand))).status == 'SUBMITTED'
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
    with pytest.raises(UnresolvedBrokerCommand):
        await send_journaled(client, api, request())
    assert client.run_in_thread.await_count == 1


@pytest.mark.asyncio
async def test_account_commands_are_serialized_across_independent_connections(pg):
    client, api = broker_client()
    active = peak = 0
    async def send(fn, value):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(.03)
        active -= 1
        return fn(value)
    client.run_in_thread.side_effect = send
    await asyncio.gather(*(send_journaled(client, api, request()) for _ in range(4)))
    assert peak == 1 and client.run_in_thread.await_count == 4


@pytest.mark.asyncio
async def test_durable_claim_includes_original_message_and_only_one_winner(pg):
    from sqlalchemy.dialects.postgresql import insert
    async def claim():
        async with database.get_session() as session:
            row = await session.execute(insert(SignalReceipt).values(chat_id=1, message_id=2,
                raw_message='SELL XAUUSD 4182', context={'reply_to_msg_id': 99})
                .on_conflict_do_nothing().returning(SignalReceipt.message_id))
            return row.scalar_one_or_none()
    outcomes = await asyncio.gather(*(claim() for _ in range(10)))
    assert outcomes.count(2) == 1
    async with database.get_session() as session:
        receipt = await session.scalar(select(SignalReceipt))
        assert receipt.raw_message == 'SELL XAUUSD 4182' and receipt.context['reply_to_msg_id'] == 99


@pytest.mark.asyncio
async def test_concurrent_replay_reclaims_only_one_safe_attempt(pg):
    from app.models.signal import Signal
    from app.trading.replay import reclaim_unexecuted_message
    async with database.get_session() as session:
        source = SignalSource(name='retry', chat_id=42)
        session.add(source)
        await session.flush()
        session.add(Signal(source_id=source.id, telegram_message_id=2,
                           raw_message='BUY', status='REJECTED'))
        session.add(SignalReceipt(chat_id=42, message_id=2, raw_message='BUY'))
    async def retry():
        async with database.get_session() as session:
            return await reclaim_unexecuted_message(session, 42, 2, 'BUY', {})
    outcomes = await asyncio.gather(*(retry() for _ in range(10)))
    assert outcomes.count(True) == 1


@pytest.mark.asyncio
async def test_second_application_is_rejected_and_lost_lease_blocks_sending(pg):
    first = await database.acquire_application_lock()
    try:
        with pytest.raises(RuntimeError, match='Another trading bot'):
            await database.acquire_application_lock()
        client, api = broker_client()
        await first.close()
        with pytest.raises(UnresolvedBrokerCommand, match='quyền thực thi'):
            await send_journaled(client, api, request())
        client.run_in_thread.assert_not_awaited()
    finally:
        await first.close()


@pytest.mark.asyncio
async def test_notification_claim_has_one_winner_and_fifo(pg):
    from app.telegram.notifications import NotificationStore
    store = NotificationStore('test-bot', 42)
    await store.enqueue('first')
    await store.enqueue('second')
    claims = await asyncio.gather(*(store.claim() for _ in range(10)))
    winners = [c for c in claims if c is not None]
    assert len(winners) == 1 and winners[0].text == 'first'
    await store.finish(winners[0], message_id=100)
    assert (await store.claim()).text == 'second'


@pytest.mark.asyncio
async def test_latest_reentry_source_uses_same_chat_and_prior_telegram_id_not_audit_time(pg):
    from app.models.signal import Signal
    from app.telegram.signal_listener import TelegramSignalListener
    async with database.get_session() as session:
        source = SignalSource(name='source', chat_id=42)
        other = SignalSource(name='other', chat_id=43)
        session.add_all([source, other])
        await session.flush()
        # Insert the older message last: DB row ID/received_at must not determine recency.
        selected = Signal(source_id=source.id, telegram_message_id=200,
                          raw_message='SELL', message_type='NEW_ORDER', status='EXECUTED')
        session.add_all([selected,
            Signal(source_id=source.id, telegram_message_id=400, raw_message='BUY', message_type='NEW_ORDER'),
            Signal(source_id=other.id, telegram_message_id=250, raw_message='BUY', message_type='NEW_ORDER'),
            Signal(source_id=source.id, telegram_message_id=299, raw_message='update', message_type='UNKNOWN'),
            Signal(source_id=source.id, telegram_message_id=100, raw_message='BUY', message_type='NEW_ORDER')])
        await session.flush()
        selected_id = selected.id
    listener = TelegramSignalListener(TradingService(broker=AsyncMock()))
    assert await listener.latest_reentry_signal(42, 300) == (selected_id, 200)


@pytest.mark.asyncio
async def test_notification_backoff_cannot_be_overtaken_and_expired_lease_recovers(pg):
    from datetime import timedelta
    from sqlalchemy import update
    from app.models.notification import TelegramNotification
    from app.telegram.notifications import NotificationStore
    store = NotificationStore('test-bot', 42)
    await store.enqueue('first')
    await store.enqueue('second')
    first = await store.claim()
    await store.finish(first, error='offline', retry_delay=60)
    assert await store.claim() is None
    async with database.get_session() as session:
        await session.execute(update(TelegramNotification).where(TelegramNotification.id == first.id)
                              .values(available_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    restarted = NotificationStore('test-bot', 42)
    second_try = await restarted.claim()
    assert second_try.id == first.id and second_try.attempts == 2
    # Simulate a crash after claiming, then recover the durable SENDING row.
    async with database.get_session() as session:
        await session.execute(update(TelegramNotification).where(TelegramNotification.id == first.id)
                              .values(available_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    third_try = await restarted.claim()
    assert third_try.attempts == 3 and third_try.token != second_try.token
    await store.finish(second_try, message_id=101)  # A stale sender cannot overwrite the new owner.
    async with database.get_session() as session:
        row = await session.get(TelegramNotification, first.id)
        assert row.status == 'SENDING' and row.claim_token == third_try.token
    await restarted.finish(third_try, message_id=102)
    assert (await restarted.claim()).text == 'second'


@pytest.mark.asyncio
async def test_notifications_are_scoped_to_bot_and_admin_and_failed_rows_do_not_block(pg):
    from app.telegram.notifications import NotificationStore
    first, other_bot, other_admin = NotificationStore('bot1', 42), NotificationStore('bot2', 42), NotificationStore('bot1', 43)
    for store, text in [(other_bot, 'other bot'), (other_admin, 'other admin'), (first, 'bad'), (first, 'good')]:
        await store.enqueue(text)
    bad = await first.claim()
    assert bad.text == 'bad'
    await first.finish(bad, error='malformed HTML')
    assert (await first.claim()).text == 'good'
    assert (await other_bot.claim()).text == 'other bot'
    assert (await other_admin.claim()).text == 'other admin'


@pytest.mark.asyncio
async def test_notification_enqueue_rollback_does_not_publish_partial_payload(pg, monkeypatch):
    from app.models.notification import TelegramNotification
    from app.telegram.notifications import NotificationStore
    original = database.get_session
    @asynccontextmanager
    async def failing_commit():
        async with original() as session:
            yield session
            raise RuntimeError('commit failed')
    monkeypatch.setattr(database, 'get_session', failing_commit)
    with pytest.raises(RuntimeError, match='commit failed'):
        await NotificationStore('bot', 42).enqueue('not committed')
    async with original() as session:
        assert await session.scalar(select(func.count()).select_from(TelegramNotification)) == 0


@pytest.mark.asyncio
async def test_notification_cleanup_removes_only_old_sent_rows(pg):
    from datetime import timedelta
    from app.models.notification import TelegramNotification
    from app.telegram.notifications import NotificationStore
    store = NotificationStore('bot', 42)
    old = datetime.now(timezone.utc)-timedelta(days=4)
    async with database.get_session() as session:
        for status in ['PENDING', 'SENDING', 'SENT', 'FAILED']:
            session.add(TelegramNotification(bot_key=store.bot_key, chat_id=42, text=status,
                status=status, created_at=old, sent_at=old if status == 'SENT' else None))
    await store.cleanup()
    async with database.get_session() as session:
        rows = (await session.scalars(select(TelegramNotification))).all()
        assert {row.status for row in rows} == {'PENDING', 'SENDING', 'FAILED'}


@pytest.mark.asyncio
async def test_schema_migration_preserves_existing_financial_rows(pg):
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    def old_schema(connection):
        Base.metadata.drop_all(connection)
        with Operations.context(MigrationContext.configure(connection)):
            for name in ['6fc62769571a_initial_schema', 'b2c3d4e5f6a7_add_tp1_tp2_to_orders', 'c3d4e5f6a7b8_signal_receipts']:
                import_module('migrations.versions.' + name).upgrade()
    async with pg.begin() as conn:
        await conn.run_sync(old_schema)
        await conn.execute(text("INSERT INTO trades (mt5_ticket, symbol, volume, price, profit) VALUES (456, 'XAUUSD.sc', .01, 4182, 369.32)"))
        def upgrade(connection):
            with Operations.context(MigrationContext.configure(connection)):
                import_module('migrations.versions.d4e5f6a7b8c9_execution_journal').upgrade()
                import_module('migrations.versions.e5f6a7b8c9d0_telegram_notifications').upgrade()
        await conn.run_sync(upgrade)
        assert await conn.scalar(text('SELECT profit FROM trades WHERE mt5_ticket = 456')) == D('369.32')
        assert await conn.scalar(text("SELECT to_regclass('broker_commands')")) == 'broker_commands'
        assert await conn.scalar(text("SELECT to_regclass('telegram_notifications')")) == 'telegram_notifications'
        await conn.execute(text("INSERT INTO telegram_notifications (bot_key, chat_id, text, status, attempts) VALUES ('test', 42, 'pending', 'PENDING', 0)"))
        def upgrade_again(connection):
            with Operations.context(MigrationContext.configure(connection)):
                import_module('migrations.versions.e5f6a7b8c9d0_telegram_notifications').upgrade()
        await conn.run_sync(upgrade_again)
        assert await conn.scalar(text('SELECT count(*) FROM telegram_notifications')) == 1
