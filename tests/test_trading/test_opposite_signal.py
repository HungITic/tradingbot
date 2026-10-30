"""Opposite-entry reductions use remaining logical volume and durable signal identity."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import AccountInfo, OrderResult, PositionInfo, SymbolInfo, TickInfo
from app.models.risk import RiskEvent
from app.models.signal import Signal
from app.signal.parser import SignalParser
from app.trading.risk import RiskConfig
from app.trading.service import TradingService


def position(ticket=1, volume='1.5', side='BUY', symbol='XAUUSD', comment='bot-market'):
    return PositionInfo(ticket, symbol, D(volume), D('2650'), D('2650'), D('2640'), D('2670'),
                        D('0'), D('0'), D('0'), side, datetime.now(timezone.utc), comment=comment)


def opening(pos, signal_id=10):
    return NS(mt5_ticket=pos.ticket, signal_id=signal_id, comment=pos.comment, volume=pos.volume,
              status='FILLED', symbol=pos.symbol)


@pytest.fixture
def opposite(monkeypatch):
    live, rows, events, added = [], [], [], []
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = 300
    result.all.return_value = []
    session.execute.return_value = result
    session.scalar.return_value = 1
    selected = MagicMock()
    selected.all.side_effect = lambda: list(rows)
    session.scalars.return_value = selected
    def add(obj):
        added.append(obj)
        if isinstance(obj, RiskEvent) and obj.event_type == 'OPPOSITE_SIGNAL_HALF':
            events.append('intent')
    session.add = MagicMock(side_effect=add)
    async def flush():
        for obj in added:
            if isinstance(obj, Signal) and obj.id is None:
                obj.id = 20
    session.flush.side_effect = flush
    @asynccontextmanager
    async def db():
        yield session
        events.append('commit')
    monkeypatch.setattr('app.config.database.get_session', db)
    broker = AsyncMock()
    broker.is_connected.return_value = True
    broker.get_account_info.return_value = AccountInfo(1, D('10000'), D('10000'), D('0'), D('10000'), 100, 'Demo', 'USD', raw={'margin_mode': 2})
    broker.get_positions.side_effect = lambda: [replace(p) for p in live]
    broker.get_symbol_info.return_value = SymbolInfo('XAUUSD', 2, D('.01'), D('.01'), D('100'), D('.01'), D('100'))
    broker.get_tick.side_effect = lambda symbol: TickInfo(symbol, D('2650'), D('2650'), datetime.now(timezone.utc))
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    async def close(ticket, volume):
        events.append('close')
        for index, pos in enumerate(live):
            if pos.ticket == ticket:
                live[index] = replace(pos, volume=pos.volume-volume)
                if live[index].volume == 0:
                    live.pop(index)
                return OrderResult(True, ticket=ticket, volume=volume, retcode=10009)
        raise AssertionError('Unknown position')
    broker.close_position.side_effect = close
    async def opened(req):
        events.append('open')
        return OrderResult(True, ticket=100 + broker.open_order.await_count, volume=req.volume, retcode=10009)
    broker.open_order.side_effect = opened
    service = TradingService(broker, risk_config=RiskConfig(max_open_positions=20))
    service._record_db_order = AsyncMock()
    service._insert_management_action = AsyncMock()
    service.cancel_dual_entry_pending_orders_on_close = AsyncMock()
    service.notify = AsyncMock()
    service.set_signal_status = AsyncMock()
    return service, live, rows, events, session, result


def entry(side='SELL'):
    return SignalParser().parse(f'{side} XAUUSD 2650\nSL: {2660 if side == "SELL" else 2640}\nTP: {2630 if side == "SELL" else 2670}').new_order


@pytest.mark.asyncio
@pytest.mark.parametrize('new_side,old_side', [('SELL', 'BUY'), ('BUY', 'SELL')])
async def test_only_opposite_managed_exact_symbol_is_halved(opposite, new_side, old_side):
    svc, live, rows, events, _, _ = opposite
    live.extend([position(side=old_side), position(2, side=new_side),
                 position(3, side=old_side, symbol='EURUSD'), position(4, side=old_side),
                 position(5, side=old_side)])
    rows.extend([opening(live[0]), opening(live[1]), opening(live[2]), opening(live[4], None)])
    assert await svc.close_opposite_signal_half(entry(new_side), 20)
    svc.broker.close_position.assert_awaited_once_with(1, D('.75'))
    intent = events.index('intent')
    assert intent < events.index('commit', intent) < events.index('close')
    svc.cancel_dual_entry_pending_orders_on_close.assert_awaited_once()
    audit = svc._insert_management_action.call_args.args
    assert audit[0] == 20 and audit[3]['reason'] == 'OPPOSITE_SIGNAL_HALF'
    assert audit[5]['close_cause'] == 'OPPOSITE_SIGNAL_HALF'


@pytest.mark.asyncio
async def test_repeat_halves_remaining_even_when_repeat_and_percent_settings_differ(opposite):
    svc, live, rows, _, _, _ = opposite
    svc.risk_config.allow_multiple_partial_closes = False
    svc.risk_config.default_partial_close_percent = D('30')
    svc.risk_config.bad_entry_partial_close_percent = D('100')
    live.append(replace(position(volume='.75'), initial_volume=D('1.5')))
    rows.append(opening(position()))
    await svc.close_opposite_signal_half(entry(), 20)
    assert live[0].volume == D('.38')
    await svc.close_opposite_signal_half(entry(), 21)
    assert live[0].volume == D('.19')
    assert [call.args[1] for call in svc.broker.close_position.await_args_list] == [D('.37'), D('.19')]


@pytest.mark.asyncio
async def test_native_tp_legs_use_total_branch_volume_and_tp1_first(opposite):
    svc, live, rows, _, _, _ = opposite
    live.extend([position(1, '.45', comment='bot-min-entry:tp1'),
                 position(2, '1.05', comment='bot-min-entry:tp2')])
    rows.extend(opening(p) for p in live)
    await svc.close_opposite_signal_half(entry(), 20)
    assert [call.args for call in svc.broker.close_position.await_args_list] == [(1, D('.45')), (2, D('.30'))]
    assert sum(p.volume for p in live) == D('.75')
    await svc.close_opposite_signal_half(entry(), 21)
    assert svc.broker.close_position.await_args.args == (2, D('.37'))
    assert live[0].volume == D('.38')


@pytest.mark.asyncio
async def test_minimum_lot_is_kept_instead_of_closed_in_full(opposite):
    svc, live, rows, _, _, _ = opposite
    live.append(position(volume='.01'))
    rows.append(opening(live[0]))
    assert await svc.close_opposite_signal_half(entry(), 20)
    svc.broker.close_position.assert_not_awaited()
    assert svc._insert_management_action.call_args.args[4] == 'SKIPPED'


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [OrderResult(False, comment='Failed'),
    OrderResult(False, raw={'uncertain': True}),
    OrderResult(True, volume=D('.1'), retcode=10010)])
async def test_failed_or_partial_close_stops_remaining_targets(opposite, result):
    svc, live, rows, _, _, _ = opposite
    live.extend([position(), position(2)])
    rows.extend(opening(p) for p in live)
    svc.broker.close_position.side_effect = None
    svc.broker.close_position.return_value = result
    assert await svc.close_opposite_signal_half(entry(), 20) is False
    assert svc.broker.close_position.await_count == 1
    if result.raw.get('uncertain'):
        assert not svc.risk_config.enabled


@pytest.mark.asyncio
async def test_intent_commit_failure_prevents_mt5_close(opposite, monkeypatch):
    svc, live, rows, _, session, _ = opposite
    live.append(position())
    rows.append(opening(live[0]))
    @asynccontextmanager
    async def broken_db():
        yield session
        raise RuntimeError('commit failed')
    monkeypatch.setattr('app.config.database.get_session', broken_db)
    with pytest.raises(RuntimeError, match='commit failed'):
        await svc.close_opposite_signal_half(entry(), 20)
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_closes_before_entry_and_duplicate_does_not_close_twice(opposite):
    svc, live, rows, events, _, claim = opposite
    live.append(position())
    rows.append(opening(live[0]))
    claim.scalar_one_or_none.side_effect = [300, None]
    raw = 'SELL XAUUSD 2650\nSL: 2660\nTP: 2630'
    await svc.process_raw_message(raw, chat_id=-1001, message_id=300)
    assert events.index('close') < events.index('open')
    await svc.process_raw_message(raw, chat_id=-1001, message_id=300)
    svc.broker.close_position.assert_awaited_once_with(1, D('.75'))


@pytest.mark.asyncio
async def test_approval_after_reduction_only_opens_and_never_halves_again(opposite):
    svc, live, rows, _, _, _ = opposite
    svc.risk_config.max_open_positions = 1
    live.append(position())
    rows.append(opening(live[0]))
    await svc.process_raw_message('SELL XAUUSD 2650\nSL: 2660\nTP: 2630')
    svc.broker.close_position.assert_awaited_once_with(1, D('.75'))
    svc.broker.open_order.assert_not_awaited()
    assert svc._pending_overrides[20]['allowed_rule'] == 'max_positions'
    result = await svc.execute_override_order(20, 'min')
    assert '✅' in result
    assert svc.broker.open_order.await_count == 1
    assert svc.broker.close_position.await_count == 1
    await svc.execute_override_order(20, 'min')
    assert svc.broker.open_order.await_count == 1


@pytest.mark.asyncio
async def test_pipeline_aborts_entry_when_reduction_fails(opposite):
    svc, live, rows, _, _, _ = opposite
    live.append(position())
    rows.append(opening(live[0]))
    svc.broker.close_position.side_effect = None
    svc.broker.close_position.return_value = OrderResult(False, comment='Market closed')
    await svc.process_raw_message('SELL XAUUSD 2650\nSL: 2660\nTP: 2630')
    assert svc.broker.close_position.await_count == 1
    svc.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_rounding_is_once_per_branch(opposite):
    svc, live, rows, _, _, _ = opposite
    live.extend([position(1, '.03', comment='bot-min-entry:tp1'),
                 position(2, '.03', comment='bot-min-entry:tp2')])
    rows.extend(opening(p) for p in live)
    await svc.close_opposite_signal_half(entry(), 20)
    svc.broker.close_position.assert_awaited_once_with(1, D('.03'))
    assert sum(p.volume for p in live) == D('.03')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['paused', 'invalid', 'disconnected', 'expired'])
async def test_pipeline_does_not_close_when_inactive_or_invalid(opposite, mode):
    from datetime import timedelta
    svc, live, rows, _, _, _ = opposite
    live.append(position())
    rows.append(opening(live[0]))
    kwargs = {}
    raw = 'SELL XAUUSD 2650\nSL: 2660\nTP: 2630'
    if mode == 'paused':
        svc.risk_config.enabled = False
    elif mode == 'disconnected':
        svc.broker.is_connected.return_value = False
    elif mode == 'invalid':
        raw = 'SELL XAUUSD 2650\nSL: 2640\nTP: 2630'
    else:
        kwargs['signal_time'] = datetime.now(timezone.utc)-timedelta(days=1)
    await svc.process_raw_message(raw, **kwargs)
    svc.broker.close_position.assert_not_awaited()
    svc.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_preview_displays_half_without_mutations(opposite):
    svc, live, rows, events, session, _ = opposite
    live.append(position(volume='.75'))
    rows.append(opening(live[0]))
    preview = await svc.preview_signal('SELL XAUUSD 2650\nSL: 2660\nTP: 2630', skip_signal_expiry_check=True)
    assert preview['can_execute']
    assert 'Signal ngược chiều' in preview['text'] and '0.37' in preview['text']
    svc.broker.close_position.assert_not_awaited()
    svc.broker.open_order.assert_not_awaited()
    session.add.assert_not_called()
