"""Global entry admission limits, including reentry, native legs and approvals."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import AccountInfo, OrderResult, PositionInfo, SymbolInfo, TickInfo
from app.models.signal import Signal
from app.trading.entry_throttle import EntryThrottleStore, throttle_enabled, throttle_problem
from app.trading.risk import RiskConfig
from app.trading.service import TradingService

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
RAW = 'BUY XAUUSD 2650 - 2648\nSL:2640\nTP:2670 - 2680'


@pytest.mark.parametrize('cooldown,cap,window,ages,blocked', [
    (60, 0, 300, [], False), (60, 0, 300, [59.999], True), (60, 0, 300, [60], False),
    (0, 3, 300, [0, 60], False), (0, 3, 300, [0, 60, 299.999], True),
    (0, 3, 300, [0, 60, 300], False), (0, 1, 300, [299], True),
    (0, 1, 300, [300], False), (60, 3, 300, [61, 120, 180], True),
    (60, 3, 300, [1, 120], True), (60, 3, 300, [61, 120], False),
    (60, 0, 300, [-1], True),
])
def test_rolling_window_and_cooldown_boundaries(cooldown, cap, window, ages, blocked):
    cfg = RiskConfig(entry_cooldown_seconds=cooldown, max_entries_per_window=cap, entry_window_seconds=window)
    history = [NOW - timedelta(seconds=age) for age in ages]
    assert bool(throttle_problem(cfg, NOW, history)) is blocked


@pytest.mark.parametrize('field,value', [('entry_cooldown_seconds', -1), ('entry_cooldown_seconds', 1.5),
    ('max_entries_per_window', -1), ('max_entries_per_window', 1.5),
    ('entry_window_seconds', 0), ('entry_window_seconds', -1)])
def test_invalid_limits_are_rejected(field, value):
    with pytest.raises(ValueError):
        RiskConfig(**{field: value})


def test_defaults_disable_both_guards_and_each_guard_can_enable_independently():
    assert not throttle_enabled(RiskConfig())
    assert throttle_enabled(RiskConfig(entry_cooldown_seconds=1))
    assert throttle_enabled(RiskConfig(max_entries_per_window=1))


class MemoryAdmissions:
    """Shared test ledger; service restarts receive a new store over this history."""
    def __init__(self, state=None):
        self.state = state if state is not None else {'now': NOW, 'rows': [], 'lock': asyncio.Lock()}
        self.reservations = 0

    async def check(self, account_key, config):
        return throttle_problem(config, self.state['now'],
            [stamp for key, _, stamp in self.state['rows'] if key == account_key])

    async def reserve(self, account_key, signal_id, config):
        if type(signal_id) is not int or signal_id <= 0:
            raise ValueError('missing signal')
        async with self.state['lock']:
            if any(key == account_key and sid == signal_id for key, sid, _ in self.state['rows']):
                return 'already admitted'
            problem = await self.check(account_key, config)
            if problem:
                return problem
            self.state['rows'].append((account_key, signal_id, self.state['now']))
            self.reservations += 1
            return None


@pytest.fixture
def throttled(monkeypatch):
    session = AsyncMock()
    added = []
    session.add = MagicMock(side_effect=added.append)
    next_id = 1
    async def flush():
        nonlocal next_id
        for obj in added:
            if isinstance(obj, Signal) and obj.id is None:
                obj.id = next_id
                next_id += 1
    session.flush.side_effect = flush
    session.scalar.return_value = 1
    result = MagicMock()
    result.rowcount = 1
    result.all.return_value = []
    session.execute.return_value = result
    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    broker = AsyncMock()
    broker.get_account_info.return_value = AccountInfo(1, D('10000'), D('10000'), D('0'),
        D('10000'), 100, 'Demo', 'USC', raw={'margin_mode': 2})
    broker.is_connected.return_value = True
    broker.get_positions.return_value = []
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    broker.get_symbol_info.side_effect = lambda symbol: SymbolInfo(symbol, 2, D('.01'), D('.01'), D('100'), D('.01'), D('100'))
    broker.get_tick.side_effect = lambda symbol: TickInfo(symbol, D('2650'), D('2650'), datetime.now(timezone.utc))
    broker.open_order.side_effect = lambda req: OrderResult(True, ticket=100 + broker.open_order.await_count,
        volume=req.volume, price=req.price, retcode=10009)
    svc = TradingService(broker=broker, risk_config=RiskConfig(entry_cooldown_seconds=60,
        default_lot='1.5', lot_min_entry='1.5', lot_max_entry='1.5', max_lot='2', max_open_positions=100,
        duplicate_timeout_seconds=0, allowed_symbols=['XAUUSD', 'EURUSD']))
    svc.entry_throttle_store = MemoryAdmissions()
    svc.notify = AsyncMock()
    svc.set_signal_status = AsyncMock()
    svc._record_db_order = AsyncMock()
    svc.save_risk_config_to_db = AsyncMock(return_value=True)
    return NS(svc=svc, broker=broker, session=session)


@pytest.mark.asyncio
@pytest.mark.parametrize('second,is_reentry', [(RAW, False), (RAW, True),
    ('SELL XAUUSD 2650 - 2652\nSL:2660\nTP:2630 - 2620', False),
    (RAW.replace('XAUUSD', 'EURUSD'), False)])
async def test_cooldown_covers_different_signals_symbols_sides_and_rounds(throttled, second, is_reentry):
    svc = throttled.svc
    await svc.process_raw_message(RAW)
    assert svc.broker.open_order.await_count == 4  # Min/Max x TP1/TP2.
    assert svc.entry_throttle_store.reservations == 1
    await svc.process_raw_message(second, is_reentry=is_reentry)
    assert svc.broker.open_order.await_count == 4
    assert 'Chặn vào lệnh liên tục' in svc.notify.call_args.args[0]
    assert svc.set_signal_status.call_args.args[1] == 'REJECTED'
    assert not svc._pending_overrides
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_rolling_cap_limits_consecutive_entries_after_cooldowns(throttled):
    svc = throttled.svc
    svc.risk_config = svc.risk_config.with_updates({'entry_cooldown_seconds': 0, 'max_entries_per_window': 2,
                                                   'entry_window_seconds': 300})
    for _ in range(3):
        await svc.process_raw_message(RAW, is_reentry=True)
        svc.entry_throttle_store.state['now'] += timedelta(seconds=60)
    assert svc.entry_throttle_store.reservations == 2 and svc.broker.open_order.await_count == 8
    svc.entry_throttle_store.state['now'] = NOW + timedelta(seconds=300)
    await svc.process_raw_message(RAW, is_reentry=True)
    assert svc.entry_throttle_store.reservations == 3 and svc.broker.open_order.await_count == 12


@pytest.mark.asyncio
async def test_preview_is_read_only_and_restart_keeps_admission_history(throttled):
    svc = throttled.svc
    assert (await svc.preview_signal(RAW))['can_execute']
    assert not svc.entry_throttle_store.state['rows']
    await svc.process_raw_message(RAW)
    saved = svc.entry_throttle_store.state
    svc._recent_signals.clear()
    svc.entry_throttle_store = MemoryAdmissions(saved)
    assert not (await svc.preview_signal(RAW, is_reentry=True))['can_execute']
    await svc.process_raw_message(RAW, is_reentry=True)
    assert svc.broker.open_order.await_count == 4


@pytest.mark.asyncio
async def test_replay_expiry_bypass_does_not_bypass_entry_rate(throttled):
    svc = throttled.svc
    await svc.process_raw_message(RAW)
    await svc.process_raw_message(RAW, skip_signal_expiry_check=True,
                                 signal_time=datetime.now(timezone.utc) - timedelta(days=30))
    assert svc.broker.open_order.await_count == 4


@pytest.mark.asyncio
async def test_override_rechecks_global_limits_without_bypassing_them(throttled):
    svc = throttled.svc
    # A favorable quote beyond the full entry zone creates the existing approval.
    svc.broker.get_tick.side_effect = lambda symbol: TickInfo(symbol, D('2646'), D('2646'), datetime.now(timezone.utc))
    await svc.process_raw_message(RAW)
    approval_id = next(iter(svc._pending_overrides))
    assert not svc.entry_throttle_store.state['rows']
    svc.broker.get_tick.side_effect = lambda symbol: TickInfo(symbol, D('2650'), D('2650'), datetime.now(timezone.utc))
    await svc.process_raw_message(RAW)
    before = svc.broker.open_order.await_count
    assert 'Chặn vào lệnh liên tục' in await svc.execute_override_order(approval_id, 'both')
    assert svc.broker.open_order.await_count == before
    svc.entry_throttle_store.state['now'] += timedelta(seconds=60)
    assert 'Chặn vào lệnh liên tục' not in await svc.execute_override_order(approval_id, 'both')
    assert svc.broker.open_order.await_count == before + 4
    assert len(svc.entry_throttle_store.state['rows']) == 2


@pytest.mark.asyncio
async def test_rejected_broker_request_keeps_its_admitted_slot(throttled):
    svc = throttled.svc
    svc.broker.open_order.side_effect = None
    svc.broker.open_order.return_value = OrderResult(False, retcode=10006, comment='Rejected')
    raw = 'BUY XAUUSD 2650\nSL:2640\nTP:2670'
    await svc.process_raw_message(raw)
    await svc.process_raw_message(raw, is_reentry=True)
    assert svc.broker.open_order.await_count == 1 and len(svc.entry_throttle_store.state['rows']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['check', 'reserve'])
async def test_database_failure_blocks_sends_and_reservation_failure_blocks_native_legs(throttled, stage):
    svc = throttled.svc
    setattr(svc.entry_throttle_store, stage, AsyncMock(side_effect=RuntimeError('DB unavailable')))
    await svc.process_raw_message(RAW)
    svc.broker.open_order.assert_not_awaited()
    svc.broker.close_position.assert_not_awaited()
    assert svc.set_signal_status.call_args.args[1] == 'REJECTED'


@pytest.mark.asyncio
async def test_management_remains_available_when_entry_limits_are_full(throttled):
    svc = throttled.svc
    await svc.process_raw_message(RAW)
    svc.entry_throttle_store.check = AsyncMock(side_effect=AssertionError('management must not check entry quota'))
    pos = PositionInfo(500, 'XAUUSD', D('1'), D('2650'), D('2670'), D('2640'), D('2690'),
                      D('0'), D('0'), D('0'), 'BUY', datetime.now(timezone.utc))
    svc.broker.get_positions.return_value = [pos]
    svc.position_orders = AsyncMock(return_value={})
    svc.management_history = AsyncMock(return_value=(set(), set()))
    svc._insert_management_action = AsyncMock()
    svc.cancel_dual_entry_pending_orders_on_close = AsyncMock()
    svc.risk_config.auto_breakeven_on_partial = False
    svc.broker.close_position.return_value = OrderResult(True, ticket=500, volume=D('.5'))
    await svc.process_raw_message('XAUUSD chốt bớt')
    svc.broker.close_position.assert_awaited_once_with(500, D('.5'))


@pytest.mark.asyncio
async def test_real_store_commit_failure_cannot_return_an_admission(monkeypatch):
    session = AsyncMock()
    session.scalar.side_effect = [None, NOW]
    rows = MagicMock()
    rows.all.return_value = []
    session.scalars.return_value = rows
    session.add = MagicMock()
    @asynccontextmanager
    async def failing_commit():
        yield session
        raise RuntimeError('commit failed')
    monkeypatch.setattr('app.config.database.get_session', failing_commit)
    with pytest.raises(RuntimeError, match='commit failed'):
        await EntryThrottleStore().reserve('Demo:1', 1, RiskConfig(entry_cooldown_seconds=60))
    assert session.add.call_count == 1
