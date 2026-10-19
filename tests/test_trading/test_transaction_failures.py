"""Failure boundaries which must not publish state or report false success."""
import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal as D
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.broker.mt5.client import MT5Client
from app.broker.base import OrderRequest, OrderResult
from app.trading.service import TradingService
from app.trading.risk import RiskConfig


@pytest.mark.asyncio
async def test_rule_commit_failure_keeps_parser_unchanged_and_reports_failure():
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    session = AsyncMock()
    session.add = MagicMock()
    session.execute.return_value = MagicMock()
    session.execute.return_value.scalar_one_or_none.return_value = None
    before = list(service.parser.rule_engine._rules)
    @asynccontextmanager
    async def fail_commit():
        yield session
        raise RuntimeError('commit failed')
    with patch('app.config.database.get_session', fail_commit):
        assert not await service.add_or_update_management_rule('acid-test', 'PARTIAL_CLOSE', D('40'))
        assert service.parser.rule_engine._rules == before
        assert not await service.remove_management_rule(before[0].keyword)
        assert service.parser.rule_engine._rules == before


@pytest.mark.asyncio
async def test_new_config_is_not_visible_until_persistence_finishes():
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    entered, finish = asyncio.Event(), asyncio.Event()
    async def save(**kwargs):
        assert kwargs['config'].default_lot == D('.02')
        entered.set()
        await finish.wait()
        return True
    service.save_risk_config_to_db = AsyncMock(side_effect=save)
    task = asyncio.create_task(service.update_config_keys({'default_lot': '.02'}))
    await entered.wait()
    assert service.risk_config.default_lot == D('.01')
    finish.set()
    assert (await task)['default_lot']
    assert service.risk_config.default_lot == D('.02')


@pytest.mark.asyncio
async def test_cancelled_bridge_call_does_not_release_io_lane_early():
    client = MT5Client()
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    second = MagicMock(return_value='second')
    def first():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(2)
        return 'first'
    task = asyncio.create_task(client.run_in_thread(first))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    next_call = asyncio.create_task(client.run_in_thread(second))
    await asyncio.sleep(.01)
    second.assert_not_called()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await next_call == 'second'


@pytest.mark.asyncio
async def test_order_cache_is_not_published_if_commit_fails():
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    @asynccontextmanager
    async def fail_commit():
        yield AsyncMock()
        raise RuntimeError('commit failed')
    with patch('app.config.database.get_session', fail_commit):
        with pytest.raises(RuntimeError):
            await service._record_db_order(1, OrderResult(True, ticket=123),
                OrderRequest('XAUUSD', 'SELL', D('.01')), message_link='https://t.me/signals/1')
    assert service._order_message_links == {} and service._ticket_to_signal_db_id == {}
    assert not service.risk_config.enabled


@pytest.mark.asyncio
async def test_emergency_pause_wins_over_config_transaction_already_in_flight():
    service = TradingService(broker=AsyncMock(), risk_config=RiskConfig())
    service.broker.get_positions.return_value = []
    service.broker.get_pending_orders.return_value = []
    entered, finish = asyncio.Event(), asyncio.Event()
    committed = []
    async def save(*, config=None, **kwargs):
        if config is not None:
            entered.set()
            await finish.wait()
        committed.append((config if config is not None else service.risk_config).enabled)
        return True
    service.save_risk_config_to_db = AsyncMock(side_effect=save)
    update = asyncio.create_task(service.update_config_keys({'default_lot': '.02'}))
    await entered.wait()
    stop = asyncio.create_task(service.emergency_stop())
    await asyncio.sleep(.01)
    assert not service.risk_config.enabled
    finish.set()
    await update
    report = await stop
    assert committed == [True, False] and report['saved']
    assert not service.risk_config.enabled
