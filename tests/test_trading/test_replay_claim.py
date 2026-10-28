from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.trading.replay import reclaim_unexecuted_message


def session_for(attempts, context=None, effects=False):
    receipt = SimpleNamespace(context=context, raw_message='old')
    session = AsyncMock()
    session.scalar.side_effect = [receipt, effects]
    result = MagicMock()
    result.all.return_value = attempts
    session.execute.return_value = result
    return session, receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['REJECTED', 'UNKNOWN', 'SKIPPED', 'EXPIRED'])
async def test_retry_terminal_unexecuted_message_preserves_reservation(status):
    session, receipt = session_for([(12, status), (10, 'REJECTED')])
    assert await reclaim_unexecuted_message(session, 1, 2, 'new', {'skip_signal_expiry_check': True})
    assert receipt.context == {'skip_signal_expiry_check': True, 'retry_after_signal_id': 12}
    assert receipt.raw_message == 'new'
    lock_sql = str(session.scalar.call_args_list[0].args[0].compile(dialect=postgresql.dialect()))
    assert 'FOR UPDATE' in lock_sql
    guard_sql = str(session.scalar.call_args_list[1].args[0].compile(dialect=postgresql.dialect()))
    for table in ('orders', 'broker_commands', 'management_actions', 'risk_events'):
        assert table in guard_sql


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['RECEIVED', 'AWAITING_APPROVAL', 'EXECUTED', 'PARTIAL', 'ERROR'])
async def test_retry_blocks_any_previous_non_safe_attempt(status):
    session, receipt = session_for([(12, 'REJECTED'), (10, status)])
    assert not await reclaim_unexecuted_message(session, 1, 2, 'new', {})
    assert receipt.raw_message == 'old'


@pytest.mark.asyncio
@pytest.mark.parametrize('attempts,context,effects', [
    ([], None, False),
    ([(12, 'REJECTED')], {'retry_after_signal_id': 12}, False),
    ([(12, 'REJECTED')], None, True),
])
async def test_retry_blocks_missing_audit_interrupted_retry_or_trading_effects(attempts, context, effects):
    session, receipt = session_for(attempts, context, effects)
    assert not await reclaim_unexecuted_message(session, 1, 2, 'new', {})
    assert receipt.raw_message == 'old'


@pytest.mark.asyncio
async def test_retry_can_repeat_after_new_attempt_was_safely_rejected():
    session, _ = session_for([(13, 'REJECTED'), (12, 'REJECTED')], {'retry_after_signal_id': 12})
    assert await reclaim_unexecuted_message(session, 1, 2, 'new', {})


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_replay', [False, True])
async def test_only_explicit_replay_can_reclaim_duplicate(monkeypatch, explicit_replay):
    from contextlib import asynccontextmanager
    from app.trading.service import TradingService
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result
    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    reclaim = AsyncMock(return_value=True)
    monkeypatch.setattr('app.trading.replay.reclaim_unexecuted_message', reclaim)
    service = TradingService(broker=AsyncMock())
    service._process_raw_message = AsyncMock()
    service.notify = AsyncMock()
    await service.process_raw_message('BUY', chat_id=1, message_id=2,
                                      skip_signal_expiry_check=explicit_replay)
    assert reclaim.await_count == int(explicit_replay)
    assert service._process_raw_message.await_count == int(explicit_replay)
