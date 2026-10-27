"""Broker-native TP regressions: sizing, failures, restart, grouping and exact scope."""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.broker.base import AccountInfo, OrderRequest, OrderResult, PositionInfo, SymbolInfo, DealInfo, ClosedPositionInfo, PartialCloseDealInfo, TickInfo, PendingOrderInfo
from app.signal.parser import SignalParser
from app.trading.risk import RiskConfig
from app.trading.service import TradingService

NOW = datetime.now(timezone.utc)


def request(**updates):
    return replace(OrderRequest('XAUUSD', 'BUY', D('1.5'), D('2650'), D('2640'), D('2690'),
                                D('2670'), D('2690'), comment='bot-min-entry', signal_id=10), **updates)


def position(ticket, volume, suffix='tp1', entry='2650'):
    return PositionInfo(ticket, 'XAUUSD', D(volume), D(entry), D('2675'), D('2640'),
                        D('2670') if suffix == 'tp1' else D('2690'), D('5'), D('0'), D('0'),
                        'BUY', NOW, comment='bot-min-entry:' + suffix)


def order(ticket, volume, suffix='tp1', signal_id=10, branch='bot-min-entry'):
    return NS(mt5_ticket=ticket, signal_id=signal_id, symbol='XAUUSD', volume=D(volume),
              price=D('2650'), sl=D('2640'), tp=D('2670') if suffix == 'tp1' else D('2690'),
              tp1=D('2670'), tp2=D('2690'), comment=branch + ':' + suffix, status='FILLED')


@pytest.fixture
def native(monkeypatch):
    rows, live = [], []
    session = AsyncMock()
    query = MagicMock()
    query.all.side_effect = lambda: list(rows)
    session.scalars.return_value = query
    result = MagicMock()
    result.all.return_value = []
    session.execute.return_value = result

    @asynccontextmanager
    async def db():
        yield session
    monkeypatch.setattr('app.config.database.get_session', db)
    broker = AsyncMock()
    broker.get_account_info.return_value = AccountInfo(1, D('10000'), D('10000'), D('0'), D('10000'), 100, 'Demo', 'USD', raw={'margin_mode': 2})
    broker.get_symbol_info.return_value = SymbolInfo('XAUUSD', 2, D('.01'), D('.01'), D('100'), D('.01'), D('100'))
    broker.get_positions.side_effect = lambda: list(live)
    broker.get_pending_orders.return_value = []
    broker.get_history_deals.return_value = []
    broker.get_tick.return_value = TickInfo('XAUUSD', D('2675'), D('2675.1'), NOW)
    broker.open_order.side_effect = lambda req: OrderResult(True, ticket=100 + broker.open_order.await_count,
                                                          retcode=10009, volume=req.volume, price=req.price)

    async def close(ticket, volume):
        target = next(p for p in live if p.ticket == ticket)
        target.volume -= volume
        if target.volume == 0:
            live.remove(target)
        return OrderResult(True, ticket=ticket, retcode=10009, volume=volume)
    broker.close_position.side_effect = close
    broker.modify_position.return_value = OrderResult(True, price=D('2650'))
    svc = TradingService(broker=broker, risk_config=RiskConfig(default_lot='1.5', lot_min_entry='1.5', lot_max_entry='1.5', max_lot='2'))
    svc.notify = AsyncMock()
    svc._record_db_order = AsyncMock()
    svc.save_risk_config_to_db = AsyncMock(return_value=True)
    svc._insert_management_action = AsyncMock()
    svc.set_signal_status = AsyncMock()
    svc.management_history = AsyncMock(return_value=(set(), set()))
    svc.cancel_dual_entry_pending_orders_on_close = AsyncMock()
    return svc, rows, live


@pytest.mark.asyncio
@pytest.mark.parametrize('ratio,volumes,targets', [
    ('0', ['1.5'], ['2690']), ('30', ['.45', '1.05'], ['2670', '2690']),
    ('100', ['1.5'], ['2670']),
])
async def test_native_sizes_and_server_targets(native, ratio, volumes, targets):
    svc, _, _ = native
    svc.risk_config.default_partial_close_percent = D(ratio)
    result = await svc.open_entry_branch(request())
    assert result.success and result.volume == D('1.5')
    sent = [c.args[0] for c in svc.broker.open_order.call_args_list]
    assert [r.volume for r in sent] == list(map(D, volumes))
    assert [r.tp for r in sent] == list(map(D, targets))
    assert all(r.signal_id == 10 and r.sl == D('2640') for r in sent)
    assert svc._record_db_order.await_count == len(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', [None, 0, 1])
async def test_netting_or_unproven_mode_rejected_before_sends(native, mode):
    svc, _, _ = native
    svc.broker.get_account_info.return_value.raw = {'margin_mode': mode}
    with pytest.raises(ValueError, match='hedging'):
        await svc.open_entry_branch(request())
    svc.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_insufficient_lot_never_silently_changes_ratio(native):
    svc, _, _ = native
    with pytest.raises(ValueError, match='Không đủ lot'):
        await svc.open_entry_branch(request(volume=D('.01')))
    svc.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_rounding_keeps_total_volume(native):
    svc, _, _ = native
    svc.risk_config.default_partial_close_percent = D('33')
    legs = await svc.prepare_native_orders(request(volume=D('.10')))
    assert [r.volume for r in legs] == [D('.03'), D('.07')]


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['reject', 'unknown', 'partial'])
async def test_half_opened_branch_stops_without_replaying(native, kind):
    svc, _, _ = native
    success = OrderResult(True, ticket=1, volume=D('.75'), price=D('2650'), retcode=10009)
    failure = OrderResult(False, ticket=2, comment='failed', raw={'uncertain': kind == 'unknown'})
    if kind == 'partial':
        failure = OrderResult(True, ticket=2, volume=D('.25'), price=D('2650'), retcode=10010)
    svc.broker.open_order.side_effect = [success, failure]
    result = await svc.open_entry_branch(request())
    assert not result.success and result.raw['partial_branch']
    assert not svc.risk_config.enabled
    assert svc.broker.open_order.await_count == 2


@pytest.mark.asyncio
async def test_pending_entries_get_separate_native_targets(native):
    svc, _, _ = native
    svc.risk_config.default_partial_close_percent = D('30')
    result = await svc.open_entry_branch(request(order_type='BUY_LIMIT', price=D('2648'), comment='bot-max-entry'))
    assert result.success
    legs = [c.args[0] for c in svc.broker.open_order.call_args_list]
    assert [r.comment for r in legs] == ['bot-max-entry:tp1', 'bot-max-entry:tp2']
    assert all(r.price == D('2648') and r.order_type == 'BUY_LIMIT' for r in legs)


@pytest.mark.asyncio
async def test_restart_groups_by_exact_signal_and_branch(native):
    svc, rows, live = native
    live += [position(1, '.03'), position(2, '.07', 'tp2'), position(3, '.1', 'tp2')]
    rows += [order(1, '.03'), order(2, '.07', 'tp2'), order(3, '.1', 'tp2', signal_id=20)]
    groups = await svc.get_open_positions()
    assert len(groups) == 2
    assert groups[0].volume == D('.10') and groups[0].initial_volume == D('.10')
    assert [p.ticket for p in groups[0].members] == [1, 2]
    assert groups[0].profit == D('10')


@pytest.mark.asyncio
async def test_bad_entry_30_percent_is_of_whole_remaining_branch(native):
    svc, rows, live = native
    live += [position(1, '.03'), position(2, '.07', 'tp2')]
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    svc.risk_config.bad_entry_partial_close_percent = D('30')
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt entry xấu'), 30, None, None)
    svc.broker.close_position.assert_awaited_once_with(1, D('.03'))
    # Closed TP1 child is never sent an SL modification.
    svc.broker.modify_position.assert_awaited_once_with(2, sl=D('2650.00'))
    assert len(live) == 1 and live[0].volume == D('.07')


@pytest.mark.asyncio
async def test_bad_entry_full_exit_closes_both_children(native):
    svc, rows, live = native
    live += [position(1, '.03'), position(2, '.07', 'tp2')]
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    svc.risk_config.bad_entry_partial_close_percent = D('100')
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt entry xấu'), 30, None, None)
    assert not live
    assert svc.broker.close_position.await_count == 2
    svc.broker.modify_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_detects_prior_partial_across_closed_child(native):
    svc, rows, live = native
    live += [position(2, '.07', 'tp2')]
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    svc.risk_config.bad_entry_partial_close_percent = D('30')
    await svc.process_management_signal(SignalParser().parse('XAUUSD chốt bớt entry xấu'), 30, None, None)
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_price_polling_close_for_native_tp_even_beyond_tp2(native):
    svc, rows, live = native
    live += [position(1, '.03'), position(2, '.07', 'tp2')]
    for p in live:
        p.current_price = D('3000')
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    await svc.check_tp_milestones()
    svc.broker.close_position.assert_not_awaited()
    svc.broker.modify_position.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,expected', [(5, True), (4, False), (0, False)])
async def test_be_requires_broker_tp_exit_and_exact_opening_order(native, reason, expected):
    svc, rows, live = native
    live += [position(2, '.07', 'tp2', entry='2650.2')]
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    svc.broker.get_history_deals.return_value = [
        DealInfo(11, 1, 9001, 'XAUUSD', 'BUY', 'IN', D('.03'), D('2650'), D('0'), D('0'), D('0'), NOW),
        DealInfo(12, 100, 9001, 'XAUUSD', 'SELL', 'OUT', D('.03'), D('2670'), D('1'), D('0'), D('0'), NOW, reason=reason)]
    await svc.check_tp_milestones()
    assert bool(svc.broker.modify_position.await_count) == expected
    if expected:
        svc.broker.modify_position.assert_awaited_once_with(2, sl=D('2650.20'))
        await svc.check_tp_milestones()
        assert svc.broker.modify_position.await_count == 1
    svc.broker.close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_combines_children_and_marks_remaining_tp2(native):
    svc, rows, live = native
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    live += [position(2, '.07', 'tp2')]
    first = ClosedPositionInfo(1, 'XAUUSD', 'BUY', D('.03'), D('2650'), D('2670'), D('10'), D('0'), D('0'), D('10'), NOW, NOW,
                               total_volume=D('.03'), opening_order_tickets=(1,),
                               partial_deals=[PartialCloseDealInfo(11, D('.03'), D('2670'), D('10'), NOW)])
    grouped = await svc.group_native_history([first], live, True)
    assert len(grouped) == 1 and grouped[0].volume == D('.03') and grouped[0].total_volume == D('.10')
    assert grouped[0].is_fully_closed is False
    second = replace(first, ticket=2, volume=D('.07'), total_volume=D('.07'), net_pnl=D('20'), opening_order_tickets=(2,),
                     partial_deals=[PartialCloseDealInfo(12, D('.07'), D('2690'), D('20'), NOW)])
    grouped = await svc.group_native_history([first, second], [], True)
    assert len(grouped) == 1 and grouped[0].volume == D('.10') and grouped[0].net_pnl == D('30')
    assert grouped[0].is_fully_closed and len(grouped[0].partial_deals) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('closed_branch,expected', [('bot-min-entry:tp1', 2), ('bot-max-entry:tp1', 0)])
async def test_min_exit_cancels_both_pending_max_legs_only(native, closed_branch, expected):
    svc, rows, live = native
    svc.cancel_dual_entry_pending_orders_on_close = TradingService.cancel_dual_entry_pending_orders_on_close.__get__(svc)
    svc.signal_sibling_tickets = AsyncMock(return_value={3, 4})
    svc._ticket_to_entry_comment[1] = closed_branch
    svc.broker.get_pending_orders.return_value = [
        PendingOrderInfo(t, 'XAUUSD', 'BUY_LIMIT', D('.05'), D('2645'), D('2640'), D('2690'),
                         comment='bot-max-entry:' + suffix, setup_time=NOW)
        for t, suffix in [(3, 'tp1'), (4, 'tp2')]]
    svc.broker.cancel_order.return_value = OrderResult(True)
    svc._update_db_order_status = AsyncMock()
    cancelled = await svc.cancel_dual_entry_pending_orders_on_close(1)
    assert len(cancelled) == expected
    assert svc.broker.cancel_order.await_count == expected


@pytest.mark.asyncio
async def test_pending_ack_zero_volume_is_not_treated_as_partial_fill(native):
    svc, _, _ = native
    svc.broker.open_order.side_effect = lambda req: OrderResult(True, ticket=100 + svc.broker.open_order.await_count,
                                                               volume=D('0'), price=D('0'), retcode=10008)
    result = await svc.open_entry_branch(request(order_type='BUY_LIMIT', price=D('2648'), comment='bot-max-entry'))
    assert result.success and result.volume == D('1.5')
    assert svc.risk_config.enabled


@pytest.mark.asyncio
async def test_be_retry_uses_persisted_completion_after_restart(native):
    svc, rows, live = native
    live += [position(2, '.07', 'tp2')]
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    svc.management_history.return_value = (set(), {2})
    await svc.check_tp_milestones()
    svc.broker.modify_position.assert_not_awaited()
    svc.broker.get_history_deals.assert_not_awaited()


@pytest.mark.asyncio
async def test_crash_guard_committed_before_first_send_and_released_after_complete(native):
    svc, _, _ = native
    events = []
    async def save(**kwargs):
        config = kwargs.get('config', svc.risk_config)
        events.append(('persist', config.enabled))
        return True
    async def send(req):
        events.append(('send', req.comment))
        return OrderResult(True, ticket=len(events), retcode=10009, volume=req.volume, price=req.price)
    svc.save_risk_config_to_db.side_effect = save
    svc.broker.open_order.side_effect = send
    result = await svc.open_entry_branch(request())
    assert result.success
    assert events == [('persist', False), ('send', 'bot-min-entry:tp1'), ('send', 'bot-min-entry:tp2'), ('persist', True)]


@pytest.mark.asyncio
async def test_guard_failure_sends_nothing(native):
    svc, _, _ = native
    svc.save_risk_config_to_db.return_value = False
    assert not (await svc.open_entry_branch(request())).success
    svc.broker.open_order.assert_not_awaited()
    assert not svc.risk_config.enabled


@pytest.mark.asyncio
async def test_reply_after_tp1_exit_targets_remaining_member(native):
    svc, rows, live = native
    rows += [order(1, '.03'), order(2, '.07', 'tp2')]
    live += [position(2, '.07', 'tp2')]
    svc.get_tickets_for_reply_message = AsyncMock(return_value=[1, 2])
    svc.risk_config.bad_entry_partial_close_percent = D('100')
    await svc.process_management_signal(SignalParser().parse('chốt bớt entry xấu'), 30, 123, None)
    svc.broker.close_position.assert_awaited_once_with(2, D('.07'))


@pytest.mark.asyncio
async def test_native_two_branches_count_four_positions_before_send(native):
    svc, _, _ = native
    svc.risk_config.max_open_positions = 3
    svc.broker.is_connected.return_value = True
    svc.broker.get_tick.return_value = TickInfo('XAUUSD', D('2650'), D('2650'), NOW)
    result = await svc.preview_signal('BUY XAUUSD 2650 - 2648\nSL: 2640\nTP1: 2670\nTP2: 2690')
    assert not result['can_execute']
    svc.broker.open_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_sl_equal_to_average_is_not_changed_to_individual_entries(native):
    svc, rows, live = native
    live += [position(1, '.05', entry='2650'), position(2, '.05', 'tp2', entry='2652')]
    rows += [order(1, '.05'), order(2, '.05', 'tp2')]
    await svc.process_management_signal(SignalParser().parse('XAUUSD dời SL về 2651'), 30, None, None)
    assert [call.kwargs['sl'] for call in svc.broker.modify_position.call_args_list] == [D('2651.00'), D('2651.00')]
